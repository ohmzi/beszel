"""Busy gates: "is it safe to do disruptive work right now?"

    busy(name) -> (is_busy, reason)        names: comfyui ollama plex immich docker_build backup apt gradle any
    cli_gate(name) -> exit code            `homelab-maint gate NAME` (0 = proceed, 1 = skip), used by systemd ExecCondition

Fail-closed rules:
  * a probe that errors, times out or returns something unparsable means BUSY;
  * the single exception is a service/container that is definitively NOT RUNNING (stopped ComfyUI container,
    inactive ollama.service): nothing can be busy there, so it is IDLE. "Running but the probe failed" is BUSY.

This module also holds the low-level read-only helpers (container ids, cgroup v2 files, /proc scanning) that
tasks/guard.py shares. PROC and CGROUP are module constants so tests can point them at a fake tree.
This file registers no tasks.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from ..core import STATE_DIR, audit, load_config, read_json, sh, write_json_atomic

PROC = Path("/proc")
CGROUP = Path("/sys/fs/cgroup")

sleep = time.sleep            # patched in tests
_clock = time.monotonic       # patched in tests

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

# systemctl is-active states that mean "not running right now"; anything else (incl. surprises) is busy.
_IDLE_UNIT_STATES = {"inactive", "failed", "dead"}

# systemd names usable with `homelab-maint gate`, mapped to the probe they run.
ALIASES = {"immich-recycle": "immich"}

# Docker build detection works on tokens, not on a joined-cmdline regex: global/compose options may sit between the
# binary and the verb (`docker compose -f a.yml -f b.yml up --build`, `docker -H tcp://h build .`). See _build_verb().
_DOCKER_BINS = {"docker", "docker-compose", "docker-buildx", "buildctl"}
_VERB_CHAIN = {"compose", "buildx", "image", "builder"}      # words that may precede the verb: `docker image build`
# Options whose value is the NEXT token (`--opt=value` is one token and needs no entry). An unknown option is treated
# as a flag; after the verb nothing matters, so only options that can appear BEFORE it are listed.
_VALUE_OPTS = {"-f", "--file", "-H", "--host", "-c", "--context", "-p", "--project-name", "--profile", "--env-file",
               "--project-directory", "--builder", "-l", "--log-level", "--config", "--ansi", "--progress",
               "--parallel", "--tlscacert", "--tlscert", "--tlskey", "--addr", "--tlsservername", "--tlsdir",
               "--timeout"}
# Gradle: a build is running when a client or a test/worker JVM exists. The daemons themselves linger idle for
# days, so daemon presence is NOT busy; daemon CPU activity is (see _gradle).
GRADLE_WORKERS = re.compile(
    r"GradleWrapperMain|org\.gradle\.launcher\.GradleMain|Gradle Test Executor|Gradle Worker|GradleWorkerMain|"
    r"org\.gradle\.process\.internal\.worker")
GRADLE_DAEMONS = re.compile(r"org\.gradle\.launcher\.daemon\.bootstrap\.GradleDaemon|KotlinCompileDaemon")
_APT_NAMES = {"apt", "apt-get", "aptitude", "dpkg", "unattended-upgrade", "apt.systemd.daily"}
_APT_LOCKS = ["/var/lib/dpkg/lock-frontend", "/var/lib/dpkg/lock", "/var/lib/apt/lists/lock",
              "/var/cache/apt/archives/lock"]


# --------------------------------------------------------------------------- shared low-level helpers
def read_text(p: Path | str) -> str | None:
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def read_int(p: Path | str) -> int | None:
    t = read_text(p)
    try:
        return int(t.strip()) if t is not None else None
    except ValueError:
        return None


class Container(NamedTuple):
    id: str
    image: str = ""
    service: str = ""      # compose service label, "" when not started by compose


def container_info() -> dict[str, Container] | None:
    """{name: Container(full id, image, compose service)} of RUNNING containers; None when docker cannot be queried.

    Names alone cannot identify a workload (`afsaane-prod-db` is postgres), so callers that decide what is
    protected need the image and the compose service too. A line without `|` (older `ID Names` output) still parses.
    """
    fmt = '{{.ID}}|{{.Names}}|{{.Image}}|{{.Label "com.docker.compose.service"}}'
    r = sh(["docker", "ps", "--no-trunc", "--format", fmt], timeout=20)
    if r.returncode != 0:
        return None
    out: dict[str, Container] = {}
    for ln in r.stdout.splitlines():
        if "|" in ln:
            cid, name, image, service = (ln.strip().split("|", 3) + ["", ""])[:4]
        else:
            cid, _, name = ln.strip().partition(" ")
            image = service = ""
        name = name.split(",")[0].strip()
        if re.fullmatch(r"[0-9a-f]{12,64}", cid) and name:
            out[name] = Container(cid, image.strip(), service.strip())
    return out


def containers() -> dict[str, str] | None:
    """{name: full container id} of RUNNING containers, or None when docker cannot be queried."""
    info = container_info()
    return None if info is None else {n: c.id for n, c in info.items()}


def cg_dir(cid: str) -> Path | None:
    """cgroup v2 directory of a container (systemd cgroup driver first, then cgroupfs)."""
    if not re.fullmatch(r"[0-9a-f]{12,64}", cid):
        return None
    for rel in (f"system.slice/docker-{cid}.scope", f"docker/{cid}"):
        p = CGROUP / rel
        if p.is_dir():
            return p
    return None


def cpu_usec(d: Path) -> int | None:
    """usage_usec from a cgroup's cpu.stat."""
    t = read_text(d / "cpu.stat")
    if t:
        for ln in t.splitlines():
            if ln.startswith("usage_usec "):
                try:
                    return int(ln.split()[1])
                except (IndexError, ValueError):
                    return None
    return None


def cpu_window(dirs: dict[str, Path], seconds: float) -> dict[str, float] | None:
    """CPU use of each cgroup as % of ONE core over `seconds`; None if any file is unreadable (fail closed)."""
    t0 = {n: cpu_usec(d) for n, d in dirs.items()}
    w0 = _clock()
    sleep(seconds)
    t1 = {n: cpu_usec(d) for n, d in dirs.items()}
    wall = max(_clock() - w0, 1e-3)
    out: dict[str, float] = {}
    for n in dirs:
        a, b = t0[n], t1[n]
        if a is None or b is None or b < a:
            return None
        out[n] = (b - a) / (wall * 1e6) * 100
    return out


@dataclass
class Proc:
    pid: int
    ppid: int
    comm: str
    state: str
    ticks: int          # utime + stime, clock ticks
    start: int          # starttime, clock ticks since boot
    argv: list[str]

    @property
    def cmd(self) -> str:
        return " ".join(self.argv) if self.argv else self.comm

    @property
    def exe(self) -> str:
        """basename of argv[0] (the real name; comm is cut at 15 chars)."""
        return os.path.basename(self.argv[0]) if self.argv else self.comm


def parse_stat(text: str) -> tuple[str, str, int, int, int] | None:
    """(comm, state, ppid, utime+stime, starttime) from /proc/<pid>/stat. comm may contain spaces/parens."""
    i, j = text.find("("), text.rfind(")")
    if i < 0 or j < i:
        return None
    f = text[j + 2:].split()
    try:
        return text[i + 1:j], f[0], int(f[1]), int(f[11]) + int(f[12]), int(f[19])
    except (IndexError, ValueError):
        return None


def read_proc(pid: int | str) -> Proc | None:
    base = f"{PROC}/{pid}"
    st = read_text(f"{base}/stat")
    parsed = parse_stat(st) if st else None
    if not parsed:
        return None            # process vanished mid-scan (normal) or unreadable
    comm, state, ppid, ticks, start = parsed
    raw = read_text(f"{base}/cmdline") or ""
    argv = [a for a in raw.split("\0") if a]
    return Proc(int(pid), ppid, comm, state, ticks, start, argv)


def scan_procs() -> list[Proc]:
    """Every visible process. Raises OSError when /proc itself is unreadable (callers treat that as busy)."""
    out = []
    for n in os.listdir(PROC):
        if n.isdigit():
            p = read_proc(n)
            if p:
                out.append(p)
    return out


def rss_anon_kb(pid: int | str) -> int | None:
    t = read_text(f"{PROC}/{pid}/status")
    if t:
        m = re.search(r"^RssAnon:\s+(\d+)\s*kB", t, re.M)
        if m:
            return int(m.group(1))
    return None


def procs_cpu_window(pids: list[int], seconds: float) -> dict[int, float] | None:
    """CPU % of one core per pid over `seconds`. A pid that exits meanwhile is dropped (it is not busy any more)."""
    t0 = {}
    for p in pids:
        pr = read_proc(p)
        if pr:
            t0[p] = pr.ticks
    w0 = _clock()
    sleep(seconds)
    wall = max(_clock() - w0, 1e-3)
    out = {}
    for p, a in t0.items():
        pr = read_proc(p)
        if pr:
            out[p] = max(pr.ticks - a, 0) / CLK_TCK / wall * 100
    return out


def http_json(url: str, timeout: float = 3.0) -> Any:
    """GET + parse JSON from an http(s) URL; raises on any failure. Proxies are ignored (loopback probes)."""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("only http(s) probe URLs are allowed")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return json.loads(resp.read(1_000_000).decode("utf-8", "replace"))


def unit_states(units: list[str]) -> dict[str, str] | None:
    """`systemctl is-active` per unit (it prints one state per line); None if the output is unusable."""
    if not units:
        return {}
    r = sh(["systemctl", "is-active", *units], timeout=15)
    states = r.stdout.split()
    if r.returncode in (124, 127) or len(states) != len(units):
        return None
    return dict(zip(units, states))


def container_state(name: str) -> str | None:
    """'running' | 'exited' | ... | 'absent' (no such container) | None (docker failed)."""
    r = sh(["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.State}}"], timeout=20)
    if r.returncode != 0:
        return None
    lines = r.stdout.split()
    return lines[0].lower() if lines else "absent"


# --------------------------------------------------------------------------- probes
def _busy_cfg(cfg: dict | None) -> dict:
    return ((cfg if cfg is not None else load_config()).get("protected", {}) or {}).get("busy", {}) or {}


def _comfyui(b: dict) -> tuple[bool, str]:
    st = container_state(str(b.get("comfyui_container", "comfyui")))
    if st is None:
        return True, "docker unavailable, cannot tell if ComfyUI runs"
    if st in ("absent", "exited", "created", "dead"):
        return False, f"ComfyUI container {st} (idle)"        # stopped on purpose: nothing to interrupt
    try:
        q = http_json(str(b.get("comfyui_queue_url", "http://127.0.0.1:8188/queue")))
        running, pending = q["queue_running"], q["queue_pending"]
        if not isinstance(running, list) or not isinstance(pending, list):
            raise ValueError("queue fields are not lists")
    except Exception as exc:  # noqa: BLE001 - every probe failure is busy
        return True, f"ComfyUI {st} but queue probe failed ({type(exc).__name__})"
    if running or pending:
        return True, f"ComfyUI queue: {len(running)} running, {len(pending)} pending"
    return False, "ComfyUI queue empty"


def _ollama(b: dict) -> tuple[bool, str]:
    unit = str(b.get("ollama_unit", "ollama.service"))
    states = unit_states([unit])
    if states is None:
        return True, "systemctl failed, cannot tell if Ollama runs"
    if states[unit] in _IDLE_UNIT_STATES:
        return False, f"{unit} {states[unit]} (idle)"
    try:
        ps = http_json(str(b.get("ollama_ps_url", "http://127.0.0.1:11434/api/ps")))
        models = ps["models"]
        if not isinstance(models, list):
            raise ValueError("models is not a list")
    except Exception as exc:  # noqa: BLE001
        return True, f"Ollama running but probe failed ({type(exc).__name__})"
    # /api/ps only says what is LOADED (models idle for the keep-alive period stay loaded), not whether a request
    # is in flight, so generation is detected from CPU of the service cgroup (the runner spins a core while decoding).
    cg = CGROUP / "system.slice" / unit
    pct_cfg = float(b.get("ollama_cpu_busy_pct", 10))
    cpu = cpu_window({unit: cg}, float(b.get("ollama_sample_s", 2)))
    if cpu is None:
        return True, "Ollama cgroup cpu.stat unreadable"
    if cpu[unit] > pct_cfg:
        return True, f"Ollama using {cpu[unit]:.0f}% of a core, {len(models)} model(s) loaded"
    return False, f"Ollama idle, {len(models)} model(s) loaded"


def _plex(b: dict) -> tuple[bool, str]:
    pats = b.get("plex_process_patterns") or ["Plex Media Scanner", "Plex Transcoder", "Plex Script Host"]
    try:
        procs = scan_procs()
        rx = [re.compile(p, re.I) for p in pats]
    except (OSError, re.error) as exc:
        return True, f"plex probe failed ({type(exc).__name__})"
    for p in procs:
        # match cmdline and comm; the persistent plug-in host retitles itself so it does not match here
        if p.state != "Z" and any(r.search(p.cmd) or r.search(p.comm) for r in rx):
            return True, f"Plex activity: {p.comm} (pid {p.pid})"
    return False, "no Plex scanner/transcoder running"


def _units(units: list[str], what: str) -> tuple[bool, str]:
    st = unit_states(units)
    if st is None:
        return True, f"{what}: systemctl failed"
    for u, s in st.items():
        if s not in _IDLE_UNIT_STATES:
            return True, f"{u} is {s}"
    return False, f"no {what} unit active"


def _backup(b: dict) -> tuple[bool, str]:
    return _units(list(b.get("backup_units", [])), "backup")


def _immich(b: dict) -> tuple[bool, str]:
    # 1) backups/prunes of the Immich stack running => busy
    busy, why = _backup(b)
    if busy:
        return True, why
    # 2) CPU of the immich containers over a window (job queues live in Redis/Postgres, not reachable
    #    without credentials, so CPU of server / machine-learning / postgres is the proxy for "jobs running")
    names = list(b.get("immich_containers", ["immich_server", "immich_machine_learning"]))
    names.append(str(b.get("immich_db_container", "immich_postgres")))
    running = containers()
    if running is None:
        return True, "docker unavailable, cannot tell if Immich is busy"
    dirs: dict[str, Path] = {}
    for n in names:
        if n in running:
            d = cg_dir(running[n])
            if d is None:
                return True, f"{n} running but its cgroup was not found"
            dirs[n] = d
    if not dirs:
        return False, "immich containers not running"
    limit = float(b.get("immich_cpu_busy_pct", 15))
    cpu = cpu_window(dirs, float(b.get("immich_sample_s", 10)))
    if cpu is None:
        return True, "immich cgroup cpu.stat unreadable"
    hot = {n: p for n, p in cpu.items() if p > limit}
    if hot:
        n, p = max(hot.items(), key=lambda kv: kv[1])
        return True, f"{n} at {p:.0f}% of a core (limit {limit:g}%)"
    return False, "immich idle (max %.0f%% of a core)" % max(cpu.values())


def _build_verb(args: list[str], binary: str) -> bool:
    """True when the tokens after a docker-ish binary spell build/bake (or compose `up|run|create --build`)."""
    compose, buildx = binary == "docker-compose", binary == "docker-buildx"
    i = 0
    while i < len(args):
        t = args[i]
        if t.startswith("-"):
            i += 2 if t in _VALUE_OPTS else 1          # skip the option and, if it takes one, its value
        elif t in _VERB_CHAIN:
            compose, buildx = compose or t == "compose", buildx or t == "buildx"
            i += 1
        else:                                           # first real word = the verb
            if t in ("build", "bake") or (t == "b" and buildx):    # `buildx b` is the alias of `buildx build`
                return True
            return compose and t in ("up", "run", "create") and any(a.split("=")[0] == "--build" for a in args[i + 1:])
    return False


def is_docker_build(argv: list[str]) -> bool:
    """Does this command line run a docker/compose/buildx/buildctl build? Finds the binary anywhere in argv, so
    `sudo docker ...`, `timeout 600 docker ...` and `bash -c 'cd x && docker compose -f a.yml build'` count too."""
    if not any("docker" in a or "buildctl" in a for a in argv):
        return False                                    # cheap pre-filter: this runs over every process
    toks = [t.strip("\"'();") for a in argv for t in a.split()]      # a `sh -c` script is ONE argv element
    return any(os.path.basename(t) in _DOCKER_BINS and _build_verb(toks[k + 1:], os.path.basename(t))
               for k, t in enumerate(toks))


def _docker_build(b: dict) -> tuple[bool, str]:
    pats = [p for p in (b.get("build_process_patterns") or []) if not re.search(r"gradle|kotlin", p, re.I)]
    try:
        # A pattern ending in a word char must end the word: the configured `docker build` would otherwise also
        # match `docker buildx ls` and `docker builder prune` (the docker_cache cleaner's own command).
        rx = [re.compile(p + (r"(?![\w.-])" if re.search(r"\w$", p) else "")) for p in pats]
        procs = scan_procs()
    except (OSError, re.error) as exc:
        return True, f"docker build probe failed ({type(exc).__name__})"
    daemons = [p for p in procs if p.exe in ("buildkitd", "docker-init", "buildkitd-entrypoint") and "buildkitd" in p.cmd]
    dpids = {p.pid for p in daemons}
    for p in procs:
        if p.pid in dpids or p.state == "Z":
            continue
        if any(r.search(p.cmd) for r in rx) or is_docker_build(p.argv):
            return True, f"docker build process: {p.exe} (pid {p.pid})"
    # buildx's dedicated buildkitd container idles forever with its flags in argv, so presence is not busy:
    # busy means it has child processes (running RUN steps) or is burning CPU.
    bk = [p.pid for p in daemons if p.exe == "buildkitd"]
    for p in procs:
        if p.ppid in bk and p.state != "Z":
            return True, f"buildkitd has a running step ({p.exe})"
    if bk:
        cpu = procs_cpu_window(bk, float(b.get("build_sample_s", 2)))
        if cpu is None:
            return True, "buildkitd cpu unreadable"
        if cpu and max(cpu.values()) > float(b.get("build_cpu_busy_pct", 10)):
            return True, f"buildkitd using {max(cpu.values()):.0f}% of a core"
    return False, "no docker build running"


def _gradle(b: dict) -> tuple[bool, str]:
    try:
        procs = scan_procs()
    except OSError as exc:
        return True, f"gradle probe failed ({type(exc).__name__})"
    for p in procs:
        if p.state != "Z" and GRADLE_WORKERS.search(p.cmd):
            return True, f"gradle client/worker running (pid {p.pid})"
    daemons = [p.pid for p in procs if GRADLE_DAEMONS.search(p.cmd) and p.state != "Z"]
    if daemons:
        cpu = procs_cpu_window(daemons, float(b.get("gradle_sample_s", 2)))
        if cpu is None:
            return True, "gradle daemon cpu unreadable"
        top = max(cpu.values(), default=0.0)
        if top > float(b.get("gradle_cpu_busy_pct", 5)):
            return True, f"gradle/kotlin daemon using {top:.0f}% of a core"
    return False, f"no gradle build running ({len(daemons)} idle daemon(s))"


def _apt(b: dict) -> tuple[bool, str]:
    try:
        procs = scan_procs()
    except OSError as exc:
        return True, f"apt probe failed ({type(exc).__name__})"
    for p in procs:
        # argv[0], or argv[1] behind an interpreter (python3 /usr/bin/unattended-upgrade). The shutdown helper
        # daemon (unattended-upgrade-shutdown) is a different name and is not an upgrade in progress.
        if p.state == "Z":
            continue
        name = p.exe
        if re.fullmatch(r"(python|perl|sh|bash|dash)[\d.]*", name) and len(p.argv) > 1:
            name = os.path.basename(p.argv[1])
        if name in _APT_NAMES:
            return True, f"{name} running (pid {p.pid})"
    locks = read_text(PROC / "locks")
    if locks is None:
        return True, "cannot read /proc/locks"
    held = set()
    for ln in locks.splitlines():
        for tok in ln.split():
            if re.fullmatch(r"[0-9a-f]+:[0-9a-f]+:\d+", tok):
                held.add(tok)
    for path in _APT_LOCKS:
        try:
            st = os.stat(path)
        except OSError:
            continue                    # lock file absent: nothing can hold it
        if f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}" in held:
            return True, f"{path} is locked"
    return False, "no apt/dpkg process or lock"


_PROBES = {"backup": _backup, "apt": _apt, "plex": _plex, "comfyui": _comfyui, "docker_build": _docker_build,
           "gradle": _gradle, "ollama": _ollama, "immich": _immich}
_ANY_ORDER = ["backup", "apt", "plex", "comfyui", "docker_build", "gradle", "ollama", "immich"]   # cheap -> slow


def _run(probe, b: dict) -> tuple[bool, str]:
    try:
        return probe(b)
    except Exception as exc:  # noqa: BLE001 - fail closed on anything unforeseen
        return True, f"probe error: {type(exc).__name__}"


def busy(name: str, cfg: dict | None = None) -> tuple[bool, str]:
    """(is_busy, reason). Never raises; unknown names and probe errors are busy."""
    try:
        b = _busy_cfg(cfg)
    except Exception as exc:  # noqa: BLE001
        return True, f"config error: {type(exc).__name__}"
    canon = ALIASES.get(name, name)
    if canon == "any":
        # probes with sampling windows (ollama 2 s, immich 10 s) overlap instead of adding up
        with ThreadPoolExecutor(max_workers=len(_ANY_ORDER)) as ex:
            futs = {n: ex.submit(_run, _PROBES[n], b) for n in _ANY_ORDER}
        for n in _ANY_ORDER:
            is_busy, why = futs[n].result()
            if is_busy:
                return True, f"{n}: {why}"
        return False, "all gates idle"
    probe = _PROBES.get(canon)
    if probe is None:
        return True, f"unknown gate {name!r}"
    return _run(probe, b)


# --------------------------------------------------------------------------- CLI
def _record(name: str, why: str | None, now: float) -> dict:
    """Update STATE_DIR/gates.json under a lock. why=None clears the deferral record (gate was idle).
    Returns the (new) record for `name`: {"since","count","last","reason"} or {} when cleared."""
    path = STATE_DIR / "gates.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "gates.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)         # several systemd units may gate at the same moment
        data = read_json(path, {}) or {}
        if not isinstance(data, dict):
            data = {}
        if why is None:
            data.pop(name, None)
        else:
            rec = data.get(name) if isinstance(data.get(name), dict) else {}
            rec = {"since": rec.get("since", now), "count": int(rec.get("count", 0)) + 1, "last": now,
                   "reason": why[:200]}
            data[name] = rec
        write_json_atomic(path, data, 0o644)
        return data.get(name, {})


def cli_gate(name: str) -> int:
    """0 = proceed (idle, or busy for longer than max_defer_hours[name]); 1 = skip this run."""
    if ALIASES.get(name, name) not in _PROBES and name != "any":
        print(f"gate {name}: unknown gate (known: {', '.join(sorted([*_PROBES, 'any', *ALIASES]))})", file=sys.stderr)
        return 1                                  # fail closed, but do not litter gates.json with typos
    is_busy, why = busy(name)
    now = time.time()
    try:
        if not is_busy:
            _record(name, None, now)
            print(f"gate {name}: idle ({why})")
            return 0
        rec = _record(name, why, now)
    except (OSError, ValueError) as exc:
        # cannot track deferrals: stay fail-closed rather than run a disruptive job blind
        print(f"gate {name}: busy ({why}); deferral state unwritable: {exc}")
        return 1
    limit = _busy_cfg(None).get("max_defer_hours", {})
    limit = limit.get(name) if isinstance(limit, dict) else None
    if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit >= 0:
        waited_h = (now - rec.get("since", now)) / 3600
        if waited_h > limit:
            audit("gate", "defer-limit", name, 0, f"proceeding after {waited_h:.1f} h of deferral: {why}")
            try:
                _record(name, None, now)       # restart the deferral clock: a forced run is not a free pass forever
            except OSError:
                pass
            print(f"gate {name}: busy ({why}) but deferred {waited_h:.1f} h > {limit} h, proceeding")
            return 0
    print(f"gate {name}: busy ({why}), skipping (deferred {rec.get('count', 1)}x)")
    return 1
