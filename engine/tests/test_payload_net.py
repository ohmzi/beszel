"""Tests for payloads.net (the ops-net widget payload): the net_watch entry of status.json only, under 4 KB, colour words precomputed.

Documentation ranges only (203.0.113.0/24, 198.51.100.0/24) and example.test hosts.
"""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import json

from homelab_maint import payloads

NOW = 1798761600.0


def _status(items, metrics=None, status="warn", last_run=NOW - 120):
    entry = {"title": "Network watch", "klass": "C0", "tier": "check", "status": status, "summary": f"{status}: Network: 3 findings",
             "last_run": last_run, "metrics": metrics if metrics is not None else {
                 "in24": 412, "out24": 1893, "svc24": 96, "lan24": 40, "edge24": 1204, "flags": len(items), "blocked": 2,
                 "stale_sources": 0, "top_talkers": "203.0.113.7 DE 812; 198.51.100.23 US 310; 203.0.113.40 NL 64"},
             "items": items, "alert": True, "mode": "check"}
    return {"generated_at": NOW - 60, "overall": status, "tasks": {"net_watch": entry, "guard": {"status": "ok", "metrics": {}}}}


def _row(i: int, severity: str = "warn") -> dict:
    return {"rule": "ssh_burst", "severity": severity, "src": f"203.0.113.{i % 250 + 1}", "target": "host" + "x" * 60,
            "why": "5 failed sign-ins in 10 minutes " + "y" * 100, "count": 5 + i, "id": f"{i:016x}"}


def test_payload_stays_under_4_kb_with_a_full_item_list_and_long_text():
    items = [_row(i, "crit" if i < 3 else "warn") for i in range(12)]
    st = _status(items, metrics={"in24": 10**9, "out24": 10**9, "svc24": 10**9, "lan24": 10**9, "edge24": 10**9, "flags": 12,
                                 "blocked": 99, "stale_sources": 3, "top_talkers": "203.0.113.7 " + "Z" * 400})
    p = payloads.build("net", st, NOW)
    size = len(json.dumps(p, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    assert size < 4096
    assert len(p["flags"]) == payloads.NET_FLAGS == 5
    assert len(p["tt"]) <= 120


def test_counts_and_top_talkers_come_from_the_net_watch_metrics():
    p = payloads.build("net", _status([_row(0)]), NOW)
    assert p["dirs"] == [{"l": "in", "v": "412"}, {"l": "out", "v": "1893"}, {"l": "svc", "v": "96"}, {"l": "lan", "v": "40"}]
    assert p["edge"] == "1204" and p["blocked"] == "2" and p["stale_src"] == "0"
    assert p["tt"].startswith("203.0.113.7 DE 812")
    assert p["head"] == "1 open"


def test_colour_words_are_precomputed_for_the_flags_and_the_header():
    items = [_row(0, "crit"), _row(1, "warn")]
    p = payloads.build("net", _status(items, status="crit"), NOW)
    assert [f["c"] for f in p["flags"]] == ["red", "yellow"]
    assert p["hc"] == "red"
    assert p["level"] == "crit" and p["c"] == "red"
    assert p["flags"][0]["n"] == "5" and p["flags"][0]["r"] == "ssh_burst"


def test_a_healthy_network_says_no_flags_and_is_teal():
    p = payloads.build("net", _status([], metrics={"flags": 0}, status="ok"), NOW)
    assert p["flags"] == [] and p["head"] == "no flags" and p["hc"] == "teal"


def test_missing_net_watch_entry_gives_every_key_with_no_data():
    p = payloads.build("net", {"generated_at": NOW - 60, "tasks": {}}, NOW)
    assert p["head"] == "no data" and p["hc"] == "gray" and p["flags"] == [] and p["tt"] == ""
    assert [d["v"] for d in p["dirs"]] == ["--", "--", "--", "--"]
    assert p["edge"] == "--" and p["blocked"] == "--"


def test_no_status_at_all_is_a_stale_payload_with_the_same_keys():
    p = payloads.build("net", {}, NOW)
    assert p["stale"] is True and "dirs" in p and "flags" in p
