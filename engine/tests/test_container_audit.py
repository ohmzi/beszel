"""The standalone container-audit script's log classifier.

A false "fail" pages the owner, so each host-specific benign line the audit has
been taught must stay benign while a genuine fault with the same wording must
still be reported.
"""
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "container-audit.py"
_spec = importlib.util.spec_from_file_location("container_audit", _SCRIPT)
ca = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ca)


def test_sure_test_database_noise_is_benign():
    line = '2026-10-21 16:14:32.477 UTC [806444] FATAL:  cannot connect to invalid database "sure_test_21"'
    assert ca.STRONG.search(line), "the line is a strong signature; it must be suppressed on purpose"
    assert ca.BENIGN.search(line), "the Sure test suite's throwaway databases are known-benign noise"


def test_invalid_database_with_a_real_name_is_still_reported():
    line = '2026-10-21 16:14:32.477 UTC [806444] FATAL:  cannot connect to invalid database "production"'
    assert ca.STRONG.search(line)
    assert not ca.BENIGN.search(line), "a non-test database name must remain a finding"


@pytest.mark.parametrize("line", [
    "10-22 02:04:14 Error Jackett.Common.IndexerException: Exception (uindex): Connection refused",
    "---> System.Net.Http.HttpRequestException: Connection refused (uindex.org:443)",
    "---> System.Net.Sockets.SocketException (111): Connection refused",
    "FileNotFoundError: [Errno 2] No such file or directory: '/data/western18to14tb/Plex/Complete/English/Shows/_UNPACK_Show S02E07' -> '/data/.../_FAILED_Show S02E07'",
    '2026/10/21 22:13:58 [error] 21#21: *1960 open() "/usr/share/nginx/html/apple-touch-icon-precomposed.png" failed (2: No such file or directory), client: 1.2.3.4, server: _, request: "GET /apple-touch-icon-precomposed.png HTTP/1.1", host: "home.ohmz.cloud"',
])
def test_known_application_noise_is_benign(line):
    """A healthy container doing its job may log an external indexer refusing, a failed unpack race, or a missing favicon.
    Those are application events, not container faults."""
    assert ca.STRONG.search(line)
    assert ca.BENIGN.search(line)


def test_scan_logs_skips_only_the_benign_line(monkeypatch):
    out = (
        '2026-10-21 16:14:32.477 UTC [806444] FATAL:  cannot connect to invalid database "sure_test_21"\n'
        '2026-10-21 16:14:32.478 UTC [806445] FATAL:  cannot connect to invalid database "billing"\n'
    )
    monkeypatch.setattr(ca, "run", lambda *a, **k: (0, out))
    strong, weak = ca.scan_logs("devcontainer-db-1", "2026-10-21T15:55:04Z")
    assert strong == ['2026-10-21 16:14:32.478 UTC [806445] FATAL:  cannot connect to invalid database "billing"']
    assert weak == 0
