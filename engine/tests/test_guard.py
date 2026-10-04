"""Tests for tasks/guard.py and tasks/gates.py: fake /proc and cgroup v2 trees under tmp_path, mocked commands."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from homelab_maint import core
from homelab_maint.core import GIB, Ctx
from homelab_maint.tasks import gates, guard

MIB = 1024 ** 2
REAL_PROTECTED = core.load_toml(Path(__file__).resolve().parent.parent / "etc" / "protected.toml")

BUSY_CFG = {
    "comfyui_queue_url": "http://127.0.0.1:8188/queue",
    "ollama_ps_url": "http://127.0.0.1:11434/api/ps",
    "backup_units": ["backup-system.service", "backup-immich.service"],
    "build_process_patterns": ["docker build", "buildctl", "buildkitd.*--", "GradleDaemon",
                               "Gradle Test Executor", "kotlin-compiler"],
    "plex_process_patterns": ["Plex Media Scanner", "Plex Transcoder", "Plex Script Host"],
    "immich_containers": ["immich_server", "immich_machine_learning"],
    "immich_cpu_busy_pct": 15,
    "max_defer_hours": {"immich-recycle": 12},
}


def cp(cmd, rc=0, out="", err=""):
    return subprocess.CompletedProcess(cmd, rc, out, err)


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def cid(n: int) -> str:
    return f"{n:064x}"


# --------------------------------------------------------------------------- fake /proc, cgroup, commands
def mk_proc(root: Path, pid: int, comm: str, ppid: int = 1, state: str = "S", ticks: int = 0, start: int = 100,
            argv=None, anon_kb=None) -> None:
    d = root / str(pid)
    write(d / "stat", f"{pid} ({comm}) {state} {ppid} {pid} {pid} 0 -1 4194304 0 0 0 0 {ticks} 0 0 0 20 0 1 0 "
                      f"{start} 1000 100")
    write(d / "cmdline", ("\0".join(argv) + "\0") if argv else "")
    write(d / "status", f"Name:\t{comm}\nState:\t{state}\nPid:\t{pid}\nPPid:\t{ppid}\n"
          + (f"RssAnon:\t{anon_kb} kB\n" if anon_kb is not None else ""))


def mk_cg(root: Path, cid_: str, anon=0, file_=0, swap=0, cur=None, peak=0, cpu_us=0, io=((259, 0, 0, 0),),
          oom=0, pf=0.0, pids=()) -> Path:
    d = root / "system.slice" / f"docker-{cid_}.scope"
    write(d / "memory.current", f"{anon + file_ if cur is None else cur}\n")
    write(d / "memory.peak", f"{peak}\n")
    write(d / "memory.swap.current", f"{swap}\n")
    write(d / "memory.stat", f"anon {anon}\nfile {file_}\nkernel 0\n")
    write(d / "memory.events", f"low 0\nhigh 0\nmax 0\noom 0\noom_kill {oom}\noom_group_kill 0\n")
    write(d / "cpu.stat", f"usage_usec {cpu_us}\nuser_usec 0\nsystem_usec 0\n")
    write(d / "io.stat", "".join(f"{a}:{b} rbytes={r} wbytes={w} rios=1 wios=1 dbytes=0 dios=0\n"
                                 for a, b, r, w in io))
    write(d / "memory.pressure", f"some avg10=0.00 avg60=0.00 avg300=0.00 total=1\n"
                                 f"full avg10=0.00 avg60={pf:.2f} avg300=0.00 total=1\n")
    write(d / "cgroup.procs", "".join(f"{p}\n" for p in pids))
    return d


def mk_host(root: Path, avail_gib=60, swap_used_gib=1, psi_full=0.0, pswpin=0, oom_kill=0) -> None:
    write(root / "meminfo", f"MemTotal: 98608356 kB\nMemAvailable: {int(avail_gib * 1024 * 1024)} kB\n"
                            f"SwapTotal: 33554428 kB\nSwapFree: {33554428 - int(swap_used_gib * 1024 * 1024)} kB\n")
    write(root / "vmstat", f"pswpin {pswpin}\npswpout 5\noom_kill {oom_kill}\n")
    for k in ("memory", "io", "cpu"):
        f = psi_full if k == "memory" else 0.0
        write(root / "pressure" / k, f"some avg10=0.00 avg60={f:.2f} avg300=0.00 total=1\n"
                                     f"full avg10=0.00 avg60={f:.2f} avg300=0.00 total=1\n")
    write(root / "uptime", "1000000.00 2000000.00\n")


class FakeSh:
    """Replacement for core.sh: first matching handler wins; unmocked commands behave like a missing binary."""

    def __init__(self):
        self.calls: list = []
        self.handlers: list = []

    def on(self, prefix, rc=0, out="", err=""):
        pre = list(prefix)
        self.handlers.insert(0, (lambda c, pre=pre: list(c[:len(pre)]) == pre,
                                 out if callable(out) else (lambda c, rc=rc, out=out, err=err: cp(c, rc, out, err))))
        return self

    def __call__(self, cmd, timeout=60, **kw):
        self.calls.append(list(cmd) if isinstance(cmd, list) else cmd)
        for pred, fn in self.handlers:
            if pred(cmd):
                r = fn(cmd)
                return r if isinstance(r, subprocess.CompletedProcess) else cp(cmd, 0, r)
        return cp(cmd, 127, "", "not mocked")

    def ran(self, *prefix) -> list:
        return [c for c in self.calls if list(c[:len(prefix)]) == list(prefix)]


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.hook = lambda: None

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s
        self.hook()


class Env:
    pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env()
    e.root, e.proc, e.cg = tmp_path, tmp_path / "proc", tmp_path / "cgroup"
    e.proc.mkdir()
    e.cg.mkdir()
    e.state, e.log = tmp_path / "state", tmp_path / "log"
    e.state.mkdir()
    e.sh, e.clock = FakeSh(), Clock()
    for mod in (core, gates, guard):
        monkeypatch.setattr(mod, "STATE_DIR", e.state, raising=False)
    monkeypatch.setattr(core, "LOG_DIR", e.log)
    for mod in (core, gates, guard):
        monkeypatch.setattr(mod, "sh", e.sh)
    monkeypatch.setattr(gates, "PROC", e.proc)
    monkeypatch.setattr(gates, "CGROUP", e.cg)
    monkeypatch.setattr(gates, "sleep", e.clock.sleep)
    monkeypatch.setattr(gates, "_clock", e.clock.monotonic)

    def no_http(url, timeout=3.0):
        raise OSError("connection refused")
    monkeypatch.setattr(gates, "http_json", no_http)
    e.mp = monkeypatch
    mk_host(e.proc)
    return e


def cfg_of(tasks=None, busy=None, patterns=("postgres", "immich", "comfyui")) -> dict:
    return {"global": {}, "caps": {}, "tasks": tasks or {},
            "protected": {"patterns": list(patterns), "busy": dict(BUSY_CFG, **(busy or {}))}}


def systemctl(env, **states):
    """Mock `systemctl is-active u1 u2...` from {unit: state}; unknown units are 'inactive'."""
    def fn(cmd):
        return cp(cmd, 0 if any(states.get(u) == "active" for u in cmd[2:]) else 3,
                  "\n".join(states.get(u, "inactive") for u in cmd[2:]) + "\n")
    env.sh.on(["systemctl", "is-active"], out=fn)


# =========================================================================== gates: comfyui
def comfy(env, state_out, http=None, rc=0):
    env.sh.on(["docker", "ps", "-a", "--filter"], rc=rc, out=state_out)
    if http is not None:
        env.mp.setattr(gates, "http_json", http)


def test_comfyui_stopped_container_is_idle(env):
    comfy(env, "exited\n")
    assert gates.busy("comfyui", cfg_of()) == (False, "ComfyUI container exited (idle)")


def test_comfyui_missing_container_is_idle(env):
    comfy(env, "")
    assert gates.busy("comfyui", cfg_of())[0] is False


def test_comfyui_docker_failure_is_busy(env):
    comfy(env, "", rc=1)
    assert gates.busy("comfyui", cfg_of())[0] is True


def test_comfyui_running_probe_failure_is_busy_not_idle(env):
    comfy(env, "running\n")          # default http stub raises: connection refused
    busy, why = gates.busy("comfyui", cfg_of())
    assert busy and "probe failed" in why


def test_comfyui_running_empty_queue_is_idle(env):
    comfy(env, "running\n", lambda u, timeout=3.0: {"queue_running": [], "queue_pending": []})
    assert gates.busy("comfyui", cfg_of()) == (False, "ComfyUI queue empty")


@pytest.mark.parametrize("q", [{"queue_running": [[0, "x"]], "queue_pending": []},
                               {"queue_running": [], "queue_pending": [[1, "y"], [2, "z"]]}])
def test_comfyui_queue_activity_is_busy(env, q):
    comfy(env, "running\n", lambda u, timeout=3.0: q)
    busy, why = gates.busy("comfyui", cfg_of())
    assert busy and "ComfyUI queue" in why


@pytest.mark.parametrize("payload", [{}, [], "oops", {"queue_running": None, "queue_pending": []}])
def test_comfyui_unparsable_payload_is_busy(env, payload):
    comfy(env, "running\n", lambda u, timeout=3.0: payload)
    assert gates.busy("comfyui", cfg_of())[0] is True


def test_comfyui_restarting_container_is_busy(env):
    comfy(env, "restarting\n")
    assert gates.busy("comfyui", cfg_of())[0] is True


# =========================================================================== gates: ollama
def test_ollama_inactive_is_idle(env):
    systemctl(env, **{"ollama.service": "inactive"})
    assert gates.busy("ollama", cfg_of()) == (False, "ollama.service inactive (idle)")


def test_ollama_systemctl_missing_is_busy(env):
    assert gates.busy("ollama", cfg_of())[0] is True         # unmocked => rc 127


def test_ollama_active_but_probe_fails_is_busy(env):
    systemctl(env, **{"ollama.service": "active"})
    busy, why = gates.busy("ollama", cfg_of())
    assert busy and "probe failed" in why


def _ollama_cg(env, pct_per_s):
    d = env.cg / "system.slice" / "ollama.service"
    write(d / "cpu.stat", "usage_usec 1000000\n")
    env.clock.hook = lambda: write(d / "cpu.stat", f"usage_usec {1000000 + int(pct_per_s * 1e4 * 2)}\n")


def test_ollama_generating_is_busy_and_loaded_but_idle_is_not(env):
    systemctl(env, **{"ollama.service": "active"})
    env.mp.setattr(gates, "http_json", lambda u, timeout=3.0: {"models": [{"name": "qwen"}]})
    _ollama_cg(env, 80)
    busy, why = gates.busy("ollama", cfg_of())
    assert busy and "80%" in why and "1 model" in why
    _ollama_cg(env, 1)
    assert gates.busy("ollama", cfg_of()) == (False, "Ollama idle, 1 model(s) loaded")


def test_ollama_unreadable_cgroup_is_busy(env):
    systemctl(env, **{"ollama.service": "active"})
    env.mp.setattr(gates, "http_json", lambda u, timeout=3.0: {"models": []})
    assert gates.busy("ollama", cfg_of())[0] is True         # no cgroup dir at all


# =========================================================================== gates: plex / backup / apt
def test_plex_scanner_and_transcoder_are_busy_idle_plugin_host_is_not(env):
    mk_proc(env.proc, 10, "Plex Script Hos", argv=["Plex Plug-in [com.plexapp.system]", "/snap/x"])
    assert gates.busy("plex", cfg_of())[0] is False
    mk_proc(env.proc, 11, "Plex Transcoder", argv=["/snap/plexmediaserver/1/Plex Transcoder", "-i", "a.mkv"])
    busy, why = gates.busy("plex", cfg_of())
    assert busy and "Plex Transcoder" in why


def test_plex_proc_unreadable_is_busy(env):
    env.mp.setattr(gates, "PROC", env.root / "nope")
    assert gates.busy("plex", cfg_of())[0] is True


def test_backup_unit_active_is_busy(env):
    systemctl(env, **{"backup-immich.service": "activating"})
    busy, why = gates.busy("backup", cfg_of())
    assert busy and "backup-immich.service is activating" in why
    systemctl(env)
    assert gates.busy("backup", cfg_of())[0] is False


def test_backup_garbled_systemctl_is_busy(env):
    env.sh.on(["systemctl", "is-active"], out="active\n")      # 1 line for 2 units
    assert gates.busy("backup", cfg_of())[0] is True


def test_apt_processes_and_locks(env):
    mk_proc(env.proc, 20, "unattended-upgr", argv=["/usr/bin/python3", "/usr/share/unattended-upgrades/"
                                                    "unattended-upgrade-shutdown", "--wait-for-signal"])
    write(env.proc / "locks", "")
    lockfile = env.root / "lock-frontend"
    lockfile.write_text("")
    env.mp.setattr(gates, "_APT_LOCKS", [str(lockfile)])
    assert gates.busy("apt", cfg_of())[0] is False             # the shutdown helper is not an upgrade
    mk_proc(env.proc, 21, "apt-get", argv=["apt-get", "upgrade", "-y"])
    assert gates.busy("apt", cfg_of())[0] is True
    os.remove(env.proc / "21" / "stat")
    mk_proc(env.proc, 22, "python3", argv=["python3", "/usr/bin/unattended-upgrade"])
    assert gates.busy("apt", cfg_of())[0] is True
    os.remove(env.proc / "22" / "stat")
    st = os.stat(lockfile)
    write(env.proc / "locks", f"1: POSIX  ADVISORY  WRITE 99 {os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:"
                              f"{st.st_ino} 0 EOF\n")
    busy, why = gates.busy("apt", cfg_of())
    assert busy and "locked" in why


# =========================================================================== gates: docker_build / gradle
def test_idle_buildkitd_daemon_is_not_a_build(env):
    """The buildx builder's buildkitd runs forever with `--allow-...` flags and matches buildkitd.*-- in the config."""
    mk_proc(env.proc, 30, "docker-init", argv=["/sbin/docker-init", "--", "/usr/bin/buildkitd-entrypoint", "--allow-x"])
    mk_proc(env.proc, 31, "buildkitd", ppid=30, ticks=500, argv=["/usr/bin/buildkitd", "--allow-x"])
    assert gates.busy("docker_build", cfg_of()) == (False, "no docker build running")


@pytest.mark.parametrize("argv", [["docker", "build", "-t", "x", "."],
                                  ["/usr/libexec/docker/cli-plugins/docker-buildx", "buildx", "build", "."],
                                  ["/usr/libexec/docker/cli-plugins/docker-compose", "compose", "build"],
                                  ["docker-compose", "compose", "up", "-d", "--build"],
                                  ["buildctl", "build"]])
def test_docker_build_client_processes_are_busy(env, argv):
    mk_proc(env.proc, 40, "docker", argv=argv)
    assert gates.busy("docker_build", cfg_of())[0] is True


def test_docker_buildx_ls_is_not_a_build(env):
    mk_proc(env.proc, 41, "docker", argv=["docker", "buildx", "ls"])
    assert gates.busy("docker_build", cfg_of())[0] is False


def test_buildkitd_with_child_step_or_cpu_is_busy(env):
    mk_proc(env.proc, 31, "buildkitd", ppid=1, argv=["/usr/bin/buildkitd", "--x"])
    mk_proc(env.proc, 32, "runc", ppid=31, argv=["runc", "run"])
    assert gates.busy("docker_build", cfg_of())[0] is True
    os.remove(env.proc / "32" / "stat")
    assert gates.busy("docker_build", cfg_of())[0] is False
    env.clock.hook = lambda: mk_proc(env.proc, 31, "buildkitd", ppid=1, ticks=100, argv=["/usr/bin/buildkitd", "--x"])
    busy, why = gates.busy("docker_build", cfg_of())            # 100 ticks (1 s) in a 2 s window = 50 %
    assert busy and "buildkitd using" in why


def test_gradle_idle_daemons_are_not_busy_workers_and_cpu_are(env):
    daemon = ["java", "-cp", "gradle.jar", "org.gradle.launcher.daemon.bootstrap.GradleDaemon", "8.5"]
    mk_proc(env.proc, 50, "java", argv=daemon, ticks=10)
    busy, why = gates.busy("gradle", cfg_of())
    assert (busy, "1 idle daemon" in why) == (False, True)
    env.clock.hook = lambda: mk_proc(env.proc, 50, "java", argv=daemon, ticks=10 + 200)
    assert gates.busy("gradle", cfg_of())[0] is True            # daemon burning 100 % of a core
    env.clock.hook = lambda: None
    mk_proc(env.proc, 50, "java", argv=daemon, ticks=10)
    mk_proc(env.proc, 51, "java", ppid=50, argv=["java", "worker.org.gradle.process.internal.worker.GradleWorkerMain",
                                                  "Gradle Test Executor 3"])
    busy, why = gates.busy("gradle", cfg_of())
    assert busy and "worker" in why


# =========================================================================== gates: immich
def immich_env(env, pct, with_db=True):
    ids = {"immich_server": cid(1), "immich_machine_learning": cid(2)}
    if with_db:
        ids["immich_postgres"] = cid(3)
    env.sh.on(["docker", "ps", "--no-trunc"], out="".join(f"{v} {k}\n" for k, v in ids.items()))
    dirs = {k: mk_cg(env.cg, v, cpu_us=5_000_000) for k, v in ids.items()}

    def tick():
        for k, d in dirs.items():
            write(d / "cpu.stat", f"usage_usec {5_000_000 + int(pct.get(k, 0) * 1e4 * 10)}\n")
    env.clock.hook = tick
    systemctl(env)


def test_immich_cpu_over_limit_is_busy(env):
    immich_env(env, {"immich_machine_learning": 60})
    busy, why = gates.busy("immich-recycle", cfg_of())          # systemd alias
    assert busy and "immich_machine_learning at 60%" in why


def test_immich_postgres_load_counts(env):
    immich_env(env, {"immich_postgres": 40})
    assert gates.busy("immich", cfg_of())[0] is True


def test_immich_idle_below_limit(env):
    immich_env(env, {"immich_server": 10})
    busy, why = gates.busy("immich", cfg_of())
    assert not busy and "idle" in why


def test_immich_backup_unit_is_busy_before_sampling(env):
    immich_env(env, {})
    systemctl(env, **{"backup-system.service": "active"})
    busy, why = gates.busy("immich", cfg_of())
    assert busy and "backup-system" in why and not env.sh.ran("docker")


def test_immich_not_running_is_idle_but_docker_down_is_busy(env):
    systemctl(env)
    env.sh.on(["docker", "ps", "--no-trunc"], out="")
    assert gates.busy("immich", cfg_of())[0] is False
    env.sh.on(["docker", "ps", "--no-trunc"], rc=1)
    assert gates.busy("immich", cfg_of())[0] is True


def test_immich_missing_cgroup_is_busy(env):
    systemctl(env)
    env.sh.on(["docker", "ps", "--no-trunc"], out=f"{cid(1)} immich_server\n")
    busy, why = gates.busy("immich", cfg_of())
    assert busy and "cgroup was not found" in why


def test_cpu_window_unreadable_file_fails_closed(env):
    d = mk_cg(env.cg, cid(9), cpu_us=1)
    env.clock.hook = lambda: os.remove(d / "cpu.stat")
    assert gates.cpu_window({"x": d}, 1) is None


# =========================================================================== gates: any / unknown / errors
def test_any_reports_first_busy_probe_and_all_idle(env):
    env.mp.setattr(gates, "_PROBES", {n: (lambda b: (False, "ok")) for n in gates._PROBES})
    assert gates.busy("any", cfg_of()) == (False, "all gates idle")
    probes = dict(gates._PROBES)
    probes["plex"] = lambda b: (True, "scanning")
    env.mp.setattr(gates, "_PROBES", probes)
    assert gates.busy("any", cfg_of()) == (True, "plex: scanning")


def test_unknown_gate_and_probe_exception_are_busy(env):
    assert gates.busy("nope", cfg_of())[0] is True
    probes = dict(gates._PROBES)
    probes["plex"] = lambda b: 1 / 0
    env.mp.setattr(gates, "_PROBES", probes)
    busy, why = gates.busy("plex", cfg_of())
    assert busy and "ZeroDivisionError" in why
    assert gates.busy("any", cfg_of())[0] is True


def test_real_protected_toml_loads_into_probes(env):
    """The shipped [busy] table must be usable as-is (guards against key typos between toml and code)."""
    b = REAL_PROTECTED["busy"]
    for key in ("comfyui_queue_url", "ollama_ps_url", "backup_units", "build_process_patterns",
                "plex_process_patterns", "immich_containers", "immich_cpu_busy_pct", "max_defer_hours"):
        assert key in b
    assert "immich-recycle" in b["max_defer_hours"] and gates.ALIASES["immich-recycle"] == "immich"


def test_http_json_rejects_non_http_schemes():
    with pytest.raises(ValueError):
        gates.http_json("file:///etc/hostname")


# =========================================================================== gates: cli_gate
@pytest.fixture
def gate_cfg(env):
    cfg = cfg_of()
    env.mp.setattr(gates, "load_config", lambda: cfg)
    return cfg


def _fake_probe(env, result):
    probes = dict(gates._PROBES)
    probes["immich"] = lambda b: result["v"]
    env.mp.setattr(gates, "_PROBES", probes)


def test_cli_gate_idle_proceeds_and_clears_record(env, gate_cfg, capsys):
    res = {"v": (True, "jobs running")}
    _fake_probe(env, res)
    assert gates.cli_gate("immich-recycle") == 1
    assert json.loads((env.state / "gates.json").read_text())["immich-recycle"]["count"] == 1
    res["v"] = (False, "idle")
    assert gates.cli_gate("immich-recycle") == 0
    assert "immich-recycle" not in json.loads((env.state / "gates.json").read_text())


def test_cli_gate_counts_deferrals_and_keeps_first_timestamp(env, gate_cfg):
    _fake_probe(env, {"v": (True, "busy")})
    for _ in range(3):
        assert gates.cli_gate("immich-recycle") == 1
    rec = json.loads((env.state / "gates.json").read_text())["immich-recycle"]
    assert rec["count"] == 3 and rec["since"] <= rec["last"]


def test_cli_gate_proceeds_after_max_defer_hours_and_audits(env, gate_cfg):
    _fake_probe(env, {"v": (True, "jobs running")})
    write(env.state / "gates.json", json.dumps({"immich-recycle": {"since": time.time() - 13 * 3600, "count": 6,
                                                                    "last": time.time() - 7200, "reason": "x"}}))
    assert gates.cli_gate("immich-recycle") == 0
    assert "immich-recycle" not in json.loads((env.state / "gates.json").read_text())   # clock restarts
    audit = [json.loads(ln) for ln in (env.log / "audit.jsonl").read_text().splitlines()]
    assert audit[-1]["action"] == "defer-limit" and audit[-1]["target"] == "immich-recycle"


def test_cli_gate_within_limit_still_skips(env, gate_cfg):
    _fake_probe(env, {"v": (True, "jobs running")})
    write(env.state / "gates.json", json.dumps({"immich-recycle": {"since": time.time() - 11 * 3600, "count": 5}}))
    assert gates.cli_gate("immich-recycle") == 1


def test_cli_gate_without_configured_limit_defers_forever(env, gate_cfg):
    probes = dict(gates._PROBES)
    probes["plex"] = lambda b: (True, "transcoding")
    env.mp.setattr(gates, "_PROBES", probes)
    write(env.state / "gates.json", json.dumps({"plex": {"since": time.time() - 900 * 3600, "count": 99}}))
    assert gates.cli_gate("plex") == 1


def test_cli_gate_unknown_name_skips_without_state(env, gate_cfg, capsys):
    assert gates.cli_gate("typo") == 1
    assert not (env.state / "gates.json").exists()
    assert "unknown gate" in capsys.readouterr().err


def test_cli_gate_unwritable_state_fails_closed(env, gate_cfg):
    _fake_probe(env, {"v": (True, "busy")})
    env.mp.setattr(gates, "STATE_DIR", env.root / "state" / "gates.json" / "x")   # parent is a path under a file
    write(env.state / "gates.json", "{}")
    assert gates.cli_gate("immich-recycle") == 1


# =========================================================================== spike_sampler
def history(env):
    """Records the sampler wrote (STATE_DIR/samples.jsonl)."""
    p = env.state / "samples.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


def add_sample(rec):
    guard.append_sample(rec, 14, rec["t"])


def test_spike_sampler_records_compact_sample(env):
    mk_host(env.proc, avail_gib=70, swap_used_gib=2, psi_full=1.5, pswpin=123, oom_kill=4)
    mk_cg(env.cg, cid(1), anon=3 * GIB, file_=GIB, swap=GIB, peak=5 * GIB, cpu_us=777, oom=2, pf=1.2, pids=[100, 101],
          io=((259, 0, 100, 50), (8, 16, 1000, 500)))
    mk_cg(env.cg, cid(2), anon=GIB, cpu_us=5)
    env.sh.on(["docker", "ps", "--no-trunc"], out=f"{cid(1)} alpha\n{cid(2)} beta\n")
    env.sh.on(["nvidia-smi"], out="6, 1312\n")
    for pid, comm, kb in ((100, "alphad", 3_000_000), (101, "alphaw", 1_000_000), (200, "chrome", 900_000),
                          (201, "python3", 500_000), (202, "kthreadd", None)):
        mk_proc(env.proc, pid, comm, anon_kb=kb)
    now = time.time()
    r = guard.spike_sampler(Ctx(cfg_of({"spike_sampler": {"keep_days": 14}}), "spike_sampler", False, now))
    (rec,) = history(env)
    assert rec["kind"] == "sample" and rec["t"] == now
    a = rec["c"]["alpha"]
    assert a == {"anon": 3 * GIB, "file": GIB, "swap": GIB, "cur": 4 * GIB, "peak": 5 * GIB, "cpu_us": 777,
                 "io_b": 1650, "oom": 2, "pressure_full60": 1.2}
    assert rec["c"]["beta"] == {"anon": GIB, "file": 0, "cur": GIB, "peak": 0, "cpu_us": 5, "io_b": 0}
    assert rec["host"]["mem_avail"] == 70 * GIB and rec["host"]["swap_used"] == 2 * GIB
    assert rec["host"]["psi_mem_full60"] == 1.5 and rec["host"]["pswpin"] == 123 and rec["host"]["gpu"] == [6, 1312]
    # container pids (100, 101) are excluded, kernel thread has no RssAnon, order is by anon desc
    assert [p[:2] for p in rec["p"]] == [["chrome", 200], ["python3", 201]]
    assert r.status == "ok" and r.metrics["containers"] == 2 and r.metrics["anon_total_gib"] == 4.0
    assert [x["name"] for x in r.metrics["largest"]] == ["alpha", "beta"] and len(r.summary) <= 140


def test_spike_sampler_top_processes_capped_at_eight(env):
    env.sh.on(["docker", "ps", "--no-trunc"], out="")
    for i in range(20):
        mk_proc(env.proc, 300 + i, f"p{i}", anon_kb=1000 + i)
    guard.spike_sampler(Ctx(cfg_of(), "spike_sampler", False, time.time()))
    p = history(env)[0]["p"]
    assert len(p) == 8 and p[0][2] == 1019


def test_spike_sampler_docker_failure_records_host_only_and_warns(env):
    env.sh.on(["docker", "ps", "--no-trunc"], rc=1, err="Cannot connect")
    r = guard.spike_sampler(Ctx(cfg_of(), "spike_sampler", False, time.time()))
    (rec,) = history(env)
    assert rec["c"] == {} and rec["err"] and r.status == "warn"


def test_spike_sampler_skips_container_whose_cgroup_vanished(env):
    mk_cg(env.cg, cid(1), anon=GIB)
    env.sh.on(["docker", "ps", "--no-trunc"], out=f"{cid(1)} here\n{cid(2)} gone\n")
    r = guard.spike_sampler(Ctx(cfg_of(), "spike_sampler", False, time.time()))
    assert list(history(env)[0]["c"]) == ["here"] and r.metrics["skipped"] == 1
    assert r.status == "warn" and "1 of 2 cgroups unreadable" in r.summary       # 50 % is not "a few vanished"


def test_spike_sampler_no_nvidia_smi_is_fine(env):
    env.sh.on(["docker", "ps", "--no-trunc"], out="")
    r = guard.spike_sampler(Ctx(cfg_of(), "spike_sampler", False, time.time()))
    assert history(env)[0]["host"]["gpu"] is None and r.metrics["gpu_util"] is None


# =========================================================================== stuck_detector
STEP = 900


def put_samples(env, series, host=None, n=None, now=None):
    """series: {name: fn(i) -> (anon, swap, cpu_us, io_b)}; n samples STEP apart, newest at `now`."""
    now = now or time.time()
    n = n or 6
    for i in range(n):
        c = {}
        for name, fn in series.items():
            anon, swap, cpu, io = fn(i)
            c[name] = {"anon": anon, "file": 0, "swap": swap, "cur": anon, "peak": anon, "cpu_us": cpu, "io_b": io}
        h = dict(host or {})
        h.setdefault("pswpin", 0)
        add_sample({"t": now - (n - 1 - i) * STEP, "kind": "sample", "host": h, "c": c, "p": []})
    return now


def mock_docker_ps(env, images=None, rc=0):
    """`docker ps` as stuck_detector's protection lookup sees it: every container of the newest sample is running,
    with image `images[name]` (default example/app:1). rc != 0 simulates docker being down."""
    last = guard.read_samples(10 ** 9)
    names = list(last[-1].get("c", {})) if last else []
    out = "".join(f"{cid(i + 1)}|{n}|{(images or {}).get(n, 'example/app:1')}|{n}\n" for i, n in enumerate(names)
                  if (images or {}).get(n, "x") is not None)          # images={name: None}: no longer running
    env.sh.on(["docker", "ps", "--no-trunc"], rc=rc, out=out)


def run_stuck(env, tasks=None, now=None, apply=False, state=None, images=None, docker_rc=0, **cfgkw):
    mock_docker_ps(env, images, docker_rc)
    cfg = cfg_of({"stuck_detector": {"min_samples": 6, "min_anon_gib": 4, "growth_gib_per_hour": 0.5,
                                     **(tasks or {})}}, **cfgkw)
    if apply:
        cfg["tasks"]["stuck_detector"]["mode"] = "apply"
    ctx = Ctx(cfg, "stuck_detector", apply, now or time.time())
    if state:
        ctx.state.update(state)
    return ctx, guard.stuck_detector(ctx)


def flat(gib, cpu=0, io=0):
    return lambda i: (int(gib * GIB), 0, 1_000_000 + cpu * i, 50 * MIB + io * i)


def test_stuck_warming_up_with_too_few_samples(env):
    put_samples(env, {"a": flat(8)}, n=3)
    _, r = run_stuck(env)
    assert r.status == "info" and "3/6" in r.summary and r.alert is False


def test_stuck_ignores_samples_that_are_too_close_together(env):
    for i in range(6):
        add_sample({"t": time.time() - 6 + i, "kind": "sample", "host": {}, "c": {"a": {
            "anon": 8 * GIB, "cpu_us": 1, "io_b": 1}}})
    _, r = run_stuck(env)
    assert r.status == "info" and "span only" in r.summary


def test_stuck_idle_but_holding_is_a_warn(env):
    put_samples(env, {"stuck-svc": flat(9)})
    _, r = run_stuck(env)
    assert r.status == "warn" and r.alert is True and r.metrics["actionable"] == 1
    (it,) = r.items
    assert it["name"] == "stuck-svc" and it["reason"].startswith("idle but holding") and it["protected"] is False
    assert "stuck-svc" in r.summary and len(r.summary) <= 140


def test_stuck_leak_runaway_when_growing_without_progress(env):
    put_samples(env, {"leaky": lambda i: (int((5 + i * 0.25) * GIB), 0, 1_000_000, 50 * MIB)})   # +1 GiB/h
    _, r = run_stuck(env)
    assert r.status == "warn" and r.items[0]["reason"].startswith("leak/runaway")
    assert r.items[0]["growth_gib_h"] == pytest.approx(1.0, abs=0.05)


def test_stuck_swap_counts_towards_footprint(env):
    put_samples(env, {"swapped": lambda i: (2 * GIB, 3 * GIB, 1_000_000, 50 * MIB)})
    _, r = run_stuck(env)
    assert r.status == "warn" and r.items[0]["swap_h"] == "3.0 GiB"


def test_stuck_not_candidates_small_busy_shrinking_or_restarted(env):
    put_samples(env, {
        "small": flat(1),                                                      # below min_anon_gib
        "busy-cpu": flat(9, cpu=10_000_000),                                   # 11 s CPU per 15 min = ~1.1 %
        "busy-io": flat(9, io=5 * MIB * 15),                                   # 5 MiB/min
        "shrinking": lambda i: (int((9 - i * 0.5) * GIB), 0, 1_000_000, 50 * MIB),
        "restarted": lambda i: (9 * GIB, 0, 9_000_000 - i * 1_000_000, 50 * MIB),   # cpu counter went backwards
    })
    _, r = run_stuck(env)
    assert r.status == "ok" and r.items == [] and r.metrics["candidates"] == 0


def test_stuck_container_started_mid_window_is_ignored(env):
    now = time.time()
    for i in range(6):
        c = {"old": {"anon": 9 * GIB, "cpu_us": 1, "io_b": 1}}
        if i >= 3:
            c["new"] = {"anon": 9 * GIB, "cpu_us": 1, "io_b": 1}
        add_sample({"t": now - (5 - i) * STEP, "kind": "sample", "host": {}, "c": c})
    _, r = run_stuck(env, now=now)
    assert [i["name"] for i in r.items] == ["old"]


def test_stuck_protected_candidates_are_reported_but_only_info(env):
    put_samples(env, {"immich_postgres": flat(9)})
    _, r = run_stuck(env)
    assert r.status == "info" and r.alert is False and r.items[0]["protected"] is True and r.metrics["actionable"] == 0


def test_stuck_unprotected_sorted_before_protected(env):
    put_samples(env, {"immich_postgres": flat(20), "plain": flat(5)})
    _, r = run_stuck(env)
    assert [i["name"] for i in r.items] == ["plain", "immich_postgres"] and r.status == "warn"


def test_stuck_app_busy_via_gate_downgrades_to_info(env):
    put_samples(env, {"comfyui-worker": flat(12)})
    calls = []
    env.mp.setattr(gates, "busy", lambda g, cfg=None: (calls.append(g), (True, "ComfyUI queue: 1 running"))[1])
    _, r = run_stuck(env, patterns=())
    assert calls == ["comfyui"] and r.status == "info" and r.items[0]["busy"] == "ComfyUI queue: 1 running"


def test_stuck_unknown_app_has_no_gate_lookup(env):
    put_samples(env, {"plain": flat(12)})
    env.mp.setattr(gates, "busy", lambda g, cfg=None: pytest.fail("no gate for this container"))
    _, r = run_stuck(env)
    assert r.status == "warn"


def test_stuck_pressure_levels_from_memory_health_thresholds(env):
    mh = {"memory_health": {"psi_mem_full_warn": 5.0, "psi_mem_full_crit": 15.0, "mem_available_warn_gib": 10,
                            "mem_available_crit_gib": 4, "swap_in_pages_per_s_warn": 2000}}
    put_samples(env, {"a": flat(1)}, host={"psi_mem_full60": 0.1, "mem_avail": 50 * GIB})
    assert run_stuck(env, tasks=None)[1].metrics["pressure"] == "none"
    for sub, host, want in (("w", {"psi_mem_full60": 7.0, "mem_avail": 50 * GIB}, "warn"),
                            ("c", {"psi_mem_full60": 20.0, "mem_avail": 50 * GIB}, "crit"),
                            ("l", {"psi_mem_full60": 0.0, "mem_avail": 3 * GIB}, "crit"),
                            ("m", {"psi_mem_full60": 0.0, "mem_avail": 8 * GIB}, "warn")):
        env.mp.setattr(guard, "STATE_DIR", env.root / sub)
        put_samples(env, {"a": flat(1)}, host=host)
        cfg = cfg_of({"stuck_detector": {"min_samples": 6}, **mh})
        r = guard.stuck_detector(Ctx(cfg, "stuck_detector", False, time.time()))
        assert r.metrics["pressure"] == want, sub
        assert r.status == "info" and "mem pressure" in r.summary          # pressure alone never pages


def test_stuck_pressure_from_sustained_swap_in(env):
    now = time.time()
    for i in range(6):
        add_sample({"t": now - (5 - i) * STEP, "kind": "sample", "c": {"a": {"anon": 1, "cpu_us": 1, "io_b": 1}},
                             "host": {"pswpin": i * 3_000_000, "mem_avail": 50 * GIB, "psi_mem_full60": 0.0}})
    cfg = cfg_of({"stuck_detector": {"min_samples": 6}, "memory_health": {"swap_in_pages_per_s_warn": 2000}})
    r = guard.stuck_detector(Ctx(cfg, "stuck_detector", False, now))
    assert r.metrics["pressure"] == "warn" and r.metrics["swap_in_pps"] == pytest.approx(3_000_000 / STEP, abs=5)


def test_stuck_ok_summary_ascii_and_short(env):
    put_samples(env, {"a": flat(1)})
    _, r = run_stuck(env)
    assert r.status == "ok" and r.summary.isascii() and len(r.summary) <= 140


# ---- the dormant restart path ------------------------------------------------------------------------------
def crit_host():
    return {"psi_mem_full60": 30.0, "mem_avail": 2 * GIB}


def restart_env(env, **series):
    put_samples(env, series or {"hog": flat(9)}, host=crit_host())
    mock_docker_ps(env)
    env.sh.on(["docker", "restart"], out="hog\n")
    env.sh.on(["logger"], out="")


def restarts(env):
    return env.sh.ran("docker", "restart")


def test_enforce_off_never_restarts_even_when_everything_else_lines_up(env):
    restart_env(env)
    run_stuck(env, tasks={"enforce": False}, apply=True)
    run_stuck(env, tasks={}, apply=True)                    # key absent
    assert restarts(env) == []


def test_enforce_true_but_c0_runner_never_applies(env):
    """core.run_task forces ctx.apply=False for C0 tasks, so flipping `enforce` alone cannot cause a restart."""
    restart_env(env)
    cfg = cfg_of({"stuck_detector": {"min_samples": 6, "enforce": True, "mode": "apply"}})
    res, _ = core.run_task(core.REGISTRY["stuck_detector"], cfg, apply=True)
    assert res.status in ("warn", "info") and restarts(env) == []


def test_enforce_restarts_biggest_actionable_under_critical_pressure(env):
    restart_env(env, hog=flat(9), bigger=flat(12))
    now = time.time()
    ctx, r = run_stuck(env, tasks={"enforce": True}, apply=True, now=now)
    assert restarts(env) == [["docker", "restart", "-t", "60", "bigger"]]       # one per run, largest first
    assert r.metrics["restarted"] == "bigger" and "restarted bigger" in r.summary
    assert ctx.state["restarts"]["bigger"] == [now]
    audit = [json.loads(ln) for ln in (env.log / "audit.jsonl").read_text().splitlines()]
    assert audit[-1]["action"] == "docker restart" and audit[-1]["outcome"] == "done" and audit[-1]["bytes"] == 0


def test_enforce_requires_critical_not_just_warn_pressure(env):
    put_samples(env, {"hog": flat(9)}, host={"psi_mem_full60": 7.0, "mem_avail": 50 * GIB})
    run_stuck(env, tasks={"enforce": True}, apply=True)
    assert restarts(env) == []


def test_enforce_never_restarts_protected_or_busy(env):
    restart_env(env, immich_postgres=flat(20))
    run_stuck(env, tasks={"enforce": True}, apply=True)
    assert restarts(env) == []
    env.mp.setattr(guard, "STATE_DIR", env.root / "busy")
    restart_env(env, **{"comfyui-x": flat(20)})
    env.mp.setattr(gates, "busy", lambda g, cfg=None: (True, "queue"))
    run_stuck(env, tasks={"enforce": True}, apply=True, patterns=())
    assert restarts(env) == []


def test_enforce_without_apply_is_a_pure_no_op(env):
    restart_env(env)
    run_stuck(env, tasks={"enforce": True}, apply=False)
    assert restarts(env) == [] and not (env.log / "audit.jsonl").exists()


def test_restart_backoff_and_six_hour_cap(env):
    now = 1_000_000_000.0
    st = {}
    assert guard._restart_allowed(st, "x", now) == (True, "")
    st = {"restarts": {"x": [now - 600]}}
    ok, why = guard._restart_allowed(st, "x", now)                  # 10 min after the first: wait 30 min
    assert not ok and "30 min" in why
    assert guard._restart_allowed({"restarts": {"x": [now - 1900]}}, "x", now)[0] is True
    ok, why = guard._restart_allowed({"restarts": {"x": [now - 7200, now - 3600]}}, "x", now)
    assert not ok and "6 h" in why                                  # two within 6 h
    # older than 6 h: the cap no longer applies, but 2 restarts in 24 h still mean a 60 min backoff (long past)
    assert guard._restart_allowed({"restarts": {"x": [now - 7 * 3600, now - 6.5 * 3600]}}, "x", now) == (True, "")
    ok, why = guard._restart_allowed({"restarts": {"x": [now - 7 * 3600, now - 1800]}}, "x", now)
    assert not ok and "60 min" in why
    ok, _ = guard._restart_allowed({"restarts": {"x": [now - 25 * 3600]}}, "x", now)
    assert ok                                                       # day-old history no longer counts


def test_enforce_backoff_blocks_second_restart_via_state(env):
    restart_env(env)
    now = time.time()
    run_stuck(env, tasks={"enforce": True}, apply=True, now=now, state={"restarts": {"hog": [now - 300]}})
    assert restarts(env) == []


def test_restart_failure_is_reported_not_raised_and_not_recorded(env):
    restart_env(env)
    env.sh.on(["docker", "restart"], rc=1, err="boom")
    ctx, r = run_stuck(env, tasks={"enforce": True}, apply=True)
    assert "restarts" not in ctx.state and r.items[0]["note"].startswith("docker restart hog rc=1")
    assert r.metrics["restarted"] == ""


# =========================================================================== orphan_report
def run_orphans(env, now=1_000_000.0, state=None, tasks=None):
    ctx = Ctx(cfg_of({"orphan_report": {"idle_hours": 3, **(tasks or {})}}), "orphan_report", False, now)
    ctx.state.update(state or {})
    return ctx, guard.orphan_report(ctx)


def test_orphan_report_clean_system(env):
    mk_proc(env.proc, 1, "systemd", ppid=0)
    mk_proc(env.proc, 500, "bash", ppid=1)
    _, r = run_orphans(env)
    assert r.status == "ok" and r.alert is False and r.metrics["findings"] == 0


def test_orphan_emulator_vs_emulator_with_live_parent(env):
    avd = ["/sdk/qemu-system-x86_64-headless", "-avd", "test_avd", "-no-window"]
    mk_proc(env.proc, 1, "systemd", ppid=0)
    mk_proc(env.proc, 600, "qemu-system-x86", ppid=1, start=100, argv=avd, anon_kb=200_000)
    mk_proc(env.proc, 700, "bash", ppid=1)
    mk_proc(env.proc, 601, "qemu-system-x86", ppid=700, start=100, argv=avd[:2] + ["live_avd"], anon_kb=100_000)
    _, r = run_orphans(env, tasks={"emulator_warn_hours": 24})        # uptime 1e6 s: both are ~277 h old
    kinds = {(i["kind"], i["avd"]) for i in r.items}
    assert kinds == {("emulator-orphan", "test_avd"), ("emulator-long", "live_avd")}
    assert r.metrics["emulator_orphans"] == 1 and r.metrics["emulators_long"] == 1
    assert r.items[0]["kind"] == "emulator-orphan" and r.status == "info" and r.alert is False


def test_emulator_reparented_to_systemd_user_subreaper_is_orphan(env):
    mk_proc(env.proc, 2000, "systemd", ppid=1)
    mk_proc(env.proc, 600, "qemu-system-x86", ppid=2000, argv=["/x/qemu-system-x86_64", "-avd", "a"])
    _, r = run_orphans(env)
    assert r.metrics["emulator_orphans"] == 1


def test_young_live_emulator_is_not_reported(env):
    mk_proc(env.proc, 700, "bash", ppid=1)
    mk_proc(env.proc, 601, "qemu-system-x86", ppid=700, start=int(1_000_000 * 100 - 3600 * 100),
            argv=["/x/qemu-system-x86_64", "-avd", "a"])
    _, r = run_orphans(env)
    assert r.metrics["findings"] == 0 and r.metrics["emulators_long"] == 0


def test_crashpad_orphan_only_when_no_emulator_alive_and_chrome_is_exempt(env):
    mk_proc(env.proc, 800, "crashpad_handle", ppid=1, argv=["/sdk/emulator/crashpad_handler", "--database=/tmp/x"])
    mk_proc(env.proc, 801, "chrome_crashpad", ppid=1, argv=["/opt/google/chrome/chrome_crashpad_handler"])
    _, r = run_orphans(env)
    assert [i["pid"] for i in r.items] == [800] and r.metrics["crashpad"] == 1
    mk_proc(env.proc, 600, "qemu-system-x86", ppid=1, argv=["/x/qemu-system-x86_64", "-avd", "a"])
    _, r = run_orphans(env)
    assert r.metrics["crashpad"] == 0 and r.metrics["emulator_orphans"] == 1


def test_stray_test_server_and_zombies(env):
    mk_proc(env.proc, 900, "python3", ppid=1, argv=["python3", "slow.py", "53565"], anon_kb=500)
    mk_proc(env.proc, 901, "python3", ppid=1, argv=["/usr/bin/python3", "app.py"])
    mk_proc(env.proc, 902, "bash", ppid=77, state="Z")
    _, r = run_orphans(env)
    assert [(i["kind"], i["pid"]) for i in r.items] == [("stray-server", 900), ("zombie", 902)]
    assert r.metrics["strays"] == 1 and r.metrics["zombies"] == 1 and "1 stray server, 1 zombie" in r.summary


GRADLE = ["java", "-cp", "x.jar", "org.gradle.launcher.daemon.bootstrap.GradleDaemon", "8.5"]


def test_gradle_idle_tracking_across_runs(env):
    mk_proc(env.proc, 1000, "java", ticks=500, start=50, argv=GRADLE, anon_kb=600_000)
    ctx, r = run_orphans(env, now=1_000_000)
    assert r.metrics["gradle_idle"] == 0                              # first sighting: nothing is known yet
    st = ctx.state
    # 4 h later, CPU ticks unchanged => idle for 4 h => reported
    ctx, r = run_orphans(env, now=1_000_000 + 4 * 3600, state=st)
    (it,) = r.items
    assert it["kind"] == "gradle-idle" and it["idle_h"] == 4.0 and it["anon_h"] == "585.9 MiB"
    assert "idle gradle daemon" in r.summary and r.alert is False


def test_gradle_active_between_runs_resets_idle_clock(env):
    mk_proc(env.proc, 1000, "java", ticks=500, start=50, argv=GRADLE)
    ctx, _ = run_orphans(env, now=1_000_000)
    mk_proc(env.proc, 1000, "java", ticks=500 + 100_000, start=50, argv=GRADLE)       # ~1000 s CPU
    ctx, r = run_orphans(env, now=1_000_000 + 4 * 3600, state=ctx.state)
    assert r.metrics["gradle_idle"] == 0


def test_gradle_reported_idle_only_without_live_build(env):
    mk_proc(env.proc, 1000, "java", ticks=500, start=50, argv=GRADLE)
    ctx, _ = run_orphans(env, now=1_000_000)
    mk_proc(env.proc, 1001, "java", ppid=1000, argv=["java", "GradleWorkerMain", "Gradle Test Executor 1"])
    _, r = run_orphans(env, now=1_000_000 + 4 * 3600, state=ctx.state)
    assert r.metrics["gradle_idle"] == 0


def test_gradle_state_is_pruned_and_pid_reuse_resets(env):
    mk_proc(env.proc, 1000, "java", ticks=500, start=50, argv=GRADLE)
    ctx, _ = run_orphans(env, now=1_000_000)
    assert list(ctx.state["gradle"]) == ["1000:50"]
    os.remove(env.proc / "1000" / "stat")
    mk_proc(env.proc, 1000, "java", ticks=500, start=99999, argv=GRADLE)         # same pid, different start time
    ctx, r = run_orphans(env, now=1_000_000 + 4 * 3600, state=ctx.state)
    assert list(ctx.state["gradle"]) == ["1000:99999"] and r.metrics["gradle_idle"] == 0


def test_gradle_runs_seconds_apart_keep_baseline(env):
    mk_proc(env.proc, 1000, "java", ticks=500, start=50, argv=GRADLE)
    ctx, _ = run_orphans(env, now=1_000_000)
    ctx, _ = run_orphans(env, now=1_000_010, state=ctx.state)
    assert ctx.state["gradle"]["1000:50"]["t"] == 1_000_000


def test_orphan_report_huge_pages_the_owner(env):
    mk_proc(env.proc, 1000, "java", ticks=500, start=50, argv=GRADLE, anon_kb=12 * 1024 * 1024)
    ctx, _ = run_orphans(env, now=1_000_000)
    _, r = run_orphans(env, now=1_000_000 + 4 * 3600, state=ctx.state)
    assert r.status == "warn" and r.alert is True


def test_orphan_report_proc_unreadable_raises_so_runner_records_error(env):
    env.mp.setattr(gates, "PROC", env.root / "gone")
    with pytest.raises(OSError):
        guard.orphan_report(Ctx(cfg_of(), "orphan_report", False, 1.0))


# =========================================================================== image_ledger
def sha(n: int) -> str:
    return "sha256:" + f"{n:064x}"


def ledger_env(env, containers_, host_images=None, inspect_rc=0):
    ids = [f"{i + 1:064x}" for i in range(len(containers_))]
    env.sh.on(["docker", "ps", "-a", "-q"], out="\n".join(ids) + "\n")
    env.sh.on(["docker", "inspect"], rc=inspect_rc,
              out="".join(f"{sha(img)} /{name}\n" for name, img in containers_.items()))
    env.sh.on(["docker", "images"], out="".join(sha(i) + "\n" for i in (host_images or [])))


def run_ledger(env, now, keep_days=30):
    return guard.image_ledger(Ctx(cfg_of({"image_ledger": {"keep_days": keep_days}}), "image_ledger", False, now))


def test_image_ledger_records_and_updates(env):
    ledger_env(env, {"web": 1, "db": 2, "web2": 1}, host_images=[1, 2, 3, 3])
    r = run_ledger(env, 1000.0)
    led = json.loads((env.state / "ledger" / "images.json").read_text())
    assert led["version"] == 1 and led["created"] == 1000.0 and led["updated"] == 1000.0
    assert led["images"][sha(1)] == {"first_seen": 1000.0, "last_seen": 1000.0, "names": ["web", "web2"]}
    assert set(led["images"]) == {sha(1), sha(2)}
    assert r.status == "ok" and r.metrics["referenced"] == 2 and r.metrics["unreferenced"] == 1
    ledger_env(env, {"web": 1, "api": 4})
    run_ledger(env, 5000.0)
    led = json.loads((env.state / "ledger" / "images.json").read_text())
    assert led["created"] == 1000.0 and led["images"][sha(1)]["first_seen"] == 1000.0
    assert led["images"][sha(1)]["last_seen"] == 5000.0 and led["images"][sha(2)]["last_seen"] == 1000.0
    assert guard.ledger_last_seen(sha(2)) == 1000.0 and guard.ledger_last_seen(sha(99)) is None
    assert guard.ledger_age_days(1000.0 + 86400 * 3) == pytest.approx(3.0)


def test_image_ledger_expires_only_after_keep_days(env):
    ledger_env(env, {"web": 1, "old": 2})
    run_ledger(env, 0.0)
    ledger_env(env, {"web": 1})
    run_ledger(env, 29 * 86400.0)
    assert sha(2) in guard.load_ledger()["images"]
    r = run_ledger(env, 31 * 86400.0)
    assert sha(2) not in guard.load_ledger()["images"] and r.metrics["expired"] == 1
    assert sha(1) in guard.load_ledger()["images"]


def test_image_ledger_docker_failure_leaves_ledger_untouched(env):
    ledger_env(env, {"web": 1})
    run_ledger(env, 1000.0)
    before = (env.state / "ledger" / "images.json").read_text()
    env.sh.on(["docker", "ps", "-a", "-q"], rc=1)
    assert run_ledger(env, 999999.0).status == "warn"
    ledger_env(env, {"web": 1}, inspect_rc=1)
    env.sh.on(["docker", "inspect"], rc=1, out="")
    assert run_ledger(env, 999999.0).status == "warn"
    assert (env.state / "ledger" / "images.json").read_text() == before


def test_image_ledger_partial_inspect_still_records_what_it_got(env):
    ledger_env(env, {"web": 1}, inspect_rc=1)
    r = run_ledger(env, 10.0)
    assert r.status == "ok" and r.metrics["partial"] is True and sha(1) in guard.load_ledger()["images"]


def test_image_ledger_garbage_lines_and_corrupt_file(env):
    write(env.state / "ledger" / "images.json", "{not json")
    env.sh.on(["docker", "ps", "-a", "-q"], out=f"{1:064x}\n")
    env.sh.on(["docker", "inspect"], out=f"<no value> /x\nsha256:short /y\n{sha(7)} /z\n")
    run_ledger(env, 10.0)
    assert set(guard.load_ledger()["images"]) == {sha(7)}
    assert guard.ledger_age_days() is not None


def test_ledger_helpers_without_file(env):
    assert guard.ledger_last_seen(sha(1)) is None and guard.ledger_age_days() is None


def test_stuck_stale_samples_are_not_judged(env):
    put_samples(env, {"stuck-svc": flat(9)}, now=time.time() - 3 * 3600)
    _, r = run_stuck(env)
    assert r.status == "info" and "sampler not running" in r.summary and r.alert is False


def test_stuck_malformed_sample_entries_are_skipped_not_fatal(env):
    now = time.time()
    for i in range(6):
        c = {"bad": {"anon": 9 * GIB}, "good": {"anon": 9 * GIB, "cpu_us": 1, "io_b": 1}}
        add_sample({"t": now - (5 - i) * STEP, "kind": "sample", "host": {}, "c": c})
    _, r = run_stuck(env, now=now)
    assert [i["name"] for i in r.items] == ["good"]


def test_stuck_swap_in_needs_the_counter_on_both_ends(env):
    now = time.time()
    for i in range(6):
        host = {"mem_avail": 50 * GIB, "psi_mem_full60": 0.0}
        if i == 5:
            host["pswpin"] = 90_000_000                         # first sample has no counter: unknown, not pressure
        add_sample({"t": now - (5 - i) * STEP, "kind": "sample", "host": host,
                             "c": {"a": {"anon": 1, "cpu_us": 1, "io_b": 1}}})
    _, r = run_stuck(env, now=now)
    assert r.metrics["pressure"] == "none"


def test_stuck_missing_host_data_is_not_pressure(env):
    put_samples(env, {"a": flat(1)}, host={})
    assert run_stuck(env)[1].metrics["pressure"] == "none"


# =========================================================================== shared helpers
def test_parse_stat_handles_spaces_and_parens_in_comm():
    st = "42 (weird (name) x) S 7 1 1 0 -1 0 0 0 0 0 11 22 0 0 20 0 1 0 999 1 1"
    assert gates.parse_stat(st) == ("weird (name) x", "S", 7, 33, 999)
    assert gates.parse_stat("garbage") is None and gates.parse_stat("1 (x) S 1") is None


def test_containers_parsing_and_failure(env):
    env.sh.on(["docker", "ps", "--no-trunc"], out=f"{cid(1)} alpha\n{cid(2)} beta,link\nnot-an-id junk\n\n")
    assert gates.containers() == {"alpha": cid(1), "beta": cid(2)}
    env.sh.on(["docker", "ps", "--no-trunc"], rc=1)
    assert gates.containers() is None


def test_cg_dir_layouts_and_id_validation(env):
    assert gates.cg_dir(cid(1)) is None
    (env.cg / "docker" / cid(1)).mkdir(parents=True)                      # cgroupfs driver layout
    assert gates.cg_dir(cid(1)) == env.cg / "docker" / cid(1)
    mk_cg(env.cg, cid(1))
    assert gates.cg_dir(cid(1)).name == f"docker-{cid(1)}.scope"          # systemd layout preferred
    assert gates.cg_dir("../../etc") is None and gates.cg_dir("") is None


def test_sampler_is_fast_for_72_containers_and_1200_processes(env):
    out = ""
    for i in range(72):
        mk_cg(env.cg, cid(i + 1), anon=GIB, file_=GIB, peak=2 * GIB, cpu_us=i, pids=[5000 + i])
        out += f"{cid(i + 1)} c{i}\n"
    env.sh.on(["docker", "ps", "--no-trunc"], out=out)
    for i in range(1200):
        mk_proc(env.proc, 5000 + i, f"p{i}", anon_kb=100 + i)
    t0 = time.time()
    r = guard.spike_sampler(Ctx(cfg_of(), "spike_sampler", False, time.time()))
    assert time.time() - t0 < 3 and r.metrics["containers"] == 72
    assert all(p[1] >= 5072 for p in history(env)[0]["p"])              # the 72 container pids were excluded


def test_cli_gate_command_is_wired_to_gates(env, gate_cfg, capsys):
    from homelab_maint import cli
    env.mp.setattr(cli, "load_tasks", lambda: None)
    probes = dict(gates._PROBES)
    probes["plex"] = lambda b: (True, "transcoding")
    env.mp.setattr(gates, "_PROBES", probes)
    assert cli.main(["gate", "plex"]) == 1
    probes["plex"] = lambda b: (False, "idle")
    assert cli.main(["gate", "plex"]) == 0
    assert "idle" in capsys.readouterr().out


# =========================================================================== regression: review round 1
# ---- 1. samples have their own compact, atomically trimmed file ---------------------------------------------------
def sample_lines(env):
    return (env.state / "samples.jsonl").read_text().splitlines()


def test_sampler_writes_compact_samples_file_and_leaves_history_alone(env):
    env.sh.on(["docker", "ps", "--no-trunc"], out=f"{cid(1)}|alpha|img:1|\n")
    mk_cg(env.cg, cid(1), anon=GIB, cpu_us=5)
    guard.spike_sampler(Ctx(cfg_of(), "spike_sampler", False, time.time()))
    assert not (env.state / "history.jsonl").exists()          # core's history file no longer carries samples
    (line,) = sample_lines(env)
    assert '", "' not in line and '": ' not in line            # compact separators
    assert json.loads(line)["c"]["alpha"]["anon"] == GIB


def test_sample_record_for_72_containers_is_compact(env):
    for i in range(72):
        mk_cg(env.cg, cid(i + 1), anon=1234567890 + i, file_=987654321, swap=0, peak=2345678901, cpu_us=901409715 + i,
              io=((259, 0, 7236718592, 14929920),))
    env.sh.on(["docker", "ps", "--no-trunc"], out="".join(f"{cid(i + 1)}|container-{i}|img|\n" for i in range(72)))
    guard.spike_sampler(Ctx(cfg_of(), "spike_sampler", False, time.time()))
    (line,) = sample_lines(env)
    assert len(line) < 0.95 * len(json.dumps(json.loads(line))) and len(line) < 10_000


def test_trim_waits_until_oldest_line_is_a_day_past_keep_days_then_replaces_atomically(env):
    now, p = 100 * 86400.0, env.state / "samples.jsonl"

    def put(*ages_d):
        write(p, "".join(json.dumps({"t": now - a * 86400, "kind": "sample", "c": {}}) + "\n" for a in ages_d))

    put(14.5, 13, 1)                                    # oldest is past keep_days but inside the one-day slack
    ino = p.stat().st_ino
    guard.append_sample({"t": now, "kind": "sample", "c": {}}, 14, now)
    assert len(sample_lines(env)) == 4 and p.stat().st_ino == ino          # untouched: no rewrite on every append
    put(15.5, 14.2, 13, 1)
    guard.append_sample({"t": now, "kind": "sample", "c": {}}, 14, now)
    ages = [round((now - json.loads(ln)["t"]) / 86400, 1) for ln in sample_lines(env)]
    assert ages == [13.0, 1.0, 0.0]                      # everything older than keep_days is gone, newest kept
    assert not (env.state / "samples.jsonl.tmp").exists()


def test_trim_interrupted_midway_never_truncates_the_file(env):
    """SIGALRM (core._Timeout) or an OOM kill during the rewrite must leave the old content, not a half file."""
    now, p = 100 * 86400.0, env.state / "samples.jsonl"
    write(p, "".join(json.dumps({"t": now - a * 86400, "kind": "sample", "c": {}}) + "\n" for a in (20, 13, 1)))
    before = p.read_bytes()

    def alarm(fd):
        raise core._Timeout()
    env.mp.setattr(os, "fsync", alarm)
    with pytest.raises(core._Timeout):
        guard.append_sample({"t": now, "kind": "sample", "c": {}}, 14, now)
    after = p.read_bytes()
    assert after.startswith(before) and len(after.splitlines()) == 4      # old lines intact + the new sample
    assert not (env.state / "samples.jsonl.tmp").exists()


def test_keep_days_option_is_honoured_by_the_sampler(env):
    env.sh.on(["docker", "ps", "--no-trunc"], out="")
    now = time.time()
    old = json.dumps({"t": now - 6 * 86400, "kind": "sample", "c": {}}) + "\n"
    write(env.state / "samples.jsonl", old)
    guard.spike_sampler(Ctx(cfg_of({"spike_sampler": {"keep_days": 14}}), "spike_sampler", False, now))
    assert len(sample_lines(env)) == 2                                     # 6 d < 14 d: kept
    guard.spike_sampler(Ctx(cfg_of({"spike_sampler": {"keep_days": 3}}), "spike_sampler", False, now))
    assert [json.loads(x)["t"] for x in sample_lines(env)] == [now, now]   # 6 d > 3 d + 1 d slack: trimmed


def test_read_samples_filters_by_age_skips_garbage_and_parses_any_key_order(env):
    now = 1_000_000.0
    write(env.state / "samples.jsonl",
          json.dumps({"t": now - 7200, "kind": "sample", "c": {"old": {}}}) + "\n"
          "{truncated\n"
          + json.dumps({"kind": "sample", "t": now - 60, "c": {"new": {}}}) + "\n"      # "t" not first: slow path
          + json.dumps({"t": now - 30, "kind": "sample", "c": {"newer": {}}}) + "\n")
    got = guard.read_samples(3600, now)
    assert [list(r["c"]) for r in got] == [["new"], ["newer"]]
    assert guard.read_samples(10, now) == [] and len(guard.read_samples(10 ** 7, now)) == 3


def test_read_samples_without_file_is_empty(env):
    assert guard.read_samples(3600) == []


# ---- 2. docker build gate sees builds through global / compose options ---------------------------------------------
BUILD_VARIANTS = ["docker compose -f a.yml build",
                  "docker compose -f a.yml -f b.yml up -d --build",
                  "docker-compose -f x.yml build",
                  "docker compose --project-name x build",
                  "docker buildx --builder x build .",
                  "docker image build .",
                  "docker builder build .",
                  "docker -H tcp://h build .",
                  "docker --context c buildx build",
                  "docker buildx bake",
                  "docker compose -p x --profile y run --build app",
                  "docker --host=tcp://h compose --env-file e.env up --build=true",
                  "sudo -u ohmz docker compose -f a.yml build",
                  "timeout 600 /usr/bin/docker --config /tmp/c buildx build -t x .",
                  "buildctl --addr tcp://h build --frontend dockerfile.v0"]


@pytest.mark.parametrize("cmd", BUILD_VARIANTS)
def test_docker_build_with_options_before_the_verb_is_busy(env, cmd):
    mk_proc(env.proc, 40, "docker", argv=cmd.split())
    busy, why = gates.busy("docker_build", cfg_of())
    assert busy and "docker build process" in why


def test_docker_build_inside_a_shell_script_is_busy(env):
    mk_proc(env.proc, 40, "bash", argv=["bash", "-c", "cd /srv/app && docker compose -f a.yml -f b.yml build --pull"])
    assert gates.busy("docker_build", cfg_of())[0] is True


@pytest.mark.parametrize("cmd", ["docker compose -f a.yml up -d", "docker compose -f a.yml -f b.yml ps",
                                 "docker -H tcp://h ps", "docker builder prune -f", "docker buildx --builder x ls",
                                 "docker --context c buildx du",
                                 "docker buildx prune --builder x -f --max-used-space 8gb",
                                 "docker run --rm img build", "docker exec c npm run build",
                                 "docker compose exec web npm run build", "docker compose up --no-build",
                                 "docker image prune -f", "docker -v", "vim docker-compose.yml build"])
def test_docker_commands_that_are_not_builds_stay_idle(env, cmd):
    mk_proc(env.proc, 41, "docker", argv=cmd.split())
    assert gates.busy("docker_build", cfg_of()) == (False, "no docker build running")


def test_is_docker_build_pre_filter_and_odd_input():
    assert gates.is_docker_build([]) is False and gates.is_docker_build(["ls", "build"]) is False
    assert gates.is_docker_build(["docker"]) is False and gates.is_docker_build(["docker", "-f"]) is False


# ---- 3. protection looks at image and compose service, not only the container name --------------------------------
def test_container_info_parses_image_and_compose_service(env):
    env.sh.on(["docker", "ps", "--no-trunc"],
              out=f"{cid(1)}|afsaane-prod-db|postgres:17-alpine|db\n{cid(2)}|plain|img:1|\n{cid(3)} legacy\n")
    info = gates.container_info()
    assert info["afsaane-prod-db"] == gates.Container(cid(1), "postgres:17-alpine", "db")
    assert info["plain"] == gates.Container(cid(2), "img:1", "")
    assert info["legacy"] == gates.Container(cid(3), "", "")           # the old `ID Names` output still parses
    assert gates.containers() == {"afsaane-prod-db": cid(1), "plain": cid(2), "legacy": cid(3)}
    fmt = env.sh.ran("docker", "ps")[0][-1]
    assert "{{.Image}}" in fmt and "com.docker.compose.service" in fmt
    env.sh.on(["docker", "ps", "--no-trunc"], rc=1)
    assert gates.container_info() is None


@pytest.mark.parametrize("name,image,protected", [
    ("afsaane-prod", "postgres:17-alpine", True),                   # name says nothing, image is a database
    ("store", "docker.io/library/mysql:8", True),
    ("cache", "valkey/valkey:8", True),
    ("buildx_buildkit_immaculaterr-builder0", "moby/buildkit:buildx-stable-1", True),
    ("ci-runner", "moby/buildkit:v0.15", True),
    ("afsaane-test-db", "example/app:1", True),                     # name ends in -db
    ("web", "nginx:1.27", False),
    ("worker", "example/worker:3", False)])
def test_stuck_protection_follows_image_not_only_name(env, name, image, protected):
    put_samples(env, {name: flat(9)})
    _, r = run_stuck(env, patterns=(), images={name: image})              # no protected.toml patterns at all
    assert r.items[0]["protected"] is protected and r.items[0]["image"] == image[:40]
    assert r.metrics["actionable"] == (0 if protected else 1) and (r.status == "info") == protected


def test_stuck_with_the_shipped_protected_toml_never_flags_the_afsaane_dbs_or_the_builder(env):
    series = {n: flat(9) for n in ("afsaane-prod-db", "afsaane-test-db", "buildx_buildkit_immaculaterr-builder0")}
    put_samples(env, series)
    _, r = run_stuck(env, patterns=REAL_PROTECTED["patterns"],
                     images={"afsaane-prod-db": "postgres:17-alpine", "afsaane-test-db": "postgres:17-alpine",
                             "buildx_buildkit_immaculaterr-builder0": "moby/buildkit:buildx-stable-1"})
    assert len(r.items) == 3 and all(i["protected"] for i in r.items)
    assert r.status == "info" and r.alert is False and r.metrics["actionable"] == 0


def test_enforce_refuses_to_restart_a_database_that_only_its_image_gives_away(env):
    restart_env(env, **{"afsaane-prod": flat(9)})
    run_stuck(env, tasks={"enforce": True}, apply=True, patterns=(), images={"afsaane-prod": "postgres:17-alpine"})
    assert restarts(env) == []
    # control: the same container as a plain image IS restarted, so the refusal above came from protection
    restart_env(env, **{"afsaane-prod": flat(9)})
    run_stuck(env, tasks={"enforce": True}, apply=True, patterns=(), images={"afsaane-prod": "nginx:1.27"})
    assert restarts(env) == [["docker", "restart", "-t", "60", "afsaane-prod"]]


def test_stuck_candidates_are_unverified_and_protected_when_docker_cannot_be_asked(env):
    put_samples(env, {"hog": flat(9)})
    _, r = run_stuck(env, docker_rc=1)
    (it,) = r.items
    assert it["protected"] is True and "unverified" in it["note"]
    assert r.status == "info" and r.alert is False and r.metrics["actionable"] == 0


def test_stuck_candidate_that_stopped_meanwhile_is_not_actionable(env):
    put_samples(env, {"hog": flat(9)})
    _, r = run_stuck(env, images={"hog": None})
    assert r.items[0]["protected"] is True and r.items[0]["note"] == "no longer running"


def test_stuck_no_docker_lookup_when_there_is_no_candidate(env):
    put_samples(env, {"small": flat(1)})
    run_stuck(env)
    assert env.sh.ran("docker", "ps") == []


def test_buildkit_containers_ask_the_docker_build_gate(env):
    put_samples(env, {"ci-runner": flat(9)})
    calls = []
    env.mp.setattr(gates, "busy", lambda g, cfg=None: (calls.append(g), (True, "build running"))[1])
    _, r = run_stuck(env, patterns=(), images={"ci-runner": "moby/buildkit:v0.15"})
    assert calls == ["docker_build"] and r.items[0]["busy"] == "build running"


# ---- 4. a counter the cgroup could not give is unknown, never "zero progress" --------------------------------------
def test_cg_sample_omits_counters_it_cannot_read(env):
    d = mk_cg(env.cg, cid(1), anon=9 * GIB)
    os.remove(d / "io.stat")
    os.remove(d / "cpu.stat")
    rec, _ = guard._cg_sample(d)
    assert rec["anon"] == 9 * GIB and "cpu_us" not in rec and "io_b" not in rec
    write(d / "io.stat", "")                                    # present but no byte counters: still unknown
    write(d / "cpu.stat", "user_usec 5\n")                      # no usage_usec line
    rec, _ = guard._cg_sample(d)
    assert "cpu_us" not in rec and "io_b" not in rec
    z = mk_cg(env.cg, cid(2), cpu_us=0, io=((259, 0, 0, 0),))   # a readable zero IS data
    rec, _ = guard._cg_sample(z)
    assert rec["cpu_us"] == 0 and rec["io_b"] == 0


def test_stuck_container_without_progress_counters_is_not_a_candidate(env):
    now = time.time()

    def put(missing_in=()):
        for i in range(6):
            c = {"anon": 9 * GIB, "cur": 9 * GIB, "peak": 9 * GIB}
            if i not in missing_in:
                c.update(cpu_us=1_000_000, io_b=50 * MIB)
            add_sample({"t": now - (5 - i) * STEP, "kind": "sample", "host": {}, "c": {"a": c}})
    put(missing_in=range(6))                                    # io accounting off: no counters in any sample
    _, r = run_stuck(env, now=now)
    assert r.status == "ok" and r.items == []
    env.mp.setattr(guard, "STATE_DIR", env.root / "second")
    put(missing_in=(2,))                                        # one sample in the middle lacks them: no verdict
    assert run_stuck(env, now=now)[1].items == []
    env.mp.setattr(guard, "STATE_DIR", env.root / "control")
    put()                                                       # control: with counters it IS an idle-but-holding hit
    assert [i["name"] for i in run_stuck(env, now=now)[1].items] == ["a"]


def test_evaluate_itself_returns_none_for_missing_counters_instead_of_raising(env):
    """The verdict function must decline on its own; stuck_detector's blanket except is only a second net."""
    full = {"anon": 9 * GIB, "cpu_us": 1, "io_b": 1}
    win = [{"t": 1000.0 + i * STEP, "c": {"a": dict(full)}} for i in range(6)]
    args = ("a", win[-1]["c"]["a"], 5 * STEP, 4 * GIB, 0.5, 1.0, 1.0)
    assert guard._evaluate(win, *args)["reason"].startswith("idle but holding")          # control
    del win[3]["c"]["a"]["io_b"]
    assert guard._evaluate(win, *args) is None
    win[3]["c"]["a"]["io_b"] = None
    assert guard._evaluate(win, *args) is None
    del win[3]["c"]["a"]["io_b"], win[5]["c"]["a"]["cpu_us"]                             # newest sample too
    assert guard._evaluate(win, *args) is None


# ---- 5. a sampler that cannot read any cgroup must not look healthy -----------------------------------------------
def test_sampler_warns_when_no_cgroup_is_readable(env):
    env.sh.on(["docker", "ps", "--no-trunc"], out="".join(f"{cid(i)}|c{i}|img|\n" for i in range(1, 73)))
    r = guard.spike_sampler(Ctx(cfg_of(), "spike_sampler", False, time.time()))      # CGROUP tree is empty
    assert r.status == "warn" and r.alert is True and "72 of 72 cgroups unreadable" in r.summary
    assert r.metrics["skipped"] == 72 and len(r.summary) <= 140 and r.summary.isascii()
    assert history(env)[0]["c"] == {}


def test_sampler_tolerates_a_few_vanished_cgroups_but_not_many(env):
    env.sh.on(["docker", "ps", "--no-trunc"], out="".join(f"{cid(i)}|c{i}|img|\n" for i in range(1, 11)))
    for i in range(1, 10):
        mk_cg(env.cg, cid(i), anon=GIB)
    r = guard.spike_sampler(Ctx(cfg_of(), "spike_sampler", False, time.time()))      # 1 of 10 vanished
    assert r.status == "ok" and r.metrics["skipped"] == 1
    mk_cg(env.cg, cid(10), anon=GIB)
    for i in (1, 2, 3):
        shutil.rmtree(env.cg / "system.slice" / f"docker-{cid(i)}.scope")
    r = guard.spike_sampler(Ctx(cfg_of(), "spike_sampler", False, time.time()))      # 3 of 10 = 30 %
    assert r.status == "warn" and "3 of 10 cgroups unreadable" in r.summary
    shutil.rmtree(env.cg / "system.slice" / f"docker-{cid(4)}.scope")
    r = guard.spike_sampler(Ctx(cfg_of(), "spike_sampler", False, time.time()))      # 4 of 10
    assert r.status == "warn"


def put_empty(env, n, now, step=STEP):
    for i in range(n):
        add_sample({"t": now - (n - 1 - i) * step, "kind": "sample", "host": {}, "c": {}})


def test_stuck_detector_warns_about_no_usable_samples_after_two_hours(env):
    now = time.time()
    put_empty(env, 10, now)                                     # 2 h 15 min of records, none with container data
    _, r = run_stuck(env, now=now)
    assert r.status == "warn" and r.alert is True and "no usable samples" in r.summary
    assert "0 of 10 records" in r.summary and len(r.summary) <= 140 and r.metrics["usable"] == 0
    env.mp.setattr(guard, "STATE_DIR", env.root / "short")
    put_empty(env, 5, now)                                      # only 1 h so far: still just warming up
    _, r = run_stuck(env, now=now)
    assert r.status == "info" and "collecting samples: 0/6" in r.summary and r.alert is False


def test_stuck_detector_warns_when_container_data_stops_after_a_good_run(env):
    now = time.time()
    put_samples(env, {"a": flat(1)}, now=now - 4 * 3600)        # 6 good samples ending 4 h ago
    put_empty(env, 12, now - 900)                               # then records with no container data until now
    _, r = run_stuck(env, now=now)
    assert r.status == "warn" and "no usable samples" in r.summary


def test_stuck_detector_does_not_warn_when_good_samples_are_recent_or_sampler_is_dead(env):
    now = time.time()
    put_samples(env, {"a": flat(1)}, now=now)
    put_empty(env, 3, now - 30 * 60)                            # a docker blip long ago is not an outage
    assert run_stuck(env, now=now)[1].status == "ok"
    env.mp.setattr(guard, "STATE_DIR", env.root / "dead")
    put_empty(env, 12, now - 5 * 3600)                          # empty records but nothing for 5 h: sampler is dead
    _, r = run_stuck(env, now=now)
    assert r.status == "info" and "no usable" not in r.summary


# =========================================================================== registration / contract
def test_tasks_registered_with_contract_fields():
    want = {"spike_sampler", "stuck_detector", "orphan_report", "image_ledger"}
    assert want <= set(core.REGISTRY)
    for n in want:
        t = core.REGISTRY[n]
        assert (t.klass, t.tier) == ("C0", "check")


def test_all_tasks_return_short_ascii_summaries_via_runner(env):
    env.sh.on(["docker", "ps", "--no-trunc"], out=f"{cid(1)} nöme\n")
    mk_cg(env.cg, cid(1), anon=GIB)
    env.sh.on(["docker", "ps", "-a", "-q"], out="")
    mk_proc(env.proc, 1, "systemd", ppid=0)
    cfg = cfg_of()
    for n in ("spike_sampler", "stuck_detector", "orphan_report", "image_ledger"):
        res, _ = core.run_task(core.REGISTRY[n], cfg, apply=False)
        assert res.status != "error", (n, res.summary)
        assert res.summary.isascii() and len(res.summary) <= 140 and len(res.items) <= 12
