"""Tests for homelab_maint/netwatch/cloudflare.py: the stdlib Cloudflare client, driven through a fake `http` callable only.

No test touches the network. The fake records every request so the tests can check the method, path, query, headers and body.
"""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import json
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from homelab_maint.netwatch.cloudflare import CF, CFUnavailable
from homelab_maint.netwatch.model import EdgeRow, NetEvent

FIX = Path(__file__).parent / "fixtures" / "netwatch"
API = "/client/v4"
TOKEN = "tok-SECRET-123"
T0 = "2026-10-19T00:00:00Z"
T1 = "2026-10-19T12:00:00Z"
CLASSES = {"api": r"^/api/", "login": r"login|wp-"}


def _epoch(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


class FakeResp:
    def __init__(self, body: bytes = b"", status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


class FakeHTTP:
    """Routes by 'METHOD /path' (query ignored). A route value is a fixture name, raw bytes, an exception to raise,
    a FakeResp, or a callable taking the request and returning any of those."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list = []
        self.timeouts: list = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        self.timeouts.append(timeout)
        key = f"{req.get_method()} {urlsplit(req.full_url).path}"
        spec = self.routes[key]
        if callable(spec) and not isinstance(spec, Exception):
            spec = spec(req)
        if isinstance(spec, Exception):
            raise spec
        if isinstance(spec, FakeResp):
            return spec
        if isinstance(spec, str):
            spec = (FIX / spec).read_bytes()
        return FakeResp(spec)

    def body(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].data)


def make(tmp_path, routes, account="acc-test", token=f"  {TOKEN}\n"):
    tok = tmp_path / "cloudflare.token"
    tok.write_text(token)
    http = FakeHTTP(routes)
    return CF(token_path=tok, http=http, account_id=account), http


def _json(payload: dict) -> bytes:
    return json.dumps(payload).encode()


# ---- auth and token handling -------------------------------------------------------------------------------------------

def test_sends_bearer_token_read_from_file_stripped(tmp_path):
    cf, http = make(tmp_path, {f"GET {API}/zones": "cf_zones.json"})
    cf.zones()
    assert http.requests[0].get_header("Authorization") == f"Bearer {TOKEN}"
    assert http.timeouts[0] == 30


def test_missing_token_file_raises_token(tmp_path):
    http = FakeHTTP({})
    cf = CF(token_path=tmp_path / "absent.token", http=http, account_id="acc-test")
    with pytest.raises(CFUnavailable) as info:
        cf.zones()
    assert str(info.value) == "token"
    assert http.requests == []


def test_empty_token_file_raises_token(tmp_path):
    cf, http = make(tmp_path, {}, token="  \n")
    with pytest.raises(CFUnavailable) as info:
        cf.zones()
    assert str(info.value) == "token"
    assert http.requests == []


def test_repr_never_shows_the_token(tmp_path):
    cf, _ = make(tmp_path, {f"GET {API}/zones": "cf_zones.json"})
    assert TOKEN not in repr(cf)
    cf.zones()
    assert TOKEN not in repr(cf)


# ---- zones and tunnels -------------------------------------------------------------------------------------------------

def test_zones_maps_name_to_id(tmp_path):
    cf, http = make(tmp_path, {f"GET {API}/zones": "cf_zones.json"})
    assert cf.zones() == {"example.test": "zone-a", "second.example.test": "zone-b"}
    assert parse_qs(urlsplit(http.requests[0].full_url).query) == {"per_page": ["50"]}


def test_tunnel_hostnames_combines_tunnels_and_skips_catch_all(tmp_path):
    routes = {
        f"GET {API}/accounts/acc-test/cfd_tunnel": "cf_tunnels.json",
        f"GET {API}/accounts/acc-test/cfd_tunnel/tun-1/configurations": "cf_tunnel_config_1.json",
        f"GET {API}/accounts/acc-test/cfd_tunnel/tun-2/configurations": "cf_tunnel_config_2.json",
    }
    cf, http = make(tmp_path, routes)
    assert cf.tunnel_hostnames() == {
        "app.example.test": "http://127.0.0.1:8080",
        "plex.example.test": "http://127.0.0.1:32400",
        "wiki.second.example.test": "http://127.0.0.1:3000",
    }
    listing = urlsplit(http.requests[0].full_url)
    assert parse_qs(listing.query) == {"is_deleted": ["false"]}


def test_tunnel_hostnames_needs_an_account_id(tmp_path):
    cf, http = make(tmp_path, {}, account=None)
    with pytest.raises(CFUnavailable) as info:
        cf.tunnel_hostnames()
    assert str(info.value) == "account"
    assert http.requests == []


# ---- edge requests -----------------------------------------------------------------------------------------------------

def _edge(tmp_path, fixture="cf_edge.json"):
    cf, http = make(tmp_path, {"POST /client/v4/graphql": fixture})
    rows = cf.edge_requests("zone-a", _epoch(T0), _epoch(T1), CLASSES)
    return rows, http


def test_edge_requests_maps_groups_to_rows_with_path_class(tmp_path):
    rows, _ = _edge(tmp_path)
    by_ip = {r.client_ip: r for r in rows}
    assert by_ip["203.0.113.10"] == EdgeRow(
        bucket=_epoch("2026-10-19T10:00:00Z"), zone="zone-a", host="app.example.test", client_ip="203.0.113.10",
        country="NL", asn=64500, status=200, path_class="api", count=12, bytes=4096,
    )
    assert by_ip["203.0.113.20"] == EdgeRow(
        bucket=_epoch("2026-10-19T11:00:00Z"), zone="zone-a", host="plex.example.test", client_ip="203.0.113.20",
        country="", asn=0, status=200, path_class="other", count=5, bytes=900,
    )


def test_edge_requests_stores_no_raw_path(tmp_path):
    rows, _ = _edge(tmp_path)
    assert len(rows) == 3
    for row in rows:
        assert isinstance(row, EdgeRow)
        assert not hasattr(row, "path")
        assert "items" not in repr(row) and "wp-login" not in repr(row)


def test_v6_client_ip_is_preserved(tmp_path):
    rows, _ = _edge(tmp_path)
    v6 = [r for r in rows if ":" in r.client_ip]
    assert [(r.client_ip, r.path_class, r.status, r.asn) for r in v6] == [("2001:db8::10", "login", 404, 64501)]


def test_edge_requests_sums_groups_that_share_a_key(tmp_path):
    rows, _ = _edge(tmp_path, "cf_edge_same_class.json")
    assert rows == [EdgeRow(
        bucket=_epoch("2026-10-19T10:00:00Z"), zone="zone-a", host="app.example.test", client_ip="203.0.113.30",
        country="DE", asn=64502, status=200, path_class="api", count=10, bytes=300,
    )]


def test_edge_requests_posts_query_and_variables(tmp_path):
    _, http = _edge(tmp_path)
    body = http.body()
    assert set(body) == {"query", "variables"}
    assert "httpRequestsAdaptiveGroups" in body["query"]
    assert "edgeResponseBytes" in body["query"]
    assert body["variables"] == {"z": "zone-a", "s": T0, "e": T1}


def test_edge_requests_empty_zone_list_is_no_rows(tmp_path):
    cf, _ = make(tmp_path, {"POST /client/v4/graphql": _json({"data": {"viewer": {"zones": []}}, "errors": None})})
    assert cf.edge_requests("zone-a", _epoch(T0), _epoch(T1), CLASSES) == []


# ---- security events ---------------------------------------------------------------------------------------------------

def test_security_events_map_to_net_events(tmp_path):
    cf, http = make(tmp_path, {"POST /client/v4/graphql": "cf_security.json"})
    events = cf.security_events("zone-a", _epoch(T0), _epoch(T1))
    assert events == [
        NetEvent(ts=_epoch("2026-10-19T10:15:00Z"), kind="cf_security", src="2001:db8::66",
                 dst="app.example.test", dport=443, detail="block/waf"),
        NetEvent(ts=_epoch("2026-10-19T10:16:30Z"), kind="cf_security", src="203.0.113.66",
                 dst="plex.example.test", dport=443, detail="managed_challenge/ratelimit"),
    ]
    assert "firewallEventsAdaptive" in http.body()["query"]


# ---- GraphQL and HTTP failures -----------------------------------------------------------------------------------------

def test_graphql_error_with_http_200_is_unavailable(tmp_path):
    cf, _ = make(tmp_path, {"POST /client/v4/graphql": "cf_graphql_error.json"})
    with pytest.raises(CFUnavailable) as info:
        cf.edge_requests("zone-a", _epoch(T0), _epoch(T1), CLASSES)
    reason = info.value.reason
    assert reason.startswith("graphql:") and "retention" in reason
    assert len(reason) <= len("graphql:") + 80


def test_graphql_error_message_is_scrubbed_of_the_token(tmp_path):
    leak = _json({"data": None, "errors": [{"message": f"bad credential {TOKEN} for zone-a"}]})
    cf, _ = make(tmp_path, {"POST /client/v4/graphql": leak})
    with pytest.raises(CFUnavailable) as info:
        cf.security_events("zone-a", _epoch(T0), _epoch(T1))
    assert TOKEN not in str(info.value)
    assert info.value.reason.startswith("graphql:")


def test_429_raises_with_reason_and_no_token(tmp_path):
    err = urllib.error.HTTPError(f"https://api.cloudflare.com{API}/zones", 429, "Too Many Requests", {}, None)
    cf, _ = make(tmp_path, {f"GET {API}/zones": err})
    with pytest.raises(CFUnavailable) as info:
        cf.zones()
    assert str(info.value) == "429"
    assert TOKEN not in str(info.value) and TOKEN not in repr(info.value)


@pytest.mark.parametrize("code,reason", [(503, "http-503"), (500, "http-500"), (403, "http-403"), (404, "http-404")])
def test_http_errors_map_to_short_reasons(tmp_path, code, reason):
    err = urllib.error.HTTPError(f"https://api.cloudflare.com{API}/zones", code, "x", {}, None)
    cf, _ = make(tmp_path, {f"GET {API}/zones": err})
    with pytest.raises(CFUnavailable) as info:
        cf.zones()
    assert str(info.value) == reason


def test_status_429_on_a_returned_response_is_unavailable(tmp_path):
    cf, _ = make(tmp_path, {f"GET {API}/zones": FakeResp(b"{}", status=429)})
    with pytest.raises(CFUnavailable) as info:
        cf.zones()
    assert str(info.value) == "429"


@pytest.mark.parametrize("exc", [urllib.error.URLError("refused"), TimeoutError("timed out")])
def test_network_failures_map_to_network(tmp_path, exc):
    cf, _ = make(tmp_path, {f"GET {API}/zones": exc})
    with pytest.raises(CFUnavailable) as info:
        cf.zones()
    assert str(info.value) == "network"


def test_rest_success_false_is_unavailable(tmp_path):
    cf, _ = make(tmp_path, {f"GET {API}/zones": _json({"success": False, "errors": [{"code": 1}], "result": None})})
    with pytest.raises(CFUnavailable):
        cf.zones()


# ---- IP access rules --------------------------------------------------------------------------------------------------

def test_create_access_rule_v4_posts_block_body(tmp_path):
    cf, http = make(tmp_path, {f"POST {API}/zones/zone-a/firewall/access_rules/rules": "cf_access_rule_created.json"})
    rule_id = cf.create_access_rule("zone-a", "203.0.113.66", "netwatch: scanner")
    assert rule_id == "rule-new"
    assert http.requests[0].get_method() == "POST"
    assert http.body() == {
        "mode": "block",
        "configuration": {"target": "ip", "value": "203.0.113.66"},
        "notes": "netwatch: scanner",
    }


def test_create_access_rule_v6_uses_ip6_target(tmp_path):
    cf, http = make(tmp_path, {f"POST {API}/zones/zone-a/firewall/access_rules/rules": "cf_access_rule_created.json"})
    cf.create_access_rule("zone-a", "2001:db8::66", "netwatch: scanner")
    assert http.body()["configuration"] == {"target": "ip6", "value": "2001:db8::66"}


def test_delete_access_rule_sends_delete(tmp_path):
    ok = _json({"success": True, "errors": [], "result": {"id": "rule-1"}})
    cf, http = make(tmp_path, {f"DELETE {API}/zones/zone-a/firewall/access_rules/rules/rule-1": ok})
    assert cf.delete_access_rule("zone-a", "rule-1") is None
    assert http.requests[0].get_method() == "DELETE"


def test_list_access_rules_follows_pages(tmp_path):
    def page(req):
        number = parse_qs(urlsplit(req.full_url).query)["page"][0]
        return "cf_access_rules_page1.json" if number == "1" else "cf_access_rules_page2.json"

    cf, http = make(tmp_path, {f"GET {API}/zones/zone-a/firewall/access_rules/rules": page})
    rules = cf.list_access_rules("zone-a")
    assert [r["id"] for r in rules] == ["rule-1", "rule-2", "rule-3"]
    queries = [parse_qs(urlsplit(r.full_url).query) for r in http.requests]
    assert [q["page"] for q in queries] == [["1"], ["2"]]
    assert all(q["per_page"] == ["100"] for q in queries)
