"""Tests for tasks/cleaners_pkgs.py: stale_driver_packages, apt_autoremove_unused, flatpak_unused.

A tiny model of dpkg/apt (`Host`) answers dpkg-query, apt-get -s, apt-mark, dpkg --verify/--audit, dkms, nvidia-smi,
systemctl and flatpak; /proc is a fake tree (inuse.PROC) and package file lists live in a tmp dir. Nothing touches the
host: an unmocked command returns rc 127, mutating commands are recorded, never run."""
import hashlib
import json
import os
import re
import subprocess

import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint is imported)
import pytest
from test_inuse import mapline, mkproc, with_me

from homelab_maint import core, inuse
from homelab_maint.inuse import Driver
from homelab_maint.tasks import cleaners_pkgs as cp

NOW = 1_800_000_000.0
PROTECTED = {"patterns": ["immich", "plexmediaserver", "postgres", "tunarr", "kometa", "/mnt/backup", "buildkitd"]}
KERNEL = "6.11.0-29-generic"
V580 = "580.173.02-0ubuntu0.24.04.1"
MUTATING = ("apt-get -y", "flatpak --system uninstall --unused -y", "flatpak --user uninstall --unused -y",
            "runuser -u ohmz -- flatpak --user uninstall --unused -y", "apt-get purge", "dpkg --purge", "rm ")


# =========================================================================== a model of the host
class Host:
    """`sh` stand-in. Package names are APT form (native arch bare, foreign `:i386`)."""

    def __init__(self):
        self.pkgs: dict[str, dict] = {}          # name -> {st, ver, size (KiB), ma (multiarch: dpkg-query adds :amd64)}
        self.rdeps: dict[str, set] = {}          # removing name also removes these (reverse dependencies)
        self.autorm: list[str] = []
        self.holds: set[str] = set()
        self.audit = ""
        self.audit_rc = 0
        self.verify = (0, "", "")
        self.dkms = (0, f"nvidia/580.173.02, {KERNEL}, x86_64: installed\n", "")
        self.smi = [(0, "580.173.02\n", "")]     # consecutive answers; the last repeats
        self.failed = [(0, "", "")]              # `systemctl --failed` answers, last repeats
        self.sim_hook = None                     # f(names, calls_so_far) -> optional (rc, out, err) override
        self.apt_purge_rc = 0
        self.calls: list[str] = []
        self.inputs: list = []
        self.envs: list = []
        self.purges: list[list[str]] = []
        self.native_arch = "amd64"
        self.flatpak: dict = {}
        self.depends: dict[str, set] = {}        # extra Depends (name -> names), besides the ones derived from rdeps
        self.recommends: dict[str, set] = {}     # name -> names it Recommends (purge simulation ignores these)
        self.provides: dict[str, set] = {}
        self.conffiles: dict[str, list] = {}     # name -> [(path, md5)]
        self.mod_version = "580.173.02"          # what `modinfo -F version nvidia` says (the NEXT boot's module)
        self.mod_file = ""                       # what `modinfo -F filename nvidia` says (a real file is made by the fixture)
        self.last_input = None
        self.eof_is_yes = False                  # a flatpak whose [Y/n] prompt takes anything but an explicit "n" as yes
        self.users: dict[str, tuple] = {}        # logged-in users -> consecutive `systemctl --user --failed` answers

    # -- model -------------------------------------------------------------------------------------------------
    def add(self, name, ver="1.0", st="ii", size=100, ma=False, rdeps=()):
        self.pkgs[name] = {"st": st, "ver": ver, "size": size, "ma": ma}
        if rdeps:
            self.rdeps[name] = set(rdeps)

    def closure(self, names):
        out, todo = set(), [n for n in names if n in self.pkgs]
        while todo:
            n = todo.pop()
            if n not in out:
                out.add(n)
                todo += [d for d in self.rdeps.get(n, ()) if d in self.pkgs]
        return out

    def status_text(self):
        """/var/lib/dpkg/status as the model sees it (Depends derived from rdeps, plus the explicit relations)."""
        deps: dict[str, set] = {}
        for d, dependents in self.rdeps.items():
            for p in dependents:
                deps.setdefault(p, set()).add(d.split(":")[0])
        word = {"i": "installed", "c": "config-files", "n": "not-installed", "H": "half-installed", "F": "half-configured",
                "U": "unpacked"}
        out = []
        for n, p in self.pkgs.items():
            base, _, arch = n.partition(":")
            lines = [f"Package: {base}", f"Status: install ok {word.get(p['st'][1], 'half-installed')}",
                     f"Architecture: {arch or self.native_arch}", f"Version: {p['ver']}"]
            for field, extra in (("Depends", deps.get(n, set()) | self.depends.get(n, set())),
                                 ("Recommends", self.recommends.get(n, set())), ("Provides", self.provides.get(n, set()))):
                if extra:
                    lines.append(f"{field}: " + ", ".join(sorted(extra)))
            if self.conffiles.get(n):
                lines += ["Conffiles:"] + [f" {path} {md5}" for path, md5 in self.conffiles[n]]
            lines.append("Description: x\n y")
            out.append("\n".join(lines))
        return "\n\n".join(out) + "\n"

    def dpkgq(self):
        lines = []
        for n, p in self.pkgs.items():
            dn = n + ":amd64" if p["ma"] and ":" not in n else n
            lines.append(f"{dn}\t{p['st']} \t{p['ver']}\t{p['size']}")
        return "\n".join(lines) + "\n"

    # -- dispatch ----------------------------------------------------------------------------------------------
    def __call__(self, cmd, timeout=60, **kw):
        key = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.calls.append(key)
        self.inputs.append(kw.get("input_"))
        self.last_input = kw.get("input_")
        self.envs.append(kw.get("env"))
        rc, out, err = self.answer(key)
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def answer(self, key):
        if key == "dpkg --print-architecture":
            return (0, self.native_arch + "\n", "")
        if key.startswith("dpkg-query -W"):
            return (0, self.dpkgq(), "")
        if key.startswith("dpkg-query -L"):
            return (1, "", "no list")
        if key == "apt-mark showhold":
            return (0, "\n".join(sorted(self.holds)) + "\n", "")
        if key == "dpkg --audit":
            return (self.audit_rc, self.audit, "")
        if key.startswith("dpkg --verify"):
            return self.verify
        if key == "dkms status":
            return self.dkms
        if key == "modinfo -F version nvidia":
            return (0, self.mod_version + "\n", "") if self.mod_version else (1, "", "modinfo: ERROR: Module nvidia not found.")
        if key == "modinfo -F filename nvidia":
            return (0, self.mod_file + "\n", "")
        if key == "loginctl list-users --no-legend":
            return (0, "".join(f"{1000 + i} {u} yes active\n" for i, u in enumerate(self.users)), "") if self.users else (1, "", "no")
        if key.startswith("systemctl --user --machine "):
            u = key.split()[3].split("@")[0]
            seq = self.users[u]
            r = seq[0]
            if len(seq) > 1:
                self.users[u] = seq[1:]
            return r
        if key.startswith("nvidia-smi"):
            r = self.smi[0]
            if len(self.smi) > 1:
                self.smi.pop(0)
            return r
        if key.startswith("systemctl --failed"):
            r = self.failed[0]
            if len(self.failed) > 1:
                self.failed.pop(0)
            return r
        if key.startswith("apt-get -s autoremove"):
            return (0, "".join(f"Purg {n} [1.0]\n" for n in self.autorm), "")
        if key.startswith("apt-get -s purge"):
            names = key.split()[3:]
            if self.sim_hook:
                o = self.sim_hook(names, self)
                if o:
                    return o
            gone = self.closure(names)
            held = sorted(n for n in gone if n.split(":")[0] in self.holds)
            if held:
                return (100, "", "E: Held packages were changed and -y was used without --allow-change-held-packages.\n"
                                 "E: Error, pkgProblemResolver::Resolve generated breaks, this may be caused by held packages.")
            return (0, "".join(f"Purg {n} [{self.pkgs[n]['ver']}]\n" for n in sorted(gone)), "")
        if key.startswith("apt-get -y purge"):
            names = key.split()[3:]
            self.purges.append(names)
            if self.apt_purge_rc:
                return (self.apt_purge_rc, "", "E: dpkg returned an error code")
            for n in self.closure(names):
                self.pkgs.pop(n, None)
            return (0, "", "")
        if key.startswith("flatpak") or key.startswith("runuser -u ohmz -- flatpak"):
            return self.flatpak_answer(key)
        return (127, "", "unmocked: " + key)

    def flatpak_answer(self, key):
        spec = re.sub(r"^(runuser -u \S+ -- )?flatpak ", "", key)
        inst = "user" if spec.startswith("--user") else "system"
        f = self.flatpak.setdefault(inst, {"apps": [], "runtimes": [], "unused": [], "raw": None})
        if "--columns=runtime" in spec:
            return (0, "".join(f"{r}\n" for r in f.get("app_rt", [])), "") if f.get("app_rt_rc", 0) == 0 else (1, "", "error")
        if " list --app " in " " + spec:
            return (0, "".join(f"{i}\t{b}\t1.0 MB\t{c}\n" for i, b, c in f["apps"]), "")
        if " list --runtime " in " " + spec:
            return (0, "".join(f"{i}\t{b}\t{s} MB\t{c}\n" for i, b, c, s in f["runtimes"]), "")
        if spec.endswith("uninstall --unused"):
            if self.eof_is_yes and not str(self.last_input or "").lower().startswith("n"):
                f["done"] = True                                  # a future flatpak: EOF == default answer == uninstall
                f["unused"] = []
                return (0, "Uninstalling...\nUninstall complete.\n", "")
            if f["raw"] is not None:
                return (1, f["raw"], "")
            if not f["unused"]:
                return (0, "Nothing unused to uninstall\n", "")
            rows = "".join(f" {k}.\t   \t{i}\t{b}\t{op}\n" for k, (i, b, op) in enumerate(f["unused"], 1))
            return (1, f"\n\n{rows}\nProceed with these changes to the {inst} installation? [Y/n]: n\n", "")
        if "uninstall --unused -y --noninteractive" in spec:
            f["done"] = True
            f["unused"] = []
            return (0, "", "")
        return (127, "", "unmocked: " + key)

    def mutating(self):
        return [c for c in self.calls if c.startswith(MUTATING) or " uninstall --unused -y" in c]


# =========================================================================== fixtures
@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    inuse.reset_caches()
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(core, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(core, "CONF_DIR", tmp_path / "conf")
    (tmp_path / "conf").mkdir()
    monkeypatch.setattr(core, "sh", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))   # audit's `logger`
    monkeypatch.setattr(cp, "_busy", lambda name: (False, "idle"))
    monkeypatch.setattr(cp, "_apt_lock_state", lambda *a: "free")
    monkeypatch.setattr(cp, "_euid", lambda: 0)
    monkeypatch.setattr(cp, "_uname", lambda: KERNEL)
    monkeypatch.setattr(cp, "DPKG_INFO", tmp_path / "dpkg-info")
    (tmp_path / "dpkg-info").mkdir()
    monkeypatch.setattr(cp, "FS_ROOT", str(tmp_path / "root"))              # fake package files live under tmp/root/usr/...
    monkeypatch.setattr(cp, "HOME_ROOT", str(tmp_path / "home"))           # no flatpak user installations unless a test adds one
    monkeypatch.setattr(inuse, "PROC", tmp_path / "proc")
    (tmp_path / "proc").mkdir()
    with_me(tmp_path)
    monkeypatch.setattr(inuse, "loaded_nvidia_driver",
                        lambda: Driver("580.173.02", "580", "580.173.02", "loaded 580.173.02 (proc+sys)"))
    host = Host()
    monkeypatch.setattr(cp, "sh", host)
    monkeypatch.setattr(cp, "_status_text", host.status_text)
    mod = tmp_path / "modules" / "nvidia.ko.zst"
    mod.parent.mkdir()
    mod.write_text("ko")
    host.mod_file = str(mod)
    yield host
    inuse.reset_caches()


@pytest.fixture
def host(sandbox):
    return sandbox


def deny_maps(monkeypatch, only=None):
    """Make /proc/*/maps unreadable (PermissionError) like a non-root run; `only` limits it to one pid. Our own pid
    stays readable so the failure is about OTHER processes."""
    real = inuse._read_text

    def read(p, limit=-1):
        if p.endswith("/maps") and f"/{os.getpid()}/" not in p and (only is None or f"/{only}/" in p):
            raise PermissionError(13, "denied")
        return real(p, limit)

    monkeypatch.setattr(inuse, "_read_text", read)
    inuse.reset_caches()


def mk(name, *, apply=False, now=NOW, protected=None, **opts):
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
    json.dumps(res.items)


def pkgfiles(tmp_path, name, files):
    """Create `files` (relative to tmp_path/root) on disk and write the package's dpkg .list; returns abs paths."""
    out = []
    for rel in files:
        p = tmp_path / "root" / rel
        if rel.endswith("/"):
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        out.append(str(p))
    (tmp_path / "dpkg-info" / f"{name}.list").write_text("\n".join(["/."] + out) + "\n")
    return out


# =========================================================================== registry
def test_registered_with_spec_classes():
    t = core.REGISTRY
    assert (t["stale_driver_packages"].klass, t["stale_driver_packages"].tier) == ("C1", "daily")
    assert (t["apt_autoremove_unused"].klass, t["apt_autoremove_unused"].tier) == ("C1", "daily")
    assert (t["flatpak_unused"].klass, t["flatpak_unused"].tier) == ("C1", "weekly")
    for n in ("stale_driver_packages", "apt_autoremove_unused", "flatpak_unused"):
        assert t[n].needs_root and t[n].timeout >= 900 and t[n].title


# =========================================================================== name classification
@pytest.mark.parametrize("name,want", [
    ("nvidia-dkms-535", ("535", "")), ("nvidia-kernel-source-570", ("570", "")), ("nvidia-driver-575-open", ("575", "")),
    ("nvidia-utils-535-server", ("535", "")), ("libnvidia-compute-535:i386", ("535", "")),
    ("libnvidia-gl-575", ("575", "")), ("libnvidia-cfg1-570", ("570", "")), ("libnvidia-fbc1-535", ("535", "")),
    ("xserver-xorg-video-nvidia-575", ("575", "")), ("nvidia-compute-utils-575", ("575", "")),
    ("nvidia-firmware-535-535.274.02", ("535", "535.274.02")), ("nvidia-headless-no-dkms-535-server", ("535", "")),
    ("linux-modules-nvidia-535-6.8.0-45-generic", ("535", "")), ("linux-objects-nvidia-570-6.11.0-29-generic", ("570", "")),
    ("linux-signatures-nvidia-535-generic-hwe-24.04", ("535", "")),
    # never part of a driver set
    ("nvidia-container-toolkit", None), ("nvidia-container-toolkit-base", None), ("libnvidia-container1:amd64", None),
    ("libnvidia-container-tools", None), ("nvidia-cuda-toolkit", None), ("nvidia-cuda-dev:amd64", None),
    ("nvidia-settings", None), ("nvidia-prime", None), ("nvtop", None), ("nsight-compute", None),
    ("libnvidia-ml-dev:amd64", None), ("libnvidia-egl-wayland1:i386", None), ("cuda-toolkit-12-4", None),
    ("nvidia-profiler", None), ("libcudart12:amd64", None), ("nvidia-modprobe", None), ("nvidia-docker2", None),
    # names that WOULD match the branch pattern but are never driver packages (the deny list is the second guard)
    ("nvidia-settings-535", None), ("nvidia-prime-535", None), ("nvidia-cuda-toolkit-570", None),
    ("nvidia-container-toolkit-535", None), ("nvidia-docker-535", None)])
def test_branch_classification(name, want):
    assert cp._branch_of(name) == want


def test_norm_and_pkg_name_rules():
    assert cp._norm("libnvidia-gl-580:amd64", "amd64") == "libnvidia-gl-580"
    assert cp._norm("libnvidia-gl-580:i386", "amd64") == "libnvidia-gl-580:i386"
    assert cp._norm("tree", "amd64") == "tree"
    assert all(cp._PKG.fullmatch(n) for n in ("a", "libx11-6:i386", "g++-13", "libstdc++6"))
    assert not any(cp._PKG.fullmatch(n) for n in ("-rf", "a b", "A", "x;y", "$(id)", ""))


# =========================================================================== stale_driver_packages
STALE = {  # apt-form name -> (version, size KiB, state, multiarch)
    "nvidia-dkms-535": ("535.274.02-0ubuntu0.24.04.1", 150, "ii", False),
    "nvidia-kernel-source-535": ("535.274.02-0ubuntu0.24.04.1", 120_000, "ii", False),
    "libnvidia-compute-535": ("535.274.02-0ubuntu0.24.04.1", 330_000, "ii", True),
    "libnvidia-compute-535:i386": ("535.274.02-0ubuntu0.24.04.1", 190_000, "ii", False),
    "nvidia-firmware-535-535.274.02": ("535.274.02-0ubuntu0.24.04.1", 100_000, "ii", False),
    "linux-modules-nvidia-535-6.8.0-45-generic": ("6.8.0-45.45+1", 80_000, "ii", False),
    "linux-objects-nvidia-570-6.11.0-29-generic": ("6.11.0-29.29+1", 50_000, "ii", False),
    "nvidia-utils-570": ("570.133.07-0ubuntu0.24.04.1", 1_700, "ii", False),
    "xserver-xorg-video-nvidia-575": ("575.57.08-0ubuntu0.24.04.1", 1_600, "ii", False),
    "libnvidia-gl-575": ("575.57.08-0ubuntu0.24.04.1", 500_000, "rc", True),          # config files only: purge cleans them
}
LOADED = {  # the loaded 580 driver: held, as on the real host
    "nvidia-utils-580": 1_700, "libnvidia-compute-580": 333_000, "libnvidia-gl-580": 530_000, "nvidia-dkms-580": 150,
    "nvidia-driver-580": 1_400, "libnvidia-cfg1-580": 400, "nvidia-firmware-580-580.173.02": 103_000,
    "xserver-xorg-video-nvidia-580": 1_000,
}
OTHER = ["nvidia-container-toolkit", "nvidia-container-toolkit-base", "libnvidia-container1", "nvidia-cuda-toolkit",
         "nvidia-settings", "nvidia-prime", "libnvidia-ml-dev", "nvtop", "libnvidia-egl-wayland1"]


def driver_host(host, tmp_path, stale=STALE, stale_files=True):
    for n, (v, sz, st, ma) in stale.items():
        host.add(n, v, st, sz, ma)
    for n, sz in LOADED.items():
        host.add(n, V580, "ii", sz, ma=n.startswith("lib"))
        host.holds.add(n)
    for n in OTHER:
        host.add(n, "1.17.8-1", "ii", 70)
    host.add("libnvidia-compute-580:i386", V580, "ii", 198_000)
    if stale_files:
        for n in stale:                      # every stale package has a readable file list (an unreadable one is refused)
            if n.startswith(("libnvidia", "nvidia-utils")):
                pkgfiles(tmp_path, n, [f"usr/lib/x/{n.replace(':', '_')}.so.1", "usr/lib/x/"])
            else:
                pkgfiles(tmp_path, n, [f"usr/src/{n.replace(':', '_')}/dkms.conf"])
    return host


def stale_names(stale=STALE):
    return sorted(stale)


def test_stale_nothing_to_do(host, tmp_path):
    driver_host(host, tmp_path, stale={})
    res = cp.stale_driver_packages(mk("stale_driver_packages"))
    assert res.status == "ok" and "no stale NVIDIA driver packages (loaded 580.173.02)" == res.summary
    assert res.metrics["selected"] == 0 and host.mutating() == []
    ascii_ok(res)


def test_stale_report_selects_exactly_the_stale_set(host, tmp_path):
    driver_host(host, tmp_path)
    res = cp.stale_driver_packages(mk("stale_driver_packages"))
    ascii_ok(res)
    assert res.status == "info" and res.summary.startswith("report: would purge 10 stale NVIDIA pkgs (535,570,575, ")
    assert res.metrics["selected"] == 10 and res.metrics["stale_branches"] == ["535", "570", "575"]
    assert res.metrics["mode"] == "report" and res.reclaimed_bytes == 0
    assert host.mutating() == [] and outcomes(tmp_path) == ["dry-run"]            # nothing executed; the act was only audited
    proof = res.items[0]
    assert proof["name"] == "proofs" and "dpkg --verify clean (9 pkgs)" in proof["proof"]
    assert f"dkms nvidia/580.173.02 installed for {KERNEL}" in proof["proof"] and "apt -s purge == stale set (10)" in proof["proof"]
    rows = {r["name"]: r for r in res.items[1:]}
    assert all(r["state"] == "would purge" for r in rows.values())
    assert rows["libnvidia-compute-535"]["size"] == "322.3 MiB" or "MiB" in rows["libnvidia-compute-535"]["size"]
    assert "branch 535 != loaded 580" in rows["libnvidia-compute-535"]["proof"]
    # the simulation the proof rests on was run on exactly the stale set (arch-qualified for the foreign one)
    sims = [c for c in host.calls if c.startswith("apt-get -s purge")]
    assert len(sims) == 1 and sorted(sims[0].split()[3:]) == stale_names()


def test_stale_apply_purges_once_with_exactly_the_set_and_verifies(host, tmp_path):
    driver_host(host, tmp_path)
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    ascii_ok(res)
    assert res.status == "ok" and res.summary.startswith("purged 10 stale NVIDIA pkgs (535,570,575), freed ")
    assert res.summary.endswith("nvidia-smi 580.173.02 ok")
    assert host.purges == [stale_names()]                                      # ONE apt run, exactly the stale set
    assert not any(n in LOADED or n in OTHER for n in host.purges[0])           # never the loaded 580 or toolkit packages
    i = host.calls.index(f"apt-get -y purge {' '.join(stale_names())}")
    assert host.envs[i]["DEBIAN_FRONTEND"] == "noninteractive"
    assert res.reclaimed_bytes == sum(v[1] for v in STALE.values() if v[2] == "ii") * 1024
    assert "done" in outcomes(tmp_path) and all(r["state"] == "purged" for r in res.items[1:])
    assert set(host.pkgs) >= set(LOADED) | set(OTHER)                           # the model really removed only the stale ones
    # idempotent: the next run finds nothing
    again = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert again.status == "ok" and len(host.purges) == 1


def test_stale_generic_for_a_future_driver_bump(host, tmp_path, monkeypatch):
    monkeypatch.setattr(inuse, "loaded_nvidia_driver", lambda: Driver("590.44.01", "590", "590.44.01", "loaded 590.44.01"))
    cur = {"nvidia-utils-590": 1700, "libnvidia-compute-590": 300, "nvidia-dkms-590": 150}
    for n, sz in cur.items():
        host.add(n, "590.44.01-0ubuntu1", "ii", sz)
    host.dkms = (0, f"nvidia/590.44.01, {KERNEL}, x86_64: installed\n", "")
    host.smi = [(0, "590.44.01\n", "")]
    host.mod_version = "590.44.01"
    for n, (v, sz, st, ma) in {"nvidia-utils-580": (V580, 1700, "ii", False), "nvidia-dkms-580": (V580, 150, "ii", False),
                               "libnvidia-compute-580": (V580, 300, "ii", False)}.items():
        host.add(n, v, st, sz, ma)
        pkgfiles(tmp_path, n, [f"usr/lib/x/{n}.so.1"])
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert res.status == "ok" and host.purges == [["libnvidia-compute-580", "nvidia-dkms-580", "nvidia-utils-580"]]
    assert res.metrics["stale_branches"] == ["580"] and res.metrics["loaded"] == "590.44.01"


def test_stale_same_branch_old_firmware_build_is_stale_but_new_is_not(host, tmp_path):
    driver_host(host, tmp_path, stale={})
    host.add("nvidia-firmware-580-580.159.03", "580.159.03-0ubuntu1", "ii", 90_000)
    pkgfiles(tmp_path, "nvidia-firmware-580-580.159.03", ["usr/lib/firmware/nvidia/580.159.03/gsp.bin"])
    res = cp.stale_driver_packages(mk("stale_driver_packages"))
    assert res.status == "info" and res.metrics["selected"] == 1 and res.metrics["stale_branches"] == ["580"]
    assert [r["name"] for r in res.items[1:]] == ["nvidia-firmware-580-580.159.03"]


def test_stale_keep_branches_option(host, tmp_path):
    driver_host(host, tmp_path)
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True, keep_branches=["535"]))
    assert res.status == "ok"
    assert host.purges and not any("535" in n for n in host.purges[0])
    assert any("575" in n for n in host.purges[0]) and any("570" in n for n in host.purges[0])


def test_stale_protected_names_do_not_block_driver_packages(host, tmp_path):
    # protected.toml has "xorg"/"nvidia"-like patterns: they guard containers/paths and must not veto the driver set
    # (neither the package names nor the act target label contain such a word)
    driver_host(host, tmp_path)
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True, protected={"patterns": ["xorg", "nvidia"]}))
    assert res.status == "ok" and "xserver-xorg-video-nvidia-575" in host.purges[0]


@pytest.mark.parametrize("drv", [Driver("", "", "", "nvidia kernel module not loaded or unreadable"),
                                 Driver("", "", "", "loaded-driver sources disagree")])
def test_stale_driver_unknown_touches_nothing(host, tmp_path, monkeypatch, drv):
    driver_host(host, tmp_path)
    monkeypatch.setattr(inuse, "loaded_nvidia_driver", lambda: drv)
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert res.status == "skipped" and "nothing touched" in res.summary
    assert host.mutating() == [] and not [c for c in host.calls if c.startswith("apt-get -s")]
    assert outcomes(tmp_path) == []


def test_stale_newer_branch_means_reboot_pending_not_stale(host, tmp_path):
    driver_host(host, tmp_path)
    host.add("nvidia-utils-590", "590.44.01-0ubuntu1", "ii", 1700)
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert res.status == "skipped" and "590 installed but 580 loaded" in res.summary and host.mutating() == []


def test_stale_same_branch_upgrade_pending_reboot(host, tmp_path):
    driver_host(host, tmp_path)
    host.pkgs["nvidia-utils-580"]["ver"] = "580.190.01-0ubuntu0.24.04.1"          # installed newer than the loaded module
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert res.status == "skipped" and "580.190.01 but 580.173.02 is loaded" in res.summary and host.mutating() == []


def test_stale_names_dpkg_knows_but_are_not_installed_are_ignored(host, tmp_path):
    driver_host(host, tmp_path, stale={})
    host.add("nvidia-utils-470", "", "un", 0)                  # `un`: in dpkg's database, not installed
    host.add("nvidia-dkms-470", "", "pn", 0)                   # `pn`: purged
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert res.status == "ok" and res.metrics["stale_pkgs"] == 0 and host.mutating() == []


def test_stale_half_installed_driver_package_refuses_everything(host, tmp_path):
    driver_host(host, tmp_path)
    host.pkgs["nvidia-dkms-535"]["st"] = "iF"
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert res.status == "skipped" and "inconsistent" in res.summary and host.mutating() == []


def refused(res, host, text):
    ascii_ok(res)
    assert res.status == "info" and res.metrics["selected"] == 0, res.summary
    assert text in res.metrics["refused"] and res.items[0]["state"] == "kept" and text in res.items[0]["proof"], res.items[0]
    assert all(r["state"] == "kept" for r in res.items)
    assert host.mutating() == []


def test_stale_held_stale_package_is_the_owners_decision(host, tmp_path):
    driver_host(host, tmp_path)
    host.holds.add("nvidia-dkms-535")
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, "nvidia-dkms-535 is on apt hold")


@pytest.mark.parametrize("missing", ["nvidia-utils-580", "libnvidia-compute-580"])
def test_stale_loaded_driver_must_be_installed_via_dpkg(host, tmp_path, missing):
    driver_host(host, tmp_path)
    del host.pkgs[missing]
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, f"no {missing.rsplit('-', 1)[0]}-580 package")


def test_stale_dpkg_verify_dirty_loaded_driver_refuses(host, tmp_path):
    driver_host(host, tmp_path)
    host.verify = (1, "??5??????   /usr/lib/x86_64-linux-gnu/libnvidia-gl-580.so\nmissing     /usr/bin/nvidia-smi\n", "")
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, "dpkg --verify: 2 changed files")
    host.verify = (2, "", "dpkg: error")
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, "dpkg --verify rc=2")


def test_stale_dpkg_verify_tolerates_config_file_edits(host, tmp_path):
    driver_host(host, tmp_path)
    host.verify = (1, "??5?????? c /etc/nvidia/nvidia-application-profiles-rc\nmissing   c /etc/modprobe.d/x.conf\n", "")
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert res.status == "ok" and host.purges


def test_stale_dkms_must_hold_the_loaded_version_for_the_running_kernel(host, tmp_path):
    driver_host(host, tmp_path)
    host.dkms = (0, "nvidia/535.274.02, 6.11.0-29-generic, x86_64: installed\n", "")           # only the stale one
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, "dkms has no nvidia/580.173.02")
    host.dkms = (0, f"nvidia/580.173.02, 6.8.0-45-generic, x86_64: installed\n", "")           # wrong kernel
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, "dkms has no nvidia/580.173.02")
    host.dkms = (0, f"nvidia/580.173.02, {KERNEL}, x86_64: built\n", "")                       # built but not installed
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, "dkms has no nvidia/580.173.02")
    host.dkms = (0, f"nvidia, 580.173.02, {KERNEL}, x86_64: installed\n", "")                  # dkms 2.x output style
    assert cp.stale_driver_packages(mk("stale_driver_packages")).metrics["selected"] == 10


def test_stale_without_dkms_a_prebuilt_module_package_is_required(host, tmp_path):
    driver_host(host, tmp_path)
    host.dkms = (127, "", "not found")
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, "no dkms and no linux-modules-nvidia-580")
    host.add(f"linux-modules-nvidia-580-{KERNEL}", "6.11.0-29.29+1", "ii", 90_000)
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert res.status == "ok" and f"linux-modules-nvidia-580-{KERNEL} installed for {KERNEL}" in res.items[0]["proof"]
    assert f"linux-modules-nvidia-580-{KERNEL}" not in host.purges[0]


def test_stale_library_in_use_by_a_process_refuses(host, tmp_path):
    driver_host(host, tmp_path)
    lib = str(tmp_path / "root" / "usr/lib/x/libnvidia-compute-535.so.1")
    mkproc(tmp_path, 700, "cuda-app", maps=mapline(lib))
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, "stale driver file in use: pid 700 (cuda-app)")


def test_stale_unreadable_proc_fails_closed(host, tmp_path, monkeypatch):
    driver_host(host, tmp_path)
    mkproc(tmp_path, 701, "rootproc")
    deny_maps(monkeypatch, only=701)
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, "stale driver file in use: unknown")


def test_stale_library_in_use_appearing_after_the_report_snapshot_aborts_the_purge(host, tmp_path):
    driver_host(host, tmp_path)
    lib = str(tmp_path / "root" / "usr/lib/x/libnvidia-compute-535.so.1")
    started = []

    def hook(names, h):
        if not started:                                       # between the proofs and the purge a CUDA job starts
            started.append(mkproc(tmp_path, 702, "late-job", maps=mapline(lib)))
        return None

    host.sim_hook = hook
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert host.purges == [] and res.metrics["gone"] == 1 and "vanished/changed" in res.summary


@pytest.mark.parametrize("extra,text", [
    (lambda names, h: (0, "".join(f"Purg {n} [1]\n" for n in names) + "Purg nvidia-driver-580 [1]\n", ""), "purge would remove extras nvidia-driver-580"),
    (lambda names, h: (0, "".join(f"Purg {n} [1]\n" for n in names[1:]), ""), "purge would not remove"),
    (lambda names, h: (100, "", "E: Unable to correct problems"), "apt-get -s purge failed: E: Unable to correct problems"),
    (lambda names, h: (0, "Inst libfoo [1]\n" + "".join(f"Purg {n} [1]\n" for n in names), ""), "simulation would install packages"),
    (lambda names, h: (0, "Purg we$ird [1]\n", ""), "unparsable package name"),
])
def test_stale_simulation_must_equal_the_stale_set_exactly(host, tmp_path, extra, text):
    driver_host(host, tmp_path)
    host.sim_hook = extra
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, text)


@pytest.mark.parametrize("setup,text", [
    (lambda h, c: setattr(c, "_busy", lambda name: (True, "apt running (pid 5)")), "apt/dpkg busy"),
    (lambda h, c: setattr(c, "_apt_lock_state", lambda *a: "busy"), "apt lock busy"),
    (lambda h, c: setattr(c, "_apt_lock_state", lambda *a: "unknown"), "apt lock unknown"),
    (lambda h, c: setattr(c, "_euid", lambda: 1000), "needs root"),
    (lambda h, c: setattr(h, "audit", "The following packages are only half installed:\n foo\n"), "dpkg --audit"),
    (lambda h, c: setattr(h, "audit_rc", 2), "dpkg --audit failed"),
])
def test_stale_never_while_apt_busy_locked_unrooted_or_dpkg_unclean(host, tmp_path, monkeypatch, setup, text):
    driver_host(host, tmp_path)
    setup(host, cp)
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert res.status == "skipped" and text in res.summary and host.mutating() == []


def test_stale_benign_dpkg_audit_blocks_are_tolerated(host, tmp_path):
    driver_host(host, tmp_path)
    host.audit = ("The following packages are missing the list control file in the\ndatabase, they need to be reinstalled:\n"
                  " gitkraken            Unleash your repo\n\nThe following packages are missing the md5sums control file in the\n"
                  "database, they need to be reinstalled:\n gitkraken            Unleash your repo\n\n")
    assert cp.stale_driver_packages(mk("stale_driver_packages")).metrics["selected"] == 10
    host.audit += "The following packages have been triggered, but the trigger processing has not yet been done:\n foo\n"
    assert cp.stale_driver_packages(mk("stale_driver_packages")).status == "skipped"


def test_stale_nvidia_smi_broken_after_purge_is_crit(host, tmp_path):
    driver_host(host, tmp_path)
    host.smi = [(9, "", "NVIDIA-SMI has failed")]                                  # only the post-check calls nvidia-smi
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    ascii_ok(res)
    assert res.status == "crit" and res.summary.startswith("PURGED stale NVIDIA pkgs but nvidia-smi no longer reports 580.173.02")
    assert host.purges                                                          # it did purge; the failure is reported loudly
    assert "failed" in " ".join(outcomes(tmp_path))


def test_stale_dkms_lost_after_purge_is_crit(host, tmp_path):
    driver_host(host, tmp_path)
    calls = {"n": 0}
    orig = host.answer

    def answer(key):
        if key == "dkms status":
            calls["n"] += 1
            if host.purges:                                       # after the purge dkms lost the 580 build
                return (0, "", "")
        return orig(key)

    host.answer = answer
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert res.status == "crit" and "dkms lost nvidia/580.173.02" in res.summary


def test_stale_apt_failure_is_warn_and_backs_off_next_run(host, tmp_path):
    driver_host(host, tmp_path)
    host.apt_purge_rc = 100
    ctx = mk("stale_driver_packages", apply=True)
    res = cp.stale_driver_packages(ctx)
    assert res.status == "warn" and "failed" in res.summary and res.reclaimed_bytes == 0 and len(host.purges) == 1
    ctx.save_state()                                              # the runner persists ctx.state after every task
    res2 = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert len(host.purges) == 1 and res2.metrics["backoff"] == 1 and "retry backoff" in res2.summary


def test_stale_pause_and_report_mode_never_mutate(host, tmp_path):
    driver_host(host, tmp_path)
    (tmp_path / "conf" / "PAUSE").write_text("")
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert res.metrics["mode"] == "report" and host.mutating() == []
    (tmp_path / "conf" / "PAUSE").unlink()
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=False))
    assert res.metrics["mode"] == "report" and host.mutating() == []
    # mode = "report" in the config wins over a --apply run
    cfg = {"global": {}, "caps": {}, "protected": PROTECTED, "tasks": {"stale_driver_packages": {"mode": "report"}}}
    res = cp.stale_driver_packages(core.Ctx(cfg, "stale_driver_packages", True, NOW))
    assert host.mutating() == [] and res.metrics["mode"] == "report"


def test_stale_cap_skips_an_oversize_set(host, tmp_path):
    driver_host(host, tmp_path)
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True, max_gib_per_run=0.1))
    assert host.purges == [] and res.metrics["oversize"] == 1 and "over cap" in res.summary


def test_stale_closure_changing_before_the_purge_aborts_it(host, tmp_path):
    driver_host(host, tmp_path)
    runs = {"n": 0}

    def hook(names, h):
        runs["n"] += 1
        if runs["n"] >= 2:                                      # the last-moment re-simulation sees an extra package
            return (0, "".join(f"Purg {n} [1]\n" for n in names) + "Purg nvidia-driver-580 [1]\n", "")
        return None

    host.sim_hook = hook
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert host.purges == [] and res.metrics["gone"] == 1 and "vanished/changed" in res.summary


def test_stale_apt_becoming_busy_before_the_purge_aborts_it(host, tmp_path, monkeypatch):
    driver_host(host, tmp_path)
    seq = iter([(False, "idle"), (True, "apt running")])
    monkeypatch.setattr(cp, "_busy", lambda name: next(seq, (True, "apt running")))
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert host.purges == [] and res.metrics["gone"] == 1


# =========================================================================== apt_autoremove_unused
def auto_host(host, tmp_path):
    """The real host's situation (read-only run, 2026-10-02): inxi/tree owner-kept, libxapp1 mapped by gsd-*, xapps-common
    whose purge would drag libxapp1 along, and xapp-sn-watcher: a D-Bus-activatable, autostarted service that nothing maps
    today and that libxapp1 (kept) Recommends."""
    for n, sz, files in [("libgl1-amber-dri", 15_000, ["usr/lib/dri/amber.so"]),
                         ("libglapi-mesa:i386", 196, ["usr/lib/i386/libglapi.so.0"]),
                         ("xapp-sn-watcher", 138, ["usr/bin/xapp-sn-watcher", "usr/share/dbus-1/services/org.x.StatusNotifierWatcher.service",
                                                   "etc/xdg/autostart/xapp-sn-watcher.desktop"]),
                         ("libdrm-radeon1:i386", 86, ["usr/lib/i386/libdrm_radeon.so.1"]),
                         ("libglapi-mesa", 253, ["usr/lib/x86_64/libglapi.so.0"]),
                         ("libxcb-cursor0", 39, ["usr/lib/x86_64/libxcb-cursor.so.0"]),
                         ("libxapp1", 280, ["usr/lib/x86_64/libxapp.so.2"]),
                         ("libxapp-gtk3-module", 38, ["usr/lib/x86_64/gtk3-modules/libxapp-gtk3-module.so"]),
                         ("xapps-common", 500, ["usr/share/xapps/x.svg"]),
                         ("tree", 108, ["usr/bin/tree"]), ("inxi", 1600, ["usr/bin/inxi"])]:
        host.add(n, "1.0", "ii", sz, ma=n.startswith("lib") and ":" not in n)
        # multiarch-same native packages keep their list as <name>:amd64.list, foreign ones as <name>:i386.list
        pkgfiles(tmp_path, n if ":" in n or not n.startswith("lib") else n + ":amd64", files)
    host.rdeps["xapps-common"] = {"libxapp1", "libxapp-gtk3-module"}      # they depend on xapps-common
    host.recommends["libxapp1"] = {"xapp-sn-watcher", "libxapp-gtk3-module"}    # purge simulation ignores Recommends
    host.autorm = ["inxi", "libdrm-radeon1:i386", "libgl1-amber-dri", "libglapi-mesa", "libglapi-mesa:i386",
                   "libxapp-gtk3-module", "xapp-sn-watcher", "libxapp1", "libxcb-cursor0", "tree", "xapps-common"]
    host.add("systemd", "255", "ii", 1)
    mkproc(tmp_path, 800, "gsd-color", maps=mapline(str(tmp_path / "root/usr/lib/x86_64/libxapp.so.2"))
           + mapline(str(tmp_path / "root/usr/lib/x86_64/gtk3-modules/libxapp-gtk3-module.so")))
    mkproc(tmp_path, 801, "easyeffects", maps=mapline(str(tmp_path / "root/usr/lib/x86_64/libxcb-cursor.so.0")))
    mkproc(tmp_path, 802, "Xvfb", maps=mapline(str(tmp_path / "root/usr/lib/x86_64/libglapi.so.0")))
    return host


WOULD = ["libdrm-radeon1:i386", "libgl1-amber-dri", "libglapi-mesa:i386"]
KEEP = ["tree", "inxi"]


def states(res):
    return {r["name"]: (r["state"], r["proof"]) for r in res.items}


def test_autoremove_reproduces_the_hand_analysis(host, tmp_path):
    auto_host(host, tmp_path)
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", keep=KEEP))
    ascii_ok(res)
    assert res.status == "info" and res.summary.startswith("report: would purge 3 unused pkgs (") and res.summary.endswith("; 8 kept")
    st = states(res)
    assert sorted(n for n, (s, _) in st.items() if s == "would purge") == WOULD
    for n in WOULD:
        assert st[n][1] == "not mapped/open/named by any process; shared libs/plugins only; closure ok"
    assert st["tree"][0] == "kept: owner keep-list" and st["inxi"][0] == "kept: owner keep-list"
    assert st["libxapp1"][0] == "kept: in use" and "pid 800 (gsd-color)" in st["libxapp1"][1]
    assert st["libxcb-cursor0"][0] == "kept: in use" and "pid 801 (easyeffects)" in st["libxcb-cursor0"][1]
    assert st["libglapi-mesa"][0] == "kept: in use" and "pid 802 (Xvfb)" in st["libglapi-mesa"][1]
    # a D-Bus-activatable, autostarted service and a data-only package are not provable from /proc: kept
    assert st["xapp-sn-watcher"][0] == "kept: not provable from /proc" and "usr/bin/xapp-sn-watcher" in st["xapp-sn-watcher"][1]
    assert st["xapps-common"][0] == "kept: not provable from /proc"
    assert host.mutating() == [] and outcomes(tmp_path) == ["dry-run"]


def test_autoremove_apply_purges_the_same_set_in_one_apt_run(host, tmp_path):
    auto_host(host, tmp_path)
    dry = cp.apt_autoremove_unused(mk("apt_autoremove_unused", keep=KEEP))
    dry_set = sorted(r["name"] for r in dry.items if r["state"] == "would purge")
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, keep=KEEP))
    ascii_ok(res)
    assert host.purges == [dry_set] == [WOULD]                                    # dry-run list == what apply purges
    assert res.status == "ok" and res.summary.startswith("purged 3 unused pkgs, freed ") and res.summary.endswith("; 8 kept")
    assert res.reclaimed_bytes == (15_000 + 196 + 86) * 1024
    assert [r["state"] for r in res.items[:3]] == ["purged"] * 3
    assert outcomes(tmp_path).count("done") == 1
    for kept in ("libxapp1", "libxapp-gtk3-module", "xapps-common", "xapp-sn-watcher", "tree", "inxi", "libxcb-cursor0", "libglapi-mesa"):
        assert kept in host.pkgs


def test_autoremove_new_failed_unit_after_purge_is_crit(host, tmp_path):
    auto_host(host, tmp_path)
    host.failed = [(0, "old.service loaded failed failed Old\n", ""),
                   (0, "old.service loaded failed failed Old\nxapp.service loaded failed failed X\n", "")]
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, keep=KEEP))
    ascii_ok(res)
    assert res.status == "crit" and "NEW failed units: xapp.service" in res.summary and host.purges


def test_autoremove_unchanged_failed_units_is_fine(host, tmp_path):
    auto_host(host, tmp_path)
    host.failed = [(0, "old.service loaded failed failed Old\n", "")]
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, keep=KEEP))
    assert res.status == "ok"


def test_autoremove_unreadable_failed_units_after_the_purge_is_warn(host, tmp_path):
    auto_host(host, tmp_path)
    host.failed = [(0, "", ""), (1, "", "Failed to connect to bus")]
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, keep=KEEP))
    assert res.status == "warn" and "unreadable after the purge" in res.summary


def test_autoremove_unreadable_failed_units_before_apply_refuses(host, tmp_path):
    auto_host(host, tmp_path)
    host.failed = [(1, "", "no bus")]
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, keep=KEEP))
    assert res.status == "skipped" and "cannot verify afterwards" in res.summary and host.purges == []


def test_autoremove_unreadable_proc_selects_nothing(host, tmp_path, monkeypatch):
    auto_host(host, tmp_path)
    deny_maps(monkeypatch, only=800)                              # ONE process we may not look into is enough to doubt
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, keep=KEEP))
    ascii_ok(res)
    assert res.metrics["selected"] == 0 and host.purges == []
    st = states(res)
    assert all(s == "kept: unknown (cannot prove)" for n, (s, _) in st.items() if n not in KEEP)
    assert res.status == "ok" and res.summary.startswith("no unused package to purge")


def test_autoremove_package_with_unreadable_file_list_is_kept(host, tmp_path):
    auto_host(host, tmp_path)
    (tmp_path / "dpkg-info" / "xapp-sn-watcher.list").unlink()
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", keep=KEEP))
    st = states(res)
    assert st["xapp-sn-watcher"] == ("kept: unknown (cannot prove)", "unknown: file list of the package unreadable")
    assert sorted(n for n, (s, _) in st.items() if s == "would purge") == [n for n in WOULD if n != "xapp-sn-watcher"]


@pytest.mark.parametrize("how", ["exe", "fd", "cwd"])
def test_autoremove_non_library_use_keeps_the_package(host, tmp_path, how):
    auto_host(host, tmp_path)
    exe = str(tmp_path / "root/usr/bin/xapp-sn-watcher")
    kw = {"exe": {"exe": exe}, "fd": {"fds": [exe]}, "cwd": {"cwd": exe}}[how]
    mkproc(tmp_path, 810, "watcher", **kw)
    st = states(cp.apt_autoremove_unused(mk("apt_autoremove_unused", keep=KEEP)))
    assert st["xapp-sn-watcher"][0] == "kept: in use" and "pid 810 (watcher)" in st["xapp-sn-watcher"][1]


def test_autoremove_deleted_mapping_still_counts(host, tmp_path):
    auto_host(host, tmp_path)
    mkproc(tmp_path, 811, "gl", maps=mapline(str(tmp_path / "root/usr/lib/dri/amber.so") + " (deleted)"))
    assert states(cp.apt_autoremove_unused(mk("apt_autoremove_unused", keep=KEEP)))["libgl1-amber-dri"][0] == "kept: in use"


def mini(host, tmp_path, names, sizes=None, files=None):
    """A small autoremove world: each name is a one-library package (so it can be proven unused), no dependencies.
    `files` overrides the package's file list (relative to the fake root)."""
    for n in names:
        host.add(n, "1.0", "ii", (sizes or {}).get(n, 10))
        pkgfiles(tmp_path, n, files or [f"usr/lib/x86_64-linux-gnu/{n}.so.1"])
        host.autorm.append(n)
    return host


def lib(tmp_path, n):
    return str(tmp_path / "root" / f"usr/lib/x86_64-linux-gnu/{n}.so.1")


def test_autoremove_keep_glob_hold_protected_and_never_option(host, tmp_path):
    mini(host, tmp_path, ["libxcb-cursor0", "xapp-sn-watcher", "libgl1-amber-dri", "libdrm-radeon1", "tree", "plain-one"])
    host.holds.add("xapp-sn-watcher")
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", keep=["lib*-cursor*", "tree"], never=["libdrm-*"],
                                      protected={"patterns": ["libgl1-amber"]}))
    st = states(res)
    assert st["libxcb-cursor0"] == ("kept: owner keep-list", "in keep list")      # fnmatch globs work
    assert st["tree"][0] == "kept: owner keep-list"
    assert st["xapp-sn-watcher"][0] == "kept: on apt hold"
    assert st["libgl1-amber-dri"][0] == "kept: protected"                         # protected.toml still applies to packages
    assert st["libdrm-radeon1"][0] == "kept: critical package class"              # the `never` option extends the built-in list
    assert [n for n, (s, _) in st.items() if s == "would purge"] == ["plain-one"] and res.metrics["selected"] == 1


@pytest.mark.parametrize("name", ["linux-image-6.8.0-45-generic", "linux-modules-extra-6.8.0-45-generic", "nvidia-utils-535",
                                  "libnvidia-gl-535", "dkms", "grub-pc", "shim-signed", "initramfs-tools", "systemd-sysv",
                                  "libc6-dev", "ubuntu-minimal", "snapd", "docker-ce-cli", "containerd.io", "libvirt0",
                                  "qemu-system-x86"])
def test_autoremove_critical_classes_are_never_selected(host, tmp_path, name):
    mini(host, tmp_path, [name, "plain-one"])
    st = states(cp.apt_autoremove_unused(mk("apt_autoremove_unused", protected={"patterns": []})))
    assert st[name][0] == "kept: critical package class" and st["plain-one"][0] == "would purge"


def test_autoremove_closure_is_iterated_to_a_fixed_point(host, tmp_path):
    for n in ("a", "b", "c", "d", "e"):
        host.add(n, "1", "ii", 10)
        pkgfiles(tmp_path, n, [f"usr/lib/x86_64-linux-gnu/{n}.so.1"])
    # purging c also removes b (depends on c), purging b also removes a (depends on b); a is in use => c, b both must stay
    host.rdeps = {"c": {"b"}, "b": {"a"}, "d": {"e"}}
    host.autorm = ["a", "b", "c", "d", "e"]
    mkproc(tmp_path, 820, "user-of-a", maps=mapline(lib(tmp_path, "a")))
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True))
    st = states(res)
    assert st["a"][0] == "kept: in use"
    assert st["b"][0] == "kept: closure" and "a" in st["b"][1] and st["c"][0] == "kept: closure"
    assert st["d"][0] == "purged" and st["e"][0] == "purged"              # d + e only drag each other: both selected
    assert host.purges == [["d", "e"]]


def test_autoremove_combined_simulation_mismatch_purges_nothing(host, tmp_path):
    auto_host(host, tmp_path)

    def hook(names, h):
        if len(names) > 1:                                      # only the whole-set simulation misbehaves
            return (0, "".join(f"Purg {n} [1]\n" for n in names) + "Purg libc6 [1]\n", "")
        return None

    host.sim_hook = hook
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, keep=KEEP))
    assert host.purges == [] and res.metrics["selected"] == 0
    assert all(states(res)[n][0] == "kept: closure" for n in WOULD)


def test_autoremove_single_candidate_simulation_failure_keeps_it(host, tmp_path):
    auto_host(host, tmp_path)

    def hook(names, h):
        if names == ["libgl1-amber-dri"]:
            return (100, "", "E: Unable to correct problems")
        return None

    host.sim_hook = hook
    st = states(cp.apt_autoremove_unused(mk("apt_autoremove_unused", keep=KEEP)))
    assert st["libgl1-amber-dri"] == ("kept: closure unknown", "apt -s purge failed: E: Unable to correct problems")
    assert sorted(n for n, (s, _) in st.items() if s == "would purge") == [n for n in WOULD if n != "libgl1-amber-dri"]


def test_autoremove_package_that_becomes_in_use_before_the_purge_aborts_it(host, tmp_path):
    auto_host(host, tmp_path)
    started = []

    def hook(names, h):
        if len(names) > 1 and not started:                    # the whole-set simulation: proofs are done, purge is next
            started.append(mkproc(tmp_path, 830, "late", fds=[str(tmp_path / "root/usr/lib/dri/amber.so")]))
        return None

    host.sim_hook = hook
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, keep=KEEP))
    # the report-time snapshot (cached) did not know the process; the last-moment fresh snapshot does
    assert started and host.purges == [] and res.metrics["gone"] == 1 and "vanished/changed" in res.summary


def test_autoremove_file_list_falls_back_to_dpkg_query(host, tmp_path):
    mini(host, tmp_path, ["solo"])
    (tmp_path / "dpkg-info" / "solo.list").unlink()
    exe = lib(tmp_path, "solo")
    orig = host.answer
    host.answer = lambda key: (0, f"/.\n{exe}\n", "") if key == "dpkg-query -L solo" else orig(key)
    mkproc(tmp_path, 840, "solo", exe=exe)
    assert states(cp.apt_autoremove_unused(mk("apt_autoremove_unused")))["solo"][0] == "kept: in use"


def test_autoremove_and_stale_stop_when_dpkg_or_apt_state_is_unreadable(host, tmp_path):
    auto_host(host, tmp_path)
    driver_host(host, tmp_path)
    orig = host.answer
    host.answer = lambda key: (0, "garbage without tabs\n", "") if key.startswith("dpkg-query -W") else orig(key)
    assert "dpkg" in cp.apt_autoremove_unused(mk("apt_autoremove_unused")).summary
    assert cp.stale_driver_packages(mk("stale_driver_packages")).status == "skipped"
    host.answer = lambda key: (1, "", "E: lock") if key == "apt-mark showhold" else orig(key)
    assert cp.apt_autoremove_unused(mk("apt_autoremove_unused")).status == "skipped"
    refused(cp.stale_driver_packages(mk("stale_driver_packages")), host, "apt-mark showhold failed")
    host.answer = lambda key: (1, "", "no") if key == "dpkg --print-architecture" else orig(key)
    assert cp.stale_driver_packages(mk("stale_driver_packages")).status == "skipped"


@pytest.mark.parametrize("setup,text", [
    (lambda h, c: setattr(c, "_busy", lambda name: (True, "unattended-upgrade running")), "apt/dpkg busy"),
    (lambda h, c: setattr(c, "_apt_lock_state", lambda *a: "busy"), "apt lock busy"),
    (lambda h, c: setattr(c, "_euid", lambda: 1000), "needs root"),
    (lambda h, c: setattr(h, "audit", "The following packages are only half installed:\n foo\n"), "dpkg --audit"),
])
def test_autoremove_never_while_apt_busy_locked_unrooted_or_dpkg_unclean(host, tmp_path, setup, text):
    auto_host(host, tmp_path)
    setup(host, cp)
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, keep=KEEP))
    assert res.status == "skipped" and text in res.summary and host.purges == []


def test_autoremove_report_mode_as_non_root_is_unknown_not_selected(host, tmp_path, monkeypatch):
    auto_host(host, tmp_path)
    monkeypatch.setattr(cp, "_euid", lambda: 1000)
    host.audit_rc = 2                                              # dpkg --audit cannot take its lock as a normal user
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", keep=KEEP))
    assert res.status == "skipped" and "dpkg --audit failed" in res.summary and host.mutating() == []


def test_autoremove_no_candidates_and_simulation_failure(host, tmp_path):
    host.add("libc6", "1", "ii", 1)
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused"))
    assert res.status == "ok" and res.summary == "no autoremove candidates"
    orig = host.answer
    host.answer = lambda key: (100, "", "E: broken") if key.startswith("apt-get -s autoremove") else orig(key)
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused"))
    assert res.status == "skipped" and "autoremove simulation failed" in res.summary


def test_autoremove_max_candidates_defers_the_rest(host, tmp_path):
    auto_host(host, tmp_path)
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", keep=KEEP, max_candidates=2, apply=True))
    st = states(res)
    assert sum(1 for s, _ in st.values() if s == "kept: deferred (too many)") >= 1
    assert len(host.purges[0] if host.purges else []) <= 2


def test_autoremove_pause_and_report_mode_never_mutate(host, tmp_path):
    auto_host(host, tmp_path)
    (tmp_path / "conf" / "PAUSE.apt_autoremove_unused").write_text("")
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, keep=KEEP))
    assert res.metrics["mode"] == "report" and host.mutating() == []
    assert outcomes(tmp_path) == ["dry-run"]


def test_autoremove_byte_cap_skips_oversize_set_and_item_cap_applies(host, tmp_path):
    auto_host(host, tmp_path)
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, keep=KEEP, max_gib_per_run=0.0001))
    assert host.purges == [] and res.metrics["oversize"] == 1


def test_autoremove_apt_failure_is_warn(host, tmp_path):
    auto_host(host, tmp_path)
    host.apt_purge_rc = 100
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, keep=KEEP))
    assert res.status == "warn" and res.reclaimed_bytes == 0 and "failed" in res.summary


def test_autoremove_items_are_capped_and_ascii(host, tmp_path):
    auto_host(host, tmp_path)
    for i in range(30):
        host.add(f"zz{i}", "1", "ii", 10 + i)
        host.autorm.append(f"zz{i}")
        pkgfiles(tmp_path, f"zz{i}", [f"usr/lib/x86_64-linux-gnu/zz{i}.so.1"])
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", keep=KEEP))
    ascii_ok(res)
    assert len(res.items) == 12 and res.items[0]["state"] == "would purge"          # selected rows first, then kept ones
    assert res.metrics["selected"] == 33


# =========================================================================== flatpak_unused
UNUSED_TABLE = [("org.gnome.Platform", "46", "r"), ("org.gnome.Platform.Locale", "46", "r"),
                ("org.freedesktop.Platform.GL.default", "23.08", "r")]


def fp_host(host, unused=None, apps=None):
    host.flatpak["system"] = {
        "apps": apps if apps is not None else [("org.gnome.Showtime", "stable", "aaaaaaaaaaaa")],
        "runtimes": [("org.gnome.Platform", "46", "111111111111", "951.3"), ("org.gnome.Platform.Locale", "46", "222222222222", "0.5"),
                     ("org.freedesktop.Platform.GL.default", "23.08", "333333333333", "539.2"),
                     ("org.gnome.Platform", "50", "444444444444", "1100.0")],
        "unused": UNUSED_TABLE if unused is None else unused, "raw": None}
    return host


def fp_ctx(apply=False, **kw):
    return mk("flatpak_unused", apply=apply, installations=["system"], **kw)


def test_flatpak_report_lists_runtimes_with_proof_and_never_answers_yes(host):
    fp_host(host)
    res = cp.flatpak_unused(fp_ctx())
    ascii_ok(res)
    assert res.status == "info" and res.summary.startswith("report: would remove 3 unused flatpak runtimes (")
    assert res.metrics["refs"] == 3 and res.metrics["selected"] == 3 and host.mutating() == []
    assert [r["name"] for r in res.items] == ["org.gnome.Platform//46 (system)", "org.gnome.Platform.Locale//46 (system)",
                                              "org.freedesktop.Platform.GL.default//23.08 (system)"]
    assert all(r["state"] == "would remove" and "runtime/extension, not an app" in r["proof"] for r in res.items)
    assert res.items[0]["size"] == "951.3 MB" or "MiB" in res.items[0]["size"]
    # the listing run: no -y/--noninteractive, and an explicit "n" on stdin answers flatpak's own prompt
    i = host.calls.index("flatpak --system uninstall --unused")
    assert host.inputs[i] == "n\n"


def test_flatpak_apply_runs_unused_uninstall_once_and_verifies_apps(host, tmp_path):
    fp_host(host)
    res = cp.flatpak_unused(fp_ctx(apply=True))
    ascii_ok(res)
    assert res.status == "ok" and res.summary.startswith("removed 3 unused flatpak runtimes, freed ")
    assert [c for c in host.calls if "uninstall --unused -y" in c] == ["flatpak --system uninstall --unused -y --noninteractive"]
    assert res.reclaimed_bytes > 0 and "done" in outcomes(tmp_path)
    again = cp.flatpak_unused(fp_ctx(apply=True))                                  # idempotent
    assert again.status == "ok" and again.summary == "no unused flatpak runtimes"
    assert len([c for c in host.calls if "uninstall --unused -y" in c]) == 1


def test_flatpak_nothing_unused(host):
    fp_host(host, unused=[])
    res = cp.flatpak_unused(fp_ctx())
    assert res.status == "ok" and res.summary == "no unused flatpak runtimes" and res.items[0]["state"] == "nothing unused"


@pytest.mark.parametrize("row,why", [(("org.gnome.Showtime", "stable", "r"), "org.gnome.Showtime//stable is not an installed runtime"),
                                     (("org.unknown.Thing", "1", "r"), "is not an installed runtime")])
def test_flatpak_a_listed_app_or_unknown_ref_refuses_the_whole_list(host, row, why):
    fp_host(host, unused=[*UNUSED_TABLE, row])
    res = cp.flatpak_unused(fp_ctx(apply=True))
    assert res.status == "ok" and res.metrics["refused"] == 1 and res.metrics["selected"] == 0
    assert why in res.items[0]["proof"] and "refused: not all runtimes" == res.items[0]["state"]
    assert host.mutating() == []


@pytest.mark.parametrize("op", ["i", "u", "x"])
def test_flatpak_unexpected_table_operations_are_not_trusted(host, op):
    fp_host(host, unused=[("org.gnome.Platform", "46", op)])
    res = cp.flatpak_unused(fp_ctx(apply=True))
    assert res.status == "skipped" and "unexpected row" in res.items[0]["proof"] and host.mutating() == []


def test_flatpak_output_without_the_prompt_is_never_trusted(host):
    fp_host(host)
    host.flatpak["system"]["raw"] = "Uninstalling runtime/org.gnome.Platform/x86_64/46\nUninstall complete.\n"
    res = cp.flatpak_unused(fp_ctx(apply=True))
    assert res.status == "skipped" and "unexpected flatpak output" in res.items[0]["proof"] and host.mutating() == []
    host.flatpak["system"]["raw"] = "\n 1.\t \torg.gnome.Platform\t46\tr\n"                  # no prompt line at all
    assert cp.flatpak_unused(fp_ctx(apply=True)).status == "skipped"


def sandbox_info(commit, runtime="runtime/org.gnome.Platform/x86_64/46"):
    return (f"[Application]\nname=org.x.App\nruntime={runtime}\n\n[Instance]\nruntime-commit={commit * 6}{'0' * 52}\n"
            f"runtime-extensions=org.gnome.Platform.Locale={commit * 6}{'0' * 52};\n")


def mk_sandbox(tmp_path, pid, text):
    d = mkproc(tmp_path, pid, "app")
    root = d / "root"
    root.mkdir()
    (root / ".flatpak-info").write_text(text)


def test_flatpak_runtime_in_use_by_a_running_sandbox_is_refused(host, tmp_path):
    fp_host(host)
    mk_sandbox(tmp_path, 900, sandbox_info("111111", runtime="runtime/org.gnome.Platform/x86_64/46"))
    res = cp.flatpak_unused(fp_ctx(apply=True))
    assert res.metrics["refused"] == 1 and "org.gnome.Platform//46 is used by a running sandbox" in res.items[0]["proof"]
    assert host.mutating() == []


def test_flatpak_sandbox_using_other_branches_does_not_block(host, tmp_path):
    fp_host(host)
    # a running app on Platform 50 (different commit, different branch): the unused 46 refs are unaffected
    mk_sandbox(tmp_path, 901, sandbox_info("444444", runtime="runtime/org.gnome.Platform/x86_64/50"))
    res = cp.flatpak_unused(fp_ctx(apply=True))
    assert res.status == "ok" and [c for c in host.calls if "uninstall --unused -y" in c]


def test_flatpak_busy_and_visibility_gates(host, tmp_path, monkeypatch):
    fp_host(host)
    mkproc(tmp_path, 910, "flatpak")
    res = cp.flatpak_unused(fp_ctx(apply=True))
    assert res.status == "skipped" and "a flatpak command is running" in res.summary
    import shutil
    shutil.rmtree(tmp_path / "proc" / "910")
    mkproc(tmp_path, 911, "other")
    deny_maps(monkeypatch)
    res = cp.flatpak_unused(fp_ctx(apply=True))
    assert res.status == "skipped" and "cannot see every process" in res.summary and host.mutating() == []


def test_flatpak_apply_needs_root(host, monkeypatch):
    fp_host(host)
    monkeypatch.setattr(cp, "_euid", lambda: 1000)
    res = cp.flatpak_unused(fp_ctx(apply=True))
    assert res.status == "skipped" and "needs root" in res.summary and host.mutating() == []


def test_flatpak_application_list_changing_after_uninstall_is_crit(host):
    fp_host(host)
    orig = host.flatpak_answer

    def fa(key):
        if "uninstall --unused -y" in key:
            host.flatpak["system"]["apps"] = []                      # an app vanished
        return orig(key)

    host.flatpak_answer = fa
    res = cp.flatpak_unused(fp_ctx(apply=True))
    assert res.status == "crit" and "application list changed" in res.summary


def test_flatpak_list_changed_between_report_and_apply_aborts(host):
    fp_host(host)
    orig = host.flatpak_answer
    n = {"i": 0}

    def fa(key):
        if key.endswith("uninstall --unused"):
            n["i"] += 1
            if n["i"] == 2:                                          # the last-moment re-listing differs
                host.flatpak["system"]["unused"] = UNUSED_TABLE[:1]
        return orig(key)

    host.flatpak_answer = fa
    res = cp.flatpak_unused(fp_ctx(apply=True))
    assert res.metrics["gone"] == 1 and [c for c in host.calls if "uninstall --unused -y" in c] == []


def test_flatpak_user_installation_runs_as_the_owner_when_root(host, monkeypatch):
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda n: pwd.struct_passwd(("ohmz", "x", 1000, 1000, "", "/home/ohmz", "/bin/bash")))
    fp_host(host)
    host.flatpak["user"] = {"apps": [], "runtimes": [("org.gnome.Platform", "46", "555555555555", "951.3")],
                            "unused": [("org.gnome.Platform", "46", "r")], "raw": None}
    res = cp.flatpak_unused(mk("flatpak_unused", apply=True, installations=["system", "user:ohmz"]))
    assert res.status == "ok" and res.metrics["refs"] == 4
    assert "runuser -u ohmz -- flatpak --user uninstall --unused -y --noninteractive" in host.calls
    assert "flatpak --system uninstall --unused -y --noninteractive" in host.calls


def test_flatpak_bad_or_unrunnable_specs_are_reported_not_run(host, monkeypatch):
    fp_host(host)
    res = cp.flatpak_unused(mk("flatpak_unused", installations=["system", "user:../x", "user:nosuchuser", "weird"]))
    assert res.metrics["unavailable"] == 3 and host.mutating() == []
    rows = {r["name"]: r for r in res.items}
    assert rows["user:../x"]["state"] == "skipped: cannot run from here" and rows["weird"]["state"].startswith("skipped")
    monkeypatch.setattr(cp, "_euid", lambda: 1000)
    assert cp._fp_cmd("user:root") is None                      # not root, not that user: cannot run it
    assert cp._fp_cmd("system") == (["flatpak", "--system"], "system")


def test_flatpak_unreadable_flatpak_is_skipped(host):
    fp_host(host)
    orig = host.flatpak_answer
    host.flatpak_answer = lambda key: (1, "", "error: no") if " list " in key else orig(key)
    res = cp.flatpak_unused(fp_ctx(apply=True))
    assert res.status == "skipped" and host.mutating() == []
    host.flatpak_answer = orig
    assert cp.flatpak_unused(mk("flatpak_unused", installations=[])).summary == "no flatpak installations configured"


def test_flatpak_nbsp_sizes_and_missing_columns_parse(host):
    host.flatpak["system"] = {"apps": [], "runtimes": [("org.a.B", "1", "abcdef123456", "668.9")], "unused": [], "raw": None}
    out = cp._fp_list(["flatpak", "--system"], "runtime")
    assert out == {("org.a.B", "1"): (668_900_000, "abcdef123456")}


def test_flatpak_pause_never_mutates(host, tmp_path):
    fp_host(host)
    (tmp_path / "conf" / "PAUSE").write_text("")
    res = cp.flatpak_unused(fp_ctx(apply=True))
    assert res.metrics["mode"] == "report" and host.mutating() == []


# =========================================================================== regressions from the independent review
# Each test below is a case that WOULD have deleted something in use (or something that cannot be proven unused).
def autoremove(host, tmp_path, names=(), apply=False, files=None, **opts):
    """mini() world + one run of apt_autoremove_unused (no protected patterns)."""
    mini(host, tmp_path, list(names), files=files)
    return cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=apply, protected={"patterns": []}, **opts))


# ---- 1. interpreter-run scripts and imported modules leave no trace in maps/fds ------------------------------------------
def test_review_interpreter_script_named_only_on_argv_is_in_use(host, tmp_path):
    """solaar runs as `python3 /usr/bin/solaar --window=hide`: not mapped, not an exe, not an fd. It was judged unused."""
    exe = tmp_path / "root/usr/bin/solaar"
    mini(host, tmp_path, ["solaar"], files=["usr/bin/solaar"])
    mkproc(tmp_path, 4009, "python3", cmdline=f"python3 {exe} --window=hide", exe="/usr/bin/python3.12")
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, protected={"patterns": []}, allow_purge=["solaar"]))
    assert states(res)["solaar"][0] == "kept: in use" and "pid 4009 (python3) argv" in states(res)["solaar"][1]
    assert host.purges == []
    # and the same package, same process, with the old default kinds (map/exe/fd/cwd) really is invisible:
    assert inuse.files_in_use([str(exe)])[str(exe)].unused
    # a library preloaded through the environment is named only in environ
    lib_ = lib(tmp_path, "libshim")
    mini(host, tmp_path, ["libshim"])
    mkproc(tmp_path, 4010, "app", environ=f"LD_PRELOAD={lib_}")
    inuse.proc_snapshot(refresh=True)
    assert states(cp.apt_autoremove_unused(mk("apt_autoremove_unused", protected={"patterns": []})))["libshim"][0] == "kept: in use"


@pytest.mark.parametrize("pkg,files,why", [
    ("solaar", ["usr/bin/solaar"], "usr/bin/solaar"),
    ("unattended-upgrades", ["usr/share/unattended-upgrades/unattended-upgrade-shutdown", "usr/lib/x86_64-linux-gnu/libua.so.1"],
     "unattended-upgrade-shutdown"),
    ("python3-jinja2", ["usr/lib/python3/dist-packages/jinja2/__init__.py"], "jinja2/__init__.py"),
    ("python3-attr", ["usr/lib/python3/dist-packages/attr/__init__.py", "usr/lib/python3/dist-packages/attr/_cext.cpython-312.so"],
     "attr/__init__.py"),
    ("python3-cext", ["usr/lib/python3/dist-packages/_x.cpython-312-x86_64-linux-gnu.so"], "python3 plugin dir"),
    ("libfoo-dbus", ["usr/lib/x86_64-linux-gnu/libfoo.so.1", "usr/share/dbus-1/services/org.x.StatusNotifierWatcher.service"],
     "dbus-1/services"),
    ("libfoo-autostart", ["usr/lib/x86_64-linux-gnu/libfoo2.so.1", "etc/xdg/autostart/foo.desktop"], "autostart/foo.desktop"),
    ("libfoo-unit", ["usr/lib/x86_64-linux-gnu/libfoo3.so.1", "lib/systemd/system/foo.service"], "systemd/system/foo.service"),
    ("libfoo-userunit", ["usr/lib/x86_64-linux-gnu/libfoo4.so.1", "usr/lib/systemd/user/foo.service"], "systemd/user/foo.service"),
    ("libfoo-cron", ["usr/lib/x86_64-linux-gnu/libfoo5.so.1", "etc/cron.d/foo"], "cron.d/foo"),
    ("libfoo-udev", ["usr/lib/x86_64-linux-gnu/libfoo6.so.1", "lib/udev/rules.d/60-foo.rules"], "udev/rules.d"),
    ("libfoo-polkit", ["usr/lib/x86_64-linux-gnu/libfoo7.so.1", "usr/share/polkit-1/actions/foo.policy"], "polkit-1"),
    ("libpam-foo", ["usr/lib/x86_64-linux-gnu/security/pam_foo.so"], "security plugin dir"),
    ("foo-desktop", ["usr/lib/x86_64-linux-gnu/libfoo8.so.1", "usr/share/applications/foo.desktop"], "applications/foo.desktop"),
    ("foo-sbin", ["usr/sbin/food"], "usr/sbin/food"),
    ("foo-libexec", ["usr/libexec/foo/helper"], "libexec/foo/helper"),
    ("foo-jvm", ["usr/lib/jvm/foo/lib/libjvm.so"], "jvm plugin dir"),
    ("foo-data", ["usr/share/foo/data.bin"], "usr/share/foo/data.bin"),
    ("libfoo-dev", ["usr/include/foo.h", "usr/lib/x86_64-linux-gnu/libfoo.a"], "usr/include/foo.h"),
])
def test_review_packages_proc_cannot_speak_for_are_kept(host, tmp_path, pkg, files, why):
    mini(host, tmp_path, [pkg], files=files)
    res = autoremove(host, tmp_path, ["plain-one"], apply=True)
    st = states(res)
    assert st[pkg][0] == "kept: not provable from /proc" and why in st[pkg][1], st[pkg]
    assert host.purges == [["plain-one"]]                      # the genuinely checkable library is still purged


def test_review_allow_purge_waives_only_the_file_class_check(host, tmp_path):
    mini(host, tmp_path, ["foo-dev"], files=["usr/include/foo.h"])
    mini(host, tmp_path, ["busy-dev"], files=["usr/bin/busy"])
    mini(host, tmp_path, ["solaar"], files=["usr/bin/solaar"])
    mkproc(tmp_path, 4011, "busy", exe=str(tmp_path / "root/usr/bin/busy"))
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", protected={"patterns": []}, allow_purge=["*-dev"]))
    st = states(res)
    assert st["foo-dev"][0] == "would purge"                                   # waived by the owner's glob
    assert st["busy-dev"][0] == "kept: in use"                                 # the in-use proof still applies to it
    assert st["solaar"][0] == "kept: not provable from /proc"                  # not listed: still not provable


def test_review_checkable_library_shapes_are_allowed(host, tmp_path):
    mini(host, tmp_path, ["libgtkmod", "libdri", "libold", "libdoc"],
         files=None)
    for n, files in {"libgtkmod": ["usr/lib/x86_64-linux-gnu/gtk-3.0/modules/libx.so", "usr/share/doc/libgtkmod/copyright"],
                     "libdri": ["usr/lib/dri/x_dri.so"], "libold": ["lib/x86_64-linux-gnu/libold.so.1"],
                     "libdoc": ["usr/lib/x86_64-linux-gnu/libdoc.so.1", "usr/share/man/man3/doc.3.gz", "usr/share/lintian/overrides/libdoc"]}.items():
        pkgfiles(tmp_path, n, files)
    st = states(cp.apt_autoremove_unused(mk("apt_autoremove_unused", protected={"patterns": []})))
    assert all(st[n][0] == "would purge" for n in ("libgtkmod", "libdri", "libold", "libdoc")), st


# ---- 2. dormant, activatable services and Recommends of packages that stay ---------------------------------------------------
def test_review_xapp_sn_watcher_is_kept_even_when_the_owner_waives_the_class_check(host, tmp_path):
    """It ships a D-Bus activation file and an autostart entry; nothing maps it TODAY (NameHasOwner=false only because nothing
    asked yet) and libxapp1 (kept: mapped by gsd-*) Recommends it, which `apt-get -s purge` does not see."""
    auto_host(host, tmp_path)
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, keep=KEEP, allow_purge=["xapp-sn-watcher"]))
    st = states(res)
    assert st["xapp-sn-watcher"] == ("kept: needed by installed pkg", "libxapp1 depends on or recommends it")
    assert "xapp-sn-watcher" not in (host.purges[0] if host.purges else [])
    # with the default policy it is kept even earlier, by what it ships
    assert states(cp.apt_autoremove_unused(mk("apt_autoremove_unused", keep=KEEP)))["xapp-sn-watcher"][0] == "kept: not provable from /proc"


def test_review_recommends_depends_and_provides_of_packages_that_stay_keep_the_candidate(host, tmp_path):
    mini(host, tmp_path, ["liba", "libb", "libc-virt-impl", "libd", "libe"])
    host.add("manual-app", "1", "ii", 5)                                       # NOT an autoremove candidate: stays
    host.recommends["manual-app"] = {"liba"}                                   # recommends (the simulation never sees it)
    host.depends["manual-app"] = {"libb"}
    host.depends["libusing-virtual"] = {"virt-impl"}
    host.add("libusing-virtual", "1", "ii", 5)
    host.provides["libc-virt-impl"] = {"virt-impl"}                            # a virtual package some installed package needs
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, protected={"patterns": []}))
    st = states(res)
    assert st["liba"] == ("kept: needed by installed pkg", "manual-app depends on or recommends it")
    assert st["libb"][0] == "kept: needed by installed pkg" and st["libc-virt-impl"][0] == "kept: needed by installed pkg"
    assert host.purges == [["libd", "libe"]]


def test_review_referrer_that_is_itself_purged_does_not_block_and_a_kept_one_cascades(host, tmp_path):
    mini(host, tmp_path, ["libx", "liby", "libz", "libw"])
    host.recommends["libx"] = {"liby"}                    # libx is purged too: liby is only needed by a package going away
    host.recommends["libw"] = {"libz"}
    mkproc(tmp_path, 4012, "app", maps=mapline(lib(tmp_path, "libw")))     # libw is in use => stays => libz must stay
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, protected={"patterns": []}))
    st = states(res)
    assert st["libw"][0] == "kept: in use" and st["libz"][0] == "kept: needed by installed pkg"
    assert sorted(host.purges[0]) == ["libx", "liby"]


def test_review_new_failed_user_unit_after_the_purge_is_crit(host, tmp_path):
    """A system-unit check cannot see a broken desktop session: failed units of logged-in users are compared too."""
    auto_host(host, tmp_path)
    host.users = {"ohmz": [(0, "old.service loaded failed failed Old\n", ""),
                           (0, "old.service loaded failed failed Old\napp-xapp.service loaded failed failed X\n", "")]}
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, keep=KEEP))
    assert res.status == "crit" and "NEW failed units: ohmz:app-xapp.service" in res.summary and host.purges


def test_review_user_units_unchanged_or_unreadable_before_do_not_cry_wolf(host, tmp_path, monkeypatch):
    auto_host(host, tmp_path)
    host.users = {"ohmz": [(0, "old.service loaded failed failed Old\n", "")]}              # same failure before and after
    assert cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, keep=KEEP)).status == "ok"
    # unreadable before the purge: not comparable (no false alarm from "everything is new"); the system-unit check gates alone
    inuse.reset_caches()
    h = auto_host(Host(), tmp_path)
    h.users = {"ohmz": [(1, "", "no bus"), (0, "x.service loaded failed failed X\n", "")]}
    monkeypatch.setattr(cp, "sh", h)
    monkeypatch.setattr(cp, "_status_text", h.status_text)
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, keep=KEEP))
    assert res.status == "ok" and h.purges


def test_review_user_units_readable_before_but_not_after_is_warn(host, tmp_path):
    auto_host(host, tmp_path)
    host.users = {"ohmz": [(0, "", ""), (1, "", "Failed to connect to bus")]}
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, keep=KEEP))
    assert res.status == "warn" and "user units unreadable after the purge" in res.summary


# ---- 3. a partial /proc view must never read as "nothing uses it" -----------------------------------------------------------
def partial_view(tmp_path, monkeypatch, total=7776):
    """The reviewer's `unshare --pid --fork --mount-proc` shape: our pid (+ pid 1) visible, the kernel counts thousands more."""
    from test_inuse import host_view
    monkeypatch.setattr(inuse, "FULL_VIEW_CHECK", True)
    host_view(tmp_path, total=total)
    inuse.reset_caches()


def test_review_partial_proc_view_selects_nothing_in_autoremove(host, tmp_path, monkeypatch):
    auto_host(host, tmp_path)
    ok = cp.apt_autoremove_unused(mk("apt_autoremove_unused", keep=KEEP))
    assert sorted(n for n, (s, _) in states(ok).items() if s == "would purge") == WOULD          # complete view: as before
    partial_view(tmp_path, monkeypatch, total=50)             # a handful of processes, fifty threads on the machine
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, keep=KEEP))
    assert res.metrics["selected"] == 0 and host.purges == []
    st = states(res)
    assert all(s == "kept: unknown (cannot prove)" and "partial /proc view" in w for n, (s, w) in st.items() if n not in KEEP), st


def test_review_partial_proc_view_refuses_the_stale_driver_set(host, tmp_path, monkeypatch):
    driver_host(host, tmp_path)
    partial_view(tmp_path, monkeypatch)
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, "stale driver file in use: unknown")
    assert host.purges == []


def test_review_partial_proc_view_stops_flatpak(host, tmp_path, monkeypatch):
    fp_host(host)
    partial_view(tmp_path, monkeypatch)
    res = cp.flatpak_unused(fp_ctx(apply=True))
    assert res.status == "skipped" and "cannot see every process" in res.summary and host.mutating() == []


# ---- 4. owner options: a bare string is iterated per character ---------------------------------------------------------------
@pytest.mark.parametrize("opt,val", [("keep", "tree"), ("keep", ["tree", ""]), ("keep", [1]), ("keep", {"tree": 1}), ("keep", 5),
                                     ("never", "libdrm-*"), ("never", [None]), ("allow_purge", "solaar"), ("allow_purge", [" "])])
def test_review_malformed_autoremove_option_skips_the_task_before_any_probe(host, tmp_path, opt, val):
    auto_host(host, tmp_path)
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, **{opt: val}))
    assert res.status == "skipped" and f"bad config: {opt} must be a list" in res.summary and res.summary.endswith("nothing done")
    assert host.purges == [] and not [c for c in host.calls if c.startswith(("apt-get", "dpkg"))]     # not even a simulation


def test_review_keep_as_a_string_used_to_unprotect_tree_and_inxi(host, tmp_path):
    auto_host(host, tmp_path)
    res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", keep="tree"))                   # per-character: t,r,e => no protection
    assert res.status == "skipped"
    st = states(cp.apt_autoremove_unused(mk("apt_autoremove_unused", keep=["tree", "inxi"])))
    assert st["tree"][0] == st["inxi"][0] == "kept: owner keep-list"


@pytest.mark.parametrize("val", ["535", 535, [535], ["5"], ["535x"], [""], None, {"535": 1}])
def test_review_malformed_keep_branches_skips_the_driver_task(host, tmp_path, val):
    driver_host(host, tmp_path)
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True, keep_branches=val))
    assert res.status == "skipped" and "bad config: keep_branches must be a list" in res.summary
    assert host.purges == [] and not [c for c in host.calls if c.startswith("apt-get")]


def test_review_keep_branches_string_used_to_purge_the_branch_the_owner_kept(host, tmp_path):
    driver_host(host, tmp_path)
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True, keep_branches=["535"]))
    assert res.status == "ok" and not any("535" in n for n in host.purges[0])


@pytest.mark.parametrize("val", ["system", [1], [""], {"system": 1}, 3])
def test_review_malformed_flatpak_installations_skip_the_task(host, val):
    fp_host(host)
    res = cp.flatpak_unused(mk("flatpak_unused", apply=True, installations=val))
    assert res.status == "skipped" and "bad config: installations" in res.summary and host.mutating() == []
    assert not [c for c in host.calls if c.startswith("flatpak")]


# ---- 5. dlopen()ed stacks the /proc proof cannot see: the never list ---------------------------------------------------------
@pytest.mark.parametrize("name", [
    "libcudart12", "libcublas12", "libcusparse12", "libcufft11", "libcurand10", "libcusolver11", "cuda-toolkit-12-4",
    "nvidia-cuda-toolkit", "libcudnn8", "libnccl2", "nsight-systems", "nsight-compute", "tensorrt", "libnvinfer10", "libnvjpeg12",
    "openjdk-17-jre-headless", "openjdk-21-jdk", "default-jre", "ovmf", "swtpm-tools", "intel-microcode", "amd64-microcode",
    "firmware-sof-signed", "mdadm", "lvm2", "zfsutils-linux", "cryptsetup-bin", "netplan.io", "network-manager-openvpn", "ufw",
    "openssh-sftp-server", "nftables", "iptables", "apparmor-utils", "cloud-init"])
def test_review_gpu_jdk_hardware_and_network_stacks_are_never_selected(host, tmp_path, name):
    res = autoremove(host, tmp_path, [name, "plain-one"])
    st = states(res)
    assert st[name][0] == "kept: critical package class" and st["plain-one"][0] == "would purge"


def test_review_never_list_does_not_overreach_into_ordinary_libraries(host, tmp_path):
    res = autoremove(host, tmp_path, ["libcurl4-extra", "libcups2", "libcue2", "libnotify4"])
    assert all(v[0] == "would purge" for v in states(res).values()), states(res)


# ---- 6. vacuous proofs ----------------------------------------------------------------------------------------------------------
def test_review_metapackage_with_nothing_to_check_is_unknown_not_unused(host, tmp_path):
    """Only /usr/share/doc content: nothing was checked, so nothing was proven. Daily runs used to peel such stacks."""
    mini(host, tmp_path, ["meta-thing"], files=["usr/share/doc/meta-thing/changelog.gz", "usr/share/doc/meta-thing/copyright"])
    mini(host, tmp_path, ["ghost"], files=["usr/lib/x86_64-linux-gnu/ghost.so.1"])
    os.remove(tmp_path / "root/usr/lib/x86_64-linux-gnu/ghost.so.1")           # listed but gone from disk
    st = states(cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, protected={"patterns": []})))
    assert st["meta-thing"] == ("kept: unknown (cannot prove)", "unknown: no files on disk to prove anything about")
    assert st["ghost"][0] == "kept: unknown (cannot prove)"
    assert host.purges == []


def test_review_deleted_but_still_mapped_library_of_a_candidate_counts(host, tmp_path):
    mini(host, tmp_path, ["libreplaced", "libother"])
    # the file was replaced on disk (still listed) while an old process still maps the deleted original
    mkproc(tmp_path, 4013, "old", maps=mapline(lib(tmp_path, "libreplaced") + " (deleted)"))
    assert states(cp.apt_autoremove_unused(mk("apt_autoremove_unused", protected={"patterns": []})))["libreplaced"][0] == "kept: in use"


# ---- 7. stale driver: an unreadable file list is not "no library mapped" -----------------------------------------------------
def test_review_stale_set_with_unreadable_file_lists_is_refused(host, tmp_path):
    driver_host(host, tmp_path, stale_files=False)               # no .list for any stale package; `dpkg-query -L` fails too
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, "file list of ")
    assert host.purges == []


def test_review_stale_set_with_one_unreadable_or_empty_list_is_refused(host, tmp_path):
    driver_host(host, tmp_path)
    (tmp_path / "dpkg-info" / "libnvidia-compute-535.list").unlink()
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, "file list of libnvidia-compute-535 unreadable")
    (tmp_path / "dpkg-info" / "libnvidia-compute-535.list").write_text("/.\n")              # damaged: lists nothing at all
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, "file list of libnvidia-compute-535 is empty")


def test_review_stale_list_lost_between_the_proofs_and_the_purge_aborts_it(host, tmp_path):
    driver_host(host, tmp_path)
    lst = tmp_path / "dpkg-info" / "libnvidia-compute-535.list"
    hits = []

    def hook(names, h):
        if not hits:
            hits.append(lst.unlink())                             # after the report-time proofs, before purge()'s re-proof
        return None

    host.sim_hook = hook
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert host.purges == [] and res.metrics["gone"] == 1


def test_review_config_only_stale_package_needs_no_file_list(host, tmp_path):
    driver_host(host, tmp_path)
    (tmp_path / "dpkg-info" / "libnvidia-gl-575.list").unlink()          # state `rc`: dpkg keeps no list, owns no files
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert res.status == "ok" and "libnvidia-gl-575" in host.purges[0]


# ---- 8. the module the NEXT boot loads -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("ondisk,text", [("575.57.08", "is 575.57.08 but 580.173.02 is loaded"), ("", "is unreadable but 580.173.02")])
def test_review_module_on_disk_is_not_the_loaded_one_refuses(host, tmp_path, monkeypatch, ondisk, text):
    driver_host(host, tmp_path)
    monkeypatch.setattr(inuse, "loaded_nvidia_driver", lambda: Driver("580.173.02", "580", ondisk, "loaded 580.173.02"))
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert res.status == "skipped" and text in res.summary and "nothing touched" in res.summary
    assert host.mutating() == [] and not [c for c in host.calls if c.startswith("apt-get -s")]


def test_review_purge_that_removes_the_next_boot_module_is_crit_with_the_recovery_command(host, tmp_path):
    """dkms removes /lib/modules/<k>/updates/dkms/nvidia.ko* when the removed build was the active one. nvidia-smi still answers
    from the module in memory and `dkms status` still lists 580: only `modinfo` shows what the next boot would find."""
    driver_host(host, tmp_path)
    orig = host.answer

    def answer(key):
        r = orig(key)
        if key.startswith("apt-get -y purge"):
            os.remove(host.mod_file)
        return r

    host.answer = answer
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    ascii_ok(res)
    assert res.status == "crit" and host.purges
    assert f"dkms install nvidia/580.173.02 -k {KERNEL}" in res.summary
    assert res.items[0]["name"] == "post-purge check" and "nvidia module file is missing" not in res.items[0]["proof"] \
        and f"dkms install nvidia/580.173.02 -k {KERNEL}" in res.items[0]["proof"]


def test_review_modinfo_reporting_another_version_after_the_purge_is_crit(host, tmp_path):
    driver_host(host, tmp_path)
    orig = host.answer

    def answer(key):
        if key == "modinfo -F version nvidia" and host.purges:
            return (0, "575.57.08\n", "")
        return orig(key)

    host.answer = answer
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert res.status == "crit" and "next boot" in res.summary


def test_review_loaded_driver_files_that_stop_verifying_after_the_purge_are_crit(host, tmp_path):
    driver_host(host, tmp_path)
    orig = host.answer

    def answer(key):
        if key.startswith("dpkg --verify") and host.purges:
            return (1, "missing     /usr/bin/nvidia-smi\n", "")
        return orig(key)

    host.answer = answer
    res = cp.stale_driver_packages(mk("stale_driver_packages", apply=True))
    assert res.status == "crit" and "no longer verify" in res.items[0]["proof"]


# ---- 9. purge deletes locally edited conffiles for good -----------------------------------------------------------------------
def add_conf(host, tmp_path, name, *, text="orig", recorded="orig", write=True, md5=None):
    p = tmp_path / "etc" / f"{name}.conf"
    p.parent.mkdir(exist_ok=True)
    if write:
        p.write_text(text)
    host.conffiles.setdefault(name, []).append((str(p), md5 or hashlib.md5(recorded.encode()).hexdigest()))
    return p


def test_review_autoremove_keeps_packages_whose_conffile_the_owner_edited(host, tmp_path):
    mini(host, tmp_path, ["libinxi-like", "libpristine", "libdeleted", "libunreadable", "libnochecksum"])
    add_conf(host, tmp_path, "libinxi-like", text="my own settings")                  # edited: purge would destroy it
    add_conf(host, tmp_path, "libpristine")                                           # untouched: nothing to lose
    add_conf(host, tmp_path, "libdeleted", write=False)                               # owner deleted it: nothing to lose
    p = add_conf(host, tmp_path, "libunreadable")
    add_conf(host, tmp_path, "libnochecksum", md5="newconffile")                      # dpkg has no checksum for it: cannot tell
    os.chmod(p, 0)
    try:
        res = cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True, protected={"patterns": []}))
    finally:
        os.chmod(p, 0o644)
    st = states(res)
    if os.geteuid() == 0:                                                              # root reads mode-0 files
        assert st["libunreadable"][0] == "would purge" or st["libunreadable"][0] == "purged"
    else:
        assert st["libunreadable"][0] == "kept: edited config" and "unreadable" in st["libunreadable"][1]
    assert st["libinxi-like"][0] == "kept: edited config" and "was edited (purge would delete it)" in st["libinxi-like"][1]
    assert st["libnochecksum"][0] == "kept: edited config" and "no usable checksum" in st["libnochecksum"][1]
    assert st["libpristine"][0] == "purged" and st["libdeleted"][0] == "purged"
    assert "libinxi-like" not in host.purges[0] and "libpristine" in host.purges[0]


def test_review_candidate_missing_from_the_dpkg_database_is_kept(host, tmp_path, monkeypatch):
    mini(host, tmp_path, ["libghost-db", "libreal"])
    orig = host.status_text
    monkeypatch.setattr(cp, "_status_text", lambda: orig().replace("Package: libghost-db", "Package: libother-name"))
    st = states(cp.apt_autoremove_unused(mk("apt_autoremove_unused", protected={"patterns": []})))
    assert st["libghost-db"] == ("kept: edited config", "package not in the dpkg database") and st["libreal"][0] == "would purge"


def test_review_unreadable_dpkg_status_stops_both_apt_tasks(host, tmp_path, monkeypatch):
    auto_host(host, tmp_path)
    driver_host(host, tmp_path)
    monkeypatch.setattr(cp, "_status_text", lambda: None)
    assert cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True)).status == "skipped"
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, "dpkg status file unreadable")
    monkeypatch.setattr(cp, "_status_text", lambda: "Package: broken\nthis line has no colon\n")
    assert cp.apt_autoremove_unused(mk("apt_autoremove_unused", apply=True)).status == "skipped" and host.purges == []


def test_review_stale_set_with_an_edited_conffile_is_refused_and_pristine_is_purged(host, tmp_path):
    driver_host(host, tmp_path)
    add_conf(host, tmp_path, "nvidia-utils-570", text="owner tweaked this")
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, "nvidia-utils-570: ")
    assert "was edited (purge would delete it)" in cp.stale_driver_packages(mk("stale_driver_packages")).items[0]["proof"]
    host.conffiles.clear()
    add_conf(host, tmp_path, "nvidia-utils-570")                                       # pristine
    add_conf(host, tmp_path, "libnvidia-gl-575", text="edited rc conffile")            # config-only package: purge still deletes it
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, "libnvidia-gl-575: ")
    host.conffiles.clear()
    assert cp.stale_driver_packages(mk("stale_driver_packages", apply=True)).status == "ok"


def test_status_file_parser_handles_real_world_shapes(monkeypatch):
    native = "amd64"
    cp_text = (
        "Package: libglapi-mesa\nStatus: install ok installed\nArchitecture: i386\nMulti-Arch: same\n"
        "Depends: libc6:i386 (>= 2.34), libx11-6 | libfoo (>= 1) [amd64], foo:any\nPre-Depends: dpkg (>= 1.17.5)\n"
        "Recommends: bar (>= 1.0) <!nocheck>\nProvides: libgl-abi (= 1)\nConffiles:\n /etc/a.conf 0123456789abcdef0123456789abcdef\n"
        " /etc/b.conf 0123456789abcdef0123456789abcdef obsolete\nDescription: x\n y: z\n .\n more\n\n"
        "Package: python3-attr\nStatus: install ok installed\nArchitecture: all\nDepends: python3\n\n"
        "Package: oldcfg\nStatus: deinstall ok config-files\nArchitecture: amd64\nDepends: gone\n\n")
    monkeypatch.setattr(cp, "_status_text", lambda: cp_text)
    db = cp._dpkg_db(native)
    assert set(db) == {"libglapi-mesa:i386", "python3-attr", "oldcfg"}          # foreign arch qualified, arch:all bare
    r = db["libglapi-mesa:i386"]
    assert r.needs == {("libc6", "i386"), ("libx11-6", ""), ("libfoo", ""), ("foo", "any"), ("dpkg", ""), ("bar", "")}
    assert r.provides == {"libgl-abi"} and r.arch == "i386" and r.foreign is False
    assert [c[0] for c in r.conffiles] == ["/etc/a.conf", "/etc/b.conf"] and r.installed
    assert not db["oldcfg"].installed                                          # config-files only never counts as a referrer
    idx = cp._referrer_index(db)
    assert idx["python3"] == [("python3-attr", "all", "")] and "gone" not in idx


# ---- 10. flatpak: never rely on what EOF means to the installed flatpak; other installations' apps -------------------------------
def test_review_a_flatpak_that_treats_eof_as_yes_still_uninstalls_nothing_in_report_mode(host):
    fp_host(host)
    host.eof_is_yes = True                    # a future flatpak: `uninstall --unused` with EOF on stdin == the default answer == yes
    res = cp.flatpak_unused(fp_ctx())                                  # REPORT mode
    assert "done" not in host.flatpak["system"] and host.flatpak["system"]["unused"] == UNUSED_TABLE     # nothing was uninstalled
    assert res.status == "info" and res.metrics["selected"] == 3 and host.mutating() == []
    assert all(i == "n\n" for c, i in zip(host.calls, host.inputs) if c.endswith("uninstall --unused"))


@pytest.fixture
def ohmz(monkeypatch):
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda n: pwd.struct_passwd(("ohmz", "x", 1000, 1000, "", "/home/ohmz", "/bin/bash")))


def user_apps(host, runtimes, rc=0):
    host.flatpak["user"] = {"apps": [("com.x.App", "stable", "cccccccccccc")], "runtimes": [], "unused": [], "raw": None,
                            "app_rt": runtimes, "app_rt_rc": rc}


def test_review_user_app_running_on_a_system_runtime_blocks_the_system_list(host, ohmz):
    fp_host(host)
    user_apps(host, ["org.gnome.Platform/x86_64/46"])                          # the system list wants to remove Platform//46
    res = cp.flatpak_unused(mk("flatpak_unused", apply=True, installations=["system", "user:ohmz"]))
    assert res.metrics["refused"] == 1 and host.mutating() == []
    row = res.items[0]
    assert row["state"] == "refused: used across installations" and "an app of user ohmz runs on org.gnome.Platform//46" in row["proof"]


def test_review_user_installation_is_a_consumer_even_when_not_configured(host, tmp_path, ohmz, monkeypatch):
    fp_host(host)
    user_apps(host, ["org.gnome.Platform/x86_64/46"])
    (tmp_path / "home" / "ohmz" / ".local" / "share" / "flatpak").mkdir(parents=True)      # discovered, not configured
    res = cp.flatpak_unused(fp_ctx(apply=True))
    assert res.metrics["refused"] == 1 and "used across installations" in res.items[0]["state"] and host.mutating() == []


def test_review_unreadable_other_installation_refuses_instead_of_guessing(host, tmp_path, ohmz):
    fp_host(host)
    user_apps(host, [], rc=1)
    (tmp_path / "home" / "ohmz" / ".local" / "share" / "flatpak").mkdir(parents=True)
    res = cp.flatpak_unused(fp_ctx(apply=True))
    assert res.items[0]["state"] == "refused: cannot cross-check" and host.mutating() == []


def test_review_user_apps_on_other_runtimes_do_not_block(host, tmp_path, ohmz):
    fp_host(host)
    user_apps(host, ["org.kde.Platform/x86_64/6.10", "org.gnome.Platform/x86_64/99"])
    (tmp_path / "home" / "ohmz" / ".local" / "share" / "flatpak").mkdir(parents=True)
    res = cp.flatpak_unused(fp_ctx(apply=True))
    assert res.status == "ok" and [c for c in host.calls if "uninstall --unused -y" in c]


def test_flatpak_app_runtime_column_parsing(host):
    fp_host(host)
    host.flatpak["system"]["app_rt"] = ["org.gnome.Platform/x86_64/46", "", "org.freedesktop.Platform/x86_64/25.08-extra"]
    assert cp._fp_app_runtimes(["flatpak", "--system"]) == {("org.gnome.Platform", "46"), ("org.freedesktop.Platform", "25.08-extra")}
    host.flatpak["system"]["app_rt"] = ["not a ref"]
    assert cp._fp_app_runtimes(["flatpak", "--system"]) is None
    host.flatpak["system"]["app_rt_rc"] = 1
    host.flatpak["system"]["app_rt"] = []
    assert cp._fp_app_runtimes(["flatpak", "--system"]) is None


def test_review_dependency_in_another_architecture_does_not_keep_the_twin(host, tmp_path):
    """xserver-xorg-video-radeon (amd64) needs libdrm-radeon1 (amd64): it says nothing about the i386 twin (live host)."""
    mini(host, tmp_path, ["libdrm-radeon1:i386", "libany", "libqual", "libqual:i386"])
    host.add("xserver-xorg-video-radeon", "1", "ii", 5)
    host.depends["xserver-xorg-video-radeon"] = {"libdrm-radeon1"}              # unqualified, from an amd64 package: amd64 libdrm
    host.add("needs-any", "1", "ii", 5)
    host.depends["needs-any"] = {"libany:any"}
    host.add("needs-i386", "1", "ii", 5)
    host.depends["needs-i386"] = {"libqual:i386"}                               # an explicit foreign arch names the i386 package only
    st = states(cp.apt_autoremove_unused(mk("apt_autoremove_unused", protected={"patterns": []})))
    assert st["libdrm-radeon1:i386"][0] == "would purge"
    assert st["libany"] == ("kept: needed by installed pkg", "needs-any depends on or recommends it")
    assert st["libqual"][0] == "would purge"
    assert st["libqual:i386"] == ("kept: needed by installed pkg", "needs-i386 depends on or recommends it")


def test_referrers_match_architectures_the_careful_way(monkeypatch):
    text = ("Package: tool\nStatus: install ok installed\nArchitecture: amd64\nDepends: libbar, libfoo, libz\n\n"
            "Package: libfoo\nStatus: install ok installed\nArchitecture: i386\nMulti-Arch: foreign\n\n"      # foreign: satisfies all
            "Package: libbar\nStatus: install ok installed\nArchitecture: i386\nMulti-Arch: same\n\n"
            "Package: libz\nStatus: install ok installed\nArchitecture: all\n\n"
            "Package: docs\nStatus: install ok installed\nArchitecture: all\nRecommends: libbar\n\n")
    monkeypatch.setattr(cp, "_status_text", lambda: text)
    db = cp._dpkg_db("amd64")
    idx = cp._referrer_index(db)
    assert cp._referrers("libfoo:i386", db, idx) == {"tool"}                    # Multi-Arch: foreign
    assert cp._referrers("libbar:i386", db, idx) == {"docs"}                    # amd64 `tool` does not need the i386 libbar; arch:all does
    assert cp._referrers("libz", db, idx) == {"tool"}                           # arch:all is needed by every architecture
    assert cp._referrers("not-installed-at-all", db, idx) == set()


def test_review_stale_set_without_any_library_on_an_unreadable_proc_is_still_unknown(host, tmp_path, monkeypatch):
    """No stale library => nothing to look up; on an unreadable /proc that used to read as "no stale lib mapped"."""
    only_src = {"nvidia-dkms-535": ("535.274.02-0ubuntu0.24.04.1", 150, "ii", False),
                "nvidia-kernel-source-535": ("535.274.02-0ubuntu0.24.04.1", 120_000, "ii", False)}
    driver_host(host, tmp_path, stale=only_src)
    mkproc(tmp_path, 701, "rootproc")
    deny_maps(monkeypatch, only=701)
    refused(cp.stale_driver_packages(mk("stale_driver_packages", apply=True)), host, "stale driver file in use: unknown")
