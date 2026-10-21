"""DB-IP Lite city and ASN lookup for the website access log.

The dashboard needs a city and a network organization for each visitor IP. Cloudflare gives the country and (on some plans)
the ASN, but not a city or an organization name. This module owns a small SQLite database built from the free DB-IP Lite
CSV exports:

  dbip-city-lite-YYYY-MM.csv.gz   ip_start, ip_end, continent, country, region, city, lat, lon
  dbip-asn-lite-YYYY-MM.csv.gz    ip_start, ip_end, asn, organization

The database is *lookup-only* at publish time. `update()` downloads and imports it; it is not called from `publish` because
the city file is ~86 MB compressed and the import takes minutes, not milliseconds. Run it monthly:

    python3 -m homelab_maint.netwatch.webgeo update

IP geolocation by DB-IP (https://db-ip.com), CC BY 4.0. The city is approximate and is often the ISP or VPN exit, not the
visitor's desk.
"""
from __future__ import annotations

import csv
import gzip
import ipaddress
import os
import sqlite3
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import core

DB_NAME = "webgeo.db"
CITY_URL = "https://download.db-ip.com/free/dbip-city-lite-{month}.csv.gz"
ASN_URL = "https://download.db-ip.com/free/dbip-asn-lite-{month}.csv.gz"
DOWNLOAD_TIMEOUT = 300
BATCH = 20_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS city (
    family    INTEGER NOT NULL,
    start_hex TEXT    NOT NULL,
    end_hex   TEXT    NOT NULL,
    country   TEXT    NOT NULL,
    region    TEXT    NOT NULL,
    city      TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS asn (
    family    INTEGER NOT NULL,
    start_hex TEXT    NOT NULL,
    end_hex   TEXT    NOT NULL,
    asn       INTEGER NOT NULL,
    org       TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def db_path() -> Path:
    return core.STATE_DIR / DB_NAME


def _month(offset: int = 0) -> str:
    first = datetime.now(timezone.utc).replace(day=1)
    if offset:
        first -= timedelta(days=1)
    return first.strftime("%Y-%m")


def ip_key(value: str) -> tuple[int, str] | None:
    """Return (family, fixed-width hex) for an IPv4/IPv6 address, or None when it does not parse."""
    try:
        addr = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    width = 8 if addr.version == 4 else 32
    return addr.version, format(int(addr), f"0{width}x")


def _open(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "homelab-maint-webgeo/1"})
    response = urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT)
    return gzip.GzipFile(fileobj=response)


def _download(month: str, url_template: str) -> tuple[str, object]:
    last: Exception | None = None
    for offset in (0, 1):
        month_ = _month(offset) if month == "auto" else month
        url = url_template.format(month=month_)
        try:
            return url, _open(url)
        except Exception as exc:  # noqa: BLE001 - fall through to the previous month
            last = exc
    raise RuntimeError(f"could not download {url_template}: {last}")


def _import_city(reader, db: sqlite3.Connection) -> int:
    batch: list[tuple] = []
    count = 0
    for row in reader:
        if len(row) < 6:
            continue
        key = ip_key(row[0])
        end = ip_key(row[1])
        if not key or not end or key[0] != end[0]:
            continue
        country, region, city = row[3].strip(), row[4].strip(), row[5].strip()
        batch.append((key[0], key[1], end[1], country, region, city))
        count += 1
        if len(batch) >= BATCH:
            db.executemany("INSERT INTO city VALUES (?,?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        db.executemany("INSERT INTO city VALUES (?,?,?,?,?,?)", batch)
    return count


def _import_asn(reader, db: sqlite3.Connection) -> int:
    batch: list[tuple] = []
    count = 0
    for row in reader:
        if len(row) < 4:
            continue
        key = ip_key(row[0])
        end = ip_key(row[1])
        if not key or not end or key[0] != end[0]:
            continue
        try:
            asn = int(row[2])
        except ValueError:
            continue
        batch.append((key[0], key[1], end[1], asn, row[3].strip()))
        count += 1
        if len(batch) >= BATCH:
            db.executemany("INSERT INTO asn VALUES (?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        db.executemany("INSERT INTO asn VALUES (?,?,?,?,?)", batch)
    return count


def update(month: str = "auto", city_url: str | None = None, asn_url: str | None = None,
           path: Path | None = None) -> dict:
    """Download the two DB-IP Lite files and replace the lookup database atomically.

    `city_url`/`asn_url` may be file:// URLs (or any URL the stdlib opens) in tests. Pass a month
    such as "2026-10" to pin the download; "auto" tries the current month, then the previous one.
    """
    db_path_ = Path(path) if path else db_path()
    db_path_.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkstemp(prefix=".webgeo-", suffix=".db", dir=db_path_.parent)[1])
    city_source, asn_source = city_url or CITY_URL, asn_url or ASN_URL
    try:
        db = sqlite3.connect(tmp)
        db.executescript(SCHEMA)
        db.execute("PRAGMA journal_mode=OFF")
        db.execute("PRAGMA synchronous=OFF")
        city_url_used, city_fh = _download(month, city_source)
        with city_fh:
            city_count = _import_city(csv.reader((line.decode("utf-8", "replace") for line in city_fh)), db)
        asn_url_used, asn_fh = _download(month, asn_source)
        with asn_fh:
            asn_count = _import_asn(csv.reader((line.decode("utf-8", "replace") for line in asn_fh)), db)
        db.execute("CREATE INDEX city_range ON city (family, start_hex)")
        db.execute("CREATE INDEX asn_range ON asn (family, start_hex)")
        db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
            ("updated_at", str(int(time.time()))),
            ("city_source", city_url_used),
            ("asn_source", asn_url_used),
            ("city_rows", str(city_count)),
            ("asn_rows", str(asn_count)),
        ])
        db.commit()
        db.execute("PRAGMA optimize")
        db.close()
        os.chmod(tmp, 0o644)
        os.replace(tmp, db_path_)
        return {"path": str(db_path_), "city_rows": city_count, "asn_rows": asn_count,
                "city_source": city_url_used, "asn_source": asn_url_used}
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


class GeoLookup:
    """Read-only lookups against the DB-IP database. One connection, one cache for one export run."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else db_path()
        self.conn = None
        self.cache: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
                self.conn.row_factory = sqlite3.Row
            except sqlite3.DatabaseError:
                self.conn = None

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _range(self, table: str, ip: str, columns: str) -> dict | None:
        key = ip_key(ip)
        if not key or self.conn is None:
            return None
        family, hex_ = key
        row = self.conn.execute(
            f"SELECT {columns}, start_hex, end_hex FROM {table} "
            "WHERE family = ? AND start_hex <= ? ORDER BY start_hex DESC LIMIT 1",
            (family, hex_),
        ).fetchone()
        if row is None or row["end_hex"] < hex_:
            return None
        return dict(row)

    def lookup(self, ip: str) -> dict:
        if ip in self.cache:
            return self.cache[ip]
        city = self._range("city", ip, "country, region, city") or {}
        asn = self._range("asn", ip, "asn, org") or {}
        out = {
            "country": city.get("country", ""),
            "region": city.get("region", ""),
            "city": city.get("city", ""),
            "asn": asn.get("asn", 0),
            "org": asn.get("org", ""),
        }
        self.cache[ip] = out
        return out


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] != ["update"]:
        print("usage: python3 -m homelab_maint.netwatch.webgeo update [YYYY-MM]", file=sys.stderr)
        return 2
    month = args[1] if len(args) > 1 else "auto"
    try:
        result = update(month=month)
    except Exception as exc:  # noqa: BLE001
        print(f"webgeo update failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {result['path']}: {result['city_rows']} city rows, {result['asn_rows']} ASN rows")
    print(f"  city: {result['city_source']}")
    print(f"  asn:  {result['asn_source']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
