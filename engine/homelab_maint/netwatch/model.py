"""netwatch.model: the row types the collectors write and the store keeps. Frozen, no behaviour.

Entity names are `container:<name>`, `host`, `lan:<ip>`, `ts:<ip>` or a bare IP.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Flow:
    """Connection counts and bytes per hour, per (direction, src, dst, port, proto). `bucket` is the epoch hour (seconds)."""
    bucket: int
    direction: str  # in | out | svc | lan
    src: str
    dst: str
    dport: int
    proto: str
    conns: int
    bytes_in: int
    bytes_out: int


@dataclass(frozen=True)
class EdgeRow:
    """Cloudflare edge requests per hour. Metadata only: `path_class` is a label, never a raw request path."""
    bucket: int
    zone: str
    host: str
    client_ip: str
    country: str
    asn: int
    status: int
    path_class: str
    count: int
    bytes: int


@dataclass(frozen=True)
class NetEvent:
    """One thing worth a line in the log. `kind` is fw_block | cf_security | plex_exploit | codec_unexplained | auth_fail | auth_ok."""
    ts: float
    kind: str
    src: str
    dst: str
    dport: int
    detail: str
