"""Tests for the home.ohmz.cloud access-log export: parsing, filtering, UA parsing and target matching."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

from homelab_maint.netwatch import weblog

NOW = 1798761600 + 1234
LINE = (
    '8.8.8.8 - - [21/Oct/2026:12:49:03 -0400] "GET /work/tday/ HTTP/1.1" 200 1234 '
    '"https://example.test/" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
    '(KHTML, like Gecko) Version/27.2 Safari/605.1.15" "8.8.8.8"'
)
LINE2 = (
    '1.1.1.1 - - [21/Oct/2026:12:49:04 -0400] "GET / HTTP/1.1" 200 15379 '
    '"-" "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0 Safari/537.36" "1.1.1.1"'
)
PROBE = (
    '192.168.0.1 - - [21/Oct/2026:12:49:00 -0400] "GET / HTTP/1.1" 200 70264 '
    '"-" "homelab-maint-probe/1" "-"'
)


def test_parse_line():
    row = weblog.parse_line("2026-10-21T16:49:03.123456789Z " + LINE)
    assert row is not None
    assert row["ip"] == "8.8.8.8"
    assert row["path"] == "/work/tday/"
    assert row["status"] == 200
    assert row["referer"] == "https://example.test/"
    assert row["ts"] == int(weblog.datetime.strptime("21/Oct/2026:12:49:03 -0400", weblog._TIME).timestamp())


def test_parse_ua():
    assert weblog.parse_ua("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Version/18.0 Mobile/15E148 Safari/604.1") == ("iPhone", "Safari", "iOS")
    assert weblog.parse_ua("Mozilla/5.0 (X11; Linux x86_64) Chrome/154.0 Safari/537.36") == ("Desktop", "Chrome", "Linux")
    assert weblog.parse_ua("Googlebot/2.1") == ("Bot", "Bot", "Bot")
    assert weblog.parse_ua("") == ("", "", "")


def test_match_target():
    targets = [{"name": "Salesforce", "match": ["salesforce"]}, {"name": "Ford", "match": ["ford motor"]}]
    assert weblog.match_target("Salesforce.com, Inc.", targets) == "Salesforce"
    assert weblog.match_target("Ford Motor Company", targets) == "Ford"
    assert weblog.match_target("Cloudflare, Inc.", targets) is None
    assert weblog.match_target("", targets) is None


class FakeGeo:
    conn = object()

    def lookup(self, ip):
        if ip == "8.8.8.8":
            return {"country": "US", "region": "California", "city": "Mountain View", "asn": 15169,
                    "org": "Salesforce.com, Inc."}
        return {"country": "US", "region": "California", "city": "San Francisco", "asn": 13335,
                "org": "Cloudflare, Inc."}

    def close(self):
        pass


def test_build_web_filters_probes_and_matches_target():
    doc = weblog.build_web(
        now=NOW,
        logs=[PROBE, LINE, LINE2],
        geo=FakeGeo(),
        targets=[{"name": "Salesforce", "match": ["salesforce"]}],
    )
    assert doc["available"] is True
    assert len(doc["rows"]) == 2
    assert doc["rows"][0]["ip"] == "1.1.1.1"
    assert doc["rows"][0]["browser"] == "Chrome"
    assert doc["rows"][0]["city"] == "San Francisco"
    assert doc["rows"][0]["target"] is None
    assert doc["rows"][1]["target"] == "Salesforce"
    assert doc["rows"][1]["country"] == "US"
    assert doc["targets"] == ["Salesforce"]
    assert doc["sources"]["geo"]["status"] == "ok"


def test_build_web_empty_is_unavailable():
    doc = weblog.build_web(now=NOW, logs=[PROBE], geo=FakeGeo(), targets=[])
    assert doc["available"] is False
    assert doc["rows"] == []
    assert doc["sources"]["nginx"]["status"] == "empty"
