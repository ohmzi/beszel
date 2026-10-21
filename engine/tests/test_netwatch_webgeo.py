"""Tests for the DB-IP Lite importer and range lookup. The fixtures are tiny hand-written CSV files, not a download."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import csv
import gzip

from homelab_maint.netwatch import webgeo


def test_ip_key():
    assert webgeo.ip_key("8.8.8.8") == (4, "08080808")
    assert webgeo.ip_key("2001:db8::1") == (6, "20010db8" + "0" * 23 + "1")
    assert webgeo.ip_key("not-an-ip") is None


def _gdump(path, rows):
    with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerows(rows)


def test_update_imports_and_lookup(tmp_path):
    city = tmp_path / "city.csv.gz"
    asn = tmp_path / "asn.csv.gz"
    _gdump(city, [
        ["8.8.8.0", "8.8.8.255", "NA", "US", "California", "Mountain View", "0", "0"],
        ["2001:db8::", "2001:db8::ffff", "NA", "US", "Test", "Test City", "0", "0"],
    ])
    _gdump(asn, [
        ["8.8.8.0", "8.8.8.255", "15169", "Google LLC"],
        ["2001:db8::", "2001:db8::ffff", "64512", "Example Network"],
    ])
    db = tmp_path / "webgeo.db"
    out = webgeo.update(city_url=city.as_uri(), asn_url=asn.as_uri(), path=db)
    assert out["city_rows"] == 2
    assert out["asn_rows"] == 2

    geo = webgeo.GeoLookup(db)
    try:
        row = geo.lookup("8.8.8.8")
        assert row == {"country": "US", "region": "California", "city": "Mountain View",
                       "asn": 15169, "org": "Google LLC"}
        v6 = geo.lookup("2001:db8::1")
        assert v6["city"] == "Test City"
        assert v6["org"] == "Example Network"
        assert geo.lookup("1.1.1.1")["org"] == ""
        assert geo.lookup("not-an-ip")["org"] == ""
    finally:
        geo.close()
