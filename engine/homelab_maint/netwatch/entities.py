"""netwatch.entities: who is on this host. Docker topology, listening ports and peer classification.

Everything here reads state; nothing changes docker, the firewall or the network. `sh` is core.sh by default and is
passed as a parameter so tests can fake it. When a source can't be read the topology comes back `stale=True` and empty
rather than raising: the caller must not treat a stale view as "nothing here".
"""
import json
from dataclasses import dataclass, field
from ipaddress import ip_address, ip_network

from homelab_maint import core

COMPOSE_LABEL = "com.docker.compose.project"
DEFAULT_LAN = ["192.168.40.0/24"]
DEFAULT_TAILSCALE = ["100.64.0.0/10"]
# Never overridable: these are the owner's hard ranges, whatever [netwatch] says about LAN and Tailscale.
_FIXED_HARD = ["192.168.40.0/24", "100.64.0.0/10", "127.0.0.0/8", "::1/128", "169.254.0.0/16", "fe80::/10"]


@dataclass
class ContainerInfo:
    name: str
    pid: int
    ips: list[str]
    project: str


@dataclass
class Topology:
    containers: dict[str, ContainerInfo]
    subnets: list  # ip_network, both families
    compose: dict[str, str]  # container name -> compose project ("" when not a compose container)
    listening: set[tuple[str, int]]  # ("tcp"|"udp", port) bound on the host
    stale: bool = False
    host_ips: frozenset = field(default_factory=frozenset)  # gateways and the host's own addresses

    @classmethod
    def empty_stale(cls) -> "Topology":
        return cls(containers={}, subnets=[], compose={}, listening=set(), stale=True)


@dataclass(frozen=True)
class Peer:
    kind: str  # container | host | lan | tailscale | internet
    name: str


def _cp_ok(res) -> bool:
    return res.returncode == 0


def _json(res):
    return json.loads(res.stdout)


def _listening(sh) -> tuple[set[tuple[str, int]], bool]:
    found: set[tuple[str, int]] = set()
    ok = True
    for proto, flag in (("tcp", "-tlnH"), ("udp", "-ulnH")):
        res = sh(["ss", flag])
        if not _cp_ok(res):
            ok = False
            continue
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            try:
                found.add((proto, int(parts[3].rsplit(":", 1)[-1])))
            except ValueError:
                continue
    return found, ok


def load_topology(sh=core.sh) -> Topology:
    """Read containers, networks, host addresses and listening ports. Any failure returns an empty stale Topology."""
    try:
        ps = sh(["docker", "ps", "-q"])
        if not _cp_ok(ps):
            return Topology.empty_stale()
        ids = ps.stdout.split()

        containers: dict[str, ContainerInfo] = {}
        compose: dict[str, str] = {}
        if ids:
            ins = sh(["docker", "inspect", *ids])
            if not _cp_ok(ins):
                return Topology.empty_stale()
            for c in _json(ins):
                name = c["Name"].lstrip("/")
                project = (c.get("Config") or {}).get("Labels", {}).get(COMPOSE_LABEL, "") or ""
                ips = []
                for net in (c.get("NetworkSettings") or {}).get("Networks", {}).values():
                    ips += [ip for ip in (net.get("IPAddress"), net.get("GlobalIPv6Address")) if ip]
                containers[name] = ContainerInfo(name=name, pid=int(c.get("State", {}).get("Pid", 0)), ips=ips, project=project)
                compose[name] = project

        nets_ls = sh(["docker", "network", "ls", "-q"])
        if not _cp_ok(nets_ls):
            return Topology.empty_stale()
        net_ids = nets_ls.stdout.split()
        subnets, gateways = [], set()
        if net_ids:
            nets = sh(["docker", "network", "inspect", *net_ids])
            if not _cp_ok(nets):
                return Topology.empty_stale()
            for net in _json(nets):
                for cfg in (net.get("IPAM") or {}).get("Config") or []:
                    if cfg.get("Subnet"):
                        subnets.append(ip_network(cfg["Subnet"], strict=False))
                    if cfg.get("Gateway"):
                        gateways.add(ip_address(cfg["Gateway"]))

        host_ips = set(gateways)
        hn = sh(["hostname", "-I"])
        host_ok = _cp_ok(hn)
        if host_ok:
            host_ips |= {ip_address(tok) for tok in hn.stdout.split()}

        listening, ss_ok = _listening(sh)
        return Topology(containers=containers, subnets=subnets, compose=compose, listening=listening,
                        stale=not (host_ok and ss_ok), host_ips=frozenset(host_ips))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return Topology.empty_stale()


def _networks(cfg: dict, key: str, default: list[str]) -> list:
    return [ip_network(c, strict=False) for c in (cfg.get(key) or default)]


def classify(ip: str, topo: Topology, cfg: dict) -> Peer:
    """Name the peer at `ip`: a container, this host, a LAN or Tailscale address, or the internet.

    Host addresses win over everything (a gateway sits inside a Docker subnet). Then container addresses, then the
    configured LAN and Tailscale ranges, and anything else is the internet.
    """
    addr = ip_address(ip)
    if addr.is_loopback or addr in topo.host_ips:
        return Peer("host", "host")
    for info in topo.containers.values():
        if any(ip_address(x) == addr for x in info.ips):
            return Peer("container", info.name)
    if any(addr in net for net in topo.subnets):
        return Peer("container", "unknown")
    if any(addr in net for net in _networks(cfg, "lan_cidrs", DEFAULT_LAN)):
        return Peer("lan", ip)
    if any(addr in net for net in _networks(cfg, "tailscale_cidrs", DEFAULT_TAILSCALE)):
        return Peer("tailscale", ip)
    return Peer("internet", ip)


def HARD_RANGES(topo: Topology) -> list:
    """Addresses the owner must never block: LAN, Tailscale, every Docker subnet, loopback and link-local, both families."""
    return [ip_network(c) for c in _FIXED_HARD] + list(topo.subnets)
