"""netwatch.store: the Network Watch SQLite database (STATE_DIR/netwatch.db, mode 0600, WAL).

One owner for the schema. Tables: meta (key/value), flows and edge (hourly aggregates), events (log lines), baseline (what each
entity has been seen talking to), blocks (approved blocks) and signins (last successful sign-in per address and source).
Retention: prune() drops flows, edge, events and signins older than keep_days. meta, baseline and blocks are never pruned.
"""
import os
import sqlite3
from pathlib import Path

from homelab_maint import core
from homelab_maint.netwatch.model import EdgeRow, Flow, NetEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS flows (
    bucket    INTEGER NOT NULL,
    direction TEXT    NOT NULL,
    src       TEXT    NOT NULL,
    dst       TEXT    NOT NULL,
    dport     INTEGER NOT NULL,
    proto     TEXT    NOT NULL,
    conns     INTEGER NOT NULL,
    bytes_in  INTEGER NOT NULL,
    bytes_out INTEGER NOT NULL,
    PRIMARY KEY (bucket, direction, src, dst, dport, proto)
);
CREATE TABLE IF NOT EXISTS edge (
    bucket      INTEGER NOT NULL,
    zone        TEXT    NOT NULL,
    host        TEXT    NOT NULL,
    client_ip   TEXT    NOT NULL,
    country     TEXT    NOT NULL,
    asn         INTEGER NOT NULL,
    status      INTEGER NOT NULL,
    path_class  TEXT    NOT NULL,
    count       INTEGER NOT NULL,
    bytes       INTEGER NOT NULL,
    PRIMARY KEY (bucket, zone, host, client_ip, status, path_class)
);
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL    NOT NULL,
    kind   TEXT    NOT NULL,
    src    TEXT    NOT NULL,
    dst    TEXT    NOT NULL,
    dport  INTEGER NOT NULL,
    detail TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS baseline (
    entity     TEXT NOT NULL,
    peer_key   TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    PRIMARY KEY (entity, peer_key)
);
CREATE TABLE IF NOT EXISTS blocks (
    id          TEXT    PRIMARY KEY,
    ip          TEXT    NOT NULL,
    target      TEXT    NOT NULL,
    zone        TEXT    NOT NULL,
    created     REAL    NOT NULL,
    until       REAL    NULL,
    permanent   INTEGER NOT NULL,
    reason      TEXT    NOT NULL,
    cf_rule_id  TEXT    NULL,
    removed     REAL    NULL
);
CREATE TABLE IF NOT EXISTS signins (
    ip        TEXT NOT NULL,
    source    TEXT NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY (ip, source)
);
"""


def _default_path() -> Path:
    return core.STATE_DIR / "netwatch.db"


def open_db(path: Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the Network Watch database with the schema applied. The file is 0600."""
    path = Path(path) if path is not None else _default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    os.chmod(path, 0o600)
    db.executescript(_SCHEMA)
    return db


def meta_get(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(db: sqlite3.Connection, key: str, value: str) -> None:
    with db:
        db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def upsert_flows(db: sqlite3.Connection, rows: list[Flow]) -> int:
    """Add the counts and bytes of each flow to the stored hour for its key. Returns the number of rows given."""
    with db:
        db.executemany(
            "INSERT INTO flows (bucket, direction, src, dst, dport, proto, conns, bytes_in, bytes_out) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(bucket, direction, src, dst, dport, proto) DO UPDATE SET "
            "conns = conns + excluded.conns, bytes_in = bytes_in + excluded.bytes_in, bytes_out = bytes_out + excluded.bytes_out",
            [(r.bucket, r.direction, r.src, r.dst, r.dport, r.proto, r.conns, r.bytes_in, r.bytes_out) for r in rows],
        )
    return len(rows)


def upsert_edge(db: sqlite3.Connection, rows: list[EdgeRow]) -> int:
    """Store edge rows, replacing any stored row with the same key. Re-reading a Cloudflare window is idempotent."""
    with db:
        db.executemany(
            "INSERT INTO edge (bucket, zone, host, client_ip, country, asn, status, path_class, count, bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(bucket, zone, host, client_ip, status, path_class) DO UPDATE SET "
            "country = excluded.country, asn = excluded.asn, count = excluded.count, bytes = excluded.bytes",
            [(r.bucket, r.zone, r.host, r.client_ip, r.country, r.asn, r.status, r.path_class, r.count, r.bytes) for r in rows],
        )
    return len(rows)


def add_events(db: sqlite3.Connection, rows: list[NetEvent]) -> int:
    with db:
        db.executemany(
            "INSERT INTO events (ts, kind, src, dst, dport, detail) VALUES (?, ?, ?, ?, ?, ?)",
            [(r.ts, r.kind, r.src, r.dst, r.dport, r.detail) for r in rows],
        )
    return len(rows)


def prune(db: sqlite3.Connection, now: float, keep_days: int = 30) -> int:
    """Delete flows, edge rows, events and signins older than keep_days. Returns the total number of rows deleted.

    Flows and edge rows are compared by their hour bucket (epoch seconds at the start of the hour), events and signins by ts and last_seen.
    meta, baseline and blocks are never pruned.
    """
    cutoff = now - keep_days * 86400
    deleted = 0
    with db:
        for sql, params in (
            ("DELETE FROM flows WHERE bucket < ?", (cutoff,)),
            ("DELETE FROM edge WHERE bucket < ?", (cutoff,)),
            ("DELETE FROM events WHERE ts < ?", (cutoff,)),
            ("DELETE FROM signins WHERE last_seen < ?", (cutoff,)),
        ):
            deleted += db.execute(sql, params).rowcount
    return deleted
