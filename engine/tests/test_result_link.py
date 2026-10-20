"""tests/test_result_link.py: a check's Result.link reaches the alert email as its dashboard link, never the SMS text.

The path is HermesNotifier._deliver (facts["link"]) -> notify.send -> notify_templates.site_url. The transport is a fake that
records the Message; the real transports refuse to run under pytest.
"""
from __future__ import annotations

import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import conftest  # noqa: F401
from homelab_maint import core, notify
from homelab_maint import notify_templates as T

TZ = ZoneInfo("America/Toronto")
NOON = datetime(2026, 10, 2, 12, 0, tzinfo=TZ).timestamp()
LINK = "/network?finding=0123456789abcdef"
FULL = "https://example.test/network?finding=0123456789abcdef"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    for name, sub in (("STATE_DIR", "state"), ("LOG_DIR", "log"), ("RUN_DIR", "run"), ("CONF_DIR", "conf")):
        p = tmp_path / sub
        p.mkdir()
        monkeypatch.setattr(core, name, p)
    monkeypatch.setattr(core, "sh", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    def boom(*a, **k):
        raise AssertionError(f"a notify test tried to start a process: {a!r}")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(notify, "playbook_loader", lambda task: [])
    monkeypatch.setattr(notify, "acks_loader", lambda: None)


class Fake:
    """Records every message; reports both legs delivered."""

    def __init__(self):
        self.calls: list[notify.Message] = []

    def __call__(self, msg, nc):
        self.calls.append(msg)
        legs = {c: "sent" for c in msg.channels}
        return notify.TransportResult(True, legs, {}, "", 0, "fake")


def cfg() -> dict:
    return {"notify": {"site": {"host_label": "testhost", "url": "https://example.test"},
                       "quiet_hours": {"tz": "America/Toronto"}, "ack": {"button": False, "mint_url": ""}}}


def deliver_alert(res: core.Result) -> Fake:
    """A crit alert for a task whose Result is `res`, delivered through the fake transport."""
    c = cfg()
    fk = Fake()
    n = notify.HermesNotifier(c, defer=True)
    assert n._deliver("net_watch", "Network watch", res.summary, NOON, recovery=False, level=2, res=res)
    notify.flush_pending(c, NOON, transport=fk)
    return fk


def test_result_link_is_in_the_alert_email_and_not_in_the_sms():
    fk = deliver_alert(core.Result("crit", "x", link=LINK))
    assert fk.calls, "the alert was not delivered"
    msg = fk.calls[0]
    assert FULL in msg.html
    assert "finding=" not in msg.sms and "network?" not in msg.sms
    assert FULL not in msg.sms


def test_result_without_link_has_no_dashboard_path_for_the_page():
    fk = deliver_alert(core.Result("crit", "x"))
    assert fk.calls
    assert "/network?finding=" not in fk.calls[0].html


@pytest.mark.parametrize("frag", [LINK, "/network?f=a", "/health/x?k=v"])
def test_site_url_accepts_the_path_form(frag):
    assert T.site_url("https://example.test", frag) == "https://example.test" + frag


@pytest.mark.parametrize("frag", ["/network?finding=../x", "/net work", "/a?b=1&c=2", "/a?b=1;c=2", "/..?x=1"])
def test_site_url_rejects_unsafe_path_forms(frag):
    # a refused fragment falls back to the bare base, exactly as before the path form existed
    assert T.site_url("https://example.test", frag) == "https://example.test"


@pytest.mark.parametrize("frag,expected", [
    ("#/incidents", "https://example.test/#/incidents"),
    ("#/health", "https://example.test/#/health"),
    ("", "https://example.test"),
    ("incidents", "https://example.test/#/incidents"),
])
def test_site_url_keeps_the_existing_forms(frag, expected):
    assert T.site_url("https://example.test", frag) == expected


def test_site_url_base_with_trailing_slash_is_not_doubled():
    assert T.site_url("https://example.test/", LINK) == FULL
