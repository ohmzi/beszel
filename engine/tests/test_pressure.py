"""Tests for tasks/pressure.py (SPEC3 S2, the spike manager): pressure_state, pressure_response (the graduated ladder),
qos_classes, bulkhead_check, export(), and etc/classes.toml.

Nothing here touches the host. An autouse fixture makes subprocess and urllib raise, every command goes through a fake
`sh` (an unmocked command returns rc 127 and is recorded), Ollama/ComfyUI are fake functions, /proc and the cgroup tree
live under tmp_path, and `Ctx(now=...)` is the injectable clock, so nothing ever sleeps.

Layout: class map and config invariants; levels and hysteresis (pure); pressure_state; each rung of pressure_response;
the retry budget and backoff; qos_classes; bulkhead_check; export(); then three replays of recorded-like sample
sequences (a legitimate indexing spike, a runaway, an IO-only spike) driven through the same task order the check tier
uses (pressure_state, spike_sampler's record, pressure_response), plus PAUSE and report-only defaults.
"""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import json
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from homelab_maint import core
from homelab_maint.core import GIB, Ctx
from homelab_maint.tasks import gates, guard
from homelab_maint.tasks import pressure as P

MIB = 1024 ** 2
REPO = Path(__file__).resolve().parent.parent
REAL_PROTECTED = core.load_toml(REPO / "etc" / "protected.toml")
# Local-time anchor: max_per_day is a per-LOCAL-day budget, so a replay must not cross midnight.
T0 = time.mktime((2026, 10, 2, 8, 0, 0, 0, 0, -1))

# `docker ps` on this host, 2026-10-02 (name|image|compose service): the running containers, minus the retired
# maintenance-web site and its throw-away maintenance-web-test-N builds; comfyui is stopped.
REAL_CONTAINERS = """
afsaane-prod|afsaane:0.1.0|afsaane
afsaane-prod-db|postgres:17-alpine|db
afsaane-test|afsaane:0.1.0-test|afsaane-test
afsaane-test-db|postgres:17-alpine|db-test
anchor|anchor|anchor
Audiobookshelf|advplyr/audiobookshelf:latest|audiobookshelf
bazarr|lscr.io/linuxserver/bazarr|bazarr
booklore|booklore:develop|booklore
buildx_buildkit_immaculaterr-builder0|moby/buildkit:buildx-stable-1|
Deluge|linuxserver/deluge:latest|deluge
diun|crazymax/diun:4.29|diun
docker-socket-proxy|tecnativa/docker-socket-proxy:0.3.0|docker-socket-proxy
ebook2audiobook-ebook2audiobook-gpu-1|athomasson2/ebook2audiobook:cu130|ebook2audiobook-gpu
friendarr-caddy|caddy:2.11.4|caddy
friendarr|friendarr:local|friendarr
goodreads|goodreads:local|goodreads
grimmory-db|lscr.io/linuxserver/mariadb:11.4.8|grimmory-db
grimmory|ghcr.io/grimmory-tools/grimmory:v3.4.1|grimmory
homarr|homarr:develop|
hometube|ghcr.io/egalitarianmonkey/hometube:v2.9.1-yt-dlp-2026.06.09|hometube
ImmaculaterrDemo|immaculaterr-demo:local|immaculaterr-demo
ImmaculaterrHttps|caddy:2.8.4-alpine|immaculaterrhttps
Immaculaterr|immaculaterr:local|immaculaterr
immich_machine_learning|ghcr.io/immich-app/immich-machine-learning:v3.2.4-cuda|immich-machine-learning
immich_postgres|ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0|database
immich-public-proxy|alangrainger/immich-public-proxy:latest|immich-public-proxy
immich_redis|valkey/valkey:9|redis
immich_server|immich-server:v3.2.4-fork|immich-server
infinity-rerank|michaelf34/infinity:0.0.77-cpu|infinity-rerank
Jackett|linuxserver/jackett:latest|jackett
kavita|jvmilazz0/kavita:latest|kavita
kokoro|ghcr.io/remsky/kokoro-fastapi-cpu:v0.6.0|kokoro
kometa|kometa-local:a51f2d9|kometa
mariadb|lscr.io/linuxserver/mariadb:11.4.5|mariadb
nextcloud_cron|nextcloud:34.0.2-apache|cron
nextcloud|nextcloud:34.0.2-apache|app
nextcloud_postgres|postgres:17-alpine|database
nextcloud_redis|valkey/valkey:9|redis
ohmz-cloud|ohmz-cloud:local|web
omar-iqbal|omar-iqbal:local|web
omnivoice-studio-gpu|ghcr.io/debpalash/omnivoice-studio:latest|omnivoice-gpu
open-notebook-open_notebook-1|open-notebook:local|open_notebook
open-notebook-speaches-1|ghcr.io/speaches-ai/speaches:latest-cuda|speaches
open-notebook-surrealdb-1|surrealdb/surrealdb:v2|surrealdb
open-webui|ai-stack/open-webui:task-mode|
open-webui-public|ghcr.io/open-webui/open-webui|open-webui-public
owui-public-gate|nginx:alpine|owui-public-gate
owui-public-ollama|alpine/socat|owui-public-ollama
owui-public-quota|python:3.12-alpine|owui-public-quota
pa-driving-flashcards|pa-driving-flashcards:1.0.0|flashcards
portainer|portainer/portainer-ce:lts|portainer
prowlarr|lscr.io/linuxserver/prowlarr:latest|prowlarr
qbittorrent|lscr.io/linuxserver/qbittorrent:latest|qbittorrent
qdrant|e0f50bf8ac92|qdrant
Radarr|linuxserver/radarr:latest|radarr
Sabnzbd|linuxserver/sabnzbd:latest|sabnzbd
saved-vault|saved-vault:local|saved-vault
searxng-hermes|searxng/searxng|searxng-hermes
searxng|searxng/searxng|searxng
Seerr|seerr-seerr|seerr
shelfmark|ghcr.io/calibrain/shelfmark:latest|shelfmark-dev
Sonarr|linuxserver/sonarr:latest|sonarr
sonarr-status|sonarr-status-sonarr-status|sonarr-status
tautulli|ghcr.io/tautulli/tautulli|tautulli
tday_backend|ghcr.io/ohmzi/tday:v0.7.49|tday-backend
tday_db|postgres:15|database
tday_ollama|ollama/ollama:latest|ollama
tika|apache/tika:3.3.0.0-full|tika
Transmission|linuxserver/transmission:latest|transmission
trek|mauriceboe/trek:latest|app
tunarr-host-net|tunarr:gpu-softload-local|tunarr-host-net
uptime-kuma|louislam/uptime-kuma:1|
"""
REAL_CONTAINERS = [tuple(ln.split("|")) for ln in REAL_CONTAINERS.strip().splitlines()]

# `systemctl list-units --type=service --state=running` on this host, 2026-10-01.
REAL_UNITS = """accounts-daemon avahi-daemon bluetooth cloudflared colord containerd cron cups-browsed cups dbus docker
fail2ban fwupd gdm glances homelab-maint-www kerneloops ModemManager NetworkManager nvidia-persistenced ollama polkit
power-profiles-daemon rsyslog rtkit-daemon sensor-exporter smart-bridge smartmontools snapd snap.plexmediaserver.plexmediaserver
ssh switcheroo-control systemd-journald systemd-logind systemd-machined systemd-oomd systemd-resolved systemd-timesyncd
systemd-udevd tailscaled teamviewerd thermal-log udisks2 unattended-upgrades upower user@1000 virtlockd virtlogd
wpa_supplicant""".split()
REAL_UNITS = [u + ".service" for u in REAL_UNITS]

DBS = {"open-notebook-surrealdb-1", "grimmory-db", "immich_postgres", "immich_redis", "nextcloud_postgres",
       "nextcloud_redis", "afsaane-prod-db", "afsaane-test-db", "tday_db", "mariadb", "qdrant",
       "buildx_buildkit_immaculaterr-builder0"}


# =========================================================================== helpers
def cp(cmd, rc=0, out="", err=""):
    return subprocess.CompletedProcess(cmd, rc, out, err)


def cid(n: int) -> str:
    return f"{n:064x}"


def pw(points, x):
    """Piecewise-linear interpolation of [(x, y), ...] (clamped at both ends)."""
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0) if x1 > x0 else y1
    return points[-1][1]


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


def psi_text(some: float, full: float) -> str:
    return (f"some avg10={some:.2f} avg60={some:.2f} avg300={some:.2f} total=1\n"
            f"full avg10={full:.2f} avg60={full:.2f} avg300={full:.2f} total=1\n")


def mkcfg(tasks=None) -> dict:
    return {"global": {}, "caps": {}, "tasks": dict(tasks or {}), "protected": REAL_PROTECTED}


@pytest.fixture(autouse=True)
def no_live_host(monkeypatch):
    """Safety net: any real subprocess or network call made by code under test fails the test loudly."""
    def boom(*a, **k):
        raise AssertionError("test reached the live host (subprocess or network)")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", boom)
    monkeypatch.setattr(urllib.request, "urlopen", boom)


class Cont:
    """One fake container: docker's view (shares/blkio/...) plus the trajectory the sampler would record."""

    def __init__(self, name, n, image="img:1", service="", shares=0, blkio=0, resv=0, mem=0):
        self.name, self.id, self.image, self.service = name, cid(n), image, service
        self.shares, self.blkio, self.resv, self.mem = shares, blkio, resv, mem
        self.running = True
        self.anon, self.cpu_us, self.io_b = 0.3 * GIB, 0.0, 0.0
        self.prof = lambda m: (0.3, 0.01, 0.01)       # minutes -> (anon GiB, cpu cores, io MiB/s)


class World:
    """The fake host: /proc, cgroups, docker, Ollama, ComfyUI, GPU and systemd, all behind `sh` and two http functions."""

    def __init__(self, tmp: Path, mp: pytest.MonkeyPatch):
        self.tmp, self.mp = tmp, mp
        self.proc, self.cg, self.sysblock = tmp / "proc", tmp / "cgroup", tmp / "sys_block"
        self.state, self.log, self.conf = tmp / "state", tmp / "log", tmp / "conf"
        for d in (self.proc / "pressure", self.cg / "system.slice", self.sysblock, self.state, self.log, self.conf):
            d.mkdir(parents=True, exist_ok=True)
        for mod in (core, gates, guard):
            mp.setattr(mod, "STATE_DIR", self.state, raising=False)
        mp.setattr(core, "LOG_DIR", self.log)
        mp.setattr(core, "CONF_DIR", self.conf)
        mp.setattr(gates, "PROC", self.proc)
        mp.setattr(gates, "CGROUP", self.cg)
        mp.setattr(P, "SYSBLOCK", self.sysblock)
        for mod in (core, gates, guard, P):
            mp.setattr(mod, "sh", self.sh)
        mp.setattr(P, "_sleep", self._sleep)
        mp.setattr(P, "_http_get", self.http_get)
        mp.setattr(P, "_http_post", self.http_post)
        mp.setattr(gates, "busy", self._busy)
        # fake state
        self.conts: dict[str, Cont] = {}
        self.units = list(REAL_UNITS)
        self.calls: list[list[str]] = []
        self.unmocked: list[list[str]] = []
        self.muts: list[str] = []                 # every mutating call, as text: "update kavita --cpu-shares 128"
        self.posts: list[tuple] = []
        self.gpu: tuple | None = (1000, 24576)    # (used MiB, total MiB); None = nvidia-smi fails
        self.ollama: list | None = []             # list of /api/ps model dicts; None = unreachable
        self.comfy: tuple | None = None           # (running, pending); None = unreachable (stopped container)
        self.busy: dict = {}                      # gate name -> (busy, why)
        self.fail: set = set()                    # command prefixes ("update", "restart", ...) that return rc 1
        self.fail_post = False
        self.sleep_hook = lambda s: None
        self.on_restart = lambda name: None
        self.runc = P.weight_linear               # shares -> cpu.weight map of the fake runc
        self.comfy_cmd = '["python","main.py","--disable-smart-memory"]'
        self.notebook_env = "OPEN_NOTEBOOK_WORKER_MAX_TASKS=1"
        self.ollama_env = "OLLAMA_NUM_PARALLEL=2 OLLAMA_MAX_QUEUE=128 OLLAMA_KEEP_ALIVE=60s OLLAMA_MAX_LOADED_MODELS=3 PATH=/usr/bin"
        self.opts: dict = {}
        self.pswpin, self._host_t, self.oom = 1000, None, 0
        self._t = T0

    # ---- fleet -----------------------------------------------------------------------------------------------
    def add(self, name, image="img:1", service="", **kw) -> Cont:
        k = Cont(name, len(self.conts) + 1, image, service, **kw)
        self.conts[name] = k
        d = self.cg / "system.slice" / f"docker-{k.id}.scope"
        d.mkdir(parents=True, exist_ok=True)
        (d / "cpu.weight").write_text("100\n")
        return k

    def fleet(self) -> "World":
        for name, image, service in REAL_CONTAINERS:
            self.add(name, image, service)
        return self

    def c(self, name) -> Cont:
        return self.conts[name]

    def weight(self, name) -> int:
        return int((self.cg / "system.slice" / f"docker-{self.c(name).id}.scope" / "cpu.weight").read_text())

    # ---- the fake `sh` ---------------------------------------------------------------------------------------
    def sh(self, cmd, timeout=60, **kw):
        c = list(cmd) if isinstance(cmd, list) else cmd.split()
        self.calls.append(c)
        head = " ".join(c[:2])
        for pre in self.fail:
            if " ".join(c[:len(pre.split())]) == pre or (pre in ("update", "restart", "stop", "start") and c[1:2] == [pre]):
                return cp(c, 1, "", "boom")
        if c[0] == "logger":
            return cp(c)
        if c[0] == "nvidia-smi":
            return cp(c, 127, "", "no gpu") if self.gpu is None else cp(c, 0, f"{self.gpu[0]}, {self.gpu[1]}\n")
        if c[0] == "systemctl" and c[1] == "list-units":
            return cp(c, 0, "".join(f"{u} loaded active running Some unit\n" for u in self.units))
        if c[0] == "systemctl" and c[1] == "show":
            return cp(c, 0, self.ollama_env + "\n") if self.ollama_env is not None else cp(c, 1, "", "no unit")
        if head == "docker ps" and "--no-trunc" in c:
            return cp(c, 0, "".join(f"{k.id}|{k.name}|{k.image}|{k.service}\n" for k in self.conts.values() if k.running))
        if head == "docker ps" and "-a" in c:
            k = self.conts.get(c[c.index("--filter") + 1].split("^")[1].rstrip("$"))
            return cp(c, 0, ("running" if k.running else "exited") + "\n" if k else "")
        if head == "docker inspect":
            fmt = c[c.index("--format") + 1]
            names = c[c.index("--format") + 2:]
            if fmt == P.INSPECT_FMT:
                lines = [f"/{k.name}|{k.id}|{k.shares}|{k.blkio}|{k.resv}|{k.mem}" for n in names
                         if (k := self.conts.get(n))]
                return cp(c, 0 if lines else 1, "\n".join(lines) + "\n", "" if lines else "No such object")
            if "State.Running" in fmt:                                  # the start-back's existence check (--type container)
                k = self.conts.get(names[0])
                if k is None:
                    return cp(c, 1, "\n", f"Error response from daemon: No such container: {names[0]}")
                return cp(c, 0, f"{k.id}|{'true' if k.running else 'false'}\n")
            if "Config.Cmd" in fmt:
                return cp(c, 0, self.comfy_cmd + "\n") if "comfyui" in names else cp(c, 1, "", "No such object")
            if "OPEN_NOTEBOOK_WORKER_MAX_TASKS" in fmt:
                return cp(c, 0, self.notebook_env + "\n") if "open-notebook-open_notebook-1" in names else cp(c, 1)
        if head == "docker update":
            name, flags = c[-1], c[2:-1]
            k = self.conts.get(name)
            if k is None:
                return cp(c, 1, "", f"No such container: {name}")
            self.muts.append(f"update {name} {' '.join(flags)}")
            for f, v in zip(flags[::2], flags[1::2]):
                if f == "--cpu-shares" and int(v) > 0:                # `--cpu-shares 0` changes nothing, like docker
                    k.shares = int(v)
                    (self.cg / "system.slice" / f"docker-{k.id}.scope" / "cpu.weight").write_text(f"{self.runc(int(v))}\n")
                elif f == "--blkio-weight":
                    k.blkio = int(v)
                elif f == "--memory-reservation":
                    k.resv = int(v)
                elif f == "--memory":
                    k.mem = int(v)
            return cp(c)
        if head == "docker restart":
            self.muts.append(f"restart {c[-1]}")
            self.on_restart(c[-1])
            return cp(c)
        if head == "docker stop":
            self.muts.append(f"stop {c[-1]}")
            if c[-1] in self.conts:
                self.conts[c[-1]].running = False
            return cp(c)
        if head == "docker start":
            self.muts.append(f"start {c[-1]}")
            if c[-1] not in self.conts:
                return cp(c, 1, "", f"Error response from daemon: No such container: {c[-1]}")
            self.conts[c[-1]].running = True
            return cp(c)
        self.unmocked.append(c)
        return cp(c, 127, "", "not mocked")

    def kinds(self) -> set:
        return {m.split()[0] for m in self.muts}

    def killed(self) -> list:
        return [m for m in self.muts if m.split()[0] in ("restart", "stop", "kill", "rm")]

    # ---- fake http, sleep, gates ----------------------------------------------------------------------------
    def http_get(self, url, timeout=3.0):
        if url.endswith("/api/ps"):
            if self.ollama is None:
                raise OSError("connection refused")
            return {"models": [dict(m) for m in self.ollama]}
        if url.endswith("/queue"):
            if self.comfy is None:
                raise OSError("connection refused")
            return {"queue_running": [0] * self.comfy[0], "queue_pending": [0] * self.comfy[1]}
        raise OSError("unexpected GET " + url)

    def http_post(self, url, payload, timeout=8.0):
        self.posts.append((url, payload))
        self.muts.append(f"post {urlparse(url).path} {json.dumps(payload, sort_keys=True)}")
        if self.fail_post:
            raise RuntimeError("HTTP 500")
        if url.endswith("/api/generate") and self.ollama is not None:
            self.ollama = [m for m in self.ollama if m["name"] != payload["model"]]

    def _sleep(self, s):
        self.sleep_hook(s)

    def _busy(self, name, cfg=None):
        return self.busy.get(name, (False, "idle"))

    def model(self, name, expires_t, vram_gib=4.0) -> dict:
        m = {"name": name, "expires_at": iso(expires_t), "size_vram": int(vram_gib * GIB)}
        self.ollama.append(m)
        return m

    # ---- the host's numbers -----------------------------------------------------------------------------------
    def set_host(self, t, mem_some=0.2, mem_full=0.0, io_some=3.0, io_full=0.0, cpu_some=1.0, avail_gib=70.0,
                 swap_used_gib=2.0, swapin_pps=0.0, load1=2.0):
        if self._host_t is not None:
            self.pswpin += int(swapin_pps * (t - self._host_t))
        self._host_t = t
        (self.proc / "pressure" / "memory").write_text(psi_text(mem_some, mem_full))
        (self.proc / "pressure" / "io").write_text(psi_text(io_some, io_full))
        (self.proc / "pressure" / "cpu").write_text(psi_text(cpu_some, 0.0))
        (self.proc / "meminfo").write_text(
            f"MemTotal: 98608356 kB\nMemAvailable: {int(avail_gib * 1024 * 1024)} kB\n"
            f"SwapTotal: 33554428 kB\nSwapFree: {33554428 - int(swap_used_gib * 1024 * 1024)} kB\n")
        (self.proc / "vmstat").write_text(f"pswpin {self.pswpin}\npswpout 5\noom_kill {self.oom}\n")
        (self.proc / "loadavg").write_text(f"{load1:.2f} {load1:.2f} {load1:.2f} 1/100 1234\n")

    def advance(self, t):
        """Move every running container's counters to time t along its profile."""
        dt = t - self._t
        m = (t - T0) / 60
        for k in self.conts.values():
            if k.running:
                anon, cores, io = k.prof(m)
                k.anon = anon * GIB
                k.cpu_us += cores * dt * 1e6
                k.io_b += io * MIB * dt
        self._t = t

    def write_sample(self, t):
        """What spike_sampler appends for this run (guard record format)."""
        c = {k.name: {"anon": int(k.anon), "file": 0, "cur": int(k.anon), "peak": int(k.anon), "cpu_us": int(k.cpu_us),
                      "io_b": int(k.io_b)} for k in self.conts.values() if k.running}
        guard.append_sample({"t": t, "kind": "sample", "host": {}, "c": c, "p": []}, 14, t)

    # ---- running tasks ----------------------------------------------------------------------------------------
    def opt(self, task, **kw):
        self.opts.setdefault(task, {}).update(kw)

    def cfg(self) -> dict:
        return mkcfg(self.opts)

    def run(self, name, t, apply=False):
        fn = {"pressure_state": P.pressure_state, "pressure_response": P.pressure_response,
              "qos_classes": P.qos_classes, "bulkhead_check": P.bulkhead_check}[name]
        ctx = Ctx(self.cfg(), name, apply, t)
        res = fn(ctx)
        ctx.save_state()
        return res

    def tstate(self, name) -> dict:
        return core.read_json(self.state / "tasks" / f"{name}.json", {})

    def set_pstate(self, t, level=2, mem=None, io=0, cpu=0, gpu=0, spike_id=1, stall_min=0.0):
        """Write pressure_state's task state directly (isolates one rung from the level state machine)."""
        mem = level if mem is None else mem
        st = {"level": level, "t": t, "since": t, "stall_min": stall_min, "history": [], "members": {},
              "dims": {"mem": {"level": mem}, "io": {"level": io}, "cpu": {"level": cpu}, "gpu": {"level": gpu}},
              "level_name": P.LEVEL_NAMES[level]}
        if level:
            st["spike"] = {"id": spike_id, "start": t - 600, "peak": level, "dims": {}, "psi": {}, "contrib": []}
        core.write_json_atomic(self.state / "tasks" / "pressure_state.json", st, 0o600)

    def log_rows(self) -> list:
        return P._read_jsonl(P.log_path())

    def spikes(self) -> list:
        return P._read_jsonl(P.spikes_path())

    def audit(self) -> list:
        return P._read_jsonl(self.log / "audit.jsonl")

    def pause(self, name=None):
        (self.conf / (f"PAUSE.{name}" if name else "PAUSE")).write_text("paused\n")


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def check_text(*results):
    """SPEC rule 7: a Result.summary is <= 140 chars of ASCII (it can become an SMS); rows are short ASCII too."""
    for r in results:
        assert len(r.summary) <= 140 and r.summary.isascii(), r.summary
        assert len(r.items) <= 12
        for row in r.items:
            assert all(str(v).isascii() for v in row.values())
            assert len(row.get("outcome", "")) <= 100 and len(row.get("target", "")) <= 60 and len(row.get("name", "")) <= 40


def rows(res, rung=None, outcome=None):
    return [r for r in res.items if (rung is None or r["rung"] == rung)
            and (outcome is None or r["outcome"].startswith(outcome))]


# =========================================================================== class map (etc/classes.toml)
def test_registry_has_the_four_tasks_with_the_spec_classes():
    reg = core.REGISTRY
    assert (reg["pressure_state"].klass, reg["pressure_state"].tier) == ("C0", "check")
    assert (reg["pressure_response"].klass, reg["pressure_response"].tier) == ("C1", "check")
    assert (reg["qos_classes"].klass, reg["qos_classes"].tier) == ("C1", "daily")
    assert (reg["bulkhead_check"].klass, reg["bulkhead_check"].tier) == ("C0", "weekly")


def test_classes_toml_parses_and_every_real_container_has_an_explicit_class():
    cm = P.load_classes()
    assert cm.ok
    unmatched = [n for n, _i, _s in REAL_CONTAINERS if not cm.of(n)[1]]
    assert unmatched == []                                   # none falls through to the "unknown => P2" default


def test_every_real_container_matches_exactly_one_class():
    cm = P.load_classes()
    for name, _i, _s in REAL_CONTAINERS:
        hits = [c for c in P.CLASSES if any(r.search(name) for r in cm._rx["classes"][c])]
        assert len(hits) == 1, (name, hits)


def test_every_real_unit_has_an_explicit_class():
    cm = P.load_classes()
    assert [u for u in REAL_UNITS if not cm.of(u, "units")[1]] == []


def test_platform_and_serving_assignments_that_matter():
    cm = P.load_classes()
    assert cm.cls("docker-socket-proxy") == "P0"
    for u in ("docker.service", "containerd.service", "NetworkManager.service", "tailscaled.service",
              "cloudflared.service", "ssh.service", "systemd-journald.service"):
        assert cm.cls(u, "units") == "P0", u
    for n in ("Seerr", "homarr", "uptime-kuma", "immich_server", "open-webui", "nextcloud"):
        assert cm.cls(n) == "P1", n
    for u in ("snap.plexmediaserver.plexmediaserver.service", "ollama.service", "glances.service",
              "homelab-maint-www.service", "beszel-hub.service", "beszel-agent.service"):
        assert cm.cls(u, "units") == "P1", u
    for n in ("tunarr-host-net", "Sonarr", "Radarr", "kavita", "immich_machine_learning", "comfyui", "kokoro"):
        assert cm.cls(n) == "P2", n
    for n in ("afsaane-test", "ImmaculaterrDemo", "diun"):
        assert cm.cls(n) == "P3", n
    assert cm.cls("never-heard-of-it") == "P2"               # unknown => batch, never P0/P1 and not the first to be slowed
    assert cm.cls("never-heard-of.service", "units") == "P2"


def test_first_match_wins_in_protective_order():
    cm = P.ClassMap({"classes": {"P1": ["^x$"], "P3": ["^x$", "^y$"]}})
    assert cm.cls("x") == "P1" and cm.cls("y") == "P3"


def test_databases_and_builders_are_recognised_by_what_they_run():
    infos = {n: (i, s) for n, i, s in REAL_CONTAINERS}
    never = {n for n, (i, s) in infos.items() if P._never_throttle(n, i, s)}
    assert never == DBS
    # name alone is not enough: afsaane-prod-db is just "postgres" in its image
    assert P._never_throttle("something", "postgres:17-alpine", "db")
    assert not P._never_throttle("kavita", "jvmilazz0/kavita:latest", "kavita")


def _protected_by_toml(name):
    return any(re.search(p, name, re.I) for p in REAL_PROTECTED["patterns"])


def test_throttle_exemptions_cover_protected_batch_and_never_p0_p1_or_databases():
    cm = P.load_classes()
    ex = [re.compile(p, re.I) for p in cm.lad["throttle_unprotect"]]
    for name, image, service in REAL_CONTAINERS:
        cls = cm.cls(name)
        exempt = any(r.search(name) for r in ex)
        if cls in ("P0", "P1") or P._never_throttle(name, image, service):
            assert not exempt, name                            # never in reach of an L3 throttle, whatever protected.toml says
        elif _protected_by_toml(name):
            assert exempt, f"{name} is protected.toml-protected batch work; L3 would refuse it"


def test_qos_exemptions_cover_every_protected_baseline_target_and_never_p0():
    cm = P.load_classes()
    ex = [re.compile(p, re.I) for p in cm.lad["qos_unprotect"]]
    for name, image, service in REAL_CONTAINERS:
        cls = cm.cls(name)
        exempt = any(r.search(name) for r in ex)
        gets_baseline = cls in ("P1", "P3") and not (cls == "P3" and P._never_throttle(name, image, service))
        if cls == "P0":
            assert not exempt, name
        elif exempt:
            assert cls == "P1", f"{name}: qos_unprotect is for the protected P1 names (raise only)"
        elif gets_baseline:
            assert not _protected_by_toml(name), f"{name} gets a qos baseline but protected.toml would refuse it"


def test_weight_invariants_hold_under_both_runc_conversions():
    """The comment block in classes.toml promises this: P1 above docker's default weight and P3 below it, whichever
    shares->cpu.weight map the installed runc uses (<= 1.2.5 linear, >= 1.3 quadratic)."""
    cm = P.load_classes()
    assert P.weight_linear(1024) == 39 and P.weight_quadratic(1024) == 100       # the two maps, as documented
    p1, p3 = int(cm.section("defaults")["P1"]["cpu_shares"]), int(cm.section("defaults")["P3"]["cpu_shares"])
    for conv in (P.weight_linear, P.weight_quadratic):
        assert conv(p1) > 100, conv.__name__
        assert conv(p3) < 100, conv.__name__
        for cls in ("P3", "P2"):
            assert conv(int(cm.sub("throttle", cls, 0))) < 100, (conv.__name__, cls)
    fl = cm.section("floors")
    assert p3 >= fl["cpu_shares_min"] and int(cm.sub("throttle", "P3", 0)) >= fl["cpu_shares_min"]
    assert cm.section("defaults")["P3"]["blkio_weight"] >= fl["blkio_weight_min"]
    assert P._shares_for_weight_linear(100) == 2598 and P.weight_linear(2598) == 100


def test_ladder_defaults_in_classes_toml():
    cm = P.load_classes()
    assert cm.lad["emergency_stop"] == []                    # L5 does nothing until the owner names containers
    assert cm.n("max_restarts_per_6h", -1) == 2
    assert cm.n("enter_runs", -1) == 2 and cm.n("leave_runs", -1) == 3
    assert cm.n("max_stop_min", -1) == 60 and cm.n("stop_cooldown_min", -1) == 360      # an L5 stop is time-bounded
    assert cm.n("io_max_level", -1) == 3 and cm.n("cpu_max_level", -1) == 3 and cm.n("gpu_max_level", -1) == 2
    assert set(cm.section("policy")) == set(P.CLASSES)


def test_classes_toml_signal_table_matches_the_builtin_defaults():
    """The thresholds are documented in classes.toml [ladder.signals.*]; the code falls back to the same numbers when the
    file is missing. A test keeps the two from drifting apart."""
    cm = P.load_classes()
    table = cm.lad["signals"]
    assert set(table) == set(P.SIGNALS)
    for name, (dim, kind, enter, leave, mx) in P.SIGNALS.items():
        assert table[name] == {"enter": enter, "leave": leave, "max": mx}, name
    assert P.signals_cfg(cm) == P.SIGNALS                         # all eight overrides are valid, and identical


def test_missing_or_broken_classes_file_fails_closed(world):
    (world.conf / "classes.toml").write_text('[classes]\nP1 = ["(unclosed"]\n')
    cm = P.load_classes()
    assert not cm.ok                                         # one broken pattern disables the destructive rungs
    assert not P.ClassMap({}).ok and not P.ClassMap(None).ok
    # a classes file with a TOML syntax error is ignored in favour of the shipped one rather than crashing the runner
    (world.conf / "classes.toml").write_text("[classes\n")
    assert P.load_classes().ok


def test_broken_classes_refuse_l3_l4_l5_but_still_do_l2(world, monkeypatch):
    world.fleet()
    (world.conf / "classes.toml").write_text('[classes]\nP2 = ["(unclosed"]\n')
    world.opt("pressure_response", throttle="apply", restart="apply", emergency="apply")
    world.set_pstate(T0, level=5, mem=5, stall_min=60)
    world.comfy = (0, 0)
    res = world.run("pressure_response", T0, apply=True)
    refused = [r for r in res.items if "fail closed" in r["outcome"]]
    assert {r["rung"] for r in refused} == {"L3", "L4", "L5"}
    assert world.killed() == [] and "update" not in world.kinds()
    assert any(m.startswith("post /free") for m in world.muts)       # the non-destructive rung still ran


# =========================================================================== levels and hysteresis (pure)
def test_raw_levels_per_resource_and_caps():
    sigs = P.signals_cfg(P.ClassMap({}))
    caps = {"mem": 5, "io": 3, "cpu": 3, "gpu": 2}
    quiet = {"mem_full60": 0.1, "mem_some60": 1, "mem_avail_gib": 60, "swap_in_pps": 0, "io_full60": 1, "io_some60": 5,
             "cpu_some60": 2, "gpu_vram_pct": 5}
    assert {d: v["enter"] for d, v in P.raw_levels(sigs, quiet, caps).items()} == dict(mem=0, io=0, cpu=0, gpu=0)
    rl = P.raw_levels(sigs, {**quiet, "mem_full60": 16}, caps)
    assert rl["mem"]["enter"] == 4 and rl["io"]["enter"] == 0
    assert rl["mem"]["leave"] >= rl["mem"]["enter"]                       # the leave band is never stricter than enter
    assert P.raw_levels(sigs, {**quiet, "io_full60": 99, "io_some60": 99}, caps)["io"]["enter"] == 3   # io capped at L3
    assert P.raw_levels(sigs, {**quiet, "cpu_some60": 99}, caps)["cpu"]["enter"] == 3
    assert P.raw_levels(sigs, {**quiet, "gpu_vram_pct": 100}, caps)["gpu"]["enter"] == 2               # VRAM only reaches L2
    low = P.raw_levels(sigs, {**quiet, "mem_avail_gib": 4.0}, caps)["mem"]["enter"]
    assert low == 4                                                        # "lo" signal: smaller is worse (<= 5 GiB)
    assert P.raw_levels(sigs, {**quiet, "mem_avail_gib": 2.0}, caps)["mem"]["enter"] == 5


def test_unknown_signals_are_not_pressure():
    sigs = P.signals_cfg(P.ClassMap({}))
    vals = {k: None for k in sigs}                             # nvidia-smi missing, no swap baseline, no meminfo ...
    assert all(v["enter"] == 0 and v["leave"] == 0 for v in P.raw_levels(sigs, vals, {"mem": 5, "io": 3, "cpu": 3, "gpu": 2}).values())


def test_signal_overrides_are_validated_not_half_applied():
    good = {"ladder": {"signals": {"mem_full60": {"enter": [2, 4, 9, 16, 31], "leave": [1, 3, 6, 11, 21]}}}}
    assert P.signals_cfg(P.ClassMap(good))["mem_full60"][2] == [2, 4, 9, 16, 31]
    for bad in ({"enter": [2, 4, 9, 16], "leave": [1, 3, 6, 11, 21]},                 # wrong length
                {"enter": [5, 4, 9, 16, 31], "leave": [1, 3, 6, 11, 21]},              # not monotonic
                {"enter": [2, 4, 9, 16, 31], "leave": [3, 5, 10, 17, 32]},             # leave stricter than enter
                {"enter": [2, 4, 9, 16, 31], "leave": "x"}):
        got = P.signals_cfg(P.ClassMap({"ladder": {"signals": {"mem_full60": bad}}}))["mem_full60"]
        assert got == P.SIGNALS["mem_full60"]
    assert "nonsense" not in P.signals_cfg(P.ClassMap({"ladder": {"signals": {"nonsense": {}}}}))


def feed(seq, enter_runs=2, leave_runs=3, st=None):
    """Run the hysteresis over [(enter_raw, leave_raw), ...] and return the effective level after each step."""
    st = {} if st is None else st
    return [P.hysteresis(st, e, l, enter_runs, leave_runs) for e, l in seq]


def test_hysteresis_enters_after_two_consecutive_runs_not_one():
    assert feed([(3, 3), (3, 3)]) == [0, 3]
    assert feed([(3, 3), (0, 0), (3, 3), (0, 0)]) == [0, 0, 0, 0]       # a one-run blip never changes the level
    assert feed([(2, 2), (3, 3)]) == [0, 2]                              # enters at the LOWER of the last two
    assert feed([(4, 4), (4, 4), (1, 4), (1, 4)])[:2] == [0, 4]


def test_hysteresis_leaves_only_after_three_runs_below_the_leave_band():
    # enter L3, then the enter-level drops at once but the (lower) leave thresholds still hold L2 for a while
    assert feed([(3, 3), (3, 3)] + [(0, 2)] * 2 + [(0, 0)] * 3) == [0, 3, 3, 3, 2, 2, 0]
    lv = feed([(3, 3), (3, 3), (1, 1), (1, 1), (1, 1), (0, 0), (0, 0), (0, 0)])
    assert lv == [0, 3, 3, 3, 1, 1, 1, 0]                                # steps down 3 -> 1 -> 0, 3 quiet runs each


def test_hysteresis_band_prevents_flapping_at_a_threshold():
    """A signal hovering around an enter threshold: enter_raw alternates 3/2 but leave_raw stays 3 (the lower leave
    threshold is still crossed), so the level climbs once and then holds."""
    lv = feed([(3, 3), (3, 3)] + [(2, 3), (3, 3)] * 6)
    assert lv[1] == 3 and set(lv[1:]) == {3}
    # and it does not climb at all when it only touches the enter threshold every other run
    assert set(feed([(3, 3), (2, 3)] * 8)) <= {0, 2}


def test_hysteresis_climbs_in_steps_and_drops_in_steps():
    assert feed([(1, 1), (1, 1), (3, 3), (3, 3), (5, 5), (5, 5)]) == [0, 1, 1, 3, 3, 5]
    st: dict = {}
    feed([(5, 5), (5, 5)], st=st)
    assert feed([(2, 2), (2, 2), (2, 2)], st=st) == [5, 5, 2]


def test_hysteresis_window_is_configurable():
    assert feed([(2, 2)] * 3, enter_runs=3) == [0, 0, 2]
    assert feed([(2, 2)] * 2 + [(0, 0)] * 5, leave_runs=5)[-1] == 0
    assert feed([(2, 2)] * 2 + [(0, 0)] * 4, leave_runs=5)[-1] == 2


# =========================================================================== host readers
def test_read_host_parses_proc_and_computes_swap_in_rate(world):
    world.set_host(T0, mem_some=1.5, mem_full=0.7, io_some=55.0, io_full=47.0, cpu_some=0.1, avail_gib=69.0,
                   load1=30.0)
    ctx = Ctx(mkcfg(), "pressure_state", False, T0)
    h = P.read_host(ctx)
    v = h["vals"]
    assert v["mem_full60"] == 0.7 and v["mem_some60"] == 1.5 and v["io_full60"] == 47.0 and v["cpu_some60"] == 0.1
    assert v["mem_avail_gib"] == pytest.approx(69.0) and v["swap_in_pps"] is None       # no baseline on the first run
    assert v["gpu_vram_pct"] == pytest.approx(1000 / 24576 * 100)
    assert h["load1"] == 30.0 and h["load_ratio"] == round(30.0 / P._cores(), 2)
    world.set_host(T0 + 600, swapin_pps=500)
    ctx2 = Ctx(mkcfg(), "pressure_state", False, T0 + 600)
    ctx2.state = ctx.state
    assert P.read_host(ctx2)["vals"]["swap_in_pps"] == pytest.approx(500, abs=1)
    # a swap relief the owner ran on purpose inside the window is not pressure: its pages come off the rate
    import homelab_maint.swapwatch as SW
    world.set_host(T0 + 1200, swapin_pps=500)
    ctx2b = Ctx(mkcfg(), "pressure_state", False, T0 + 1200)
    ctx2b.state = {"vm": {"t": T0 + 600, "pswpin": 0}}
    raw = P.read_host(ctx2b)["vals"]["swap_in_pps"]
    assert raw > 100, "control: with no ledger the rate is real"
    ctx2c = Ctx(mkcfg(), "pressure_state", False, T0 + 1200)
    ctx2c.state = {"vm": {"t": T0 + 600, "pswpin": 0}}
    pages = int(raw * 600)
    SW.core.write_json_atomic(SW._ledger_path(), {"done": [{"t0": T0 + 650, "t1": T0 + 1100, "pages_in": pages}]}, 0o644)
    try:
        assert P.read_host(ctx2c)["vals"]["swap_in_pps"] == pytest.approx(0, abs=0.01)
    finally:
        SW._ledger_path().unlink(missing_ok=True)
    ctx3 = Ctx(mkcfg(), "pressure_state", False, T0 + 600 + 30)           # two runs 30 s apart tell nothing
    ctx3.state = ctx2.state
    world.set_host(T0 + 630)
    assert P.read_host(ctx3)["vals"]["swap_in_pps"] is None
    ctx4 = Ctx(mkcfg(), "pressure_state", False, T0 + 600 + 4 * 3600)    # ... and neither does a stale baseline
    ctx4.state = {"vm": {"t": T0, "pswpin": 0}}
    world.set_host(T0 + 600 + 4 * 3600)
    assert P.read_host(ctx4)["vals"]["swap_in_pps"] is None


def test_read_host_none_without_psi_and_gpu_failure_is_unknown(world):
    ctx = Ctx(mkcfg(), "pressure_state", False, T0)
    assert P.read_host(ctx) is None                              # nothing written yet: PSI unreadable
    world.set_host(T0)
    world.gpu = None
    assert P.read_host(ctx)["vals"]["gpu_vram_pct"] is None
    res = P.pressure_state(Ctx(mkcfg(), "pressure_state", False, T0))
    assert res.status == "ok"
    (world.proc / "pressure" / "memory").unlink()
    res = P.pressure_state(Ctx(mkcfg(), "pressure_state", False, T0 + 900))
    assert res.status == "error" and "cannot read /proc/pressure" in res.summary


# =========================================================================== pressure_state
def run_state(world, t, **host):
    world.set_host(t, **host)
    return world.run("pressure_state", t)


def test_pressure_state_level_zero_on_a_quiet_host(world):
    world.fleet()
    res = run_state(world, T0)
    assert res.status == "ok" and res.metrics["level"] == 0 and res.metrics["why"] == "no pressure"
    assert res.summary.startswith("L0 normal") and "GiB avail" in res.summary
    assert res.alert is False and res.metrics["top_contributors"] == []      # level 0: nothing to page
    assert world.spikes() == [] and "spike" not in world.tstate("pressure_state")


def test_pressure_state_hysteresis_over_real_runs_with_an_injected_clock(world):
    """enter at X for 2 consecutive runs, leave at Y for 3: levels over 15-minute runs."""
    world.fleet()
    seq = [(0.1, 0.0), (4.0, 4.0), (4.0, 4.0), (4.0, 4.0), (0.1, 0.0), (0.1, 0.0), (0.1, 0.0), (0.1, 0.0)]
    levels = []
    for i, (some, full) in enumerate(seq):
        res = run_state(world, T0 + i * 900, mem_some=some, mem_full=full)
        levels.append(res.metrics["level"])
    # full60 = 4 is memory L2; the first elevated run changes nothing, the second enters, three quiet runs leave
    assert levels == [0, 0, 2, 2, 2, 2, 0, 0]
    st = world.tstate("pressure_state")
    assert st["since"] == T0 + 6 * 900                      # `since` is when the level last changed
    assert [h["level"] for h in st["history"]] == levels and st["history"][2]["t"] == round(T0 + 2 * 900)


def test_pressure_state_status_alert_and_level_mapping(world):
    world.fleet()

    def settle(**host):
        run_state(world, T0, **host)
        return run_state(world, T0 + 900, **host)

    r = settle(mem_full=1.5)                                            # memory L1: annotate only
    assert (r.metrics["level"], r.status, r.alert) == (1, "info", False)
    r = settle(mem_full=5.0)                                            # memory L2
    assert (r.metrics["level"], r.status, r.alert) == (2, "warn", True)
    r = settle(mem_full=16.0)                                           # memory L4
    assert (r.metrics["level"], r.status, r.alert) == (4, "crit", True)


def test_pressure_state_io_only_pressure_never_pages_and_has_no_memory_level(world):
    """Right now this host runs io PSI full ~47% (find on a spinning disk) with memory PSI ~0 and load 33."""
    world.fleet()
    run_state(world, T0, io_some=56.0, io_full=47.0, load1=33.0)
    r = run_state(world, T0 + 900, io_some=56.0, io_full=47.0, load1=33.0)
    assert r.metrics["dims"] == {"mem": 0, "io": 3, "cpu": 0, "gpu": 0}
    assert r.metrics["level"] == 3 and r.status == "info" and r.alert is False        # dashboard only: no page, no warn
    assert "io stall" in r.metrics["why"] and r.metrics["load_ratio"] > 1


def test_pressure_state_items_list_the_top_contributors_for_the_checks_table(world):
    """etc/playbooks.toml sends the owner to "the top contributors in the pressure_state items"."""
    world.fleet()
    world.c("tunarr-host-net").prof = lambda m: (pw([(0, 3), (60, 12)], m), 2.5, 30.0)
    for i in range(4):
        t = T0 + i * 900
        world.advance(t)
        r = run_state(world, t, mem_some=24.0, mem_full=2.0)
        world.write_sample(t)
    assert r.items and r.items[0]["name"] == "tunarr-host-net" and r.items == r.metrics["top_contributors"]
    assert all(isinstance(v, (str, int, float)) for row in r.items for v in row.values())          # scalars only
    assert run_state(world, T0 + 9 * 900).items == []


def test_pressure_state_load_average_alone_is_not_pressure(world):
    world.fleet()
    run_state(world, T0, load1=60.0)
    r = run_state(world, T0 + 900, load1=60.0)                          # 30 tasks in D state inflate the load average
    assert r.metrics["level"] == 0 and r.metrics["load_ratio"] > 2


def test_pressure_state_low_available_memory_and_swap_in_drive_the_memory_level(world):
    world.fleet()
    t = [T0 - 900]

    def two(**host):                                         # two consecutive runs with the same numbers
        for _ in range(2):
            t[0] += 900
            r = run_state(world, t[0], **host)
        return r

    r = two(avail_gib=3.5)
    assert r.metrics["dims"]["mem"] == 4 and r.status == "crit" and "GiB avail" in r.metrics["why"]    # <= 5 GiB: L4
    assert two(avail_gib=2.5).metrics["dims"]["mem"] == 5                                              # <= 3 GiB: L5
    for _ in range(4):                                       # recover (3 quiet runs per step down)
        two()
    # swap USED is not pressure (this host sits at 24 GiB of swap with 70 GiB available); swap-IN rate is
    r = two(swap_used_gib=24.0)
    assert r.metrics["dims"]["mem"] == 0 and r.metrics["level"] == 0
    r = two(swap_used_gib=24.0, swapin_pps=3000)
    assert r.metrics["swap_in_pps"] == 3000 and r.metrics["dims"]["mem"] == 3                         # swap-in alone caps at L3


def test_pressure_state_gpu_memory_reaches_l2_at_most(world):
    world.fleet()
    world.gpu = (24000, 24576)
    run_state(world, T0)
    r = run_state(world, T0 + 900)
    assert r.metrics["dims"]["gpu"] == 2 and r.metrics["level"] == 2 and r.metrics["gpu_vram_pct"] == 98
    assert r.alert is False                                              # a full card is routine with Plex+Immich+ComfyUI


def test_pressure_state_stall_clock_for_l5(world):
    world.fleet()
    for i in range(5):
        r = run_state(world, T0 + i * 900, mem_full=20.0)
    assert r.metrics["dims"]["mem"] == 4 and r.metrics["stall_min"] == pytest.approx((4 * 900 - 900) / 60, abs=0.1)
    run_state(world, T0 + 5 * 900, mem_full=0.0)
    run_state(world, T0 + 6 * 900, mem_full=0.0)
    r = run_state(world, T0 + 7 * 900, mem_full=0.0)
    assert r.metrics["stall_min"] == 0 and "mem4_since" not in world.tstate("pressure_state")


def test_spike_is_recorded_once_with_contributors_class_and_outcome(world):
    world.fleet()
    tu = world.c("tunarr-host-net")
    tu.prof = lambda m: (pw([(0, 3), (60, 12)], m), 2.5, 30.0)           # growing, busy: the textbook indexing spike
    for i in range(14):
        t = T0 + i * 900
        world.advance(t)
        busy = 2 <= i <= 6
        run_state(world, t, mem_some=24.0 if busy else 0.2, mem_full=2.0 if busy else 0.0)
        world.write_sample(t)
    assert world.tstate("pressure_state").get("spike") is None            # closed
    sp = world.spikes()
    assert len(sp) == 1 and sp[0]["state"] == "closed" and sp[0]["kind"] == "spike"
    assert sp[0]["peak_level"] == 2 and sp[0]["duration_s"] >= 900 and sp[0]["dims"]["mem"] == 2
    assert sp[0]["psi"]["mem_some60"] == 24.0 and sp[0]["oom_kills"] == 0
    assert sp[0]["nothing_killed"] is True and "nothing killed" in sp[0]["outcome"]
    top = sp[0]["contributors"][0]
    assert top["name"] == "tunarr-host-net" and top["class"] == "P2" and set(top) == {"name", "class", "anon_gib", "cpu_pct"}
    assert top["cpu_pct"] > 100 and top["anon_gib"] > 3


def test_spike_oom_kills_during_the_spike_are_counted_and_flag_harm(world):
    world.fleet()
    for i in range(2):
        run_state(world, T0 + i * 900, mem_full=5.0)
    world.oom += 2
    for i in range(2, 8):
        run_state(world, T0 + i * 900, mem_full=0.0)
    sp = world.spikes()[0]
    assert sp["oom_kills"] == 2 and sp["nothing_killed"] is False


def test_spike_outcome_names_what_the_ladder_did(world):
    world.fleet()
    for i in range(2):
        run_state(world, T0 + i * 900, mem_full=5.0)
    for r in ({"rung": "L2", "outcome": "done"}, {"rung": "L4", "outcome": "done", "target": "kavita"},
              {"rung": "L3", "outcome": "would"}):
        P._append_jsonl(P.log_path(), {"ts": T0 + 1000, "level": 2, "action": "x", "target": "t", "class": "P2", **r})
    for i in range(2, 8):
        run_state(world, T0 + i * 900, mem_full=0.0)
    sp = world.spikes()[0]
    assert sp["restarted"] == ["kavita"] and sp["nothing_killed"] is False and sp["outcome"].startswith("handled: restarted kavita")
    assert sp["reclaimed"] == 1 and sp["would"] == 1


def test_one_blip_run_is_not_a_spike(world):
    world.fleet()
    levels = [run_state(world, T0 + i * 900, mem_full=20.0 if i == 3 else 0.0).metrics["level"] for i in range(8)]
    assert set(levels) == {0} and world.spikes() == []


def test_a_very_long_spike_is_recorded_in_slices(world):
    """This host can sit at io PSI 50% for hours while a scan runs; reports read closed records, so a spike is
    cut every spike_max_hours (6) and a new one opens at once while the pressure continues."""
    world.fleet()
    for i in range(34):                                          # 8.5 hours of io pressure at 15-minute runs
        run_state(world, T0 + i * 900, io_some=90.0, io_full=60.0)
    sp = world.spikes()
    assert [s["duration_s"] for s in sp] == [6 * 3600]           # one full slice so far ...
    assert sp[0]["peak_level"] == 3 and sp[0]["dims"]["io"] == 3 and sp[0]["id"] != world.tstate("pressure_state")["spike"]["id"]
    ex = P.export(T0 + 34 * 900)
    assert [s["state"] for s in ex["spikes"]] == ["open", "closed"]      # ... and the continuation is shown as in progress
    for i in range(34, 44):
        run_state(world, T0 + i * 900, io_some=2.0, io_full=0.0)
    assert [s["state"] for s in world.spikes()] == ["closed", "closed"]


def test_pressure_state_writes_members_for_the_class_table(world):
    world.fleet()
    run_state(world, T0)
    mem = world.tstate("pressure_state")["members"]
    assert "docker-socket-proxy" in mem["P0"] and "docker.service" in mem["P0"]
    assert "ollama.service" in mem["P1"] and "tunarr-host-net" in mem["P2"] and "diun" in mem["P3"]
    assert sum(len(v) for v in mem.values()) == len(REAL_CONTAINERS) + len(REAL_UNITS)


def test_pressure_state_is_registered_c0_and_runs_through_the_runner(world, monkeypatch):
    """core.run_task forces C0 tasks read-only and handles state saving; the real clock is fine here."""
    world.fleet()
    world.set_host(time.time())
    res, _dur = core.run_task(core.REGISTRY["pressure_state"], mkcfg(), apply=True)
    assert res.status == "ok" and res.metrics["level"] == 0 and world.muts == []
    assert world.tstate("pressure_state")["level"] == 0


# =========================================================================== who is causing it (contributors, helpers)
def sample_rec(t, **c):
    return {"t": t, "kind": "sample", "host": {}, "p": [["java", 7, 3 * 1024 * 1024]],
            "c": {n: dict(anon=int(v[0] * GIB), cpu_us=int(v[1]), io_b=int(v[2] * MIB)) for n, v in c.items()}}


def test_activity_computes_cpu_io_and_growth_rates_from_guard_samples(world):
    guard.append_sample(sample_rec(T0, a=(2.0, 0, 0), b=(5.0, 1e6, 10)), 14, T0)
    assert set(P.activity(T0)) == {"a", "b", "\0host"}                     # one sample: shown, but no rates
    assert P.activity(T0)["a"]["cpu_pct"] == 0.0 and P.activity(T0)["a"]["growth"] == 0.0
    guard.append_sample(sample_rec(T0 + 600, a=(8.0, 6e8, 600), b=(5.0, 1e6 + 6e6, 10)), 14, T0 + 600)
    act = P.activity(T0 + 600)
    assert act["a"]["cpu_pct"] == pytest.approx(100.0) and act["a"]["io_mib_s"] == pytest.approx(1.0)
    assert act["a"]["growth"] == pytest.approx(36.0) and act["b"]["growth"] == 0.0 and act["b"]["cpu_pct"] == pytest.approx(1.0)
    assert act["\0host"]["p"][0][0] == "java"


def test_activity_ignores_counters_that_went_backwards_and_missing_samples(world):
    assert P.activity(T0) == {}                                          # no samples at all
    guard.append_sample(sample_rec(T0, a=(2.0, 9e9, 900)), 14, T0)
    guard.append_sample(sample_rec(T0 + 600, a=(2.0, 100, 1)), 14, T0 + 600)   # the container restarted in between
    a = P.activity(T0 + 600)["a"]
    assert a["cpu_pct"] == 0.0 and a["io_mib_s"] == 0.0


def test_contributors_pick_by_the_resource_that_is_under_pressure():
    cm = P.load_classes()
    act = {n: {"anon": int(a * GIB), "swap": 0, "cpu_pct": c, "io_mib_s": i, "growth": g}
           for n, (a, c, i, g) in {"leaker": (9, 0.1, 0.0, 30.0), "big": (40, 0.5, 0.0, 0.0), "busy": (1, 300.0, 0.0, 0.0),
                                   "scanner": (1, 2.0, 120.0, 0.0), "tiny": (0.2, 0.1, 0.0, 0.0)}.items()}
    act["\0host"] = {"p": [["java", 7, 5 * 1024 * 1024]]}
    mem = P.contributors(act, cm, {"mem": 2})
    assert [c["name"] for c in mem][:2] == ["leaker", "big"] and mem[-1]["class"] == "host" and mem[-1]["anon_gib"] == 5.0
    assert [c["name"] for c in P.contributors(act, cm, {"io": 3})][0] == "scanner"
    assert [c["name"] for c in P.contributors(act, cm, {"cpu": 3})][0] == "busy"
    assert all(set(c) == {"name", "class", "anon_gib", "cpu_pct"} for c in mem)
    assert P.contributors({}, cm, {"mem": 2}) == []
    assert P.contributors({"\0host": {}}, cm, {"mem": 1}) == []


def test_io_pressure_with_no_container_moving_data_names_the_host_tasks_in_io_wait(world):
    """Right now this host stalls on a `sha256sum`/`find` run from a shell, not a container: say that, and do not
    blame the biggest container just because it is big."""
    world.fleet()
    world.c("tunarr-host-net").prof = lambda m: (15.0, 0.1, 0.01)           # large and quiet
    for pid, (comm, st) in enumerate([("sha256sum", "D"), ("sha256sum", "D"), ("find", "D"), ("bash", "S")], start=300):
        d = world.proc / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} ({comm}) {st} 1 {pid} {pid} 0 -1 4194304 0 0 0 0 5 0 0 0 20 0 1 0 100 1000 100")
        (d / "cmdline").write_text(comm + "\0")
    for i in range(4):
        t = T0 + i * 900
        world.advance(t)
        r = run_state(world, t, io_some=60.0, io_full=47.0)
        world.write_sample(t)
    assert r.metrics["dims"]["io"] == 3 and r.metrics["dims"]["mem"] == 0
    assert [(c["name"], c["class"]) for c in r.items] == [("sha256sum", "host"), ("find", "host")]
    assert r.metrics["d_state"] == ["sha256sum x2", "find x1"] and "tunarr" not in r.summary


def test_parse_ts_handles_ollama_formats_and_garbage():
    t = P._parse_ts("2026-10-02T09:00:00-04:00")
    assert t == datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc).timestamp()
    assert P._parse_ts("2026-10-02T09:00:00.123456789-04:00") == pytest.approx(t + 0.123456)   # nanosecond precision
    assert P._parse_ts("2026-10-02T13:00:00Z") == t and P._parse_ts("2026-10-02T13:00:00") == t
    assert P._parse_ts("0001-01-01T00:00:00Z") < 0                                              # "never expires" zero time
    for bad in (None, "", "soon", "2026-13-45T00:00:00Z", 12345):
        assert P._parse_ts(bad) is None


def test_d_state_names_the_tasks_stuck_in_io_wait(world):
    def proc(pid, comm, state):
        d = world.proc / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} ({comm}) {state} 1 {pid} {pid} 0 -1 4194304 0 0 0 0 5 0 0 0 20 0 1 0 100 1000 100")
        (d / "cmdline").write_text("" if comm.startswith("jbd2") else comm + "\0")      # kernel threads have no cmdline
    for i, (comm, st) in enumerate([("find", "D"), ("find", "D"), ("du", "D"), ("python3", "S"), ("jbd2/sda1-8", "D")]):
        proc(100 + i, comm, st)
    assert P._d_state() == ["find x2", "du x1", "jbd2/sda1-8 x1"]


def test_members_is_none_when_docker_is_down_and_pressure_state_keeps_the_old_table(world):
    world.fleet()
    run_state(world, T0)
    before = world.tstate("pressure_state")["members"]
    world.fail.add("docker ps")
    assert P.members(P.load_classes()) is None
    r = run_state(world, T0 + 900)
    assert r.status == "ok" and world.tstate("pressure_state")["members"] == before


def test_pressure_state_without_loadavg_or_gpu_still_works(world):
    world.fleet()
    world.set_host(T0)
    (world.proc / "loadavg").unlink()
    world.gpu = None
    r = world.run("pressure_state", T0)
    assert r.status == "ok" and r.metrics["load1"] is None and r.metrics["load_ratio"] is None and r.metrics["gpu_vram_pct"] is None


def test_no_watching_rows_are_ever_logged(world):
    runaway_world(world)
    world.opt("pressure_response", restart="apply")
    out = drive_runaway(world, "kavita", 150, apply=True)
    assert any(r["outcome"].startswith("watching") for _m, _s, rs in out for r in rs.items)    # the run itself shows it
    assert not any(r["outcome"].startswith("watching") for r in world.log_rows())               # the log does not


# =========================================================================== mode defaults
def test_defaults_report_only_except_reclaim():
    for apply_, tcfg, want in (
            (True, {}, {"reclaim": "apply", "throttle": "report", "restart": "report", "emergency": "report"}),
            (False, {}, {"reclaim": "report", "throttle": "report", "restart": "report", "emergency": "report"}),
            (True, {"mode": "report"}, dict.fromkeys(("reclaim", "throttle", "restart", "emergency"), "report")),
            (True, {"restart": "apply", "emergency": "apply", "throttle": "apply"},
             {"reclaim": "apply", "throttle": "apply", "restart": "apply", "emergency": "apply"}),
            (False, {"restart": "apply", "throttle": "apply"}, dict.fromkeys(("reclaim", "throttle", "restart", "emergency"), "report")),
            (True, {"reclaim": "off", "restart": "off"}, {"reclaim": "off", "throttle": "report", "restart": "off", "emergency": "report"}),
            (True, {"restart": "yes please", "throttle": 1}, {"reclaim": "apply", "throttle": "report", "restart": "report", "emergency": "report"}),
    ):
        assert P._modes(Ctx(mkcfg({"pressure_response": tcfg}), "pressure_response", apply_, T0)) == want, (apply_, tcfg)
    assert P.RUNG_DEFAULTS == {"reclaim": "apply", "throttle": "report", "restart": "report", "emergency": "report"}


def test_pause_forces_every_rung_to_report(world):
    world.pause()
    m = P._modes(Ctx(mkcfg({"pressure_response": {"throttle": "apply", "restart": "apply"}}), "pressure_response", True, T0))
    assert set(m.values()) == {"report"}
    (world.conf / "PAUSE").unlink()
    world.pause("pressure_response")                         # the per-task kill switch works the same way
    m = P._modes(Ctx(mkcfg(), "pressure_response", True, T0))
    assert set(m.values()) == {"report"}


def test_with_default_modes_and_apply_only_reclaim_changes_anything_even_when_every_rung_has_work(world):
    """The default config at the worst level with --apply: a runaway candidate (L4), an active P3 (L3) and an
    emergency list (L5) all have work to do and each is only logged as "would"; only the L2 reclaim touches the host."""
    world.fleet()
    world.c("kavita").prof = lambda m: (pw([(0, 3), (45, 3), (300, 60)], m), 0.002, 0.002)       # leaking, no progress
    world.c("ImmaculaterrDemo").prof = lambda m: (1.0, 1.5, 0.1)                                  # an active P3
    world.comfy = (0, 0)
    world.model("qwen3:8b", T0 + 6 * 3600)
    (world.conf / "classes.toml").write_text((REPO / "etc" / "classes.toml").read_text()
                                             .replace("emergency_stop = []", 'emergency_stop = ["diun"]'))
    for m in range(0, 151, 15):
        world.advance(T0 + m * 60)
        world.write_sample(T0 + m * 60)
    t_end = T0 + 150 * 60
    results = []
    for k in range(3):
        world.set_pstate(t_end + k * 400, level=5, mem=5, stall_min=45)
        results.append(world.run("pressure_response", t_end + k * 400, apply=True))
    res = results[-1]
    assert res.metrics["mode_reclaim"] == "apply" and res.metrics["mode_throttle"] == "report"
    assert res.metrics["mode_restart"] == "report" and res.metrics["mode_emergency"] == "report"
    assert world.kinds() == {"post"}                          # the reclaim ran; no docker update/restart/stop/start at all
    would = {(r["rung"], r["target"]) for r in res.items if r["outcome"] == "would"}
    assert {("L3", "ImmaculaterrDemo"), ("L4", "kavita"), ("L5", "diun")} <= would
    dry = {(x["action"], x["outcome"]) for x in world.audit() if x["action"] in ("docker-update-throttle", "docker restart", "docker stop")}
    assert dry == {("docker-update-throttle", "dry-run"), ("docker restart", "dry-run"), ("docker stop", "dry-run")}
    assert world.c("diun").running and world.c("kavita").shares == 0


# =========================================================================== L1 annotate / stale / level 0
def test_level_zero_and_stale_state_do_nothing(world):
    world.fleet()
    world.set_pstate(T0, level=0)
    res = world.run("pressure_response", T0, apply=True)
    assert res.status == "ok" and res.summary == "L0 normal: nothing to do" and world.muts == [] and res.items == []
    world.set_pstate(T0 - 3600, level=5, mem=5, stall_min=60)          # a pressure_state that stopped running 1 h ago
    res = world.run("pressure_response", T0, apply=True)
    assert res.metrics["stale"] is True and res.metrics["level"] == 0 and world.muts == []
    (world.state / "tasks" / "pressure_state.json").unlink()            # never ran at all
    assert world.run("pressure_response", T0, apply=True).metrics["stale"] is True


def test_l1_annotates_once_per_spike_and_acts_on_nothing(world):
    world.fleet()
    world.set_pstate(T0, level=1, mem=1, spike_id=77)
    r1 = world.run("pressure_response", T0, apply=True)
    assert [r["rung"] for r in r1.items] == ["L1"] and r1.items[0]["action"] == "annotate" and world.muts == []
    world.set_pstate(T0 + 900, level=1, mem=1, spike_id=77)
    assert world.run("pressure_response", T0 + 900, apply=True).items == []        # same spike: not repeated
    world.set_pstate(T0 + 1800, level=1, mem=1, spike_id=78)
    assert [r["action"] for r in world.run("pressure_response", T0 + 1800, apply=True).items] == ["annotate"]
    assert {r["action"] for r in world.log_rows()} == {"annotate"} and len(world.log_rows()) == 2


def test_explanatory_rows_are_logged_once_per_spike_not_every_run(world):
    world.fleet()
    for i in range(6):                                        # io-only L3 for 90 minutes: one "no lever" row, not six
        world.set_pstate(T0 + i * 900, level=3, mem=0, io=3, spike_id=5)
        res = world.run("pressure_response", T0 + i * 900, apply=True)
        assert any("no lever" in r["outcome"] for r in res.items)          # the Result always explains itself ...
    assert sum("no lever" in r["outcome"] for r in world.log_rows()) == 1  # ... the persistent log does not repeat
    world.set_pstate(T0 + 9000, level=0)
    world.run("pressure_response", T0 + 9000, apply=True)
    world.set_pstate(T0 + 9900, level=3, mem=0, io=3, spike_id=6)
    world.run("pressure_response", T0 + 9900, apply=True)
    assert sum("no lever" in r["outcome"] for r in world.log_rows()) == 2  # a new spike is explained again


# =========================================================================== L2 reclaim
def l2(world, t, apply=True, **modes):
    world.opt("pressure_response", **modes)
    world.set_pstate(t, level=2, mem=2)
    return world.run("pressure_response", t, apply=apply)


def test_l2_unloads_an_idle_ollama_model_after_it_has_been_idle_long_enough(world):
    world.fleet()
    world.model("qwen3:8b", T0 + 3600)                          # expires_at never moves: idle
    r = l2(world, T0)
    assert world.muts == [] and not rows(r, "L2")               # first sight: the idle clock starts, nothing unloaded
    r = l2(world, T0 + 120)
    assert world.muts == []                                      # idle for 2 min < idle_s (300)
    r = l2(world, T0 + 330)
    assert world.muts == ['post /api/generate {"keep_alive": 0, "model": "qwen3:8b"}']
    (row,) = rows(r, "L2")
    assert row["outcome"] == "done" and row["target"] == "ollama:qwen3:8b" and row["class"] == "P1" and "VRAM" in row["action"]
    assert world.ollama == []
    a = [x for x in world.audit() if x["action"] == "ollama-unload"]
    assert a and a[-1]["outcome"] == "done" and a[-1]["bytes"] == 0   # protected.toml says "ollama": the exemption worked


def test_l2_never_touches_a_model_that_is_in_use(world):
    world.fleet()
    world.model("busy-model", T0 + 3000)
    world.model("inflight-model", T0 - 10)                      # expires_at already past: a request is running/unloading
    world.model("steady-model", T0 + 3000)

    def tick(t):                                                 # busy-model's expires_at moves on every probe (use)
        world.ollama[0]["expires_at"] = iso(t + 3000)

    for t in (T0, T0 + 330, T0 + 660, T0 + 990):
        tick(t)
        l2(world, t)
    assert [m.split(" ", 2)[1:] for m in world.muts] == [["/api/generate", '{"keep_alive": 0, "model": "steady-model"}']]
    assert {m["name"] for m in world.ollama} == {"busy-model", "inflight-model"}


def test_l2_second_probe_vetoes_a_model_that_became_active_during_the_gap(world):
    world.fleet()
    world.model("m", T0 + 3000)
    l2(world, T0)

    def wake(_s):
        world.ollama[0]["expires_at"] = iso(T0 + 9999)           # a request arrived between the two probes
    world.sleep_hook = wake
    r = l2(world, T0 + 400)
    assert world.muts == [] and not rows(r, "L2", "done")


def test_l2_ollama_cpu_gate_vetoes_unloading_while_it_generates(world):
    world.fleet()
    world.model("m", T0 + 3000)
    l2(world, T0)
    world.busy["ollama"] = (True, "Ollama using 80% of a core")
    l2(world, T0 + 400)
    assert world.muts == []
    world.busy.clear()
    l2(world, T0 + 800)
    assert len(world.muts) == 1


def test_l2_unreachable_or_garbled_ollama_means_do_nothing(world):
    world.fleet()
    world.ollama = None
    assert l2(world, T0).status in ("ok", "info") and world.muts == []
    real_get = world.http_get

    def garbled(url, timeout=3.0):
        return {"models": "nope"} if url.endswith("/api/ps") else real_get(url, timeout)
    world.mp.setattr(P, "_http_get", garbled)
    assert l2(world, T0 + 400).status in ("ok", "info") and world.muts == []


def test_l2_report_modes_log_would_and_change_nothing(world):
    world.fleet()
    world.model("m", T0 + 3000)
    world.comfy = (0, 0)
    for apply_, modes in ((True, {"reclaim": "report"}), (False, {})):
        world.muts.clear()
        l2(world, T0, apply=apply_, **modes)
        r = l2(world, T0 + 400, apply=apply_, **modes)
        assert world.muts == [], (apply_, modes)
        assert {x["outcome"] for x in rows(r, "L2")} == {"would"}
    assert [x["outcome"] for x in world.audit() if x["action"] in ("ollama-unload", "comfyui-free")][-1] == "dry-run"


def test_l2_comfyui_free_only_with_an_empty_queue_seen_twice(world):
    world.fleet()
    for q in ((1, 0), (0, 2), None):                            # running job, pending job, container stopped
        world.comfy = q
        l2(world, T0)
        assert world.muts == [], q
    world.comfy = (0, 0)

    def sneak_in(_s):
        world.comfy = (0, 1)                                    # a job lands between the two probes
    world.sleep_hook = sneak_in
    l2(world, T0 + 100)
    assert world.muts == []
    world.sleep_hook = lambda s: None
    world.comfy = (0, 0)
    r = l2(world, T0 + 200)
    assert world.muts == ['post /free {"free_memory": true, "unload_models": true}']
    assert rows(r, "L2")[0]["target"] == "comfyui:free" and rows(r, "L2")[0]["outcome"] == "done"
    l2(world, T0 + 300)                                          # cooldown (30 min)
    assert len(world.muts) == 1
    l2(world, T0 + 200 + 31 * 60)
    assert len(world.muts) == 2


def test_refusals_that_need_a_look_warn_without_paging_and_design_refusals_do_not(world):
    busy_fleet(world, ImmaculaterrDemo=1.5)
    (world.conf / "classes.toml").write_text((REPO / "etc" / "classes.toml").read_text().replace("throttle = 24", "throttle = 0"))
    r = l3(world, T0, throttle="apply")                          # per-day budget used up
    assert any(x["outcome"] == "refused: max_per_day" for x in r.items)
    assert r.status == "warn" and r.alert is False
    # backoff, PAUSE-adjacent and "never stop a P0/P1/database" refusals are the design working: no warning
    ctx = Ctx(world.cfg(), "pressure_response", True, T0)
    run = P._Run(ctx, 4)
    run.add("L4", "restart", "kavita", "P2", "refused: backoff: wait 30 min after the last restart")
    run.add("L5", "emergency stop", "homarr", "P1", "refused: P1/database is never stopped")
    res = P._finish(ctx, run, P.load_classes(), {"level": 4, "dims": {}, "stale": False, "modes": P._modes(ctx)}, "")
    assert res.status == "ok" and res.alert is False


def test_l2_failed_post_is_reported_and_not_retried_inside_the_run(world):
    world.fleet()
    world.comfy = (0, 0)
    world.fail_post = True
    r = l2(world, T0)
    assert r.status == "warn" and r.alert is True and rows(r, "L2", "failed: HTTP 500")
    assert len(world.posts) == 1
    assert any(x["outcome"].startswith("failed") for x in world.audit())


def test_l2_needs_memory_or_gpu_pressure_not_io(world):
    world.fleet()
    world.comfy = (0, 0)
    world.model("m", T0 + 3000)
    for t in (T0, T0 + 400):
        world.set_pstate(t, level=3, mem=0, io=3)
        r = world.run("pressure_response", t, apply=True)
    assert world.muts == [] and any("io/cpu only" in x["outcome"] for x in r.items)


def test_l2_runs_for_gpu_pressure(world):
    world.fleet()
    world.comfy = (0, 0)
    world.set_pstate(T0, level=2, mem=0, gpu=2)
    world.run("pressure_response", T0, apply=True)
    assert world.muts == ['post /free {"free_memory": true, "unload_models": true}']


def test_l2_caps_models_per_run_and_per_day(world):
    world.fleet()
    for i in range(16):
        world.model(f"m{i}", T0 + 99999)
    l2(world, T0)                                                # first sight of all 16: the idle clocks start
    r = l2(world, T0 + 400)
    assert len(world.posts) == 4 and len(rows(r, "L2", "done")) == 4         # max_models_per_run
    l2(world, T0 + 800)
    r = l2(world, T0 + 1200)
    assert len(world.posts) == 12                                # reclaim = 12 actions per day ...
    r = l2(world, T0 + 1600)
    assert len(world.posts) == 12 and rows(r, "L2", "refused: max_per_day")  # ... then it stops and says so
    r = l2(world, T0 + 25 * 3600)                                # the budget is per local day
    assert len(world.posts) == 16 and len(rows(r, "L2", "done")) == 4


def test_budget_has_builtin_defaults_when_classes_toml_is_absent():
    ctx = Ctx(mkcfg(), "pressure_response", True, T0)
    ctx.state = {}
    empty = P.ClassMap({})
    assert [P._budget(ctx, empty, "restart") for _ in range(6)] == [True] * 4 + [False] * 2


def test_http_helpers_only_talk_to_loopback():
    with pytest.raises(ValueError):
        P._http_post("http://example.com/api/generate", {})
    with pytest.raises(ValueError):
        P._http_get("http://10.0.0.5:11434/api/ps")
    assert P._loopback("http://127.0.0.1:8188/free") and P._loopback("http://localhost/x")
    assert not P._loopback("http://127.0.0.1.evil.example/x")


# =========================================================================== L3 throttle
def busy_fleet(world, **active):
    """The fleet, with the named containers burning CPU in the guard samples (cores) so the throttle sees them."""
    if not world.conts:
        world.fleet()
    for name, cores in active.items():
        world.c(name).prof = lambda m, cores=cores: (1.0, cores, 0.1)
    for i in range(4):
        t = T0 - 3600 + i * 900
        world.advance(t)
        world.write_sample(t)


def l3(world, t, mem=3, io=0, cpu=0, apply=True, **modes):
    world.opt("pressure_response", **modes)
    world.set_pstate(t, level=max(mem, io, cpu), mem=mem, io=io, cpu=cpu)
    return world.run("pressure_response", t, apply=apply)


def test_l3_slows_p3_first_and_leaves_p2_alone_while_a_p3_candidate_exists(world):
    busy_fleet(world, ImmaculaterrDemo=1.5, Radarr=2.0)
    r = l3(world, T0, throttle="apply")
    assert world.muts == ["update ImmaculaterrDemo --cpu-shares 128"]
    (row,) = rows(r, "L3", "done")
    assert row["class"] == "P3" and row["target"] == "ImmaculaterrDemo" and "was shares default" in row["action"]
    rec = world.tstate("pressure_response")["throttled"]["ImmaculaterrDemo"]
    assert rec["shares"] == 0 and rec["weight"] == 100 and rec["cid"] == world.c("ImmaculaterrDemo").id
    assert world.weight("ImmaculaterrDemo") == P.weight_linear(128)


def test_l3_moves_to_p2_only_when_no_active_p3_is_left(world):
    busy_fleet(world, Radarr=2.0, kavita=1.2)
    r = l3(world, T0, throttle="apply")
    assert sorted(world.muts) == ["update Radarr --cpu-shares 256", "update kavita --cpu-shares 256"]
    assert {x["class"] for x in rows(r, "L3", "done")} == {"P2"}
    world.muts.clear()
    r = l3(world, T0 + 900, throttle="apply")                    # already throttled: idempotent, no second update
    assert world.muts == []


def test_l3_never_touches_p0_p1_databases_builders_or_idle_containers(world):
    busy_fleet(world, **{n: 3.0 for n, _i, _s in REAL_CONTAINERS if n not in ("kavita",)})
    l3(world, T0, throttle="apply")
    touched = {m.split()[1] for m in world.muts}
    cm = P.load_classes()
    assert touched and all(cm.cls(n) in ("P2", "P3") for n in touched)
    assert not touched & DBS and "buildx_buildkit_immaculaterr-builder0" not in touched
    assert "homarr" not in touched and "docker-socket-proxy" not in touched and "immich_server" not in touched
    # P3 batch first: every candidate this run was P3 (and none of the P3 databases or builders)
    assert {cm.cls(n) for n in touched} == {"P3"} and touched == {"ImmaculaterrDemo", "afsaane-test", "diun"}
    assert world.c("kavita").shares == 0                          # idle: not worth slowing


def test_l3_respects_protected_toml_except_for_the_explicit_weight_exemptions(world):
    """Sonarr is protected.toml-protected batch work and is in classes.toml throttle_unprotect, so it can be slowed;
    a protected P2 that is NOT in that list is left alone and the refusal is audited."""
    world.fleet()
    world.add("tika-protected-by-name", "img", "x")
    (world.conf / "classes.toml").write_text((REPO / "etc" / "classes.toml").read_text()      # a rung acts only on a NAMED class
                                             .replace('"^tika$",', '"^tika$", "^tika-protected-by-name$",'))
    busy_fleet(world, Sonarr=2.0, **{"tika-protected-by-name": 2.0})
    protected = dict(REAL_PROTECTED, patterns=REAL_PROTECTED["patterns"] + ["tika-protected"])
    world.cfg = lambda: dict(mkcfg(world.opts), protected=protected)
    r = l3(world, T0, throttle="apply")
    assert "update Sonarr --cpu-shares 256" in world.muts
    assert not any("tika-protected" in m for m in world.muts)
    assert any(x["target"] == "tika-protected-by-name" and x["action"] == "docker-update-throttle"
               and x["outcome"] == "refused-protected" for x in world.audit())
    assert any(x["target"] == "tika-protected-by-name" and x["outcome"] == "refused: protected.toml" for x in r.items)
    # report mode predicts the refusal instead of promising an action that apply would not take
    world.muts.clear()
    r = l3(world, T0 + 900, throttle="report")
    assert [x["outcome"] for x in r.items if x["target"] == "tika-protected-by-name"] == ["refused: protected.toml"]


def test_l3_default_is_report_only_and_logs_would(world):
    busy_fleet(world, ImmaculaterrDemo=1.5)
    for apply_ in (True, False):
        world.muts.clear()
        r = l3(world, T0, apply=apply_)                           # throttle stays at its default: "report"
        assert world.muts == [] and {x["outcome"] for x in rows(r, "L3")} == {"would"}
        assert not world.tstate("pressure_response").get("throttled")             # nothing is recorded: nothing to undo


def test_l3_blkio_lever_only_where_the_host_honours_io_weights(world):
    world.fleet()
    world.c("afsaane-test").blkio = 500                           # P3 with an explicit weight: can be put back exactly
    world.c("ImmaculaterrDemo").blkio = 0                         # P3 without one: left alone (no exact restore)
    busy_fleet(world)
    for n in ("afsaane-test", "ImmaculaterrDemo"):
        world.c(n).prof = lambda m: (1.0, 0.0, 30.0)
    for i in range(4):
        t = T0 - 600 + i * 150
        world.advance(t)
        world.write_sample(t)
    r = l3(world, T0, mem=0, io=3, throttle="apply")              # io pressure, no bfq/io.cost: no lever
    assert world.muts == [] and any("no lever" in x["outcome"] for x in r.items)
    (world.sysblock / "nvme0n1" / "queue").mkdir(parents=True)
    (world.sysblock / "nvme0n1" / "queue" / "scheduler").write_text("none mq-deadline [bfq]\n")
    assert P.io_weights_effective()
    l3(world, T0 + 900, mem=0, io=3, throttle="apply")
    assert world.muts == ["update afsaane-test --blkio-weight 125"]
    world.muts.clear()
    for k in range(3):
        l3(world, T0 + 1800 + k, mem=0, io=3, throttle="apply")
    assert not any("--cpu-shares" in m for m in world.muts)       # io pressure never touches cpu shares


def test_io_weights_effective_detects_bfq_and_iocost(world):
    assert not P.io_weights_effective()
    (world.sysblock / "sda" / "queue").mkdir(parents=True)
    (world.sysblock / "sda" / "queue" / "scheduler").write_text("[none] mq-deadline\n")
    assert not P.io_weights_effective()
    (world.cg / "io.cost.qos").write_text("259:0 enable=1 ctrl=user rpct=0.00\n")
    assert P.io_weights_effective()


def test_l3_refuses_targets_below_the_floor(world):
    busy_fleet(world, ImmaculaterrDemo=1.5)
    (world.conf / "classes.toml").write_text(
        (REPO / "etc" / "classes.toml").read_text().replace("P3 = 128", "P3 = 8"))
    r = l3(world, T0, throttle="apply")
    assert world.muts == [] and any("below floor 64" in x["outcome"] for x in r.items)


def test_l3_per_run_and_per_day_limits(world):
    busy_fleet(world, **{n: 2.0 for n in ("ImmaculaterrDemo", "afsaane-test", "diun")})
    (world.conf / "classes.toml").write_text((REPO / "etc" / "classes.toml").read_text()
                                             .replace("throttle = 24", "throttle = 2"))
    r = l3(world, T0, throttle="apply")
    assert len(world.muts) == 2                               # the per-day budget of 2 stops the third eligible candidate ...
    assert any(x["outcome"] == "refused: max_per_day" for x in r.items)


def test_throttle_is_restored_when_pressure_returns_to_zero_idempotently(world):
    busy_fleet(world, ImmaculaterrDemo=1.5)
    l3(world, T0, throttle="apply")
    assert world.c("ImmaculaterrDemo").shares == 128
    assert world.tstate("pressure_response")["throttled"]["ImmaculaterrDemo"]["map"] == "linear"
    world.muts.clear()
    world.set_pstate(T0 + 900, level=0)
    r = world.run("pressure_response", T0 + 900, apply=True)
    # the container had no explicit shares (docker 0 = default weight 100). The throttle noticed this runc maps shares
    # linearly, so the share count that maps back to weight 100 (2598) goes first: no wrong intermediate weight
    assert world.muts == ["update ImmaculaterrDemo --cpu-shares 2598"]
    assert world.weight("ImmaculaterrDemo") == 100 and rows(r, "L3", "done: pressure over")
    assert not world.tstate("pressure_response")["throttled"]
    world.muts.clear()
    world.run("pressure_response", T0 + 1800, apply=True)
    assert world.muts == []                                       # nothing recorded any more: idempotent
    assert any(x["action"] == "restore cpu-shares" and "pressure over" in x["outcome"] for x in world.audit())


def test_restore_uses_the_recorded_shares_when_the_container_had_an_explicit_value(world):
    busy_fleet(world, Radarr=2.0)
    world.c("Radarr").shares = 512
    l3(world, T0, mem=3, throttle="apply")
    assert world.c("Radarr").shares == 256
    world.muts.clear()
    world.set_pstate(T0 + 900, level=0)
    world.run("pressure_response", T0 + 900, apply=True)
    assert world.muts == ["update Radarr --cpu-shares 512"] and world.c("Radarr").shares == 512


def test_restore_finds_the_default_weight_under_the_quadratic_runc_too(world):
    world.runc = P.weight_quadratic
    busy_fleet(world, ImmaculaterrDemo=1.5)
    l3(world, T0, throttle="apply")
    assert world.weight("ImmaculaterrDemo") == P.weight_quadratic(128)
    assert world.tstate("pressure_response")["throttled"]["ImmaculaterrDemo"]["map"] == "quadratic"
    world.muts.clear()
    world.set_pstate(T0 + 900, level=0)
    world.run("pressure_response", T0 + 900, apply=True)
    assert world.muts == ["update ImmaculaterrDemo --cpu-shares 1024"] and world.weight("ImmaculaterrDemo") == 100


def test_restore_without_a_recorded_map_tries_1024_then_the_linear_equivalent(world):
    """A record written by an older version (or an unrecognised map): fall back to trying both, verifying the weight."""
    world.fleet()
    k = world.c("diun")
    k.shares = 128
    (world.cg / "system.slice" / f"docker-{k.id}.scope" / "cpu.weight").write_text("5\n")
    out = P._restore_cpu("diun", {"shares": 0, "weight": 100, "cid": k.id})
    assert world.muts == ["update diun --cpu-shares 1024", "update diun --cpu-shares 2598"]
    assert world.weight("diun") == 100 and "2598" in out
    world.runc = P.weight_quadratic
    assert "approx" in P._restore_cpu("diun", {"shares": 0, "weight": 77, "cid": k.id})        # unreachable weight: say so


def test_restore_drops_records_of_containers_that_are_gone_or_recreated(world):
    busy_fleet(world, ImmaculaterrDemo=1.5, Radarr=2.0)
    world.c("Radarr").running = False
    l3(world, T0, throttle="apply")
    world.c("Radarr").running = True
    thr = world.tstate("pressure_response")["throttled"]
    assert set(thr) == {"ImmaculaterrDemo"}
    world.c("ImmaculaterrDemo").id = cid(999)                    # recreated: new container id
    world.muts.clear()
    world.set_pstate(T0 + 900, level=0)
    world.run("pressure_response", T0 + 900, apply=True)
    assert world.muts == [] and not world.tstate("pressure_response")["throttled"]
    assert any("container gone or recreated" in x["outcome"] for x in world.audit())


def test_restore_keeps_the_record_when_docker_update_fails(world):
    busy_fleet(world, ImmaculaterrDemo=1.5)
    l3(world, T0, throttle="apply")
    world.fail.add("update")
    world.set_pstate(T0 + 900, level=0)
    world.run("pressure_response", T0 + 900, apply=True)
    assert "ImmaculaterrDemo" in world.tstate("pressure_response")["throttled"]       # retried next run
    world.fail.clear()
    world.run("pressure_response", T0 + 1800, apply=True)
    assert not world.tstate("pressure_response")["throttled"]


def test_release_when_throttle_is_switched_back_to_report_and_when_state_is_stale(world):
    busy_fleet(world, ImmaculaterrDemo=1.5)
    l3(world, T0, throttle="apply")
    world.opt("pressure_response", throttle="report")
    world.set_pstate(T0 + 900, level=3, mem=3)
    r = world.run("pressure_response", T0 + 900, apply=True)
    assert rows(r, "L3", "done: throttle not in apply mode") and not world.tstate("pressure_response")["throttled"]
    world.opt("pressure_response", throttle="apply")
    busy_fleet(world, ImmaculaterrDemo=1.5)
    l3(world, T0 + 1000, throttle="apply")
    world.muts.clear()
    world.set_pstate(T0 - 7200, level=3, mem=3)                  # pressure_state died: unknown, so the safe direction
    world.run("pressure_response", T0 + 1900, apply=True)
    assert world.muts and not world.tstate("pressure_response")["throttled"]


def test_dry_run_never_releases_and_says_what_it_would_do(world):
    busy_fleet(world, ImmaculaterrDemo=1.5)
    l3(world, T0, throttle="apply")
    world.muts.clear()
    world.set_pstate(T0 + 900, level=0)
    r = world.run("pressure_response", T0 + 900, apply=False)     # `homelab-maint run` without --apply
    assert world.muts == [] and any("would release 1 throttle" in x["outcome"] for x in r.items)
    assert world.tstate("pressure_response")["throttled"]


# =========================================================================== PAUSE
def test_pause_releases_throttles_and_takes_no_new_action(world):
    busy_fleet(world, ImmaculaterrDemo=1.5)
    world.model("m", T0 + 99999)
    world.comfy = (0, 0)
    l3(world, T0, throttle="apply", restart="apply")
    assert world.c("ImmaculaterrDemo").shares == 128
    world.muts.clear()
    world.pause()
    world.set_pstate(T0 + 900, level=4, mem=4)
    r = world.run("pressure_response", T0 + 900, apply=True)
    assert r.summary == "paused: no action taken, changes released"
    assert world.muts == ["update ImmaculaterrDemo --cpu-shares 2598"]
    assert world.kinds() == {"update"}                            # no unload, no /free, no restart: only our own undo
    world.muts.clear()
    r = world.run("pressure_response", T0 + 1800, apply=True)
    assert world.muts == [] and r.summary == "paused: no action taken"


def test_pause_mid_run_stops_the_remaining_actions_of_the_same_run(world):
    """The kill switch appears while a run is already unloading models: the next unload is refused and audited."""
    world.fleet()
    for i in range(3):
        world.model(f"m{i}", T0 + 99999)
    l2(world, T0)                                                # first sight of the models
    real_post = world.http_post

    def post_then_pause(url, payload, timeout=8.0):
        real_post(url, payload, timeout)
        world.pause()                                            # an operator hits `homelab-maint pause` right now
    world.mp.setattr(P, "_http_post", post_then_pause)
    r = l2(world, T0 + 400)
    assert len(world.posts) == 1                                 # only the unload that was already running
    outs = [x["outcome"] for x in rows(r, "L2")]
    assert outs[0] == "done" and outs[1:] and all(o == "refused: paused" for o in outs[1:])
    assert [x["outcome"] for x in world.audit() if x["action"] == "ollama-unload"][-1] == "refused-paused"
    assert len(world.ollama) == 2


def test_pause_mid_run_stops_further_throttle_updates_and_the_next_run_undoes_the_first(world):
    busy_fleet(world, **{n: 2.0 for n in ("ImmaculaterrDemo", "afsaane-test", "diun")})
    real_sh = world.sh

    def sh_then_pause(cmd, timeout=60, **kw):
        r = real_sh(cmd, timeout, **kw)
        if list(cmd[:2]) == ["docker", "update"]:
            world.pause()                                         # the kill switch lands right after the first update
        return r
    for mod in (core, gates, guard, P):
        world.mp.setattr(mod, "sh", sh_then_pause)
    r = l3(world, T0, throttle="apply")
    assert [m for m in world.muts if m.startswith("update")] == ["update ImmaculaterrDemo --cpu-shares 128"]
    assert len(rows(r, "L3", "done")) == 1 and set(world.tstate("pressure_response")["throttled"]) == {"ImmaculaterrDemo"}
    world.muts.clear()
    world.set_pstate(T0 + 900, level=3, mem=3)
    r = world.run("pressure_response", T0 + 900, apply=True)      # still paused: undo what we did, nothing new
    assert r.summary == "paused: no action taken, changes released"
    assert all(m.startswith("update ImmaculaterrDemo --cpu-shares") for m in world.muts) and world.muts


def test_pause_blocks_qos_classes_apply(world):
    world.fleet()
    world.opt("qos_classes", mode="apply")
    world.pause()
    r = world.run("qos_classes", T0, apply=True)
    assert world.muts == [] and r.metrics["mode"] == "report"


# =========================================================================== L4 restart: budget, backoff, gating
def test_restart_budget_two_per_six_hours_across_all_containers_with_an_injected_clock():
    st: dict = {}
    assert P._restart_ok(st, "kavita", T0, 2) == (True, "")
    P._note_restart(st, "kavita", T0)
    ok, why = P._restart_ok(st, "Jackett", T0 + 60, 2)           # a different container: only the budget matters
    assert ok
    P._note_restart(st, "Jackett", T0 + 60)
    ok, why = P._restart_ok(st, "tautulli", T0 + 120, 2)
    assert not ok and why.startswith("budget") and "2 restarts in the last 6 h" in why
    assert P._restart_ok(st, "tautulli", T0 + 6 * 3600 - 1, 2)[0] is False
    assert P._restart_ok(st, "tautulli", T0 + 6 * 3600 + 61, 2) == (True, "")        # the window slid past both


def test_restart_backoff_is_exponential_with_deterministic_jitter():
    st: dict = {}
    jitter = P.zlib.crc32(b"kavita") % 300
    assert 0 <= jitter < 300
    P._note_restart(st, "kavita", T0)
    ok, why = P._restart_ok(st, "kavita", T0 + 1800, 99)          # budget wide open: only the backoff speaks
    assert not ok and why == f"backoff: wait {(1800 + jitter) // 60} min after the last restart"
    assert P._restart_ok(st, "kavita", T0 + 1800 + jitter + 1, 99) == (True, "")
    P._note_restart(st, "kavita", T0 + 4000)                       # second restart within 24 h: wait doubles
    assert not P._restart_ok(st, "kavita", T0 + 4000 + 3599, 99)[0]
    assert P._restart_ok(st, "kavita", T0 + 4000 + 3600 + jitter, 99)[0]
    P._note_restart(st, "kavita", T0 + 20000)                      # third: 2 h
    assert not P._restart_ok(st, "kavita", T0 + 20000 + 7199, 99)[0]
    assert P._restart_ok(st, "kavita", T0 + 20000 + 7200 + jitter, 99)[0]
    # the jitter is per container, so two containers never line up exactly
    assert P.zlib.crc32(b"kavita") != P.zlib.crc32(b"Jackett")


def test_note_restart_forgets_what_is_older_than_a_day():
    st = {"restarts": {"old": [T0 - 90000], "kavita": [T0 - 100, T0 - 86000]}}
    P._note_restart(st, "kavita", T0)
    assert "old" not in st["restarts"] and st["restarts"]["kavita"] == [T0 - 100, T0 - 86000, T0]
    P._note_restart(st, "new", T0 + 86400 * 2)
    assert st["restarts"] == {"new": [T0 + 86400 * 2]}


def test_stuck_candidate_cannot_be_judged_without_enough_fresh_samples(world):
    world.fleet()
    ctx = Ctx(world.cfg(), "pressure_response", True, T0)
    assert P._stuck(ctx) is None                                   # no samples at all
    for i in range(6):
        world.advance(T0 - 600 + i * 60)
        world.write_sample(T0 - 600 + i * 60)
    assert P._stuck(ctx) is None                                   # 6 samples but they span only 5 minutes (need 30)
    for i in range(6):
        world.write_sample(T0 + 3000 + i * 600)
    assert P._stuck(Ctx(world.cfg(), "pressure_response", True, T0 + 3000 + 5 * 600 + 46 * 60)) is None   # newest too old


def test_stuck_candidate_needs_a_long_stable_window(world):
    """leak/runaway needs memory growth with flat cpu+io; busy containers are never candidates."""
    world.fleet()
    world.c("kavita").prof = lambda m: (pw([(0, 3), (120, 40)], m), 0.001, 0.001)       # runaway, no progress
    world.c("tunarr-host-net").prof = lambda m: (pw([(0, 3), (120, 40)], m), 2.5, 30.0)  # same growth, but working
    world.c("Jackett").prof = lambda m: (6.0, 0.001, 0.001)                              # large, flat, idle
    for i in range(8):
        t = T0 + i * 900
        world.advance(t)
        world.write_sample(t)
    c = {x["name"]: x for x in P._stuck(Ctx(world.cfg(), "pressure_response", True, T0 + 7 * 900))}
    assert set(c) == {"kavita", "Jackett"}
    assert c["kavita"]["reason"].startswith("leak/runaway") and c["Jackett"]["reason"].startswith("idle but holding")
    assert not c["kavita"]["protected"]


def runaway_world(world, culprit="kavita", step_min=15):
    """72 containers; `culprit` leaks 24 GiB/h with flat cpu/io from minute 45."""
    world.fleet()
    world.c(culprit).prof = lambda m: (pw([(0, 3), (45, 3), (300, 3 + 255 / 60 * 24)], m), 0.002, 0.002)
    world.opt("stuck_detector")
    return world


def drive_runaway(world, culprit, minutes, apply, step_min=15, t_start_level=None):
    """Replay a runaway one run at a time; returns [(minute, state_result, response_result)]."""
    out = []
    for k in range(minutes // step_min + 1):
        m = k * step_min
        t = T0 + m * 60
        world.advance(t)
        world.set_host(t, mem_some=pw([(0, 0.3), (45, 0.5), (60, 3), (75, 10), (90, 22), (105, 38), (120, 52), (135, 60), (300, 60)], m),
                       mem_full=pw([(0, 0), (45, 0), (60, 0.5), (75, 2.5), (90, 6), (105, 11), (120, 17), (135, 24), (300, 28)], m),
                       avail_gib=pw([(0, 70), (45, 70), (60, 60), (75, 48), (90, 36), (105, 24), (120, 13), (135, 7), (300, 4.5)], m),
                       swapin_pps=pw([(0, 0), (75, 100), (105, 800), (135, 3000), (300, 3000)], m))
        st = world.run("pressure_state", t)
        world.write_sample(t)
        rs = world.run("pressure_response", t, apply=apply)
        check_text(st, rs)
        out.append((m, st, rs))
    return out


def test_l4_restarts_a_proven_stuck_unprotected_candidate_only_in_apply_mode(world):
    runaway_world(world)
    world.opt("pressure_response", restart="apply")
    out = drive_runaway(world, "kavita", 150, apply=True)
    assert world.muts and world.killed() == ["restart kavita"] and set(world.muts) == {"restart kavita"}
    done = [(m, r) for m, _s, rs in out for r in rows(rs, "L4", "done")]
    assert len(done) == 1 and done[0][1]["target"] == "kavita" and done[0][1]["class"] == "P2"
    assert "leak/runaway" in done[0][1]["action"]
    assert world.tstate("pressure_response")["restarts"]["kavita"]
    assert [x["outcome"] for x in world.audit() if x["action"] == "docker restart"] == ["done"]


@pytest.mark.parametrize("apply_,restart", [(True, "report"), (False, "apply"), (False, "report")])
def test_l4_without_apply_or_with_restart_report_it_is_would_only(world, apply_, restart):
    runaway_world(world)
    world.opt("pressure_response", restart=restart)
    out = drive_runaway(world, "kavita", 150, apply=apply_)
    assert world.killed() == []
    would = [r for _m, _s, rs in out for r in rows(rs, "L4", "would")]
    assert would and would[0]["target"] == "kavita"


def test_l4_waits_for_a_streak_and_for_memory_level_four(world):
    runaway_world(world)
    world.opt("pressure_response", restart="apply")
    out = drive_runaway(world, "kavita", 150, apply=True)
    outcomes = [(m, r["outcome"]) for m, _s, rs in out for r in rows(rs, "L4")]
    assert any(o.startswith("watching: candidate 1/2") for _m, o in outcomes)        # first sighting is only noted
    first_done = min(m for m, o in outcomes if o == "done")
    lvl4 = min(m for m, s, _r in out if s.metrics["dims"]["mem"] >= 4)
    assert first_done >= lvl4 and first_done == 135
    assert all(not rows(rs, "L4") for _m, s, rs in out if s.metrics["dims"]["mem"] < 2)       # silent below memory L2


def test_l4_protected_or_database_runaway_is_alert_only_never_restarted(world):
    """A SurrealDB-style runaway: fast anon growth, memory PSI rising, no progress. protected.toml and the database
    rule keep the ladder away; the owner is told and the container memory ceiling (the `caps` cleaner) is the backstop."""
    runaway_world(world, culprit="open-notebook-surrealdb-1")
    world.opt("pressure_response", restart="apply", throttle="apply", emergency="apply")
    out = drive_runaway(world, "open-notebook-surrealdb-1", 150, apply=True)
    assert world.killed() == [] and not any(m.startswith("update open-notebook-surrealdb-1") for m in world.muts)
    alert = [r for _m, _s, rs in out for r in rows(rs, "L4", "protected/database")]
    assert alert and {r["target"] for r in alert} == {"open-notebook-surrealdb-1"} and alert[0]["action"] == "alert only"
    final = out[-1][2]
    assert final.status == "warn" and final.alert is True and "ALERT ONLY open-notebook-surrealdb-1" in final.summary
    assert not any(x["action"] == "docker restart" for x in world.audit())


@pytest.mark.parametrize("culprit", ["Sabnzbd", "Seerr", "afsaane-prod-db", "docker-socket-proxy", "tday_db"])
def test_l4_protected_p1_p0_and_database_runaways_are_never_restarted(world, culprit):
    runaway_world(world, culprit=culprit)
    world.opt("pressure_response", restart="apply", throttle="apply", emergency="apply")
    out = drive_runaway(world, culprit, 150, apply=True)
    assert world.killed() == []
    assert any(r["outcome"].startswith("protected/database") for _m, _s, rs in out for r in rows(rs, "L4"))


def test_l4_picks_the_unprotected_candidate_even_when_a_protected_one_is_larger(world):
    runaway_world(world, culprit="open-notebook-surrealdb-1")
    world.c("kavita").prof = lambda m: (pw([(0, 3), (45, 3), (300, 3 + 255 / 60 * 6)], m), 0.002, 0.002)   # slower leak
    world.opt("pressure_response", restart="apply")
    drive_runaway(world, "open-notebook-surrealdb-1", 150, apply=True)
    assert world.killed() == ["restart kavita"]


def test_l4_second_candidate_is_tried_when_the_first_is_in_backoff(world):
    """kavita (the larger leak) is restarted first; a run later it is still a candidate but in backoff, and that must
    not shield the next proven-stuck container (Jackett): one restart per run, the budget allows two per 6 h."""
    runaway_world(world)
    world.c("Jackett").prof = lambda m: (pw([(0, 3), (45, 3), (300, 3 + 255 / 60 * 12)], m), 0.002, 0.002)
    world.opt("pressure_response", restart="apply")
    out = drive_runaway(world, "kavita", 165, apply=True)
    assert world.killed() == ["restart kavita", "restart Jackett"]
    by_min = {m: rs for m, _s, rs in out}
    assert [r["target"] for r in rows(by_min[135], "L4", "done")] == ["kavita"]
    assert [r["target"] for r in rows(by_min[150], "L4", "done")] == ["Jackett"]
    assert any(r["target"] == "kavita" and r["outcome"].startswith("refused: backoff") for r in rows(by_min[150], "L4"))
    # the third would exceed the budget (2 per 6 h): refused for everyone, whichever candidate is next
    assert not rows(by_min[165], "L4", "done")
    assert any(r["outcome"].startswith("refused: budget") for r in rows(by_min[165], "L4"))


def test_l4_budget_and_backoff_through_the_task_with_an_injected_clock(world):
    """Same stuck candidate, run after run: one restart, then backoff, then (hours later) the second, then the budget."""
    runaway_world(world)
    world.opt("pressure_response", restart="apply")
    world.opt("stuck_detector", min_samples=3, min_window_min=20)

    def hold_level4(t):
        world.advance(t)
        world.set_host(t, mem_some=60, mem_full=26.0, avail_gib=4.0)
        world.run("pressure_state", t)
        world.write_sample(t)
        return world.run("pressure_response", t, apply=True)

    # the leak keeps going and the host stays in memory L4 for ~6 h: 15-minute runs
    t = T0
    world.c("kavita").prof = lambda m: (3 + m / 60 * 24, 0.002, 0.002)
    results = []
    for i in range(30):
        t = T0 + i * 900
        results.append((i, hold_level4(t)))
    done_at = [i for i, r in results if any(x["rung"] == "L4" and x["outcome"] == "done" for x in r.items)]
    assert world.killed() == ["restart kavita"] * len(done_at) and len(done_at) >= 3
    for i in done_at:                                              # never more than 2 restarts in any 6 h window
        assert sum(1 for j in done_at if 0 <= (i - j) * 900 < 6 * 3600) <= 2, done_at
    assert done_at[1] - done_at[0] >= 2                            # the second waited out the 30 min backoff (+ jitter)
    assert done_at[2] - done_at[0] >= 24                           # the third had to wait for the first to leave the window
    refusals = [(i, x["outcome"]) for i, r in results for x in r.items if x["rung"] == "L4" and x["outcome"].startswith("refused: ")]
    assert any(o.startswith("refused: backoff") for i, o in refusals if done_at[0] < i < done_at[1])
    assert any(o.startswith("refused: budget") for i, o in refusals if done_at[1] < i < done_at[2])


def test_l4_busy_candidate_and_missing_reclaim_attempt_are_not_restarted(world, monkeypatch):
    world.fleet()
    cands = [{"name": "kavita", "reason": "leak/runaway: x", "protected": False, "busy": "ComfyUI queue: 1 running",
              "_ident": ("kavita", "img", "kavita")}]
    monkeypatch.setattr(P, "_stuck", lambda ctx: cands)
    world.opt("pressure_response", restart="apply")
    world.set_pstate(T0, level=4, mem=4)
    ctx = Ctx(world.cfg(), "pressure_response", True, T0)
    run = P._Run(ctx, 4)
    P._rung_restart(run, P.load_classes(), "apply", {"mem": 4}, True)
    assert [r["outcome"][:14] for r in run.rows] == ["skipped: busy "] and world.muts == []
    cands[0]["busy"] = ""
    ctx.state["streak"] = {"kavita": 5}
    run = P._Run(ctx, 4)
    P._rung_restart(run, P.load_classes(), "apply", {"mem": 4}, True)         # no reclaim attempted yet
    assert run.rows[-1]["outcome"] == "waiting: reclaim (L2) has not been tried yet" and world.muts == []
    ctx.state["reclaim_attempts"] = 1
    run = P._Run(ctx, 4)
    P._rung_restart(run, P.load_classes(), "apply", {"mem": 4}, True)
    assert run.rows[-1]["outcome"] == "done" and world.muts == ["restart kavita"]


def test_l4_restart_failure_is_reported_and_not_counted_against_the_budget(world, monkeypatch):
    world.fleet()
    cands = [{"name": "kavita", "reason": "leak/runaway: x", "protected": False, "busy": "",
              "_ident": ("kavita", "img", "kavita")}]
    monkeypatch.setattr(P, "_stuck", lambda ctx: cands)
    world.fail.add("restart")
    ctx = Ctx(world.cfg(), "pressure_response", True, T0)
    ctx.state = {"streak": {"kavita": 5}, "reclaim_attempts": 1}
    world.opt("pressure_response", restart="apply")
    run = P._Run(ctx, 4)
    P._rung_restart(run, P.load_classes(), "apply", {"mem": 4}, True)
    assert run.rows[-1]["outcome"].startswith("failed:") and run.fail == 1 and "restarts" not in ctx.state


# =========================================================================== L5 emergency
def l5(world, t, stall=30.0, names=None, mode="apply", apply=True):
    toml = (REPO / "etc" / "classes.toml").read_text()
    if names is not None:
        toml = toml.replace("emergency_stop = []", f"emergency_stop = {json.dumps(names)}")
    (world.conf / "classes.toml").write_text(toml)
    world.opt("pressure_response", emergency=mode)
    world.set_pstate(t, level=5, mem=5, stall_min=stall)
    return world.run("pressure_response", t, apply=apply)


def test_l5_does_nothing_with_an_empty_list(world):
    world.fleet()
    r = l5(world, T0)
    assert world.killed() == [] and any("emergency_stop list is empty" in x["outcome"] for x in r.items)


def test_l5_waits_for_the_stall_to_last_then_stops_one_container_per_run_p3_before_p2(world):
    world.fleet()
    names = ["Jackett", "ImmaculaterrDemo", "diun", "kavita"]
    r = l5(world, T0, stall=5, names=names)
    assert world.killed() == [] and any("waiting: memory stall 5 of 20 min" in x["outcome"] for x in r.items)
    r = l5(world, T0 + 900, stall=25, names=names)
    assert world.killed() == ["stop ImmaculaterrDemo"]                    # a P3 first, ONE per run
    l5(world, T0 + 1800, stall=40, names=names)
    l5(world, T0 + 2700, stall=55, names=names)
    assert world.killed() == ["stop ImmaculaterrDemo", "stop diun", "stop Jackett"]       # P3, P3, then P2
    assert set(world.tstate("pressure_response")["stopped"]) == {"ImmaculaterrDemo", "diun", "Jackett"}
    assert [x["outcome"] for x in world.audit() if x["action"] == "docker stop"] == ["done"] * 3


def test_l5_never_stops_p0_p1_databases_or_protected_names(world):
    world.fleet()
    names = ["docker-socket-proxy", "homarr", "afsaane-prod-db", "afsaane-test-db", "buildx_buildkit_immaculaterr-builder0",
             "Sabnzbd", "tunarr-host-net", "uptime-kuma", "mariadb"]
    for i in range(10):
        l5(world, T0 + i * 900, stall=60, names=names)
    assert world.killed() == []
    refusals = {x["target"]: x["outcome"] for x in world.log_rows() if x["rung"] == "L5" and x["target"] != "-"}
    assert set(refusals) == set(names)                            # every entry was looked at, none starved behind another
    assert refusals["docker-socket-proxy"].startswith("refused: P0") and refusals["homarr"].startswith("refused: P1")
    assert refusals["afsaane-test-db"].startswith("refused") and refusals["mariadb"].startswith("refused")
    # Sabnzbd and tunarr are P2 batch work but protected.toml names them: refused, audited, and no budget is spent
    assert refusals["Sabnzbd"] == "refused: protected.toml" and refusals["tunarr-host-net"] == "refused: protected.toml"
    assert any(x["action"] == "docker stop" and x["target"] == "Sabnzbd" and x["outcome"] == "refused-protected" for x in world.audit())
    assert not any(x["outcome"] == "refused: max_per_day" for x in world.log_rows())


def test_l5_a_protected_entry_at_the_head_does_not_starve_a_stoppable_one(world):
    world.fleet()
    l5(world, T0, stall=60, names=["Sabnzbd", "ImmaculaterrDemo"])          # P2 protected, then a P3 (P3 sorts first anyway)
    assert world.killed() == ["stop ImmaculaterrDemo"]
    l5(world, T0 + 900, stall=60, names=["Sabnzbd", "tunarr-host-net", "Deluge"])
    assert world.killed() == ["stop ImmaculaterrDemo", "stop Deluge"]       # skipped the two protected ones, stopped the next


def test_l5_report_mode_and_budget(world):
    world.fleet()
    r = l5(world, T0, stall=60, names=["diun", "ImmaculaterrDemo"], mode="report")
    assert world.killed() == [] and rows(r, "L5", "would")
    for i in range(5):
        r = l5(world, T0 + 900 * (i + 1), stall=60, names=["diun", "ImmaculaterrDemo"], mode="report")
    assert any(x["outcome"] == "refused: max_per_day" for x in world.log_rows())          # emergency = 3 per day


def test_l5_stopped_containers_are_started_back_when_pressure_is_over_or_paused(world):
    world.fleet()
    l5(world, T0, stall=60, names=["diun"])
    assert world.killed() == ["stop diun"] and not world.c("diun").running
    world.set_pstate(T0 + 900, level=0)
    r = world.run("pressure_response", T0 + 900, apply=True)
    assert world.c("diun").running and world.muts[-1] == "start diun" and rows(r, "L5", "done: pressure over")
    assert not world.tstate("pressure_response")["stopped"]
    # PAUSE brings them back as well (it undoes only what the ladder did)
    l5(world, T0 + 1800, stall=60, names=["diun"])
    assert not world.c("diun").running
    world.pause()
    world.set_pstate(T0 + 2700, level=2, mem=2)                   # memory is below L4: nothing left to protect
    world.run("pressure_response", T0 + 2700, apply=True)
    assert world.c("diun").running


def test_l5_stopped_containers_stay_down_while_the_pressure_state_is_unknown(world):
    world.fleet()
    l5(world, T0, stall=60, names=["diun"])
    world.set_pstate(T0 - 7200, level=5, mem=5)                   # stale: not proof that the stall is over
    world.run("pressure_response", T0 + 900, apply=True)
    assert not world.c("diun").running and world.tstate("pressure_response")["stopped"]


# =========================================================================== qos_classes
def test_qos_classes_defaults_to_report_and_changes_nothing(world):
    world.fleet()
    for apply_ in (True, False):
        r = world.run("qos_classes", T0, apply=apply_)
        assert world.muts == [] and r.metrics["mode"] == "report" and r.summary.startswith("report: would set baseline")
        assert r.alert is False
    sel = r.metrics["selected"]
    assert sel > 20                                              # P1 raised, P3 lowered: dozens of containers
    assert {x["state"].split(":")[0] for x in r.items} <= {"would", "kept", "done", "skipped", "refused"}


def test_qos_classes_apply_sets_baseline_idempotently_and_never_lowers_what_it_must_not(world):
    world.fleet()
    world.opt("qos_classes", mode="apply")
    r = world.run("qos_classes", T0, apply=True)
    ups = {m.split()[1]: m for m in world.muts}
    assert ups["homarr"] == "update homarr --cpu-shares 4096" and ups["immich_server"].endswith("--cpu-shares 4096")
    assert ups["diun"] == "update diun --cpu-shares 256" and ups["ImmaculaterrDemo"].endswith("--cpu-shares 256")
    assert "docker-socket-proxy" not in ups                       # P0: never touched
    assert "Radarr" not in ups and "kavita" not in ups            # P2 is left at docker's default on purpose
    for db in ("afsaane-test-db", "buildx_buildkit_immaculaterr-builder0"):
        assert db not in ups                                      # P3 databases/builders are never LOWERED
    assert r.metrics["kept"] == 2                                  # the two P3 databases/builders were kept as they are
    assert all("--blkio-weight" not in m for m in world.muts)     # no bfq/io.cost on this host: blkio skipped
    assert r.metrics["io_weights_effective"] is False and "io weights skipped" in r.summary
    n = len(world.muts)
    r2 = world.run("qos_classes", T0 + 86400, apply=True)         # second daily run: nothing left to do
    assert len(world.muts) == n and r2.status == "ok" and r2.metrics["selected"] == 0


def test_qos_classes_raises_protected_p1_stores_via_qos_unprotect_and_reports_gaps(world):
    world.fleet()
    world.opt("qos_classes", mode="apply")
    r = world.run("qos_classes", T0, apply=True)
    ups = {m.split()[1] for m in world.muts}
    assert {"immich_postgres", "immich_redis", "nextcloud_postgres", "tday_db", "tday_ollama", "owui-public-ollama"} <= ups
    assert r.metrics["protected"] == 0 and "protected (not in qos_unprotect)" not in r.summary
    assert {x["outcome"] for x in world.audit() if x["action"] == "docker-update-qos"} == {"done"}
    # a protected P1 name that is NOT listed is refused, audited, and shown as a config gap (not as "would")
    world.muts.clear()
    world.add("new-protected-app", "img", "x")
    (world.conf / "classes.toml").write_text((REPO / "etc" / "classes.toml").read_text()
                                             .replace('"^searxng$",', '"^searxng$", "^new-protected-app$",'))
    protected = dict(REAL_PROTECTED, patterns=REAL_PROTECTED["patterns"] + ["new-protected"])
    world.cfg = lambda: dict(mkcfg(world.opts), protected=protected)
    r = world.run("qos_classes", T0 + 86400, apply=True)
    assert not any("new-protected-app" in m for m in world.muts)
    assert r.metrics["protected"] == 1 and "1 protected (not in qos_unprotect)" in r.summary
    assert any(x["name"] == "new-protected-app" and x["state"].startswith("refused: protected.toml") for x in r.items)
    assert any(x["target"] == "new-protected-app" and x["outcome"] == "refused-protected" for x in world.audit())


def test_qos_classes_leaves_stricter_and_higher_explicit_values_alone(world):
    world.fleet()
    world.c("homarr").shares = 8192                               # owner raised P1 further
    world.c("diun").shares = 128                                  # owner made P3 stricter than the baseline
    world.opt("qos_classes", mode="apply")
    world.run("qos_classes", T0, apply=True)
    assert not any(m.startswith(("update homarr", "update diun")) for m in world.muts)


def test_qos_classes_applies_blkio_and_memory_reservation_only_where_they_work(world, monkeypatch):
    world.fleet()
    world.c("homarr").resv = 0
    world.opt("qos_classes", mode="apply")
    monkeypatch.setattr(P, "io_weights_effective", lambda: True)
    monkeypatch.setattr(P, "mem_reservation_effective", lambda: False)
    world.run("qos_classes", T0, apply=True)
    assert "update homarr --cpu-shares 4096 --blkio-weight 800" in world.muts
    assert "update diun --cpu-shares 256 --blkio-weight 20" in world.muts
    assert not any("--blkio-weight" in m and ("afsaane-test-db" in m or "buildx" in m) for m in world.muts)   # not lowered
    assert not any("--memory-reservation" in m for m in world.muts)


def test_qos_classes_refuses_values_below_the_floor(world):
    world.fleet()
    (world.conf / "classes.toml").write_text((REPO / "etc" / "classes.toml").read_text().replace("cpu_shares = 256", "cpu_shares = 10"))
    world.opt("qos_classes", mode="apply")
    r = world.run("qos_classes", T0, apply=True)
    assert not any(m.startswith("update diun") for m in world.muts)
    assert r.metrics["refused"] >= 1 and any("below floor 64" in x["state"] for x in r.items)


def test_qos_classes_does_not_fight_an_active_l3_throttle(world):
    world.fleet()
    core.write_json_atomic(world.state / "tasks" / "pressure_response.json", {"throttled": {"diun": {"shares": 0}}}, 0o600)
    world.opt("qos_classes", mode="apply")
    r = world.run("qos_classes", T0, apply=True)
    assert not any(m.startswith("update diun") for m in world.muts)
    assert r.metrics["kept"] >= 1 and "kept" in r.summary


def test_qos_classes_skips_when_docker_or_classes_are_unavailable(world):
    world.fail.add("docker ps")
    r = world.run("qos_classes", T0, apply=True)
    assert r.status == "skipped" and world.muts == []
    world.fail.clear()
    (world.conf / "classes.toml").write_text('[classes]\nP1 = ["(bad"]\n')
    assert world.run("qos_classes", T0, apply=True).status == "skipped"


def test_qos_classes_docker_update_failure_is_reported_per_container(world):
    world.fleet()
    world.opt("qos_classes", mode="apply")
    world.fail.add("update")
    r = world.run("qos_classes", T0, apply=True)
    assert r.status == "warn" and r.metrics["refused"] > 0 and any(x["state"].startswith("failed") for x in r.items)


# =========================================================================== bulkhead_check
def test_bulkhead_check_reports_drift_with_the_recommended_value_and_principle(world):
    world.ollama_env = "OLLAMA_MAX_LOADED_MODELS=3 OLLAMA_FLASH_ATTENTION=1 OLLAMA_KEEP_ALIVE=60s PATH=/usr/bin"
    world.add("comfyui", "comfyui-local", "comfyui")
    world.comfy_cmd = '["python","main.py","--listen","0.0.0.0"]'
    world.notebook_env = ""                                      # variable absent
    world.add("open-notebook-open_notebook-1")
    world.mp.setattr(P, "_plex_prefs", lambda: world.tmp / "nope.xml")
    r = world.run("bulkhead_check", T0)
    assert r.status == "info" and r.alert is False
    drift = {(i["item"], i["setting"]): i for i in r.items}
    assert drift[("ollama", "OLLAMA_NUM_PARALLEL")]["recommended"] == "2" and "KV cache" in drift[("ollama", "OLLAMA_NUM_PARALLEL")]["why"]
    assert drift[("ollama", "OLLAMA_MAX_QUEUE")]["recommended"] == "128"
    assert drift[("comfyui", "--disable-smart-memory")]["actual"] == "missing"
    assert drift[("open-notebook", "OPEN_NOTEBOOK_WORKER_MAX_TASKS")]["actual"] == "unset"
    assert all("principle 5" in i["principle"] for i in r.items)
    assert ("plex", "TranscodeCountLimit") in drift and drift[("plex", "TranscodeCountLimit")]["actual"] == "unreadable"
    assert world.muts == []                                       # read-only by construction
    assert r.metrics["drift"] == 4 and r.metrics["unreadable"] == 1 and "immich_jobs" in r.metrics


def test_bulkhead_check_is_quiet_when_everything_matches(world):
    world.ollama_env = "OLLAMA_NUM_PARALLEL=2 OLLAMA_MAX_QUEUE=128 OLLAMA_KEEP_ALIVE=300 OLLAMA_MAX_LOADED_MODELS=3"
    world.add("open-notebook-open_notebook-1")
    prefs = world.tmp / "Preferences.xml"
    prefs.write_text('<Preferences TranscodeCountLimit="4" />')
    world.mp.setattr(P, "_plex_prefs", lambda: prefs)
    r = world.run("bulkhead_check", T0)
    assert r.status == "ok" and r.items == [] and r.summary.startswith("bulkheads match the agreed limits")
    assert set(r.metrics["checked"]) == {"ollama", "open-notebook", "plex"}


def test_bulkhead_check_flags_excessive_values_and_unlimited_plex(world):
    world.ollama_env = "OLLAMA_NUM_PARALLEL=16 OLLAMA_MAX_QUEUE=4096 OLLAMA_KEEP_ALIVE=-1 OLLAMA_MAX_LOADED_MODELS=9"
    world.add("open-notebook-open_notebook-1")
    world.notebook_env = "OPEN_NOTEBOOK_WORKER_MAX_TASKS=8"
    prefs = world.tmp / "Preferences.xml"
    prefs.write_text('<Preferences TranscodeCountLimit="0" />')
    world.mp.setattr(P, "_plex_prefs", lambda: prefs)
    r = world.run("bulkhead_check", T0)
    settings = {i["setting"] for i in r.items}
    assert {"OLLAMA_NUM_PARALLEL", "OLLAMA_MAX_LOADED_MODELS", "OLLAMA_KEEP_ALIVE", "OPEN_NOTEBOOK_WORKER_MAX_TASKS",
            "TranscodeCountLimit"} <= settings
    assert [i for i in r.items if i["setting"] == "TranscodeCountLimit"][0]["actual"] == "0 (unlimited)"


def test_bulkhead_check_only_reads_the_named_variables_and_survives_missing_tools(world):
    world.ollama_env = None                                       # systemctl cannot show the unit
    world.add("open-notebook-open_notebook-1")
    world.mp.setattr(P, "_plex_prefs", lambda: world.tmp / "nope.xml")
    r = world.run("bulkhead_check", T0)
    assert any(i["item"] == "ollama" and i["actual"] == "unreadable" for i in r.items)
    inspect_calls = [c for c in world.calls if c[:2] == ["docker", "inspect"]]
    assert all("{{range .Config.Env}}{{println .}}" not in " ".join(c) for c in inspect_calls)   # env dump is filtered by docker
    assert P._dur_s("60s") == 60 and P._dur_s("5m") == 300 and P._dur_s("2h") == 7200 and P._dur_s("-1") == float("inf")
    assert P._dur_s("soon") is None


# =========================================================================== export() -> pressure.json
def test_export_shape_and_contents(world):
    world.fleet()
    world.opt("pressure_response", throttle="apply")
    (world.conf / "maint.toml").write_text('[tasks.pressure_response]\nthrottle = "apply"\nrestart = "report"\n')
    for i in range(6):                                           # 90 min of memory pressure, then calm
        run_state(world, T0 + i * 900, mem_full=5.0 if i < 4 else 0.0)
    P._append_jsonl(P.log_path(), {"ts": T0 + 1000, "level": 2, "rung": "L2", "action": "unload idle model", "target": "ollama:m",
                                   "class": "P1", "outcome": "done"})
    ex = P.export(T0 + 6 * 900)
    assert set(ex) >= {"generated_at", "level", "since", "history", "spikes", "actions", "classes", "ladder", "level_name", "dims"}
    assert ex["generated_at"] == T0 + 6 * 900 and ex["level"] == 2 and ex["level_name"] == "reclaim"
    assert [h["level"] for h in ex["history"]] == [0, 2, 2, 2, 2, 2] and set(ex["history"][0]) == {"t", "level"}
    assert [c["class"] for c in ex["classes"]] == ["P0", "P1", "P2", "P3"]
    assert "docker-socket-proxy" in ex["classes"][0]["members"] and ex["classes"][0]["policy"].startswith("Platform")
    assert len(ex["spikes"]) == 1 and ex["spikes"][0]["state"] == "open" and "kind" not in ex["spikes"][0]    # in progress
    assert ex["actions"] == [{"ts": T0 + 1000, "level": 2, "rung": "L2", "action": "unload idle model", "target": "ollama:m",
                              "class": "P1", "outcome": "done"}]
    modes = {x["level"]: x["mode"] for x in ex["ladder"]}
    assert modes == {0: "always", 1: "always", 2: "apply", 3: "apply", 4: "report", 5: "report"}
    json.dumps(ex)                                               # serialisable, no secrets, no raw output


def test_export_open_spike_then_closed_spike_never_duplicates(world):
    world.fleet()
    for i in range(3):
        run_state(world, T0 + i * 900, mem_full=5.0)
    assert [s["state"] for s in P.export(T0 + 2000)["spikes"]] == ["open"] and world.spikes() == []
    for i in range(3, 9):
        run_state(world, T0 + i * 900, mem_full=0.0)
    assert [s["state"] for s in P.export(T0 + 9 * 900)["spikes"]] == ["closed"] and len(world.spikes()) == 1


def test_export_caps_and_orders(world):
    for i in range(40):
        P._append_jsonl(P.spikes_path(), {"t": T0 + i, "kind": "spike", "id": i, "state": "closed", "peak_level": 1})
    for i in range(80):
        P._append_jsonl(P.log_path(), {"ts": T0 + i, "level": 2, "rung": "L2", "action": "a", "target": "t", "class": "P1", "outcome": "done"})
    ex = P.export(T0 + 100)
    assert len(ex["spikes"]) == 30 and ex["spikes"][0]["id"] == 39 and len(ex["actions"]) == 50 and ex["actions"][0]["ts"] == T0 + 79


def test_export_history_is_the_last_24_hours_only(world):
    core.write_json_atomic(world.state / "tasks" / "pressure_state.json", {
        "level": 1, "t": T0, "history": [{"t": T0 - 90000, "level": 3}, {"t": T0 - 3600, "level": 1}, {"t": T0, "level": 1}]}, 0o600)
    assert [h["t"] for h in P.export(T0)["history"]] == [T0 - 3600, T0]


def test_export_without_any_state_is_valid(world):
    ex = P.export(T0)
    assert ex["level"] == 0 and ex["level_name"] == "unknown" and ex["history"] == [] and ex["spikes"] == [] and ex["actions"] == []
    assert len(ex["classes"]) == 4 and all(c["members"] == [] for c in ex["classes"])


def test_nothing_secret_or_non_ascii_reaches_rows_and_summaries(world):
    assert P._a("café \x07 ✓ " + "x" * 300).isascii() and len(P._a("x" * 300)) == 140
    row = P._row(T0, 3, "L3", "aé", "t" * 100, "P2", "o" * 200)
    assert row["action"].isascii() and len(row["target"]) <= 60 and len(row["outcome"]) <= 100


# =========================================================================== replays: recorded-like sample sequences
Tick = SimpleNamespace


def replay(world, minutes, step_min, host, apply=True):
    """The check tier's order for one run: pressure_state, spike_sampler's record, pressure_response."""
    out = []
    for k in range(minutes // step_min + 1):
        m = k * step_min
        t = T0 + m * 60
        world.advance(t)
        world.set_host(t, **host(m))
        st = world.run("pressure_state", t)
        world.write_sample(t)
        rs = world.run("pressure_response", t, apply=apply)
        check_text(st, rs)
        out.append(Tick(m=m, t=t, st=st, rs=rs, level=st.metrics["level"], dims=st.metrics["dims"]))
    return out


def test_replay_1_legitimate_20_minute_indexing_spike_kills_nothing_and_stays_at_or_below_l2(world):
    """tunarr/meilisearch indexing: anon grows 4 -> 13 GiB in 20 minutes (27 GiB/h) with steady CPU and IO progress,
    moderate memory PSI. Whatever the response ladder does, nothing may be killed, restarted, throttled or stopped, and
    the only mutation allowed is an L2 reclaim (an idle Ollama model unloaded)."""
    world.fleet()
    # a model that has been idle (expires_at constant) so L2 has something legitimate to reclaim
    world.model("qwen3:8b", T0 + 6 * 3600)
    world.opt("stuck_detector", min_samples=6, min_window_min=20)      # the 5-minute cadence of this replay
    world.c("tunarr-host-net").prof = lambda m: (
        pw([(0, 4), (40, 4), (60, 13), (80, 7), (150, 5)], m), pw([(0, 0.1), (39, 0.1), (40, 2.5), (60, 2.5), (61, 0.1)], m),
        pw([(0, 0.5), (39, 0.5), (40, 40), (60, 40), (61, 0.5)], m))
    world.c("kavita").prof = lambda m: (6.0, 0.001, 0.001)           # an unrelated idle-but-holding container: a candidate
    host = lambda m: dict(                                                       # noqa: E731
        mem_some=pw([(0, 0.3), (35, 0.4), (40, 6), (45, 14), (50, 22), (55, 24), (60, 21), (65, 12), (70, 5), (75, 1.5), (80, 0.4), (150, 0.3)], m),
        mem_full=pw([(0, 0), (40, 0.2), (45, 0.7), (50, 1.5), (55, 2.0), (60, 1.6), (65, 0.8), (70, 0.3), (80, 0), (150, 0)], m),
        avail_gib=pw([(0, 70), (40, 70), (60, 52), (80, 68), (150, 70)], m),
        io_some=pw([(0, 4), (40, 4), (45, 25), (60, 25), (65, 6), (150, 4)], m), io_full=0.5)
    world.opt("pressure_response", throttle="apply", restart="apply", emergency="apply")     # even with EVERY rung switched on
    ticks = replay(world, 150, 5, host, apply=True)

    peak = max(t.level for t in ticks)
    assert peak == 2                                             # the spike was seen and reached L2, no further
    assert max(t.dims["mem"] for t in ticks) == 2 and all(t.dims["io"] == 0 and t.dims["cpu"] == 0 for t in ticks)
    assert ticks[0].level == 0 and ticks[-1].level == 0           # it ends: hysteresis releases the level
    assert world.killed() == [], world.muts                       # nothing killed, restarted or stopped
    assert world.kinds() <= {"post"} and all("/api/generate" in m and '"keep_alive": 0' in m for m in world.muts)
    assert "update" not in world.kinds()                          # not even a CPU throttle at L2
    assert world.unmocked == []
    # the judgement was actually possible during the spike: tunarr has progress so it is no candidate, the idle
    # container IS one, and pressure alone (L2) is not enough to touch it
    at_peak = next(t for t in ticks if t.m == 55)
    cands = {c["name"]: c for c in P._stuck(Ctx(world.cfg(), "pressure_response", True, at_peak.t))}
    assert "tunarr-host-net" not in cands and cands["kavita"]["reason"].startswith("idle but holding")
    assert not any(r["rung"] in ("L3", "L4", "L5") and r["outcome"] in ("done", "would") for t in ticks for r in t.rs.items)
    assert all(r["outcome"] != "done" or r["rung"] == "L2" for t in ticks for r in t.rs.items)
    assert all(t.rs.status in ("ok", "info") for t in ticks)       # the response never warns: nothing went wrong
    # recorded for the report: ONE spike, resolved without harm
    (sp,) = world.spikes()
    assert sp["peak_level"] == 2 and sp["nothing_killed"] is True and sp["restarted"] == [] and sp["oom_kills"] == 0
    assert sp["contributors"][0]["name"] == "tunarr-host-net" and sp["contributors"][0]["class"] == "P2"
    assert "nothing killed" in sp["outcome"] and sp["duration_s"] >= 20 * 60
    assert [x["rung"] for x in world.log_rows() if x["outcome"] == "done"] in ([], ["L2"])


def test_replay_1b_even_at_memory_level_four_a_busy_indexer_is_never_a_restart_candidate(world):
    """Same indexing job but the host really is stressed (PSI full 18%): the restart rung is reached, yet tunarr is
    both protected and making progress, so there is no candidate and nothing is restarted."""
    world.fleet()
    world.opt("stuck_detector", min_samples=6, min_window_min=20)
    world.c("tunarr-host-net").prof = lambda m: (pw([(0, 4), (150, 40)], m), 3.0, 60.0)
    world.opt("pressure_response", restart="apply", emergency="apply")          # throttle stays report-only (default)
    host = lambda m: dict(mem_some=pw([(0, 0.3), (30, 0.3), (60, 55), (150, 55)], m),   # noqa: E731
                          mem_full=pw([(0, 0), (30, 0), (60, 18), (150, 18)], m), avail_gib=pw([(0, 70), (60, 6), (150, 6)], m))
    ticks = replay(world, 150, 5, host, apply=True)
    assert max(t.dims["mem"] for t in ticks) >= 4
    assert world.killed() == [] and not any("tunarr-host-net" in m for m in world.muts)
    assert not any(r["rung"] == "L4" and r["target"] == "tunarr-host-net" for t in ticks for r in t.rs.items)
    assert any(r["rung"] == "L3" and r["outcome"] == "would" and r["target"] == "tunarr-host-net" for t in ticks for r in t.rs.items)


@pytest.mark.parametrize("variant", ["defaults_with_apply", "restart_apply_without_apply_flag", "restart_apply_with_apply_flag"])
def test_replay_2_runaway_reaches_the_restart_rung_only_in_apply_mode(world, variant):
    """A runaway container: anon +24 GiB/h from minute 45, flat CPU and IO (no useful progress), memory PSI full rising
    0 -> 28%, MemAvailable 70 -> 4.5 GiB, swap-in rising. The restart rung is reached in every variant but only
    `restart = "apply"` together with `--apply` executes it, once, for the unprotected candidate."""
    runaway_world(world, culprit="kavita")
    if variant != "defaults_with_apply":
        world.opt("pressure_response", restart="apply")
    apply_ = variant != "restart_apply_without_apply_flag"
    out = drive_runaway(world, "kavita", 150, apply=apply_)
    l4 = [(m, r) for m, _s, rs in out for r in rs.items if r["rung"] == "L4" and r["target"] == "kavita"]
    assert any(r["outcome"] in ("done", "would") for _m, r in l4)            # the rung was reached
    if variant == "restart_apply_with_apply_flag":
        assert world.killed() == ["restart kavita"]
        assert [r["outcome"] for _m, r in l4 if r["outcome"] == "done"] == ["done"]
        assert sum(1 for m in world.muts if m.startswith("restart")) == 1
    else:
        assert world.killed() == []
        assert [r["outcome"] for _m, r in l4 if r["outcome"] in ("done", "would")][0] == "would"
        assert [x["outcome"] for x in world.audit() if x["action"] == "docker restart"][-1] == "dry-run"
    # the ladder climbed in order: reclaim was tried before anything was restarted, throttles stayed report-only
    assert "update" not in world.kinds()
    assert max(s.metrics["level"] for _m, s, _r in out) >= 4 and world.unmocked == []


def test_replay_2_runaway_after_the_restart_the_spike_resolves_and_is_recorded_with_its_outcome(world):
    """After the restart the container is small again and the host recovers (pressure decays 70% per run); the ladder
    stands down, the spike closes, and its record says who was restarted."""
    runaway_world(world, culprit="kavita")
    world.opt("pressure_response", restart="apply")
    clock = {"m": 0, "restarted_at": None}

    def on_restart(name):
        k = world.c(name)
        k.cpu_us = k.io_b = 0.0                                  # counters restart with the container
        k.prof = lambda m: (1.0, 0.05, 0.05)
        clock["restarted_at"] = clock["m"]
    world.on_restart = on_restart
    out = []
    for k in range(24):
        m = clock["m"] = k * 15
        t = T0 + m * 60
        world.advance(t)
        if clock["restarted_at"] is None:
            host = dict(mem_some=pw([(0, 0.3), (60, 3), (90, 22), (120, 52), (135, 60), (400, 60)], m),
                        mem_full=pw([(0, 0), (60, 0.5), (90, 6), (120, 17), (135, 24), (400, 28)], m),
                        avail_gib=pw([(0, 70), (60, 60), (120, 13), (135, 7), (400, 4.5)], m))
        else:
            e = (m - clock["restarted_at"]) / 15
            host = dict(mem_some=max(60 * 0.3 ** e, 0.3), mem_full=max(24 * 0.3 ** e, 0.0), avail_gib=min(7 + 20 * e, 70))
        world.set_host(t, **host)
        st = world.run("pressure_state", t)
        world.write_sample(t)
        out.append((m, st, world.run("pressure_response", t, apply=True)))
    assert world.killed() == ["restart kavita"] and clock["restarted_at"] == 135
    assert out[-1][1].metrics["level"] == 0                        # the host recovered and the ladder stood down
    (sp,) = world.spikes()
    assert sp["restarted"] == ["kavita"] and sp["nothing_killed"] is False and sp["outcome"].startswith("handled: restarted kavita")
    assert sp["peak_level"] >= 4 and sp["dims"]["mem"] >= 4


def test_replay_3_short_io_only_spike_never_triggers_memory_rungs(world):
    """`du` on the backup disk plus a library scan: io PSI some 90% / full 60% for ~25 minutes, memory PSI 0.1, 69 GiB
    free, no swap-in. Every rung is switched on and --apply is given: nothing on the memory path may run."""
    world.fleet()
    world.model("qwen3:8b", T0 + 6 * 3600)
    world.comfy = (0, 0)
    world.c("Radarr").prof = lambda m: (1.0, 0.3, pw([(0, 0.1), (35, 0.1), (40, 150), (60, 150), (65, 0.1)], m))
    world.c("Jackett").prof = lambda m: (1.0, 0.05, 40.0)
    world.opt("pressure_response", throttle="apply", restart="apply", emergency="apply")
    host = lambda m: dict(                                                       # noqa: E731
        io_some=pw([(0, 4), (30, 5), (35, 60), (40, 85), (50, 90), (60, 70), (65, 20), (70, 5), (150, 4)], m),
        io_full=pw([(0, 0.5), (30, 1), (35, 20), (40, 48), (50, 60), (60, 40), (65, 12), (70, 3), (80, 0.5), (150, 0.5)], m),
        mem_some=0.1, mem_full=0.0, avail_gib=69.0, load1=pw([(0, 2), (40, 33), (65, 8), (80, 2)], m))
    ticks = replay(world, 150, 5, host, apply=True)
    assert max(t.level for t in ticks) == 3                       # the io spike is seen (io caps at L3) ...
    assert all(t.dims["mem"] == 0 and t.dims["gpu"] == 0 for t in ticks)            # ... and it is never a memory level
    assert world.muts == [], world.muts                          # no unload, no /free, no update, no restart, no stop
    assert world.unmocked == []
    assert all(t.st.alert is False for t in ticks)                # io is dashboard-only, never a page
    assert not [r for t in ticks for r in t.rs.items if r["rung"] in ("L4", "L5") or (r["rung"] == "L2" and r["outcome"] in ("done", "would"))]
    assert any("no lever" in r["outcome"] for t in ticks for r in t.rs.items)         # it says why it did not act
    assert any("io/cpu only" in r["outcome"] for t in ticks for r in t.rs.items)
    (sp,) = world.spikes()
    assert sp["dims"]["mem"] == 0 and sp["psi"]["io_full60"] >= 55 and sp["nothing_killed"] is True
    assert sp["contributors"][0]["name"] == "Radarr"               # the io contributor is named (class P2)


def test_replay_3b_io_only_with_bfq_may_only_lower_blkio_weights_never_memory_or_cpu(world):
    world.fleet()
    world.c("afsaane-test").blkio = 500
    world.c("afsaane-test").prof = lambda m: (1.0, 0.0, 30.0)
    (world.sysblock / "nvme0n1" / "queue").mkdir(parents=True)
    (world.sysblock / "nvme0n1" / "queue" / "scheduler").write_text("none [bfq]\n")
    world.opt("pressure_response", throttle="apply", restart="apply", emergency="apply")
    host = lambda m: dict(io_some=90.0, io_full=60.0, mem_some=0.1, mem_full=0.0, avail_gib=69.0)   # noqa: E731
    replay(world, 90, 15, host, apply=True)
    assert world.muts == ["update afsaane-test --blkio-weight 125"]
    assert world.killed() == [] and not any("--cpu-shares" in m for m in world.muts)


def test_replay_3c_a_single_io_blip_is_not_even_a_spike(world):
    world.fleet()
    world.opt("pressure_response", throttle="apply", restart="apply")
    host = lambda m: dict(io_some=92.0 if m == 30 else 5.0, io_full=70.0 if m == 30 else 0.5, mem_some=0.1)   # noqa: E731
    ticks = replay(world, 120, 15, host, apply=True)
    assert {t.level for t in ticks} == {0} and world.spikes() == [] and world.muts == []
    assert world.log_rows() == []


@pytest.mark.parametrize("pause_at", [None, 120])
def test_replay_pause_during_a_runaway_stops_every_rung(world, pause_at):
    """The control (no PAUSE) restarts the runaway at minute 135. With PAUSE dropped at minute 120, from then on
    nothing new happens at all (the runaway is left to the memory ceiling) while pressure_state keeps reporting."""
    runaway_world(world, culprit="kavita")
    world.opt("pressure_response", restart="apply", throttle="apply")
    out = []
    for k in range(0, 11):
        m = k * 15
        t = T0 + m * 60
        if m == pause_at:
            world.pause()
        world.advance(t)
        world.set_host(t, mem_some=pw([(0, 0.3), (60, 3), (90, 22), (120, 52), (300, 60)], m),
                       mem_full=pw([(0, 0), (60, 0.5), (90, 6), (120, 17), (135, 24), (300, 28)], m),
                       avail_gib=pw([(0, 70), (60, 60), (120, 13), (135, 7), (300, 4.5)], m))
        st = world.run("pressure_state", t)
        world.write_sample(t)
        out.append((m, st, world.run("pressure_response", t, apply=True)))
    assert out[-1][1].metrics["level"] >= 4                    # the state task is read-only and keeps reporting
    if pause_at is None:
        assert world.killed() == ["restart kavita"]
    else:
        assert world.killed() == [] and "update" not in world.kinds()
        assert out[-1][2].summary == "paused: no action taken"
        assert all(r["outcome"] != "done" for _m, _s, rs in out[pause_at // 15:] for r in rs.items)


# =========================================================================== reviewer round 2: regression tests
# 1  io/gpu-only pressure is not host pressure for the scheduler, the routine gate, the live page or the SLO
def test_io_only_pressure_publishes_gate_level_zero_and_is_labelled_by_its_resource(world):
    world.fleet()
    run_state(world, T0, io_some=56.0, io_full=47.0, load1=33.0)
    r = run_state(world, T0 + 900, io_some=56.0, io_full=47.0, load1=33.0)
    assert r.metrics["level"] == 3 and r.metrics["gate_level"] == 0          # the dashboard keeps the level ...
    assert r.status == "info" and r.alert is False                           # ... every status-based consumer sees "fine"
    assert r.metrics["level_name"] == "io stall" and "slow batch" not in r.summary and "info only" in r.summary
    st = world.tstate("pressure_state")
    assert (st["level"], st["gate_level"], st["level_name"]) == (3, 0, "io stall")
    ex = P.export(T0 + 900)
    assert (ex["level"], ex["gate_level"], ex["level_name"]) == (3, 0, "io stall")


def test_gate_level_follows_memory_and_cpu_only(world):
    world.fleet()
    clock = [T0]

    def settle(**host):                                                       # two runs at the same numbers: the level is entered
        for _ in range(2):
            r = run_state(world, clock[0], **host)
            clock[0] += 900
        return r

    def calm():                                                               # four quiet runs: hysteresis lets every level go
        for _ in range(4):
            run_state(world, clock[0])
            clock[0] += 900

    r = settle(mem_full=5.0)                                                  # memory L2
    assert (r.metrics["level"], r.metrics["gate_level"], r.status, r.alert) == (2, 2, "warn", True)
    assert r.metrics["level_name"] == "reclaim"
    calm()
    r = settle(cpu_some=60.0)                                                 # cpu L2: a person waits for a core, but no page
    assert (r.metrics["gate_level"], r.status, r.alert) == (2, "warn", False)
    calm()
    r = settle(io_some=60.0, io_full=47.0, mem_full=5.0)                      # io L3 over memory L2: the gate is memory's L2
    assert (r.metrics["level"], r.metrics["gate_level"], r.status, r.alert) == (3, 2, "warn", True)
    assert "info only" not in r.summary
    calm()
    world.gpu = (24000, 24576)
    r = settle()
    assert (r.metrics["level"], r.metrics["gate_level"], r.status) == (2, 0, "info")      # a full card is not host pressure
    assert r.metrics["level_name"] == "vram full"


def test_the_label_names_the_resource_when_io_and_gpu_drive_the_level():
    eff = {"mem": 0, "io": 3, "cpu": 0, "gpu": 2}
    assert P.level_label(3, eff) == "io stall" and P.level_label(2, {**eff, "io": 2}) == "io wait + vram full"
    assert P.level_label(3, {**eff, "mem": 3}) == "slow batch" and P.level_label(0, {}) == "normal"
    assert P.gate_level(eff) == 0 and P.gate_level({**eff, "cpu": 3}) == 3


def test_a_nightly_io_plateau_never_warns_never_gates_and_never_burns_the_slo(world):
    """The measured pattern: io L3 for 32 of 39 runs. The scheduler, the routine gate and the SLO all read the task's
    state, status or history, none of which may call this host pressure."""
    world.fleet()
    hist = []
    for i in range(39):
        plateau = i < 32
        r = run_state(world, T0 + i * 900, io_some=60.0 if plateau else 3.0, io_full=47.0 if plateau else 0.0)
        hist.append({"t": T0 + i * 900, "kind": "task", "task": "pressure_state", "status": r.status,
                     "metrics": {k: v for k, v in r.metrics.items() if isinstance(v, (int, float))}})
        assert r.metrics["gate_level"] == 0 and r.alert is False
    assert max(h["metrics"]["level"] for h in hist) == 3                       # the plateau is visible ...
    assert not any(h["status"] in ("warn", "crit", "error") for h in hist)     # ... and counts as healthy
    from homelab_maint import incidents                                        # the real consumer, on this very history
    row = next((o for o in incidents.export_slo(hist, T0 + 39 * 900)["objectives"] if "pressure_state" in o["checks"]), None)
    if row is not None:
        assert row["availability_pct"] == 100 and row["status"] == "ok"


def test_old_state_without_gate_level_is_read_as_memory_and_cpu_dims(world):
    world.set_pstate(T0, level=3, mem=0, io=3)                                # written by the previous version
    ex = P.export(T0)
    assert ex["level"] == 3 and ex["gate_level"] == 0
    world.set_pstate(T0, level=3, mem=3)
    assert P.export(T0)["gate_level"] == 3


# 2  holds are released by the resource each rung answers, not by the host level (which io/gpu keep high)
def test_holds_are_released_when_the_memory_cause_is_gone_even_with_io_at_l3(world):
    busy_fleet(world, ImmaculaterrDemo=1.5)
    l3(world, T0, throttle="apply")
    assert world.c("ImmaculaterrDemo").shares == 128
    l5(world, T0 + 900, stall=60, names=["diun"])
    assert not world.c("diun").running
    for i in range(9):                                                        # the stall ends; io sits at L3 for hours
        t = T0 + 1800 + i * 900
        world.set_pstate(t, level=3, mem=0, io=3, spike_id=9)
        world.run("pressure_response", t, apply=True)
    assert world.c("ImmaculaterrDemo").shares != 128 and world.weight("ImmaculaterrDemo") == 100
    assert world.c("diun").running
    st = world.tstate("pressure_response")
    assert not st.get("throttled") and not st.get("stopped")


def test_each_hold_waits_for_its_own_resource_to_recover(world):
    busy_fleet(world, ImmaculaterrDemo=1.5)
    l3(world, T0, throttle="apply")
    l5(world, T0 + 900, stall=60, names=["diun"])

    def tick(k, mem, io=0):
        t = T0 + 1800 + k * 900
        world.set_pstate(t, level=max(mem, io), mem=mem, io=io, stall_min=60 + k * 15)
        return world.run("pressure_response", t, apply=True)

    tick(0, 4)                                                                # still memory L4: both holds stay
    assert world.c("ImmaculaterrDemo").shares == 128 and not world.c("diun").running
    tick(1, 3)                                                                # memory L3: the stop is over, the throttle is not
    assert world.c("diun").running and world.c("ImmaculaterrDemo").shares == 128
    r = tick(2, 2, io=3)                                                      # memory L2 (io L3 stays): the throttle is over too
    assert world.c("ImmaculaterrDemo").shares != 128 and rows(r, "L3", "done: resource below L3")


def test_a_blkio_throttle_is_kept_for_io_pressure_and_released_when_it_ends(world):
    world.fleet()
    world.c("afsaane-test").blkio = 500
    world.c("afsaane-test").prof = lambda m: (1.0, 0.0, 30.0)
    busy_fleet(world)
    for i in range(4):
        t = T0 - 600 + i * 150
        world.advance(t)
        world.write_sample(t)
    (world.sysblock / "nvme0n1" / "queue").mkdir(parents=True)
    (world.sysblock / "nvme0n1" / "queue" / "scheduler").write_text("none [bfq]\n")
    l3(world, T0, mem=0, io=3, throttle="apply")
    assert world.c("afsaane-test").blkio == 125
    for k in range(1, 4):                                                     # io still L3: the weight stays
        l3(world, T0 + k * 900, mem=0, io=3, throttle="apply")
    assert world.c("afsaane-test").blkio == 125
    world.set_pstate(T0 + 4 * 900, level=0)
    world.run("pressure_response", T0 + 4 * 900, apply=True)
    assert world.c("afsaane-test").blkio == 500


def test_an_emergency_stop_is_time_bounded_and_not_repeated_at_once(world):
    world.fleet()
    l5(world, T0, stall=60, names=["diun"])
    assert not world.c("diun").running
    outs = [l5(world, T0 + k * 900, stall=60 + k * 15, names=["diun"]) for k in range(1, 6)]
    assert world.muts.count("start diun") == 1 and world.muts.count("stop diun") == 1
    assert [bool(rows(o, "L5", "done: max stop time")) for o in outs] == [False, False, False, True, False]
    assert world.c("diun").running                                            # back after 60 minutes, still L5 ...
    assert any("cooldown" in x["outcome"] for x in outs[4].items)             # ... and not stopped again straight away
    assert world.tstate("pressure_response").get("stopped") == {}


def test_a_failed_start_back_at_the_max_stop_time_warns_and_alerts(world):
    world.fleet()
    l5(world, T0, stall=60, names=["diun"])
    for k in range(1, 4):
        l5(world, T0 + k * 900, stall=60 + k * 15, names=["diun"])
    world.fail.add("start")
    r = l5(world, T0 + 4 * 900, stall=120, names=["diun"])
    assert r.status == "warn" and r.alert is True and rows(r, "L5", "failed")
    assert "diun" in world.tstate("pressure_response")["stopped"]


# 3  a failed throttle restore is visible
def test_a_failed_throttle_restore_is_a_warning_with_an_alert_not_silence(world):
    busy_fleet(world, ImmaculaterrDemo=1.5)
    l3(world, T0, throttle="apply")
    world.fail.add("update")
    world.set_pstate(T0 + 900, level=0)
    r = world.run("pressure_response", T0 + 900, apply=True)
    assert r.status == "warn" and r.alert is True and r.metrics["throttled"] == 1 and r.metrics["failed"] == 1
    (row,) = rows(r, "L3", "failed")
    assert row["target"] == "ImmaculaterrDemo" and row["action"] == "restore cpu-shares"
    assert r.summary != "L0 normal: nothing to do" and "1 throttle(s) not restored" in r.summary
    for k in range(2, 6):                                                     # retried every run, logged once
        world.set_pstate(T0 + k * 900, level=0)
        assert world.run("pressure_response", T0 + k * 900, apply=True).status == "warn"
    assert len([x for x in world.log_rows() if x["outcome"].startswith("failed")]) == 1
    world.fail.clear()
    world.set_pstate(T0 + 6 * 900, level=0)
    r = world.run("pressure_response", T0 + 6 * 900, apply=True)
    assert r.status == "info" and r.metrics["throttled"] == 0 and r.alert is False


# 4  a stopped container that is gone must not page forever
def stop_diun(world):
    world.fleet()
    l5(world, T0, stall=60, names=["diun"])
    assert not world.c("diun").running


def test_a_stopped_container_that_no_longer_exists_is_dropped_not_retried_forever(world):
    stop_diun(world)
    del world.conts["diun"]
    for k in (1, 2, 3):
        world.set_pstate(T0 + k * 900, level=0)
        r = world.run("pressure_response", T0 + k * 900, apply=True)
        assert r.status in ("ok", "info") and r.alert is False
    assert not world.tstate("pressure_response").get("stopped")
    assert not [c for c in world.calls if c[:2] == ["docker", "start"]]
    assert any("dropped: container gone" in x["outcome"] for x in world.audit())


def test_a_stopped_container_recreated_or_started_by_its_owner_is_not_started_again(world):
    stop_diun(world)
    world.c("diun").id = cid(999)                                             # recreated under the same name
    world.set_pstate(T0 + 900, level=0)
    world.run("pressure_response", T0 + 900, apply=True)
    assert not world.tstate("pressure_response").get("stopped") and not world.c("diun").running
    assert any("recreated" in x["outcome"] for x in world.audit())
    world.c("diun").running = True
    l5(world, T0 + 1800, stall=60, names=["diun"])                            # stop it again ...
    world.c("diun").running = True                                            # ... and the owner starts it by hand
    world.set_pstate(T0 + 2700, level=0)
    world.run("pressure_response", T0 + 2700, apply=True)
    assert not world.tstate("pressure_response").get("stopped")
    assert [c for c in world.calls if c[:2] == ["docker", "start"]] == []


def test_a_start_back_that_keeps_failing_gives_up_after_three_attempts_with_one_alert(world):
    stop_diun(world)
    world.fail.add("start")
    res = []
    for k in range(1, 6):
        world.set_pstate(T0 + k * 900, level=0)
        res.append(world.run("pressure_response", T0 + k * 900, apply=True))
    assert [r.status for r in res] == ["warn", "warn", "warn", "ok", "ok"]
    assert "start-back failed" in res[0].summary and "L0 normal: nothing" not in res[0].summary
    assert len([c for c in world.calls if c[:2] == ["docker", "start"]]) == 3
    assert any("gave up" in x["outcome"] for x in res[2].items)
    assert not world.tstate("pressure_response").get("stopped")
    assert [r.alert for r in res] == [True, True, True, False, False]


# 5  unknown is not P2 for the rungs that act
def test_l3_never_slows_an_unclassified_container(world):
    world.fleet()
    world.add("jellyfin-new", "jellyfin/jellyfin", "jellyfin")                # not in classes.toml, not protected, not a db
    busy_fleet(world, **{"jellyfin-new": 2.0})
    r = l3(world, T0, throttle="apply")
    assert world.muts == [] and "jellyfin-new" not in world.tstate("pressure_response").get("throttled", {})
    assert any(x["outcome"].startswith("skipped: unclassified") and "jellyfin-new" in x["outcome"] for x in r.items)


def test_l4_only_alerts_for_an_unclassified_runaway_and_l5_refuses_to_stop_it(world):
    world.fleet()
    world.add("newapp", "newapp:1", "newapp")
    world.c("newapp").prof = lambda m: (pw([(0, 3), (45, 3), (300, 3 + 255 / 60 * 24)], m), 0.002, 0.002)
    world.opt("stuck_detector")
    world.opt("pressure_response", restart="apply", throttle="apply", emergency="apply")
    out = drive_runaway(world, "newapp", 150, apply=True)
    assert world.killed() == [] and not any(m.startswith("update newapp") for m in world.muts)
    alert = [r for _m, _s, rs in out for r in rows(rs, "L4", "unclassified")]
    assert alert and alert[0]["target"] == "newapp" and alert[0]["action"] == "alert only"
    assert out[-1][2].status == "warn" and out[-1][2].alert is True and "ALERT ONLY newapp" in out[-1][2].summary
    r = l5(world, T0 + 20000, stall=60, names=["newapp"])
    assert world.killed() == [] and any(x["outcome"].startswith("refused: unclassified") for x in r.items)


def test_unclassified_containers_are_reported_in_state_export_and_the_weekly_check(world):
    world.fleet()
    assert run_state(world, T0).metrics["unclassified"] == []
    world.add("newapp", "newapp:1", "newapp")
    r = run_state(world, T0 + 900)
    assert r.metrics["unclassified"] == ["newapp"] and r.metrics["unclassified_n"] == 1
    assert P.export(T0 + 900)["unclassified"] == ["newapp"]
    world.mp.setattr(P, "_plex_prefs", lambda: world.tmp / "nope.xml")
    b = world.run("bulkhead_check", T0)
    item = next(i for i in b.items if i["item"] == "classes")
    assert "newapp" in item["actual"] and "classes.toml" in item["recommended"] and b.metrics["unclassified"] == 1
    assert b.alert is False and b.status == "info" and world.muts == []
    ctx = Ctx(world.cfg(), "pressure_state", False, T0)
    cm = P.load_classes()
    act = {"newapp": {"anon": 5 * GIB, "swap": 0, "cpu_pct": 0.0, "io_mib_s": 0.0, "growth": 9.0}}
    assert P.contributors(act, cm, {"mem": 2})[0]["class"] == "unclassified"
    assert P.contributors({"kavita": act["newapp"]}, cm, {"mem": 2})[0]["class"] == "P2"


def test_qos_classes_leaves_an_unclassified_container_alone(world):
    world.fleet()
    world.add("newapp", "newapp:1", "newapp")
    (world.conf / "classes.toml").write_text((REPO / "etc" / "classes.toml").read_text()
                                             .replace("[defaults.P2]\n", "[defaults.P2]\ncpu_shares = 512\n"))
    world.opt("qos_classes", mode="apply")
    world.run("qos_classes", T0, apply=True)
    assert "update kavita --cpu-shares 512" in world.muts and not any(m.startswith("update newapp") for m in world.muts)


# 6  a throttle going back to report, or a PAUSE, must not start an emergency-stopped container into the stall
def test_switching_throttle_to_report_releases_the_throttle_but_never_starts_emergency_stops(world):
    busy_fleet(world, ImmaculaterrDemo=1.5)
    l3(world, T0, throttle="apply")
    l5(world, T0 + 900, stall=60, names=["diun"])
    world.muts.clear()
    world.opt("pressure_response", throttle="report")
    world.set_pstate(T0 + 1800, level=5, mem=5, stall_min=75)
    r = world.run("pressure_response", T0 + 1800, apply=True)
    assert world.muts == ["update ImmaculaterrDemo --cpu-shares 2598"]       # only our own throttle was undone
    assert not world.c("diun").running and "diun" in world.tstate("pressure_response")["stopped"]
    assert rows(r, "L3", "done: throttle not in apply mode")
    world.set_pstate(T0 + 2700, level=5, mem=5, stall_min=90)
    world.run("pressure_response", T0 + 2700, apply=True)
    assert world.muts.count("stop diun") == 0 and world.muts.count("start diun") == 0       # no stop/start/stop flap


def test_pause_during_a_live_memory_stall_keeps_the_emergency_stop_until_memory_recovers(world):
    world.fleet()
    l5(world, T0, stall=60, names=["diun"])
    world.pause()
    world.set_pstate(T0 + 900, level=4, mem=4)
    r = world.run("pressure_response", T0 + 900, apply=True)
    assert not world.c("diun").running and "diun" in world.tstate("pressure_response")["stopped"]
    assert r.summary.startswith("paused: no action taken") and "kept down" in r.summary and "L4" in r.summary
    assert any(x["rung"] == "L5" and x["outcome"].startswith("held") for x in r.items)
    world.set_pstate(T0 + 1800, level=2, mem=2)
    r = world.run("pressure_response", T0 + 1800, apply=True)
    assert world.c("diun").running and r.summary == "paused: no action taken, changes released"


def test_pause_with_an_unknown_pressure_state_keeps_the_stop_until_the_max_stop_time(world):
    world.fleet()
    l5(world, T0, stall=60, names=["diun"])
    world.pause()
    world.set_pstate(T0 - 7200, level=5, mem=5)                               # stale: memory is unknown
    world.run("pressure_response", T0 + 1800, apply=True)
    assert not world.c("diun").running
    world.run("pressure_response", T0 + 3600, apply=True)                     # 60 minutes after the stop: time is up
    assert world.c("diun").running


def test_dry_run_says_what_it_would_start_back_and_changes_nothing(world):
    stop_diun(world)
    world.muts.clear()
    world.set_pstate(T0 + 900, level=0)
    r = world.run("pressure_response", T0 + 900, apply=False)                 # `homelab-maint run` without --apply
    assert world.muts == [] and any("would start back 1 container" in x["outcome"] for x in r.items)
    assert not world.c("diun").running and "diun" in world.tstate("pressure_response")["stopped"]
    world.set_pstate(T0 + 4000, level=5, mem=5, stall_min=90)                 # past the max stop time: still only a "would"
    r = world.run("pressure_response", T0 + 4000, apply=False)
    assert world.muts == [] and any("max stop time" in x["outcome"] for x in r.items)


def test_the_response_summary_and_the_spike_record_name_an_io_only_episode_as_such(world):
    world.fleet()
    world.set_pstate(T0, level=3, mem=0, io=3, spike_id=4)
    r = world.run("pressure_response", T0, apply=True)
    assert r.summary.startswith("L3 io stall") and "slow batch" not in r.summary and r.metrics["level"] == 3
    for i in range(4):
        run_state(world, T0 + i * 900, io_some=60.0, io_full=47.0)
    for i in range(4, 10):
        run_state(world, T0 + i * 900)
    for i in range(10, 14):
        run_state(world, T0 + i * 900, mem_full=5.0)
    for i in range(14, 20):
        run_state(world, T0 + i * 900)
    io_sp, mem_sp = world.spikes()
    assert (io_sp["peak_level"], io_sp["gate_peak"]) == (3, 0) and (mem_sp["peak_level"], mem_sp["gate_peak"]) == (2, 2)
