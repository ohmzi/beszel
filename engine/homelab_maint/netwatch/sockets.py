"""netwatch.sockets: established TCP sockets, on the host and in each container's network namespace, as hourly Flow rows.

Read-only. The host is read with `ss`; each container with `nsenter -t <pid> -n ss`. A namespace that can't be read (the
container exited since `docker ps`) is skipped alone; the other namespaces still count. Metadata only: who, which port,
how many sockets, how many bytes. Never packet contents.

Direction, from the point of view of the namespace being read:
- in:  the peer is on the internet and the local port is one this namespace listens on.
- out: the peer is on the internet and the local port is not listening (an outbound connection).
- lan: the peer is a LAN or Tailscale address. If the local port is listening the flow is inbound: src is the
  "lan:<ip>" / "ts:<ip>" peer, dst the local entity, dport the local port. Otherwise the local entity connected out:
  src the local entity, dst "lan:<ip>" / "ts:<ip>", dport the remote port.
- svc: the peer is a container or the host. Counted from the client end only: the server end is skipped, because the
  client's namespace already counts the same connection, and counting both would double it.

Loopback sockets are skipped in every namespace: a process talking to itself is not network traffic. That covers
cloudflared's legs to its local services (the tunnel ports in `cfg["tunnel_local_ports"]`), whose real peers come from
Cloudflare (Task 5).

Known limitation: a client socket whose ephemeral port collides with a listening port is counted as "in".
"""
import re
from dataclasses import dataclass
from ipaddress import IPv6Address, ip_address

from homelab_maint import core
from homelab_maint.netwatch.entities import Topology, classify
from homelab_maint.netwatch.model import Flow

_SENT = re.compile(r"bytes_sent:(\d+)")
_RECEIVED = re.compile(r"bytes_received:(\d+)")
_SS_ESTABLISHED = ["ss", "-tinHn", "state", "established"]


@dataclass
class _Sock:
    laddr: str
    lport: int
    paddr: str
    pport: int
    sent: int = 0
    received: int = 0


def _addr(text: str) -> tuple[str, int]:
    """Split `addr:port` or `[addr]:port` into a normalised address and a port. IPv4-mapped IPv6 becomes IPv4."""
    host, _, port = text.rpartition(":")
    addr = ip_address(host.strip("[]"))
    if isinstance(addr, IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return str(addr), int(port)


def _parse(stdout: str) -> list[_Sock]:
    """`ss -tinHn` prints one header line per socket, then indented info lines carrying the byte counters."""
    socks: list[_Sock] = []
    cur: _Sock | None = None
    for line in stdout.splitlines():
        if not line.strip():
            continue
        if line[0].isspace():
            if cur is not None:
                if m := _SENT.search(line):
                    cur.sent = int(m.group(1))
                if m := _RECEIVED.search(line):
                    cur.received = int(m.group(1))
            continue
        cur = None
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            laddr, lport = _addr(parts[3])
            paddr, pport = _addr(parts[4])
        except ValueError:
            continue
        cur = _Sock(laddr, lport, paddr, pport)
        socks.append(cur)
    return socks


def _established(sh, argv: list[str]) -> list[_Sock] | None:
    res = sh(argv)
    if res.returncode != 0:
        return None
    return _parse(res.stdout)


def _listening_ports(sh, pid: int) -> set[int] | None:
    res = sh(["nsenter", "-t", str(pid), "-n", "ss", "-tlnH"])
    if res.returncode != 0:
        return None
    ports: set[int] = set()
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            ports.add(int(parts[3].rsplit(":", 1)[-1]))
        except ValueError:
            continue
    return ports


def _row(sock: _Sock, local: str, listening: set[int], topo: Topology, cfg: dict) -> tuple[str, str, str, int] | None:
    """(direction, src, dst, dport) for one socket, or None when it is not counted."""
    if ip_address(sock.laddr).is_loopback or ip_address(sock.paddr).is_loopback:
        return None  # a process talking to itself, in any namespace
    listens = sock.lport in listening
    peer = classify(sock.paddr, topo, cfg)
    if peer.kind == "internet":
        if listens:
            return ("in", sock.paddr, local, sock.lport)
        return ("out", local, sock.paddr, sock.pport)
    if peer.kind in ("lan", "tailscale"):
        ext = f"lan:{sock.paddr}" if peer.kind == "lan" else f"ts:{sock.paddr}"
        if listens:
            return ("lan", ext, local, sock.lport)
        return ("lan", local, ext, sock.pport)
    if listens:
        return None  # server end of a connection the client's namespace counts
    peer_entity = "host" if peer.kind == "host" else f"container:{peer.name}"
    return ("svc", local, peer_entity, sock.pport)


def sample(topo: Topology, cfg: dict, now: float, sh=core.sh) -> tuple[list[Flow], str]:
    """Hourly flows from every established socket on the host and in each container's namespace.

    Returns (flows, status). status is "ok", or "stale:<why>" when the sample can't be trusted:
    - "stale:topology": the topology is stale, so classification is unreliable. Nothing is read; returns ([], ...).
    - "stale:host-ss": the host's `ss` failed. The container flows that were read are still returned.
    A container whose namespace can't be read is skipped without making the status stale.
    """
    if topo.stale:
        return [], "stale:topology"
    bucket = int(now) - int(now) % 3600
    totals: dict[tuple[str, str, str, int], list[int]] = {}

    def count(socks: list[_Sock], local: str, listening: set[int]) -> None:
        for sock in socks:
            row = _row(sock, local, listening, topo, cfg)
            if row is None:
                continue
            t = totals.setdefault(row, [0, 0, 0])
            t[0] += 1
            t[1] += sock.received
            t[2] += sock.sent

    status = "ok"
    host = _established(sh, _SS_ESTABLISHED)
    if host is None:
        status = "stale:host-ss"
    else:
        count(host, "host", {port for proto, port in topo.listening if proto == "tcp"})
    for info in topo.containers.values():
        listening = _listening_ports(sh, info.pid)
        if listening is None:
            continue
        socks = _established(sh, ["nsenter", "-t", str(info.pid), "-n", *_SS_ESTABLISHED])
        if socks is None:
            continue
        count(socks, f"container:{info.name}", listening)

    flows = [Flow(bucket, d, s, t, p, "tcp", c, bi, bo) for (d, s, t, p), (c, bi, bo) in totals.items()]
    return flows, status
