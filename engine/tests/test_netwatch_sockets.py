"""Tests for homelab_maint/netwatch/sockets.py: ss output turned into hourly flows, container namespaces faked."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import subprocess
from ipaddress import ip_address, ip_network
from pathlib import Path

from homelab_maint.netwatch.entities import ContainerInfo, Topology
from homelab_maint.netwatch.model import Flow
from homelab_maint.netwatch.sockets import sample

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "netwatch" / "ss_tin.txt"
HOST_SS = FIXTURE.read_text()

NOW = 1798761600 + 1234  # 2027-01-01T00:20:34Z
BUCKET = 1798761600
CFG = {"tunnel_local_ports": [9000]}

WEB_LISTEN = "LISTEN 0 4096 0.0.0.0:8080 0.0.0.0:*\n"
WEB_ESTAB = (
    "ESTAB 0 0 172.28.0.5:55001 172.28.0.6:5432\n"
    "\t cubic bytes_sent:2000 bytes_acked:2000 bytes_received:3000\n"
    "ESTAB 0 0 172.28.0.5:8080 [2001:db8::5]:60000\n"
    "\t cubic bytes_sent:700 bytes_acked:700 bytes_received:100\n"
    "ESTAB 0 0 172.28.0.5:55002 198.51.100.20:443\n"
    "\t cubic bytes_sent:50 bytes_acked:50 bytes_received:60\n"
)
DB_LISTEN = "LISTEN 0 4096 0.0.0.0:5432 0.0.0.0:*\n"
# The server end of the web->db connection. The web namespace already counts it, so it must not be counted twice.
DB_ESTAB = (
    "ESTAB 0 0 172.28.0.6:5432 172.28.0.5:55001\n"
    "\t cubic bytes_sent:3000 bytes_acked:3000 bytes_received:2000\n"
)

EXPECTED = {
    Flow(BUCKET, "in", "203.0.113.7", "host", 32400, "tcp", 2, 500, 1500),
    Flow(BUCKET, "lan", "lan:192.168.40.50", "host", 22, "tcp", 1, 800, 4000),
    Flow(BUCKET, "out", "container:web", "198.51.100.20", 443, "tcp", 1, 60, 50),
    Flow(BUCKET, "in", "2001:db8::5", "container:web", 8080, "tcp", 1, 100, 700),
    Flow(BUCKET, "svc", "container:web", "container:db", 5432, "tcp", 1, 3000, 2000),
}


def _cp(stdout="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


def _topo():
    return Topology(
        containers={
            "web": ContainerInfo(name="web", pid=4242, ips=["172.28.0.5"], project="stack"),
            "db": ContainerInfo(name="db", pid=4343, ips=["172.28.0.6"], project="stack"),
        },
        subnets=[ip_network("172.28.0.0/16")],
        compose={"web": "stack", "db": "stack"},
        listening={("tcp", 32400), ("tcp", 22)},
        host_ips=frozenset({ip_address("192.168.40.20"), ip_address("172.28.0.1")}),
    )


def make_sh(failing=(), host_ss=HOST_SS, host_rc=0):
    """Fake core.sh. `failing` PIDs make nsenter exit 1, as a container that vanished does."""
    namespaces = {
        4242: {"listen": WEB_LISTEN, "established": WEB_ESTAB},
        4343: {"listen": DB_LISTEN, "established": DB_ESTAB},
    }

    def fake(cmd, timeout=60, check=False, input_=None, env=None):
        if cmd[0] == "ss":
            return _cp(host_ss, host_rc)
        if cmd[0] == "nsenter":
            pid = int(cmd[2])
            if pid in failing:
                return _cp("", 1)
            kind = "listen" if "-tlnH" in cmd else "established"
            return _cp(namespaces[pid][kind])
        raise AssertionError(f"unexpected command {cmd}")
    return fake


def test_flows_for_host_and_containers():
    flows = sample(_topo(), CFG, NOW, sh=make_sh())
    assert len(flows) == 5
    assert set(flows) == EXPECTED


def test_vanished_container_skipped():
    flows = sample(_topo(), CFG, NOW, sh=make_sh(failing=(4343,)))
    assert set(flows) == EXPECTED


def test_failed_host_ss_gives_no_host_flows():
    flows = sample(_topo(), CFG, NOW, sh=make_sh(host_rc=1))
    assert not {f for f in flows if "host" in (f.src, f.dst)}
    assert {f for f in flows if f.src == "container:web" and f.dst == "198.51.100.20"}
