"""netwatch.cloudflare: a small stdlib client for Cloudflare's v4 REST API and GraphQL Analytics API.

Read-mostly: zones, tunnel hostnames, edge request counts, firewall events, and per-zone IP access rules (the only write
path, used for blocks the owner asked for). The token is read lazily from a file, cached privately, sent only in the
Authorization header, and never placed in a reason, an exception, a repr or a log line.

`http` is called like urllib.request.urlopen(request, timeout=...) so tests inject a fake. Every failure surfaces as
CFUnavailable(reason) with a short fixed reason, not a Cloudflare error body.
"""
import ipaddress
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from homelab_maint import core
from homelab_maint.netwatch.model import EdgeRow, NetEvent

BASE_URL = "https://api.cloudflare.com/client/v4"
TIMEOUT = 30
_HTTPS_PORT = 443


class CFUnavailable(Exception):
    """Cloudflare cannot be read right now. `reason` is short and never contains the token."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _rfc3339(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def _status_reason(code: int) -> str:
    return str(code) if code == 429 else f"http-{code}"


def _zone_query(selection: str) -> str:
    return (
        "query ($z: String!, $s: Time!, $e: Time!) { viewer { zones(filter: {zoneTag: $z}) { "
        + selection
        + " } } }"
    )


EDGE_SELECTION = (
    "httpRequestsAdaptiveGroups(filter: {datetime_geq: $s, datetime_leq: $e}, limit: 5000, orderBy: [count_DESC]) "
    "{ count sum { edgeResponseBytes } dimensions { clientIP clientRequestHTTPHost clientRequestPath "
    "edgeResponseStatus clientCountryName clientAsn datetimeHour } }"
)
SECURITY_SELECTION = (
    "firewallEventsAdaptive(filter: {datetime_geq: $s, datetime_leq: $e}, limit: 1000, orderBy: [datetime_DESC]) "
    "{ action source clientIP clientRequestHTTPHost datetime }"
)


class CF:
    def __init__(self, token_path: Path = core.CONF_DIR / "cloudflare.token", http=urllib.request.urlopen,
                 account_id: str | None = None):
        self._token_path = Path(token_path)
        self._http = http
        self._account_id = account_id
        self._tok: str | None = None

    def __repr__(self) -> str:
        return f"CF(token_path={str(self._token_path)!r}, account_id={self._account_id!r})"

    # ---- transport -----------------------------------------------------------------------------------------------------

    def _load_token(self) -> str:
        if self._tok is None:
            try:
                text = self._token_path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                raise CFUnavailable("token") from None
            if not text:
                raise CFUnavailable("token")
            self._tok = text
        return self._tok

    def _scrub(self, text: str) -> str:
        if self._tok:
            text = text.replace(self._tok, "[token]")
        return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())

    def _request(self, method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict:
        token = self._load_token()
        url = BASE_URL + path + (f"?{urlencode(query)}" if query else "")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._http(req, timeout=TIMEOUT) as resp:
                status = getattr(resp, "status", 200)
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise CFUnavailable(_status_reason(exc.code)) from None
        except (urllib.error.URLError, OSError):
            raise CFUnavailable("network") from None
        if status >= 400:
            raise CFUnavailable(_status_reason(status))
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            raise CFUnavailable("bad-response") from None
        if not isinstance(payload, dict):
            raise CFUnavailable("bad-response")
        if payload.get("success") is False:
            raise CFUnavailable("api")
        return payload

    def _graphql_nodes(self, selection: str, zone_id: str, start: float, end: float, dataset: str) -> list:
        body = {
            "query": _zone_query(selection),
            "variables": {"z": zone_id, "s": _rfc3339(start), "e": _rfc3339(end)},
        }
        payload = self._request("POST", "/graphql", body)
        errors = payload.get("errors")
        if errors:
            first = errors[0]
            message = first.get("message", "") if isinstance(first, dict) else str(first)
            raise CFUnavailable("graphql:" + self._scrub(str(message))[:80])
        data = payload.get("data")
        if not isinstance(data, dict):
            raise CFUnavailable("bad-response")
        zones = (data.get("viewer") or {}).get("zones") or []
        if not zones:
            return []
        return zones[0].get(dataset) or []

    def _account(self) -> str:
        if not self._account_id:
            raise CFUnavailable("account")
        return self._account_id

    # ---- reads ---------------------------------------------------------------------------------------------------------

    def zones(self) -> dict[str, str]:
        """Zone name to zone id."""
        payload = self._request("GET", "/zones", query={"per_page": 50})
        return {z["name"]: z["id"] for z in payload.get("result") or []}

    def tunnel_hostnames(self) -> dict[str, str]:
        """Hostname to the local service URL it is routed to, across every live tunnel on the account."""
        account = quote(self._account(), safe="")
        listing = self._request("GET", f"/accounts/{account}/cfd_tunnel", query={"is_deleted": "false"})
        hostnames: dict[str, str] = {}
        for tunnel in listing.get("result") or []:
            tid = quote(str(tunnel["id"]), safe="")
            cfg = self._request("GET", f"/accounts/{account}/cfd_tunnel/{tid}/configurations")
            ingress = ((cfg.get("result") or {}).get("config") or {}).get("ingress") or []
            for rule in ingress:
                host = rule.get("hostname")
                if host:  # the catch-all rule has no hostname
                    hostnames[host] = rule.get("service", "")
        return hostnames

    def edge_requests(self, zone_id: str, start: float, end: float, path_classes: dict[str, str]) -> list[EdgeRow]:
        """Edge request counts for one zone and window. Raw paths are reduced to a path_class label (the first regex
        in `path_classes` that matches, else "other") and rows that land on the same key are summed."""
        compiled = [(name, re.compile(pattern)) for name, pattern in path_classes.items()]

        def path_class(path: str | None) -> str:
            for name, rx in compiled:
                if rx.search(path or ""):
                    return name
            return "other"

        nodes = self._graphql_nodes(EDGE_SELECTION, zone_id, start, end, "httpRequestsAdaptiveGroups")
        merged: dict[tuple, list] = {}
        for node in nodes:
            d = node["dimensions"]
            key = (
                _epoch(d["datetimeHour"]), zone_id, d["clientRequestHTTPHost"], d["clientIP"],
                int(d["edgeResponseStatus"]), path_class(d.get("clientRequestPath")),
            )
            row = merged.setdefault(key, [d.get("clientCountryName") or "", int(d.get("clientAsn") or 0), 0, 0])
            row[2] += int(node.get("count") or 0)
            row[3] += int((node.get("sum") or {}).get("edgeResponseBytes") or 0)
        return [
            EdgeRow(bucket=k[0], zone=k[1], host=k[2], client_ip=k[3], country=v[0], asn=v[1], status=k[4],
                    path_class=k[5], count=v[2], bytes=v[3])
            for k, v in merged.items()
        ]

    def security_events(self, zone_id: str, start: float, end: float) -> list[NetEvent]:
        nodes = self._graphql_nodes(SECURITY_SELECTION, zone_id, start, end, "firewallEventsAdaptive")
        return [
            NetEvent(
                ts=_epoch(n["datetime"]), kind="cf_security", src=n.get("clientIP") or "",
                dst=n.get("clientRequestHTTPHost") or "", dport=_HTTPS_PORT,
                detail=f"{n.get('action') or ''}/{n.get('source') or ''}",
            )
            for n in nodes
        ]

    def list_access_rules(self, zone_id: str) -> list[dict]:
        path = f"/zones/{quote(zone_id, safe='')}/firewall/access_rules/rules"
        rules: list[dict] = []
        page = 1
        while True:
            payload = self._request("GET", path, query={"per_page": 100, "page": page})
            rules.extend(payload.get("result") or [])
            total = (payload.get("result_info") or {}).get("total_pages") or 1
            if page >= total:
                return rules
            page += 1

    # ---- writes (IP access rules only) ---------------------------------------------------------------------------------

    def create_access_rule(self, zone_id: str, ip: str, note: str) -> str:
        """Block one address on a zone. Returns the new rule id."""
        target = "ip6" if ipaddress.ip_address(ip).version == 6 else "ip"
        body = {"mode": "block", "configuration": {"target": target, "value": ip}, "notes": note}
        payload = self._request("POST", f"/zones/{quote(zone_id, safe='')}/firewall/access_rules/rules", body)
        return payload["result"]["id"]

    def delete_access_rule(self, zone_id: str, rule_id: str) -> None:
        self._request(
            "DELETE",
            f"/zones/{quote(zone_id, safe='')}/firewall/access_rules/rules/{quote(rule_id, safe='')}",
        )
