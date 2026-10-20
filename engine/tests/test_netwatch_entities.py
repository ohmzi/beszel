"""Tests for homelab_maint/netwatch/entities.py: topology from docker (faked), peer classification, hard ranges."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import json
import subprocess
from ipaddress import ip_network
from pathlib import Path

from homelab_maint.netwatch.entities import HARD_RANGES, Topology, classify, load_topology

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "netwatch" / "docker_networks.json"

CONTAINER_INSPECT = [{
    "Name": "/web",
    "State": {"Pid": 4242},
    "Config": {"Labels": {"com.docker.compose.project": "stack"}},
    "NetworkSettings": {"Networks": {"stack_default": {"IPAddress": "172.28.0.5", "GlobalIPv6Address": "fd00::1"}}},
}]


def _cp(stdout="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


def make_sh(ss_tcp="", ss_udp="", docker_rc=0, hostname="192.168.40.20 100.101.1.20\n"):
    """Fake core.sh. Answers by argv; never runs anything real."""
    networks = FIXTURE.read_text()

    def fake(cmd, timeout=60, check=False, input_=None, env=None):
        if cmd[:2] == ["docker", "ps"]:
            return _cp("c1abc\n" if docker_rc == 0 else "", docker_rc)
        if cmd[:2] == ["docker", "inspect"]:
            return _cp(json.dumps(CONTAINER_INSPECT), docker_rc)
        if cmd[:3] == ["docker", "network", "ls"]:
            return _cp("aa11bb22cc33\ndd44ee55ff66\n", docker_rc)
        if cmd[:3] == ["docker", "network", "inspect"]:
            return _cp(networks, docker_rc)
        if cmd[0] == "hostname":
            return _cp(hostname)
        if cmd[0] == "ss":
            return _cp(ss_udp if "-u" in cmd[1] else ss_tcp)
        raise AssertionError(f"unexpected command {cmd}")
    return fake


def _topo():
    return load_topology(make_sh())


def test_container_ip_is_container():
    assert classify("172.28.0.5", _topo(), {}).kind == "container"
    assert classify("172.28.0.5", _topo(), {}).name == "web"


def test_ipv6_on_docker_subnet_is_container():
    p = classify("fd00::1", _topo(), {})
    assert (p.kind, p.name) == ("container", "web")


def test_lan_address():
    assert classify("192.168.40.12", _topo(), {}).kind == "lan"


def test_tailscale_address():
    assert classify("100.101.1.2", _topo(), {}).kind == "tailscale"


def test_public_v4_is_internet():
    assert classify("203.0.113.9", _topo(), {}).kind == "internet"


def test_public_v6_is_internet():
    assert classify("2001:db8::5", _topo(), {}).kind == "internet"


def test_docker_gateway_and_host_addresses_are_host():
    topo = _topo()
    assert classify("172.28.0.1", topo, {}).kind == "host"
    assert classify("fd00::fe", topo, {}).kind == "host"
    assert classify("192.168.40.20", topo, {}).kind == "host"
    assert classify("127.0.0.1", topo, {}).kind == "host"


def test_config_ranges_override_defaults():
    cfg = {"lan_cidrs": ["10.9.0.0/16"], "tailscale_cidrs": ["100.100.0.0/16"]}
    assert classify("10.9.3.4", _topo(), cfg).kind == "lan"
    assert classify("192.168.40.12", _topo(), cfg).kind == "internet"
    assert classify("100.100.1.1", _topo(), cfg).kind == "tailscale"


def test_hard_ranges_contain_lan_tailscale_and_docker_subnets():
    ranges = HARD_RANGES(_topo())
    assert ip_network("192.168.40.0/24") in ranges
    assert ip_network("100.64.0.0/10") in ranges
    assert ip_network("172.28.0.0/16") in ranges
    assert ip_network("fd00::/64") in ranges
    assert ip_network("127.0.0.0/8") in ranges
    assert ip_network("::1/128") in ranges
    assert ip_network("169.254.0.0/16") in ranges
    assert ip_network("fe80::/10") in ranges


def test_load_topology_reads_containers_and_listening_ports():
    ss = "LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*\nLISTEN 0 4096 [::]:8080 [::]:*\n"
    topo = load_topology(make_sh(ss_tcp=ss))
    assert topo.stale is False
    assert topo.containers["web"].pid == 4242
    assert topo.containers["web"].project == "stack"
    assert topo.compose["web"] == "stack"
    assert ("tcp", 22) in topo.listening and ("tcp", 8080) in topo.listening


def test_when_docker_fails_topology_is_empty_and_stale():
    topo = load_topology(make_sh(docker_rc=1))
    assert isinstance(topo, Topology)
    assert topo.stale is True
    assert topo.containers == {}
    assert classify("172.28.0.5", topo, {}).kind == "internet"


def test_docker_missing_binary_does_not_raise():
    def boom(cmd, **kw):
        return _cp("", 127)
    topo = load_topology(boom)
    assert topo.stale is True
