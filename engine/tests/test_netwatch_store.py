"""Tests for homelab_maint/netwatch/store.py: the SQLite store for Network Watch (schema, upserts, prune, meta, file mode)."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import os
import stat
import time

from homelab_maint.netwatch import store
from homelab_maint.netwatch.model import EdgeRow, Flow, NetEvent

DAY = 86400


def _hour(ts: float) -> int:
    return int(ts) // 3600 * 3600


def test_prune_keeps_29_days_drops_31(tmp_path):
    now = time.time()
    db = store.open_db(tmp_path / "n.db")
    old_ok, old_gone = now - 29 * DAY, now - 31 * DAY
    store.upsert_flows(db, [
        Flow(_hour(old_ok), "in", "198.51.100.7", "container:web", 443, "tcp", 1, 10, 20),
        Flow(_hour(old_gone), "in", "198.51.100.8", "container:web", 443, "tcp", 1, 10, 20),
    ])
    store.upsert_edge(db, [
        EdgeRow(_hour(old_ok), "example.test", "app.example.test", "203.0.113.5", "US", 64500, 200, "page", 3, 900),
        EdgeRow(_hour(old_gone), "example.test", "app.example.test", "203.0.113.6", "US", 64500, 200, "page", 3, 900),
    ])
    store.add_events(db, [
        NetEvent(old_ok, "auth_fail", "203.0.113.9", "host", 22, "ssh"),
        NetEvent(old_gone, "auth_fail", "203.0.113.10", "host", 22, "ssh"),
    ])

    deleted = store.prune(db, now, keep_days=30)

    assert deleted == 3
    assert [r[0] for r in db.execute("SELECT src FROM flows")] == ["198.51.100.7"]
    assert [r[0] for r in db.execute("SELECT client_ip FROM edge")] == ["203.0.113.5"]
    assert [r[0] for r in db.execute("SELECT src FROM events")] == ["203.0.113.9"]


def test_upsert_flows_sums(tmp_path):
    db = store.open_db(tmp_path / "n.db")
    bucket = _hour(time.time())
    key = (bucket, "in", "198.51.100.7", "container:web", 443, "tcp")
    store.upsert_flows(db, [Flow(*key, 2, 100, 200)])
    store.upsert_flows(db, [Flow(*key, 3, 50, 25)])

    rows = db.execute("SELECT conns, bytes_in, bytes_out FROM flows").fetchall()
    assert rows == [(5, 150, 225)]


def test_upsert_edge_idempotent(tmp_path):
    db = store.open_db(tmp_path / "n.db")
    row = EdgeRow(_hour(time.time()), "example.test", "app.example.test", "203.0.113.5", "US", 64500, 200, "page", 4, 1200)
    store.upsert_edge(db, [row])
    store.upsert_edge(db, [row])

    rows = db.execute("SELECT count, bytes FROM edge").fetchall()
    assert rows == [(4, 1200)]


def test_db_mode_0600(tmp_path):
    path = tmp_path / "n.db"
    db = store.open_db(path)
    store.meta_set(db, "k", "v")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_meta_persists_across_reopen(tmp_path):
    path = tmp_path / "n.db"
    db = store.open_db(path)
    store.meta_set(db, "learning_started", "1700000000")
    db.close()

    reopened = store.open_db(path)
    assert store.meta_get(reopened, "learning_started") == "1700000000"
    assert store.meta_get(reopened, "missing") is None
