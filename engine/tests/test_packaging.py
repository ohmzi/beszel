"""Packaging tests: installer, uninstaller, systemd units, drop-in and README.

Three layers, none of which touches the real system:
  * static checks of the shell scripts (bash -n, required strings, "every mutation goes through
    run()", "install never starts a tier service", idempotency markers);
  * unit-file checks, including `systemd-analyze verify` on temporary copies;
  * consistency of the shipped config (every job, routine step and probe names something real; every registered cleaner is a
    routine step with a [tasks.X] table and a playbook; only the spike ladder's reclaim rung ships on);
  * the acknowledge postbox and the rules registry as the installer lays them out (modes, secrets generated once and never
    overwritten, rules.d only when the release ships rules, adoption never silent);
  * real runs of install.sh / uninstall.sh against a staging prefix (HM_ROOT=<tmp>), inside an
    unprivileged user namespace so the scripts' root check passes without real root; the live-systemd branch of both
    scripts is only ever run with --dry-run and a stub `systemctl` first on PATH (see `_live_dry`).
Every script run below sets HM_ROOT or is a stubbed dry run; see `_run`, which refuses to run without HM_ROOT.
"""
from __future__ import annotations

import functools
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: E402,F401  (points HOMELAB_MAINT_* at temp dirs, adds the repo to sys.path)

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "install.sh"
UNINSTALL = REPO / "uninstall.sh"
SYSTEMD = REPO / "systemd"
README = REPO / "README.md"
ETC = REPO / "etc"

TIER_SERVICES = ("check", "daily", "weekly")
# One umbrella: the tick (with metrics) runs next to the tier timers; there is deliberately NO routine timer. The self-health refresher is
# a timer of its own on purpose: it is the dead-man of the tick, so it must not run from it.
ENABLED_TIMERS = {f"homelab-maint-{n}.timer" for n in (*TIER_SERVICES, "metrics", "tick", "selfhealth")}
DAEMONS = {"homelab-maint-www.service", "homelab-maint-live.service"}
# Triggered by their timers only: install.sh must never start, restart or enable these directly.
TIMER_DRIVEN = {f"homelab-maint-{n}.service" for n in (*TIER_SERVICES, "metrics", "tick", "selfhealth")}
ORIGINAL_IMMICH_UNIT = """\
[Unit]
Description=Restart immich_server to bound memory growth during large job backlogs
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/docker restart immich_server
"""
DROPIN_REL = "immich-server-recycle.service.d/10-homelab-gate.conf"


# --------------------------------------------------------------------------- helpers
def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _userns_ok() -> bool:
    """Can we get a 'root' (EUID 0) that may chown to root:root, without real privileges?"""
    if not _have("unshare"):
        return False
    r = subprocess.run(["unshare", "-r", "bash", "-c", "[[ $EUID -eq 0 ]] && touch \"$0\" && chown root:root \"$0\"",
                        os.path.join(os.environ["HOMELAB_MAINT_RUN"], "userns-probe")],
                       capture_output=True, text=True)
    return r.returncode == 0


needs_userns = pytest.mark.skipif(not _userns_ok(), reason="unprivileged user namespaces unavailable")
not_root = pytest.mark.skipif(os.geteuid() == 0, reason="tests the non-root refusal")


def code_lines(path: Path) -> list[tuple[int, str]]:
    """Script lines with comments and quoted strings blanked out, so token checks see code only."""
    out = []
    in_heredoc = None
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        if in_heredoc:
            if raw.strip() == in_heredoc:
                in_heredoc = None
            continue
        m = re.search(r"<<-?\s*'?(\w+)'?\s*(\|\||$)", raw)
        line = re.sub(r"\"[^\"]*\"|'[^']*'", '""', raw)
        line = re.sub(r"(^|\s)#.*$", "", line)
        if line.strip():
            out.append((n, line))
        if m:
            in_heredoc = m.group(1)
    return out


def parse_unit(path: Path) -> dict[str, dict[str, list[str]]]:
    """Minimal systemd INI parser (keys may repeat, so values are lists)."""
    sections: dict[str, dict[str, list[str]]] = {}
    cur = None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = sections.setdefault(line[1:-1], {})
        else:
            key, _, val = line.partition("=")
            assert cur is not None, f"{path}: key before any section: {raw}"
            cur.setdefault(key.strip(), []).append(val.strip())
    return sections


def one(unit: dict, section: str, key: str) -> str:
    vals = unit.get(section, {}).get(key, [])
    assert len(vals) == 1, f"{section}/{key}: expected exactly one value, got {vals}"
    return vals[0]


def cli_subcommands() -> set[str]:
    return set(re.findall(r'add_parser\("([\w-]+)"', (REPO / "homelab_maint" / "cli.py").read_text()))


def tree_state(root: Path, skip: tuple[str, ...] = ("run",)) -> dict[str, tuple]:
    """Mode, mtime and content hash of every file/dir under root (for 'nothing changed' checks)."""
    snap = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if rel.parts[0] in skip:
            continue
        st = p.lstat()
        digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else ""
        snap[str(rel)] = (st.st_mode, st.st_mtime_ns, digest)
    return snap


# --------------------------------------------------------------------------- fake project
def make_project(tmp: Path, real_tree: bool = False) -> Path:
    """A throw-away copy of the repo layout. The scripts find their source next to themselves, so
    running tmp/proj/install.sh installs *that* tree. The real systemd/ directory is always used."""
    proj = tmp / "proj"
    proj.mkdir()
    for s in (INSTALL, UNINSTALL):
        shutil.copy2(s, proj / s.name)
    shutil.copytree(SYSTEMD, proj / "systemd")
    if real_tree:
        shutil.copytree(REPO / "homelab_maint", proj / "homelab_maint", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(REPO / "etc", proj / "etc")
        shutil.copy2(REPO / "homelab-maint", proj / "homelab-maint")
        return proj
    pkg = proj / "homelab_maint"
    (pkg / "tasks").mkdir(parents=True)
    for name in ("__init__.py", "cli.py", "core.py", "server.py", "tasks/__init__.py", "tasks/one.py"):
        (pkg / name).write_text(f'"""fake {name}"""\nVALUE = 1\n')
    (proj / "homelab-maint").write_text("#!/usr/bin/env python3\nprint('fake entry')\n")
    (proj / "etc").mkdir()
    (proj / "etc" / "maint.toml").write_text(
        '[global]\nwww_port = 9111\n[tasks.cleaner_a]\nmode = "report"\n[tasks.cleaner_b]\nmode = "apply"\n')
    (proj / "etc" / "protected.toml").write_text('patterns = ["postgres"]\n')
    return proj


class Env:
    """One staged install target plus the project to install from."""

    def __init__(self, tmp: Path, real_tree: bool = False):
        self.tmp = tmp
        self.root = tmp / "root"
        self.root.mkdir()
        (tmp / "tmpdir").mkdir()
        self.proj = make_project(tmp, real_tree)

    def with_original_unit(self) -> None:
        d = self.root / "etc/systemd/system"
        d.mkdir(parents=True, exist_ok=True)
        (d / "immich-server-recycle.service").write_text(ORIGINAL_IMMICH_UNIT)

    def p(self, rel: str) -> Path:
        return self.root / rel

    def _run(self, script: str, *args: str, userns: bool = True, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "HM_ROOT": str(self.root), "TMPDIR": str(self.tmp / "tmpdir"), **(extra_env or {})}
        assert env["HM_ROOT"].startswith(str(self.tmp)), "test must only ever target its own tmp dir"
        cmd = ["bash", str(self.proj / script), *args]
        if userns:
            cmd = ["unshare", "-r", *cmd]
        return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)

    def install(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return self._run("install.sh", *args, **kw)

    def uninstall(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return self._run("uninstall.sh", *args, **kw)


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


# --------------------------------------------------------------------------- static: shell scripts
@pytest.mark.parametrize("script", [INSTALL, UNINSTALL])
def test_scripts_syntax_shebang_strict_mode_and_exec_bit(script):
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = script.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    head = [ln for ln in text.splitlines()[:60] if ln.strip() and not ln.lstrip().startswith("#")]
    assert head[0] == "set -euo pipefail", head[:3]
    assert os.access(script, os.X_OK), f"{script.name} must be executable"


def test_install_required_strings():
    t = INSTALL.read_text()
    for needle in (
        "$EUID", "must run as root",                       # refuses unless root
        "/usr/local/lib/homelab-maint", "/usr/local/sbin/homelab-maint",
        "/etc/homelab-maint", "/var/lib/homelab-maint", "/var/log/homelab-maint",
        "/etc/systemd/system", "daemon-reload",
        "homelab-maint-check.timer", "homelab-maint-daily.timer", "homelab-maint-weekly.timer",
        "homelab-maint-metrics.timer", "homelab-maint-tick.timer", "homelab-maint-selfhealth.timer",
        "homelab-maint-www.service", "homelab-maint-live.service", "systemd/dropins",
        "--dry-run", "--no-start", "--first-check", "--deploy-web", "--no-web-ready", "--adopt-rules",
        "wait_web_healthy", "mark_web_ready", "web bootstrap",                                # the e-mail button follows the site; the login secret
        "$STATE/ack/inbox", "$STATE/ack/web.key", "$STATE/ack/bootstrap.secret", "10001",     # the acknowledge postbox
        "rules sync --adopt", "00-baseline-invariants.toml",                                  # the registry's adoption command
        "docker-compose.override.yml",                      # the website needs BOTH compose files on this host
        "plugins.d", "probes.d", "legacy-retirement.d",     # the owner's extension points
    ):
        assert needle in t, f"install.sh lacks {needle!r}"


def test_install_idempotency_markers():
    t = INSTALL.read_text()
    assert "cmp -s" in t, "files must be compared before being rewritten"
    assert "diff -rq" in t, "the package tree must be compared before being swapped"
    assert "flock -n" in t, "concurrent installs must be refused"
    assert "mv -T" in t, "package swap must be a rename"
    assert "install -d" in t, "directories must be created idempotently"
    assert ".dist" in t, "existing config must be kept, with the shipped copy beside it"
    assert re.search(r'\[\[ -e \$dest \|\| -L \$dest \]\]', t), "config is only installed when absent"
    assert "systemctl is-enabled" in t and "systemctl is-active" in t, "enable/start must check current state"
    assert "NeedDaemonReload" in t, "an interrupted earlier run must be recoverable"


@pytest.mark.parametrize("script", [INSTALL, UNINSTALL])
def test_every_mutation_goes_through_run(script):
    """With --dry-run exact, no mutating command may run outside run()/ctl(), except in the temp stage."""
    mutators = r"(?:install|cp|mv|rm|rmdir|chmod|chown|ln|mkdir|touch|tee|truncate|dd)"
    sysctl_mut = r"systemctl\s+(?:enable|disable|start|stop|restart|try-restart|daemon-reload|reset-failed|mask|unmask|kill|set-property)"
    pat = re.compile(rf"(?:^\s*|[;&|(]\s*|\b(?:then|else|do)\s+)(?P<cmd>{mutators}|{sysctl_mut})\b")
    bad = []
    raw_lines = script.read_text().splitlines()
    for n, line in code_lines(script):
        raw = raw_lines[n - 1]          # quoted strings are blanked in `line`; $STAGE lives inside them
        for m in pat.finditer(line):
            before = line[: m.start("cmd")].rstrip()
            if before.endswith("run"):
                continue
            if "$STAGE" in raw or "run/lock" in raw:
                continue
            bad.append(f"{script.name}:{n}: {line.strip()}")
    assert not bad, "unguarded mutation(s):\n" + "\n".join(bad)


def test_install_enables_only_the_timers_and_the_two_daemons_and_never_starts_a_tier_service():
    t = INSTALL.read_text()
    timers = re.search(r"TIMERS=\(([^)]*)\)", t)
    assert timers and set(timers.group(1).split()) == ENABLED_TIMERS
    assert "WWW=homelab-maint-www.service" in t and "LIVE=homelab-maint-live.service" in t
    assert 'to_enable+=("$WWW")' in t and 'to_enable+=("$LIVE")' in t
    for n, line in code_lines(INSTALL):
        m = re.search(r"\b(?:ctl|systemctl)\s+(?:start|restart|enable|try-restart)\b(.*)", line)
        if not m:
            continue
        rest = m.group(1)
        assert not re.search(r"homelab-maint-(daily|weekly|tick|metrics)\.service", rest), f"line {n} starts a timer-driven service"
        if "homelab-maint-check.service" in rest:
            assert "--no-block" in rest, "the only allowed service start is the first check"
    # the literal timer-driven service names must never be passed to enable/start through variables either
    bare = re.sub(r"#.*", "", t)
    for svc in ("daily", "weekly", "tick", "metrics", "selfhealth"):
        assert f"homelab-maint-{svc}.service" not in bare, f"install.sh names the {svc} service"
    assert "docker-prune" not in re.sub(r"#.*", "", t.split("# Heads-up")[0]), "no legacy unit is enabled, started or listed by install.sh"
    # one umbrella: the routine is a job of the tick, there is no routine timer anywhere
    assert "routine.timer" not in bare and not list(SYSTEMD.glob("*routine*"))


def test_install_never_deletes_config_state_or_logs():
    raw_lines = INSTALL.read_text().splitlines()
    for n, line in code_lines(INSTALL):
        if re.search(r"\brm\b", line):
            assert re.search(r"\.homelab_maint|\$STAGE", raw_lines[n - 1]), f"install.sh:{n} removes something unexpected: {line.strip()}"
    assert "--apply" not in INSTALL.read_text(), "install.sh must not run the runner in apply mode"


def test_uninstall_purge_is_explicit_and_scoped():
    t = UNINSTALL.read_text()
    assert "--purge" in t and "--yes" in t and "--dry-run" in t and "--force" in t
    assert "$EUID" in t and "must run as root" in t
    purge_at = t.index("if ((PURGE)); then")
    for var in ("$CONF", "$STATE", "$LOGD"):
        uses = [m.start() for m in re.finditer(re.escape(f'rm_path "{var}"'), t)]
        assert len(uses) == 1 and uses[0] > purge_at, f"{var} may only be deleted inside the --purge block"
    assert "add --yes to confirm" in t


def test_scripts_do_not_leak_secrets_or_use_unsafe_constructs():
    for s in (INSTALL, UNINSTALL):
        t = s.read_text()
        assert not re.search(r"\beval\b|curl|wget|\|\s*(ba)?sh\b", t), f"{s.name}: eval/download-and-run"
        assert not re.search(r"(?i)token\s*=|password|secret\s*=", re.sub(r"#.*", "", t).replace("secrets and belong", ""))


# --------------------------------------------------------------------------- units
EXPECTED_UNITS = {
    "homelab-maint-check.service", "homelab-maint-check.timer",
    "homelab-maint-daily.service", "homelab-maint-daily.timer",
    "homelab-maint-weekly.service", "homelab-maint-weekly.timer",
    "homelab-maint-metrics.service", "homelab-maint-metrics.timer",     # the 1-minute sensor sampler
    "homelab-maint-tick.service", "homelab-maint-tick.timer",           # the 1-minute scheduler tick (routine, probes, jobs)
    "homelab-maint-selfhealth.service", "homelab-maint-selfhealth.timer",   # the 1-minute pipeline self-check (public/self.json)
    "homelab-maint-live.service",                                       # the 5 s live monitor
    "homelab-maint-www.service",
}


def test_unit_inventory():
    assert {p.name for p in SYSTEMD.iterdir() if p.is_file()} == EXPECTED_UNITS
    dropins = sorted(str(p.relative_to(SYSTEMD / "dropins")) for p in (SYSTEMD / "dropins").rglob("*") if p.is_file())
    assert dropins == [DROPIN_REL]


def test_tier_services():
    want = {
        # --apply only PERMITS mutation: the checks are C0 (forced read-only), the ports in this tier ship "report", and the one
        # thing that acts is the spike ladder's reclaim rung (see test_the_check_tier_may_apply_because_only_reclaim_ships_on).
        "check": "/usr/local/sbin/homelab-maint run --tier check --apply",
        "daily": "/usr/local/sbin/homelab-maint run --tier daily --apply",
        "weekly": "/usr/local/sbin/homelab-maint run --tier weekly --apply",
    }
    for tier, cmd in want.items():
        u = parse_unit(SYSTEMD / f"homelab-maint-{tier}.service")
        assert one(u, "Service", "Type") == "oneshot"
        assert one(u, "Service", "ExecStart") == cmd
        assert "Install" not in u, "tier services are started by their timers only"
        assert one(u, "Service", "Nice") == "10"
        assert one(u, "Service", "IOSchedulingClass") == "idle"
        assert one(u, "Service", "OOMScoreAdjust") == "500"
        # run as root, no sandbox that would stop the Hermes delivery (runuser to ohmz, reading ~ohmz/.hermes)
        assert "User" not in u["Service"] and "DynamicUser" not in u["Service"]
    assert "homelab-maint-daily.service" in one(parse_unit(SYSTEMD / "homelab-maint-weekly.service"), "Unit", "After")


def test_timers():
    chk = parse_unit(SYSTEMD / "homelab-maint-check.timer")
    assert chk["Timer"]["OnCalendar"] == ["*:0/15"]
    assert chk["Timer"]["RandomizedDelaySec"] == ["60"]
    day = parse_unit(SYSTEMD / "homelab-maint-daily.timer")
    assert day["Timer"]["OnCalendar"] == ["*-*-* 07:30:00"]
    assert day["Timer"]["RandomizedDelaySec"] == ["20min"]
    assert day["Timer"]["Persistent"] == ["true"]
    wk = parse_unit(SYSTEMD / "homelab-maint-weekly.timer")
    assert wk["Timer"]["OnCalendar"] == ["Wed *-*-* 07:45:00"]
    assert wk["Timer"]["Persistent"] == ["true"]
    met = parse_unit(SYSTEMD / "homelab-maint-metrics.timer")
    assert met["Timer"]["OnBootSec"] == ["1min"] and met["Timer"]["OnUnitActiveSec"] == ["1min"]
    tick = parse_unit(SYSTEMD / "homelab-maint-tick.timer")
    assert tick["Timer"]["OnCalendar"] == ["minutely"]
    assert tick["Timer"]["AccuracySec"] == ["1s"], "the default 1 min accuracy would coalesce ticks"
    assert "Persistent" not in tick["Timer"], "a missed tick is pointless; the scheduler catches jobs up itself"
    selfh = parse_unit(SYSTEMD / "homelab-maint-selfhealth.timer")
    assert selfh["Timer"]["OnBootSec"] == ["2min"] and selfh["Timer"]["OnUnitActiveSec"] == ["1min"]
    assert "Persistent" not in selfh["Timer"], "the page ages public/self.json by itself; a missed minute is not caught up"
    for t in (chk, day, wk, met, tick, selfh):
        assert t["Install"]["WantedBy"] == ["timers.target"]
    # each timer triggers the service of the same name (no Unit= override)
    for n in ("check", "daily", "weekly", "metrics", "tick", "selfhealth"):
        assert "Unit" not in parse_unit(SYSTEMD / f"homelab-maint-{n}.timer")["Timer"]
        assert (SYSTEMD / f"homelab-maint-{n}.service").is_file()


def test_www_service_is_hardened_and_loopback_only():
    u = parse_unit(SYSTEMD / "homelab-maint-www.service")
    s = u["Service"]
    for key, val in {
        "DynamicUser": "yes", "ProtectSystem": "strict", "ReadOnlyPaths": "/var/lib/homelab-maint",
        "PrivateTmp": "yes", "NoNewPrivileges": "yes", "IPAddressAllow": "localhost", "IPAddressDeny": "any",
        "ProtectHome": "yes", "CapabilityBoundingSet": "",
    }.items():
        assert s.get(key) == [val], f"{key}={val} expected, got {s.get(key)}"
    assert "User" not in s and "Group" not in s
    assert u["Install"]["WantedBy"] == ["multi-user.target"]
    assert "homelab_maint.server" in s["ExecStart"][0]
    assert any("kuma.toml" in v for v in s.get("InaccessiblePaths", [])), "push tokens must be hidden from the server"
    # no write access is granted to anything
    assert not {"ReadWritePaths", "StateDirectory", "LogsDirectory", "RuntimeDirectory"} & set(s)


def test_the_tick_is_one_unlimited_oneshot_that_leaves_its_jobs_running():
    u = parse_unit(SYSTEMD / "homelab-maint-tick.service")
    s = u["Service"]
    assert one(u, "Service", "Type") == "oneshot"
    assert one(u, "Service", "ExecStart") == "/usr/local/sbin/homelab-maint tick"
    assert "Install" not in u, "started by its timer only"
    # jobs (backups!) are detached children in this unit's cgroup: a cap or a sandbox here would cap and sandbox them too, and the
    # default KillMode=control-group would kill them the moment the tick exits
    assert s["KillMode"] == ["process"] and s["SendSIGKILL"] == ["no"]
    for key in ("MemoryMax", "TasksMax", "CPUQuota", "ProtectSystem", "ProtectHome", "PrivateTmp", "NoNewPrivileges", "PrivateUsers",
                "ReadOnlyPaths", "IPAddressDeny"):
        assert key not in s, f"{key} on the tick would also apply to every job it starts (and to the notification delivery)"
    assert "User" not in s and "DynamicUser" not in s


def test_services_that_send_notifications_do_not_block_runuser_or_the_hermes_config():
    """notify.py runs the Hermes transports as ohmz (runuser) and reads ~ohmz/.hermes: ProtectHome, NoNewPrivileges and PrivateUsers
    on the units that deliver would silently break every page."""
    for n in (*TIER_SERVICES, "tick"):
        s = parse_unit(SYSTEMD / f"homelab-maint-{n}.service")["Service"]
        for key in ("ProtectHome", "NoNewPrivileges", "PrivateUsers", "PrivateDevices", "DynamicUser", "User"):
            assert key not in s, f"homelab-maint-{n}.service sets {key}"


def test_metrics_sampler_is_a_small_sandboxed_oneshot_that_keeps_the_gpu():
    u = parse_unit(SYSTEMD / "homelab-maint-metrics.service")
    s = u["Service"]
    assert one(u, "Service", "Type") == "oneshot" and "Install" not in u
    assert one(u, "Service", "ExecStart") == "/usr/bin/python3 -B -m homelab_maint.metrics_ring sample"
    assert s["Environment"] == ["PYTHONPATH=/usr/local/lib/homelab-maint"]
    for key, val in {"User": "root", "ProtectSystem": "strict", "ReadWritePaths": "/var/lib/homelab-maint", "NoNewPrivileges": "yes",
                     "PrivateTmp": "yes", "IPAddressAllow": "localhost", "IPAddressDeny": "any", "TimeoutStartSec": "20",
                     "TimeoutStopSec": "5", "UMask": "0022", "Nice": "15", "IOSchedulingClass": "idle"}.items():
        assert s.get(key) == [val], f"{key}={val} expected, got {s.get(key)}"
    assert "PrivateDevices" not in s, "nvidia-smi needs /dev/nvidia*"
    # It stays the bare sampler: a publish (about 0.4 s of CPU, 40 MB, several systemctl calls) would run inside this 256 MB / 20 s sandbox
    # and a slow one could cost a sample. metrics.json is refreshed by each tier run instead (README, "Troubleshooting").
    assert "ExecStartPost" not in s and "metrics-sample" not in s["ExecStart"][0]


def test_the_selfhealth_refresher_is_its_own_sandboxed_oneshot_and_not_a_tick_job():
    """public/self.json is the website's dead-man for the runner: if the refresher ran from the tick, a dead tick would stop the very
    file that says so. It is a unit of its own, reads docker and the website over loopback only, and writes only the state dir."""
    u = parse_unit(SYSTEMD / "homelab-maint-selfhealth.service")
    s = u["Service"]
    assert one(u, "Service", "Type") == "oneshot" and "Install" not in u
    assert one(u, "Service", "ExecStart") == "/usr/bin/python3 -B -m homelab_maint.tasks.self_health --refresh"
    assert s["Environment"] == ["PYTHONPATH=/usr/local/lib/homelab-maint", "DOCKER_CONFIG=/nonexistent"]
    for key, val in {"User": "root", "ProtectSystem": "strict", "ReadWritePaths": "/var/lib/homelab-maint", "ProtectHome": "yes",
                     "NoNewPrivileges": "yes", "PrivateTmp": "yes", "IPAddressDeny": "any", "IPAddressAllow": "localhost",
                     "UMask": "0022", "TimeoutStartSec": "30"}.items():
        assert s.get(key) == [val], f"{key}={val} expected, got {s.get(key)}"
    # the docker gate reads /proc/<pid>/comm of dockerd, and docker.sock/systemd's bus are unix sockets
    assert not {"ProtectProc", "ProcSubset", "RestrictAddressFamilies", "PrivateDevices"} & set(s)
    jobs = toml_file("jobs.toml")["job"]
    assert not [j["name"] for j in jobs if "self_health" in " ".join(j["command"])], "the self-health refresher must not be a tick job"


def test_live_monitor_is_a_sandboxed_always_on_daemon_with_a_budget():
    u = parse_unit(SYSTEMD / "homelab-maint-live.service")
    s = u["Service"]
    for key, val in {"Type": "simple", "Restart": "always", "User": "root", "ProtectSystem": "strict", "ReadWritePaths": "/var/lib/homelab-maint",
                     "ProtectHome": "true", "NoNewPrivileges": "true", "PrivateTmp": "true", "MemoryMax": "64M", "CPUQuota": "10%",
                     "IPAddressAllow": "localhost", "IPAddressDeny": "any", "UMask": "0022", "KillSignal": "SIGTERM"}.items():
        assert s.get(key) == [val], f"{key}={val} expected, got {s.get(key)}"
    assert "PrivateDevices" not in s, "nvidia-smi needs /dev/nvidia*"
    assert s["ExecStart"] == ["/usr/bin/python3 -B -m homelab_maint.live"]
    assert u["Install"]["WantedBy"] == ["multi-user.target"]
    # ordering only: starting the monitor must never start or restart what it watches (dockerd is socket-activated)
    assert "Wants" not in u["Unit"] and "Requires" not in u["Unit"]


def test_every_python_module_a_unit_runs_exists_and_can_be_run():
    for p in SYSTEMD.glob("*.service"):
        for line in p.read_text().splitlines():
            m = re.match(r"ExecStart=\S*python3? (?:-B )?-m (homelab_maint\.([\w.]+))", line)
            if m:
                src = (REPO / "homelab_maint" / f"{m.group(2).replace('.', '/')}.py").read_text()      # homelab_maint.tasks.self_health too
                assert "def main" in src, f"{p.name}: {m.group(1)} has no main()"
                assert "__main__" in src, f"{p.name}: {m.group(1)} cannot be run with -m"
    for p in SYSTEMD.glob("*.service"):          # the library path every unit uses is the one install.sh installs to
        for line in p.read_text().splitlines():
            if line.startswith("Environment=PYTHONPATH="):
                assert line == "Environment=PYTHONPATH=/usr/local/lib/homelab-maint", p.name


def test_immich_dropin_exact():
    d = parse_unit(SYSTEMD / "dropins" / DROPIN_REL)
    assert d == {"Service": {"ExecCondition": ["/usr/local/sbin/homelab-maint gate immich-recycle"]}}


def test_gate_name_exists_in_shipped_config():
    prot = (REPO / "etc" / "protected.toml").read_text()
    assert re.search(r'"immich-recycle"\s*=', prot), "protected.toml must define max_defer_hours for immich-recycle"


def test_unit_commands_use_real_cli_subcommands():
    subs = cli_subcommands()
    assert {"run", "gate", "doctor", "pause", "resume", "approve", "plan", "status", "tick", "schedule", "metrics-sample",
            "publish", "routine", "migrate", "probes", "job", "live", "notify", "smart-event"} <= subs
    for p in list(SYSTEMD.glob("*.service")) + [SYSTEMD / "dropins" / DROPIN_REL]:
        for line in p.read_text().splitlines():
            m = re.match(r"(?:ExecStart|ExecCondition)=/usr/local/sbin/homelab-maint (\w+)", line)
            if m:
                assert m.group(1) in subs, f"{p.name}: unknown subcommand {m.group(1)}"


@pytest.mark.skipif(not _have("systemd-analyze"), reason="systemd-analyze missing")
def test_systemd_analyze_verify_units(tmp_path):
    """verify on copies; the installed binary is replaced by a stub so only the unit syntax is judged."""
    stub = tmp_path / "homelab-maint"
    stub.write_text("#!/bin/sh\n")
    stub.chmod(0o755)
    work = tmp_path / "units"
    work.mkdir()
    for p in SYSTEMD.iterdir():
        if p.is_file():
            (work / p.name).write_text(p.read_text().replace("/usr/local/sbin/homelab-maint", str(stub)))
    r = subprocess.run(["systemd-analyze", "verify", *sorted(str(p) for p in work.iterdir())],
                       capture_output=True, text=True, cwd=tmp_path)
    ours = [ln for ln in (r.stdout + r.stderr).splitlines() if str(tmp_path) in ln or "homelab-maint" in ln]
    assert not ours, "systemd-analyze verify complains about our units:\n" + "\n".join(ours)


@pytest.mark.skipif(not _have("systemd-analyze"), reason="systemd-analyze missing")
def test_systemd_analyze_verify_dropin_on_copy_of_original(tmp_path):
    stub = tmp_path / "homelab-maint"
    stub.write_text("#!/bin/sh\n")
    stub.chmod(0o755)
    (tmp_path / "immich-server-recycle.service").write_text(ORIGINAL_IMMICH_UNIT)
    dd = tmp_path / "immich-server-recycle.service.d"
    dd.mkdir()
    src = (SYSTEMD / "dropins" / DROPIN_REL).read_text()
    (dd / "10-homelab-gate.conf").write_text(src.replace("/usr/local/sbin/homelab-maint", str(stub)))
    r = subprocess.run(["systemd-analyze", "verify", str(tmp_path / "immich-server-recycle.service")],
                       capture_output=True, text=True, cwd=tmp_path)
    ours = [ln for ln in (r.stdout + r.stderr).splitlines() if str(tmp_path) in ln or "immich-server-recycle" in ln]
    assert not ours, "\n".join(ours)


@pytest.mark.skipif(not _have("systemd-analyze"), reason="systemd-analyze missing")
def test_timer_calendars_parse():
    for p in SYSTEMD.glob("*.timer"):
        for expr in parse_unit(p)["Timer"].get("OnCalendar", []):
            r = subprocess.run(["systemd-analyze", "calendar", expr], capture_output=True, text=True)
            assert r.returncode == 0, f"{p.name}: {expr!r}: {r.stderr}"


# --------------------------------------------------------------------------- shipped config: consistency
@functools.lru_cache(maxsize=None)
def registry() -> dict[str, tuple[str, str, str]]:
    """name -> (klass, tier, module) of every task the runner registers, from a fresh process (this one's registry is shared with other tests)."""
    tmp = Path(os.environ["HOMELAB_MAINT_RUN"]) / "registry-probe"
    tmp.mkdir(exist_ok=True)
    env = {**os.environ, **{f"HOMELAB_MAINT_{k}": str(tmp / k.lower()) for k in ("STATE", "LOG", "RUN", "CONF")}, "PYTHONDONTWRITEBYTECODE": "1"}
    code = ("import json, sys; sys.path.insert(0, sys.argv[1])\n"
            "from homelab_maint import cli, core\ncli.load_tasks()\n"
            "print(json.dumps({n: [t.klass, t.tier, t.run.__module__] for n, t in core.REGISTRY.items()}))")
    r = subprocess.run([sys.executable, "-B", "-c", code, str(REPO)], capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    return {n: tuple(v) for n, v in json.loads(r.stdout.splitlines()[-1]).items()}


def toml_file(name: str) -> dict:
    return tomllib.loads((ETC / name).read_text())


# The task modules packaging has wired: each has its [tasks.X] tables, routine steps and playbooks. A module another stream adds later
# goes in this set once it is wired. Not wiring a C1/C2 task is safe by construction: no table means mode "report", and a task no routine
# names is held to report-only by the guard. (rules_registry, registered by registry.register_tasks(), has its playbook and needs no table:
# an extra table in etc/ reshuffles the random variants test_registry builds from the shipped files.)
WIRED_MODULES = {"homelab_maint.reports", "homelab_maint.routine"} | {
    f"homelab_maint.tasks.{m}" for m in ("checks_basic", "checks_health", "cleaners", "cleaners_apps", "cleaners_dev", "cleaners_pkgs", "guard",
                                          "legacy_audit", "monitors", "native", "pressure", "scans", "self_health", "swap", "swap_auto")}


def wired(klass_in: tuple[str, ...] = ("C0", "C1", "C2")) -> set[str]:
    return {n for n, (klass, _tier, mod) in registry().items() if mod in WIRED_MODULES and klass in klass_in}


def test_every_shipped_toml_parses():
    files = sorted(ETC.glob("*.toml")) + sorted((REPO / "homelab_maint" / "data").glob("*.toml"))
    assert {"maint.toml", "routine.toml", "jobs.toml", "probes.toml", "classes.toml", "playbooks.toml", "notify.toml",
            "legacy-retirement.toml", "protected.toml"} <= {f.name for f in files}
    for f in files:
        tomllib.loads(f.read_text())


def test_maint_toml_names_only_real_tasks_and_covers_every_wired_one():
    reg = registry()
    tables = set(toml_file("maint.toml")["tasks"])
    assert not tables - set(reg), f"[tasks.X] for a task that is not registered (typo?): {sorted(tables - set(reg))}"
    missing = wired() - tables
    assert not missing, f"add [tasks.X] tables to etc/maint.toml for: {sorted(missing)}"
    assert len(wired()) >= 40, "the wired modules must still register their tasks"


def test_only_the_spike_ladders_reclaim_rung_ships_on():
    """The check, daily and weekly services pass --apply, so this is what keeps 'report-only by default' true."""
    reg, tasks = registry(), toml_file("maint.toml")["tasks"]
    on = sorted(n for n, t in tasks.items() if t.get("mode") == "apply")
    assert on == ["pressure_response"], f"these ship in apply mode: {on}; the owner enables apply, the shipped file does not"
    rungs = {k: tasks["pressure_response"].get(k) for k in ("reclaim", "throttle", "restart", "emergency")}
    assert rungs == {"reclaim": "apply", "throttle": "report", "restart": "report", "emergency": "report"}
    for n, (klass, _tier, _mod) in reg.items():
        if klass in ("C1", "C2") and n in tasks and n != "pressure_response":
            assert tasks[n].get("mode", "report") == "report", f"{n} must ship report-only"
    ladder = toml_file("classes.toml")
    assert ladder.get("emergency_stop", []) == [], "no container is on the emergency-stop list by default"


def test_the_check_tier_may_apply_because_only_reclaim_ships_on():
    """check.service passes --apply (the spike glue): every C1 task of that tier but pressure_response must be report-only."""
    tasks = toml_file("maint.toml")["tasks"]
    assert "run --tier check --apply" in (SYSTEMD / "homelab-maint-check.service").read_text()
    c1 = sorted(n for n, (klass, tier, _mod) in registry().items() if tier == "check" and klass != "C0")
    assert {"pressure_response", "comfyui_idle_reclaim", "immich_recycle"} <= set(c1)
    for n in c1:
        if n != "pressure_response":      # an unlisted task means mode "report", which is also fine
            assert tasks.get(n, {}).get("mode", "report") == "report", f"{n} runs under --apply every 15 min and must ship report"
    for n in ("comfyui_idle_reclaim", "immich_recycle"):
        assert tasks[n]["mode"] == "report", "the ports are explicit about it"


def test_routine_steps_and_post_checks_name_registered_tasks():
    reg = registry()
    code = ("import json, sys; sys.path.insert(0, sys.argv[1])\n"
            "from homelab_maint import routine\nrc = routine.load_config(sys.argv[2])\n"
            "print(json.dumps({'valid': rc.valid, 'errors': rc.errors, 'steps': [s.task for e in rc.entries for s in e.steps],"
            " 'post': [n for e in rc.entries for n in e.post_check]}))")
    tmp = Path(os.environ["HOMELAB_MAINT_RUN"]) / "routine-probe"
    tmp.mkdir(exist_ok=True)
    env = {**os.environ, **{f"HOMELAB_MAINT_{k}": str(tmp / k.lower()) for k in ("STATE", "LOG", "RUN", "CONF")}, "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-B", "-c", code, str(REPO), str(ETC / "routine.toml")], capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    d = json.loads(r.stdout.splitlines()[-1])
    assert d["valid"] and not d["errors"], d["errors"]
    assert [s for s in d["steps"] if s not in reg] == []
    assert [n for n in d["post"] if n not in reg or reg[n][0] != "C0"] == [], "a post-check is always a registered read-only task"
    assert "legacy_audit" in d["steps"], "the legacy audit is a weekly routine step"
    unnamed = sorted(n for n in wired(("C1", "C2")) if reg[n][1] != "check" and n not in d["steps"])
    assert not unnamed, f"add these cleaners to a routine in etc/routine.toml (else the guard holds them to report-only): {unnamed}"


PLAYBOOKS_FILE = REPO / "homelab_maint" / "data" / "playbooks.toml"
CLEANERS_V2 = ("app_cache_trim", "apt_autoremove_unused", "apt_cache", "crash_dumps", "dangling_images", "flatpak_unused", "large_cold_files",
               "log_compress", "stale_build_output", "stale_driver_packages", "tool_caches", "unused_venvs")


def routine_doc() -> dict:
    return toml_file("routine.toml")


def step_names(entry: dict) -> list[str]:
    return [s if isinstance(s, str) else s["task"] for s in entry["steps"]]


def test_every_wired_task_has_a_playbook_a_table_and_every_cleaner_a_routine_step():
    """The three lists that must never drift apart: registered tasks, [tasks.X] tables, playbooks, routine steps."""
    reg, tables = registry(), toml_file("maint.toml")["tasks"]
    playbooks = set(tomllib.loads(PLAYBOOKS_FILE.read_text())["playbook"]) - {"_default"}
    # the routine's 13 built-in steps (routine_*) are explained by their parent routine, not by a playbook of their own
    mine = {n for n in wired() if not n.startswith("routine_")}
    assert not mine - playbooks, f"add [playbook.X] to homelab_maint/data/playbooks.toml for: {sorted(mine - playbooks)}"
    assert not mine - set(tables), f"add [tasks.X] tables to etc/maint.toml for: {sorted(mine - set(tables))}"
    assert not set(tables) - set(reg), f"[tasks.X] for a task nobody registers: {sorted(set(tables) - set(reg))}"
    steps = {n for e in routine_doc()["routine"] for n in step_names(e)}
    assert not {n for n, (klass, tier, _m) in reg.items() if klass in ("C1", "C2") and tier != "check" and n not in steps
                and n not in ("routine_rotate",)}, "a registered cleaner or plan that no routine names is held to report-only by the guard"
    assert not set(CLEANERS_V2) - set(reg), "the cleaners-v2 modules must still register their tasks"
    assert set(CLEANERS_V2) <= steps and set(CLEANERS_V2) <= set(tables) and set(CLEANERS_V2) <= playbooks


def test_cleaners_v2_ship_report_only_in_the_routine_order_the_builders_asked_for():
    tables, doc = toml_file("maint.toml")["tasks"], routine_doc()
    for n in CLEANERS_V2:
        assert tables[n].get("mode", "report") == "report", f"{n} must ship report-only (the owner enables apply per task)"
    daily = step_names(next(e for e in doc["routine"] if e["name"] == "daily"))
    weekly = step_names(next(e for e in doc["routine"] if e["name"] == "weekly"))
    at = daily.index
    assert at("dangling_images") < at("docker_images"), "dangling first: tagged unused images are docker_images' job"
    assert at("apt_clean") < at("stale_driver_packages") < at("apt_autoremove_unused") < at("snap_revisions")
    assert at("retention") < at("app_cache_trim") < at("log_compress") < at("crash_dumps") < at("trash")
    assert at("gradle_reaper") < at("tool_caches") < at("openwebui_media_prune")
    assert at("verify") > at("tool_caches") and at("report") == len(daily) - 1
    assert weekly.index("c2_candidates") < min(weekly.index(n) for n in ("stale_build_output", "unused_venvs", "large_cold_files", "flatpak_unused"))
    heavy = {s["task"] for e in doc["routine"] for s in e["steps"] if isinstance(s, dict) and s.get("disruptive")}
    assert {"app_cache_trim", "log_compress", "dangling_images", "stale_build_output", "unused_venvs", "large_cold_files"} <= heavy
    # `apt_cache` and `apt_clean` are the same command: both are named (the guard needs a routine step), only one is switched on
    assert "apt_cache" in daily and "apt_clean" in daily
    assert tables["apt_cache"].get("enabled") is False and tables["apt_clean"].get("enabled", True) is True, "never both"


def test_retention_no_longer_owns_what_the_new_cleaners_own():
    tables = toml_file("maint.toml")["tasks"]
    ret, trim = tables["retention"], tables["app_cache_trim"]
    assert [r["name"] for r in ret["rules"]] == ["kavita-logs", "kavita-backups"], "tunarr-subtitles is app_cache_trim's, crash-dumps is crash_dumps'"
    assert ret["allowed_roots"] == ["/volume1/docker/kavita/config", "/var/log"] and "unprotect" not in ret
    cache = "/home/ohmz/StudioProjects/tunarr/.docker-data/tunarr/cache"
    rule = next(r for r in trim["rules"] if r["name"] == "tunarr-subtitles")
    assert rule["path"] == cache + "/subtitles" and rule["container"] == "tunarr-host-net" and rule["max_age_days"] == 30
    assert cache in trim["allowed_roots"] and rule["path"].startswith(cache + "/"), "a rule path must be strictly below an allowed root"
    assert all(re.search(u, rule["path"]) for u in trim["unprotect"]), "the narrow exemption must cover the rule's own path"
    assert not re.search(trim["unprotect"][0], "/home/ohmz/StudioProjects/tunarr/.docker-data/tunarr/config"), "and nothing else of the app dir"
    assert {"path": rule["path"], "warn_gib_per_day": 0.5} in tables["growth_watch"]["paths"], "the cache is watched for growth"


def test_every_job_runs_a_real_command_and_installing_takes_nothing_over():
    jobs = toml_file("jobs.toml")["job"]
    subs, reg = cli_subcommands(), registry()
    names = [j["name"] for j in jobs]
    assert len(names) == len(set(names))
    for j in jobs:
        cmd = j["command"]
        assert isinstance(cmd, list) and cmd and all(isinstance(a, str) for a in cmd), j["name"]
        assert j.get("schedule"), f"{j['name']} has no schedule"
        if cmd[0] == "{self}":
            assert cmd[1] in subs, f"{j['name']}: unknown subcommand {cmd[1]}"
            if cmd[1] == "run":
                assert cmd[cmd.index("--tier") + 1] in ("check", "daily", "weekly", "monthly")
                if "--task" in cmd:
                    assert cmd[cmd.index("--task") + 1] in reg
        elif "-m" in cmd:
            mod = cmd[cmd.index("-m") + 1]
            assert mod.startswith("homelab_maint.") and (REPO / "homelab_maint" / f"{mod.split('.')[1]}.py").is_file(), f"{j['name']}: {mod}"
            assert j["env"]["PYTHONPATH"] == "/usr/local/lib/homelab-maint", j["name"]
        else:
            assert cmd[0].startswith("/"), f"{j['name']}: the program must be an absolute path"
    # Shipped state: only the umbrella's own engines run from the first tick, plus container-audit (added 2026-10-04: a
    # native job with no legacy driver); every legacy-driven job is observe (or retired).
    managed = sorted(j["name"] for j in jobs if j.get("mode") == "managed")
    assert managed == ["container-audit", "probes-run", "routine-run"], managed
    assert all(j.get("mode") in ("managed", "observe", "retired") for j in jobs)
    byname = {j["name"]: j for j in jobs}
    assert byname["routine-run"]["command"][-3:] == ["homelab_maint.routine", "run", "--apply"], "the routine is a job of the tick"
    # the acknowledge inbox and the registry sync are driven by the tick itself (cli._ack_tick, cli._rules_sync): a job would run them twice
    assert not [j["name"] for j in jobs if re.search(r"homelab_maint\.(acks|registry)\b|\b(ack|rules)\b", " ".join(j["command"]))], \
        "no job may also run what the tick runs inline"
    for name in ("tier-check", "tier-daily", "tier-weekly"):
        assert byname[name]["mode"] == "observe", "the tier timers stay the drivers for now"
    assert byname["tier-check"]["command"] == ["{self}", "run", "--tier", "check", "--apply"], "same as check.service"
    # the units the jobs file mentions as external really are what this repo ships
    for e in toml_file("jobs.toml").get("external", []):
        if str(e.get("unit", "")).startswith("homelab-maint-"):
            assert e["unit"] in EXPECTED_UNITS, e


def _units_probed(obj) -> list[str]:
    found = []
    if isinstance(obj, dict):
        if obj.get("type") == "systemd" and isinstance(obj.get("target"), str):
            found.append(obj["target"])
        for v in obj.values():
            found += _units_probed(v)
    elif isinstance(obj, list):
        for v in obj:
            found += _units_probed(v)
    return found


def test_probes_watch_the_units_this_repo_ships():
    ours = [u for u in _units_probed(toml_file("probes.toml")) if u.startswith("homelab-maint-")]
    assert set(ours) >= {"homelab-maint-www.service", "homelab-maint-live.service", "homelab-maint-tick.timer",
                         "homelab-maint-metrics.timer", "homelab-maint-check.timer", "homelab-maint-daily.timer",
                         "homelab-maint-weekly.timer", "homelab-maint-selfhealth.timer"}
    assert set(ours) <= EXPECTED_UNITS, sorted(set(ours) - EXPECTED_UNITS)


def test_shipped_playbooks_file_is_an_overrides_only_template():
    """The baseline lives in the package (replaced on every install); a full copy in /etc would shadow its later fixes."""
    cfg = toml_file("playbooks.toml")
    assert not cfg.get("playbook") and not cfg.get("slo"), "etc/playbooks.toml must hold no playbooks or objectives of its own"
    assert (REPO / "homelab_maint" / "data" / "playbooks.toml").is_file()


# --------------------------------------------------------------------------- README
def test_readme_covers_required_topics():
    t = README.read_text()
    for needle in ("## One umbrella", "## Layout", "## Install", "## What runs", "## Commands", "## Turning cleanup on", "## Kill switch",
                   "## Homarr wiring", "## The maintenance website", "## Adding a task", "## Legacy migration",
                   "/etc/homelab-maint/PAUSE", "127.0.0.1:9111", "mode = \"apply\"",
                   "install.sh", "uninstall.sh", "10-homelab-gate.conf", "homelab-maint-www.service",
                   "no separate routine timer", "docs/MIGRATION.md", "docs/EXTENDING.md", "--deploy-web", "the owner enables apply",
                   # the acknowledge flow, the registry, the two-sides model, the first-run login, cleaners per task, docker-prune
                   "## Two sides: the registry and the website", "## Acknowledging a known issue", "## The rules registry",
                   "## First-run web login", "### Enabling the newer cleaners, one task at a time", "### Re-enabling docker-prune safely",
                   "ack/inbox", "ack/web.key", "ack/bootstrap.secret", "ack/web_ready", "1730", "10001", "--adopt-rules",
                   "sudo homelab-maint rules sync --adopt", "rules/orig", "homelab-maint-selfhealth.timer",
                   "docker_prune_exposure", "docker_prune_parity", "docker-prune.timer", "00-baseline-invariants.toml", "read-only mirror"):
        assert needle in t, f"README lacks {needle!r}"


# Subcommands whose module exists and is documented here, while their one-line `PASS` entry in cli.py is the code glue's job: the README
# may name them whether or not cli.py has them yet (test_readme_documents_every_command still demands every command cli.py DOES have).
CLI_GLUE_PENDING = {"ack", "rules", "self-health"}


def test_readme_commands_and_units_exist():
    t = README.read_text()
    subs = cli_subcommands() | CLI_GLUE_PENDING
    for m in re.finditer(r"^(?:sudo )?homelab-maint ([\w-]+)", t, re.M):
        assert m.group(1) in subs, f"README documents a command cli.py does not have: {m.group(0)}"
    for module in ("acks.py", "registry.py", "tasks/self_health.py"):         # ... but the module behind each must really be there
        assert (REPO / "homelab_maint" / module).is_file(), module
    for m in re.finditer(r"homelab-maint-(?:check|daily|weekly|metrics|tick|selfhealth|live|www)\.(?:service|timer)", t):
        assert m.group(0) in EXPECTED_UNITS, m.group(0)
    for m in re.finditer(r"widgets/(ops-[\w-]+)\.json", t):
        assert (REPO / "widgets" / f"{m.group(1)}.json").is_file(), m.group(0)


def test_readme_documents_every_command():
    t = README.read_text()
    for sub in sorted(cli_subcommands()):
        assert re.search(rf"^homelab-maint {re.escape(sub)}\b", t, re.M), f"README's command list lacks `homelab-maint {sub}`"


def test_readme_lists_every_shipped_unit_and_config_file():
    t = README.read_text()
    for unit in sorted(EXPECTED_UNITS - {"homelab-maint-metrics.service", "homelab-maint-tick.service", "homelab-maint-check.service",
                                         "homelab-maint-daily.service", "homelab-maint-weekly.service"}):
        assert unit in t, f"README does not mention {unit}"
    for f in ("maint.toml", "protected.toml", "classes.toml", "routine.toml", "jobs.toml", "probes.toml", "notify.toml", "ack.toml", "playbooks.toml",
              "legacy-retirement.toml", "rules.d"):
        assert f in t, f"README does not mention etc/{f}"


# --------------------------------------------------------------------------- runtime: refusal and dry run
@not_root
def test_install_refuses_without_root_and_writes_nothing(env):
    r = env._run("install.sh", userns=False)
    assert r.returncode != 0
    assert "must run as root" in r.stderr
    assert list(env.root.rglob("*")) == []


@not_root
def test_uninstall_refuses_without_root(env):
    r = env._run("uninstall.sh", userns=False)
    assert r.returncode != 0 and "must run as root" in r.stderr


def test_unknown_option_is_rejected(env):
    r = env._run("install.sh", "--frobnicate", userns=False)
    assert r.returncode == 2 and "unknown option" in r.stderr
    r = env._run("uninstall.sh", "--frobnicate", userns=False)
    assert r.returncode == 2


def test_install_dry_run_needs_no_root_and_writes_nothing(env):
    env.with_original_unit()
    before = tree_state(env.root)
    r = env._run("install.sh", "--dry-run", userns=False)
    assert r.returncode == 0, r.stderr
    assert "DRY RUN" in r.stdout and "would-install" in r.stdout
    assert "Dry run:" in r.stdout and "nothing was written" in r.stdout
    assert tree_state(env.root) == before
    assert not (env.root / "usr").exists()


def test_uninstall_dry_run_writes_nothing(env):
    r = env._run("uninstall.sh", "--dry-run", userns=False)
    assert r.returncode == 0, r.stderr
    assert "DRY RUN" in r.stdout


def test_uninstall_purge_requires_yes(env):
    r = env._run("uninstall.sh", "--purge", userns=False)
    assert r.returncode != 0 and "--yes" in r.stderr


def test_install_help():
    r = subprocess.run(["bash", str(INSTALL), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--dry-run" in r.stdout and "never" in r.stdout.lower()


# --------------------------------------------------------------------------- runtime: staged install
@needs_userns
def test_staged_install_lays_everything_out(env):
    env.with_original_unit()
    r = env.install()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Done:" in r.stdout
    assert (env.p("usr/local/lib/homelab-maint/homelab_maint/cli.py")).is_file()
    assert (env.p("usr/local/lib/homelab-maint/homelab_maint/tasks/one.py")).is_file()
    entry = env.p("usr/local/sbin/homelab-maint")
    assert entry.is_file() and entry.stat().st_mode & 0o777 == 0o755
    for rel in ("etc/homelab-maint", "var/lib/homelab-maint", "var/log/homelab-maint"):
        assert env.p(rel).is_dir() and env.p(rel).stat().st_mode & 0o777 == 0o755, rel
    for name in ("maint.toml", "protected.toml"):
        f = env.p(f"etc/homelab-maint/{name}")
        assert f.read_text() == (env.proj / "etc" / name).read_text()
        assert f.stat().st_mode & 0o777 == 0o644
    for name in EXPECTED_UNITS:
        assert env.p(f"etc/systemd/system/{name}").read_text() == (SYSTEMD / name).read_text()
        assert env.p(f"etc/systemd/system/{name}").stat().st_mode & 0o777 == 0o644
    dropin = env.p(f"etc/systemd/system/{DROPIN_REL}")
    assert dropin.read_text() == (SYSTEMD / "dropins" / DROPIN_REL).read_text()
    # the original unit is not edited
    assert env.p("etc/systemd/system/immich-server-recycle.service").read_text() == ORIGINAL_IMMICH_UNIT
    # the precompiled bytecode matches the installed source (else python would silently recompile as root)
    src = env.p("usr/local/lib/homelab-maint/homelab_maint/cli.py")
    pyc = Path(importlib.util.cache_from_source(str(src)))
    assert pyc.is_file(), "bytecode was not precompiled"
    head = pyc.read_bytes()[:16]
    assert int.from_bytes(head[8:12], "little") == int(src.stat().st_mtime) & 0xFFFFFFFF
    assert int.from_bytes(head[12:16], "little") == src.stat().st_size
    # nothing leaked into the temp dir
    assert not list((env.tmp / "tmpdir").iterdir())
    # the cleaners that can mutate are announced
    assert "cleaner_b" in r.stdout and "cleaner_a" not in r.stdout.split("mode = \"apply\"")[1]


@needs_userns
def test_staged_install_is_idempotent(env):
    env.with_original_unit()
    first = env.install()
    assert first.returncode == 0, first.stdout + first.stderr
    before = tree_state(env.root)
    second = env.install()
    assert second.returncode == 0, second.stdout + second.stderr
    assert "Nothing to do: already up to date." in second.stdout
    assert not re.search(r"^\s+(install|mkdir|enable|start|restart|remove|reloaded)\b", second.stdout, re.M)
    assert tree_state(env.root) == before, "second run modified files"
    assert not list((env.tmp / "tmpdir").iterdir())


@needs_userns
def test_staged_install_creates_the_public_export_dirs_and_the_extension_points(env):
    r = env.install()
    assert r.returncode == 0, r.stdout + r.stderr
    for rel in ("var/lib/homelab-maint/public", "var/lib/homelab-maint/public/reports",
                "etc/homelab-maint/plugins.d", "etc/homelab-maint/probes.d", "etc/homelab-maint/legacy-retirement.d"):
        assert env.p(rel).is_dir() and env.p(rel).stat().st_mode & 0o777 == 0o755, rel      # root-owned 0755: the loaders refuse anything looser
    # the public dir is only ever created or chmod-ed (a bind mount follows its inode): a file in it survives an upgrade
    keep = env.p("var/lib/homelab-maint/public/overview.json")
    keep.write_text("{}")
    inode = env.p("var/lib/homelab-maint/public").stat().st_ino
    (env.proj / "homelab_maint" / "core.py").write_text("VALUE = 9\n")
    assert env.install().returncode == 0
    assert keep.read_text() == "{}" and env.p("var/lib/homelab-maint/public").stat().st_ino == inode


@needs_userns
def test_release_data_is_replaced_but_owner_config_is_kept(env):
    (env.proj / "etc" / "legacy-retirement.toml").write_text('[[item]]\nname = "v1"\n')
    assert env.install().returncode == 0
    inv = env.p("etc/homelab-maint/legacy-retirement.toml")
    assert inv.read_text() == '[[item]]\nname = "v1"\n' and inv.stat().st_mode & 0o777 == 0o644
    inv.write_text("# stale copy\n")                        # a stale copy would keep old parity checks
    (env.proj / "etc" / "legacy-retirement.toml").write_text('[[item]]\nname = "v2"\n')
    r = env.install()
    assert r.returncode == 0, r.stderr
    assert inv.read_text() == '[[item]]\nname = "v2"\n'
    assert not env.p("etc/homelab-maint/legacy-retirement.toml.dist").exists()
    assert env.p("etc/homelab-maint/legacy-retirement.d").is_dir(), "owner items live next to it"


@needs_userns
def test_existing_config_is_never_overwritten(env):
    assert env.install().returncode == 0
    mine = env.p("etc/homelab-maint/maint.toml")
    mine.write_text('# my edits\n[global]\nwww_port = 1234\n[tasks.cleaner_a]\nmode = "apply"\n')
    os.chmod(mine, 0o640)
    old_prot = env.p("etc/homelab-maint/protected.toml").read_text()
    # the shipped default changes in the next release
    (env.proj / "etc" / "maint.toml").write_text('[global]\nwww_port = 9111\n[tasks.new_task]\nmode = "report"\n')
    r = env.install()
    assert r.returncode == 0, r.stderr
    assert mine.read_text().startswith("# my edits"), "config was overwritten"
    assert mine.stat().st_mode & 0o777 == 0o640, "config mode was changed"
    assert env.p("etc/homelab-maint/protected.toml").read_text() == old_prot
    dist = env.p("etc/homelab-maint/maint.toml.dist")
    assert dist.read_text() == (env.proj / "etc" / "maint.toml").read_text()
    assert "kept" in r.stdout and "maint.toml.dist" in r.stdout
    assert "tables your copy lacks" in r.stdout and "[tasks.new_task]" in r.stdout, "the merge notice names the missing tables"
    # and the announced apply set is the one in the user's file, not the shipped one
    assert "cleaner_a" in r.stdout
    again = env.install()
    assert "Nothing to do" in again.stdout and mine.read_text().startswith("# my edits")


@needs_userns
def test_unparsable_existing_config_warns_but_install_completes(env):
    assert env.install().returncode == 0
    env.p("etc/homelab-maint/maint.toml").write_text("this is [not toml\n")
    r = env.install()
    assert r.returncode == 0
    assert "does not parse" in r.stderr
    assert env.p("etc/homelab-maint/maint.toml").read_text() == "this is [not toml\n"


@needs_userns
def test_upgrade_replaces_package_and_drops_removed_modules(env):
    assert env.install().returncode == 0
    lib = env.p("usr/local/lib/homelab-maint/homelab_maint")
    (env.proj / "homelab_maint" / "tasks" / "one.py").unlink()
    (env.proj / "homelab_maint" / "tasks" / "two.py").write_text("VALUE = 2\n")
    (env.proj / "homelab_maint" / "core.py").write_text("VALUE = 3\n")
    r = env.install()
    assert r.returncode == 0, r.stderr
    assert (lib / "tasks" / "two.py").read_text() == "VALUE = 2\n"
    assert (lib / "core.py").read_text() == "VALUE = 3\n"
    assert not (lib / "tasks" / "one.py").exists(), "stale module left behind"
    assert not (lib.parent / ".homelab_maint.new").exists() and not (lib.parent / ".homelab_maint.old").exists()


@needs_userns
def test_syntax_error_aborts_before_touching_the_live_install(env):
    assert env.install().returncode == 0
    lib = env.p("usr/local/lib/homelab-maint/homelab_maint")
    good = tree_state(lib.parent)
    (env.proj / "homelab_maint" / "core.py").write_text("def broken(:\n")
    r = env.install()
    assert r.returncode != 0 and "does not compile" in r.stderr
    assert tree_state(lib.parent) == good, "live package changed despite the compile failure"


@needs_userns
def test_invalid_shipped_config_aborts_before_writing(env):
    (env.proj / "etc" / "maint.toml").write_text("[broken\n")
    r = env.install()
    assert r.returncode != 0 and "does not parse" in r.stderr
    assert list(env.root.rglob("*")) == [] or all(p.parts[0] == "run" for p in
                                                  (q.relative_to(env.root) for q in env.root.rglob("*")))


@needs_userns
def test_incomplete_source_tree_is_refused(env):
    (env.proj / "homelab_maint" / "cli.py").unlink()
    r = env.install()
    assert r.returncode != 0 and "incomplete source tree" in r.stderr


@needs_userns
def test_dropin_skipped_when_original_unit_is_missing(env):
    r = env.install()
    assert r.returncode == 0
    assert not env.p("etc/systemd/system/immich-server-recycle.service.d").exists()
    assert "skipped" in r.stdout and "immich-server-recycle.service" in r.stdout


@needs_userns
def test_shipped_kuma_secrets_file_is_never_installed(env):
    (env.proj / "etc" / "kuma.toml").write_text('[push]\nx = "sekret"\n')
    r = env.install()
    assert r.returncode == 0
    assert not env.p("etc/homelab-maint/kuma.toml").exists()
    assert "kuma.toml" in r.stderr


@needs_userns
def test_installed_dropin_is_refreshed_when_changed(env):
    env.with_original_unit()
    assert env.install().returncode == 0
    dropin = env.p(f"etc/systemd/system/{DROPIN_REL}")
    dropin.write_text("[Service]\nExecCondition=/bin/true\n")
    assert env.install().returncode == 0
    assert dropin.read_text() == (SYSTEMD / "dropins" / DROPIN_REL).read_text()


@needs_userns
def test_real_source_tree_installs_cleanly(tmp_path):
    """The actual package, config and units as they are in the repo right now."""
    broken = subprocess.run([sys.executable, "-c",
                             "import compileall,sys; sys.exit(0 if compileall.compile_dir(sys.argv[1], quiet=1, force=True, legacy=False, workers=1, maxlevels=10) else 1)",
                             str(REPO / "homelab_maint")], capture_output=True, text=True,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if broken.returncode != 0:
        pytest.skip("source tree currently has syntax errors (owned by other modules): " + broken.stdout[-200:])
    env = Env(tmp_path, real_tree=True)
    env.with_original_unit()
    r = env.install()
    assert r.returncode == 0, r.stdout + r.stderr
    lib = env.p("usr/local/lib/homelab-maint/homelab_maint")
    assert (lib / "core.py").is_file() and (lib / "tasks" / "native.py").is_file()
    # the playbook/SLO baseline travels INSIDE the package (replaced on every install); /etc holds only the overrides template
    assert (lib / "data" / "playbooks.toml").read_bytes() == (REPO / "homelab_maint" / "data" / "playbooks.toml").read_bytes()
    for mod in ("scheduler", "live", "metrics_ring", "routine", "probes", "notify", "publish", "legacy"):
        assert (lib / f"{mod}.py").is_file(), f"{mod}.py was not installed (the units and tick jobs run it)"
    assert env.p("etc/homelab-maint/maint.toml").is_file()
    for name in ("jobs.toml", "routine.toml", "probes.toml", "classes.toml", "notify.toml", "ack.toml", "legacy-retirement.toml", "playbooks.toml"):
        f = env.p(f"etc/homelab-maint/{name}")
        assert f.read_text() == (REPO / "etc" / name).read_text() and f.stat().st_mode & 0o777 == 0o644, name
    # the registry is installed only once the release ships rules: the baseline alone would compile to a config with no protections
    shipped = sorted(q.name for q in (ETC / "rules.d").glob("*.toml"))
    has_rules = any(not re.match(r"(00|99)-", n) for n in shipped)
    assert env.p("etc/homelab-maint/rules.d").is_dir() == has_rules, shipped
    # the acknowledge postbox exists with its two secrets
    assert env.p("var/lib/homelab-maint/ack/web.key").is_file() and env.p("var/lib/homelab-maint/ack/bootstrap.secret").is_file()
    assert env.install().stdout.count("Nothing to do") == 1


# --------------------------------------------------------------------------- runtime: the acknowledge postbox
ACK = "var/lib/homelab-maint/ack"


def _modes(env: Env, *rels: str) -> dict[str, int]:
    return {r: env.p(f"{ACK}/{r}" if r else ACK).stat().st_mode & 0o7777 for r in rels}


@needs_userns
def test_staged_install_creates_the_ack_postbox_with_the_documented_modes_and_secrets(env):
    r = env.install()
    assert r.returncode == 0, r.stdout + r.stderr
    # ack/ 0750 (root + the website's gid, like `ack init --group`); inbox 1730 (the container may create a file in it, not list or read the
    # others); rejected 0700; key 0640; bootstrap 0600; web_ready is not created by a plain install (only --deploy-web, after the site is healthy)
    assert _modes(env, "", "inbox", "inbox/rejected", "web.key", "bootstrap.secret") == {
        "": 0o750, "inbox": 0o1730, "inbox/rejected": 0o700, "web.key": 0o640, "bootstrap.secret": 0o600}
    assert not env.p(f"{ACK}/web_ready").exists()
    key, boot = env.p(f"{ACK}/web.key").read_text(), env.p(f"{ACK}/bootstrap.secret").read_text()
    assert re.fullmatch(r"[0-9a-f]{64}\n", key), "the HMAC key is 64 hex characters of text (acks.sign uses that text)"
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}\n", boot)
    assert key.strip() != boot.strip()
    assert key.strip() not in r.stdout + r.stderr and boot.strip() not in r.stdout + r.stderr, "a secret must never be printed"
    assert not list((env.tmp / "tmpdir").iterdir()), "the secrets pass through the 0700 stage dir only, which is removed"
    # the group is not applicable in an unprivileged user namespace: the installer falls back to root, closed (checked in the dry run below)
    assert "cannot use group" not in r.stderr, "staging stays quiet about it (a real host warns)"


@needs_userns
def test_existing_secrets_are_never_overwritten_only_their_mode_is_put_back(env):
    assert env.install().returncode == 0
    key, boot = env.p(f"{ACK}/web.key"), env.p(f"{ACK}/bootstrap.secret")
    before = (key.read_text(), boot.read_text())
    os.chmod(key, 0o644)                      # someone loosened it
    os.chmod(boot, 0o660)
    os.chmod(env.p(f"{ACK}/inbox"), 0o777)
    os.chmod(env.p(ACK), 0o755)                                                    # an older install made ack/ 0755: closed to the group now
    r = env.install()
    assert r.returncode == 0, r.stderr
    assert (key.read_text(), boot.read_text()) == before, "a secret was regenerated"
    assert _modes(env, "", "web.key", "bootstrap.secret", "inbox") == {"": 0o750, "web.key": 0o640, "bootstrap.secret": 0o600, "inbox": 0o1730}
    assert before[0].strip() not in r.stdout + r.stderr
    again = env.install()
    assert "Nothing to do" in again.stdout and (key.read_text(), boot.read_text()) == before
    # only an ABSENT file is generated: deleting the bootstrap secret creates a fresh one, the key stays
    boot.unlink()
    assert env.install().returncode == 0
    assert boot.read_text() != before[1] and key.read_text() == before[0]


@needs_userns
def test_a_symlinked_secret_is_left_alone(env):
    assert env.install().returncode == 0
    key = env.p(f"{ACK}/web.key")
    key.unlink()
    elsewhere = env.tmp / "elsewhere"
    elsewhere.write_text("not mine\n")
    key.symlink_to(elsewhere)
    r = env.install()
    assert r.returncode == 0 and "web.key is a symlink: left alone" in r.stderr
    assert elsewhere.read_text() == "not mine\n" and key.is_symlink()


def test_dry_run_plans_the_ack_postbox_for_gid_10001_and_writes_nothing(env):
    before = tree_state(env.root)
    r = env._run("install.sh", "--dry-run", userns=False)
    assert r.returncode == 0, r.stderr
    assert "install -d -m 1730 -o root -g 10001 -- " in r.stdout and "/var/lib/homelab-maint/ack/inbox" in r.stdout
    assert re.search(r"install -d -m 0750 -o root -g 10001 -- \S+/var/lib/homelab-maint/ack$", r.stdout, re.M)          # ack/ itself: 0750 root:<web gid>
    assert re.search(r"install -m 0640 -o root -g 10001 -- \S+/new\.value \S+/ack/web\.key", r.stdout)
    assert re.search(r"install -m 0600 -o root -g root -- \S+/new\.value \S+/ack/bootstrap\.secret", r.stdout)
    assert tree_state(env.root) == before and not env.p("var").exists()
    r2 = env._run("install.sh", "--dry-run", userns=False, extra_env={"HM_WEB_GID": "20001"})
    assert "-g 20001 -- " in r2.stdout and "-g 10001" not in r2.stdout, "HM_WEB_GID overrides the container's gid"


def test_the_websites_gid_is_the_one_web_key_already_has_unless_the_owner_says_otherwise(env):
    """A host that deployed another gid keeps it on a re-install (the runner's web_gid() trusts web.key's group too); root says nothing."""
    other = [g for g in os.getgroups() if g not in (0, os.getgid())]
    if not other:
        pytest.skip("needs a second group this user belongs to")
    d = env.p(ACK)
    d.mkdir(parents=True)
    (d / "web.key").write_text("k" * 64 + "\n")
    os.chown(d / "web.key", -1, other[0])
    r = env._run("install.sh", "--dry-run", userns=False)
    assert r.returncode == 0 and f"-g {other[0]} -- " in r.stdout and "-g 10001" not in r.stdout, r.stdout
    assert re.search(rf"install -d -m 0750 -o root -g {other[0]} -- \S+/ack$", r.stdout, re.M)
    r2 = env._run("install.sh", "--dry-run", userns=False, extra_env={"HM_WEB_GID": "20001"})
    assert "-g 20001 -- " in r2.stdout and f"-g {other[0]} -- " not in r2.stdout                  # the owner's explicit setting wins
    os.chown(d / "web.key", -1, os.getgid())                                                      # ... and a key in the host user's own group is just a group: used as is
    assert f"-g {os.getgid()} -- " in env._run("install.sh", "--dry-run", userns=False).stdout


def test_install_creates_a_secret_only_through_the_stage_dir_and_never_puts_one_on_a_command_line():
    t = INSTALL.read_text()
    assert t.count("ensure_secret ") >= 3 and "umask 077" in t
    for n, line in code_lines(INSTALL):
        assert not re.search(r"token_(hex|urlsafe)", line) or "new_secret()" in line, f"line {n}: a secret is generated outside new_secret"


def _install_funcs(*names: str) -> str:
    text = INSTALL.read_text()
    out = []
    for n in names:
        m = re.search(rf"(?ms)^{n}\(\) \{{\n.*?^\}}\n", text)
        assert m, f"install.sh has no function {n}"
        out.append(m.group(0))
    return "\n".join(out)


def test_web_ready_is_created_in_one_place_and_only_after_the_container_reports_healthy():
    """ack/web_ready turns the e-mail Acknowledge button on: a link to a site that is not up is worse than none. The only creation is
    mark_web_ready, called once, in the branch that needed wait_web_healthy to succeed (and never with --no-web-ready or a dry run)."""
    t = INSTALL.read_text()
    assert t.count('-- /dev/null "$f"') == 1 and '-- /dev/null "$f"' in _install_funcs("mark_web_ready")
    assert not re.search(r"\btouch\b[^\n]*web_ready", "\n".join(l for _n, l in code_lines(INSTALL))), "web_ready is made by mark_web_ready, not by a stray touch"
    calls = list(re.finditer(r"^\s+mark_web_ready$", t, re.M))
    assert len(calls) == 1
    chain = t[t.rindex("if ((!WEB_READY)); then", 0, calls[0].start()):calls[0].end()]
    assert re.search(r"elif \(\(DRY\)\); then.*elif wait_web_healthy; then\s+mark_web_ready$", chain, re.S), chain
    assert "docker compose" in t[:t.rindex("if ((!WEB_READY)); then")] and t.index("up -d --build") < t.index("if ((!WEB_READY)); then")


STUB_DOCKER_HEALTH = """#!/bin/sh
# `docker inspect --format ... maintenance-web`: the next status from $STUB_HEALTH (one per line, the last one repeats); every call counted
n=$(cat "$STUB_CNT" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$STUB_CNT"
[ "$1" = inspect ] || exit 9
sed -n "${n}p" "$STUB_HEALTH" | grep . || tail -n 1 "$STUB_HEALTH"
"""


def _wait_web(tmp_path: Path, statuses: list[str], wait: int = 5, mark: bool = False):
    bindir = tmp_path / "hbin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(STUB_DOCKER_HEALTH)
    (bindir / "docker").chmod(0o755)
    (tmp_path / "health").write_text("\n".join(statuses) + "\n")
    (tmp_path / "cnt").unlink(missing_ok=True)
    state = tmp_path / "state"
    (state / "ack").mkdir(parents=True, exist_ok=True)
    script = ("set -euo pipefail; DRY=0; STATE=\"$1\"\n"
              "note() { printf '%s %s\\n' \"$1\" \"${2:-}\"; }; verb() { printf '%s' \"$1\"; }; changed() { :; }; run() { \"$@\"; }\n"
              + _install_funcs("wait_web_healthy", "mark_web_ready")
              + ("\nif wait_web_healthy; then mark_web_ready; echo READY; else echo NOT-HEALTHY; fi\n" if mark else "\nwait_web_healthy && echo HEALTHY || echo NOT-HEALTHY\n"))
    cmd = ["unshare", "-r", "bash", "-c", script, "x", str(state)] if mark else ["bash", "-c", script, "x", str(state)]
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "STUB_HEALTH": str(tmp_path / "health"), "STUB_CNT": str(tmp_path / "cnt"), "HM_WEB_WAIT_S": str(wait)}
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
    return r, state / "ack" / "web_ready", int((tmp_path / "cnt").read_text())


def test_wait_web_healthy_polls_docker_until_healthy_and_stops_on_unhealthy_or_timeout(tmp_path):
    r, _f, polls = _wait_web(tmp_path, ["starting", "starting", "healthy"], wait=10)
    assert r.stdout.strip() == "HEALTHY" and polls == 3, (r.stdout, r.stderr)
    r, _f, polls = _wait_web(tmp_path, ["starting", "unhealthy", "healthy"], wait=10)
    assert r.stdout.strip() == "NOT-HEALTHY" and polls == 2                                    # an unhealthy container is not waited on
    r, _f, polls = _wait_web(tmp_path, ["starting"], wait=2)
    assert r.stdout.strip() == "NOT-HEALTHY" and 2 <= polls <= 4                              # never healthy: gives up after HM_WEB_WAIT_S
    r, _f, _p = _wait_web(tmp_path, ["none"], wait=1)
    assert r.stdout.strip() == "NOT-HEALTHY"                                                  # a container without a healthcheck does not count as healthy


@needs_userns
def test_web_ready_exists_only_when_the_container_was_healthy(tmp_path):
    r, flag, _p = _wait_web(tmp_path, ["starting", "healthy"], wait=10, mark=True)
    assert "READY" in r.stdout and flag.is_file() and flag.stat().st_mode & 0o777 == 0o644 and flag.read_bytes() == b""
    flag.unlink()
    r, flag, _p = _wait_web(tmp_path, ["starting"], wait=1, mark=True)
    assert "NOT-HEALTHY" in r.stdout and not flag.exists()
    r, flag, _p = _wait_web(tmp_path, ["unhealthy"], wait=5, mark=True)
    assert "NOT-HEALTHY" in r.stdout and not flag.exists()
    flag.write_text("")
    r, flag, _p = _wait_web(tmp_path, ["healthy"], wait=5, mark=True)                          # an existing one is kept, not rewritten
    assert "same" in r.stdout and flag.is_file()


# --------------------------------------------------------------------------- runtime: the rules registry
BASELINE = "00-baseline-invariants.toml"


def _with_rules(env: Env, *names: str) -> Path:
    d = env.proj / "etc" / "rules.d"
    d.mkdir(exist_ok=True)
    (d / BASELINE).write_text('[meta]\ncategory = "safety"\n# baseline v1\n')
    for n in names:
        (d / n).write_text(f'[meta]\ncategory = "checks"\n# {n} v1\n')
    return d


@needs_userns
def test_rules_d_is_not_installed_while_the_release_ships_only_the_baseline(env):
    _with_rules(env)
    r = env.install()
    assert r.returncode == 0, r.stderr
    assert not env.p("etc/homelab-maint/rules.d").exists(), "a baseline-only registry compiles to no protections and would be refused"
    assert "rules.d (this release ships the safety baseline only" in r.stdout
    assert "rules sync" not in r.stdout


@needs_userns
def test_rules_d_installs_with_the_baseline_always_replaced_and_the_owners_files_kept(env):
    src = _with_rules(env, "10-checks.toml")
    r = env.install()
    assert r.returncode == 0, r.stdout + r.stderr
    rd = env.p("etc/homelab-maint/rules.d")
    assert rd.stat().st_mode & 0o777 == 0o755
    for n in (BASELINE, "10-checks.toml"):
        assert (rd / n).read_text() == (src / n).read_text() and (rd / n).stat().st_mode & 0o777 == 0o644, n
    # a new release: the baseline is release data (it mirrors the floor pinned in the code), the owner's rule file is his
    (rd / BASELINE).write_text("# stale copy\n")
    (rd / "10-checks.toml").write_text("# my rules\n")
    (src / BASELINE).write_text('[meta]\ncategory = "safety"\n# baseline v2\n')
    (src / "10-checks.toml").write_text("# shipped v2\n")
    (src / "20-cleanup.toml").write_text("# a new shipped file\n")
    r2 = env.install()
    assert r2.returncode == 0, r2.stderr
    assert "baseline v2" in (rd / BASELINE).read_text()
    assert (rd / "10-checks.toml").read_text() == "# my rules\n" and "kept" in r2.stdout
    assert (rd / "20-cleanup.toml").read_text() == "# a new shipped file\n"
    assert not list(rd.glob("*.dist")), "nothing but registry files may sit in rules.d"
    assert "Nothing to do" in env.install().stdout


@needs_userns
def test_the_first_registry_install_prints_the_adopt_command_and_never_runs_it(env):
    _with_rules(env, "10-checks.toml")
    r = env.install()
    assert r.returncode == 0, r.stderr
    assert "sudo homelab-maint rules sync --adopt" in r.stdout and "homelab-maint rules diff" in r.stdout and "rules/orig" in r.stdout
    assert "blocked" in r.stdout, "it says why: a differing file is reported as blocked, never replaced"
    assert "before the first check run" in r.stdout and "rules.d is installed" in r.stdout
    assert not env.p("var/lib/homelab-maint/rules").exists(), "the installer must not run `rules sync` (it would write state/rules)"
    # once the registry has been adopted (state/rules/current.json) the hint goes away
    cur = env.p("var/lib/homelab-maint/rules")
    cur.mkdir()
    (cur / "current.json").write_text("{}")
    assert "rules sync --adopt" not in env.install().stdout


@needs_userns
def test_adopt_rules_is_explicit_and_skipped_in_a_staged_install(env):
    _with_rules(env, "10-checks.toml")
    r = env.install("--adopt-rules")
    assert r.returncode == 0, r.stderr
    assert "rules sync --adopt (staging root)" in r.stdout and "skipped" in r.stdout
    code = INSTALL.read_text()
    runs = [n for n, line in code_lines(INSTALL) if re.search(r'run "" rules sync', line)]       # (quoted strings are blanked)
    assert len(runs) == 1, "exactly one place may run the sync"
    lines = code.splitlines()
    assert any("ADOPT_RULES" in lines[i] for i in range(runs[0] - 8, runs[0])), "...and only behind --adopt-rules"


def test_adopt_rules_in_a_dry_run_runs_nothing(env):
    _with_rules(env, "10-checks.toml")
    r = env._run("install.sh", "--dry-run", "--adopt-rules", userns=False)
    assert r.returncode == 0, r.stderr
    # (a dry run installs nothing, but it still says that adoption is pending and plans the sync, behind --adopt-rules only)
    assert "would-install" in r.stdout and "rules.d/10-checks.toml" in r.stdout
    assert "rules.d would be installed" in r.stdout and "+ " in r.stdout and "rules sync --adopt" in r.stdout
    assert not env.p("etc").exists() and not env.p("var").exists()


def test_a_dry_run_names_the_adoption_step_like_a_real_run_does(env):
    """Reported by the verifier: the 'Rules registry ... adopt with rules sync --adopt' notice only printed once rules.d existed, so a dry run on a fresh host
    (rules.d not there yet) said nothing about it. It must read the same, and the 'Next:' line must put adoption before the first check run."""
    _with_rules(env, "10-checks.toml")
    r = env._run("install.sh", "--dry-run", userns=False)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "Rules registry: rules.d would be installed" in out and "sudo homelab-maint rules sync --adopt" in out and "homelab-maint rules diff" in out
    assert "before the first check run" in out and "blocked" in out
    code = INSTALL.read_text()                                      # (a staged run prints no "Next:" block: read the order from the script itself)
    nxt = code[code.index("ADOPT_PENDING && !ADOPT_RULES"):]
    assert nxt.index("rules sync --adopt") < nxt.index("run --tier check"), "adoption comes before the first check run"
    assert not env.p("etc").exists() and not env.p("var").exists(), "a dry run writes nothing"
    assert "rules sync --adopt" not in "\n".join(l for l in out.splitlines() if l.startswith("    + ")), "and plans no sync without --adopt-rules"


# --------------------------------------------------------------------------- the website's mounts match what the installer creates
COMPOSE = REPO / "web" / "docker-compose.yml"


def test_every_state_directory_the_website_container_mounts_is_created_by_the_installer_and_only_the_inbox_is_writable():
    """Reads web/docker-compose.yml (owned by the website team) and checks packaging against it: every host path it mounts from the
    state dir exists after install.sh, and nothing but ack/inbox is mounted writable. Passes before the acknowledge mounts are added."""
    if not COMPOSE.is_file():
        pytest.skip("web/docker-compose.yml is not in this tree (the old website was decommissioned)")
    text = COMPOSE.read_text()
    mounts = []
    for raw in text.splitlines():
        m = re.match(r'^\s*-\s*"?(?:\$\{\w+:-)?(/var/lib/homelab-maint[^}:"\s]*)\}?:(/[^:"\s]+)(?::(\w+))?"?\s*(?:#.*)?$', raw)
        if m:
            mounts.append((m.group(1), m.group(2), m.group(3) or "rw"))
    assert ("/var/lib/homelab-maint/public", "/data/public", "ro") in mounts, mounts
    script = INSTALL.read_text()
    for host, _inside, mode in mounts:
        rel = host.removeprefix("/var/lib/homelab-maint/")
        assert f'"$STATE/{rel}"' in script, f"install.sh does not create {host}, which web/docker-compose.yml mounts"
        if mode != "ro":
            assert rel == "ack/inbox", f"{host} is mounted writable: the container may only write the ack inbox"
    code = re.sub(r"(?m)#.*$", "", text)
    assert "docker.sock" not in code and not re.search(r"privileged:\s*true", code), "the website container gets no docker socket and no privileges"
    assert re.search(r"read_only:\s*true", code) and re.search(r"cap_drop:\s*\[ALL\]", code)


# --------------------------------------------------------------------------- runtime: staged uninstall
@needs_userns
def test_uninstall_removes_program_and_units_but_keeps_config_state_logs(env):
    env.with_original_unit()
    assert env.install().returncode == 0
    env.p("var/lib/homelab-maint/status.json").write_text("{}")
    env.p("var/log/homelab-maint/audit.jsonl").write_text("{}\n")
    r = env.uninstall()
    assert r.returncode == 0, r.stdout + r.stderr
    assert not env.p("usr/local/sbin/homelab-maint").exists()
    assert not env.p("usr/local/lib/homelab-maint").exists()
    for name in EXPECTED_UNITS:
        assert not env.p(f"etc/systemd/system/{name}").exists(), name
    assert not env.p("etc/systemd/system/immich-server-recycle.service.d").exists()
    assert env.p("etc/systemd/system/immich-server-recycle.service").read_text() == ORIGINAL_IMMICH_UNIT
    assert env.p("etc/homelab-maint/maint.toml").is_file()
    assert env.p("var/lib/homelab-maint/status.json").is_file()
    assert env.p("var/log/homelab-maint/audit.jsonl").is_file()
    assert "Kept" in r.stdout
    # the acknowledgements and the registry's history live in the state dir: an uninstall keeps the keys and every record
    assert env.p(f"{ACK}/web.key").is_file() and env.p(f"{ACK}/bootstrap.secret").is_file() and env.p(f"{ACK}/inbox").is_dir()
    # idempotent: a second uninstall finds nothing to do and still succeeds
    again = env.uninstall()
    assert again.returncode == 0 and "Done: 0 path(s) removed." in again.stdout


@needs_userns
def test_uninstall_leaves_foreign_dropins_alone(env):
    env.with_original_unit()
    assert env.install().returncode == 0
    mine = env.p("etc/systemd/system/immich-server-recycle.service.d/50-mine.conf")
    mine.write_text("[Service]\nNice=5\n")
    assert env.uninstall().returncode == 0
    assert mine.is_file()
    assert not env.p(f"etc/systemd/system/{DROPIN_REL}").exists()


@needs_userns
def test_uninstall_purge_with_yes_removes_config_state_logs(env):
    assert env.install().returncode == 0
    r = env.uninstall("--purge", "--yes")
    assert r.returncode == 0, r.stderr
    for rel in ("etc/homelab-maint", "var/lib/homelab-maint", "var/log/homelab-maint", "usr/local/lib/homelab-maint"):
        assert not env.p(rel).exists(), rel


@needs_userns
def test_uninstall_dry_run_removes_nothing(env):
    env.with_original_unit()
    assert env.install().returncode == 0
    before = tree_state(env.root)
    r = env.uninstall("--dry-run", "--purge", "--yes")
    assert r.returncode == 0
    assert "would-remove" in r.stdout
    assert tree_state(env.root) == before


# --------------------------------------------------------------------------- regression: uninstall in-progress guard
# The guard lives in the live-systemd branch, which the HM_ROOT staging runs skip. It is exercised here by
# putting a stub `systemctl` first on PATH and running the real uninstall.sh with --dry-run (so run() only
# prints and nothing on this machine can change; _uninstall_live_dry refuses to run without --dry-run).
STUB_SYSTEMCTL = """#!/usr/bin/env bash
# Stand-in for systemctl. States come from $STUB_STATES ("UNIT STATE" per line, default inactive); every
# call is appended to $STUB_LOG. Exit codes mimic the real one: `is-active` is 0 ONLY for active/reloading
# and 3 otherwise, so a Type=oneshot unit that is mid-run (state "activating") looks "not active" to it.
echo "$*" >> "$STUB_LOG"
state_of() { awk -v u="$1" '$1 == u { print $2; f = 1 } END { if (!f) print "inactive" }' "$STUB_STATES"; }
rc=0
case $1 in
  show) [[ -z ${STUB_SHOW_FAILS:-} ]] || exit 1; state_of "${@: -1}" ;;
  is-active)
    quiet=0; units=()
    for a in "${@:2}"; do if [[ $a == --quiet || $a == -q ]]; then quiet=1; else units+=("$a"); fi; done
    s=$(state_of "${units[0]}"); ((quiet)) || echo "$s"
    [[ $s == active || $s == reloading ]] || rc=3 ;;
  is-enabled) echo disabled; rc=1 ;;
esac
exit $rc
"""
STATE_CHANGING_VERBS = {"disable", "enable", "stop", "start", "restart", "daemon-reload", "reset-failed", "kill", "mask"}


def _uninstall_live_dry(tmp_path: Path, states: dict[str, str], *args: str, show_fails: bool = False, extra_env: dict | None = None):
    assert "--dry-run" in args, "the live-systemd branch may only ever be run as a dry run from a test"
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    stub = bindir / "systemctl"
    stub.write_text(STUB_SYSTEMCTL)
    stub.chmod(0o755)
    (tmp_path / "states").write_text("".join(f"{u} {s}\n" for u, s in states.items()))
    log = tmp_path / "systemctl.log"
    e = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "STUB_STATES": str(tmp_path / "states"),
         "STUB_LOG": str(log),
         # nothing about the real host may decide these tests: no scheduler job running, nothing retired by a cutover
         "HM_RUNNING_JOBS": "", "HM_RETIRED_UNITS": "", "HM_RETIRED_PATHS": "", **(extra_env or {})}
    e.pop("HM_ROOT", None)
    if show_fails:
        e["STUB_SHOW_FAILS"] = "1"
    r = subprocess.run(["bash", str(UNINSTALL), *args], capture_output=True, text=True, env=e, timeout=60)
    calls = log.read_text().splitlines() if log.exists() else []
    assert not [c for c in calls if c.split()[0] in STATE_CHANGING_VERBS], f"state change attempted: {calls}"
    return r, calls


def test_stub_systemctl_is_active_misses_a_running_oneshot(tmp_path):
    """Documents the trap behind the guard: is-active --quiet says 'not active' for an activating unit."""
    r, calls = _uninstall_live_dry(tmp_path, {"x.service": "activating"}, "--dry-run")   # builds the stub
    stub = tmp_path / "stubbin" / "systemctl"
    env = {**os.environ, "STUB_STATES": str(tmp_path / "states"), "STUB_LOG": str(tmp_path / "t.log")}
    assert subprocess.run([str(stub), "is-active", "--quiet", "x.service"], env=env).returncode == 3
    assert subprocess.run([str(stub), "show", "-p", "ActiveState", "--value", "x.service"],
                          env=env, capture_output=True, text=True).stdout.strip() == "activating"


@pytest.mark.parametrize("tier", TIER_SERVICES)
@pytest.mark.parametrize("state", ["activating", "active", "reloading", "deactivating"])
def test_uninstall_refuses_while_a_tier_run_is_in_progress(tmp_path, tier, state):
    unit = f"homelab-maint-{tier}.service"
    r, calls = _uninstall_live_dry(tmp_path, {unit: state}, "--dry-run")
    assert r.returncode != 0, r.stdout
    assert f"{unit} is running" in r.stderr and f"state: {state}" in r.stderr and "--force" in r.stderr
    assert any(c.startswith("show") and "ActiveState" in c for c in calls), "must ask systemd for ActiveState"
    assert "Stop and disable" not in r.stdout, "refusal must come before anything is stopped"


@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_uninstall_proceeds_when_tier_services_are_idle(tmp_path, state):
    states = {f"homelab-maint-{t}.service": state for t in TIER_SERVICES}
    r, _ = _uninstall_live_dry(tmp_path, states, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "is running" not in r.stderr and "Stop and disable" in r.stdout


def test_uninstall_force_proceeds_with_a_warning_when_a_tier_is_running(tmp_path):
    r, _ = _uninstall_live_dry(tmp_path, {"homelab-maint-daily.service": "activating"}, "--dry-run", "--force")
    assert r.returncode == 0, r.stderr
    assert "homelab-maint-daily.service is running and will be stopped" in r.stderr
    assert "Stop and disable" in r.stdout


def test_uninstall_fails_closed_when_systemd_gives_no_answer(tmp_path):
    r, _ = _uninstall_live_dry(tmp_path, {}, "--dry-run", show_fails=True)
    assert r.returncode != 0
    assert "is running" in r.stderr and "state: unknown" in r.stderr


def _purge_dry(tmp_path: Path, docker_state: str):
    """A purge dry run with stub systemctl AND stub docker first on PATH; returns (result, docker calls)."""
    dockerbin = tmp_path / "dockerbin"
    dockerbin.mkdir()
    (dockerbin / "docker").write_text('#!/bin/sh\necho "$*" >> "$STUB_LOG_DOCKER"\ncase $1 in ps) echo maintenance-web ;; esac\nexit 0\n')
    (dockerbin / "docker").chmod(0o755)
    path = f"{dockerbin}:{tmp_path / 'stubbin'}:{os.environ['PATH']}"
    r, _calls = _uninstall_live_dry(tmp_path, {"docker.service": docker_state}, "--dry-run", "--purge", "--yes",
                                    extra_env={"PATH": path, "STUB_LOG_DOCKER": str(tmp_path / "docker.log")})
    log = tmp_path / "docker.log"
    return r, (log.read_text().splitlines() if log.exists() else [])


def test_a_purge_removes_the_website_container_it_would_leave_on_deleted_inodes_but_never_wakes_dockerd(tmp_path):
    """The container bind-mounts public/ and ack/; a purge deletes them under it, and a reinstall would then serve stale data from the
    deleted inodes. dockerd is socket-activated here, so docker is only asked when systemd says docker.service is already active."""
    (tmp_path / "up").mkdir()
    r, docker = _purge_dry(tmp_path / "up", "active")
    assert r.returncode == 0, r.stderr
    assert "+ docker rm -f maintenance-web" in r.stdout and not [c for c in docker if c.startswith(("rm", "stop", "kill"))], docker
    (tmp_path / "down").mkdir()
    r2, docker2 = _purge_dry(tmp_path / "down", "inactive")
    assert r2.returncode == 0 and docker2 == [], f"dockerd is stopped: docker must not be asked (it would start it): {docker2}"
    assert "docker rm" not in r2.stdout
    t = UNINSTALL.read_text()
    assert t.index("is-active --quiet docker.service") < t.index("docker rm -f maintenance-web") < t.index('rm_path "$CONF"')
    assert t.index("if ((PURGE)); then") < t.index("docker rm -f maintenance-web"), "only a purge touches the container"


# --------------------------------------------------------------------------- the live-systemd branch of install.sh (stubbed dry runs)
# Which units get enabled, what a cutover keeps off, and the website deploy all live in the branch that HM_ROOT staging skips. It is run
# here with --dry-run only (run() prints, nothing executes), with stub `systemctl` and `docker` first on PATH, from a throw-away project.
STUB_DOCKER = """#!/bin/sh
echo "$*" >> "$STUB_LOG_DOCKER"
exit 0
"""
FAKE_MODULES = ("live.py", "metrics_ring.py", "scheduler.py", "tasks/self_health.py")   # what NEEDS in install.sh looks for (server.py is in the fake project)


def _install_live_dry(env: Env, *args: str, retired_units: str = "", retired_paths: str = "", with_modules: bool = True):
    assert "--dry-run" in args, "the live-systemd branch may only ever be run as a dry run from a test"
    if with_modules:
        for m in FAKE_MODULES:
            (env.proj / "homelab_maint" / m).write_text("VALUE = 1\n")
    bindir = env.tmp / "stubbin"
    bindir.mkdir(exist_ok=True)
    for name, body in (("systemctl", STUB_SYSTEMCTL), ("docker", STUB_DOCKER)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    (env.tmp / "states").write_text("")
    e = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "STUB_STATES": str(env.tmp / "states"), "STUB_LOG": str(env.tmp / "systemctl.log"),
         "STUB_LOG_DOCKER": str(env.tmp / "docker.log"), "TMPDIR": str(env.tmp / "tmpdir"),
         "HM_RETIRED_UNITS": retired_units, "HM_RETIRED_PATHS": retired_paths}
    e.pop("HM_ROOT", None)
    r = subprocess.run(["bash", str(env.proj / "install.sh"), *args], capture_output=True, text=True, env=e, timeout=180)
    log = env.tmp / "systemctl.log"
    calls = log.read_text().splitlines() if log.exists() else []
    assert not [c for c in calls if c.split()[0] in STATE_CHANGING_VERBS], f"state change attempted: {calls}"
    dlog = env.tmp / "docker.log"
    return r, calls, (dlog.read_text().splitlines() if dlog.exists() else [])


def _planned(r, verb: str) -> set[str]:
    """Units a dry run would `systemctl VERB` (run() prints them as '    + systemctl VERB UNIT')."""
    return set(re.findall(rf"^\s+\+ systemctl {verb} (\S+)$", r.stdout, re.M))


def test_install_would_enable_and_start_the_timers_and_the_two_daemons_only(env):
    r, _calls, _d = _install_live_dry(env, "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    want = ENABLED_TIMERS | DAEMONS
    assert _planned(r, "enable") == want
    assert _planned(r, "start") == want, "timers and the two daemons are started; the services the timers trigger start by themselves"
    assert not _planned(r, "restart"), "nothing is running (stub), so nothing is restarted"
    assert "+ systemctl daemon-reload" in r.stdout or "daemon-reload not needed" in r.stdout      # the dry run reads the real host: already-installed units need no reload
    assert not set(re.findall(r"systemctl \w+(?: --no-block)? (homelab-maint-\S+\.service)", r.stdout)) & TIMER_DRIVEN
    assert "WARNING" not in r.stderr


def test_install_dry_run_leaves_what_a_cutover_retired_alone(env):
    dropin = "/etc/systemd/system/immich-server-recycle.service.d/10-homelab-gate.conf"
    r, _c, _d = _install_live_dry(env, "--dry-run", retired_units="homelab-maint-check.timer\nhomelab-maint-tick.timer\n", retired_paths=dropin + "\n")
    assert r.returncode == 0, r.stdout + r.stderr
    want = (ENABLED_TIMERS | DAEMONS) - {"homelab-maint-check.timer", "homelab-maint-tick.timer"}
    assert _planned(r, "enable") == want and _planned(r, "start") == want
    assert re.search(r"retired\s+homelab-maint-check\.timer", r.stdout) and re.search(r"retired\s+homelab-maint-tick\.timer", r.stdout)
    assert re.search(r"retired\s+immich-server-recycle\.service\.d/10-homelab-gate\.conf", r.stdout)
    assert "10-homelab-gate.conf" not in "".join(ln for ln in r.stdout.splitlines() if "install" in ln), "the drop-in must not be put back"
    # the unit FILES are still installed (a file is harmless); only enabling is withheld
    for unit in ("homelab-maint-check.timer", "homelab-maint-tick.timer"):
        assert re.search(rf"(would-install|same)\s+\S*{re.escape(unit)}$", r.stdout, re.M), unit      # "same" when this host already has it
    # and without the retirement the same drop-in is planned (the test above would otherwise pass vacuously)
    r2, _c, _d = _install_live_dry(env, "--dry-run")
    assert re.search(r"(would-install|same)\s+\S*10-homelab-gate\.conf", r2.stdout)


def test_install_dry_run_skips_a_unit_whose_module_is_not_in_the_source_tree(env):
    r, _c, _d = _install_live_dry(env, "--dry-run", with_modules=False)       # the fake project has server.py but no live/metrics/scheduler
    assert r.returncode == 0, r.stdout + r.stderr
    assert _planned(r, "enable") == {"homelab-maint-check.timer", "homelab-maint-daily.timer", "homelab-maint-weekly.timer", "homelab-maint-www.service"}
    for mod, unit in (("live.py", "homelab-maint-live.service"), ("metrics_ring.py", "homelab-maint-metrics.timer"),
                      ("scheduler.py", "homelab-maint-tick.timer"), ("tasks/self_health.py", "homelab-maint-selfhealth.timer")):
        assert f"homelab_maint/{mod} is missing" in r.stderr and f"{unit} not enabled" in r.stderr


def test_deploy_web_dry_run_uses_both_compose_files_and_builds_nothing(env):
    web = env.proj / "web"
    web.mkdir()
    (web / "docker-compose.yml").write_text("services: {}\n")
    (web / "docker-compose.override.yml").write_text("networks: {}\n")
    r, _c, docker = _install_live_dry(env, "--dry-run", "--deploy-web")
    assert r.returncode == 0, r.stdout + r.stderr
    up = [ln for ln in r.stdout.splitlines() if "docker compose" in ln and "up -d --build" in ln]
    assert len(up) == 1 and f"-f {web}/docker-compose.yml -f {web}/docker-compose.override.yml" in up[0], up
    assert docker == ["compose version"], f"a dry run may only ask whether compose exists: {docker}"
    assert "would-deploy" in r.stdout and "web/README.md" in r.stdout
    assert re.search(r"would-wait\s+for maintenance-web to report healthy .*then create ack/web_ready", r.stdout)       # the button follows the site
    assert not re.search(r"^\s+\+ .*web_ready", r.stdout, re.M), "a dry run creates nothing"
    (env.tmp / "docker.log").unlink()
    r3, _c, docker3 = _install_live_dry(env, "--dry-run", "--deploy-web", "--no-web-ready")
    assert r3.returncode == 0 and "would-wait" not in r3.stdout and re.search(r"skipped\s+ack/web_ready \(--no-web-ready\).*sudo touch /var/lib/homelab-maint/ack/web_ready", r3.stdout)
    assert docker3 == ["compose version"]
    # without --deploy-web nothing about docker happens at all
    (env.tmp / "docker.log").unlink()
    r2, _c, docker2 = _install_live_dry(env, "--dry-run")
    assert docker2 == [] and "docker compose" not in r2.stdout


def test_deploy_web_refuses_without_the_override_compose_file(env):
    web = env.proj / "web"
    web.mkdir()
    (web / "docker-compose.yml").write_text("services: {}\n")                  # the override pins the subnet: a plain `up` fails here
    r, _c, docker = _install_live_dry(env, "--dry-run", "--deploy-web")
    assert r.returncode == 0 and "docker-compose.override.yml is missing" in r.stderr and "NOT deployed" in r.stderr
    assert docker == [] and "up -d" not in r.stdout


@needs_userns
def test_deploy_web_is_skipped_in_a_staged_install(env):
    r = env.install("--deploy-web")
    assert r.returncode == 0, r.stderr
    assert "docker compose (staging root)" in r.stdout


def test_uninstall_removes_exactly_the_units_install_puts_down():
    m = re.search(r"UNIT_NAMES=\(([^)]*)\)", UNINSTALL.read_text())
    assert m and set(m.group(1).split()) == EXPECTED_UNITS
    names = m.group(1).split()
    assert names.index("homelab-maint-tick.timer") < names.index("homelab-maint-tick.service"), "timers first, so nothing new is triggered"


def test_uninstall_refuses_while_a_scheduler_job_is_running(tmp_path):
    r, calls = _uninstall_live_dry(tmp_path, {}, "--dry-run", extra_env={"HM_RUNNING_JOBS": "backup-system:4242"})
    assert r.returncode != 0 and "scheduler jobs are running (backup-system:4242)" in r.stderr and "--force" in r.stderr
    assert "Stop and disable" not in r.stdout, "refusal must come before anything is stopped"


def test_uninstall_force_goes_on_past_a_running_job_but_says_it_is_not_stopped(tmp_path):
    r, _ = _uninstall_live_dry(tmp_path, {}, "--dry-run", "--force", extra_env={"HM_RUNNING_JOBS": "backup-system:4242\nstack-backup:77"})
    assert r.returncode == 0, r.stderr
    assert "backup-system:4242 stack-backup:77" in r.stderr and "not stopped" in r.stderr and "Stop and disable" in r.stdout


def test_uninstall_refuses_while_a_cutover_has_retired_legacy_items(tmp_path):
    """Removing the umbrella would leave a retired backup timer retired and nothing running the backup."""
    r, _ = _uninstall_live_dry(tmp_path, {}, "--dry-run", extra_env={"HM_RETIRED_UNITS": "backup-system.timer",
                                                                        "HM_RETIRED_PATHS": "/etc/systemd/system/x.service.d/10-gate.conf"})
    assert r.returncode != 0 and "backup-system.timer" in r.stderr and "10-gate.conf" in r.stderr
    assert "migrate rollback" in r.stderr and "--force" in r.stderr and "Stop and disable" not in r.stdout
    (tmp_path / "again").mkdir()
    forced, _ = _uninstall_live_dry(tmp_path / "again", {}, "--dry-run", "--force", extra_env={"HM_RETIRED_UNITS": "backup-system.timer"})
    assert forced.returncode == 0 and "nothing will run them" in forced.stderr


@needs_userns
def test_staged_uninstall_reads_the_schedulers_state_for_running_jobs(env):
    assert env.install().returncode == 0
    sched = env.p("var/lib/homelab-maint/sched.json")
    sched.write_text(json.dumps({"jobs": {"backup-system": {"running": {"pid": os.getpid()}},
                                          "probes-run": {"last_status": "ok"},
                                          "old-job": {"running": {"pid": 2 ** 22 + 12345}}}}))       # alive; no running record; long dead
    r = env.uninstall()
    assert r.returncode != 0 and f"backup-system:{os.getpid()}" in r.stderr and "old-job" not in r.stderr
    assert env.p("usr/local/sbin/homelab-maint").exists(), "nothing was removed"
    sched.write_text("{not json")                                           # unreadable state: not a reason to refuse
    assert env.uninstall().returncode == 0
    assert not env.p("usr/local/sbin/homelab-maint").exists()


@needs_userns
def test_staged_uninstall_refuses_over_retired_items_unless_forced(env):
    assert env.install().returncode == 0
    r = env.uninstall(extra_env={"HM_RETIRED_UNITS": "backup-immich.timer"})
    assert r.returncode != 0 and "backup-immich.timer" in r.stderr and env.p("usr/local/sbin/homelab-maint").exists()
    assert env.uninstall("--force", extra_env={"HM_RETIRED_UNITS": "backup-immich.timer"}).returncode == 0


# --------------------------------------------------------------------------- regression: import smoke test at install
# compileall only catches syntax errors. A module-level NameError/ImportError compiles fine, then breaks every
# tier run, and breaks `homelab-maint gate` too, whose crash exit code 1 makes systemd skip the Immich recycle
# silently. install.sh therefore imports the staged package (cli + every task module, as cli.load_tasks does)
# before the live tree is touched.
BREAKAGES = {
    "task-name-error": ("homelab_maint/tasks/one.py", "VALUE = 1\nraise NameError('introduced by a later edit')\n", "NameError"),
    "cli-import-error": ("homelab_maint/cli.py", "import module_that_does_not_exist_zz\n", "ModuleNotFoundError"),
}


@needs_userns
@pytest.mark.parametrize("breakage", BREAKAGES)
def test_runtime_import_error_aborts_before_touching_the_live_install(env, breakage):
    rel, body, exc = BREAKAGES[breakage]
    assert env.install().returncode == 0
    lib = env.p("usr/local/lib/homelab-maint")
    good = tree_state(lib)
    (env.proj / rel).write_text(body)
    compiled = subprocess.run([sys.executable, "-m", "py_compile", str(env.proj / rel)], capture_output=True)
    assert compiled.returncode == 0, "the breakage must compile, otherwise this tests compileall, not the import check"
    r = env.install()
    assert r.returncode != 0
    assert "does not import" in r.stderr and exc in r.stderr and "nothing was installed" in r.stderr
    assert tree_state(lib) == good, "live package changed despite the import failure"
    assert not list((env.tmp / "tmpdir").iterdir()), "stage dir leaked"


@pytest.mark.parametrize("breakage", BREAKAGES)
def test_import_error_is_reported_by_dry_run_without_writing(env, breakage):
    rel, body, exc = BREAKAGES[breakage]
    (env.proj / rel).write_text(body)
    r = env._run("install.sh", "--dry-run", userns=False)
    assert r.returncode != 0 and "does not import" in r.stderr and exc in r.stderr
    assert list(env.root.rglob("*")) == []


@needs_userns
def test_import_check_imports_the_staged_tree_not_a_stale_one_on_the_path(env, tmp_path):
    """A homelab_maint on PYTHONPATH or in the cwd must not satisfy the check."""
    shadow = tmp_path / "shadow" / "homelab_maint"
    (shadow / "tasks").mkdir(parents=True)
    for rel in ("__init__.py", "cli.py", "tasks/__init__.py"):
        (shadow / rel).write_text("VALUE = 0\n")
    (env.proj / "homelab_maint" / "tasks" / "one.py").write_text("raise NameError('only in the real tree')\n")
    e = {**os.environ, "HM_ROOT": str(env.root), "TMPDIR": str(env.tmp / "tmpdir"), "PYTHONPATH": str(tmp_path / "shadow")}
    r = subprocess.run(["unshare", "-r", "bash", str(env.proj / "install.sh")], capture_output=True, text=True,
                       env=e, cwd=tmp_path / "shadow", timeout=180)
    assert r.returncode != 0 and "NameError" in r.stderr


# --------------------------------------------------------------------------- the deploy branch, for real, in a staged install with a stub docker
STUB_DOCKER_DEPLOY = """#!/bin/sh
# compose version / compose ... up: succeed and are logged; inspect: the health status in $STUB_HEALTH. Anything else is refused.
echo "$*" >> "$STUB_LOG_DOCKER"
case "$1" in
  compose) exit 0 ;;
  inspect) cat "$STUB_HEALTH" ;;
  *) exit 9 ;;
esac
"""


def _staged_deploy(env: Env, health: str, *args: str):
    """install.sh --deploy-web on the staging root, with the 'staging skips docker' guard cut out of a COPY of the script (the project copy) and a stub
    docker first on PATH: every other line is the shipped one, so what runs after `up -d --build` (the health wait, the web_ready marker) is the
    real code. The real docker is never reached: the stub is on PATH for every call this helper makes, and it is the only way the copy is run."""
    web = env.proj / "web"
    web.mkdir(exist_ok=True)
    (web / "docker-compose.yml").write_text("services: {}\n")
    (web / "docker-compose.override.yml").write_text("networks: {}\n")
    script = env.proj / "install.sh"
    guard = '  if [[ -n $ROOT ]]; then\n    note skipped "docker compose (staging root)"\n  elif'
    src = script.read_text()
    if guard in src:
        script.write_text(src.replace(guard, '  if false; then\n    note skipped "docker compose (staging root)"\n  elif', 1))
    bindir = env.tmp / "dbin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(STUB_DOCKER_DEPLOY)
    (bindir / "docker").chmod(0o755)
    (env.tmp / "health").write_text(health + "\n")
    return env._run("install.sh", "--deploy-web", *args, extra_env={"PATH": f"{bindir}:{os.environ['PATH']}", "STUB_HEALTH": str(env.tmp / "health"),
                                                                   "STUB_LOG_DOCKER": str(env.tmp / "docker.log"), "HM_WEB_WAIT_S": "2"})


@needs_userns
def test_a_deployed_healthy_site_gets_web_ready_and_an_unhealthy_one_does_not(env):
    r = _staged_deploy(env, "healthy")
    assert r.returncode == 0, r.stdout + r.stderr
    flag = env.p(f"{ACK}/web_ready")
    assert flag.is_file() and flag.stat().st_mode & 0o777 == 0o644 and flag.read_bytes() == b""
    assert re.search(r"create\s+\S+/ack/web_ready \(the e-mail Acknowledge button is on\)", r.stdout)
    calls = (env.tmp / "docker.log").read_text().splitlines()
    assert any(c.startswith("compose") and "up -d --build" in c for c in calls) and any(c.startswith("inspect") and c.endswith("maintenance-web") for c in calls)
    flag.unlink()
    r = _staged_deploy(env, "unhealthy")
    assert r.returncode == 0 and not flag.exists(), r.stdout
    assert "maintenance-web did not report healthy: ack/web_ready was NOT created" in r.stderr and "sudo touch /var/lib/homelab-maint/ack/web_ready" in r.stderr
    r = _staged_deploy(env, "starting")                                                      # never healthy inside HM_WEB_WAIT_S
    assert r.returncode == 0 and not flag.exists() and "did not report healthy" in r.stderr
    r = _staged_deploy(env, "healthy", "--no-web-ready")                                     # healthy, but the owner asked to wait
    assert r.returncode == 0 and not flag.exists() and re.search(r"skipped\s+ack/web_ready \(--no-web-ready\)", r.stdout)
    assert _staged_deploy(env, "healthy").returncode == 0 and flag.is_file()
    again = _staged_deploy(env, "healthy")                                                   # the marker is kept, not rewritten
    assert again.returncode == 0 and re.search(r"same\s+\S+/ack/web_ready", again.stdout)
