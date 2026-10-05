"""tests/test_notify.py: the ONE notification path (homelab_maint/notify.py + notify_templates.py + etc/notify.toml).

NOTHING here can reach the real world, three times over:
  * every test runs in tmp dirs (core.STATE_DIR/LOG_DIR/CONF_DIR/RUN_DIR are re-pointed per test),
  * core.sh (the `logger` call inside core.audit) is replaced, and subprocess.run/Popen raise for the whole file,
  * the transport is a fake injected through send(..., transport=..., fallback=...); notify's own guard also refuses
    to run a real transport under pytest (proved below).
Matrices: routing (kind x severity), escalation, dedupe/coverage, quiet hours, budgets, mute; SMS rules; HTML safety;
failure fallbacks; the transport layer with a fake `sh`; delivery-log redaction and the website export.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import importlib.util
import io
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import tomllib
import types
import urllib.parse
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import website_helper as wh
from homelab_maint import core, notify
from homelab_maint import notify_templates as T

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("America/Toronto")
_REAL_RUN, _REAL_POPEN = subprocess.run, subprocess.Popen     # for the one test that starts our own child with a stub transport
_REAL_ACKS_LOADER = notify.acks_loader                          # `env` replaces it per test (no acknowledgements unless a test asks)


def at(h: int, m: int = 0, day: int = 2) -> float:
    return datetime(2026, 10, day, h, m, tzinfo=TZ).timestamp()


NOON = at(12)
H, D = 3600, 86400


# --------------------------------------------------------------------------- fixtures and fakes
@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Per-test dirs, no logger, no subprocess anywhere."""
    for name, sub in (("STATE_DIR", "state"), ("LOG_DIR", "log"), ("RUN_DIR", "run"), ("CONF_DIR", "conf")):
        p = tmp_path / sub
        p.mkdir()
        monkeypatch.setattr(core, name, p)
    monkeypatch.setattr(core, "sh", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)         # the user-unit fallback directory must never be the real one

    def boom(*a, **k):
        raise AssertionError(f"a notify test tried to start a process: {a!r}")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(notify, "playbook_loader", lambda task: [])
    monkeypatch.setattr(notify, "acks_loader", lambda: None)     # no acknowledgements unless a test asks (the `acks` fixture), whatever acks.py does
    return tmp_path


class Fake:
    """An injectable transport. legs=None means every requested channel is delivered."""

    def __init__(self, legs=None, ok=None, fatal="", rc=0, exc=None, errors=None, via="fake", delay=0.0):
        self.legs, self.ok, self.fatal, self.rc, self.exc, self.errors, self.via, self.delay = legs, ok, fatal, rc, exc, errors or {}, via, delay
        self.calls: list[notify.Message] = []

    def __call__(self, msg, nc):
        self.calls.append(msg)
        if self.delay:
            time.sleep(self.delay)
        if self.exc:
            raise self.exc
        legs = {c: "sent" for c in msg.channels} if self.legs is None else dict(self.legs)
        ok = any(v == "sent" for v in legs.values()) if self.ok is None else self.ok
        return notify.TransportResult(ok, legs, dict(self.errors), self.fatal, self.rc, self.via)


def cfgx(**over) -> dict:
    """Inline notify config on top of the defaults: deterministic host label and quiet-hours zone."""
    # ack.mint_url = "": the unit tests mint locally (FakeAcks) rather than call the hub's /mint over HTTP; the shipped
    # default (http://127.0.0.1:8088/api/beszel/maintenance/ack/mint) is exercised by the _mint_via_hub tests, which set it.
    base = {"site": {"host_label": "testhost"}, "quiet_hours": {"tz": "America/Toronto"},
            "ack": {"button": True, "mint_url": ""}}                                          # (the button is "auto": it waits for the site)
    notify._merge(base, over)
    return {"notify": base}


def mk(kind="alert", sev="crit", title="Disk space", summary="/ is 4% free", **kw) -> notify.Event:
    kw.setdefault("task", "disk_forecast")
    kw.setdefault("dedupe_key", kw["task"])
    return notify.Event(kind, sev, title, summary, **kw)


def go(ev, cfg=None, now=NOON, transport=None, fallback=None, **kw) -> tuple[notify.Delivery, Fake]:
    fk = transport or Fake()
    return notify.send(ev, cfgx() if cfg is None else cfg, now, transport=fk, fallback=fallback, **kw), fk


def state() -> dict:
    return json.loads((core.STATE_DIR / "notify-state.json").read_text())


def log_rows() -> list[dict]:
    p = core.STATE_DIR / "notifications.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def audit_rows() -> list[dict]:
    p = core.LOG_DIR / "audit.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# =========================================================================== configuration
def test_defaults_without_a_file_and_handle_from_maint_global():
    nc = notify.load_config({"global": {"notify_handle": "someone", "bridge": "/opt/b.py"}})
    assert nc["transport"]["handle"] == "someone" and nc["transport"]["user"] == "someone"
    assert nc["transport"]["bridge"] == "/opt/b.py"
    assert notify.load_config()["transport"]["handle"] == "ohmz"
    assert set(nc["routes"]) >= set(T.KINDS)


def test_shipped_notify_toml_parses_and_mirrors_the_defaults():
    user = tomllib.loads((ROOT / "etc" / "notify.toml").read_text())
    eff = notify._merge(copy.deepcopy(notify.DEFAULTS), user)

    def diff(a, b, path=""):
        out = []
        for k in set(a) | set(b):
            if k in ("handle", "user", "bridge") and path == "transport.":
                continue                                    # filled from maint.toml [global] when the defaults leave them empty
            if k not in a or k not in b:
                out.append(f"{path}{k} only on one side")
            elif isinstance(a[k], dict) and isinstance(b[k], dict):
                out += diff(a[k], b[k], f"{path}{k}.")
            elif a[k] != b[k]:
                out.append(f"{path}{k}: {a[k]!r} != {b[k]!r}")
        return out
    assert diff(eff, notify.DEFAULTS) == []
    assert not {"handle", "user", "bridge"} & set(user["transport"])             # those come from maint.toml [global]: one source of truth
    nc = notify.load_config({"global": {"notify_handle": "ohmz", "bridge": "/usr/local/sbin/backup-notify-hermes.py"}})
    assert (nc["transport"]["handle"], nc["transport"]["user"], nc["transport"]["bridge"]) == ("ohmz", "ohmz", "/usr/local/sbin/backup-notify-hermes.py")
    assert user["site"]["url"] == "https://maintainer.ohmzhomelab.ca"
    assert user["mute_file"] == "NOTIFY_MUTE"                 # a top-level key, not swallowed by a table
    for kind in T.KINDS:                                      # every kind has a route and a dedupe window and a budget
        assert kind in user["routes"] or kind in ("alert", "incident_open")
        assert kind in user["dedupe"]["window_s"] and kind in user["budget"]["per_day"]


def test_installed_file_is_used_and_a_broken_one_falls_back_to_defaults():
    (core.CONF_DIR / "notify.toml").write_text('[site]\nhost_label = "from-file"\n[routes]\nmaintenance = "none"\n')
    nc = notify.load_config()
    assert nc["site"]["host_label"] == "from-file" and nc["routes"]["maintenance"] == "none"
    assert nc["routes"]["recovery"] == "both"                  # untouched keys keep the default
    (core.CONF_DIR / "notify.toml").write_text("[routes\nthis is not toml")
    d, fk = go(mk(), cfg={})                                   # cfg={} -> read the (broken) file
    assert d.ok and "notify.toml unreadable" in d.note         # the alert still went out, and the log says why
    assert set(fk.calls[0].channels) == {"sms", "email"}


def test_inline_config_and_unknown_route_values_fail_safe_to_email():
    assert notify.load_config({"notify": {"routes": {"alert": {"crit": "email"}}}})["routes"]["alert"]["crit"] == "email"
    d, fk = go(mk(), cfgx(routes={"alert": {"crit": "pigeon"}}))
    assert fk.calls[0].channels == ["email"] and any("not understood" in w for w in d.why)


@pytest.mark.parametrize("bad", [{"budget": {"per_day": 5}}, {"budget": 7}, {"dedupe": {"window_s": "x", "covered_by": []}}, {"routes": "both"},
                                 {"escalation": None}, {"quiet_hours": ["x"]}, {"transport": "hermes"}, {"site": 1}, {"log": "x"}, {"todo": 3}])
def test_a_config_type_error_never_blocks_delivery(bad):
    """`[budget] per_day = 5` is a typo, not a reason to stop paging: the table is replaced by its default and named in the note."""
    d, fk = go(mk(), {"notify": {**bad}})
    assert d.ok and len(fk.calls) == 1 and "must be a table" in d.note and "defaults used" in d.note


def test_quiet_hours_suppress_may_be_a_string():
    nc = notify.load_config({"notify": {"quiet_hours": {"suppress": "sms+email"}}})
    assert nc["quiet_hours"]["suppress"] == ["sms", "email"]
    d, fk = go(mk("digest_daily", "ok"), cfgx(quiet_hours={"suppress": "email"}), now=at(2))
    assert d.skipped == "quiet-hours"


@pytest.mark.parametrize("value,expect", [("both", {"sms", "email"}), ("none", set()), ("sms", {"sms"}), ("email", {"email"}),
                                          (["sms", "email"], {"sms", "email"}), ("sms+email", {"sms", "email"}), (" EMAIL ", {"email"}),
                                          ("pigeon", None), (None, None), ("sms,pigeon", None)])
def test_route_value_parsing(value, expect):
    assert notify._chan(value) == expect


# =========================================================================== routing matrix
@pytest.mark.parametrize("kind,sev,facts,expect", [
    ("alert", "crit", None, {"sms", "email"}),
    ("alert", "warn", None, {"email"}),
    ("alert", "info", None, {"email"}),
    ("recovery", "ok", None, {"sms", "email"}),
    ("maintenance", "ok", None, {"email"}),
    ("maintenance", "ok", {"significant": True}, {"sms", "email"}),
    ("maintenance", "warn", None, {"email"}),
    ("digest_daily", "ok", None, {"email"}),
    ("report_weekly", "info", None, {"email"}),
    ("incident_open", "crit", None, {"sms", "email"}),
    ("incident_open", "warn", None, {"email"}),
    ("incident_resolved", "ok", None, {"sms", "email"}),
])
def test_default_routing_matrix(kind, sev, facts, expect):
    d, fk = go(mk(kind, sev, facts=facts))
    assert d.ok and set(fk.calls[0].channels) == expect and set(d.channels) == expect
    assert bool(fk.calls[0].html) == ("email" in expect)       # the HTML is only built when an email is going


@pytest.mark.parametrize("sev", ["ok", "info", "warn", "crit"])
def test_sms_never_goes_for_email_only_kinds_at_any_severity(sev):
    for kind in ("digest_daily", "report_weekly"):
        d, fk = go(mk(kind, sev, dedupe_key=f"{kind}-{sev}"))
        assert fk.calls[0].channels == ["email"]


def test_route_none_sends_nothing_and_is_handled():
    d, fk = go(mk("maintenance", "ok"), cfgx(routes={"maintenance": "none"}))
    assert (d.ok, d.handled, d.skipped) == (False, True, "route-none") and fk.calls == []
    assert log_rows()[-1]["skipped"] == "route-none" and log_rows()[-1]["ok"] is True      # held by policy is not a failure


def test_task_route_overrides_and_is_final():
    cfg = cfgx(task_routes={"docker_df": "email", "noisy": "none"})
    d, fk = go(mk("alert", "crit", task="docker_df"), cfg)
    assert fk.calls[0].channels == ["email"]                   # a crit that the owner explicitly demoted
    d2, fk2 = go(mk("alert", "crit", task="noisy"), cfg)
    assert d2.skipped == "route-none" and fk2.calls == []
    d3, fk3 = go(mk("alert", "crit", task="other"), cfg)
    assert set(fk3.calls[0].channels) == {"sms", "email"}


def test_sms_only_route_builds_no_html():
    d, fk = go(mk("alert", "warn"), cfgx(routes={"alert": {"warn": "sms"}}))
    assert fk.calls[0].channels == ["sms"] and fk.calls[0].html == "" and fk.calls[0].sms


def test_unknown_kind_is_treated_as_an_alert_and_garbage_never_raises():
    d, fk = go(notify.Event("nonsense", "crit", "x", "y", task="t1", dedupe_key="t1"))
    assert d.ok and d.kind == "alert"
    junk = [notify.Event("alert", None, None, None, 5, "facts", 7, 9, 3.5), notify.Event("", "??", "t" * 5000, "s" * 5000),
            notify.Event("alert", "crit", "x", "y", details=object(), facts=[("a", 1), ("b",)]),
            notify.Event("recovery", "ok", "x", details={"todo": 5, "done": None, "timeline": 3, "sections": "x"}, facts={"tiles": [1, "a"], "link": 5})]
    for ev in junk:
        out = notify.send(ev, cfgx(), NOON, transport=Fake())
        assert isinstance(out, notify.Delivery)
    assert isinstance(notify.send(None, cfgx(), NOON, transport=Fake()), notify.Delivery)      # even a missing event
    assert isinstance(notify.send(mk(), "not a dict", NOON, transport=Fake()), notify.Delivery)


def test_transport_kind_none_logs_and_sends_nothing():
    d = notify.send(mk(), cfgx(transport={"kind": "none"}), NOON)
    assert (d.ok, d.handled, d.skipped) == (False, True, "disabled") and log_rows()[-1]["skipped"] == "disabled"


def test_severity_derivation():
    assert T.norm_severity("critical") == "crit" and T.norm_severity(None, "error") == "crit"
    assert T.norm_severity("warning") == "warn" and T.norm_severity("SKIPPED") == "info"
    assert T.norm_severity(None, None, "recovery") == "ok" and T.norm_severity(None, None, "alert") == "info"
    assert T.norm_severity("bogus", "warn") == "info"          # an explicit but unknown severity is not guessed from status


# =========================================================================== escalation (warn -> sms once it persists)
def test_warn_emails_first_then_texts_when_it_survives_a_reminder():
    ev = mk("alert", "warn")
    d1, f1 = go(ev, now=NOON)
    assert f1.calls[0].channels == ["email"]
    d2, f2 = go(ev, now=NOON + 25 * H)                         # the Notifier's 24 h reminder
    assert set(f2.calls[0].channels) == {"sms", "email"} and any("escalated" in w for w in d2.why)
    d3, f3 = go(ev, now=NOON + 50 * H)
    assert set(f3.calls[0].channels) == {"sms", "email"}
    assert "Notified: 3 times" in f3.calls[0].plain and "Notified: 2 times" in f2.calls[0].plain
    assert "Notified" not in f1.calls[0].plain


def test_warn_sms_after_zero_never_escalates_and_one_escalates_at_once():
    ev = mk("alert", "warn")
    go(ev, cfgx(escalation={"warn_sms_after": 0}), now=NOON)
    d, fk = go(ev, cfgx(escalation={"warn_sms_after": 0}), now=NOON + 25 * H)
    assert fk.calls[0].channels == ["email"]
    d, fk = go(mk("alert", "warn", task="other"), cfgx(escalation={"warn_sms_after": 1}))
    assert set(fk.calls[0].channels) == {"sms", "email"}


def test_facts_escalate_forces_sms_and_task_route_is_never_escalated():
    d, fk = go(mk("alert", "warn", facts={"escalate": True}))
    assert set(fk.calls[0].channels) == {"sms", "email"}
    cfg = cfgx(task_routes={"disk_forecast": "email"})
    go(mk("alert", "warn"), cfg, now=NOON)
    d, fk = go(mk("alert", "warn"), cfg, now=NOON + 25 * H)
    assert fk.calls[0].channels == ["email"]


def test_escalation_state_is_per_key_and_resets_after_recovery():
    go(mk("alert", "warn", task="a"), now=NOON)
    d, fk = go(mk("alert", "warn", task="b"), now=NOON + H)
    assert fk.calls[0].channels == ["email"]                   # b is a different problem: first notification
    go(mk("recovery", "ok", task="a"), now=NOON + 2 * H)
    d, fk = go(mk("alert", "warn", task="a"), now=NOON + 3 * H)
    assert fk.calls[0].channels == ["email"]                   # a new episode starts from zero
    assert "a" in state()["esc"] and state()["esc"]["a"]["n"] == 1


def test_stale_episode_expires_after_ttl():
    go(mk("alert", "warn"), now=NOON)
    d, fk = go(mk("alert", "warn"), now=NOON + 80 * H)         # nothing for 80 h > episode_ttl_h (72)
    assert fk.calls[0].channels == ["email"]


def test_crit_is_not_touched_by_escalation_state():
    for i in range(3):
        d, fk = go(mk("alert", "crit"), now=NOON + i * 7 * H)
        assert set(fk.calls[0].channels) == {"sms", "email"}


# =========================================================================== recovery text only if the problem was texted
def test_recovery_after_crit_texts_but_after_an_email_only_warn_does_not():
    go(mk("alert", "crit", task="c"))
    d, fk = go(mk("recovery", "ok", task="c"), now=NOON + H)
    assert set(fk.calls[0].channels) == {"sms", "email"}
    go(mk("alert", "warn", task="w"))
    d, fk = go(mk("recovery", "ok", task="w"), now=NOON + H)
    assert fk.calls[0].channels == ["email"] and any("never texted" in w for w in d.why)


def test_recovery_after_an_escalated_warn_texts():
    go(mk("alert", "warn", task="w"), now=NOON)
    go(mk("alert", "warn", task="w"), now=NOON + 25 * H)       # escalated: this one was texted
    d, fk = go(mk("recovery", "ok", task="w"), now=NOON + 26 * H)
    assert set(fk.calls[0].channels) == {"sms", "email"}


def test_recovery_without_state_uses_the_was_hint_or_keeps_the_route():
    d, fk = go(mk("recovery", "ok", task="u0"))
    assert set(fk.calls[0].channels) == {"sms", "email"}        # unknown history: the default route stands
    d, fk = go(mk("recovery", "ok", task="u1", facts={"was": "warn"}))
    assert fk.calls[0].channels == ["email"]                    # the caller says it was only a warning: it was never texted
    d, fk = go(mk("recovery", "ok", task="u2", facts={"was": "crit"}))
    assert set(fk.calls[0].channels) == {"sms", "email"}
    d, fk = go(mk("recovery", "ok", task="u3"), cfgx(escalation={"recovery_sms_only_if_texted": False}))
    assert set(fk.calls[0].channels) == {"sms", "email"}
    go(mk("alert", "warn", task="u4"))
    d, fk = go(mk("recovery", "ok", task="u4"), cfgx(escalation={"recovery_sms_only_if_texted": False}))
    assert set(fk.calls[0].channels) == {"sms", "email"}        # the switch turns the rule off


# =========================================================================== dedupe window
def test_same_kind_severity_key_inside_the_window_is_not_sent_twice():
    ev = mk("alert", "crit")
    d1, f1 = go(ev, now=NOON)
    d2, f2 = go(ev, now=NOON + 5 * 60)
    assert d1.ok and (d2.ok, d2.handled, d2.skipped) == (False, True, "dedupe") and f2.calls == []
    assert "5 min ago" in d2.note and log_rows()[-1]["skipped"] == "dedupe"
    d3, f3 = go(ev, now=NOON + 6 * H + 1)                      # window (6 h) over: the reminder goes
    assert d3.ok and len(f3.calls) == 1


def test_severity_change_key_change_and_kind_change_are_not_duplicates():
    go(mk("alert", "warn"), now=NOON)
    assert go(mk("alert", "crit"), now=NOON + 60)[0].ok          # warn -> crit must page at once
    assert go(mk("alert", "crit", task="other"), now=NOON + 60)[0].ok
    assert go(mk("maintenance", "ok"), now=NOON + 60)[0].ok


def test_a_failed_delivery_does_not_consume_the_dedupe_slot_or_budget():
    ev = mk("alert", "crit")
    bad = Fake(legs={"sms": "failed", "email": "failed"}, errors={"sms": "gateway down", "email": "smtp 421"})
    d, fk = go(ev, transport=bad)
    assert (d.ok, d.handled) == (False, False)
    st = state()
    assert st["sent"] == [] and "alert|disk_forecast" not in st["dedupe"] and st["esc"] == {}
    d2, fk2 = go(ev, now=NOON + 60)                              # the next run retries and succeeds immediately
    assert d2.ok and len(fk2.calls) == 1 and len(state()["sent"]) == 1


def test_recovery_clears_the_alert_dedupe_so_a_relapse_pages_at_once():
    go(mk("alert", "crit"), now=NOON)
    go(mk("recovery", "ok"), now=NOON + 600)
    d, fk = go(mk("alert", "crit"), now=NOON + 1200)             # flaps back 20 min later, well inside the 6 h window
    assert d.ok and len(fk.calls) == 1
    d, fk = go(mk("recovery", "ok"), now=NOON + 1800)            # and ITS recovery is not mistaken for a repeat
    assert d.ok and len(fk.calls) == 1


def test_zero_window_never_dedupes_and_key_is_derived_from_the_title_when_missing():
    cfg = cfgx(dedupe={"window_s": {"alert": 0}})
    assert go(mk("alert", "crit"), cfg)[0].ok and go(mk("alert", "crit"), cfg)[0].ok
    e = notify.Event("alert", "crit", "Disk Space: /home!", "x")
    assert go(e, cfgx())[0].dedupe_key == "disk-space-home"
    assert go(notify.Event("alert", "crit", "", "x"), cfgx())[0].dedupe_key == "unnamed"


# =========================================================================== one page, not two: alert vs incident coverage
def test_incident_open_is_covered_by_a_recent_alert_of_the_same_problem():
    go(mk("alert", "crit"), now=NOON)
    d, fk = go(mk("incident_open", "crit"), now=NOON + 60)
    assert (d.skipped, d.handled) == ("covered", True) and fk.calls == [] and "already sent as alert" in d.note
    d, fk = go(mk("incident_open", "warn"), now=NOON + 60)       # a lesser severity is covered too
    assert d.skipped == "covered"


def test_a_more_severe_event_is_never_covered_by_a_milder_one():
    go(mk("incident_open", "warn"), now=NOON)
    d, fk = go(mk("alert", "crit"), now=NOON + 60)               # the page for the crit must not be swallowed
    assert d.ok and set(fk.calls[0].channels) == {"sms", "email"}


def test_coverage_is_symmetric_expires_and_is_per_key():
    go(mk("incident_open", "crit"), now=NOON)
    assert go(mk("alert", "crit"), now=NOON + 60)[0].skipped == "covered"          # the incident spoke first
    assert go(mk("alert", "crit"), now=NOON + 31 * 60)[0].ok                       # outside cover_window_s: speaks
    assert go(mk("alert", "crit", task="elsewhere"), now=NOON + 60)[0].ok          # another problem is not covered
    d, fk = go(mk("incident_open", "crit", task="x"), cfgx(dedupe={"covered_by": {"incident_open": []}}), now=NOON + 3 * H)
    assert d.ok                                                                    # coverage can be switched off


def test_recovery_and_incident_resolved_cover_each_other():
    go(mk("alert", "crit"), now=NOON)
    assert go(mk("recovery", "ok"), now=NOON + H)[0].ok
    assert go(mk("incident_resolved", "ok"), now=NOON + H + 30)[0].skipped == "covered"
    go(mk("alert", "crit", task="z"), now=NOON)
    assert go(mk("incident_resolved", "ok", task="z"), now=NOON + H)[0].ok
    assert go(mk("recovery", "ok", task="z"), now=NOON + H + 30)[0].skipped == "covered"


# =========================================================================== quiet hours
@pytest.mark.parametrize("hh,mm,expect", [(22, 59, False), (23, 29, False), (23, 30, True), (0, 0, True), (3, 0, True),
                                          (6, 59, True), (7, 0, False), (12, 0, False)])
def test_quiet_window_crossing_midnight(hh, mm, expect):
    qh = {"enabled": True, "window": "23:30-07:00", "tz": "America/Toronto"}
    assert notify.in_quiet_hours(at(hh, mm), qh) is expect


def test_quiet_window_same_day_disabled_malformed_and_timezone():
    assert notify.in_quiet_hours(at(13, 30), {"enabled": True, "window": "13:00-14:00", "tz": "America/Toronto"})
    assert not notify.in_quiet_hours(at(14, 0), {"enabled": True, "window": "13:00-14:00", "tz": "America/Toronto"})
    assert not notify.in_quiet_hours(at(2), {"enabled": False, "window": "23:30-07:00", "tz": "America/Toronto"})
    for bad in ("", "nonsense", "25:00-26:00", "10:00-10:00", "23:30"):
        assert not notify.in_quiet_hours(at(2), {"enabled": True, "window": bad, "tz": "America/Toronto"})
    # the same instant is 02:00 in Toronto (quiet) and 15:00 in Tokyo (not quiet)
    assert notify.in_quiet_hours(at(2), {"enabled": True, "window": "23:30-07:00", "tz": "America/Toronto"})
    assert not notify.in_quiet_hours(at(2), {"enabled": True, "window": "23:30-07:00", "tz": "Asia/Tokyo"})
    assert isinstance(notify.in_quiet_hours(at(2), {"enabled": True, "window": "23:30-07:00", "tz": "Not/AZone"}), bool)   # bad zone: host time


def test_quiet_hours_drop_the_text_but_not_the_email_for_non_critical():
    d, fk = go(mk("alert", "warn", facts={"escalate": True}, task="n"), now=at(2))
    assert fk.calls[0].channels == ["email"] and any("quiet hours" in w for w in d.why)
    d, fk = go(mk("recovery", "ok", task="r"), now=at(3))
    assert fk.calls[0].channels == ["email"]                     # a recovery text waits for the morning too


def test_critical_is_never_held_by_quiet_hours():
    d, fk = go(mk("alert", "crit"), now=at(3))
    assert set(fk.calls[0].channels) == {"sms", "email"}
    d, fk = go(mk("incident_open", "crit", task="i"), now=at(3))
    assert set(fk.calls[0].channels) == {"sms", "email"}


def test_sms_only_route_during_quiet_hours_is_held_and_handled():
    d, fk = go(mk("alert", "warn"), cfgx(routes={"alert": {"warn": "sms"}}), now=at(2))
    assert (d.skipped, d.handled, d.ok) == ("quiet-hours", True, False) and fk.calls == []


def test_quiet_hours_can_hold_email_too_and_never_hold_a_test():
    cfg = cfgx(quiet_hours={"suppress": ["sms", "email"]})
    d, fk = go(mk("digest_daily", "ok"), cfg, now=at(2))
    assert d.skipped == "quiet-hours" and fk.calls == []
    assert go(mk("alert", "crit"), cfg, now=at(2))[0].ok         # crit still passes
    t = notify.Event("test", "ok", "x", "y", facts={"as": "recovery"}, dedupe_key="t1")
    d, fk = go(t, now=at(2))
    assert set(fk.calls[0].channels) == {"sms", "email"}         # you can test the text path at 02:00


def test_quiet_hours_do_not_spend_the_sms_budget():
    go(mk("alert", "warn", facts={"escalate": True}), now=at(2))
    assert [r[2] for r in state()["sent"]] == [0]


# =========================================================================== budgets
def test_per_kind_budget_then_throttled_audit_and_retryable():
    cfg = cfgx(budget={"per_day": {"alert": 3}}, routes={"alert": {"warn": "email"}})
    for i in range(3):
        assert go(mk("alert", "warn", task=f"t{i}"), cfg, now=NOON + i)[0].ok
    d, fk = go(mk("alert", "warn", task="t3"), cfg, now=NOON + 10)
    assert (d.ok, d.handled, d.skipped) == (False, False, "budget") and fk.calls == []   # not handled: the caller retries
    assert [a["action"] for a in audit_rows()].count("budget-exhausted") == 1
    n_log = len(log_rows())
    d, fk = go(mk("alert", "warn", task="t3"), cfg, now=NOON + 20)                       # the retry next run: same story, no spam
    assert d.skipped == "budget" and len(log_rows()) == n_log
    assert [a["action"] for a in audit_rows()].count("budget-exhausted") == 1
    assert go(mk("alert", "warn", task="t3"), cfg, now=NOON + 2 * H)[0].skipped == "budget"
    assert [a["action"] for a in audit_rows()].count("budget-exhausted") == 2          # an hour later it says so again
    assert go(mk("alert", "warn", task="t3"), cfg, now=NOON + D + 100)[0].ok            # the window rolled


def test_critical_bypasses_kind_and_total_caps_but_not_the_hard_cap():
    cfg = cfgx(budget={"per_day": {"alert": 1}, "total_per_day": 2, "hard_cap_per_day": 4})
    assert go(mk("alert", "warn", task="a"), cfg, now=NOON)[0].ok
    assert go(mk("alert", "warn", task="b"), cfg, now=NOON + 1)[0].skipped == "budget"      # kind cap
    for i, t in enumerate(("c", "d", "e")):                                                  # critical ignores both caps ...
        assert go(mk("alert", "crit", task=t), cfg, now=NOON + 2 + i)[0].ok
    d, fk = go(mk("alert", "crit", task="f"), cfg, now=NOON + 9)
    assert d.skipped == "budget" and "hard" in d.note and fk.calls == []                    # 4 sent: even critical stops


def test_total_budget_across_kinds_and_crit_bypass_switch():
    cfg = cfgx(budget={"total_per_day": 2})
    assert go(mk("maintenance", "ok", task="m1"), cfg)[0].ok
    assert go(mk("digest_daily", "ok", task="m2"), cfg)[0].ok
    assert go(mk("maintenance", "ok", task="m3"), cfg)[0].skipped == "budget"
    assert go(mk("alert", "crit", task="c"), cfg)[0].ok
    cfg2 = cfgx(budget={"total_per_day": 1, "crit_bypass": False})
    go(mk("maintenance", "ok", task="m1"), cfg2)
    assert go(mk("alert", "crit", task="c2"), cfg2)[0].skipped == "budget"


def test_sms_budget_falls_back_to_email_and_critical_has_a_reserve():
    cfg = cfgx(budget={"sms_per_day": 2, "crit_sms_reserve": 1})
    for t in ("w1", "w2"):
        assert set(go(mk("alert", "warn", task=t, facts={"escalate": True}), cfg)[1].calls[0].channels) == {"sms", "email"}
    d, fk = go(mk("alert", "warn", task="w3", facts={"escalate": True}), cfg)
    assert fk.calls[0].channels == ["email"] and "sms budget reached" in d.note + " ".join(d.why)
    d, fk = go(mk("alert", "crit", task="c1"), cfg)
    assert set(fk.calls[0].channels) == {"sms", "email"}         # the reserve is for criticals
    d, fk = go(mk("alert", "crit", task="c2"), cfg)
    assert fk.calls[0].channels == ["email"]                      # and it ends too: a flood cannot run up a phone bill


def test_sms_only_route_over_the_sms_budget_is_a_budget_skip():
    cfg = cfgx(budget={"sms_per_day": 0, "crit_sms_reserve": 0}, routes={"alert": {"warn": "sms"}})
    d, fk = go(mk("alert", "warn"), cfg)
    assert d.skipped == "budget" and fk.calls == []


def test_budget_window_is_rolling_24h_and_failures_do_not_count():
    cfg = cfgx(budget={"per_day": {"maintenance": 1}})
    assert go(mk("maintenance", "ok", task="a"), cfg, now=NOON)[0].ok
    assert go(mk("maintenance", "ok", task="b"), cfg, now=NOON + D - 5)[0].skipped == "budget"
    assert go(mk("maintenance", "ok", task="b"), cfg, now=NOON + D + 5)[0].ok
    cfg = cfgx(budget={"per_day": {"maintenance": 1}})
    (core.STATE_DIR / "notify-state.json").unlink()
    assert not go(mk("maintenance", "ok", task="c"), cfg, transport=Fake(legs={"email": "failed"}))[0].ok
    assert go(mk("maintenance", "ok", task="c"), cfg)[0].ok       # the failed attempt used nothing up


def test_future_timestamps_in_state_expire_instead_of_blocking_forever():
    cfg = cfgx(budget={"per_day": {"maintenance": 1}})
    go(mk("maintenance", "ok", task="a"), cfg, now=NOON + 10 * D)       # the clock was wrong (far in the future) ...
    d, fk = go(mk("maintenance", "ok", task="b"), cfg, now=NOON)        # ... and then stepped back
    assert d.ok


# =========================================================================== mute
def test_mute_file_silences_everything_but_critical_and_tests():
    (core.CONF_DIR / "NOTIFY_MUTE").write_text("")
    d, fk = go(mk("alert", "warn"))
    assert (d.skipped, d.handled) == ("muted", True) and fk.calls == []
    assert go(mk("recovery", "ok", task="r"))[0].skipped == "muted"
    assert go(mk("maintenance", "ok", task="m"))[0].skipped == "muted"
    assert go(mk("alert", "crit", task="c"))[0].ok
    t = notify.Event("test", "warn", "x", "y", facts={"as": "alert"}, dedupe_key="t")
    assert go(t)[0].ok


def test_the_global_pause_file_does_not_mute_notifications():
    (core.CONF_DIR / "PAUSE").write_text("")
    assert go(mk("alert", "warn"))[0].ok


# =========================================================================== delivery outcomes and fallbacks
def test_partial_success_sms_ok_email_failed_counts_as_delivered():
    fk = Fake(legs={"sms": "sent", "email": "failed"}, errors={"email": "smtp 535 auth"})
    d, _ = go(mk(), transport=fk)
    assert d.ok and d.handled and d.channels == ["sms"] and d.legs == {"sms": "sent", "email": "failed"}
    assert "email failed (smtp 535 auth)" in d.note
    rows = audit_rows()
    assert any(r["action"] == "send" and r["outcome"] == "sent" for r in rows)
    assert any(r["action"] == "send" and r["target"] == "alert: email leg" and r["outcome"].startswith("failed rc=1") and "smtp 535" in r["outcome"]
               for r in rows)                                    # a failed channel is a failed "send" row: alert_path_health reads exactly that
    assert log_rows()[-1]["legs"] == {"sms": "sent", "email": "failed"} and log_rows()[-1]["ok"] is True
    assert [r[2] for r in state()["sent"]] == [1]                  # the text really went: it spent one SMS


def test_partial_success_email_ok_sms_failed_and_state_reflects_it():
    d, _ = go(mk("alert", "crit"), transport=Fake(legs={"sms": "failed", "email": "sent"}, errors={"sms": "no route"}))
    assert d.ok and d.channels == ["email"]
    assert [r[2] for r in state()["sent"]] == [0]
    assert state()["esc"]["disk_forecast"]["sms"] is False       # so its recovery will not text either
    d2, fk = go(mk("recovery", "ok"), now=NOON + H)
    assert fk.calls[0].channels == ["email"]


def test_both_legs_failing_is_a_failure_that_leaves_no_trace_in_state():
    fk = Fake(legs={"sms": "failed", "email": "failed"}, errors={"sms": "a", "email": "b"})
    d, _ = go(mk(), transport=fk)
    assert (d.ok, d.handled) == (False, False) and d.channels == []
    assert {k: v for k, v in state().items() if k != "outbox"} == {"v": 1, "sent": [], "dedupe": {}, "esc": {}}     # no budget, dedupe or escalation trace
    assert d.queued and [e["kind"] for e in state()["outbox"]] == ["alert"]            # ... but a critical page is never just lost: it waits in the outbox
    assert audit_rows()[-1]["outcome"].startswith("failed rc=1") and audit_rows()[-1]["action"] == "send"
    rec = log_rows()[-1]
    assert rec["ok"] is False and rec["channels"] == [] and "sms failed (a)" in rec["note"]


def test_transport_exception_is_a_failed_delivery_not_a_crash():
    d, _ = go(mk(), transport=Fake(exc=RuntimeError("boom with owner@example.com")))
    assert not d.ok and "RuntimeError" in d.note and "owner@example.com" not in d.note


def test_transport_says_ok_without_legs_assumes_the_requested_channels():
    d, _ = go(mk(), transport=Fake(legs={}, ok=True))
    assert d.ok and set(d.channels) == {"sms", "email"}


def test_primary_broke_before_any_channel_reported_tries_the_bridge():
    primary = Fake(legs={}, ok=False, fatal="cannot import alert_transports", rc=1)
    bridge = Fake(via="bridge")
    d, _ = go(mk(), transport=primary, fallback=bridge)
    assert d.ok and len(bridge.calls) == 1 and "via bridge fallback" in d.note
    assert bridge.calls[0].channels == ["sms", "email"]


@pytest.mark.parametrize("primary,route", [
    (Fake(legs={}, ok=False, fatal="timed out", rc=124), "both"),                        # may have half-sent: never resend
    (Fake(legs={"sms": "failed", "email": "failed"}, ok=False, fatal="x", rc=1), "both"),  # the channels answered: not a transport break
    (Fake(legs={}, ok=False, fatal="", rc=1), "both"),                                    # nothing to explain
    (Fake(legs={}, ok=False, fatal="broken", rc=1), "email"),                             # the bridge cannot honour an email-only route
])
def test_bridge_fallback_is_not_used_when_it_would_be_wrong(primary, route):
    bridge = Fake(via="bridge")
    d, _ = go(mk(), cfgx(routes={"alert": {"crit": route}}), transport=primary, fallback=bridge)
    assert not d.ok and bridge.calls == []


def test_both_transports_failing_reports_both_reasons():
    d, _ = go(mk(), transport=Fake(legs={}, ok=False, fatal="child died", rc=1),
              fallback=Fake(legs={}, ok=False, fatal="bridge rc=1", rc=1, via="bridge"))
    assert not d.ok and "child died" in d.note and "bridge rc=1" in d.note
    d, _ = go(mk(task="x2"), transport=Fake(exc=OSError("a")), fallback=Fake(exc=OSError("b")), now=NOON + 400)       # past the breaker window
    assert not d.ok and "fallback raised" in d.note


def test_html_render_failure_sends_plain_text_email(monkeypatch):
    def broken(*a, **k):
        raise ValueError("renderer exploded")
    monkeypatch.setattr(T, "build_html", broken)
    d, fk = go(mk())
    m = fk.calls[0]
    assert d.ok and m.html == "" and m.plain and "Disk space" in m.plain and "email" in m.channels
    assert "html render failed" in d.note and "plain text sent" in d.note


def test_oversized_html_is_replaced_by_plain_text(monkeypatch):
    monkeypatch.setattr(T, "build_html", lambda *a, **k: "x" * 100_000)
    d, fk = go(mk())
    assert d.ok and fk.calls[0].html == "" and "html too large" in d.note


def test_prepare_failure_still_sends_bare_text(monkeypatch):
    monkeypatch.setattr(T, "prepare", lambda *a, **k: (_ for _ in ()).throw(KeyError("x")))
    d, fk = go(mk())
    m = fk.calls[0]
    assert d.ok and m.sms.startswith("homelab:") and m.subject.startswith("[homelab]") and "Disk space" in m.plain
    assert m.html == "" and "prepare failed" in d.note


def test_a_failing_sms_builder_degrades_to_a_bare_line_that_is_still_valid(monkeypatch):
    monkeypatch.setattr(T, "build_sms", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    d, fk = go(mk(title="Disk space — café", summary="see https://example.com/x"))
    m = fk.calls[0]
    assert d.ok and m.sms.isascii() and len(m.sms) <= 130 and "http" not in m.sms and not T.URL_RE.search(m.sms)


def test_real_transports_are_refused_under_pytest_so_a_test_can_never_send():
    d = notify.send(mk(), cfgx(), NOON)                           # no transport injected
    assert not d.ok and "blocked under pytest" in d.note
    assert state()["sent"] == []                                  # and nothing was spent


def test_concurrent_senders_cannot_overspend_the_budget():
    cfg = cfgx(budget={"per_day": {"maintenance": 3}})
    fk = Fake(delay=0.02)
    out: list[notify.Delivery] = []

    def one(i):
        out.append(notify.send(mk("maintenance", "ok", task=f"t{i}"), cfg, NOON, transport=fk))
    th = [threading.Thread(target=one, args=(i,)) for i in range(10)]
    [t.start() for t in th]
    [t.join() for t in th]
    assert sum(d.ok for d in out) == 3 and len(fk.calls) == 3
    assert sum(d.skipped == "budget" for d in out) == 7 and len(state()["sent"]) == 3


def test_concurrent_duplicates_send_once():
    fk = Fake(delay=0.05)
    out: list[notify.Delivery] = []
    th = [threading.Thread(target=lambda: out.append(notify.send(mk(), cfgx(), NOON, transport=fk))) for _ in range(6)]
    [t.start() for t in th]
    [t.join() for t in th]
    assert len(fk.calls) == 1 and sum(d.ok for d in out) == 1 and sum(d.skipped == "dedupe" for d in out) == 5


def test_a_dead_senders_claim_expires_instead_of_blocking_the_alert():
    """A process that died between claim and commit leaves a claim row and an in-flight dedupe entry."""
    (core.STATE_DIR / "notify-state.json").write_text(json.dumps({
        "v": 1, "sent": [[NOON, "alert", 1, "deadbeef"]], "dedupe": {"alert|disk_forecast": {"ts": NOON, "sev": "crit", "c": "deadbeef"}}, "esc": {}}))
    assert go(mk(), now=NOON + 60)[0].skipped == "dedupe"            # in flight: a second sender waits
    d, fk = go(mk(), now=NOON + notify.CLAIM_TTL_S + 60)             # ten minutes on, the claim is dead
    assert d.ok and len(fk.calls) == 1 and len(state()["sent"]) == 1


def test_unwritable_state_fails_open_and_still_sends():
    blocker = core.STATE_DIR.parent / "afile"
    blocker.write_text("x")
    core.STATE_DIR = blocker / "state"                               # mkdir under a regular file: impossible
    d, fk = go(mk())
    assert d.ok and len(fk.calls) == 1                               # a silent alert path is worse than a repeated one


def test_corrupt_state_is_replaced_not_fatal():
    (core.STATE_DIR / "notify-state.json").write_text("{ not json")
    assert go(mk())[0].ok
    (core.STATE_DIR / "notify-state.json").write_text(json.dumps({"v": 1, "sent": "x", "dedupe": [], "esc": 3}))
    assert go(mk(task="b"))[0].ok and state()["v"] == 1


def test_state_and_log_file_permissions():
    go(mk())
    assert (core.STATE_DIR / "notify-state.json").stat().st_mode & 0o777 == 0o600
    assert (core.STATE_DIR / "notifications.jsonl").stat().st_mode & 0o777 == 0o644      # world-readable: no secrets in it


def test_dry_run_renders_and_routes_but_records_nothing():
    fk = Fake()
    d = notify.send(mk(), cfgx(), NOON, transport=fk, dry_run=True)
    assert fk.calls == [] and d.rendered and d.rendered["sms"] and d.rendered["html"] and d.skipped == "dry-run"
    assert not (core.STATE_DIR / "notify-state.json").exists() and not (core.STATE_DIR / "notifications.jsonl").exists()


# =========================================================================== SMS
def sms_of(title="Disk space", summary="/ is 4% free", kind="alert", sev="crit", **facts) -> str:
    m = T.prepare(notify.Event(kind, sev, title, summary, facts=facts or None), site={"host_label": "h"})
    return T.build_sms(m, "homelab")


def test_sms_shape_and_prefix_per_kind():
    assert sms_of("disk", "4% free") == "homelab: CRIT disk / 4% free"
    assert sms_of("Services", "1 failed unit", sev="warn").startswith("homelab: WARN Services / ")
    assert sms_of("Disk space", "back to 31% free", "recovery", "ok").startswith("homelab: OK Disk space / ")
    assert sms_of("Cleanup", "24 GiB freed", "maintenance", "ok").startswith("homelab: DONE ")
    assert sms_of("Plex mount", "gone", "incident_open", "crit").startswith("homelab: INCIDENT ")
    assert sms_of("Plex mount", "back", "incident_resolved", "ok").startswith("homelab: RESOLVED ")
    assert sms_of("x", "", "recovery", "ok") == "homelab: OK x / recovered"
    assert sms_of("Disk space", "/ is 4% free").startswith("homelab: CRIT Disk space - / is")      # no "/ /" stutter


def test_sms_is_one_ascii_segment_whatever_the_input():
    nasty = ["café — “smart” quotes … → €5", "emoji \U0001F525 fire \U0001F680", "‮RTL‬ override",
             "tab\there\nnewline\r\nmore", "x" * 500, "a b " * 200, "中文 only", "\x00\x01 nul"]
    for t in nasty:
        for s in nasty:
            out = sms_of(t, s)
            assert out.isascii() and len(out) <= T.SMS_LIMIT == 130, out
            assert "\n" not in out and "\r" not in out and "\t" not in out and not re.search(r"[\x00-\x1f\x7f]", out)
            assert out.startswith("homelab: CRIT ")


def test_sms_title_survives_a_long_summary_and_a_long_title_is_clipped_with_ellipsis():
    out = sms_of("Disk space", "z" * 400)
    assert out.startswith("homelab: CRIT Disk space / z") and out.endswith("...") and len(out) == 130
    out = sms_of("T" * 300, "fits after the clipped title")                              # titles are clipped to 100 first
    assert out.startswith("homelab: CRIT TTT") and len(out) <= 130 and "fits after" in out
    long_prefix = T.build_sms(T.prepare(notify.Event("alert", "crit", "T" * 300, "never shown")), "p" * 60)
    assert long_prefix.startswith("pppp") and long_prefix.endswith("...") and len(long_prefix) == 130 and "never" not in long_prefix


@pytest.mark.parametrize("text", [
    "see https://maintainer.ohmzhomelab.ca/#/capacity now", "mail owner@example.com about it", "visit www.example.org today",
    "plex.tv is down", "docker.io pull failed", "check example.ca/path?x=1 please", "http://10.0.0.5:8080/x", "HTTPS://UPPER.EXAMPLE.COM",
    "user:pw@host.example.net", "ftp://files.example.com/a", "a.b.c.d.example.co.uk"])
def test_sms_never_contains_a_url_address_or_bare_hostname(text):
    out = sms_of("Check", text)
    assert not T.URL_RE.search(out), out
    assert "http" not in out.lower() and "@" not in out and "www." not in out.lower()


def test_sms_keeps_the_words_of_dotted_names_instead_of_losing_them():
    out = sms_of("Failed", "smart-alert.sh and nginx.service and notebook-db-alert.sh died")
    assert "nginx.service" in out and "smart-alert sh" in out                         # `.sh` is a TLD the gateway would drop
    assert "docker io" in sms_of("x", "docker.io timed out")


def test_sms_caller_wording_replaces_the_summary_and_is_sanitised():
    out = sms_of("Disk space", "long summary that should not appear", sms="4% left on root; clean up https://x.example.com")
    assert out == "homelab: CRIT Disk space / 4% left on root; clean up", out


def test_sms_test_label_and_custom_prefix():
    ev = notify.Event("test", "crit", "Disk space", "ignored", facts={"as": "alert"})
    out = T.build_sms(T.prepare(ev), "my.io")                      # a prefix is sanitised like any other text
    assert out == "my io: TEST CRIT Disk space / ignore, notification test"
    assert T.build_sms(T.prepare(ev), "") .startswith("homelab: TEST CRIT")


def test_sms_matches_what_hermes_would_send_unchanged():
    """The real alert_transports.sms_body must be a no-op on our text, and its URL pattern must still be ours."""
    p = Path("/home/ohmz/StudioProjects/ai-stack/scripts/alert_transports.py")
    if not p.exists():
        pytest.skip("alert_transports.py not on this machine")
    spec = importlib.util.spec_from_file_location("at_under_test", p)
    at_mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(at_mod)
    except Exception as exc:                                        # noqa: BLE001
        pytest.skip(f"alert_transports not importable here: {exc}")
    assert at_mod.URL_RE.pattern == T.URL_RE.pattern, "the SMS URL pattern drifted from Hermes's"
    for ev in notify.sample_events().values():
        s = T.build_sms(T.prepare(ev), "homelab")
        assert at_mod.sms_body(s) == s, s
    assert at_mod.sms_body(sms_of("a", "see https://x.example.com")) == sms_of("a", "see https://x.example.com")


# =========================================================================== subject, plain text
def test_subject_is_one_ascii_line_without_header_injection():
    ev = notify.Event("alert", "crit", "Bad\r\nBcc: evil@example.com\tx", "café\nsecond line " + "w" * 300)
    s = T.build_subject(T.prepare(ev))
    assert s.isascii() and len(s) <= 110 and not re.search(r"[\r\n\t\x00]", s) and s.startswith("[homelab] CRIT: Bad Bcc:")


def test_plain_text_has_every_section_and_a_dashboard_line():
    ev = mk("alert", "crit", details={"done": ["cleaned x"], "todo": ["do y"], "log": ["l1"], "text": "para",
                                       "timeline": [("22:01", "went red")], "sections": [{"title": "Sec", "lines": ["sl"], "kv": [("k", "v")],
                                                                                           "checks": [("c1", True, "n"), ("c2", False, ""), ("c3", None, "")]}]},
            facts={"Free": "4%", "link": "#/capacity", "tiles": [["4%", "free", "crit"]]})
    m = T.prepare(ev, site={"url": "https://maintainer.ohmzhomelab.ca", "host_label": "h"}, now=NOON)
    txt = T.build_plain(m)
    for needle in ("[CRITICAL] Disk space", "Host: h", "Check: disk_forecast", "Free: 4%", "What was done", "- cleaned x", "What to do", "1. do y",
                   "22:01  went red", "Details", "para", "Sec", "[ok] c1 n", "[FAIL] c2", "[--] c3", "Log excerpt", "4% free",
                   "Dashboard: https://maintainer.ohmzhomelab.ca/#/capacity", "Sent by homelab-maint on h."):
        assert needle in txt, needle


# =========================================================================== HTML: palette, family, safety
def reference_module():
    p = Path(T.REFERENCE)
    if not p.exists():
        pytest.skip("backup_report_html.py not on this machine")
    spec = importlib.util.spec_from_file_location("backup_ref", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_embedded_palette_is_byte_identical_to_the_backup_renderer():
    ref = reference_module()
    for name in ("CANVAS", "PANEL", "RAISE", "HOVER", "LINE", "LINE_SOFT", "TEXT", "SECONDARY", "MUTED", "AMBER", "AMBER_BRIGHT",
                 "ON_AMBER", "GREEN", "RED", "RADIUS", "FONT", "MONO"):
        assert T._DEFAULT_PALETTE[name] == getattr(ref, name), name
        assert getattr(T, name) == getattr(ref, name), name
    assert T.load_palette(T.REFERENCE) == T._DEFAULT_PALETTE


def test_palette_loader_reads_data_only_and_rejects_hostile_values(tmp_path):
    marker = tmp_path / "executed"
    f = tmp_path / "ref.py"
    f.write_text(f"open({str(marker)!r}, 'w').write('x')\nCANVAS = '#000001'\nPANEL = 'red; background:url(http://evil)'\n"
                 "TEXT, MUTED = '#fedcba', '#123456'\nGREEN = ('#111111',)\nRADIUS = '3px'\nFONT = 'a\"b'\nRED = '#12345'\n")
    pal = T.load_palette(f)
    assert not marker.exists(), "the reference file must never be executed"
    assert pal["CANVAS"] == "#000001" and pal["TEXT"] == "#fedcba" and pal["MUTED"] == "#123456" and pal["RADIUS"] == "3px"
    assert pal["PANEL"] == T._DEFAULT_PALETTE["PANEL"] and pal["GREEN"] == T._DEFAULT_PALETTE["GREEN"]       # rejected
    assert pal["FONT"] == T._DEFAULT_PALETTE["FONT"] and pal["RED"] == T._DEFAULT_PALETTE["RED"]
    assert T.load_palette(tmp_path / "missing.py") == T._DEFAULT_PALETTE
    (tmp_path / "bad.py").write_text("def (:\n")
    assert T.load_palette(tmp_path / "bad.py") == T._DEFAULT_PALETTE


ALLOWED_TAGS = {"html", "head", "meta", "title", "body", "div", "table", "tr", "td", "span", "a", "br"}
ALLOWED_ATTRS = {"lang", "style", "width", "height", "align", "valign", "role", "cellpadding", "cellspacing", "border", "bgcolor", "charset", "name",
                 "content", "href", "colspan"}


class Audit(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags, self.attrs, self.hrefs, self.text = [], [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for k, v in attrs:
            self.attrs.append((tag, k, v))
            if k == "href":
                self.hrefs.append(v)

    def handle_data(self, data):
        self.text.append(data)


def audit_html(doc: str, base="https://maintainer.ohmzhomelab.ca") -> Audit:
    a = Audit()
    a.feed(doc)
    assert set(a.tags) <= ALLOWED_TAGS, set(a.tags) - ALLOWED_TAGS
    for tag, k, v in a.attrs:
        assert k in ALLOWED_ATTRS and not k.startswith("on"), (tag, k)
        if k == "style":
            assert not re.search(r"(?i)url\(|expression\(|javascript:|@import|behavior:|-moz-binding", v or ""), v
    for h in a.hrefs:
        assert h.startswith(base) and re.fullmatch(r"[A-Za-z0-9:/._#~?=&-]+", h), h
    assert not re.search(r"(?i)<(script|style|link|iframe|object|embed|img|form|input|svg|base)\b", doc)
    return a


def render_all() -> dict[str, str]:
    nc = notify.load_config(cfgx())
    out = {}
    for k, ev in notify.sample_events().items():
        dec = notify.decide(ev, nc, copy.deepcopy(notify._EMPTY_STATE), NOON)
        dec.channels = ["sms", "email"]
        out[k] = notify._render(ev, nc, dec, NOON)[0].html
    return out


@pytest.mark.parametrize("name", sorted(notify.sample_events()))
def test_every_template_renders_in_the_ohmz_cloud_family(name):
    doc = render_all()[name]
    a = audit_html(doc)
    for token in (T.CANVAS, T.PANEL, T.RAISE, T.LINE_SOFT, T.TEXT, T.SECONDARY, T.MUTED, T.AMBER):
        assert token in doc
    assert doc.startswith("<!doctype html>") and 'role="presentation"' in doc and "&#937;" in doc and "Ohmz" in doc and "Maintenance" in doc
    assert 'bgcolor="#211f1d"' in doc and "max-width:600px" in doc and 'meta name="color-scheme"' in doc
    assert len(doc) < 60_000 and "Ohmz Cloud" in doc
    assert any("maintainer.ohmzhomelab.ca" in h for h in a.hrefs)       # the dashboard link is email-only and present


def test_status_colours_stay_real():
    docs = render_all()
    assert T.RED in docs["alert.crit"] and T.GREEN not in docs["alert.crit"].split("</tr>", 1)[0]
    assert T.GREEN in docs["recovery"] and T.GREEN in docs["incident_resolved"] and T.GREEN in docs["maintenance"]
    assert T.AMBER_BRIGHT in docs["alert.warn"]
    assert docs["alert.crit"].count(T.RED) >= 2 and "Critical" in docs["alert.crit"] and "Warning" in docs["alert.warn"]
    assert "Recovered" in docs["recovery"] and "Maintenance done" in docs["maintenance"] and "Daily digest" in docs["digest_daily"]
    assert "Weekly report" in docs["report_weekly"] and "Incident SEV2" in docs["incident_open"] and "Incident resolved" in docs["incident_resolved"]


# ---- review fix 1: the email must be phone-fluid (an unbreakable token in ANY field wraps; it never widens or clips the card)
class _WrapProbe(HTMLParser):
    """Text nodes with the style of the nearest <td>/<div> that holds them (spans and links inherit from their cell)."""
    VOID = {"meta", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str]] = []
        self.hits: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append((tag, dict(attrs).get("style") or ""))

    def handle_endtag(self, tag):
        while self.stack and self.stack.pop()[0] != tag:
            pass

    def handle_data(self, data):
        if "LONGTOKEN" in data and not any(t in ("head", "title") for t, _s in self.stack):            # <title> is never laid out
            owner = next((st for t, st in reversed(self.stack) if t in ("td", "div")), "")
            if "display:none" not in owner:                                                          # nor is the inbox preheader
                self.hits.append((data.strip(), owner))


def long_token_event() -> notify.Event:
    """Every dynamic field carries a long unbreakable token (a path, a unit name, a hash), the way real alerts do."""
    def tok(name: str, n: int = 64) -> str:
        return ("LONGTOKEN-" + name + "-" + "x" * n)[:n]
    ev = notify.Event(
        "alert", "crit", tok("title", 90), tok("summary", 300),
        {"done": [tok("done", 120)], "todo": [tok("todo", 200)], "log": [tok("log", 200)], "text": [tok("para", 200)],
         "timeline": [(tok("when", 26), tok("tltext", 150))],
         "sections": [{"title": tok("sectitle", 50), "lines": [tok("secline", 150)], "kv": [(tok("seckey", 50), tok("secval", 120))],
                       "checks": [(tok("checkname", 70), False, tok("checknote", 50))]}]},
        {tok("factkey", 38): tok("factval", 190), "tiles": [[tok("tilev", 12), tok("tilel", 18), "ok"]], "host": tok("host", 58), "sev": tok("sev", 12),
         "notified": 3}, "crit", "k", tok("task", 58))
    return ev


def test_email_panel_is_fixed_layout_and_every_dynamic_cell_wraps_long_tokens():
    """The two halves of the fix, asserted on the markup: `table-layout:fixed` on the 600px panel (its width never depends on its
    content) and `word-break:break-word` next to every `overflow-wrap:break-word` (overflow-wrap alone does not shrink an
    auto-layout table's min-content, so a long token still widened the card; fixed layout alone clipped it inside the card)."""
    m = T.prepare(long_token_event(), site={"url": "https://maintainer.ohmzhomelab.ca", "host_label": "h"}, now=NOON)
    doc = T.build_html(m)
    panel = re.search(r'<table[^>]*width="600"[^>]*style="([^"]*)"', doc)
    assert panel and "table-layout:fixed;" in panel[1] and "max-width:600px" in panel[1] and "width:100%" in panel[1]
    assert "word-break:break-word;" in panel[1] and "overflow-wrap:break-word;" in panel[1]            # inherited by every descendant too
    probe = _WrapProbe()
    probe.feed(doc)
    assert len(probe.hits) >= 22, [h[0][:20] for h in probe.hits]                                   # every field above was found in the page
    for text, style in probe.hits:
        if "white-space:nowrap" in style:                                                           # a status pill or a time stamp: bounded, never prose
            assert len(text) <= 40, text
        else:
            assert "overflow-wrap:break-word;" in style and "word-break:break-word;" in style, (text[:40], style)
    # the property the reviewer measured: no overflow-wrap is ever used without its word-break twin
    for style in re.findall(r'style="([^"]*)"', doc):
        assert ("overflow-wrap:break-word;" in style) == ("word-break:break-word;" in style), style
    # the real alert from the review: the Plex playbook step is one long quoted path
    step = 'findmnt "/var/snap/plexmediaserver/common/Library/Application Support/Plex Media Server/Media"'
    d2 = T.build_html(T.prepare(notify.Event("incident_open", "crit", "Plex media mount missing", "x", {"todo": [step]}), now=NOON))
    assert "table-layout:fixed;" in d2 and step.replace('"', "&quot;") in d2


def _chrome() -> str | None:
    import shutil
    return None if os.environ.get("HOMELAB_MAINT_NO_BROWSER_TESTS") else shutil.which("google-chrome") or shutil.which("chromium")


@pytest.mark.skipif(_chrome() is None, reason="no headless Chrome here (set HOMELAB_MAINT_NO_BROWSER_TESTS=1 to skip on purpose)")
def test_email_fits_a_390px_phone_in_headless_chrome_with_long_tokens(tmp_path, monkeypatch):
    """Measured, not assumed: each message in a 390px iframe (and a 320px one); no horizontal scroll, and nothing inside the
    card sticks out past its right edge (the clipped `Applica|` case). The long-token event and the real Plex alert overflowed
    (443 and 800+ px) before the fix."""
    import html as htmllib
    nc = notify.load_config(cfgx())
    plex = notify.Event("incident_open", "crit", "Plex media mount missing", "Plex would regenerate Media on the root disk",
                        {"todo": ['Check the bind mount: findmnt "/var/snap/plexmediaserver/common/Library/Application Support/Plex Media Server/Media"']},
                        {"sev": "SEV2"}, "crit", "k", "plex_media_mount_check")
    events = {**{k: v for k, v in notify.sample_events().items()}, "long-tokens": long_token_event(), "plex-real": plex}
    frames = ""
    for i, (name, ev) in enumerate(events.items()):
        dec = notify.decide(ev, nc, copy.deepcopy(notify._EMPTY_STATE), NOON)
        dec.channels = ["sms", "email"]
        frames += (f'<iframe data-name="{name}" style="width:WIDTHpx;height:900px;border:0;display:block" '
                   f'srcdoc="{htmllib.escape(notify._render(ev, nc, dec, NOON)[0].html, quote=True)}"></iframe>')
    script = """<pre id="out"></pre><script>addEventListener('load', () => setTimeout(() => { const r = {};
      document.querySelectorAll('iframe').forEach(f => { const doc = f.contentDocument, pe = doc.querySelector('table[width="600"]'), p = pe.getBoundingClientRect();
        let worst = 0; pe.querySelectorAll('td,div,span,a,tr,table').forEach(e => { const b = e.getBoundingClientRect(); if (b.width > 0) worst = Math.max(worst, b.right - p.right); });
        r[f.dataset.name] = [doc.documentElement.scrollWidth, Math.round(worst), Math.round(p.width)]; });
      document.getElementById('out').textContent = 'RESULT' + JSON.stringify(r) + 'END'; }, 300));</script>"""
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)               # the one place a test starts a real process: our own headless browser
    for width in (390, 320):
        page = tmp_path / f"page{width}.html"
        page.write_text(f"<!doctype html><html><body>{frames.replace('WIDTH', str(width))}{script}</body></html>")
        r = _REAL_RUN([_chrome(), "--headless=new", "--no-sandbox", "--disable-gpu", f"--user-data-dir={tmp_path}/prof{width}", "--virtual-time-budget=8000",
                       "--dump-dom", page.as_uri()], capture_output=True, text=True, timeout=90)
        m = re.search(r"RESULT(.*?)END", r.stdout, re.S)
        if not m:
            pytest.skip(f"headless Chrome produced no result (rc={r.returncode}): {r.stderr[-120:]!r}")
        res = json.loads(htmllib.unescape(m[1]))
        assert len(res) == len(events)
        for name, (scroll, clipped, panel) in res.items():
            assert scroll <= width and clipped <= 1, f"{name} at {width}px: scrollWidth {scroll}, {clipped}px past the card edge (card {panel}px)"


EVIL = ['<script>alert(1)</script>', '"><img src=x onerror=alert(1)>', "'; DROP TABLE x;--", '</td></tr></table><div onmouseover="x()">',
        '&lt;b&gt; &amp; &#x3c;', 'javascript:alert(1)', '<a href="https://evil.example/x">click</a>', '{{7*7}} ${jndi:ldap://x}',
        '</title><script>1</script>', '<style>body{display:none}</style>', "<svg onload=alert(1)>"]


def model_strings(m: T.Model) -> list[str]:
    """Every dynamic string the model will put into the page."""
    out = [m.title, m.summary, m.task, m.host, m.pill, *m.done, *m.todo, *m.paras, *m.log]
    out += [x for pair in [*m.facts, *m.timeline] for x in pair] + [x for t in m.tiles for x in t[:2]]
    for sec in m.sections:
        out += [sec["title"], *sec["lines"]] + [x for kv in sec["kv"] for x in kv] + [x for c in sec["checks"] for x in (c[0], c[2])]
    return [x for x in out if x]


@pytest.mark.parametrize("evil", EVIL)
def test_every_dynamic_field_is_escaped(evil):
    ev = notify.Event(
        "alert", "crit", "T " + evil, "S " + evil,
        {"done": ["D " + evil], "todo": ["W " + evil], "log": ["L " + evil], "text": "P " + evil,
         "timeline": [("TW " + evil, "TT " + evil)],
         "sections": [{"title": "ST " + evil, "lines": ["SL " + evil], "kv": [("SK " + evil, "SV " + evil)], "checks": [("SC " + evil, False, "SN " + evil)]}]},
        {"FK " + evil: "FV " + evil, "tiles": [["V " + evil, "TL " + evil, "ok"]], "host": "H " + evil, "sev": "SEV " + evil,
         "link": evil, "notified": 3}, "crit", "k", "task " + evil)
    m = T.prepare(ev, site={"url": "https://maintainer.ohmzhomelab.ca", "host_label": "x"}, now=NOON)
    doc = T.build_html(m)
    audit_html(doc)                                                  # only allow-listed tags/attributes, one safe href at most
    shown = model_strings(m)
    assert len(shown) >= 18, "the model dropped fields it should show"
    for text in shown:
        assert T.esc(text) in doc, text
    if T.esc(evil) != evil:
        assert evil not in doc                                       # the raw payload never appears
    # the plain part is text/plain and the SMS/subject are single-line ASCII text: no control characters, no newlines
    for one_line in (T.build_sms(m), T.build_subject(m)):
        assert one_line.isascii() and "\n" not in one_line


def test_quotes_angle_brackets_and_ampersands_are_entity_encoded_in_every_field():
    evil = '<b onclick="x">&\''
    ev = notify.Event("alert", "crit", "T" + evil, "S" + evil, {"done": ["D" + evil], "todo": ["W" + evil], "text": "P" + evil},
                      {"FK" + evil: "FV" + evil, "tiles": [["V" + evil, "TL" + evil]]}, "crit", "k", "K" + evil)
    doc = T.build_html(T.prepare(ev, site={"host_label": "H" + evil}, now=NOON))
    for p in "T S D W P FK FV V TL K H".split():
        assert T.esc(p + evil) in doc, p
    assert T.esc(evil) == "&lt;b onclick=&quot;x&quot;&gt;&amp;&#x27;"
    assert "<b onclick" not in doc and 'onclick="x"' not in doc
    audit_html(doc)


def test_links_are_built_only_from_validated_parts():
    site = {"url": "https://maintainer.ohmzhomelab.ca", "host_label": "h"}
    good = T.prepare(notify.Event("alert", "crit", "t", "s", facts={"link": "#/incidents"}), site=site)
    assert good.url == "https://maintainer.ohmzhomelab.ca/#/incidents"
    assert T.site_url("https://x.ca/", "reports/2026-W40") == "https://x.ca/#/reports/2026-W40"
    assert T.site_url("https://x.ca/maint", "") == "https://x.ca/maint"
    for frag in ("javascript:alert(1)", '#/x" onmouseover="y', "//evil.example", "#/../etc", "#/a/../b", "../x", "#/a b", "#/<x>", "x" * 90, "data:text/html,x"):
        assert T.site_url("https://x.ca", frag) == "https://x.ca", frag
    for base in ("javascript:alert(1)", "ftp://x.ca", 'https://x.ca" onclick="y', "https://", "", None, "//x.ca", "https://x.ca/a b"):
        assert T.site_url(base, "#/ok") == "", base
    doc = T.build_html(T.prepare(notify.Event("alert", "crit", "t", "s", facts={"link": "javascript:alert(1)"}), site={"url": 'javascript:alert(1)'}))
    assert "href=" not in doc and "javascript" not in doc.lower()                       # no button, no href, nothing to click
    audit_html(doc)


def test_control_and_bidi_characters_never_reach_any_rendering():
    ev = notify.Event("alert", "crit", "a‮b⁦c\x00d\x1be\x7f", "s‮\r\nBcc: x\x0b", {"todo": ["t‮\x00"]}, {"K\x00": "V‮"})
    m = T.prepare(ev)
    for out in (T.build_html(m), T.build_plain(m), T.build_sms(m), T.build_subject(m)):
        assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f‪-‮⁦-⁩]", out)


def test_secrets_quoted_by_a_task_never_leave_in_any_rendering_or_the_log():
    secrets = ["hunter2", "abcdefgh12345678", "sk-" + "A" * 24, "ghp_" + "b" * 30, "AKIA" + "C" * 16, "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 8, "p4ss"]
    summary = (f"login failed password={secrets[0]} Authorization: Bearer {secrets[1]} key {secrets[2]} gh {secrets[3]} aws {secrets[4]} "
               f"jwt {secrets[5]} url https://user:{secrets[6]}@host.example/x X-Fan-Token: {secrets[1]} api_key=zz9 token: qq1")
    d, fk = go(mk(summary=summary, title="Login " + secrets[2], details={"log": [summary], "todo": [summary], "text": summary}, facts={"Cfg": summary}))
    m = fk.calls[0]
    blob = " ".join([m.sms, m.subject, m.plain, m.html]) + (core.STATE_DIR / "notifications.jsonl").read_text() + json.dumps(audit_rows())
    for s in secrets + ["zz9", "qq1"]:
        assert s not in blob, s
    assert "password=<redacted>" in m.plain and "login failed" in m.plain               # the sentence survives, only the value goes


def test_output_is_bounded_whatever_the_input_size():
    ev = notify.Event("alert", "crit", "t" * 9000, "s" * 90000,
                      {"log": ["l" * 5000] * 500, "todo": ["t" * 5000] * 99, "done": ["d" * 5000] * 99, "text": "p\n" * 9000,
                       "timeline": [("w", "x" * 999)] * 99, "sections": [{"title": "s", "lines": ["x" * 999] * 99, "kv": [("k", "v")] * 99}] * 30},
                      {f"k{i}": "v" * 999 for i in range(300)} | {"tiles": [["v", "l"]] * 50})
    m = T.prepare(ev)
    assert len(m.facts) <= 24 and len(m.log) <= 40 and len(m.todo) <= 12 and len(m.done) <= 20 and len(m.tiles) <= 4 and len(m.sections) <= 8
    assert len(m.title) <= 100 and len(m.summary) <= 600 and len(T.build_plain(m)) <= 30_000
    d, fk = go(ev)                                                    # end to end: Gmail clips near 102 KB, so a monster goes as plain text
    assert d.ok and (fk.calls[0].html == "" or len(fk.calls[0].html) <= 95_000) and len(fk.calls[0].plain) <= 30_000
    assert len(fk.calls[0].sms) <= 130 and len(fk.calls[0].subject) <= 110
    d, fk = go(mk(details={"log": ["one line"]}, facts={"Free": "4%"}), cfgx(), now=NOON + 7 * H)
    assert fk.calls[0].html and len(fk.calls[0].html) < 30_000        # a normal alert is small


def test_tiles_facts_and_value_formatting():
    ev = notify.Event("maintenance", "ok", "t", "s", facts={"Mode": "apply", "Count": 3, "Ratio": 0.5, "Flag": True, "Gone": None, "List": ["a", "b"],
                                                           "tiles": [["24.6 GiB", "freed", "ok"], ["3", "actions"], "bad", ["x"]]})
    m = T.prepare(ev)
    assert m.facts == [("Mode", "apply"), ("Count", "3"), ("Ratio", "0.5"), ("Flag", "yes"), ("List", "a, b")]
    assert m.tiles == [("24.6 GiB", "freed", T.GREEN), ("3", "actions", None)]
    assert T.dur(5) == "5s" and T.dur(65) == "1m05s" and T.dur(3700) == "1h01m" and T.dur(90000) == "1d01h" and T.dur(None) == "0s"


# =========================================================================== playbook text for alerts
def test_alert_what_to_do_comes_from_the_playbook_unless_the_caller_or_config_says_otherwise(monkeypatch):
    monkeypatch.setattr(notify, "playbook_loader", lambda task: [f"Run: homelab-maint status {task}", "Fix: something"])
    d, fk = go(mk("alert", "crit", task="t_pb"))
    assert "What to do\n  1. Run: homelab-maint status t_pb\n  2. Fix: something" in fk.calls[0].plain
    d, fk = go(mk("alert", "crit", task="t_pb2", details={"todo": ["caller wins"]}))
    assert "caller wins" in fk.calls[0].plain and "Fix: something" not in fk.calls[0].plain
    d, fk = go(mk("alert", "crit", task="t_pb3"), cfgx(todo={"t_pb3": ["config wins"]}))
    assert "config wins" in fk.calls[0].plain and "Fix: something" not in fk.calls[0].plain
    d, fk = go(mk("recovery", "ok", task="t_pb4"))
    assert "What to do" not in fk.calls[0].plain                                        # a recovery has nothing left to do


def test_playbook_failures_degrade_to_the_default_lines_or_nothing(monkeypatch):
    monkeypatch.setattr(notify, "playbook_loader", lambda task: (_ for _ in ()).throw(RuntimeError("x")))
    d, fk = go(mk(task="a1"), cfgx(todo={"_default": ["generic line"]}))
    assert d.ok and "generic line" in fk.calls[0].plain
    d, fk = go(mk(task="a2"))
    assert d.ok and "What to do" not in fk.calls[0].plain


def test_real_playbook_lines_come_from_etc_playbooks_toml(monkeypatch):
    monkeypatch.setattr(notify, "playbook_loader", notify.playbook_lines)
    lines = notify.playbook_lines("disk_forecast")
    assert lines and any(x.startswith("Run: ") for x in lines) and not any(x.startswith("$ ") for x in lines)
    assert any(x.startswith("Fix: ") for x in lines) and any(x.startswith(("Avoid: ", "Do not")) for x in lines) and len(lines) <= 6
    assert not any(x.startswith("Avoid: Do not") for x in notify.playbook_lines("no_such_task_xyz"))
    assert notify.playbook_lines("no_such_task_xyz")                                    # the generic playbook still helps
    from homelab_maint import incidents
    monkeypatch.setattr(incidents, "playbook_for", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("broken playbooks.toml")))
    assert notify.playbook_lines("disk_forecast") == []                                 # broken playbook source: no lines, no crash


# =========================================================================== the transport layer, with a fake `sh`
class FakeSh:
    """Stands in for core.sh inside notify. One scripted CompletedProcess per call."""

    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, cmd, timeout=60, check=False, input_=None, env=None):
        self.calls.append({"cmd": cmd, "timeout": timeout, "input": input_, "env": env})
        if self.results:
            r = self.results.pop(0)
            return r(cmd) if callable(r) else r
        return subprocess.CompletedProcess(cmd, 0, "", "")


def cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def msg(**kw) -> notify.Message:
    base = dict(handle="ohmz", sms="homelab: CRIT disk / 4%", subject="[homelab] CRIT: disk", plain="SECRET-BODY-MARKER plain",
                html="<html>HTML-MARKER</html>", channels=["sms", "email"])
    base.update(kw)
    return notify.Message(**base)


NC = notify.load_config({})


def test_hermes_transport_runs_the_child_as_the_owner_with_the_body_on_stdin(monkeypatch):
    fake = FakeSh(cp(0, json.dumps({"ok": True, "legs": {"sms": "sent", "email": "sent"}, "errors": {}, "fatal": ""}) + "\n"))
    monkeypatch.setattr(notify, "sh", fake)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    res = notify.hermes_transport(msg(), NC)
    c = fake.calls[0]
    assert c["cmd"][:4] == ["runuser", "-u", "ohmz", "--"] and c["cmd"][-3:] == ["-m", "homelab_maint.notify", "--child"]
    assert not any("SECRET-BODY-MARKER" in x or "HTML-MARKER" in x or "4%" in x for x in c["cmd"]), "bodies must never appear in argv"
    payload = json.loads(c["input"])
    assert payload["plain"].startswith("SECRET-BODY-MARKER") and payload["html"] and payload["channels"] == ["sms", "email"]
    assert payload["handle"] == "ohmz" and payload["scripts_dir"] == "/home/ohmz/StudioProjects/ai-stack/scripts"
    assert c["env"]["HOME"] and c["env"]["PYTHONPATH"] == str(ROOT) and c["timeout"] == 90
    assert res.ok and res.legs == {"sms": "sent", "email": "sent"} and res.via == "hermes"


def test_hermes_transport_runs_directly_when_not_root_or_when_the_user_is_root(monkeypatch):
    for euid, user in ((1000, "ohmz"), (0, "root")):
        fake = FakeSh(cp(0, '{"ok": true, "legs": {"email": "sent"}}\n'))
        monkeypatch.setattr(notify, "sh", fake)
        monkeypatch.setattr(os, "geteuid", lambda euid=euid: euid)
        nc = notify.load_config({"notify": {"transport": {"user": user}}})
        notify.hermes_transport(msg(channels=["email"]), nc)
        assert fake.calls[0]["cmd"][0] != "runuser", (euid, user)


@pytest.mark.parametrize("user,handle", [("ohmz; rm -rf /", "ohmz"), ("ohmz", "$(touch x)"), ("Ohmz", "ohmz"), ("", ""), ("a b", "ohmz"), ("ohmz\nroot", "ohmz"), ("-u", "ohmz")])
def test_hermes_transport_refuses_unsafe_user_or_handle_without_running_anything(monkeypatch, user, handle):
    fake = FakeSh()
    monkeypatch.setattr(notify, "sh", fake)
    nc = copy.deepcopy(NC)
    nc["transport"].update(user=user, handle=handle)
    res = notify.hermes_transport(msg(), nc)
    assert not res.ok and "invalid transport" in res.fatal and fake.calls == []


def test_hermes_transport_timeout_garbage_and_redaction(monkeypatch):
    monkeypatch.setattr(notify, "sh", FakeSh(cp(124, "", "timeout")))
    r = notify.hermes_transport(msg(), NC)
    assert not r.ok and r.fatal == "transport timed out" and r.rc == 124
    monkeypatch.setattr(notify, "sh", FakeSh(cp(1, "", "Traceback ... owner@example.com +1 416 555 0199 token=abc123def")))
    r = notify.hermes_transport(msg(), NC)
    assert not r.ok and r.rc == 1
    for leak in ("owner@example.com", "416 555 0199", "abc123def"):
        assert leak not in r.fatal
    monkeypatch.setattr(notify, "sh", FakeSh(cp(0, "noise\nnot json\n")))
    assert not notify.hermes_transport(msg(), NC).ok
    monkeypatch.setattr(notify, "sh", FakeSh(cp(0, "log line\n" + json.dumps({"ok": True, "legs": {"sms": "sent", "bogus": "sent"}}) + "\n")))
    r = notify.hermes_transport(msg(), NC)
    assert r.ok and r.legs == {"sms": "sent"}                         # an unknown leg name is dropped


def test_bridge_transport_calls_the_legacy_script_with_a_temp_detail_file(monkeypatch):
    seen = {}

    def run(cmd):
        p = Path(cmd[-1])
        seen["body"], seen["mode"], seen["exists"] = p.read_text(), p.stat().st_mode & 0o777, p.exists()
        return cp(0, "  sms sent to +14165550199 (SM1) body='x'\n  email sent to owner@example.com (10 chars plain, 20 html)\nRESULT: delivered\n")
    fake = FakeSh(run)
    monkeypatch.setattr(notify, "sh", fake)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    res = notify.bridge_transport(msg(), NC)
    cmd = fake.calls[0]["cmd"]
    assert cmd[:4] == ["runuser", "-u", "ohmz", "--"] and cmd[4:8] == ["/usr/local/sbin/backup-notify-hermes.py", "ohmz", "[homelab] CRIT: disk", "homelab: CRIT disk / 4%"]
    assert seen["exists"] and seen["mode"] == 0o644 and seen["body"].startswith("SECRET-BODY-MARKER")
    assert not Path(cmd[-1]).exists(), "the temp detail file must be removed"
    assert res.ok and res.legs == {"sms": "sent", "email": "sent"} and res.via == "bridge"
    assert "owner@example.com" not in json.dumps(res.__dict__) and "5550199" not in json.dumps(res.__dict__)


def test_bridge_transport_failure_timeout_and_unsafe_settings(monkeypatch):
    monkeypatch.setattr(notify, "sh", FakeSh(cp(1, "", "no transport configured (~/.hermes/alert_transports.env missing)")))
    r = notify.bridge_transport(msg(), NC)
    assert not r.ok and "no transport configured" in r.fatal and r.legs == {}
    monkeypatch.setattr(notify, "sh", FakeSh(cp(124)))
    assert notify.bridge_transport(msg(), NC).fatal == "bridge timed out"
    monkeypatch.setattr(notify, "sh", FakeSh(cp(1, "  sms FAILED: gateway\n  email sent to a@b.co (1 chars plain, no html)\nRESULT: delivered\n")))
    r = notify.bridge_transport(msg(), NC)
    assert r.legs == {"sms": "failed", "email": "sent"} and r.errors["sms"] == "gateway" and not r.ok   # rc!=0 stays a failure
    fake = FakeSh()
    monkeypatch.setattr(notify, "sh", fake)
    for bad in ({"bridge": "relative/path.py"}, {"handle": "x;y"}):
        nc = copy.deepcopy(NC)
        nc["transport"].update(bad)
        assert "invalid bridge" in notify.bridge_transport(msg(), nc).fatal
    assert fake.calls == []


def test_real_run_is_blocked_under_pytest_even_if_asked():
    r = notify._run(["echo", "hi"], 5, None, {})
    assert r.returncode == 126 and "blocked" in r.stderr


def run_child(payload, send_report) -> tuple[int, dict]:
    out = io.StringIO()
    rc = notify.child_main(io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)), out, send_report=send_report)
    return rc, json.loads(out.getvalue())


def test_child_main_calls_send_report_and_returns_only_reduced_results():
    seen = {}

    def fake_send_report(handle, sms, subject, plain, html=None, channels=None):
        seen.update(handle=handle, sms=sms, subject=subject, plain=plain, html=html, channels=channels)
        return True, ["sms sent to +14165550199 (SM1) body='homelab: CRIT disk / 4%'", "email sent to owner@example.com (9 chars plain, 99 html)"]
    rc, res = run_child({"handle": "ohmz", "sms": "S", "subject": "SUB", "plain": "P", "html": "<h>", "channels": ["sms", "email", "pigeon"]}, fake_send_report)
    assert rc == 0 and res == {"ok": True, "legs": {"sms": "sent", "email": "sent"}, "errors": {}, "fatal": ""}
    assert seen == {"handle": "ohmz", "sms": "S", "subject": "SUB", "plain": "P", "html": "<h>", "channels": ["sms", "email"]}
    assert "5550199" not in json.dumps(res) and "example.com" not in json.dumps(res)         # raw notes (numbers, addresses) are discarded
    rc, _ = run_child({"handle": "ohmz", "html": ""}, lambda *a, **k: (True, []))
    assert rc == 0


def test_child_main_reports_partial_failures_and_never_raises():
    rc, res = run_child({"handle": "ohmz", "channels": ["sms", "email"]},
                        lambda *a, **k: (True, ["sms FAILED: gateway 550", "email sent to a@b.co (1 chars plain, no html)"]))
    assert rc == 0 and res["legs"] == {"sms": "failed", "email": "sent"} and res["errors"]["sms"] == "gateway 550"
    rc, res = run_child({"handle": "ohmz", "channels": ["sms"]}, lambda *a, **k: (False, ["sms skipped: no phone for 'ohmz' in alert_contacts.json"]))
    assert rc == 1 and not res["ok"] and res["legs"] == {"sms": "skipped"} and "5550" not in json.dumps(res)
    rc, res = run_child({"handle": "ohmz"}, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("smtp down for owner@example.com")))
    assert rc == 1 and not res["ok"] and "RuntimeError" in res["fatal"] and "owner@example.com" not in res["fatal"]
    rc, res = run_child("{ not json", lambda *a, **k: (True, []))
    assert rc == 1 and not res["ok"] and res["fatal"]
    rc, res = run_child({"handle": "ohmz"}, lambda *a, **k: (False, ["no transport configured (~/.hermes/alert_transports.env missing)"]))
    assert "no transport configured" in res["fatal"]


NOTES = [
    ("sms sent to +14165550199 (SM1a2b3c) body='x'", {"sms": "sent"}, {}),
    ("email sent to owner@example.com (10 chars plain, 20 html)", {"email": "sent"}, {}),
    ("email sent to owner@example.com (10 chars plain, no html) — ⚠️ UNVERIFIABLE: no MX; mail would fall back", {"email": "sent"}, {"email": "unverifiable"}),
    ("sms FAILED: 535 auth for +14165550199", {"sms": "failed"}, {"sms": "535 auth for <phone>"}),
    ("email FAILED: server refused recipient(s): owner@example.com", {"email": "failed"}, {"email": "server refused recipient(s): <addr>"}),
    ("email NOT SENT to owner@example.com: domain has no MX and no A record", {"email": "failed"}, {"email": "domain has no MX and no A record"}),
    ("sms skipped: no phone for 'ohmz' in alert_contacts.json", {"sms": "skipped"}, {"sms": "no phone for 'ohmz' in alert_contacts.json"}),
    ("email not requested for this report", {"email": "not-requested"}, {}),
]


@pytest.mark.parametrize("note,legs,errors_has", NOTES)
def test_reduce_notes_table(note, legs, errors_has):
    r = notify.reduce_notes([note], ())
    assert r["legs"] == legs
    for k, frag in errors_has.items():
        assert frag in r["errors"][k] or frag == "unverifiable" and "unverifiable" in r["errors"][k]
    blob = json.dumps(r)
    assert "owner@example.com" not in blob and "4165550199" not in blob


def test_reduce_notes_channels_not_enabled_and_unknown_legs():
    r = notify.reduce_notes(["sms+email not enabled in ALERT_CHANNELS; sending by email instead", "email sent to a@b.co (1 chars plain, no html)"], ["sms", "email"])
    assert r["ok"] and r["legs"] == {"email": "sent", "sms": "skipped"} and "ALERT_CHANNELS" in r["errors"]["sms"]
    r = notify.reduce_notes(["something unexpected"], ["sms"])
    assert not r["ok"] and r["legs"] == {"sms": "unknown"}
    r = notify.reduce_notes(["no transport configured (~/.hermes/alert_transports.env missing)"], ["sms"])
    assert r["fatal"].startswith("no transport configured")
    assert notify.reduce_notes(None, None) == {"ok": False, "legs": {}, "errors": {}, "fatal": ""}


# =========================================================================== delivery log: no bodies, no addresses, no phones
ALLOWED_LOG_KEYS = {"ts", "kind", "severity", "title", "channels", "ok", "note", "dedupe_key", "legs", "skipped"}


def test_log_record_shape_and_redaction():
    leaky = "call +1 416 555 0199 or mail owner@example.com password=hunter2 https://h.example/p?token=abc"
    fk = Fake(legs={"sms": "failed", "email": "sent"}, errors={"sms": f"gateway said {leaky}"})
    d, _ = go(mk(title="Backup " + leaky, summary="BODY-MARKER-SUMMARY " + leaky, details={"text": "BODY-MARKER-DETAIL", "log": ["BODY-MARKER-LOG"]},
                 dedupe_key="k " + leaky), transport=fk)
    raw = (core.STATE_DIR / "notifications.jsonl").read_text()
    for leak in ("416 555 0199", "owner@example.com", "hunter2", "token=abc", "BODY-MARKER", "SECRET"):
        assert leak not in raw, leak
    rec = log_rows()[-1]
    assert set(rec) <= ALLOWED_LOG_KEYS and {"ts", "kind", "severity", "title", "channels", "ok", "note", "dedupe_key"} <= set(rec)
    assert rec["kind"] == "alert" and rec["severity"] == "crit" and rec["channels"] == ["email"] and rec["ok"] is True
    assert isinstance(rec["ts"], float) and len(rec["title"]) <= 100 and len(rec["note"]) <= 160 and "\n" not in raw.strip()
    assert "<addr>" in rec["title"] and "<phone>" in rec["title"]


@pytest.mark.parametrize("text,gone", [
    ("mail owner@example.com now", "owner@example.com"), ("tel +1 (416) 555-0199 ok", "555-0199"), ("tel 416-555-0199", "555-0199"),
    ("tel 4165550199", "4165550199"), ("tel +14165550199", "4165550199"), ("https://u:pw@h.example/a?k=v#f", "pw@"), ("https://h.example/a?k=v", "k=v"),
    ("token=abcdef123456", "abcdef123456"), ("Bearer abcdefgh12345678", "abcdefgh12345678"), ("0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
    ("A" * 40, "A" * 40), ("multi\nline\ttext", "\n")])
def test_redact_log_removes_what_must_not_be_logged(text, gone):
    assert gone not in notify.redact_log(text)


def test_redact_log_keeps_ordinary_text_and_clips():
    assert notify.redact_log("Disk space: / is 4% free, 11.2 GiB left (rc=1)") == "Disk space: / is 4% free, 11.2 GiB left (rc=1)"
    assert notify.redact_log(None) == "" and len(notify.redact_log("x " * 500, 50)) <= 50 and notify.redact_log("x " * 500, 50).endswith("...")


def test_skipped_and_failed_deliveries_are_logged_too():
    go(mk(), now=NOON)
    go(mk(), now=NOON + 60)                                                 # dedupe
    go(mk("maintenance", "ok"), cfgx(routes={"maintenance": "none"}))       # route none
    go(mk(task="f"), transport=Fake(legs={"sms": "failed", "email": "failed"}))   # failure
    rows = log_rows()
    assert [r.get("skipped") for r in rows] == [None, "dedupe", "route-none", None]
    assert [r["ok"] for r in rows] == [True, True, True, False]


def test_log_rotation_keeps_recent_rows_within_the_caps():
    cfg = cfgx(log={"max_bytes": 3000, "max_lines": 10, "keep_days": 1}, budget={"per_day": {"maintenance": 9999}, "total_per_day": 9999, "hard_cap_per_day": 9999})
    now = time.time()
    for i in range(80):
        notify.send(mk("maintenance", "ok", task=f"t{i}", title=f"T{i}"), cfg, now + i, transport=Fake())
    rows = log_rows()
    assert len(rows) <= 80 and rows[-1]["title"] == "T79"
    assert (core.STATE_DIR / "notifications.jsonl").stat().st_size < 3000 + 600           # trimmed back under the cap each time it overflowed


def test_audit_records_keep_the_shape_alert_path_health_reads():
    go(mk(), transport=Fake())
    go(mk(task="z"), transport=Fake(legs={"sms": "failed", "email": "failed"}, errors={"sms": "x"}, rc=3), now=NOON + 5)
    sends = [r for r in audit_rows() if r["task"] == "notify" and r["action"] == "send"]
    assert sends[0]["outcome"] == "sent" and sends[1]["outcome"].startswith("failed rc=") and sends[0]["bytes"] == 0
    assert "alert: Disk space" in sends[0]["target"]


# =========================================================================== export for the website
def write_log(rows, extra_lines=()):
    p = core.STATE_DIR / "notifications.jsonl"
    p.write_text("\n".join([*(json.dumps(r) for r in rows), *extra_lines]) + "\n")


def test_export_shape_counts_failures_and_ordering():
    now = NOON
    rows = [{"ts": now - 8 * D, "kind": "alert", "severity": "crit", "title": "old", "channels": ["sms", "email"], "ok": True, "note": "", "dedupe_key": "a"},
            {"ts": now - 2 * D, "kind": "alert", "severity": "crit", "title": "mid", "channels": ["email"], "ok": True, "note": "", "dedupe_key": "a"},
            {"ts": now - 3 * H, "kind": "maintenance", "severity": "ok", "title": "m", "channels": ["email"], "ok": True, "note": "", "dedupe_key": "m"},
            {"ts": now - 2 * H, "kind": "alert", "severity": "warn", "title": "fail", "channels": [], "ok": False, "note": "sms failed", "dedupe_key": "f"},
            {"ts": now - H, "kind": "alert", "severity": "warn", "title": "dup", "channels": [], "ok": True, "note": "", "dedupe_key": "f", "skipped": "dedupe"},
            {"ts": now - 600, "kind": "recovery", "severity": "ok", "title": "r", "channels": ["sms", "email"], "ok": True, "note": "", "dedupe_key": "a"}]
    write_log(rows)
    ex = notify.export(now)
    assert ex["schema"] == 1 and ex["generated_at"] == now
    assert [r["title"] for r in ex["recent"]] == ["r", "dup", "fail", "m", "mid", "old"]          # newest first
    assert ex["counts"]["24h"] == {"total": 4, "sent": 2, "failed": 1, "skipped": 1, "suppressed": 0, "sms": 1, "email": 2}
    assert ex["counts"]["7d"]["total"] == 5 and ex["counts"]["7d"]["sms"] == 1
    assert ex["failures"]["24h"] == 1 and ex["failures"]["7d"] == 1 and ex["failures"]["last"]["title"] == "fail"
    assert ex["by_kind_24h"] == {"maintenance": 1, "recovery": 1}
    assert set(ex["recent"][0]) <= {"ts", "kind", "severity", "title", "ok", "channels", "note", "skipped"}
    json.dumps(ex)


def test_export_limits_to_100_survives_garbage_and_redacts_again():
    now = NOON
    rows = [{"ts": now - i, "kind": "maintenance", "severity": "ok", "title": f"t{i}", "channels": ["email"], "ok": True, "note": "", "dedupe_key": "k"} for i in range(150)]
    rows.append({"ts": now - 5, "kind": "alert owner@example.com", "severity": "crit", "title": "mail owner@example.com", "channels": ["sms", "pigeon"], "ok": False,
                 "note": "tel +14165550199 token=abc123xyz", "dedupe_key": "k", "legs": {"sms": "failed"}})
    rows.append({"ts": now + 99999, "kind": "alert", "title": "future", "ok": True})       # clock skew: ignored
    write_log(rows, extra_lines=["{ truncated", "", "[1,2]", '{"ts": "x"}', "null"])
    ex = notify.export(now)
    assert len(ex["recent"]) == 100
    blob = json.dumps(ex)
    for leak in ("owner@example.com", "4165550199", "abc123xyz", "future"):
        assert leak not in blob
    bad = next(r for r in ex["recent"] if r["kind"].startswith("alert"))
    assert bad["kind"] == "alert <addr>" and bad["channels"] == ["sms"] and bad["ok"] is False
    assert ex["failures"]["24h"] == 1


def test_export_of_a_missing_or_empty_log_is_valid():
    ex = notify.export(NOON)
    assert ex["recent"] == [] and ex["counts"]["24h"]["total"] == 0 and ex["failures"] == {"24h": 0, "7d": 0, "last": None} and ex["by_kind_24h"] == {}
    (core.STATE_DIR / "notifications.jsonl").write_text("")
    assert notify.export(NOON)["recent"] == []


def test_export_matches_what_send_logged():
    go(mk("alert", "crit"), transport=Fake(legs={"sms": "failed", "email": "sent"}, errors={"sms": "x"}))
    go(mk("maintenance", "ok", task="m"))
    ex = notify.export(NOON)
    assert [r["kind"] for r in ex["recent"]] == ["maintenance", "alert"] and ex["failures"]["24h"] == 1       # a failed leg is surfaced
    assert ex["counts"]["24h"]["sent"] == 2


# =========================================================================== notify-test and the CLI
def test_send_test_covers_every_kind_labelled_and_routed_like_the_real_thing():
    fk = Fake()
    out = notify.send_test(None, cfgx(), NOON, transport=fk)
    assert len(out) == len(notify.sample_events()) == 10 and all(d.ok for d in out) and all(d.kind == "test" for d in out)
    assert len(fk.calls) == 10 and len({m.subject for m in fk.calls}) == 10
    assert all("TEST" in m.subject and "TEST MESSAGE" in m.plain and "Test message" in m.html for m in fk.calls)
    assert all(re.search(r"\bTEST\b", m.sms) and m.sms.endswith("ignore, notification test") for m in fk.calls)
    chans = {m.subject.split("TEST ")[1].split(":")[0]: set(m.channels) for m in fk.calls if "TEST alert" not in m.subject}
    assert chans == {"recovery": {"sms", "email"}, "maintenance": {"email"}, "digest daily": {"email"}, "report weekly": {"email"},
                     "incident open": {"sms", "email"}, "incident resolved": {"sms", "email"}, "ack expired": {"email"}}
    alerts = sorted(tuple(m.channels) for m in fk.calls if "TEST alert" in m.subject)
    assert alerts == [("email",), ("sms", "email")]                                     # alert.warn is email-only, alert.crit texts


def test_send_test_filters_by_kind_and_severity_and_flags_unknown_kinds():
    fk = Fake()
    out = notify.send_test(["alert"], cfgx(), NOON, transport=fk)
    assert len(out) == 2 and {tuple(m.channels) for m in fk.calls} == {("sms", "email"), ("email",)}       # alert.crit and alert.warn
    out = notify.send_test(["recovery", "bogus", "recovery"], cfgx(), NOON, transport=Fake())
    assert len(out) == 2 and sum(d.ok for d in out) == 1 and any("unknown kind 'bogus'" in d.note for d in out)


def test_a_test_never_touches_dedupe_or_escalation_state_and_ignores_quiet_hours():
    for _ in range(3):
        notify.send_test(["alert.warn"], cfgx(), at(2), transport=(fk := Fake()))
        assert fk.calls[0].channels == ["email"]                          # never escalated by repetition
    st = state()
    assert st["esc"] == {} and not [k for k in st["dedupe"] if not k.startswith("test|")]
    notify.send_test(["recovery"], cfgx(), at(2), transport=(fk := Fake()))
    assert set(fk.calls[0].channels) == {"sms", "email"}                  # quiet hours do not hide the test


def test_test_deliveries_use_the_real_routes_but_a_test_route_can_override():
    fk = Fake()
    notify.send_test(["alert.crit"], cfgx(routes={"test": "email"}), NOON, transport=fk)
    assert fk.calls[0].channels == ["email"]


def test_cli_route_render_export_and_dry_run(tmp_path, capsys):
    (core.CONF_DIR / "notify.toml").write_text('[quiet_hours]\ntz = "America/Toronto"\n[site]\nhost_label = "testhost"\n')
    assert notify.main(["route", "--now", str(NOON)]) == 0
    out = capsys.readouterr().out
    assert "quiet hours" not in out
    assert "alert.crit" in out and "sms+email" in out.split("alert.crit", 1)[1].splitlines()[0]
    assert "alert.warn" in out and "-> email" in out.split("alert.warn", 1)[1].splitlines()[0]
    assert "recovery" in out and "sms+email" in out.split("recovery", 1)[1].splitlines()[0]
    assert notify.main(["route", "digest_daily", "--now", str(NOON)]) == 0 and capsys.readouterr().out.count("\n") == 1
    assert notify.main(["route", "recovery", "--now", str(at(2))]) == 0                       # the same question at 02:00
    out = capsys.readouterr().out
    assert "quiet hours" in out and "recovery" in out.splitlines()[1] and "-> email  (" in out.splitlines()[1]
    d = tmp_path / "previews"
    assert notify.main(["render", "--out", str(d)]) == 0
    files = sorted(p.name for p in d.iterdir())
    assert len(files) == 20 and "alert.crit.html" in files and "ack_expired.failing.html" in files and "report_weekly.txt" in files
    for p in d.glob("*.html"):
        audit_html(p.read_text())
    assert notify.main(["test", "--dry-run", "alert.crit"]) == 0
    out = capsys.readouterr().out
    assert "subject: [homelab] TEST alert" in out and "sms (" in out and not (core.STATE_DIR / "notifications.jsonl").exists()
    assert notify.main(["export"]) == 0 and json.loads(capsys.readouterr().out)["schema"] == 1
    assert notify.main([]) == 2 and notify.main(["nonsense"]) == 2
    assert notify.main(["test", "alert.crit"]) == 1                                  # the real transport is refused under pytest: exit 1
    assert "blocked under pytest" in capsys.readouterr().out


# =========================================================================== helpers other modules call
def test_alert_and_recovery_events_carry_what_core_notifier_knows():
    e = notify.alert_event("disk_forecast", "Disk space", "error", "/ is 4% free", playbook=["a", "b"], facts={"Mount": "/"})
    assert (e.kind, e.severity, e.task, e.dedupe_key, e.status) == ("alert", "crit", "disk_forecast", "disk_forecast", "error")
    assert e.details == {"todo": ["a", "b"]} and e.facts == {"Mount": "/"}
    assert notify.alert_event("t", "T", "warn", "s").severity == "warn" and notify.alert_event("t", "T", "info", "s").severity == "warn"
    r = notify.recovery_event("t", "T", was="crit")
    assert (r.kind, r.severity, r.facts, r.summary) == ("recovery", "ok", {"was": "crit"}, "T is back to normal")


def test_maintenance_event_formats_freed_bytes_and_significance():
    e = notify.maintenance_event("docker_cache", "Build cache pruned", "21.2 GiB freed", done=["pruned"], freed_bytes=int(21.2 * 2**30), significant=True)
    assert e.kind == "maintenance" and e.facts["Freed"] == "21.2 GiB" and e.facts["significant"] is True and e.details == {"done": ["pruned"]}
    d, fk = go(e)
    assert set(fk.calls[0].channels) == {"sms", "email"} and "21.2 GiB" in fk.calls[0].plain and "- pruned" in fk.calls[0].plain
    d2, fk2 = go(notify.maintenance_event("snap", "Snap cleaned", "ok"))
    assert fk2.calls[0].channels == ["email"]


def test_notifier_send_adapter_semantics(monkeypatch):
    cfg = cfgx()
    assert notify.notifier_send(cfg, "t", "Title", "crit", "sum", NOON) is False         # the real transport is refused under pytest: not delivered
    (core.STATE_DIR / "notify-state.json").unlink(missing_ok=True)                       # (that failure opened the transport breaker)
    fk = Fake()
    orig = notify.send
    monkeypatch.setattr(notify, "send", lambda ev, cfg=None, now=None, **k: orig(ev, cfg, now, transport=fk, **k))
    # delivered or deliberately held by policy => True; failed or over budget => False (core.Notifier retries those next run)
    assert notify.notifier_send(cfg, "t2", "Title", "crit", "sum", NOON) is True
    assert notify.notifier_send(cfg, "t2", "Title", "crit", "sum", NOON + 1) is True      # deduped: handled, do not retry
    assert notify.notifier_send(cfg, "t2", "Title", "ok", "back", NOON + 2, recovery=True, was="crit") is True
    assert fk.calls[-1].subject.startswith("[homelab] OK:") and set(fk.calls[-1].channels) == {"sms", "email"}
    assert notify.notifier_send(cfgx(budget={"per_day": {"alert": 0}}), "t3", "T", "warn", "s", NOON) is False
    assert notify.notifier_send(cfgx(routes={"alert": {"warn": "none"}}), "t4", "T", "warn", "s", NOON) is True   # route none: handled


def test_digest_event_and_report_event_from_a_reports_document():
    e = notify.digest_event("digest_daily", "All clear", "quiet day", digest_text="homelab-maint daily 2026-10-01: health A (97).", tiles=[["A", "health", "ok"]],
                            link="#/health", period="2026-10-01")
    assert e.kind == "digest_daily" and e.dedupe_key == "digest_daily-2026-10-01" and e.details["text"].startswith("homelab-maint daily")
    doc = {"id": "2026-W40", "kind": "weekly", "headline": "Quiet week, one warning cleared", "digest_text": "homelab-maint weekly 2026-W40: health A (96).",
           "health": {"grade": "A", "score": 96}, "incidents": {"opened": 1, "open_now": 0}, "actions": {"freed_bytes": 3 * 2**30},
           "highlights": ["Disk steady", "No restarts"], "capacity": {"mounts": [{"mount": "/", "free_h": "86 GiB", "days_to_full": 38.2}],
                                                                       "recommendations": ["Archive the kometa tarball"]},
           "upcoming": [{"when": "Wed 07:45", "what": "weekly cleanup"}]}
    r = notify.report_event(doc)
    assert r.kind == "report_weekly" and r.severity == "ok" and r.facts["link"] == "#/reports/2026-W40" and r.dedupe_key == "report_weekly-2026-W40"
    assert [t[1] for t in r.facts["tiles"]] == ["health", "incidents", "open now", "freed"] and r.facts["tiles"][3][0] == "3.0 GiB"
    d, fk = go(r)
    m = fk.calls[0]
    assert d.ok and m.channels == ["email"] and "Disk steady" in m.plain and "86 GiB free, 38 days to full" in m.plain and "Archive the kometa tarball" in m.plain
    audit_html(m.html)
    for odd in ({}, None, {"kind": "daily"}, {"health": "x", "incidents": 3, "capacity": []}, {"id": "../../etc", "highlights": "no"}):
        ev = notify.report_event(odd)
        assert ev.kind in ("digest_daily", "report_weekly") and notify.send(ev, cfgx(), NOON, transport=Fake()).ok in (True, False)
    assert notify.report_event({"health": {"grade": "D", "score": 55}}).severity == "warn" and notify.report_event({"incidents": {"open_now": 2}}).severity == "warn"
    assert notify.report_event({"id": "../../etc"}).facts["link"] == "#/reports"                   # an odd id never becomes a path


# =========================================================================== the seam with Hermes (read-only checks)
def load_at():
    p = Path("/home/ohmz/StudioProjects/ai-stack/scripts/alert_transports.py")
    if not p.exists():
        pytest.skip("alert_transports.py not on this machine")
    spec = importlib.util.spec_from_file_location("at_signature_check", p)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)                       # defines functions only; nothing is called
    except Exception as exc:                               # noqa: BLE001
        pytest.skip(f"alert_transports not importable here: {exc}")
    return mod


def test_the_call_matches_hermes_send_report_signature():
    import inspect
    params = list(inspect.signature(load_at().send_report).parameters)
    assert params == ["handle", "sms", "subject", "plain", "html", "channels"], params


def test_child_default_path_imports_alert_transports_from_scripts_dir(tmp_path, monkeypatch):
    """child_main without an injected sender: sys.path gets scripts_dir, HOME is set for the account, send_report is called."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "alert_transports.py").write_text(
        "import os, json\n"
        "def send_report(handle, sms, subject, plain, html=None, channels=None):\n"
        "    with open(os.environ['STUB_OUT'], 'w') as f:\n"
        "        f.write(json.dumps([handle, sms, subject, plain, html, channels, os.environ.get('HOME')]))\n"
        "    return True, ['email sent to a@b.co (1 chars plain, no html)']\n")
    monkeypatch.setenv("STUB_OUT", str(tmp_path / "out.json"))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "alert_transports", raising=False)
    monkeypatch.setenv("HOME", "/nonexistent")
    out = io.StringIO()
    rc = notify.child_main(io.StringIO(json.dumps({"handle": "ohmz", "sms": "S", "subject": "SUB", "plain": "P", "html": "", "channels": ["email"],
                                                   "scripts_dir": str(scripts)})), out)
    sys.modules.pop("alert_transports", None)
    assert rc == 0 and json.loads(out.getvalue())["legs"] == {"email": "sent"}
    got = json.loads((tmp_path / "out.json").read_text())
    assert got[:6] == ["ohmz", "S", "SUB", "P", None, ["email"]] and got[6] != "/nonexistent"      # HOME was reset to the account's home


def test_child_entry_point_end_to_end_with_a_stub_transport_module(tmp_path, monkeypatch):
    """`python3 -m homelab_maint.notify --child` as a real process, with a stub alert_transports: JSON in, JSON out, exit code.
    The only test that starts a process, and the transport it loads is the stub written below: nothing can be sent."""
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "alert_transports.py").write_text(
        "def send_report(handle, sms, subject, plain, html=None, channels=None):\n"
        "    return True, ['sms sent to +14165550199 (X) body=1', 'email FAILED: smtp down for owner@example.com']\n")
    payload = json.dumps({"handle": "ohmz", "sms": "s", "subject": "x", "plain": "p", "html": "", "channels": ["sms", "email"], "scripts_dir": str(scripts)})
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    r = _REAL_RUN([sys.executable, "-m", "homelab_maint.notify", "--child"], input=payload, capture_output=True, text=True, env=env, timeout=30, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res["ok"] is True and res["legs"] == {"sms": "sent", "email": "failed"} and "owner@example.com" not in r.stdout and "5550199" not in r.stdout
    bad = _REAL_RUN([sys.executable, "-m", "homelab_maint.notify", "--child"], input="not json", capture_output=True, text=True, env=env, timeout=30, cwd=tmp_path)
    assert bad.returncode == 1 and json.loads(bad.stdout)["ok"] is False


def test_doctor_is_read_only_and_names_what_is_wrong(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "alert_transports.py").write_text("")
    bridge = tmp_path / "bridge.py"
    bridge.write_text("#!/bin/sh\n")
    bridge.chmod(0o755)
    cfg = cfgx(transport={"scripts_dir": str(scripts), "bridge": str(bridge), "user": "nosuchuser9", "handle": "nosuchuser9"})
    rows = {label: (ok, hint) for label, ok, hint in notify.doctor(cfg)}
    assert rows["Hermes alert_transports.py present"][0] and rows["legacy bridge executable"][0] and rows["notifications not muted"][0]
    assert rows["notify.toml readable and well formed"][0] and rows["transport user and handle valid"][0]
    assert not rows["Hermes transport config present (not read)"][0]                 # no such account: reported, not guessed
    assert rows["state dir writable (budgets, delivery log)"][0]
    (core.CONF_DIR / "NOTIFY_MUTE").write_text("")
    bad = {label: ok for label, ok, _h in notify.doctor(cfgx(transport={"kind": "none", "user": "x y"}))}
    assert not bad["notifications not muted"] and not bad["transport enabled"] and not bad["transport user and handle valid"]
    assert not (core.STATE_DIR / "notifications.jsonl").exists() and not (core.STATE_DIR / "notify-state.json").exists()   # it wrote nothing
    assert notify.main(["doctor"]) == 1                                              # CLI: non-zero when a check fails


# =========================================================================== `notify send` for shell hooks
def test_cli_send_builds_one_event_from_a_hook(tmp_path, capsys, monkeypatch):
    seen = []
    monkeypatch.setattr(notify, "send", lambda ev, *a, **k: seen.append((ev, k)) or notify.Delivery(kind=ev.kind, severity=ev.severity, ok=True, handled=True,
                                                                                                   channels=["sms", "email"], note="sms sent; email sent"))
    log = tmp_path / "x.log"
    log.write_text("\n".join(f"line {i}" for i in range(5000)))
    rc = notify.main(["send", "alert", "crit", "Backup failed", "system backup exited 1", "--task", "backup-system", "--key", "backup-system",
                      "--fact", "Exit code=1", "--fact", "Unit = backup-system.service", "--done", "rolled back", "--detail-file", str(log)])
    ev = seen[0][0]
    assert rc == 0 and (ev.kind, ev.severity, ev.title, ev.summary, ev.task, ev.dedupe_key) == ("alert", "crit", "Backup failed", "system backup exited 1", "backup-system", "backup-system")
    assert ev.facts == {"Exit code": "1", "Unit": "backup-system.service"} and ev.details["done"] == ["rolled back"]
    assert ev.details["log"].endswith("line 4999") and len(ev.details["log"]) <= 20_000          # the tail of the log, bounded
    assert "alert crit: sent via sms+email" in capsys.readouterr().out
    assert notify.main(["send", "recovery", "ok", "Backup ok"]) == 0 and seen[1][0].summary == "" and seen[1][0].details is None


def test_cli_send_exit_codes_and_validation(tmp_path, capsys, monkeypatch):
    assert notify.main(["send", "alert", "crit", "T", "S"]) == 0       # real transport refused under pytest: not sent, but a CRITICAL page is queued (exit 0)
    assert "queued for retry" in capsys.readouterr().out
    assert notify.main(["send", "alert", "warn", "T", "S", "--key", "w"]) == 1                      # a warning is not queued: not sent, exit 1
    assert "not sent" in capsys.readouterr().out
    seen = []
    with monkeypatch.context() as m:
        m.setattr(notify, "send", lambda ev, *a, **k: seen.append(ev) or notify.Delivery(kind=ev.kind, severity=ev.severity, ok=True, handled=True, channels=["email"]))
        assert notify.main(["send", "alert", "crit", "T", "S", "--detail-file", str(tmp_path / "missing.log")]) == 0       # fail OPEN: the page still goes
    assert seen[0].details["log"] == ["(log unavailable: No such file or directory)"]
    assert "sending without it" in capsys.readouterr().err
    for bad in (["send", "bogus", "crit", "T"], ["send", "alert", "loud", "T"], ["send", "test", "crit", "T"], ["send", "alert"],
                ["send", "alert", "crit", "T", "--fact", "no-equals-sign"], ["send", "alert", "crit", "T", "--fact", "=v"]):
        with pytest.raises(SystemExit) as e:
            notify.main(bad)
        assert e.value.code == 2
    monkeypatch.setattr(sys, "stdin", io.StringIO("from stdin"))
    before = len(log_rows())
    assert notify.main(["send", "maintenance", "ok", "T", "--detail-file", "-", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "subject: [homelab] Maintenance: T" in out and "sms (" in out and len(log_rows()) == before            # a dry run records nothing


# =========================================================================== review round 2: regression tests
# One section per reported issue. Each test fails on the code as it was before the fix.
import errno          # noqa: E402


def call_with_deadline(fn, seconds=5):
    """Run fn in a thread; a hang (a FIFO, a pipe nobody closes) fails the test instead of freezing the suite."""
    box: list = []
    th = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    th.start()
    th.join(seconds)
    assert box, f"call did not return within {seconds}s (hung)"
    return box[0]


# ---- issue 1: `notify send --detail-file` must fail OPEN and must read only the tail of a regular file
def test_a_missing_detail_file_still_sends_the_page_with_a_note(tmp_path, capsys):
    """The OnFailure case: the run died before it logged, so the log does not exist. The page must go anyway."""
    seen = []
    fk = Fake()
    orig = notify.send
    notify_send = lambda ev, *a, **k: (seen.append(ev), orig(ev, cfgx(), NOON, transport=fk))[1]      # noqa: E731
    with pytest.MonkeyPatch.context() as m:
        m.setattr(notify, "send", notify_send)
        rc = notify.main(["send", "alert", "crit", "Backup failed", "x", "--task", "t", "--detail-file", str(tmp_path / "backup-system.service.log")])
    assert rc == 0 and len(fk.calls) == 1 and set(fk.calls[0].channels) == {"sms", "email"}            # sms + email went out
    assert "(log unavailable: No such file or directory)" in fk.calls[0].plain and "(log unavailable: No such file or directory)" in fk.calls[0].html
    assert "sending without it" in capsys.readouterr().err


def test_detail_file_that_is_a_fifo_a_directory_or_a_device_is_refused_without_hanging(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    assert call_with_deadline(lambda: notify.read_tail(str(fifo))) == ("", "not a regular file")        # no writer: open() would block forever
    assert notify.read_tail(str(tmp_path)) == ("", "not a regular file")
    assert notify.read_tail("/dev/zero") == ("", "not a regular file")                                  # and it would never end
    text, why = notify.read_tail(str(tmp_path / "nope"))
    assert (text, why) == ("", "No such file or directory")
    if os.geteuid() != 0:
        locked = tmp_path / "locked.log"
        locked.write_text("x")
        locked.chmod(0)
        assert notify.read_tail(str(locked)) == ("", "Permission denied")


def test_detail_file_reads_only_the_tail_and_starts_on_a_line_boundary(tmp_path):
    big = tmp_path / "big.log"
    with open(big, "w") as f:
        for i in range(400_000):                                       # ~ 5 MB of lines
            f.write(f"line {i:06d} {'x' * 4}\n")
    got = []
    real_read = os.read
    with pytest.MonkeyPatch.context() as m:
        m.setattr(os, "read", lambda fd, n: got.append(real_read(fd, n)) or got[-1])
        text, why = notify.read_tail(str(big))
    assert big.stat().st_size > 5_000_000 and sum(map(len, got)) <= notify.DETAIL_BYTES      # only the tail was ever read, never the whole file
    assert why == "" and len(text) <= notify.DETAIL_BYTES
    rows = text.splitlines()
    assert rows[-1] == "line 399999 xxxx" and re.fullmatch(r"line \d{6} xxxx", rows[0]), rows[0]                # no cut fragment first
    small = tmp_path / "small.log"
    small.write_text("first\nsecond\n")
    assert notify.read_tail(str(small)) == ("first\nsecond\n", "")                                         # a short file is returned whole


def test_detail_from_stdin_takes_the_end_and_never_waits_for_a_writer_that_does_not_close(monkeypatch):
    r, w = os.pipe()
    payload = b"".join(f"row {i}\n".encode() for i in range(20_000))                     # > the 64 KB pipe buffer: write from a thread
    writer = threading.Thread(target=lambda: (os.write(w, payload), os.close(w)))
    writer.start()
    rf = os.fdopen(r)
    monkeypatch.setattr(sys, "stdin", rf)
    text, why = call_with_deadline(notify.read_stdin_tail)
    writer.join(5)
    rf.close()                                                                                    # (an unclosed pipe end is a ResourceWarning: -W error)
    assert why == "" and text.splitlines()[-1] == "row 19999" and len(text) <= notify.DETAIL_BYTES and text.startswith("row ")
    r, w = os.pipe()
    os.write(w, b"early\nlate\n")                                        # the writer keeps the pipe open (a hung command)
    rf = os.fdopen(r)
    monkeypatch.setattr(sys, "stdin", rf)
    t0 = time.monotonic()
    text, why = call_with_deadline(lambda: notify.read_stdin_tail(wait_s=0.3))
    os.close(w)
    rf.close()
    assert text == "early\nlate\n" and "did not close" in why and time.monotonic() - t0 < 3
    r, w = os.pipe()
    os.close(w)                                                            # an empty stream: no log, but still no failure
    rf = os.fdopen(r)
    monkeypatch.setattr(sys, "stdin", rf)
    assert notify.read_stdin_tail() == ("", "")
    rf.close()


def test_the_documented_hook_forms_work_end_to_end(tmp_path, monkeypatch, capsys):
    """`systemctl status ... | notify send ... --detail-file -` and `--detail-file FAILED-<unit>.txt` both carry the text to the email."""
    fk = Fake()
    orig = notify.send
    monkeypatch.setattr(notify, "send", lambda ev, *a, **k: orig(ev, cfgx(), NOON, transport=fk))
    r, w = os.pipe()
    os.write(w, b"* backup-system.service - failed\n   Active: failed (Result: oom-kill)\n")
    os.close(w)
    rf = os.fdopen(r)
    monkeypatch.setattr(sys, "stdin", rf)
    assert notify.main(["send", "alert", "crit", "Backup failed", "backup-system.service terminated abnormally", "--task", "backup-system",
                        "--key", "backup-failure-backup-system.service", "--fact", "Unit=backup-system.service", "--detail-file", "-"]) == 0
    rf.close()
    assert "Active: failed (Result: oom-kill)" in fk.calls[0].plain
    marker = tmp_path / "FAILED-backup-system.service.txt"
    marker.write_text("backup-system.service FAILED at 2026-09-19 10:02:11\n\n* backup-system.service\n   Active: failed (Result: signal)\n")
    assert notify.main(["send", "alert", "crit", "Backup failed", "x", "--key", "second", "--detail-file", str(marker)]) == 0
    assert "Active: failed (Result: signal)" in fk.calls[1].plain


# ---- issue 2: the excerpt is the LAST lines, not the first
def make_log(n=4000, last="BACKUP FAILED rc=23: rsync error: some files could not be transferred"):
    return "\n".join([f"rsync: sent file {i:05d} ok" for i in range(n)] + [last])


def test_log_excerpt_is_the_last_lines_of_the_log_in_every_rendering():
    text = make_log()
    assert len(text) > 90_000
    ev = mk(details={"log": text})
    m = T.prepare(ev, site={"host_label": "h"})
    assert len(m.log) == 40 and m.log[-1].startswith("BACKUP FAILED rc=23") and m.log[0] == "rsync: sent file 03961 ok"
    assert all(re.fullmatch(r"rsync: sent file \d{5} ok|BACKUP FAILED.*", x) for x in m.log), "a line was cut in the middle"
    assert "BACKUP FAILED rc=23" in T.build_plain(m) and "BACKUP FAILED rc=23" in T.build_html(m)
    assert "sent file 00000 ok" not in T.build_plain(m)                                              # the head of the log is not shown
    d, fk = go(ev)
    assert "BACKUP FAILED rc=23" in fk.calls[0].plain and "BACKUP FAILED rc=23" in fk.calls[0].html


def test_a_20kb_byte_window_that_starts_mid_line_never_shows_the_fragment(tmp_path):
    log = tmp_path / "system.log"
    log.write_text("".join(f"{i:06d} some ordinary progress line with several words in it\n" for i in range(5000)) + "===== finished: rc=0 =====\n")
    text, _why = notify.read_tail(str(log))
    m = T.prepare(mk(details={"log": text}))
    assert m.log[-1] == "===== finished: rc=0 =====" and all(re.match(r"\d{6} some ordinary", x) or x.startswith("=====") for x in m.log)
    raw = "x" * 70_000 + "frag-tail-start\nwhole line one\nlast line"                              # the window cut lands inside a very long line
    assert T.log_lines(raw) == ["whole line one", "last line"] and "frag" not in " ".join(T.log_lines(raw))


def test_log_lines_accepts_lists_bounds_the_work_and_never_returns_more_than_asked():
    assert T.log_lines([f"l{i}" for i in range(500)]) == [f"l{i}" for i in range(460, 500)]
    assert T.log_lines(["a", None, "", "  ", "b"]) == ["a", "b"] and T.log_lines(None) == [] and T.log_lines("") == []
    t0 = time.monotonic()
    assert T.log_lines("w\n" * 4_000_000 + "THE END")[-1] == "THE END" and time.monotonic() - t0 < 2      # 8 MB: only a window is ever looked at
    assert T.log_lines("w " * 4_000_000 + "THE END") and time.monotonic() - t0 < 4                         # one giant line: a fragment, not nothing, and still fast
    assert T.log_lines("a\n\n\nb\n" * 100, items=5) == ["b", "a", "b", "a", "b"]
    assert len(T.log_lines("\n".join(map(str, range(100))), items=7)) == 7


# ---- issue 3: the secret scrubber must catch the real variable names, flags and key blocks
HERMES_NAMES = ["SMTP_PASS", "SMTP_PASSWORD", "TWILIO_AUTH_TOKEN", "CANCEL_SECRET", "access_token", "refresh_token", "client_secret", "SECRET_KEY",
                "AWS_SECRET_ACCESS_KEY", "PGPASSWORD", "MYSQL_ROOT_PASSWORD", "api-key", "API_KEY", "private_key", "db.password", "x-fan-token",
                "OPENAI_API_KEY", "session_id", "cookie", "Authorization", "passwd", "passphrase", "pwd"]


@pytest.mark.parametrize("name", HERMES_NAMES)
@pytest.mark.parametrize("fmt", ["{k}=hunter2hunter2", "{k}: hunter2hunter2", "export {k}='hunter2hunter2' next", '{{"{k}": "hunter2hunter2"}}', "{k} = hunter2hunter2"])
def test_secret_values_after_any_identifier_containing_a_secret_word_are_removed(name, fmt):
    text = fmt.format(k=name)
    for out in (T.line(text), T.scrub_secrets(text), notify.redact_log(text), " ".join(T.log_lines(text)), " ".join(T.lines(text))):
        assert "hunter2hunter2" not in out, (text, out)
    assert name in T.line(text) or name.lower() in T.line(text).lower()                         # the key stays readable: only the value goes


@pytest.mark.parametrize("text,secret", [
    ("psql failed: curl --password hunter2 https://h/x", "hunter2"), ("run --token abc123def456 --verbose", "abc123def456"),
    ("mysql -u root -pSuperSecret1 mydb", "SuperSecret1"), ("mysqldump --opt -h db -pXyz12345 db > x.sql", "Xyz12345"),
    ("docker run -e MYSQL_ROOT_PASSWORD=rootpw99 img", "rootpw99"), ("postgres://app:s3cretpw@db:5432/x", "s3cretpw"),
    ("Authorization: Bearer abcdefghijkl", "abcdefghijkl"), ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
    ("token=ab,cd;ef", "ab"), ("password='two words here'", "words")])
def test_flags_glued_options_urls_and_quoted_values_are_scrubbed(text, secret):
    assert secret not in T.line(text) and secret not in notify.redact_log(text)


def test_private_key_blocks_are_removed_even_when_truncated_or_split_across_lines():
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\nQyNTUxOQAAACBhYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5egAAAAA=\n-----END OPENSSH PRIVATE KEY-----"
    out = T.log_lines("before\n" + pem + "\nafter")
    assert out == ["before", "<redacted private key>", "after"]
    assert T.log_lines("a\n-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\ncut off here") == ["a", "<redacted private key>"]
    body = T.log_lines("QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5YWJjZGVmZ2hpams=\n-----END PRIVATE KEY-----\nnext")
    assert body[0] == "<redacted blob>" and "QUJD" not in " ".join(body)                              # BEGIN fell off the window: the body still goes
    assert "BEGIN" not in T.line(pem) and "b3BlbnNz" not in T.line(pem) and "b3BlbnNz" not in " ".join(T.lines(pem))


def test_ordinary_words_that_contain_pass_or_token_are_left_alone():
    for ok in ("12 tests passed: 5 skipped: 1", "bypass: enabled", "compass: north", "Passed: 5 Failed: 0", "token_count 5", "secretary: Bob is away".replace("secretary", "agent")):
        assert T.line(ok) == ok, ok


def test_a_detail_file_that_is_an_env_dump_never_leaks_into_email_sms_or_log(tmp_path):
    env = tmp_path / "env.txt"
    env.write_text("SMTP_HOST=smtp.gmail.com\nSMTP_PASS=hunter2hunter2\nTWILIO_AUTH_TOKEN=abcdef0123456789\nCANCEL_SECRET=zzzsecretzzz\n"
                   "PGPASSWORD=pgpw12345\nconnect: mysql -u root -pRootPw99 db\n")
    text, _ = notify.read_tail(str(env))
    d, fk = go(mk(summary="config says SMTP_PASS=hunter2hunter2", details={"log": text}))
    m = fk.calls[0]
    blob = " ".join([m.sms, m.subject, m.plain, m.html]) + (core.STATE_DIR / "notifications.jsonl").read_text() + json.dumps(audit_rows())
    for leak in ("hunter2hunter2", "abcdef0123456789", "zzzsecretzzz", "pgpw12345", "RootPw99"):
        assert leak not in blob, leak
    assert "SMTP_PASS=&lt;redacted&gt;" in m.html and "SMTP_PASS=<redacted>" in m.plain and "SMTP_HOST=smtp.gmail.com" in m.plain


# ---- issue 4: a caller that sets only `status` must not be demoted to an info email
def test_event_severity_defaults_to_unset_so_status_decides():
    assert notify.Event("alert").severity == "" and notify.Event("alert", status="crit").severity == ""
    d, fk = go(notify.Event(kind="alert", status="crit", title="Disk space", task="disk_forecast"), now=at(3))        # 03:00, quiet hours
    assert d.severity == "crit" and set(fk.calls[0].channels) == {"sms", "email"}
    assert fk.calls[0].sms.startswith("homelab: CRIT Disk space")
    d, fk = go(notify.Event(kind="alert", status="warn", title="Services", task="failed_units"))
    assert d.severity == "warn" and fk.calls[0].channels == ["email"]
    d, fk = go(notify.Event(kind="alert", status="error", title="Boom", task="b"))
    assert d.severity == "crit" and set(fk.calls[0].channels) == {"sms", "email"}


def test_the_more_severe_of_severity_and_status_wins_and_unknown_words_are_not_guessed():
    assert T.norm_severity("info", "crit") == "crit" and T.norm_severity("crit", "ok") == "crit" and T.norm_severity("warn", "error") == "crit"
    assert T.norm_severity("", "warn") == "warn" and T.norm_severity(None, "skipped") == "info" and T.norm_severity("ok", None, "recovery") == "ok"
    assert T.norm_severity("ok", "ok", "recovery") == "ok" and T.norm_severity("bogus", "crit") == "info"
    assert T.norm_severity("warn", "bogus") == "warn" and T.norm_severity(None, "bogus", "recovery") == "ok"
    d, fk = go(notify.Event("alert", "info", "Odd", "x", status="crit", task="odd"))                                # both given, they disagree
    assert d.severity == "crit" and set(fk.calls[0].channels) == {"sms", "email"}
    assert T.prepare(notify.Event("alert", status="crit")).sev == "crit"                                           # the renderer agrees with the router


# ---- issue 5: half-delivered critical pages, and the meta-monitor
SMS_DOWN = dict(legs={"sms": "failed", "email": "sent"}, errors={"sms": "gateway refused"})


def crit_with_dead_sms(**kw):
    return go(mk("alert", "crit"), transport=Fake(**SMS_DOWN), **kw)


def test_a_critical_page_whose_text_failed_is_retried_for_the_text_only():
    d, fk = crit_with_dead_sms()
    assert d.ok and d.handled and d.channels == ["email"] and "the text will be retried" in d.note
    st = state()
    assert [(p["kind"], p["key"], p["n"]) for p in st["pending"]] == [("alert", "disk_forecast", 1)] and st["dedupe"]["alert|disk_forecast"]["sp"] == 1
    assert st["pending"][0]["sms"].startswith("homelab: CRIT Disk space")
    fk2 = Fake()
    assert notify.flush_pending(cfgx(), NOON + 60, transport=fk2) == [] and fk2.calls == []                   # too soon: sms_gap_s is 300
    out = notify.flush_pending(cfgx(), NOON + 301, transport=fk2)
    assert len(out) == 1 and out[0].ok and out[0].channels == ["sms"] and "sms retry sent" in out[0].note
    assert [c.channels for c in fk2.calls] == [["sms"]] and fk2.calls[0].html == "" and fk2.calls[0].sms == st["pending"][0]["sms"]   # NOT the email again
    st = state()
    assert "pending" not in st and "sp" not in st["dedupe"]["alert|disk_forecast"] and st["esc"]["disk_forecast"]["sms"] is True
    assert [r[2] for r in st["sent"]] == [0, 1] and log_rows()[-1]["legs"] == {"sms": "sent"} and log_rows()[-1]["channels"] == ["sms"]
    assert notify.flush_pending(cfgx(), NOON + 900, transport=fk2) == [] and len(fk2.calls) == 1                # done: nothing left to retry
    d2, fk3 = go(mk("recovery", "ok"), now=NOON + H)
    assert set(fk3.calls[0].channels) == {"sms", "email"}                                                          # the problem WAS texted in the end


def test_the_text_retry_gives_up_after_the_configured_attempts_and_a_failed_try_costs_no_budget():
    crit_with_dead_sms()
    dead = Fake(**SMS_DOWN)
    for i, t in enumerate((301, 602, 903, 1204), 1):
        notify.flush_pending(cfgx(), NOON + t, transport=dead)
    assert len(dead.calls) == 2, "3 attempts in all: the original plus two retries"
    st = state()
    assert "pending" not in st and "sp" not in st["dedupe"]["alert|disk_forecast"] and [r[2] for r in st["sent"]] == [0]
    assert any("giving up" in r["note"] for r in log_rows()) and sum(1 for r in log_rows() if r["note"].startswith("sms retry failed")) == 2
    assert [a["outcome"].startswith("failed") for a in audit_rows() if a["target"] == "alert: sms retry"] == [True, True]


def test_the_retry_window_expires_and_respects_the_sms_budget_and_the_hard_cap():
    crit_with_dead_sms()
    fk = Fake()
    assert notify.flush_pending(cfgx(), NOON + 3001, transport=fk) == [] and fk.calls == [] and "pending" not in state()       # sms_window_s
    cfg = cfgx(budget={"sms_per_day": 1, "crit_sms_reserve": 0})
    go(mk("alert", "crit", task="b1"), cfg, transport=Fake(**SMS_DOWN), now=NOON + 5000)
    go(mk("alert", "warn", task="w1", facts={"escalate": True}), cfg, now=NOON + 5001)                              # spends the one text of the day
    fk = Fake()
    assert notify.flush_pending(cfg, NOON + 5400, transport=fk) == [] and fk.calls == []                           # budget spent: no retry
    cfg2 = cfgx(budget={"hard_cap_per_day": 2})
    assert notify.flush_pending(cfg2, NOON + 5400, transport=fk) == [] and fk.calls == []                          # hard cap reached (2 sent today)


def test_a_half_delivered_page_does_not_cover_a_later_incident_for_the_same_problem():
    crit_with_dead_sms()
    d, fk = go(mk("incident_open", "crit"), now=NOON + 120)
    assert d.ok and d.skipped == "" and set(fk.calls[0].channels) == {"sms", "email"}                                  # NOT "covered": the phone was never reached
    st = state()
    assert "pending" not in st and "sp" not in st["dedupe"]["alert|disk_forecast"]                                  # the text got through: nothing to retry
    fk2 = Fake()
    assert notify.flush_pending(cfgx(), NOON + 1000, transport=fk2) == [] and fk2.calls == []                       # and no second text later
    go(mk("alert", "crit", task="full"), now=NOON)                                                                  # contrast: a fully delivered page covers
    assert go(mk("incident_open", "crit", task="full"), now=NOON + 120)[0].skipped == "covered"


def test_a_later_send_retries_the_text_after_its_own_message_and_a_failed_leg_never_blocks_other_alerts():
    crit_with_dead_sms()
    fk = Fake()
    d, _ = go(mk("maintenance", "ok", task="m"), transport=fk, now=NOON + 400)
    assert d.ok and [c.channels for c in fk.calls] == [["email"], ["sms"]]                                           # the real message first, the retry after
    assert "pending" not in state()


def test_partial_delivery_rows_in_the_audit_are_what_alert_path_health_already_parses():
    from homelab_maint.tasks import checks_health
    crit_with_dead_sms()
    notify.flush_pending(cfgx(), NOON + 301, transport=Fake(**SMS_DOWN))
    lines = [ln for ln in (core.LOG_DIR / "audit.jsonl").read_text().splitlines()]
    kinds = [e[0] for e in checks_health._audit_notify_events(lines)]
    assert kinds == ["ok", "fail", "fail"]                                           # delivered, the dead sms leg, the failed retry
    rows = [json.loads(ln) for ln in lines]
    assert [(r["action"], r["target"]) for r in rows] == [("send", "alert: Disk space"), ("send", "alert: sms leg"), ("send", "alert: sms retry")]
    assert rows[1]["outcome"] == "failed rc=1 gateway refused" and rows[2]["outcome"].startswith("failed rc=1")


def test_leg_health_names_the_dead_channel_and_an_email_only_message_does_not_hide_it():
    assert notify.leg_health(NOON)["sms"] == {"ok": 0, "fails": 0, "last_ok": None, "last_fail": None, "broken": False}
    crit_with_dead_sms(now=NOON)
    go(mk("maintenance", "ok", task="m"), transport=Fake(legs={"email": "sent"}), now=NOON + 60)                       # a healthy email-only message
    lh = notify.leg_health(NOON + 120)
    assert lh["sms"]["broken"] is True and lh["sms"]["fails"] == 1 and lh["sms"]["last_fail"] == NOON
    assert lh["email"]["broken"] is False and lh["email"]["ok"] == 2 and lh["email"]["last_ok"] == NOON + 60
    notify.flush_pending(cfgx(), NOON + 400, transport=Fake())                                                            # the text finally lands
    assert notify.leg_health(NOON + 500)["sms"]["broken"] is False and notify.leg_health(NOON + 500)["sms"]["ok"] == 1
    assert notify.leg_health(NOON + 3 * D)["sms"]["fails"] == 0                                                           # outside the window
    ex = notify.export(NOON + 500)
    assert ex["legs"]["sms"]["ok"] == 1 and ex["failures"]["24h"] == 1                                                    # the website gets it too
    assert notify.main(["legs"]) == 0


def test_flush_cli_and_doctor_report_pending_texts(capsys):
    assert notify.main(["flush"]) == 0 and "nothing pending" in capsys.readouterr().out
    crit_with_dead_sms(now=time.time())
    rows = {label: (ok, hint) for label, ok, hint in notify.doctor(cfgx())}
    assert rows["no critical text waiting for a retry"][0] is False and "1 text(s)" in rows["no critical text waiting for a retry"][1]


# ---- issue 6: a recovery that could not be delivered must not be forgotten (core.Notifier clears `alerted` regardless)
class Recorder:
    """notify.send replacement for HermesNotifier tests: records events and fails or delivers on demand."""

    def __init__(self, up=True):
        self.up, self.events = up, []

    def __call__(self, ev, cfg=None, now=None, **k):
        self.events.append(ev)
        ok = self.up
        return notify.Delivery(kind=ev.kind, severity=ev.severity, ok=ok, handled=ok, note="" if ok else "smtp down")


def run_notifier(rec, status, t, name="disk_forecast", title="Disk space", summary="/ is 4% free", cfg=None):
    nt = notify.HermesNotifier(cfg or {"global": {}})
    nt.evaluate(name, title, core.Result(status, summary), t)
    nt.save()
    return json.loads((core.STATE_DIR / "alerts.json").read_text())["tasks"][name]


def test_core_notifier_alone_drops_a_failed_recovery_which_is_why_hermes_notifier_exists(monkeypatch):
    """Documents the reported defect in core.Notifier.evaluate (not owned here): the recovery send's result is ignored."""
    sent = []
    monkeypatch.setattr(core.Notifier, "_send", lambda self, name, subject, body, now: (sent.append(subject), False)[1] if subject.startswith("OK") else True)
    for i, status in enumerate(["crit", "crit", "ok", "ok", "ok", "ok"]):
        n = core.Notifier({"global": {}})
        n.evaluate("t", "T", core.Result(status, "x"), NOON + i * 900)
        n.save()
    assert sent == ["OK T: recovered"], "the failed recovery is attempted once and then never again"


def test_hermes_notifier_remembers_a_failed_recovery_and_retries_until_it_is_delivered(monkeypatch):
    rec = Recorder(up=True)
    monkeypatch.setattr(notify, "send", rec)
    t = NOON
    run_notifier(rec, "crit", t)
    st = run_notifier(rec, "crit", t + 900)                                          # confirmed on the second run: paged
    assert st["alerted"] == 2 and [e.kind for e in rec.events] == ["alert"] and rec.events[0].severity == "crit"
    rec.up = False                                                                    # the mail path is down at the moment the check recovers
    run_notifier(rec, "ok", t + 1800)
    st = run_notifier(rec, "ok", t + 2700)                                           # confirmed recovery: sent, fails
    assert [e.kind for e in rec.events] == ["alert", "recovery"] and st["alerted"] == 0
    assert st["recovery_pending"]["was"] == "crit" and st["recovery_pending"]["n"] == 1
    st = run_notifier(rec, "ok", t + 3600)                                           # next run: retried, still down
    assert [e.kind for e in rec.events][-1] == "recovery" and len(rec.events) == 3 and st["recovery_pending"]["n"] == 2
    rec.up = True
    st = run_notifier(rec, "ok", t + 4500)                                           # delivered
    assert len(rec.events) == 4 and "recovery_pending" not in st
    assert rec.events[-1].facts == {"was": "crit"} and rec.events[-1].summary == "/ is 4% free"
    run_notifier(rec, "ok", t + 5400)
    assert len(rec.events) == 4, "once delivered it is never sent again"


def test_a_recovery_that_is_never_delivered_expires_and_a_returning_problem_cancels_it(monkeypatch):
    rec = Recorder(up=True)
    monkeypatch.setattr(notify, "send", rec)
    for i in range(2):
        run_notifier(rec, "crit", NOON + i * 900, name="a")
    rec.up = False
    for i in range(2):
        st = run_notifier(rec, "ok", NOON + 1800 + i * 900, name="a")
    assert "recovery_pending" in st
    run_notifier(rec, "ok", NOON + 2700 + notify.RECOVERY_RETRY_S + 100, name="a")
    st = json.loads((core.STATE_DIR / "alerts.json").read_text())["tasks"]["a"]
    assert "recovery_pending" not in st and any(r["action"] == "recovery-expired" for r in audit_rows())
    rec.up = True
    for i in range(2):                                                                # a second problem is paged ...
        run_notifier(rec, "crit", NOON + 40 * H + i * 900, name="b")
    rec.up = False
    for i in range(2):                                                                # ... its recovery fails, then the problem returns
        st = run_notifier(rec, "ok", NOON + 41 * H + i * 900, name="b")
    assert "recovery_pending" in st
    rec.up = True
    n_before = len(rec.events)
    for i in range(2):
        st = run_notifier(rec, "crit", NOON + 42 * H + i * 900, name="b")
    assert "recovery_pending" not in st and st["alerted"] == 2
    assert [e.kind for e in rec.events[n_before:]] == ["alert"], "the stale recovery is not sent: the problem is back"


def test_hermes_notifier_keeps_the_alert_retry_the_warn_levels_and_the_flush(monkeypatch):
    rec = Recorder(up=False)
    monkeypatch.setattr(notify, "send", rec)
    run_notifier(rec, "warn", NOON)
    st = run_notifier(rec, "warn", NOON + 900)                                       # confirmed, but the page failed
    assert st["alerted"] == 0 and rec.events[-1].severity == "warn" and rec.events[-1].kind == "alert"
    rec.up = True
    st = run_notifier(rec, "warn", NOON + 1800)                                      # core retries a failed ALERT on the next run
    assert st["alerted"] == 1 and len(rec.events) == 2
    run_notifier(rec, "warn", NOON + 2700)
    assert len(rec.events) == 2                                                       # steady state: no repeat before the reminder
    flushed = []
    monkeypatch.setattr(notify, "flush_pending", lambda cfg=None, now=None, **k: flushed.append(now) or [])
    nt = notify.HermesNotifier({"global": {}})
    nt.evaluate("x", "X", core.Result("ok", ""), NOON)
    nt.evaluate("y", "Y", core.Result("ok", ""), NOON)
    assert flushed == [NOON]                                                          # once per run, before anything else


# ---- issue 7: the transport circuit breaker and the short fallback timeout
def timeout_transport(delay=0.0):
    return Fake(legs={}, ok=False, fatal="transport timed out", rc=124, delay=delay)


def test_one_dead_transport_costs_one_timeout_not_one_per_pending_alert():
    dead = timeout_transport(delay=0.15)
    t0 = time.monotonic()
    out = [notify.send(mk("alert", "crit", task=f"t{i}"), cfgx(), NOON + i, transport=dead) for i in range(5)]
    assert len(dead.calls) == 1 and time.monotonic() - t0 < 0.7, "only the first message waited for the transport"
    assert out[0].skipped == "" and not out[0].ok and "timed out" in out[0].note
    for d in out[1:]:
        assert (d.skipped, d.ok, d.handled) == ("breaker", False, False) and "circuit open" in d.note        # not handled: the caller retries later
    assert state()["breaker"]["until"] == NOON + 300 and state()["sent"] == [] and "pending" not in state()
    assert [r.get("skipped") for r in log_rows()].count("breaker") == 4          # logged once per (kind, key) per 15 min
    notify.send(mk("alert", "crit", task="t1"), cfgx(), NOON + 60, transport=dead)
    assert [r.get("skipped") for r in log_rows()].count("breaker") == 4          # a repeat inside 15 minutes is not logged again


def test_the_breaker_half_opens_after_its_window_and_closes_on_a_delivery():
    dead = timeout_transport()
    notify.send(mk(task="a"), cfgx(), NOON, transport=dead)
    assert notify.send(mk(task="b"), cfgx(), NOON + 299, transport=dead).skipped == "breaker" and len(dead.calls) == 1
    assert notify.send(mk(task="c"), cfgx(), NOON + 301, transport=dead).skipped == ""                    # half open: one probe goes out
    assert len(dead.calls) == 2 and state()["breaker"]["n"] == 2 and state()["breaker"]["until"] == NOON + 601     # still dead: closed again for 5 min
    ok = Fake()
    d = notify.send(mk(task="d"), cfgx(), NOON + 602, transport=ok)
    assert d.ok and "breaker" not in state()
    assert len(ok.calls) == 4 and "outbox" not in state()                       # d itself, then the pages a, b and c that waited for the transport


def test_only_transport_level_failures_open_the_breaker_not_channel_level_ones():
    both_failed = Fake(legs={"sms": "failed", "email": "failed"}, ok=False, fatal="x", rc=1)
    go(mk(task="a"), transport=both_failed)
    assert "breaker" not in state()
    d, fk = go(mk(task="b"), transport=Fake(), now=NOON + 1)                                           # retried at once, and delivered
    assert d.ok
    go(mk(task="c"), transport=Fake(legs={}, ok=False, fatal="child died", rc=1), now=NOON + 2)       # nothing reported at all
    assert state()["breaker"]["until"] == NOON + 2 + 300


def test_breaker_never_blocks_a_test_a_dry_run_or_a_policy_decision_and_ignores_a_stepped_back_clock():
    notify.send(mk(task="a"), cfgx(), NOON, transport=timeout_transport())
    t = notify.Event("test", "crit", "x", "y", facts={"as": "alert"}, dedupe_key="t1")
    fk = Fake()
    assert notify.send(t, cfgx(), NOON + 5, transport=fk).ok and len(fk.calls) == 1 and "breaker" not in state()       # a test probes, and its success closes it
    notify.send(mk(task="b"), cfgx(), NOON + 10, transport=timeout_transport())
    assert notify.send(mk(task="c"), cfgx(), NOON + 11, transport=fk, dry_run=True).skipped == "dry-run"
    (core.CONF_DIR / "NOTIFY_MUTE").write_text("")
    assert notify.send(mk("maintenance", "ok", task="m"), cfgx(), NOON + 12, transport=fk).skipped == "muted"          # policy answers first
    (core.CONF_DIR / "NOTIFY_MUTE").unlink()
    s = state()
    s["breaker"] = {"until": NOON + 10 * D, "n": 9, "why": "clock"}                                    # the clock was wrong when this was written
    (core.STATE_DIR / "notify-state.json").write_text(json.dumps(s))
    assert notify.send(mk(task="d"), cfgx(), NOON + 20, transport=Fake()).ok


def test_breaker_state_is_shared_between_processes_and_flush_honours_it():
    crit_with_dead_sms()
    notify.send(mk(task="x"), cfgx(), NOON + 400, transport=timeout_transport())                      # opens it (the earlier flush also failed to run)
    s = state()
    assert s["breaker"]["until"] > NOON + 400
    fk = Fake()
    assert notify.flush_pending(cfgx(), NOON + 450, transport=fk) == [] and fk.calls == []             # no point retrying a text through a dead transport
    assert notify.flush_pending(cfgx(), NOON + 701, transport=fk)[0].ok


def test_the_bridge_fallback_gets_a_much_shorter_timeout_than_the_primary_and_the_worst_case_fits_the_tick_unit():
    seen = {}

    class Rec(Fake):
        def __call__(self, msg, nc):
            seen[self.via] = nc["transport"]["timeout_s"]
            return super().__call__(msg, nc)
    d, _ = go(mk(), transport=Rec(legs={}, ok=False, fatal="child broke", rc=1, via="primary"), fallback=Rec(via="bridge"))
    assert d.ok and seen == {"primary": 90, "bridge": 20}
    nc = notify.load_config({})
    assert nc["transport"]["timeout_s"] + nc["transport"]["fallback_timeout_s"] < 180       # homelab-maint-tick.service TimeoutStartSec
    assert nc["transport"]["fallback_timeout_s"] * 3 < nc["transport"]["timeout_s"] * 2


# ---- issue 8: a state directory that cannot be written must not switch off dedupe, budgets and the log
def test_unwritable_state_dir_falls_back_to_the_run_dir_with_dedupe_budget_and_log():
    blocker = core.STATE_DIR.parent / "afile"
    blocker.write_text("x")
    core.STATE_DIR = blocker / "state"                               # mkdir under a regular file: impossible
    cfg = cfgx(budget={"hard_cap_per_day": 3})
    d1, f1 = go(mk(), cfg, now=NOON)
    d2, f2 = go(mk(), cfg, now=NOON + 900)                           # the 15-minute re-run of the same crit
    assert d1.ok and (d2.skipped, d2.handled) == ("dedupe", True) and f2.calls == [], "no longer 96 texts a day"
    assert (core.RUN_DIR / "notify-state.json").stat().st_mode & 0o777 == 0o600
    for i in range(3):
        go(mk(task=f"other{i}"), cfg, now=NOON + 1000 + i)
    d, fk = go(mk(task="one-too-many"), cfg, now=NOON + 2000)
    assert d.skipped == "budget" and "hard" in d.note and fk.calls == []                         # the hard cap works from the fallback state too
    assert len(log_rows_in(core.RUN_DIR)) >= 4 and notify.export(NOON + 2100)["counts"]["24h"]["total"] >= 4      # and the website sees the rows


def log_rows_in(d: Path) -> list[dict]:
    p = d / "notifications.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_a_full_disk_moves_the_state_to_the_run_dir_and_back_when_space_returns(monkeypatch):
    real = core.write_json_atomic

    def full(path, obj, mode=0o644):
        if str(path).startswith(str(core.STATE_DIR)):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(path, obj, mode)
    monkeypatch.setattr(core, "write_json_atomic", full)
    assert go(mk(), now=NOON)[0].ok
    d, fk = go(mk(), now=NOON + 900)
    assert d.skipped == "dedupe" and fk.calls == [] and (core.RUN_DIR / "notify-state.json").exists()
    assert not (core.STATE_DIR / "notify-state.json").exists()
    monkeypatch.setattr(core, "write_json_atomic", real)                                    # space is back
    d, fk = go(mk(), now=NOON + 1800)
    assert d.skipped == "dedupe" and fk.calls == []                                           # the newest state (the run dir's) was read
    assert go(mk(task="new"), now=NOON + 1900)[0].ok and (core.STATE_DIR / "notify-state.json").exists()
    d, fk = go(mk(), now=NOON + 2000)
    assert d.skipped == "dedupe", "and it still remembers the old problem once the primary is in use again"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_a_user_unit_without_write_access_uses_its_own_runtime_dir(tmp_path, monkeypatch):
    """stack-alert@.service runs as ohmz: /var/lib/homelab-maint and /run/homelab-maint are root-owned there."""
    for d in (core.STATE_DIR, core.RUN_DIR):
        d.chmod(0o555)
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
    try:
        assert go(mk())[0].ok
        d, fk = go(mk(), now=NOON + 60)
        assert d.skipped == "dedupe" and fk.calls == [] and (xdg / "homelab-maint" / "notify-state.json").exists()
        rows = {label: ok for label, ok, _h in notify.doctor(cfgx())}
        assert rows["state dir writable (budgets, delivery log)"] is False
    finally:
        for d in (core.STATE_DIR, core.RUN_DIR):
            d.chmod(0o755)


def test_no_writable_directory_at_all_still_sends_and_never_raises():
    blocker = core.STATE_DIR.parent / "afile"
    blocker.write_text("x")
    core.STATE_DIR = blocker / "a"
    core.RUN_DIR = blocker / "b"
    d, fk = go(mk())
    assert d.ok and len(fk.calls) == 1 and notify.export(NOON)["recent"] == [] and notify.leg_health(NOON)["sms"]["fails"] == 0


def test_a_flush_with_several_pending_texts_stops_at_the_first_dead_transport_and_leaves_the_rest_untouched():
    for t in ("a", "b", "c"):
        go(mk("alert", "crit", task=t), transport=Fake(**SMS_DOWN), now=NOON)
    assert [p["key"] for p in state()["pending"]] == ["a", "b", "c"]
    dead = timeout_transport(delay=0.1)
    out = notify.flush_pending(cfgx(), NOON + 301, transport=dead)
    assert len(dead.calls) == 1, "three retries must not mean three timeouts"
    assert len(out) == 1 and not out[0].ok
    st = state()
    assert [(p["key"], p["n"], "claim" in p) for p in st["pending"]] == [("a", 2, False), ("b", 1, False), ("c", 1, False)]
    assert [r[2] for r in st["sent"]] == [0, 0, 0] and st["breaker"]["until"] == NOON + 301 + 300           # nothing was spent, the transport is parked
    good = Fake()
    assert notify.flush_pending(cfgx(), NOON + 400, transport=good) == [] and good.calls == []              # breaker open
    out = notify.flush_pending(cfgx(), NOON + 700, transport=good)
    assert [d.dedupe_key for d in out] == ["a", "b", "c"] and all(d.ok for d in out) and "pending" not in state()


# =========================================================================== review round 3: regression tests
# One section per reported issue. Each test fails on the code as it was before the fix.
import fcntl          # noqa: E402


# ---- review issue 3: a one-shot hook (`notify send`) must not lose a critical page when the transport is down
def test_a_one_shot_critical_page_is_not_lost_when_the_transport_is_down_and_is_replayed_later(monkeypatch, capsys):
    """The reviewed scenario: the 03:29 check run times out (the breaker opens); at 03:31 stack-backup's OnFailure hook sends a
    CRIT. Before: exit 1 at once, nothing replayed. Now: queued durably, replayed (once) when the circuit closes."""
    dead = timeout_transport()
    notify.send(mk(task="check-run", title="Disk space"), cfgx(), NOON, transport=dead)              # the timeout that opens the breaker
    ev = mk(task="stack-backup", title="Backup failed", summary="stack_backup.sh exited 1",
            details={"log": ["rsync: connection reset by peer", "login password=hunter2-SECRET"], "todo": ["Run: systemctl status stack-backup"]},
            facts={"Unit": "stack-backup.service"})
    d = notify.send(ev, cfgx(), NOON + 120, transport=dead)
    assert (d.skipped, d.ok, d.handled, d.queued) == ("breaker", False, False, True) and len(dead.calls) == 1
    assert "queued for retry" in d.note and [e["key"] for e in state()["outbox"]] == ["check-run", "stack-backup"]
    # the real CLI form: exit 0 (the page is durable; a non-zero exit would leave the OnFailure unit failed for a transient outage)
    orig = notify.send
    monkeypatch.setattr(notify, "send", lambda e, *a, **k: orig(e, cfgx(), NOON + 130, transport=dead))
    assert notify.main(["send", "alert", "crit", "Stack alert", "hook two", "--task", "stack-alert", "--key", "hook2"]) == 0
    assert "queued for retry" in capsys.readouterr().out and [e["key"] for e in state()["outbox"]] == ["check-run", "stack-backup", "hook2"]
    monkeypatch.setattr(notify, "send", orig)
    good = Fake()
    out = notify.flush_pending(cfgx(), NOON + 400, transport=good)                                   # the circuit has closed (300 s)
    assert [x.dedupe_key for x in out] == ["check-run", "stack-backup", "hook2"] and all(x.ok for x in out) and "outbox" not in state()
    m = good.calls[1]
    assert "Backup failed" in m.subject and "rsync: connection reset by peer" in m.plain
    assert "Run: systemctl status stack-backup" in m.plain and "Unit: stack-backup.service" in m.plain
    assert "Delayed delivery: first attempt" in m.plain and "hunter2-SECRET" not in m.plain + m.html          # late, said so, still scrubbed
    assert m.channels == ["sms", "email"] and m.sms.startswith("homelab: CRIT Backup failed")
    assert notify.flush_pending(cfgx(), NOON + 900, transport=good) == [] and len(good.calls) == 3              # exactly once
    assert any(r["action"] == "send" and r["outcome"] == "sent" for r in audit_rows())


def test_a_failed_replay_backs_off_and_a_page_that_never_lands_expires_loudly():
    bad = Fake(legs={"sms": "failed", "email": "failed"}, errors={"sms": "a", "email": "b"})       # channel-level failure: no breaker
    d, _ = go(mk(), transport=bad, now=NOON)
    e = state()["outbox"][0]
    assert d.queued and (e["n"], e["last"], e["ts"]) == (1, NOON, NOON)
    assert notify.flush_pending(cfgx(), NOON + 100, transport=bad) == [] and len(bad.calls) == 1        # outbox_gap_s = 300
    out = notify.flush_pending(cfgx(), NOON + 301, transport=bad)
    assert len(out) == 1 and not out[0].ok and "replayed from the outbox" in out[0].note and state()["outbox"][0]["n"] == 2
    assert notify.flush_pending(cfgx(), NOON + 301 + 599, transport=bad) == []                         # the gap doubled: 600 s
    assert len(notify.flush_pending(cfgx(), NOON + 301 + 601, transport=bad)) == 1 and state()["outbox"][0]["n"] == 3
    assert state()["sent"] == [] and "esc" in state() and state()["dedupe"] == {}                         # failures spend nothing and claim nothing
    assert notify._gap_s(notify.load_config(cfgx()), 30) == 3600                                       # never more than an hour apart
    assert notify.flush_pending(cfgx(), NOON + 12 * H + 1, transport=bad) == [] and "outbox" not in state()
    assert log_rows()[-1]["skipped"] == "expired" and log_rows()[-1]["ok"] is False and "never delivered" in log_rows()[-1]["note"]
    assert any(r["action"] == "outbox-expired" and r["outcome"] == "dropped" for r in audit_rows())      # publish reads "dropped" as not-a-failure


@pytest.mark.parametrize("kind,sev,queued", [("alert", "crit", True), ("incident_open", "crit", True), ("alert", "warn", False),
                                             ("incident_open", "warn", False), ("recovery", "ok", False), ("incident_resolved", "ok", False),
                                             ("maintenance", "ok", False), ("digest_daily", "ok", False), ("test", "crit", False)])
def test_only_critical_alerts_and_incidents_are_queued(kind, sev, queued):
    bad = Fake(legs={"sms": "failed", "email": "failed"})
    d, _ = go(mk(kind, sev, task="k"), transport=bad)
    assert d.ok is False and d.queued is queued and bool(state().get("outbox")) is queued, d.note


def test_the_queued_copy_is_sanitised_and_bounded_and_the_state_file_stays_private():
    secret = "hunter2-SECRET"
    big = "\n".join(f"line {i} " + "x" * 150 for i in range(5000))
    ev = mk(summary=f"login failed password={secret}",
            details={"log": big + f"\ntoken={secret}", "text": ["p" * 280] * 30, "todo": [f"set api_key={secret}"],
                     "sections": [{"title": "S", "lines": ["l" * 230] * 30, "kv": [("k", "v" * 150)] * 20}] * 8},
            facts={"Cfg": f"api_key={secret}", "tiles": [["1", "a", "ok"]], "notified": "3", "link": "#/incidents"})
    go(ev, transport=timeout_transport())
    p = core.STATE_DIR / "notify-state.json"
    raw = p.read_text()
    e = state()["outbox"][0]
    assert secret not in raw and len(json.dumps(e["snap"])) <= notify.OUTBOX_ENTRY_BYTES and len(raw) < 40_000
    assert (p.stat().st_mode & 0o777) == 0o600
    good = Fake()
    notify.flush_pending(cfgx(), NOON + 400, transport=good)
    m = good.calls[0]
    assert "line 4999" in m.plain and "line 10 " not in m.plain and secret not in m.plain + m.html and "password=<redacted>" in m.plain
    assert "api_key=<redacted>" in m.plain and "https://maintainer.ohmzhomelab.ca/#/incidents" in m.html          # reserved facts keep their meaning


def test_replay_goes_through_the_same_policy_so_the_owner_is_never_paged_twice():
    dead = timeout_transport()
    go(mk(task="x"), transport=dead, now=NOON)                                                          # queued
    good = Fake()
    d = notify.send(mk(task="x"), cfgx(), NOON + 400, transport=good)                                   # core retries the very same alert next run
    assert d.ok and len(good.calls) == 1 and "outbox" not in state()
    assert not any(r.get("skipped") == "dedupe" for r in log_rows()), "the stale copy was dropped on delivery, not replayed into a dedupe skip"
    assert notify.flush_pending(cfgx(), NOON + 900, transport=good) == [] and len(good.calls) == 1
    go(mk(task="y"), transport=timeout_transport(), now=NOON + 1000)                                    # queued ...
    other = Fake()
    d = notify.send(mk("incident_open", "crit", task="y", dedupe_key="y"), cfgx(), NOON + 1400, transport=other)
    assert len(other.calls) == 1 and d.skipped == "covered", "the queued alert went first, the incident it covers was not sent as a second page"
    assert "outbox" not in state()


def test_older_pages_for_a_problem_are_delivered_before_its_recovery():
    go(mk(task="z"), transport=timeout_transport(), now=NOON)
    good = Fake()
    rec = mk("recovery", "ok", "Disk space", "back to normal", task="z", facts={"was": "crit"})
    d = notify.send(rec, cfgx(), NOON + 400, transport=good)
    assert d.ok and len(good.calls) == 2 and "CRIT" in good.calls[0].subject and "OK" in good.calls[1].subject
    assert good.calls[1].channels == ["sms", "email"], "the replayed page was texted, so its recovery is texted too"
    assert "outbox" not in state()


def test_enqueue_is_durable_fifo_and_a_flush_stops_at_the_first_dead_transport():
    for i, t in enumerate(("a", "b", "c")):
        assert notify.enqueue(mk(task=t), cfgx(), NOON + i) is True                                   # no network, no budget, no routing yet
    assert [e["key"] for e in state()["outbox"]] == ["a", "b", "c"] and state()["sent"] == []
    dead = timeout_transport(delay=0.05)
    out = notify.flush_pending(cfgx(), NOON + 10, transport=dead)
    assert len(dead.calls) == 1 and len(out) == 1, "three queued pages must not mean three timeouts"
    assert [(e["key"], e["n"], "claim" in e) for e in state()["outbox"]] == [("a", 1, False), ("b", 0, False), ("c", 0, False)]
    good = Fake()
    assert notify.flush_pending(cfgx(), NOON + 20, transport=good) == [] and good.calls == []           # circuit open
    out = notify.flush_pending(cfgx(), NOON + 400, transport=good)
    assert [d.dedupe_key for d in out] == ["a", "b", "c"] and all(d.ok for d in out) and "outbox" not in state()


def test_replaying_an_old_page_never_removes_a_newer_one_for_the_same_problem():
    """alert, recovery, then a NEW alert for the same key are queued (defer mode). The third was tried a moment ago, so it is not
    due when the first two go out: delivering the old alert must not take the new one with it."""
    for i, (kind, sev, summ) in enumerate((("alert", "crit", "first"), ("recovery", "ok", "fine"), ("alert", "crit", "again"))):
        assert notify.enqueue(mk(kind, sev, task="k", summary=summ, facts={"was": "crit"} if kind == "recovery" else None), cfgx(), NOON + i)
    st = state()
    assert [e["kind"] for e in st["outbox"]] == ["alert", "recovery", "alert"]
    st["outbox"][2]["n"], st["outbox"][2]["last"] = 1, NOON + 5                                          # tried at +5 s: backing off
    (core.STATE_DIR / "notify-state.json").write_text(json.dumps(st))
    good = Fake()
    out = notify.flush_pending(cfgx(), NOON + 10, transport=good)
    assert [d.kind for d in out] == ["alert", "recovery"] and [(e["kind"], e["snap"]["summary"]) for e in state()["outbox"]] == [("alert", "again")]
    out = notify.flush_pending(cfgx(), NOON + 400, transport=good)
    assert [d.kind for d in out] == ["alert"] and "outbox" not in state()
    assert [m.subject.split(":")[0] for m in good.calls] == ["[homelab] CRIT", "[homelab] OK", "[homelab] CRIT"] and "again" in good.calls[2].plain


def test_outbox_refreshes_in_place_only_for_the_last_entry_of_a_key_and_caps_its_size():
    cfg = cfgx(retry={"outbox_max": 3})
    notify.enqueue(mk(task="k", summary="v1"), cfg, NOON)
    notify.enqueue(mk(task="k", summary="v2"), cfg, NOON + 1)
    assert [(e["kind"], e["snap"]["summary"]) for e in state()["outbox"]] == [("alert", "v2")]
    notify.enqueue(mk("recovery", "ok", task="k"), cfg, NOON + 2)
    notify.enqueue(mk(task="k", summary="v3"), cfg, NOON + 3)                                           # a NEW problem after the recovery: its own slot
    assert [(e["kind"], e["snap"]["summary"]) for e in state()["outbox"]] == [("alert", "v2"), ("recovery", "/ is 4% free"), ("alert", "v3")]
    notify.enqueue(mk(task="n1"), cfg, NOON + 4)
    notify.enqueue(mk(task="n2"), cfg, NOON + 5)
    assert [e["key"] for e in state()["outbox"]] == ["k", "n1", "n2"] and state()["outbox"][0]["snap"]["summary"] == "v3"
    assert [r["action"] for r in audit_rows()].count("outbox-overflow") == 2


def test_a_claimed_entry_is_not_replayed_twice_and_a_claim_whose_sender_died_is_released():
    notify.enqueue(mk(task="c"), cfgx(), NOON)
    st = state()
    st["outbox"][0]["claim"], st["outbox"][0]["claim_ts"] = "abcd", NOON + 5                            # another process is sending it right now
    (core.STATE_DIR / "notify-state.json").write_text(json.dumps(st))
    good = Fake()
    assert notify.flush_pending(cfgx(), NOON + 10, transport=good) == [] and good.calls == []
    out = notify.flush_pending(cfgx(), NOON + 5 + notify.CLAIM_TTL_S + 10, transport=good)               # that sender is long dead
    assert len(out) == 1 and out[0].ok and "outbox" not in state()


def test_a_clock_that_stepped_back_does_not_expire_a_queued_page():
    notify.enqueue(mk(task="clk"), cfgx(), NOON + 3 * H)                                               # written when the clock said 15:00 ...
    good = Fake()
    out = notify.flush_pending(cfgx(), NOON, transport=good)                                           # ... and it is 12:00 again
    assert len(out) == 1 and out[0].ok and "outbox" not in state() and not any(r.get("skipped") == "expired" for r in log_rows())
    assert "Delayed delivery" not in good.calls[0].plain, "a page from the 'future' is not described as late"


def test_a_malformed_outbox_never_blocks_delivery_or_the_flush():
    st = {"v": 1, "sent": [], "dedupe": {}, "esc": {}, "outbox": ["junk", {"kind": 1}, {"kind": "alert", "key": "k", "snap": {"title": "t"}, "ts": "nan"}, None]}
    (core.STATE_DIR / "notify-state.json").write_text(json.dumps(st))
    assert notify.flush_pending(cfgx(), NOON, transport=Fake()) == []
    d, fk = go(mk(task="fresh"))
    assert d.ok and len(fk.calls) == 1 and "outbox" not in state()


def test_doctor_export_and_the_flush_cli_show_what_is_waiting(capsys, monkeypatch):
    go(mk(), transport=timeout_transport(), now=NOON)
    rows = {label: (ok, hint) for label, ok, hint in notify.doctor(cfgx())}
    ok, hint = rows["no critical page waiting in the outbox"]
    assert ok is False and "1 page(s) could not be delivered" in hint and "12 h" in hint
    assert notify.export(NOON)["waiting"] == {"pages": 1, "texts": 0}
    monkeypatch.setattr(notify, "flush_pending", lambda *a, **k: [notify.Delivery(kind="alert", severity="crit", ok=False, handled=True, skipped="dedupe", note="n")])
    assert notify.main(["flush"]) == 0                                                                  # held by policy is not a failure
    assert "1 queued message(s)" in capsys.readouterr().out
    monkeypatch.setattr(notify, "flush_pending", lambda *a, **k: [notify.Delivery(kind="alert", severity="crit", ok=False, note="down")])
    assert notify.main(["flush"]) == 1


def test_shipped_notify_toml_documents_the_outbox_knobs():
    r = tomllib.loads((ROOT / "etc" / "notify.toml").read_text())["retry"]
    assert (r["outbox_ttl_s"], r["outbox_gap_s"], r["outbox_max"]) == (43200, 300, 10)


# ---- review issue 2: the glue must keep core.evaluate's recovery safe, and no network work may happen under cli's state lock
class LockProbe(Fake):
    """A transport that records whether cli's state flock was FREE while it ran (it must be: delivery happens after the lock)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.lock_free: list[bool] = []

    def __call__(self, msg, nc):
        with open(core.STATE_DIR / "state.lock", "w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.lock_free.append(True)
            except OSError:
                self.lock_free.append(False)
        return super().__call__(msg, nc)


def run_deferred(status, t, *, name="disk_forecast", transport=None, deliver=True):
    """What cli.cmd_run should do: evaluate + save inside the state lock, deliver after it."""
    from homelab_maint import cli
    nt = notify.HermesNotifier(cfgx(), defer=True)
    with cli._state_lock():
        nt.evaluate(name, "Disk space", core.Result(status, "/ is 4% free"), t)
        nt.save()
    out = nt.deliver(t, transport=transport) if deliver else []
    return json.loads((core.STATE_DIR / "alerts.json").read_text())["tasks"][name], out


def test_deferred_notifier_does_no_network_inside_the_lock_and_delivers_after_it_is_released(monkeypatch):
    flushed = []
    real = notify.flush_pending
    monkeypatch.setattr(notify, "flush_pending", lambda *a, **k: flushed.append(1) or real(*a, **k))
    probe = LockProbe()
    slow = timeout_transport(delay=0.6)                                                                # a dead transport that would hold the lock for 0.6 s
    t0 = time.monotonic()
    run_deferred("crit", NOON, transport=slow, deliver=False)
    st, _ = run_deferred("crit", NOON + 900, transport=slow, deliver=False)                            # confirmed: the page is decided inside the lock
    assert time.monotonic() - t0 < 0.4 and slow.calls == [] and flushed == [], "evaluate must not touch the transport (or flush)"
    assert st["alerted"] == 2 and [e["kind"] for e in state()["outbox"]] == ["alert"]                    # core was told "sent": the queue is durable
    st, out = run_deferred("crit", NOON + 1800, transport=probe, deliver=True)                            # deliver() after the lock
    assert len(probe.calls) == 1 and probe.lock_free == [True] and out[0].ok and "CRIT" in probe.calls[0].subject
    assert flushed == [1] and "outbox" not in state()
    run_deferred("crit", NOON + 2700, transport=probe)
    assert len(probe.calls) == 1, "steady state: no repeat before the reminder"


def test_deferred_recovery_is_queued_in_order_and_survives_a_dead_mail_path():
    good, dead = Fake(), timeout_transport()
    run_deferred("crit", NOON, transport=good)
    run_deferred("crit", NOON + 900, transport=good)
    assert [m.subject.split(":")[0] for m in good.calls] == ["[homelab] CRIT"]
    run_deferred("ok", NOON + 1800, transport=dead)
    st, _ = run_deferred("ok", NOON + 2700, transport=dead)                                              # confirmed recovery, mail path down
    assert st["alerted"] == 0 and "recovery_pending" not in st, "the durable queue holds it, not the per-task retry"
    assert len(dead.calls) == 1 and [e["kind"] for e in state()["outbox"]] == ["recovery"]
    run_deferred("ok", NOON + 2900, transport=good)                                                      # still inside the 5 min circuit window
    assert len(good.calls) == 1
    run_deferred("ok", NOON + 3600, transport=good)                                                      # circuit closed: delivered
    assert [m.subject.split(":")[0] for m in good.calls] == ["[homelab] CRIT", "[homelab] OK"] and "outbox" not in state()
    run_deferred("ok", NOON + 4500, transport=good)
    assert len(good.calls) == 2, "once delivered it is never sent again"


def test_a_run_killed_before_deliver_loses_nothing():
    run_deferred("crit", NOON, deliver=False)
    run_deferred("crit", NOON + 900, deliver=False)                                                      # the process died here, before deliver()
    assert json.loads((core.STATE_DIR / "alerts.json").read_text())["tasks"]["disk_forecast"]["alerted"] == 2
    good = Fake()
    st, out = run_deferred("crit", NOON + 1800, transport=good)                                          # the next run's deliver() sends it
    assert len(good.calls) == 1 and "CRIT" in good.calls[0].subject and "Delayed delivery" in good.calls[0].plain
    assert notify.flush_pending(cfgx(), NOON + 1900, transport=good) == [] and len(good.calls) == 1


def test_a_deferred_notifier_that_cannot_store_the_page_leaves_the_retry_to_core(tmp_path):
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    core.STATE_DIR, core.RUN_DIR = blocker / "a", blocker / "b"                                           # no writable directory at all
    assert notify.enqueue(mk(), cfgx(), NOON) is False
    nt = notify.HermesNotifier(cfgx(), defer=True)
    assert nt._deliver("t", "T", "x", NOON, recovery=False, level=2) is False                           # so core does not mark it alerted: it retries next run


def test_inline_hermes_notifier_caps_the_transport_timeouts_but_a_plain_send_does_not(monkeypatch):
    seen = []

    class Spy(Fake):
        def __call__(self, msg, nc):
            seen.append((nc["transport"]["timeout_s"], nc["transport"]["fallback_timeout_s"]))
            return super().__call__(msg, nc)
    spy = Spy()
    monkeypatch.setattr(notify, "hermes_transport", spy)
    nt = notify.HermesNotifier(cfgx())                                                                   # defer is off by default: sends inline
    assert nt.defer is False and nt._deliver("t", "T", "x", NOON, recovery=False, level=2) is True
    assert seen == [(notify.INLOCK_TIMEOUT_S, notify.INLOCK_FALLBACK_S)] and notify.INLOCK_TIMEOUT_S + notify.INLOCK_FALLBACK_S < 90
    assert notify.send(mk(task="plain"), cfgx(), NOON + 1).ok and seen[-1] == (90, 20)                  # everything else keeps the full timeouts
    cfg = cfgx(transport={"timeout_s": 10, "fallback_timeout_s": 3})                                       # a smaller configured value is kept
    assert notify._capped(cfg)["transport"]["timeout_s"] == 10 and notify._capped(cfg)["transport"]["fallback_timeout_s"] == 3


# =========================================================================== acknowledged issues (SPEC5 S4 + S8)
# The acks module (homelab_maint/acks.py, ack_core's) is replaced by FakeAcks: the API of SPEC5 S7 in memory, so nothing here needs
# its files and a token is just a string the test can look for. Nothing can be sent: the transport is a Fake, a real one is refused
# under pytest, and subprocess is blocked for the whole file (see `env`).
class FpStr(str):
    """What acks.fingerprint returns: a 16-hex str that also says how it was made (`mode`: explicit | task | regex | text)."""
    mode = "regex"


class FakeAcks:
    def __init__(self):
        self.rules: dict[str, dict] = {}       # task -> rule: what etc/ack.toml [key.<task>] holds (notify asks load_config() for a "text" key)
        self.mode = "regex"                    # the mode every fingerprint is reported to have been made in (the new policy reads it)
        self.acks: dict[str, dict] = {}        # fp -> {"until": t, "sev": "warn"|"crit"}
        self.tokens: list[dict] = []           # every token handed out (the test knows the plaintext; nothing else should)
        self.suppressed: list[str] = []
        self.revoked: list[str] = []
        self.expired: list = []
        self.issue_hook = None                 # callable(**kw) -> token | raises
        self.is_acked_hook = None              # callable(fp, sev, now) -> anything | raises
        self.fingerprint_hook = None           # callable(task, res, sev) -> anything | raises

    def fingerprint(self, task, res, sev):
        if self.fingerprint_hook:
            out = self.fingerprint_hook(task, res, sev)
        else:
            text = res if isinstance(res, str) else getattr(res, "summary", "")
            out = hashlib.sha1(f"{task}|{re.sub(r'[0-9.]+', '#', str(text).lower()).strip()}".encode()).hexdigest()[:16]
        if type(out) is str:                                                   # (a hook may answer nonsense: that is passed through as it is)
            out = FpStr(out)
            out.mode = self.mode
        return out

    def load_config(self):
        return {"ack": {}, "inbox": {}, "key": dict(self.rules)}

    def is_acked(self, fp, sev, now):
        if self.is_acked_hook:
            return self.is_acked_hook(fp, sev, now)
        a = self.acks.get(fp)
        rank = {"warn": 1, "crit": 2}
        if not a or a["until"] <= now or rank[sev] > rank[a["sev"]]:           # the severity ceiling: a worse severity is not covered
            return None
        return types.SimpleNamespace(fp=fp, until=a["until"], severity=a["sev"])

    def record_suppressed(self, fp, now):
        self.suppressed.append(fp)

    def issue_token(self, fp, task, title, summary, severity, now, ttl_days=30, mode=None):
        kw = dict(fp=fp, task=task, title=title, summary=summary, severity=severity, now=now, ttl_days=ttl_days, mode=mode)
        tok = self.issue_hook(**kw) if self.issue_hook else secrets.token_urlsafe(32)
        self.tokens.append({**kw, "token": tok})
        return tok

    def revoke_token(self, token, now=None):
        self.revoked.append(token)
        return True

    def expire(self, now):
        out, self.expired = self.expired, []
        return out

    def ack(self, ev_or_fp, days=90, sev="warn", now=NOON):
        fp = ev_or_fp if isinstance(ev_or_fp, str) else self.fingerprint(ev_or_fp.task, ev_or_fp.summary, sev)
        self.acks[fp] = {"until": now + days * D, "sev": sev}
        return fp


@pytest.fixture
def acks(monkeypatch):
    fa = FakeAcks()
    monkeypatch.setattr(notify, "acks_loader", lambda: fa)
    return fa


def fp_of(fa: FakeAcks, ev: notify.Event) -> str:
    return fa.fingerprint(ev.task, ev.summary, "warn")


def disk_dump() -> str:
    """Every byte of every file the notification path may have written (state, log, run dirs), as text."""
    out = []
    for d in (core.STATE_DIR, core.LOG_DIR, core.RUN_DIR, core.CONF_DIR):
        for p in d.rglob("*"):
            if p.is_file():
                out.append(p.read_text(errors="replace"))
    return "\n".join(out)


BASE = "https://maintainer.ohmzhomelab.ca"


def luminance(h: str) -> float:
    r, g, b = (int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4          # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# ------------------------------------------------------------------ config
def test_shipped_ack_settings_agree_with_etc_ack_toml():
    mine = tomllib.loads((ROOT / "etc" / "notify.toml").read_text())["ack"]
    rule = tomllib.loads((ROOT / "etc" / "ack.toml").read_text())["ack"]
    assert mine["days"] == rule["days"] == 90 and mine["escalation_breaks"] == rule["escalation_breaks"]
    assert mine["token_ttl_days"] == rule["token_ttl_days"] == 30
    assert mine["base_url"] == "https://maintainer.ohmzhomelab.ca" and mine["button_text"] == "Acknowledge for {days} days"
    assert mine["days"] in T.ACK_DAYS_ALLOWED and notify.DEFAULTS["ack"]["days"] == mine["days"]
    assert tomllib.loads((ROOT / "etc" / "notify.toml").read_text())["routes"]["ack_expired"] == "email"       # the notice never texts


def test_ack_config_is_clamped_and_the_kind_lists_are_whitelisted():
    c = notify.ack_cfg(notify.load_config(cfgx(ack={"enabled": "false", "token_ttl_days": 9999, "days": 0, "base_url": "",
                                                    "suppress_kinds": ["alert", "maintenance", "digest_daily", "test", "ack_expired"],
                                                    "button_kinds": ["alert", "recovery", "maintenance"], "escalation_breaks": False})))
    assert c["enabled"] is False                                   # only a real `true` enables: a typo can only mean MORE alerts
    assert c["ttl"] == 90 and c["days"] == 1 and c["base"] == BASE and c["escalates"] is False
    assert c["suppress"] == ("alert",) and c["button"] == ("alert",)         # nothing outside the hard whitelists, ever


# ------------------------------------------------------------------ fail closed: no module, a broken module
def test_no_acks_module_means_alerts_go_out_exactly_as_before(monkeypatch):
    monkeypatch.setattr(notify, "acks_loader", lambda: None)
    d, fk = go(mk())
    m = fk.calls[0]
    assert d.ok and set(m.channels) == {"sms", "email"} and m.ack_url == ""
    for text in (m.plain, m.html, m.sms):
        assert "Acknowledge" not in text and "/ack?" not in text and "Issue ID" not in text
    assert "ackfp" not in state() and notify.notify_expired(NOON, cfgx()) == []


def test_the_real_loader_survives_a_missing_or_a_broken_acks_module(monkeypatch):
    import homelab_maint
    loader = _REAL_ACKS_LOADER                                     # the real one (`env` replaced it for this test)
    monkeypatch.delattr(homelab_maint, "acks", raising=False)      # `from . import acks` finds a package attribute before sys.modules
    monkeypatch.setitem(sys.modules, "homelab_maint.acks", None)
    assert loader() is None                                        # ImportError -> None
    monkeypatch.setitem(sys.modules, "homelab_maint.acks", types.ModuleType("homelab_maint.acks"))       # imports, but has no API
    assert loader() is not None
    monkeypatch.setattr(notify, "acks_loader", loader)
    d, fk = go(mk(), cfg=cfgx())                                   # AttributeError inside fingerprint/is_acked: fail closed
    assert d.ok and fk.calls[0].ack_url == "" and "Acknowledge" not in fk.calls[0].plain


# ------------------------------------------------------------------ the link and the button
@pytest.mark.parametrize("kind,sev,s", [("alert", "crit", "crit"), ("alert", "warn", "warn"), ("alert", "info", "warn"),
                                        ("incident_open", "crit", "crit"), ("incident_open", "warn", "warn")])
def test_alert_emails_carry_the_acknowledge_link_and_button(acks, kind, sev, s):
    ev = mk(kind, sev)
    d, fk = go(ev)
    m, fp, tok = fk.calls[0], fp_of(acks, ev), acks.tokens[-1]["token"]
    url = f"{BASE}/ack?id={fp}&t={tok}&d=90&s={s}"                 # SPEC5 S8: the issue id, the token, the days and the severity
    assert d.ok and len(acks.tokens) == 1 and len(tok) == 43 and m.ack_url == url
    t = acks.tokens[0]
    assert (t["fp"], t["task"], t["title"], t["summary"], t["severity"], t["now"], t["ttl_days"]) == (fp, "disk_forecast", "Disk space", "/ is 4% free", s, NOON, 30)
    assert f"Acknowledge (90 days): {url}" in m.plain.splitlines() and f"Issue ID: {fp}" in m.plain
    a = audit_html(m.html)
    assert a.hrefs.count(url) == 2                                 # the button and the text link are the very same URL
    assert url.replace("&", "&amp;") in m.html and "Acknowledge for 90 days" in m.html and T.ACK_PROMISE in m.html
    assert f"Issue ID: <span" in m.html and fp in m.html and "nothing changes until you confirm" in m.html
    assert state()["ackfp"]["disk_forecast"] == {"fp": fp, "sev": s, "ts": NOON}          # what a later recovery is matched against


def test_the_link_shape_validation_and_day_snapping():
    tok, fp = "A" * 43, "0123456789abcdef"
    good = T.ack_url(BASE, tok, fp, 90, "crit")
    assert good == f"{BASE}/ack?id={fp}&t={tok}&d=90&s=crit"
    assert T.ack_url("https://x.ca:8443/maint/", tok, fp, 90, "warn") == f"https://x.ca:8443/maint/ack?id={fp}&t={tok}&d=90&s=warn"
    assert T.ack_url(BASE, tok, fp.upper(), 90, " CRIT ") == good
    for days, snapped in [(90, 90), (7, 7), (30, 30), (365, 365), (60, 30), (89, 30), (91, 90), (400, 365), (3, 7), (0, 7), (-5, 7), ("x", 90), (None, 90), (29.9, 7)]:
        assert f"&d={snapped}&" in T.ack_url(BASE, tok, fp, days, "warn"), days          # rounded DOWN: a link never promises more than was configured
    for base in ("javascript:alert(1)", "", None, 'https://x.ca" onclick="y', "ftp://x.ca", "https://x.ca/a b"):
        assert T.ack_url(base, tok, fp, 90, "warn") == "", base
    for bad_tok in ("", "short", "A" * 42, "A" * 44, "A" * 42 + "=", "A" * 42 + '"', "A" * 42 + "&", "A" * 41 + "&d", "A" * 42 + "\n", None):
        assert T.ack_url(BASE, bad_tok, fp, 90, "warn") == "", bad_tok
    for bad_fp in ("", "xyz", fp[:15], fp + "0", "0123456789abcdeg", None, "../../etc/passwd"):
        assert T.ack_url(BASE, tok, bad_fp, 90, "warn") == "", bad_fp
    for bad_sev in ("", "info", "ok", "critical", "warn&x=1", None):
        assert T.ack_url(BASE, tok, fp, 90, bad_sev) == "", bad_sev
    assert T.ACK_TOKEN_RE.match(T.PREVIEW_TOKEN) and T.ACK_FP_RE.match(T.PREVIEW_ID)       # the inert placeholder has the real shape


def test_prepare_revalidates_the_link_and_takes_days_and_id_from_it():
    site = {"url": BASE, "host_label": "h"}
    ev = notify.Event("alert", "crit", "t", "s", task="x")
    url30 = T.ack_url(BASE, "B" * 43, "0123456789abcdef", 30, "crit")
    m = T.prepare(ev, site=site, ack={"url": url30, "days": 90, "id": "ffffffffffffffff", "text": "Silence {days}d"})
    assert (m.ack_url, m.ack_days, m.ack_id, m.ack_text) == (url30, 30, "0123456789abcdef", "Silence 30d")      # the URL wins: words match what it does
    for bad in (f"{BASE}/ack/{'B' * 43}", "javascript:alert(1)", f"{BASE}/ack?id=0123456789abcdef&t={'B' * 43}&d=91&s=crit", f"{BASE}/ack?id=zz&t={'B' * 43}&d=90&s=crit",
                f"{BASE}/ack?id=0123456789abcdef&t={'B' * 43}&d=90&s=crit&x=1", f"{BASE}/ack?id=0123456789abcdef&t={'B' * 43}&d=90&s=crit\n"):
        m = T.prepare(ev, site=site, ack={"url": bad})
        assert m.ack_url == "" and "href" not in T.build_html(m).replace(f'href="{BASE}"', ""), bad
    m = T.prepare(ev, site=site, ack={"url": "", "id": "0123456789abcdef"})              # no link could be made: the Issue ID is still shown
    assert m.ack_url == "" and ("Issue ID", "0123456789abcdef") in m.facts and "Issue ID: 0123456789abcdef" in T.build_plain(m)
    assert T.prepare(ev, site=site, ack={"url": "", "id": "<script>"}).ack_id == ""


def test_the_button_is_bulletproof_accessible_and_has_a_text_fallback(acks):
    d, fk = go(mk("alert", "crit"))
    doc, url = fk.calls[0].html, fk.calls[0].ack_url
    esc_url = url.replace("&", "&amp;")
    btn = re.search(r'<table[^>]*role="presentation"[^>]*>\s*<tr><td align="center" valign="middle" height="48" bgcolor="([^"]+)"([^>]*)>\s*<a href="([^"]+)" style="([^"]+)">([^<]+)</a>', doc)
    assert btn, "a table-based button: a cell with a solid bgcolor holding one block link"
    bg, cell_style, href, a_style, label = btn.groups()
    assert bg == T.AMBER_BRIGHT and href == esc_url and "border-radius:999px" in cell_style and f"background:{T.AMBER_BRIGHT}" in cell_style
    assert "display:block" in a_style and f"color:{T.ON_AMBER}" in a_style and "text-decoration:none" in a_style and "padding:14px" in a_style
    assert label == "Acknowledge for 90 days &rarr;" and "max-width:340px" in doc and "overflow-wrap:break-word" in cell_style
    # colour contrast (WCAG): the button text, the words, the link
    assert contrast(T.ON_AMBER, T.AMBER_BRIGHT) >= 7 and contrast(T.SECONDARY, T.RAISE) >= 7 and contrast(T.AMBER_BRIGHT, T.RAISE) >= 4.5
    assert contrast(T.TEXT, T.RAISE) >= 7                          # the Issue ID
    # the fallback for clients that hide or mangle buttons: the URL as selectable text, and Issue ID, and the promise
    assert "Button not working? Open this link:" in doc and f'">{esc_url}</a>' in doc
    assert doc.count(esc_url) == 3                                  # the button's href, and the text link's href and its visible text
    assert "<img" not in doc and "<style" not in doc and "<script" not in doc and "<form" not in doc     # one link per action, nothing for a scanner to submit
    assert "Understood and okay with it?" in doc and "If it gets worse, or anything else fails, you are told as usual." in doc
    assert 'bgcolor="#211f1d"' in doc and T.RAISE in doc            # same Ohmz Cloud palette as the rest of the family


def test_every_token_is_fresh_one_per_email_and_for_the_same_fingerprint(acks):
    ev = mk("alert", "warn", task="failed_units", summary="1 failed unit: a.service")
    d1, fk = go(ev, now=NOON)
    d2, _ = go(ev, now=NOON + D, transport=fk)                      # a reminder a day later
    d3, _ = go(mk("alert", "warn", task="docker_df", summary="images are big"), now=NOON + D, transport=fk)
    assert d1.ok and d2.ok and d3.ok and len(acks.tokens) == 3
    toks = [t["token"] for t in acks.tokens]
    assert len(set(toks)) == 3 and acks.tokens[0]["fp"] == acks.tokens[1]["fp"] != acks.tokens[2]["fp"]
    assert [m.ack_url for m in fk.calls] == [f"{BASE}/ack?id={t['fp']}&t={t['token']}&d=90&s=warn" for t in acks.tokens]


def test_only_the_whitelisted_kinds_carry_a_button(acks):
    fk = Fake()
    for k, ev in notify.sample_events().items():
        if k.split(".")[0] in ("alert", "incident_open") or k == "ack_expired.failing":
            continue
        d = notify.send(ev, cfgx(), NOON, transport=fk)
        assert d.ok, k
        m = fk.calls[-1]
        assert m.ack_url == "" and "Acknowledge (" not in m.plain and "Acknowledge for" not in m.html and "/ack?" not in m.html, k
    assert acks.tokens == [] and len(fk.calls) == 6                 # recovery, maintenance, both digests, resolved incident, "no longer occurring"
    cfg = cfgx(ack={"button_kinds": ["alert", "recovery", "maintenance", "digest_daily", "test"]})
    d, fk = go(mk("recovery", "ok", "Disk space", "back"), cfg=cfg)
    assert fk.calls[0].ack_url == "" and acks.tokens == []          # the code's whitelist wins over a config typo
    d, fk = go(mk("incident_open", "crit"), cfg=cfg)
    assert fk.calls[0].ack_url == "" and acks.tokens == []          # and a kind that was left out of button_kinds has no button


def test_button_text_days_ttl_and_kill_switch_come_from_config(acks):
    d, fk = go(mk(), cfg=cfgx(ack={"button_text": "Silence it for {days} days", "token_ttl_days": 7}))
    m = fk.calls[0]
    assert "Silence it for 90 days" in m.html and "Acknowledge (90 days):" in m.plain and acks.tokens[-1]["ttl_days"] == 7 and "expires in 7 days" in m.plain
    d, fk = go(mk(task="a1"), cfg=cfgx(ack={"days": 60}))                         # a length the site does not offer is rounded down
    m = fk.calls[0]
    assert "&d=30&" in m.ack_url and "Acknowledge (30 days)" in m.plain and "Acknowledge for 30 days" in m.html
    d, fk = go(mk(task="a2"), cfg=cfgx(ack={"base_url": "https://other.example:8443/m"}))
    assert fk.calls[0].ack_url.startswith("https://other.example:8443/m/ack?id=")
    d, fk = go(mk(task="a3"), cfg=cfgx(ack={"base_url": ""}, site={"url": "https://site.example"}))
    assert fk.calls[0].ack_url.startswith("https://site.example/ack?id=")
    n = len(acks.tokens)
    acks.ack(mk(task="a4"), sev="crit")
    d, fk = go(mk(task="a4"), cfg=cfgx(ack={"enabled": False}))                   # the kill switch: no button, nothing held, and no token spent
    assert d.ok and fk.calls[0].ack_url == "" and "Acknowledge" not in fk.calls[0].plain and len(acks.tokens) == n and acks.suppressed == []


# ------------------------------------------------------------------ the SMS, the logs, the state: where a token may NOT be
def test_the_sms_never_carries_the_link_the_token_or_the_issue_id(acks):
    for sev in ("crit", "warn"):
        ev = mk("alert", sev, summary="/ is 4% free; see https://maintainer.ohmzhomelab.ca/ack?id=0123456789abcdef&t=" + "Z" * 43 + "&d=90&s=crit")
        d, fk = go(ev, cfg=cfgx(escalation={"warn_sms_after": 1}), now=NOON + (0 if sev == "crit" else D))
        m, fp, tok = fk.calls[0], fp_of(acks, ev), acks.tokens[-1]["token"]
        assert "sms" in m.channels and m.sms.startswith("homelab: ") and len(m.sms) <= 130 and m.sms.isascii()
        for secret in (tok, fp, "Z" * 43, "http", "/ack", "ack?", "id=", "maintenance.", "ohmzhomelab", "Acknowledge", "Issue ID"):
            assert secret not in m.sms, secret
    notify.send_test(None, cfgx(), NOON, transport=(fk := Fake()))              # and not in a TEST either
    assert all("/ack" not in m.sms and "http" not in m.sms and "Z" * 43 not in m.sms for m in fk.calls)


def test_token_plaintext_exists_only_in_the_email_bodies(acks):
    ev = mk("alert", "crit")
    d, fk = go(ev)
    m, tok = fk.calls[0], acks.tokens[-1]["token"]
    assert tok in m.plain and tok in m.html and tok in m.ack_url               # exactly where it must be
    for where, text in {"sms": m.sms, "subject": m.subject, "plain_safe": m.plain_safe, "note": d.note, "why": " ".join(d.why), "repr": repr(d),
                        "attempted": str(d.attempted), "legs": str(d.legs)}.items():
        assert tok not in text, where
    assert tok not in disk_dump(), "not in notify-state.json, notifications.jsonl, audit.jsonl, the run dir or anything else on disk"
    assert tok not in json.dumps(notify.export(NOON)) and tok not in json.dumps(notify.leg_health(NOON))
    assert tok not in json.dumps(notify.doctor(cfgx()), default=str)
    notify.flush_pending(cfgx(), NOON + D, transport=Fake())
    assert tok not in disk_dump()


def test_a_page_that_could_not_be_delivered_keeps_no_token_in_the_outbox_or_the_state(acks):
    ev = mk("alert", "crit")
    d, fk = go(ev, transport=Fake(legs={"sms": "failed", "email": "failed"}, ok=False))
    assert d.queued and len(state()["outbox"]) == 1
    tok = acks.tokens[-1]["token"]
    assert tok not in disk_dump() and "ack?" not in json.dumps(state()["outbox"]) and "Acknowledge" not in json.dumps(state()["outbox"])
    out = notify.flush_pending(cfgx(), NOON + 400, transport=(fk2 := Fake()))   # the replay renders afresh: a NEW token, for the same issue
    assert len(out) == 1 and out[0].ok and acks.tokens[-1]["token"] != tok and acks.tokens[-1]["fp"] == acks.tokens[0]["fp"]
    assert fk2.calls[0].ack_url.endswith("&d=90&s=crit") and all(t["token"] not in disk_dump() for t in acks.tokens)


@pytest.mark.parametrize("fake,revoked", [
    (Fake(), False),                                                                   # delivered: the link is in the owner's mailbox
    (Fake(legs={"sms": "sent", "email": "failed"}, ok=True), True),                    # the text went, the email did not: the link reached nobody
    (Fake(legs={"sms": "failed", "email": "failed"}, ok=False), True),
    (Fake(legs={"email": "skipped"}, ok=False), True),
    (Fake(legs={}, ok=False, fatal="child crashed"), True),
    (Fake(legs={}, ok=False, fatal="transport timed out", rc=124), False),             # a timeout may have half-sent it: keep the button alive
    (Fake(legs={}, ok=True), False),                                                    # "ok" without naming legs: assumed delivered
    (Fake(legs={"sms": "sent", "email": "sent"}, ok=True, via="bridge"), True)])        # the bridge's copy has no link
def test_a_token_whose_email_certainly_did_not_go_out_is_revoked(acks, fake, revoked):
    d, _ = go(mk("alert", "crit"), transport=fake)
    tok = acks.tokens[-1]["token"]
    assert (acks.revoked == [tok]) is revoked and (acks.revoked == []) is (not revoked)
    assert all(t not in disk_dump() for t in [tok])


def test_the_legacy_bridge_fallback_never_receives_the_link(acks, monkeypatch):
    seen = {}

    def run(cmd):
        seen["body"] = Path(cmd[-1]).read_text()
        return cp(0, "  sms sent to +14165550199 (SM1)\n  email sent to owner@example.com (10 chars plain, no html)\nRESULT: delivered\n")
    monkeypatch.setattr(notify, "sh", FakeSh(run))
    ev = mk("alert", "crit")
    primary = Fake(legs={}, ok=False, fatal="child crashed")                    # the primary broke before any channel reported
    d, _ = go(ev, transport=primary, fallback=notify.bridge_transport)
    tok, fp = acks.tokens[-1]["token"], fp_of(acks, ev)
    assert d.ok and d.channels == ["sms", "email"] and "via bridge fallback" in d.note and acks.revoked == [tok]        # the link was never delivered: revoked
    assert tok not in seen["body"] and "/ack?" not in seen["body"] and "Acknowledge (" not in seen["body"]      # a 0644 file for a moment: no bearer link
    assert f"Issue ID: {fp}" in seen["body"] and "Disk space" in seen["body"]                                     # but the id, and the alert, are there
    assert primary.calls[0].ack_url and tok in primary.calls[0].plain                                              # (the primary got the real thing)
    bare = notify.Message("ohmz", "s", "subj", f"x {tok} y", "", ["sms"], ack_url=tok, plain_safe="")             # no copy built: the URL is cut out
    monkeypatch.setattr(notify, "sh", FakeSh(run))
    notify.bridge_transport(bare, NC)
    assert tok not in seen["body"] and "<link omitted>" in seen["body"]


TOK = "Abc_dEf-ghIJklMNopQRstuVWxyz0123456789ABCde"      # 43 characters of the token alphabet, with the `-` and `_` that split other rules


@pytest.mark.parametrize("text", [
    f"see {BASE}/ack?id=0123456789abcdef&t={TOK}&d=90&s=crit now", f"{BASE}/ack?t={TOK}", f"{BASE}/ack?d=90&t={TOK}&id=0123456789abcdef",
    f"{BASE}/ack/{TOK}", f"https://x.example/ack?id=0123456789abcdef&t={TOK}&d=7&s=warn&utm=1"])
def test_redaction_removes_the_token_in_every_link_shape(text):
    assert TOK not in T.line(text) and TOK not in T.scrub_secrets(text) and TOK not in notify.redact_log(text, 400)
    assert TOK not in " ".join(T.lines(text)) and TOK not in " ".join(T.log_lines(text))
    ev = notify.Event("alert", "crit", text, text, {"log": [text], "todo": [text], "text": text}, {"Cfg": text, "ack_summary": text}, "crit", "k", "disk_forecast")
    m = T.prepare(ev, now=NOON)
    for out in (T.build_html(m), T.build_plain(m), T.build_sms(m), T.build_subject(m)):
        assert TOK not in out
    assert TOK not in json.dumps(T.clean_facts(ev.facts)) and TOK not in json.dumps(T.clean_details(ev.details))


@pytest.mark.parametrize("text", [f"link: {TOK}", f"t={TOK}", f"({TOK})", f"x/{TOK}?y"])
def test_the_delivery_log_redactor_also_removes_a_bare_token(text):
    assert TOK not in notify.redact_log(text, 400) and "<token>" in notify.redact_log(text, 400)
    for name in ("x-y" * 14, "x-y" * 14 + "-z-", "gluetun-cloudflared-tunnel-immich-public-proxy-x"):       # a long hyphenated name is not a token (42 and 44+ characters)
        assert notify.redact_log("a " + name + " b") == "a " + name + " b", name


def test_a_summary_that_quotes_an_ack_link_never_leaks_its_token(acks):
    quoted = "Q" * 43
    ev = mk("alert", "crit", summary=f"mail failed: {BASE}/ack?id=0123456789abcdef&t={quoted}&d=90&s=crit", details={"log": [f"GET /ack?t={quoted}"]})
    d, fk = go(ev, transport=Fake(legs={"sms": "failed", "email": "failed"}, ok=False))        # failed: it is copied into the outbox too
    m = fk.calls[0]
    assert quoted not in m.plain + m.html + m.sms + m.subject and quoted not in disk_dump() and quoted not in json.dumps(log_rows())


def test_dry_runs_previews_and_tests_show_an_inert_link_and_never_issue_a_token(acks, capsys, tmp_path):
    d, fk = go(mk(), dry_run=True)
    assert fk.calls == [] and acks.tokens == []
    r = d.rendered
    assert T.PREVIEW_TOKEN in r["plain"] and T.PREVIEW_TOKEN in r["html"] and "PREVIEW: this link is not valid." in " ".join(r["plain"].split())
    notify.send_test(["alert", "ack_expired"], cfgx(), NOON, transport=(fk := Fake()))
    assert len(fk.calls) == 4 and acks.tokens == []
    assert all(m.ack_url == "" for m in fk.calls)                  # nothing a hook or a log could take for a real link
    with_link = [m for m in fk.calls if "ack?id=" in m.plain]
    assert len(with_link) == 3 and all(T.PREVIEW_TOKEN in m.plain and T.PREVIEW_TOKEN in m.html for m in with_link)     # the two alerts + the failing notice, not the "gone" one
    assert notify.main(["render", "--out", str(tmp_path / "p")]) == 0
    html_ = (tmp_path / "p" / "alert.crit.html").read_text()
    assert T.PREVIEW_TOKEN in html_ and acks.tokens == [] and "Acknowledge for 90 days" in html_
    assert not (core.STATE_DIR / "notify-state.json").exists() or "ackfp" not in state()


@pytest.mark.parametrize("evil", EVIL)
def test_html_escaping_holds_around_the_acknowledge_block(acks, evil):
    ev = notify.Event("alert", "crit", "T" + evil, "S" + evil, {"todo": ["W" + evil]}, {"FK" + evil: "FV" + evil}, "crit", "k", "task" + evil)
    d, fk = go(ev, cfg=cfgx(ack={"button_text": "B" + evil + " {days}"}))
    m = fk.calls[0]
    assert d.ok and m.ack_url, "a hostile task name still gets its (hex) fingerprint and a link"
    audit_html(m.html)
    if T.esc(evil) != evil:
        assert evil not in m.html
    # the only hrefs are our own site; the only attribute values are ours
    assert all(h.startswith(BASE) for h in audit_html(m.html).hrefs)


def test_hostile_base_url_token_or_module_output_means_no_button_never_a_bad_href(acks):
    for i, base in enumerate(("javascript:alert(1)", 'https://x.ca" onclick="y', "//evil.example", "https://")):
        d, fk = go(mk(task=f"b{i}"), cfg=cfgx(ack={"base_url": base}, site={"url": BASE}))
        m = fk.calls[0]
        assert d.ok and m.ack_url == "" and "javascript" not in m.html.lower() and "onclick" not in m.html
        assert "ack link unavailable" in d.note and f"Issue ID: {fp_of(acks, mk(task=f'b{i}'))}" in m.plain      # still says the id
        audit_html(m.html)
    for i, bad in enumerate(['"><script>alert(1)</script>' + "A" * 30, "A" * 42, "A" * 44, "a b" * 15, "", None, 7, "A" * 42 + "&"]):
        acks.issue_hook = lambda bad=bad, **kw: bad
        d, fk = go(mk(task=f"c{i}"))
        assert d.ok and fk.calls[0].ack_url == "" and "<script" not in fk.calls[0].html and "ack link unavailable" in d.note, bad
    acks.issue_hook = lambda **kw: (_ for _ in ()).throw(OSError("disk full"))
    d, fk = go(mk(task="d1"))
    assert d.ok and fk.calls[0].ack_url == "" and "ack link unavailable (OSError)" in d.note and "disk full" not in d.note       # the alert goes out, the log names the class only


def test_no_real_send_is_possible_with_the_acknowledge_machinery_on(acks):
    d = notify.send(mk(), cfgx(), NOON)                           # no transport injected: the real one is refused under pytest
    assert not d.ok and "blocked under pytest" in d.note and state()["sent"] == []
    tok = acks.tokens[-1]["token"]
    assert tok not in d.note and tok not in disk_dump()
    d = notify.send(mk(task="again"), cfgx(), NOON, fallback=notify.bridge_transport)      # nor can the legacy bridge be reached
    assert not d.ok and acks.tokens[-1]["token"] not in disk_dump()
    r = notify.notify_expired(NOON, cfgx(), expired=[{"fp": "0123456789abcdef", "task": "failed_units", "title": "T", "severity": "warn", "acked_at": NOON - 90 * D,
                                                     "until": NOON, "still_failing": True}])
    assert len(r) == 1 and not r[0].ok                               # (queued for a retry, never sent)


# ------------------------------------------------------------------ suppression
def test_an_acknowledged_alert_is_not_sent_and_the_log_says_why(acks):
    ev = mk("alert", "warn", task="failed_units", summary="1 failed unit: nginx.service")
    fp = acks.ack(ev, sev="warn")
    d, fk = go(ev)
    until = time.strftime("%Y-%m-%d", time.localtime(NOON + 90 * D))
    assert fk.calls == [] and not d.ok and d.handled and d.skipped == "acknowledged" and d.channels == [] and d.attempted == []
    assert d.note == f"suppressed: acknowledged until {until}" and fp in " ".join(d.why)
    row = log_rows()[-1]
    assert row["skipped"] == "acknowledged" and row["ok"] is True and row["channels"] == [] and row["note"] == d.note and row["kind"] == "alert"
    aud = [r for r in audit_rows() if r["action"] == "suppressed"]
    assert len(aud) == 1 and aud[0]["task"] == "notify" and aud[0]["outcome"] == f"acknowledged until {until}"
    assert acks.suppressed == [fp] and acks.tokens == []             # counted on the acknowledged issue; no token issued for a held message
    assert not any(r["action"] == "send" for r in audit_rows())
    for text in (disk_dump(),):
        assert "@" not in text.replace("jsonl", "")


@pytest.mark.parametrize("acked,sev,held", [("crit", "crit", True), ("crit", "warn", True), ("warn", "warn", True), ("warn", "crit", False)])
def test_the_severity_ceiling_decides_not_notify(acks, acked, sev, held):
    ev = mk("alert", sev, task="failed_units", summary="1 failed unit: a.service")
    acks.ack(ev, sev=acked)
    d, fk = go(ev)
    assert (d.skipped == "acknowledged") is held and bool(fk.calls) is (not held)


def test_a_suppressed_alert_costs_no_budget_no_dedupe_slot_no_escalation_and_no_sms(acks):
    cfg = cfgx(budget={"per_day": {"alert": 2}})
    held = mk("alert", "warn", task="noisy", summary="1 unit failed")
    acks.ack(held, sev="warn")
    for i in range(6):                                                # far more than the per-kind budget of 2
        assert go(held, cfg=cfg, now=NOON + i * 7 * H)[0].skipped == "acknowledged"
    st = state()
    assert st["sent"] == [] and st["esc"] == {} and not [k for k in st["dedupe"] if not k.startswith("ack|")]
    results = [go(mk("alert", "warn", task=t, summary=f"{t} failed"), cfg=cfg, now=NOON + 40 * H)[0] for t in ("a", "b", "c")]
    assert [d.ok for d in results] == [True, True, False] and results[2].skipped == "budget"         # the two real alerts got the budget the held ones did not use
    assert not any(r["action"] == "budget-exhausted" and "noisy" in r["target"] for r in audit_rows())
    acks.acks.clear()                                                  # un-acknowledge: normal alerting immediately, with a fresh SMS budget
    d, fk = go(mk("alert", "crit", task="noisy", summary="1 unit failed"), cfg=cfgx(), now=NOON + 41 * H)
    assert d.ok and set(fk.calls[0].channels) == {"sms", "email"}


def test_quiet_hours_do_not_turn_a_suppression_into_something_else(acks):
    night = at(2)
    crit = mk("alert", "crit", task="x1", summary="x1 is down")
    warn = mk("alert", "warn", task="x2", summary="x2 is slow")
    acks.ack(crit, sev="crit", now=night)
    acks.ack(warn, sev="warn", now=night)
    for ev in (crit, warn):
        d, fk = go(ev, now=night)
        assert d.skipped == "acknowledged" and fk.calls == []                 # not "quiet-hours": the log tells the truth
    d, fk = go(warn, cfg=cfgx(quiet_hours={"suppress": ["sms", "email"]}), now=night)
    assert d.skipped == "acknowledged"                                         # even when quiet hours would hold the email too
    assert state()["sent"] == []
    loud, _ = go(mk("alert", "warn", task="x3", summary="x3 is slow"), now=night)       # an unacknowledged warning at night: email, no text, as before
    assert loud.ok and loud.channels == ["email"] and [r[2] for r in state()["sent"]] == [0]
    assert acks.tokens[-1]["fp"] != fp_of(acks, crit)                          # and its button is for ITS issue


def test_escalation_breaks_the_acknowledgement_and_the_new_button_is_for_the_new_severity(acks):
    warn = mk("alert", "warn", task="failed_units", summary="1 failed unit: a.service")
    crit = mk("alert", "crit", task="failed_units", summary="1 failed unit: a.service")
    fp = acks.ack(warn, sev="warn")
    assert go(warn)[0].skipped == "acknowledged"
    d, fk = go(crit, now=NOON + H)                                             # warn -> crit: not covered, alerts once more
    assert d.ok and set(fk.calls[0].channels) == {"sms", "email"} and fk.calls[0].ack_url.startswith(f"{BASE}/ack?id={fp}&t=") and fk.calls[0].ack_url.endswith("&d=90&s=crit")
    assert acks.suppressed == [fp] and acks.tokens[-1]["severity"] == "crit"
    acks.ack(crit, sev="crit", now=NOON + H)                                   # the owner clicks the new button
    assert go(crit, now=NOON + 8 * H)[0].skipped == "acknowledged" and go(warn, now=NOON + 9 * H)[0].skipped == "acknowledged"


def test_another_error_an_expired_acknowledgement_and_facts_ack_false_are_sent(acks):
    ev = mk("alert", "warn", task="failed_units", summary="1 failed unit: a.service")
    acks.ack(ev, days=1, sev="warn")
    other = mk("alert", "warn", task="failed_units", summary="1 failed unit: a.service; 2 unhealthy: kavita")       # a different exact error
    assert go(other)[0].ok
    assert go(ev, now=NOON + 2 * D)[0].ok                                      # expired: normal alerting resumes (no notice from here: that is send_expired's)
    acks.ack(ev, days=90, sev="warn", now=NOON + 2 * D)
    for i, flag in enumerate((False, "false", 0, "no")):                       # `--fact ack=false` from a hook, or a frozen outbox copy: neither held nor offered
        forced = notify.Event("alert", "warn", "Services", ev.summary, None, {"ack": flag}, "warn", f"forced{i}", "failed_units")
        d, fk = go(forced, now=NOON + 3 * D)
        assert d.ok and fk.calls[0].ack_url == "" and "Acknowledge" not in fk.calls[0].plain, flag


@pytest.mark.parametrize("hook", [
    lambda fp, sev, now: (_ for _ in ()).throw(RuntimeError("acks.json is corrupt")),
    lambda fp, sev, now: {"until": "tomorrow"}, lambda fp, sev, now: {"until": True}, lambda fp, sev, now: {"until": None}, lambda fp, sev, now: {},
    lambda fp, sev, now: [], lambda fp, sev, now: 0, lambda fp, sev, now: types.SimpleNamespace(until=float("nan")),
    lambda fp, sev, now: types.SimpleNamespace(until=float("inf")), lambda fp, sev, now: types.SimpleNamespace(until=1e13),
    lambda fp, sev, now: types.SimpleNamespace(until=NOON), lambda fp, sev, now: types.SimpleNamespace(until=NOON - 1),
    lambda fp, sev, now: types.SimpleNamespace(until=-NOON), lambda fp, sev, now: types.SimpleNamespace(), lambda fp, sev, now: {"until": [NOON + 5]}])
def test_any_doubt_about_an_acknowledgement_sends_the_alert(acks, hook):
    acks.is_acked_hook = hook
    d, fk = go(mk("alert", "crit"))
    assert d.ok and fk.calls and d.skipped == "" and acks.suppressed == []


@pytest.mark.parametrize("fpv", [None, "", "zz", "ABC", 7, "0123456789abcde", "0123456789abcdef0", "0123456789abcdeg", ["0123456789abcdef"], object()])
def test_a_module_that_answers_nonsense_for_the_fingerprint_never_silences_anything(acks, fpv):
    acks.fingerprint_hook = lambda task, res, sev: fpv
    acks.is_acked_hook = lambda fp, sev, now: types.SimpleNamespace(until=NOON + D)          # (would hold everything if the fingerprint were believed)
    d, fk = go(mk("alert", "crit"))
    assert d.ok and fk.calls and fk.calls[0].ack_url == "" and acks.suppressed == []
    acks.fingerprint_hook = lambda task, res, sev: (_ for _ in ()).throw(ValueError("x"))
    assert go(mk("alert", "crit", task="t2"))[0].ok


def test_an_alert_for_a_task_without_a_name_is_never_held(acks):
    acks.is_acked_hook = lambda fp, sev, now: types.SimpleNamespace(until=NOON + D)
    d, fk = go(notify.Event("alert", "crit", "Backup failed", "exit 1", None, None, "crit", "k"))     # no task: nothing to fingerprint
    assert d.ok and fk.calls[0].ack_url == "" and acks.tokens == []


def test_the_recovery_of_an_acknowledged_issue_is_held_and_the_episode_is_closed(acks):
    ev = mk("alert", "crit")
    d, fk = go(ev)                                                             # paged before it was acknowledged
    assert d.ok and state()["ackfp"]["disk_forecast"]["sev"] == "crit"
    acks.ack(ev, sev="crit", now=NOON + H)                                     # the owner clicked the button
    rec = notify.recovery_event("disk_forecast", "Disk space", "back to 31% free", was="crit")
    d, _ = go(rec, now=NOON + 2 * H, transport=fk)
    assert d.skipped == "acknowledged" and d.handled and len(fk.calls) == 1                  # no recovery email, no recovery text
    st = state()
    assert "disk_forecast" not in st.get("ackfp", {}) and "disk_forecast" not in st["esc"] and "alert|disk_forecast" not in st["dedupe"]
    d, _ = go(ev, now=NOON + 3 * H, transport=fk)                              # it comes back inside the window: the same exact error does not page again
    assert d.skipped == "acknowledged" and len(fk.calls) == 1
    assert go(notify.recovery_event("disk_forecast", "Disk space", "back", was="crit"), now=NOON + 4 * H, transport=fk)[0].skipped == "acknowledged"
    acks.acks.clear()                                                          # un-acknowledge: back to normal at once
    assert go(ev, now=NOON + 5 * H, transport=fk)[0].ok and len(fk.calls) == 2
    other = go(notify.recovery_event("failed_units", "Services", "ok", was="warn"), now=NOON + 6 * H, transport=fk)[0]
    assert other.ok and len(fk.calls) == 3                                     # another task's recovery is untouched


def test_a_recovery_is_only_held_at_the_severity_the_owner_was_paged_for(acks):
    warn = mk("alert", "warn", task="failed_units", summary="1 failed unit: a.service")
    crit = mk("alert", "crit", task="failed_units", summary="1 failed unit: a.service")
    acks.ack(warn, sev="warn")
    d, fk = go(crit, now=NOON)                                                 # it got worse: paged at crit despite the warn acknowledgement
    assert d.ok
    d, _ = go(notify.recovery_event("failed_units", "Services", "ok", was="crit"), now=NOON + H, transport=fk)
    assert d.ok and len(fk.calls) == 2                                         # the owner WAS paged: the all-clear is sent


def test_the_incident_pair_is_held_by_the_fingerprint_the_caller_supplies(acks):
    fp = "00112233445566ff"
    acks.acks[fp] = {"until": NOON + 30 * D, "sev": "crit"}
    opened = mk("incident_open", "crit", "Plex media mount missing", "Plex would regenerate Media on the root disk", task="plex_media_mount_check",
                dedupe_key="inc-1", facts={"sev": "SEV2", "ack_fp": fp})
    d, fk = go(opened)
    assert d.skipped == "acknowledged" and fk.calls == [] and acks.suppressed == [fp]
    resolved = mk("incident_resolved", "ok", "Plex media mount missing", "Mounted again", task="plex_media_mount_check", dedupe_key="inc-1", facts={"was": "crit"})
    d, _ = go(resolved, now=NOON + H, transport=fk)
    assert d.skipped == "acknowledged" and fk.calls == [] and "inc-1" not in state().get("ackfp", {})
    d, _ = go(mk("incident_open", "crit", "Other", "x", task="other_task", dedupe_key="inc-2", facts={"ack_fp": "ffffffffffffffff"}), now=NOON + 2 * H, transport=fk)
    assert d.ok and fk.calls[0].ack_url.startswith(f"{BASE}/ack?id=ffffffffffffffff&t=")        # the caller's fingerprint is what the button is for


def test_only_alert_recovery_and_incident_kinds_can_ever_be_held(acks):
    acks.is_acked_hook = lambda fp, sev, now: types.SimpleNamespace(until=NOON + 90 * D)      # EVERYTHING is "acknowledged"
    cfg = cfgx(ack={"suppress_kinds": list(T.KINDS)})                                           # and the config tries to hold every kind
    fk = Fake()
    sent = {}
    for k, ev in notify.sample_events().items():
        facts = dict(ev.facts or {})
        d = notify.send(notify.Event(ev.kind, ev.severity, ev.title, ev.summary, ev.details, facts, ev.status, ev.dedupe_key, ev.task), cfg, NOON, transport=fk)
        sent[k] = d.skipped
    held = {k for k, why in sent.items() if why == "acknowledged"}
    assert held == {"alert.crit", "alert.warn", "recovery", "incident_open", "incident_resolved"}, held
    for ev in (notify.maintenance_event("daily", "Daily cleanup", "freed 1 GiB", done=["x"]), notify.digest_event("digest_daily", "All clear", "ok"),
               notify.digest_event("report_weekly", "Week 40", "ok"), notify.ack_expired_event({"fp": "0123456789abcdef", "task": "failed_units", "title": "T",
                                                                                              "severity": "warn", "acked_at": NOON - D, "until": NOON, "still_failing": True})):
        assert notify.send(ev, cfg, NOON + 9 * D, transport=fk).skipped != "acknowledged", ev.kind
    notify.send_test(None, cfg, NOON, transport=(fk2 := Fake()))
    assert len(fk2.calls) == len(notify.sample_events())                                       # a TEST always goes: it must prove the path works


def test_held_messages_are_logged_once_an_hour_but_counted_every_time(acks):
    ev = mk("alert", "warn", task="failed_units", summary="1 failed unit: a.service")
    fp = acks.ack(ev, sev="warn")
    for i in range(5):
        go(ev, now=NOON + i * 600)
    assert len([r for r in log_rows() if r.get("skipped") == "acknowledged"]) == 1 and len([r for r in audit_rows() if r["action"] == "suppressed"]) == 1
    assert acks.suppressed == [fp] * 5                                          # the counter on the acknowledged issue is exact
    go(ev, now=NOON + 3700)
    assert len([r for r in log_rows() if r.get("skipped") == "acknowledged"]) == 2


def test_a_dry_run_of_a_held_alert_records_nothing(acks):
    ev = mk("alert", "crit")
    acks.ack(ev, sev="crit")
    d, fk = go(ev, dry_run=True)
    assert d.skipped == "acknowledged" and fk.calls == [] and acks.suppressed == [] and not (core.STATE_DIR / "notifications.jsonl").exists()
    assert audit_rows() == [] and acks.tokens == []


def test_a_queued_critical_page_is_held_when_the_acknowledgement_arrives_before_the_replay(acks):
    ev = mk("alert", "crit")
    d, _ = go(ev, transport=Fake(legs={"sms": "failed", "email": "failed"}, ok=False))
    assert d.queued and len(state()["outbox"]) == 1
    acks.ack(ev, sev="crit", now=NOON + 100)
    out = notify.flush_pending(cfgx(), NOON + 400, transport=(fk := Fake()))
    assert len(out) == 1 and out[0].skipped == "acknowledged" and fk.calls == [] and not state().get("outbox")      # gone from the outbox, never sent


def test_the_export_counts_suppressed_messages_without_any_secret(acks):
    ev = mk("alert", "crit")
    acks.ack(ev, sev="crit")
    go(ev), go(ev, now=NOON + 2 * H)
    ex = notify.export(NOON + 3 * H)
    assert ex["counts"]["24h"]["suppressed"] == 2 and ex["counts"]["24h"]["sent"] == 0 and ex["recent"][0]["skipped"] == "acknowledged"
    assert ex["recent"][0]["note"].startswith("suppressed: acknowledged until ") and ex["failures"]["24h"] == 0
    assert "ack?" not in json.dumps(ex)


def test_hermes_notifier_uses_the_exact_fingerprint_of_the_result_and_the_recovery_stays_quiet(acks, monkeypatch):
    fk = Fake()
    monkeypatch.setattr(notify, "hermes_transport", fk)
    exact = "abcdef0123456789"
    acks.fingerprint_hook = lambda task, res, sev: exact if isinstance(res, core.Result) else "ffffffffffffffff"       # only the Result knows its issue_key
    acks.acks[exact] = {"until": NOON + 90 * D, "sev": "crit"}
    cfg = cfgx()

    def run(status, t, summary="/ is 4% free"):
        nt = notify.HermesNotifier(cfg)
        nt.evaluate("disk_forecast", "Disk space", core.Result(status, summary), t)
        nt.save()
        return json.loads((core.STATE_DIR / "alerts.json").read_text())["tasks"]["disk_forecast"]
    run("crit", NOON)
    st = run("crit", NOON + 900)                                                # confirmed: would page, but it is acknowledged
    assert fk.calls == [] and st["alerted"] == 2 and acks.suppressed == [exact]             # core believes it was handled: it reminds daily, silently
    run("ok", NOON + 1800)
    st = run("ok", NOON + 2700)                                                 # confirmed recovery: held too
    assert fk.calls == [] and st["alerted"] == 0 and "recovery_pending" not in st
    acks.acks.clear()
    run("crit", NOON + 3600)
    run("crit", NOON + 4500)                                                    # un-acknowledged: pages again, with a button for THE issue
    assert len(fk.calls) == 1 and fk.calls[0].ack_url.startswith(f"{BASE}/ack?id={exact}&t=")
    st = json.loads((core.STATE_DIR / "alerts.json").read_text())
    assert all(t["token"] not in disk_dump() for t in acks.tokens)


def test_a_deferred_notifier_queues_the_fingerprint_and_the_hold_is_decided_at_delivery(acks, monkeypatch):
    fk = Fake()
    monkeypatch.setattr(notify, "hermes_transport", fk)
    exact = "abcdef0123456789"
    acks.fingerprint_hook = lambda task, res, sev: exact
    nt = notify.HermesNotifier(cfgx(), defer=True)
    nt.evaluate("disk_forecast", "Disk space", core.Result("crit", "/ is 4% free"), NOON)
    nt.evaluate("disk_forecast", "Disk space", core.Result("crit", "/ is 4% free"), NOON + 900)
    box = state()["outbox"]
    assert len(box) == 1 and box[0]["snap"]["facts"]["ack_fp"] == exact and "ack?" not in json.dumps(box)          # the id travels, never a link
    acks.acks[exact] = {"until": NOON + 90 * D, "sev": "crit"}                  # the owner acknowledged while it was queued
    nt.deliver(NOON + 1000)
    assert fk.calls == [] and not state().get("outbox") and acks.suppressed == [exact]


# ------------------------------------------------------------------ the notice when an acknowledgement ends
ITEM = {"fp": "0123456789abcdef", "task": "failed_units", "title": "Services", "summary": "1 failed unit: nginx.service", "severity": "warn",
        "acked_at": NOON - 90 * D, "until": NOON, "by": "email", "note": "known, fix is scheduled", "count_suppressed": 14}


def test_ack_expired_event_still_failing_and_no_longer_occurring():
    ev = notify.ack_expired_event(ITEM, still_failing=True)
    assert (ev.kind, ev.severity, ev.task, ev.dedupe_key) == ("ack_expired", "warn", "failed_units", f"ack-expired-0123456789abcdef-{int(NOON):x}")
    assert "90-day acknowledgement" in ev.summary and "still failing" in ev.summary and "1 failed unit: nginx.service" in ev.summary
    assert ev.facts["ack_fp"] == ITEM["fp"] and ev.facts["still_failing"] is True and ev.facts["Held back"] == "14 alerts and emails"
    assert ev.facts["Your note"] == "known, fix is scheduled" and ev.facts["Now"] == "still failing (warn)" and "acknowledge it again" in ev.details["todo"][0]
    crit = notify.ack_expired_event({**ITEM, "severity": "crit"}, still_failing=True)
    assert crit.severity == "crit"
    gone = notify.ack_expired_event(ITEM, still_failing=False)
    assert gone.severity == "ok" and "no longer occurring" in gone.summary and gone.facts["Now"] == "no longer occurring" and "Nothing to do" in gone.details["todo"][0]
    odd = notify.ack_expired_event({"fp": "<script>", "task": "t<b>", "title": None, "until": "x", "severity": "bogus"}, still_failing=True)
    assert odd.dedupe_key.startswith("ack-expired-") and "ack_fp" not in odd.facts and odd.severity == "warn"
    assert notify.ack_expired_event("garbage").kind == "ack_expired"           # never raises


def test_one_expiry_notice_by_email_only_with_a_fresh_button_when_it_is_still_failing(acks):
    fk = Fake()
    out = notify.send_expired([{**ITEM, "still_failing": True}], cfgx(), NOON, transport=fk)
    assert len(out) == 1 and out[0].ok and out[0].channels == ["email"] and fk.calls[0].channels == ["email"]
    m = fk.calls[0]
    assert m.subject.startswith("[homelab] Ack expired: Services") and m.sms and "ACK ENDED" in m.sms
    assert m.ack_url == f"{BASE}/ack?id={ITEM['fp']}&t={acks.tokens[0]['token']}&d=90&s=warn" and acks.tokens[0]["fp"] == ITEM["fp"]
    assert "Still okay with it?" in m.html and "acknowledge it again for 90 days" in m.html and "Acknowledgement expired" in m.html and audit_html(m.html)
    assert "The 90-day acknowledgement of this exact error ended on" in m.plain and "Acknowledge (90 days):" in m.plain
    again = notify.send_expired([{**ITEM, "still_failing": True}], cfgx(), NOON + H, transport=fk)
    assert again[0].skipped == "dedupe" and len(fk.calls) == 1 and len(acks.tokens) == 1                     # one notice per acknowledgement
    later = notify.send_expired([{**ITEM, "still_failing": True, "acked_at": NOON + 2 * H, "until": NOON + 2 * H + 7 * D}], cfgx(), NOON + 3 * H, transport=fk)
    assert later[0].ok and len(fk.calls) == 2 and "7-day" in fk.calls[-1].plain               # re-acknowledged, ended again: a NEW acknowledgement, its own notice
    crit = notify.send_expired([{**ITEM, "fp": "1111111111111111", "severity": "crit", "still_failing": True}], cfgx(), NOON, transport=fk)
    assert crit[0].channels == ["email"] and "&s=crit" in fk.calls[-1].ack_url                              # a critical one still never texts
    gone = notify.send_expired([{**ITEM, "fp": "2222222222222222", "still_failing": False}], cfgx(), NOON, transport=fk)
    assert gone[0].ok and fk.calls[-1].ack_url == "" and "no longer occurring" in fk.calls[-1].plain and len(acks.tokens) == 3
    assert [r["kind"] for r in log_rows()][-3:] == ["ack_expired"] * 3


def test_an_expiry_notice_that_cannot_be_delivered_is_queued_and_sent_later_exactly_once(acks):
    out = notify.send_expired([{**ITEM, "still_failing": True}], cfgx(), NOON, transport=Fake(legs={"email": "failed"}, ok=False))
    assert not out[0].ok and out[0].queued and len(state()["outbox"]) == 1                                  # acks.expire() reports an expiry only once: do not lose it
    box = json.dumps(state()["outbox"])
    assert "ack?" not in box and ITEM["fp"] in box and all(t["token"] not in box for t in acks.tokens)
    got = notify.flush_pending(cfgx(), NOON + 400, transport=(fk := Fake()))
    assert len(got) == 1 and got[0].ok and fk.calls[0].ack_url.endswith("&d=90&s=warn") and not state().get("outbox")
    assert notify.flush_pending(cfgx(), NOON + 900, transport=fk) == [] and len(fk.calls) == 1


def test_notify_expired_asks_acks_and_decides_failing_or_gone_from_the_status(acks):
    store = {"v": 1, "acks": {ITEM["fp"]: {k: v for k, v in ITEM.items() if k != "fp"}}, "tokens": {}}
    (core.STATE_DIR / "acks.json").write_text(json.dumps(store))
    acks.fingerprint_hook = lambda task, res, sev: ITEM["fp"] if "nginx" in (res if isinstance(res, str) else res.summary) else "ffffffffffffffff"
    status = {"tasks": {"failed_units": {"status": "warn", "summary": "1 failed unit: nginx.service", "alert": True}}}
    acks.expired = [ITEM["fp"]]
    out = notify.notify_expired(NOON, cfgx(), status=status, transport=(fk := Fake()))
    assert len(out) == 1 and out[0].ok and fk.calls[0].ack_url and "still failing" in fk.calls[0].plain       # the exact error is still reported
    acks.expired = ["2222222222222222"]
    assert notify.notify_expired(NOON, cfgx(), status=status, transport=fk) == [] and len(fk.calls) == 1        # no record for that fingerprint: nothing honest to say
    store["acks"]["3333333333333333"] = {**{k: v for k, v in ITEM.items() if k != "fp"}, "task": "backup_freshness", "title": "Backups"}
    (core.STATE_DIR / "acks.json").write_text(json.dumps(store))
    acks.expired = ["3333333333333333"]
    status2 = {"tasks": {"backup_freshness": {"status": "ok", "summary": "all backups fresh"}}}
    out = notify.notify_expired(NOON, cfgx(), status=status2, transport=fk)
    assert out[0].ok and fk.calls[-1].ack_url == "" and "no longer occurring" in fk.calls[-1].plain             # recovered: nothing to click
    acks.expired = [ITEM["fp"]]
    status3 = {"tasks": {"failed_units": {"status": "warn", "summary": "1 unhealthy: kavita"}}}                  # same task, a DIFFERENT error: the acknowledged one is gone
    out = notify.notify_expired(NOON + 9 * D, cfgx(), status=status3, transport=fk)
    assert out[0].ok and "no longer occurring" in fk.calls[-1].plain
    acks.expire = lambda now: (_ for _ in ()).throw(OSError("acks.json unreadable"))
    assert notify.notify_expired(NOON, cfgx(), transport=fk) == []              # never raises


def test_after_the_acknowledgement_ends_the_same_alert_is_sent_again_with_a_new_button(acks):
    ev = mk("alert", "warn", task="failed_units", summary="1 failed unit: a.service")
    acks.ack(ev, days=1, sev="warn")
    assert go(ev)[0].skipped == "acknowledged"
    d, fk = go(ev, now=NOON + 2 * D)
    assert d.ok and fk.calls[0].ack_url.startswith(f"{BASE}/ack?id=") and len(acks.tokens) == 1 and acks.suppressed == [fp_of(acks, ev)]


# ------------------------------------------------------------------ the pictures: every variant, in the family, at phone width
@pytest.mark.parametrize("name", ["alert.crit", "alert.warn", "incident_open", "ack_expired.failing"])
def test_the_acknowledge_variants_render_in_the_family_with_an_inert_link_in_previews(acks, name):
    doc = render_all()[name]
    a = audit_html(doc)
    assert T.PREVIEW_TOKEN in doc and "PREVIEW: this link is not valid." in doc and "Acknowledge for 90 days" in doc and len(doc) < 60_000
    for token in (T.CANVAS, T.PANEL, T.RAISE, T.AMBER_BRIGHT, T.ON_AMBER, T.SECONDARY):
        assert token in doc
    assert any("/ack?id=" in h for h in a.hrefs) and not any(T.PREVIEW_TOKEN not in h for h in a.hrefs if "/ack?" in h)
    assert all(re.search(r"font:[^;]*(?:1[0-9]|[2-9][0-9])px", s) for s in re.findall(r'style="([^"]*font:[^"]*)"', doc))      # no text below 10px


@pytest.mark.skipif(_chrome() is None, reason="no headless Chrome here (set HOMELAB_MAINT_NO_BROWSER_TESTS=1 to skip on purpose)")
def test_the_acknowledge_block_fits_a_phone_and_the_button_is_a_real_tap_target(acks, tmp_path, monkeypatch):
    """Measured in headless Chrome at 390 and 320 px: no horizontal scroll, the button is >= 44 px tall and inside the card, the long
    link wraps inside the card, and the text is not clipped."""
    import html as htmllib
    nc = notify.load_config(cfgx())
    frames = ""
    names = ["alert.crit", "alert.warn", "incident_open", "ack_expired.failing"]
    for name in names:
        ev = notify.sample_events()[name]
        dec = notify.decide(ev, nc, copy.deepcopy(notify._EMPTY_STATE), NOON)
        dec.channels = ["sms", "email"]
        frames += (f'<iframe data-name="{name}" style="width:WIDTHpx;height:1200px;border:0;display:block" '
                   f'srcdoc="{htmllib.escape(notify._render(ev, nc, dec, NOON)[0].html, quote=True)}"></iframe>')
    script = """<pre id="out"></pre><script>addEventListener('load', () => setTimeout(() => { const r = {};
      document.querySelectorAll('iframe').forEach(f => { const doc = f.contentDocument, panel = doc.querySelector('table[width="600"]').getBoundingClientRect();
        const a = [...doc.querySelectorAll('a')].find(x => /Acknowledge for/.test(x.textContent)), b = a.getBoundingClientRect(), td = a.parentElement.getBoundingClientRect();
        const link = [...doc.querySelectorAll('a')].find(x => /^https/.test(x.textContent) && x.href.includes('/ack?')).getBoundingClientRect();
        r[f.dataset.name] = {scroll: doc.documentElement.scrollWidth, btnH: Math.round(td.height), btnW: Math.round(td.width), inside: b.left >= panel.left && b.right <= panel.right,
                             linkRight: Math.round(link.right - panel.right), panel: Math.round(panel.width)}; });
      document.getElementById('out').textContent = 'RESULT' + JSON.stringify(r) + 'END'; }, 300));</script>"""
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    for width in (390, 320):
        page = tmp_path / f"ack{width}.html"
        page.write_text(f"<!doctype html><html><body>{frames.replace('WIDTH', str(width))}{script}</body></html>")
        r = _REAL_RUN([_chrome(), "--headless=new", "--no-sandbox", "--disable-gpu", f"--user-data-dir={tmp_path}/prof{width}", "--virtual-time-budget=8000",
                       "--dump-dom", page.as_uri()], capture_output=True, text=True, timeout=90)
        m = re.search(r"RESULT(.*?)END", r.stdout, re.S)
        if not m:
            pytest.skip(f"headless Chrome produced no result (rc={r.returncode}): {r.stderr[-120:]!r}")
        res = json.loads(htmllib.unescape(m[1]))
        assert sorted(res) == sorted(names)
        for name, v in res.items():
            assert v["scroll"] <= width and v["btnH"] >= 44 and v["inside"] and v["linkRight"] <= 1 and v["btnW"] >= 160, f"{name} at {width}px: {v}"


# ------------------------------------------------------------------ the REAL acks.py (ack_core's), in the same tmp dirs
@pytest.fixture
def real_acks(monkeypatch):
    mod = pytest.importorskip("homelab_maint.acks")
    monkeypatch.setattr(notify, "acks_loader", lambda: mod)
    # The shipped severity policy is ["warn"] (crit always alerts: no button). These tests exercise the LINK machinery (token binding, the
    # site's rules, redaction) at both severities, as they would run under [ack] severities = ["warn", "crit"] or for a link minted before
    # the policy was tightened, so the key is widened here. The shipped default itself is tested in tests/test_acks.py::TestSeverityPolicy.
    monkeypatch.setitem(mod._BASE["ack"], "severities", ["warn", "crit"])
    return mod


def parse_link(url: str) -> dict:
    m = re.fullmatch(rf"{re.escape(BASE)}/ack\?id=([0-9a-f]{{16}})&t=([A-Za-z0-9_-]{{43}})&d=(\d+)&s=(warn|crit)", url)
    assert m, url
    return {"id": m[1], "t": m[2], "d": int(m[3]), "s": m[4]}


def test_the_real_acks_module_issues_the_token_the_link_carries_and_holds_the_exact_error(real_acks):
    acks_ = real_acks
    summary = "1 failed unit(s): nginx.service"
    ev = notify.alert_event("failed_units", "Services", "warn", summary)
    d, fk = go(ev, now=NOON)
    assert d.ok
    link = parse_link(fk.calls[0].ack_url)
    fp = str(acks_.fingerprint("failed_units", summary, "warn"))
    assert link["id"] == fp and link["d"] == 90 and link["s"] == "warn"
    rec = acks_.verify_token(link["t"], NOON)                              # the website's check: the token is real, unused, bound to THIS issue
    assert rec and rec["fp"] == fp and rec["severity"] == "warn" and rec["used"] is False and rec["exp"] == NOON + 30 * D
    assert link["t"] not in disk_dump(), "only the SHA-256 of the token is ever written (acks.json, ack/tokens.json, acks.jsonl, notify state, logs)"
    assert acks_.token_hash(link["t"]) in disk_dump()
    assert f"Issue ID: {fp}" in fk.calls[0].plain
    # the owner clicks it (the runner applies the request: add() with by="email"), then the same exact error is held
    info = acks_.add(fp, days=90, by="email", now=NOON + H)
    assert info.until == NOON + H + 90 * D
    d2, fk2 = go(ev, now=NOON + 2 * H)
    assert d2.skipped == "acknowledged" and fk2.calls == [] and d2.note == f"suppressed: acknowledged until {time.strftime('%Y-%m-%d', time.localtime(info.until))}"
    assert acks_.list_acks(NOON + 3 * H)[0].count_suppressed == 1          # record_suppressed() reached the real counter
    # a different failed unit is a different fingerprint: it alerts; a worse severity of the same error alerts
    other = notify.alert_event("failed_units", "Services", "warn", "1 failed unit(s): kavita.service")
    assert go(other, now=NOON + 7 * H)[0].ok                               # (7 h on: the 6 h dedupe window of the first alert, same task key, is over)
    worse = notify.alert_event("failed_units", "Services", "crit", summary)
    d3, fk3 = go(worse, now=NOON + 8 * H)
    assert d3.ok and parse_link(fk3.calls[0].ack_url)["s"] == "crit" and parse_link(fk3.calls[0].ack_url)["id"] == fp
    # the recovery of the (still acknowledged at warn) episode: the owner was paged at crit, so the all-clear goes
    assert go(notify.recovery_event("failed_units", "Services", "back", was="crit"), now=NOON + 9 * H)[0].ok


def test_the_real_acks_module_expiry_notice_end_to_end(real_acks):
    acks_ = real_acks
    summary = "1 failed unit(s): nginx.service"
    ev = notify.alert_event("failed_units", "Services", "warn", summary)
    d, fk = go(ev, now=NOON)
    fp = parse_link(fk.calls[0].ack_url)["id"]
    acks_.add(fp, days=7, by="email", now=NOON)
    assert go(ev, now=NOON + D)[0].skipped == "acknowledged"
    end = NOON + 7 * D + 1
    status = {"tasks": {"failed_units": {"status": "warn", "summary": summary, "title": "Services", "fp": fp}}}
    (core.STATE_DIR / "status.json").write_text(json.dumps(status))
    due = acks_.expire(end)
    assert due == [fp] and acks_.expire(end) == []                         # reported once
    got = notify.notify_expired(end, cfgx(), expired=due, transport=(fk2 := Fake()))
    assert len(got) == 1 and got[0].ok and got[0].channels == ["email"]
    m = fk2.calls[0]
    assert "The 7-day acknowledgement of this exact error ended on" in m.plain and "still failing" in m.plain and "Held back" in m.plain
    link = parse_link(m.ack_url)
    assert link["id"] == fp and link["s"] == "warn" and link["d"] == 90 and acks_.verify_token(link["t"], end)["fp"] == fp       # a fresh, working button
    assert link["t"] not in disk_dump()
    assert go(ev, now=end + H)[0].ok                                       # and alerting has resumed
    # the same expiry, when the task has recovered or now reports a different error: nothing to click
    acks_.add(fp, days=1, by="cli", now=end + 2 * H)
    (core.STATE_DIR / "status.json").write_text(json.dumps({"tasks": {"failed_units": {"status": "warn", "summary": "1 unhealthy: kavita", "fp": "ffffffffffffffff"}}}))
    due = acks_.expire(end + 2 * D)
    got = notify.notify_expired(end + 2 * D, cfgx(), expired=due, transport=(fk3 := Fake()))
    assert got[0].ok and fk3.calls[0].ack_url == "" and "no longer occurring" in fk3.calls[0].plain
    # acks' own one-minute job hands the same list over (_notify_expired): it must work with the real notify, send nothing real, and not raise
    assert acks_._notify_expired([], end) is True


def test_the_real_acks_module_builds_the_same_link_through_its_own_helper(real_acks):
    tok, fp = "A" * 43, "0123456789abcdef"
    assert real_acks._link(fp, tok, 90, "crit") == T.ack_url(BASE, tok, fp, 90, "crit") == f"{BASE}/ack?id={fp}&t={tok}&d=90&s=crit"


def test_with_the_real_acks_module_nothing_unacknowledged_changes_and_no_real_send_happens(real_acks):
    d = notify.send(mk("alert", "crit", task="failed_units", summary="2 failed unit(s): a.service, b.service"), cfgx(), NOON)      # no transport: refused under pytest
    assert not d.ok and "blocked under pytest" in d.note and state()["sent"] == []
    assert real_acks.is_acked(str(real_acks.fingerprint("failed_units", "2 failed unit(s): a.service, b.service", "crit")), "crit", NOON) is None


def test_with_the_real_acks_module_the_token_of_an_undelivered_email_stops_working(real_acks):
    ev = notify.alert_event("failed_units", "Services", "warn", "1 failed unit(s): nginx.service")
    d, fk = go(ev, transport=Fake(legs={"email": "failed"}, ok=False), now=NOON)
    tok = parse_link(fk.calls[0].ack_url)["t"]
    assert not d.ok and real_acks.verify_token(tok, NOON) is None             # revoked: nobody holds this link
    d, fk = go(ev, transport=Fake(), now=NOON + 8 * H)
    assert d.ok and real_acks.verify_token(parse_link(fk.calls[0].ack_url)["t"], NOON + 8 * H)       # a delivered one works


# =========================================================================== review fixes: one regression test per reported defect
# (1) expiry notices must not evict a queued critical page   (2) no button / no hold for a number-blind fingerprint
# (3) a held alert re-opens when the acknowledgement ends    (4) the button waits for the site    (5) the expiry notice's severity and days
def down() -> Fake:
    return Fake(legs={"email": "failed", "sms": "failed"}, ok=False)


def expiry_items(n: int, **over) -> list[dict]:
    return [{**ITEM, "fp": f"{i:016x}", "task": f"task_{i}", "title": f"Thing {i}", "summary": f"thing {i} is failing", "still_failing": True, **over}
            for i in range(1, n + 1)]


# ------------------------------------------------------------------ (1) the outbox is shared storage, not shared capacity
def test_a_flood_of_expiry_notices_never_evicts_a_queued_critical_page(acks):
    crit = mk("alert", "crit", title="Docker disk", summary="docker_df is full", task="docker_df")
    d, _ = go(crit, transport=down())
    assert d.queued and [e["kind"] for e in state()["outbox"]] == ["alert"]
    # the reviewer's reproduction: grouping off, twelve notices, the mail path still down, a small notice class
    cfg = cfgx(ack={"notice_group_over": 50}, retry={"notices_max": 3})
    out = notify.send_expired(expiry_items(12), cfg, NOON + 60, transport=down())
    kinds = [e["kind"] for e in state()["outbox"]]
    assert len(out) == 12 and all(x.queued for x in out)
    assert kinds.count("alert") == 1 and kinds.count("ack_expired") == 3, kinds        # the page is still there; only the notices were squeezed
    assert sum(1 for r in audit_rows() if r["action"] == "outbox-overflow") == 9 and not any("Docker" in r["target"] for r in audit_rows() if r["action"] == "outbox-overflow")
    # the mail path returns: the PAGE is replayed first, then the notices, and nothing is left
    fk = Fake()
    got = notify.flush_pending(cfgx(), NOON + 7200, transport=fk)
    assert [x.kind for x in got][0] == "alert" and sorted({x.kind for x in got}) == ["ack_expired", "alert"]
    assert "Docker disk" in fk.calls[0].subject and not state().get("outbox")


def test_a_flood_of_warnings_or_recoveries_cannot_push_a_critical_page_out_either(acks):
    notify.enqueue(mk("alert", "crit", title="Plex mount", task="plex_media_mount_check"), cfgx(), NOON)
    for i in range(15):                                                           # HermesNotifier(defer=True) queues every alert it sends
        notify.enqueue(mk("alert", "warn", title=f"Warn {i}", task=f"w{i}", dedupe_key=f"w{i}"), cfgx(), NOON + i + 1)
    box = state()["outbox"]
    assert len(box) == 10 and box[0]["snap"]["title"] == "Plex mount" and box[0]["snap"]["sev"] == "crit"       # outbox_max still holds; the oldest WARNINGS went
    assert [e["snap"]["title"] for e in box[1:]] == [f"Warn {i}" for i in range(6, 15)]
    for i in range(12):                                                           # and a box full of criticals still keeps the newest ten of them
        notify.enqueue(mk("alert", "crit", title=f"Crit {i}", task=f"c{i}", dedupe_key=f"c{i}"), cfgx(), NOON + 100 + i)
    assert [e["snap"]["sev"] for e in state()["outbox"]] == ["crit"] * 10 and state()["outbox"][-1]["snap"]["title"] == "Crit 11"


def test_the_two_outbox_classes_are_capped_independently_and_a_notice_replay_never_blocks_a_page(acks):
    cfg = cfgx(ack={"notice_group_over": 50}, retry={"outbox_max": 2, "notices_max": 4})
    notify.send_expired(expiry_items(6), cfg, NOON, transport=down())
    for i in range(3):
        d, _ = go(mk("alert", "crit", title=f"Page {i}", task=f"p{i}", dedupe_key=f"p{i}"), cfg=cfg, transport=down(), now=NOON + 10 + i)
        assert d.queued
    titles = [(e["kind"], e["snap"]["title"]) for e in state()["outbox"]]
    assert [t for k, t in titles if k == "alert"] == ["Page 1", "Page 2"] and len([1 for k, _t in titles if k == "ack_expired"]) == 4
    fk = Fake()
    notify.flush_pending(cfg, NOON + 4000, transport=fk)                           # pages first, whatever order they were queued in
    assert [m.subject for m in fk.calls][:2] == ["[homelab] CRIT Page 1", "[homelab] CRIT Page 2"] or all("Page" in m.subject for m in fk.calls[:2])


def test_doctor_and_export_do_not_call_a_queued_notice_a_critical_page(acks):
    notify.send_expired(expiry_items(1), cfgx(), NOON, transport=down())
    rows = {label: (ok, hint) for label, ok, hint in notify.doctor(cfgx())}
    assert rows["no critical page waiting in the outbox"][0] is True
    w = notify.export(NOON)["waiting"]
    assert w == {"pages": 0, "texts": 0, "notices": 1}


# ------------------------------------------------------------------ (1b) acknowledgements made together end together: ONE email
def test_more_than_two_expiries_in_one_run_are_one_email_that_lists_every_issue_id(acks):
    items = expiry_items(12)
    items[3]["still_failing"] = False
    fk = Fake()
    out = notify.send_expired(items, cfgx(), NOON, transport=fk)
    assert len(out) == 1 and out[0].ok and out[0].channels == ["email"] and len(fk.calls) == 1
    m = fk.calls[0]
    assert m.subject.startswith("[homelab] Ack expired: 12 acknowledgements ended") and m.ack_url == "" and acks.tokens == []          # one token binds one issue: none here
    assert "12 acknowledgements ended" in m.plain and "11 still failing, 1 no longer occurring" in m.plain and "Ended 2026-10-02:" in m.plain
    for it in items:
        assert f"Issue ID {it['fp']}" in m.plain and it["title"] in m.plain and it["fp"] in m.html
    assert "homelab-maint ack add <Issue ID>" in m.plain and "Acknowledge for" not in m.html and "/ack?" not in m.html
    assert audit_html(m.html) and len(state()["sent"]) == 1                        # one email, one slice of the daily budget (not twelve)
    again = notify.send_expired(items, cfgx(), NOON + H, transport=fk)
    assert again[0].skipped == "dedupe" and len(fk.calls) == 1                     # the same group is not sent twice


@pytest.mark.parametrize("n,grouped", [(1, False), (2, False), (3, True)])
def test_two_notices_stay_individual_three_become_one(acks, n, grouped):
    fk = Fake()
    out = notify.send_expired(expiry_items(n), cfgx(), NOON, transport=fk)
    assert len(out) == (1 if grouped else n) and len(fk.calls) == len(out)
    assert bool(fk.calls[0].ack_url) is (not grouped)                              # an individual notice keeps its fresh button


def test_the_grouped_notice_names_at_most_twelve_and_counts_the_rest_and_a_failed_one_is_queued_once(acks):
    out = notify.send_expired(expiry_items(40), cfgx(), NOON, transport=down())
    assert len(out) == 1 and out[0].queued and len(state()["outbox"]) == 1
    fk = Fake()
    notify.flush_pending(cfgx(), NOON + 400, transport=fk)
    assert len(fk.calls) == 1 and "+28 more" in fk.calls[0].plain and fk.calls[0].plain.count("Issue ID ") == 12
    ev = notify.ack_expired_group_event(expiry_items(3, severity="crit"))
    assert ev.severity == "crit" and ev.dedupe_key.startswith("ack-expired-group-") and re.fullmatch(r"[a-p]{12}", ev.dedupe_key[-12:])
    assert notify.ack_expired_group_event(expiry_items(3, still_failing=False)).severity == "ok"


# ------------------------------------------------------------------ (2) only an alert that names THE ERROR may be acknowledged
NUMBER_BLIND = [("smart_event", "Device: /dev/sda [SAT], 8 Currently unreadable (pending) sectors", "Device: /dev/sda [SAT], 8000 Currently unreadable (pending) sectors"),
                ("job:backup", "backup failed, exit code 1", "backup failed, exit code 137"),
                ("surrealdb_health", "surrealdb: WAL 900 MB", "surrealdb: WAL 9000 MB"),
                ("disk_trend_x", "/ 4% free", "/ 1% free")]


@pytest.mark.parametrize("task,before,after", NUMBER_BLIND)
def test_a_task_without_an_exact_error_key_is_neither_offered_a_button_nor_ever_held(acks, task, before, after):
    acks.mode = "text"                                                             # the number-blind fallback made this fingerprint
    ev, worse = mk("alert", "crit", task=task, summary=before), mk("alert", "crit", task=task, summary=after)
    assert fp_of(acks, ev) == fp_of(acks, worse)                                  # the hazard: the same fingerprint at 8 and at 8000
    acks.ack(ev, sev="crit")                                                       # acknowledged (say from the CLI) while the problem was small
    d, fk = go(ev)
    m = fk.calls[0]
    assert d.ok and not d.skipped and m.ack_url == "" and "Acknowledge" not in m.html and "Issue ID" not in m.plain and acks.tokens == []
    d2, fk2 = go(worse, now=NOON + 7 * H)                                          # ... and when it got 1000x worse the owner is told
    assert d2.ok and d2.skipped == "" and fk2.calls and acks.suppressed == [] and "ackfp" not in state()


def test_the_exact_hazard_end_to_end_with_the_real_acks_module(real_acks):
    acks_ = real_acks
    pending = "Device: /dev/sda [SAT], 8 Currently unreadable (pending) sectors"
    entry = {"status": "crit", "summary": pending, "title": "SMART", "alert": True}
    (core.STATE_DIR / "status.json").write_text(json.dumps({"tasks": {"smart_event": entry}}))
    fp = str(acks_.fingerprint("smart_event", entry, "crit"))
    with pytest.raises(acks_.AckError):                                           # acks.py now refuses a denied task at the door ...
        acks_.add(fp, days=90, by="cli", now=NOON)
    with acks_._txn(NOON) as store:                                               # ... so the hazard is a record written BEFORE that policy existed (an old store)
        acks_._put_ack(store, fp, {"task": "smart_event", "title": "SMART", "summary": pending, "severity": "crit", "issue_key": "", "mode": "text"}, 90, "", "cli", NOON)
    assert acks_.fingerprint("smart_event", pending.replace("8", "8000"), "crit") == fp        # (the same fingerprint at 8 and at 8000 sectors)
    d, fk = go(notify.Event("alert", "crit", "SMART on host", pending.replace("8", "8000"), None, {"device": "/dev/sda"}, None, "smart:/dev/sda:x", "smart_event"), now=NOON + H)
    assert d.ok and not d.skipped and fk.calls and fk.calls[0].ack_url == ""      # it went out: before the fix this was `skipped = acknowledged` for 90 days
    assert acks_.list_acks(NOON + 2 * H)[0].count_suppressed == 0


@pytest.mark.parametrize("mode,rules,over,ackable", [
    ("explicit", {}, {}, True),                                                     # Result.issue_key
    ("regex", {}, {}, True),                                                        # a per-task rule that matched
    ("task", {}, {}, True),
    ("text", {"smart_trend": {"mode": "text", "sort": True}}, {}, True),            # a deliberate [key.<task>] text rule
    ("text", {}, {}, False),                                                        # the fallback
    ("", {}, {}, False),                                                            # a module that does not say how: a doubt
    ("text", {}, {"allow_tasks": ["smart_trend"]}, True),                           # opted in on purpose
    ("regex", {}, {"deny_tasks": ["smart_trend"]}, False),                          # the deny list beats the rules
    ("text", {}, {"allow_tasks": ["smart_trend"], "deny_prefixes": ["smart_"]}, False),    # ... and the allow list
    ("text", {}, {"require_rule": False}, True),                                    # the owner lifts the policy for everything
    ("text", {}, {"require_rule": "no"}, False),                                    # only a real `false` does
])
def test_which_tasks_may_be_acknowledged(acks, mode, rules, over, ackable):
    acks.mode, acks.rules = mode, rules
    ev = mk("alert", "warn", task="smart_trend", summary="SMART: sda +2")
    cfg = cfgx(ack=over)
    acks.ack(ev, sev="warn")
    d, fk = go(ev, cfg=cfg)
    assert (d.skipped == "acknowledged") is ackable                                # held exactly when it may be acknowledged
    if not ackable:
        assert fk.calls[0].ack_url == "" and "Issue ID" not in fk.calls[0].plain
    d, fk = go(mk("alert", "warn", task="smart_trend", summary="SMART: sdb +1"), cfg=cfg, now=NOON + 8 * H)      # an unacknowledged one: the button follows the same rule
    assert bool(fk.calls[0].ack_url) is ackable and len(acks.tokens) == int(ackable)


def test_deny_prefix_and_task_lists_are_clamped_and_the_defaults_name_the_hooks():
    c = notify.ack_cfg(notify.load_config())
    assert c["deny"] == ("smart_event",) and c["deny_prefix"] == ("job:",) and c["require_rule"] is True and c["allow"] == ()
    c = notify.ack_cfg(notify.load_config(cfgx(ack={"deny_tasks": "a, b", "allow_tasks": None, "deny_prefixes": ["x:", 5], "notice_group_over": 9999})))
    assert c["deny"] == ("a", "b") and c["allow"] == () and c["deny_prefix"] == ("x:", "5") and c["group_over"] == 50
    assert notify.ack_cfg(notify.load_config(cfgx(ack={"notice_group_over": 0})))["group_over"] == 1


def test_hermes_notifier_supplies_a_fingerprint_only_for_a_task_that_names_its_error(acks, monkeypatch):
    acks.mode = "text"
    assert notify._result_fp("surrealdb_health", core.Result("crit", "surrealdb: WAL 900 MB"), 2, notify.load_config(cfgx())) == ""
    assert notify._result_fp("surrealdb_health", core.Result("crit", "x"), 0, notify.load_config(cfgx())) == ""
    acks.mode = "explicit"
    assert len(notify._result_fp("surrealdb_health", core.Result("crit", "x"), 2, notify.load_config(cfgx()))) == 16
    assert notify._result_fp("job:backup", core.Result("crit", "x"), 2, notify.load_config(cfgx())) == ""         # denied whatever the Result says
    fk = Fake()
    monkeypatch.setattr(notify, "hermes_transport", fk)
    acks.mode = "text"
    acks.acks[acks.fingerprint("surrealdb_health", "surrealdb: WAL 900 MB", "crit")] = {"until": NOON + 90 * D, "sev": "crit"}
    for t in (NOON, NOON + 900):
        nt = notify.HermesNotifier(cfgx())
        nt.evaluate("surrealdb_health", "SurrealDB", core.Result("crit", "surrealdb: WAL 9000 MB"), t)
        nt.save()
    assert len(fk.calls) == 1 and fk.calls[0].ack_url == "" and acks.suppressed == []        # the worse WAL paged, with no button


# ------------------------------------------------------------------ (3) a held alert is not remembered as "already sent"
class Runs:
    """HermesNotifier runs against alerts.json, the way cli.cmd_run drives them (one notifier per run, saved after)."""

    def __init__(self, monkeypatch, defer=False):
        self.fk, self.defer = Fake(), defer
        monkeypatch.setattr(notify, "hermes_transport", self.fk)

    def __call__(self, t, status="crit", summary="/ is 4% free"):
        nt = notify.HermesNotifier(cfgx(), defer=self.defer)
        nt.evaluate("disk_forecast", "Disk space", core.Result(status, summary), t)
        nt.save()
        if self.defer:
            nt.deliver(t)
        return json.loads((core.STATE_DIR / "alerts.json").read_text())["tasks"]["disk_forecast"]


@pytest.mark.parametrize("defer", [False, True])
@pytest.mark.parametrize("how", ["unack", "expiry", "module_gone"])
def test_alerting_resumes_at_the_next_run_when_the_acknowledgement_ends(acks, monkeypatch, defer, how):
    run = Runs(monkeypatch, defer)
    fp = acks.fingerprint("disk_forecast", "/ is 4% free", "crit")
    run(NOON)
    run(NOON + 900)                                                                  # confirmed: the first page
    assert len(run.fk.calls) == 1
    acks.acks[fp] = {"until": NOON + (26 * H - 300 if how == "expiry" else 90 * D), "sev": "crit"}
    st = run(NOON + 25 * H)                                                          # the 24 h reminder: held, and core thinks it was sent
    assert len(run.fk.calls) == 1 and st["alerted"] == 2 and st["last_sent"] == NOON + 25 * H and state()["ackfp"]["disk_forecast"]["held"] == 1
    assert acks.suppressed == [fp]
    run(NOON + 25 * H + 900)                                                         # still acknowledged: still held, nothing re-opened
    assert len(run.fk.calls) == 1
    if how == "unack":
        acks.acks.clear()                                                            # the owner un-acknowledges (CLI or site)
    elif how == "module_gone":
        monkeypatch.setattr(notify, "acks_loader", lambda: None)
    run(NOON + 26 * H)                                                               # the very next check run (before the fix: silence until +49 h)
    assert len(run.fk.calls) == 2 and set(run.fk.calls[1].channels) == {"sms", "email"}
    assert "held" not in state().get("ackfp", {}).get("disk_forecast", {}) and any(r["action"] == "ack-released" for r in audit_rows())
    run(NOON + 26 * H + 900)
    assert len(run.fk.calls) == 2                                                    # and once, not every run


def test_un_acknowledging_inside_the_alert_dedupe_window_pages_at_once(acks, monkeypatch):
    """The verifier saw 'same alert/crit ... 0 min ago (window 360 min)': the page that went out shortly BEFORE the acknowledgement (a reminder interval or a flap
    shorter than the 6 h window) swallowed the page that the un-acknowledge re-opens. Normal alerting resumes at once, like the runbook says."""
    run = Runs(monkeypatch)
    fp = acks.fingerprint("disk_forecast", "/ is 4% free", "crit")
    run(NOON)
    run(NOON + 900)                                                                  # the first page
    acks.acks[fp] = {"until": NOON + 90 * D, "sev": "crit"}
    run(NOON + 25 * H)                                                               # held reminder
    assert len(run.fk.calls) == 1 and state()["ackfp"]["disk_forecast"]["held"] == 1
    with notify._state() as st:
        st["dedupe"]["alert|disk_forecast"] = {"ts": NOON + 25 * H, "sev": "crit"}   # a page of the same severity 5 minutes ago (what a short alert_reminder_hours would leave)
    acks.acks.clear()                                                                # un-acknowledge
    run(NOON + 25 * H + 300)
    assert len(run.fk.calls) == 2 and set(run.fk.calls[1].channels) == {"sms", "email"}, "deduped: the un-acknowledge did not page"
    run(NOON + 25 * H + 600)
    assert len(run.fk.calls) == 2                                                    # once, and the normal window applies again from here


def test_an_acknowledgement_that_never_held_anything_re_opens_nothing(acks, monkeypatch):
    run = Runs(monkeypatch)
    fp = acks.fingerprint("disk_forecast", "/ is 4% free", "crit")
    run(NOON)
    run(NOON + 900)
    acks.acks[fp] = {"until": NOON + 90 * D, "sev": "crit"}
    run(NOON + 2 * H)
    acks.acks.clear()                                                                # acknowledged and un-acknowledged between two runs, nothing held
    st = run(NOON + 3 * H)
    assert len(run.fk.calls) == 1 and st["alerted"] == 2 and st["last_sent"] == NOON + 900            # the normal reminder cadence is untouched


def test_an_escalation_while_held_alerts_through_the_normal_path_and_the_task_that_recovered_is_not_re_opened(acks, monkeypatch):
    run = Runs(monkeypatch)
    fp = acks.fingerprint("disk_forecast", "/ is 4% free", "warn")
    run(NOON, "warn")
    run(NOON + 900, "warn")
    acks.acks[fp] = {"until": NOON + 90 * D, "sev": "warn"}
    run(NOON + 25 * H, "warn")                                                       # held reminder
    assert len(run.fk.calls) == 1
    run(NOON + 26 * H, "crit")
    run(NOON + 26 * H + 900, "crit")                                                 # worse than acknowledged: the ceiling does it, no re-open needed
    assert len(run.fk.calls) == 2 and run.fk.calls[1].ack_url.endswith("&s=crit")
    acks.acks.clear()
    st = run(NOON + 30 * H, "ok")                                                    # recovering while the hold was released: no spurious "alerted = 0"
    st = run(NOON + 30 * H + 900, "ok")
    assert st["alerted"] == 0 and len(run.fk.calls) == 3 and run.fk.calls[2].subject.startswith("[homelab] OK")


# ------------------------------------------------------------------ (3b) a worsening at the SAME severity is another error: alert at once
def test_a_changed_fingerprint_at_the_same_severity_alerts_at_once_but_a_volatile_number_does_not(acks, monkeypatch):
    run = Runs(monkeypatch)
    fp = acks.fingerprint("disk_forecast", "/ is 4% free", "crit")
    run(NOON)
    run(NOON + 900)                                                                  # the first page
    acks.acks[fp] = {"until": NOON + 90 * D, "sev": "crit"}
    run(NOON + 25 * H)                                                               # the 24 h reminder: held (core counts it as sent)
    run(NOON + 25 * H + 900, summary="/ is 3% free")                                 # a number moved: the SAME error, still held
    assert len(run.fk.calls) == 1 and state()["ackfp"]["disk_forecast"]["held"] == 1
    run(NOON + 26 * H, summary="/ and /mnt/archive are 3% free")                     # another error (a longer list), same severity: before the fix it waited for +49 h
    assert len(run.fk.calls) == 2 and "held" not in state().get("ackfp", {}).get("disk_forecast", {})
    assert any(r["action"] == "ack-released" for r in audit_rows())
    run(NOON + 26 * H + 900, summary="/ and /mnt/archive are 2% free")
    assert len(run.fk.calls) == 2                                                    # once, not every run


def test_hold_released_compares_the_current_fingerprint_only_when_it_has_one(acks):
    fp = acks.fingerprint("disk_forecast", "/ is 4% free", "crit")
    acks.acks[fp] = {"until": NOON + 90 * D, "sev": "crit"}
    go(mk(), now=NOON)                                                               # held: leaves ackfp[key] = {fp, sev, held}
    nc = notify.load_config(cfgx())
    assert state()["ackfp"]["disk_forecast"]["held"] == 1
    assert notify._hold_released("disk_forecast", nc, NOON + H) is False             # no current fingerprint: nothing to compare, still in force
    assert notify._hold_released("disk_forecast", nc, NOON + H, fp) is False         # the same error
    assert notify._hold_released("disk_forecast", nc, NOON + H, "0" * 16) is True    # another one
    assert notify._hold_released("never_held", nc, NOON + H, "0" * 16) is False      # nothing was held under that key


# ------------------------------------------------------------------ (3c) one policy: acks.ackable decides, for the pager too
class AckableAcks(FakeAcks):
    """A module that has the single policy: its answer beats whatever notify.toml says."""

    def __init__(self, allowed):
        super().__init__()
        self.allowed, self.asked = allowed, []

    def ackable(self, task, fp_or_mode=""):
        self.asked.append((task, getattr(fp_or_mode, "mode", fp_or_mode)))
        return task in self.allowed


def test_ack_allowed_delegates_to_acks_ackable_so_the_pager_and_the_dashboard_cannot_disagree(monkeypatch):
    fa = AckableAcks({"failed_units"})
    monkeypatch.setattr(notify, "acks_loader", lambda: fa)
    deny_here = cfgx(ack={"deny_tasks": ["failed_units"], "allow_tasks": ["worker"], "require_rule": True})   # the deprecated notify.toml copy says the opposite
    ack = notify.ack_cfg(notify.load_config(deny_here))
    fo = fa.fingerprint("failed_units", "1 failed unit: a", "warn")
    assert notify._ack_allowed(fa, "failed_units", fo, ack) is True and notify._ack_allowed(fa, "worker", fo, ack) is False
    assert fa.asked[0] == ("failed_units", "regex")                                  # it is asked with the Fp (its mode travels)
    ev = mk("alert", "warn", task="failed_units", summary="1 failed unit: a.service")
    d, fk = go(ev, cfg=deny_here)
    assert fk.calls[0].ack_url and f"Issue ID: {fp_of(fa, ev)}" in fk.calls[0].plain  # ack.toml (here: the module) allows it: button, whatever notify.toml says
    fa.ack(ev, sev="warn")
    assert go(ev, cfg=deny_here, now=NOON + H)[0].skipped == "acknowledged"
    w = mk("alert", "warn", task="worker", summary="worker-1 failed")
    d2, fk2 = go(w, cfg=deny_here, now=NOON + 2 * H)
    assert fk2.calls[0].ack_url == "" and "Issue ID" not in fk2.calls[0].plain       # the module refuses: no button, no id, even though notify.toml allowed it


def test_a_module_without_ackable_keeps_the_notify_toml_fallback(acks):
    assert not hasattr(acks, "ackable")
    ack = notify.ack_cfg(notify.load_config(cfgx(ack={"allow_tasks": ["worker"]})))
    assert notify._ack_allowed(acks, "worker", acks.fingerprint("worker", "x", "warn"), ack) is True
    assert notify._ack_allowed(acks, "", "", ack) is False


def test_with_the_real_module_an_allow_tasks_entry_in_ack_toml_alone_offers_the_button_and_holds(real_acks):
    """The reproduction of the old split: allow_tasks in ack.toml only made the site say "acknowledged" while the alert still paged."""
    acks_ = real_acks
    (core.CONF_DIR / "ack.toml").write_text('[ack]\nallow_tasks = ["worker"]\n')
    ev = notify.alert_event("worker", "Worker", "warn", "worker-1 failed")
    assert not tomllib.loads((ROOT / "etc" / "notify.toml").read_text())["ack"]["allow_tasks"]       # notify.toml has no such entry
    d, fk = go(ev, now=NOON)
    fp = str(acks_.fingerprint("worker", "worker-1 failed", "warn"))
    assert fk.calls[0].ack_url and f"Issue ID: {fp}" in fk.calls[0].plain
    acks_.add(fp, days=90, by="cli", now=NOON + H)
    d2, fk2 = go(ev, now=NOON + 2 * H)
    assert d2.skipped == "acknowledged" and fk2.calls == []                          # the pager follows the dashboard
    (core.CONF_DIR / "ack.toml").write_text("")
    assert go(ev, now=NOON + 30 * H)[1].calls[0].ack_url == ""                     # and the policy change is followed too: no rule, no button


# ------------------------------------------------------------------ (3d) the token is issued with the fingerprint's mode
def test_issue_token_gets_the_mode_of_the_fingerprint(acks, monkeypatch):
    acks.mode = "regex"
    go(mk("alert", "crit", task="failed_units", summary="1 failed unit: a.service"))
    assert acks.tokens[-1]["mode"] == "regex"                                        # recomputed from the summary
    acks.mode = "explicit"                                                           # a task with Result.issue_key: HermesNotifier knows it, the summary does not
    run = Runs(monkeypatch, defer=True)
    run(NOON)
    run(NOON + 900)
    assert acks.tokens[-1]["mode"] == "explicit" and acks.tokens[-1]["task"] == "disk_forecast"
    assert "ack_mode" not in " ".join(c.plain + c.html for c in run.fk.calls)         # a reserved fact: never printed as a row


# ------------------------------------------------------------------ (4) a button that opens a 404 is worse than none
def test_the_button_waits_for_the_site_and_no_token_is_minted_before_that(acks):
    shipped = tomllib.loads((ROOT / "etc" / "notify.toml").read_text())["ack"]
    assert shipped["button"] == "auto" and notify.DEFAULTS["ack"]["button"] == "auto"
    bare = {"notify": {"site": {"host_label": "testhost"}, "ack": {"mint_url": ""}}}   # an old /etc copy without [ack]; local mint (FakeAcks), not the hub
    ev = mk("alert", "crit", task="failed_units", summary="1 failed unit: a.service")
    fp = fp_of(acks, ev)
    d, fk = go(ev, cfg=bare)
    m = fk.calls[0]
    assert d.ok and m.ack_url == "" and "/ack?" not in m.plain + m.html and "Acknowledge" not in m.html and "nothing changes until you confirm" not in m.plain
    assert f"Issue ID: {fp}" in m.plain and acks.tokens == [] and not (core.STATE_DIR / "ack").exists()           # the text-only Issue ID, no token, no files
    assert [r for r in doctor_rows(bare) if r[0].startswith("acknowledge button")][0][0] == "acknowledge button: off until the site serves /ack"
    (core.STATE_DIR / "ack").mkdir()
    (core.STATE_DIR / "ack" / "web_ready").write_text("")                             # ack_web deployed
    d, fk = go(ev, cfg=bare, now=NOON + 7 * H)
    assert fk.calls[0].ack_url.startswith(f"{BASE}/ack?id={fp}&t=") and len(acks.tokens) == 1
    assert [r for r in doctor_rows(bare) if r[0].startswith("acknowledge button")][0][0] == "acknowledge button: on"
    for i, (val, expect) in enumerate(((False, False), ("false", False), ("typo", False), (True, True), ("auto", True))):
        d, fk = go(mk("alert", "crit", task=f"failed_units_{i}", summary=f"1 failed unit: {val}"), cfg=cfgx(ack={"button": val}), now=NOON + 20 * H)
        assert bool(fk.calls[0].ack_url) is expect, val
    (core.STATE_DIR / "ack" / "web_ready").unlink()
    d, fk = go(mk("alert", "crit", task="failed_units", summary="1 failed unit: forced"), cfg=cfgx(ack={"button": True}), now=NOON + 90 * H)
    assert fk.calls[0].ack_url                                                        # `true` does not wait for the marker


def doctor_rows(cfg):
    return notify.doctor(cfg)


def test_held_alerts_still_work_while_the_button_is_off_and_the_expiry_notice_words_its_todo_without_a_button(acks):
    ev = mk("alert", "warn", task="failed_units", summary="1 failed unit: a.service")
    acks.ack(ev, sev="warn")                                                          # an acknowledgement made from the CLI
    bare = {"notify": {"site": {"host_label": "testhost"}}}
    assert go(ev, cfg=bare)[0].skipped == "acknowledged"
    item = {**ITEM, "still_failing": True}
    fk = Fake()
    out = notify.send_expired([item], bare, NOON, transport=fk)
    m = fk.calls[0]
    assert out[0].ok and m.ack_url == "" and "with the button above" not in m.plain + m.html
    assert f"acknowledge it again for another 90 days with: homelab-maint ack add {ITEM['fp']}" in m.plain and acks.tokens == []
    assert "with the button above" in notify.ack_expired_event(item).details["todo"][0]


# ------------------------------------------------------------------ (5) the expiry notice describes the issue as it is NOW
def test_the_expiry_notice_uses_the_current_severity_and_the_days_the_button_promises(acks):
    rec = {**ITEM, "severity": "warn", "acked_at": NOON - D, "until": NOON}          # a 1-day WARN acknowledgement ...
    status = {"tasks": {"failed_units": {"status": "crit", "summary": "1 failed unit: nginx.service", "alert": True, "fp": ITEM["fp"]}}}      # ... of an issue that is CRIT now
    items = notify.expired_items([rec], NOON, status)
    assert items[0]["still_failing"] is True and items[0]["now_severity"] == "crit" and items[0]["severity"] == "warn"
    fk = Fake()
    out = notify.send_expired(items, cfgx(), NOON, transport=fk)
    m = fk.calls[0]
    assert out[0].ok and m.ack_url.endswith("&d=90&s=crit") and acks.tokens[0]["severity"] == "crit"                     # the link covers what is failing
    assert "Now: still failing (crit), acknowledged as warn" in m.plain and "Acknowledged: 1 day, by email" in m.plain
    assert "another 90 days with the button above" in m.plain and "another 1 days" not in m.plain and "Acknowledge for 90 days" in m.html
    assert "The 1-day acknowledgement of this exact error ended" in m.plain          # (the ended one keeps its own length)
    assert notify.ack_expired_event(items[0]).severity == "crit"


@pytest.mark.parametrize("acked,entry,now_sev,want", [
    ("crit", "warn", "warn", "warn"),                                                 # it got better: the notice says warn, the link covers warn
    ("warn", None, None, "warn"),                                                     # the entry is gone from status: the record is all there is
    ("warn", "ok", None, None),                                                       # recovered: nothing to click
])
def test_the_expiry_notice_falls_back_to_the_record_and_tells_a_recovery(acks, acked, entry, now_sev, want):
    tasks = {} if entry is None else {"failed_units": {"status": entry, "summary": "1 failed unit: nginx.service", "alert": True, "fp": ITEM["fp"]}}
    items = notify.expired_items([{**ITEM, "severity": acked, "still_failing": True if entry is None else None}], NOON, {"tasks": tasks})
    assert items[0].get("now_severity") == now_sev
    fk = Fake()
    notify.send_expired(items, cfgx(), NOON, transport=fk)
    if want:
        assert fk.calls[0].ack_url.endswith(f"&s={want}") and f"still failing ({want})" in fk.calls[0].plain
    else:
        assert fk.calls[0].ack_url == "" and "no longer occurring" in fk.calls[0].plain


def test_the_release_works_against_the_real_acks_module_un_acknowledge_and_expiry(real_acks, monkeypatch):
    acks_ = real_acks
    fk = Fake()
    monkeypatch.setattr(notify, "hermes_transport", fk)
    summary = "1 failed unit(s): nginx.service"
    entry = {"status": "warn", "summary": summary, "title": "Services", "alert": True}
    (core.STATE_DIR / "status.json").write_text(json.dumps({"tasks": {"failed_units": entry}}))

    def run(t):
        nt = notify.HermesNotifier(cfgx(), defer=True)
        nt.evaluate("failed_units", "Services", core.Result("warn", summary), t)
        nt.save()
        nt.deliver(t)
    run(NOON)
    run(NOON + 900)
    assert len(fk.calls) == 1
    fp = parse_link(fk.calls[0].ack_url)["id"]
    acks_.add(fp, days=7, by="cli", now=NOON + 1000)
    run(NOON + 25 * H)                                                              # the reminder is held
    assert len(fk.calls) == 1 and state()["ackfp"]["failed_units"]["held"] == 1
    assert acks_.remove(fp, by="web", now=NOON + 26 * H)                            # un-acknowledged on the site
    run(NOON + 26 * H + 900)
    assert len(fk.calls) == 2                                                       # the next check run alerts again, not the next reminder
    acks_.add(fp, days=1, by="cli", now=NOON + 27 * H)
    run(NOON + 50 * H + 1000)                                                       # held again (a 1-day acknowledgement, still in force for 50 minutes)
    assert len(fk.calls) == 2
    assert acks_.expire(NOON + 52 * H) == [fp]
    run(NOON + 52 * H + 900)                                                        # it has ended: alerts again
    assert len(fk.calls) == 3


def test_a_run_that_dies_before_saving_alerts_json_re_opens_the_alert_again_and_still_sends_it_once(acks, monkeypatch):
    run = Runs(monkeypatch, defer=True)
    fp = acks.fingerprint("disk_forecast", "/ is 4% free", "crit")
    run(NOON)
    run(NOON + 900)
    acks.acks[fp] = {"until": NOON + 90 * D, "sev": "crit"}
    run(NOON + 25 * H)                                                              # held
    acks.acks.clear()
    nt = notify.HermesNotifier(cfgx(), defer=True)                                  # the run that is killed after evaluate(), before save()/deliver()
    nt.evaluate("disk_forecast", "Disk space", core.Result("crit", "/ is 4% free"), NOON + 26 * H)
    assert state()["ackfp"]["disk_forecast"]["held"] == 1 and len(state()["outbox"]) == 1 and len(run.fk.calls) == 1
    run(NOON + 26 * H + 900)                                                        # the next run re-opens it again (the mark was not cleared) ...
    assert len(run.fk.calls) == 2 and not state().get("outbox")                     # ... and the page goes out once, not twice
    run(NOON + 27 * H)
    assert len(run.fk.calls) == 2 and "held" not in state().get("ackfp", {}).get("disk_forecast", {})


def test_a_dry_run_of_a_task_that_may_not_be_acknowledged_shows_no_button_like_the_real_email(acks):
    acks.mode = "text"
    d, _fk = go(mk("alert", "crit", task="surrealdb_health", summary="surrealdb: WAL 900 MB"), dry_run=True)
    assert "Acknowledge for" not in d.rendered["html"] and "/ack?" not in d.rendered["plain"]
    acks.mode = "regex"
    d, _fk = go(mk("alert", "crit", task="failed_units", summary="1 failed unit: a.service"), dry_run=True)
    assert "Acknowledge for 90 days" in d.rendered["html"] and acks.tokens == []        # the ackable one still previews the inert button


# =========================================================================== link_sync (SPEC5 S8): ONE chain, three owners
# notify builds the link -> the SITE parses it (GET /ack, POST /api/ack/confirm) -> the token hash resolves in ack/tokens.json (the
# container's verify-only view) -> the signed inbox request is applied by the runner (acks.process_inbox) -> the exact error is held.
# `website_*` is the other side of the link, written from SPEC5 S8 and deliberately NOT built from notify's own regexes
# (notify_templates._ACK_URL_RE): if the two ever disagree, this chain breaks. Everything runs in the per-test tmp dirs with the REAL
# acks.py and a fake transport: nothing is sent, nothing live is touched.
SITE_HOST = "maintainer.ohmzhomelab.ca"
WEB_ID, WEB_TOKEN, WEB_NUM = re.compile(r"[0-9a-f]{16}"), re.compile(r"[A-Za-z0-9_-]{43}"), re.compile(r"[0-9]{1,3}")
WEB_DAYS, WEB_SEV = (7, 30, 90, 365), ("warn", "crit")


def website_parse(url: str, max_days: int = 365) -> dict:
    """`GET /ack?id=&t=&d=&s=` as the site validates it: https on the site's own host, path exactly /ack, no fragment or credentials, EXACTLY the
    four parameters once each and no percent-escapes, each one fullmatching its charset/length/enum (id 16 hex, t 43 urlsafe chars, d one
    of 7/30/90/365 and not above max_days, s warn|crit). Raises ValueError."""
    u = urllib.parse.urlsplit(url)
    if (u.scheme, u.hostname, u.port, u.path, u.fragment, u.username) != ("https", SITE_HOST, None, "/ack", "", None) or "%" in u.query or "+" in u.query:
        raise ValueError("origin/path")
    q: dict[str, str] = {}
    for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True, strict_parsing=True):
        if k in q:
            raise ValueError("duplicate parameter")
        q[k] = v
    if set(q) != {"id", "t", "d", "s"}:
        raise ValueError("parameters")
    if not (WEB_ID.fullmatch(q["id"]) and WEB_TOKEN.fullmatch(q["t"]) and WEB_NUM.fullmatch(q["d"]) and q["s"] in WEB_SEV):
        raise ValueError("charset/length/enum")
    d = int(q["d"])
    if d not in WEB_DAYS or d > max_days:
        raise ValueError("days")
    return {"id": q["id"], "t": q["t"], "d": d, "s": q["s"]}


def website_lookup(p: dict, tokens_file: Path, now: float) -> dict:
    """The verify-only lookup the site does in ack/tokens.json: sha256(t), constant-time over the keys; the record must be unused, unexpired and
    bound to THIS id and severity. One answer for unknown and wrong binding (no oracle). Raises LookupError."""
    h = hashlib.sha256(p["t"].encode()).hexdigest()
    hit = None
    for k, v in json.loads(tokens_file.read_text()).items():
        if hmac.compare_digest(k, h):
            hit = v
    if hit is None or hit.get("used") or not hit.get("exp", 0) > now or hit.get("fp") != p["id"] or hit.get("severity") != p["s"]:
        raise LookupError("unknown")
    return {**hit, "hash": h, "th": h[:16]}


def website_enqueue(p: dict, found: dict, now: float, key: bytes, d_cap: int = 365) -> dict:
    """`POST /api/ack/confirm` -> the one thing the container writes: a signed source=email request naming the token HASH (never the token)
    into ack/inbox/<epoch_ms>-<8hex>.json (temp name, then rename). `d` can only keep or lower the default, never exceed the cap."""
    from homelab_maint import acks
    req = {"v": 1, "kind": "ack", "source": "email", "token_hash": found["hash"], "fp": p["id"], "days": min(p["d"], d_cap), "severity": p["s"],
           "note": "", "ts": now}
    req["sig"] = acks.sign(req, key)
    inbox = core.STATE_DIR / "ack" / "inbox"
    tmp, final = inbox / f".{secrets.token_hex(4)}.tmp", inbox / f"{int(now * 1000)}-{secrets.token_hex(4)}.json"
    tmp.write_text(json.dumps(req))
    tmp.rename(final)
    return req


def web_key() -> bytes:
    """ack/web.key as the install makes it: 0600, >= 32 bytes of text, in the directory the container mounts read-only."""
    (core.STATE_DIR / "ack" / "inbox").mkdir(parents=True, exist_ok=True)
    f = core.STATE_DIR / "ack" / "web.key"
    f.write_text(secrets.token_hex(32))
    os.chmod(f, 0o600)
    return f.read_text().strip().encode()


def test_the_email_link_end_to_end_notify_builds_it_the_site_parses_it_the_token_hash_resolves_and_the_runner_applies_it(real_acks):
    acks_, st = real_acks, core.STATE_DIR
    key = web_key()
    summary = "1 failed unit(s): nginx.service"
    ev = notify.alert_event("failed_units", "Services", "crit", summary)
    d, fk = go(ev, now=NOON)
    m, fp = fk.calls[0], str(acks_.fingerprint("failed_units", summary, "crit"))
    assert d.ok and m.channels == ["sms", "email"]
    # 1. the email: EXACTLY the SPEC5 S8 shape, the printed Issue ID, the plain-text repeat of the URL, the same URL on the button and the text link
    url = m.ack_url
    assert re.fullmatch(rf"https://{re.escape(SITE_HOST)}/ack\?id={fp}&t=[A-Za-z0-9_-]{{43}}&d=90&s=crit", url), url
    assert m.plain.splitlines().count(f"Acknowledge (90 days): {url}") == 1 and m.plain.count(url) == 1 and f"Issue ID: {fp}" in m.plain
    assert m.html.count(url.replace("&", "&amp;")) == 3 and "Issue ID: <span" in m.html and f">{fp}</span>" in m.html
    assert "/ack/" not in m.plain + m.html                                          # never the earlier /ack/<token> path shape
    p = website_parse(url)                                                          # 2. the site's own rules accept it, field by field
    assert p == {"id": fp, "t": url.split("&t=")[1].split("&")[0], "d": 90, "s": "crit"}
    # 3. the SMS is free of the link, the token and the id
    assert "http" not in m.sms and "/ack" not in m.sms and p["t"] not in m.sms and fp not in m.sms and "Issue" not in m.sms
    # 4. the token hash resolves in ack/tokens.json (the container's view): bound to this id + severity, unused, 30 days; the plaintext is nowhere
    tf = st / "ack" / "tokens.json"
    found = website_lookup(p, tf, NOON + H)
    th = hashlib.sha256(p["t"].encode()).hexdigest()
    assert found["hash"] == acks_.token_hash(p["t"]) == th and found["fp"] == fp and found["severity"] == "crit" and found["exp"] == NOON + 30 * D
    doc = json.loads(tf.read_text())
    assert [k for k in doc if k.startswith(th[:16])] == [th] and doc[th]["state"] == "pending"      # GET /api/ack/status?th=<16 hex> finds exactly one record
    assert p["t"] not in tf.read_text() and p["t"] not in disk_dump()
    for bad in ({**p, "id": "0" * 16}, {**p, "s": "warn"}, {**p, "t": ("B" if p["t"][0] != "B" else "C") + p["t"][1:]}):    # a re-pointed or forged link
        with pytest.raises(LookupError):
            website_lookup(bad, tf, NOON + H)
    with pytest.raises(LookupError):
        website_lookup(p, tf, NOON + 31 * D)                                         # expired
    # 5. the click: the site queues a signed request (the hash, not the token); the runner applies it within a minute
    req = website_enqueue(p, found, NOON + H, key)
    assert p["t"] not in json.dumps(req) and sorted((st / "ack" / "inbox").iterdir())
    rep = acks_.process_inbox(NOON + H + 60)
    assert rep.applied == [fp] and rep.rejected == [] and not [f for f in (st / "ack" / "inbox").iterdir() if f.is_file()]
    until = NOON + H + 60 + 90 * D
    rec = json.loads(tf.read_text())[th]
    assert rec["used"] is True and rec["state"] == "applied" and rec["until"] == until          # the page polls this and flips to "Applied"
    with pytest.raises(LookupError):
        website_lookup(p, tf, NOON + H + 61)                                         # single use: the link is spent
    website_enqueue(p, {**found}, NOON + H + 120, key)                               # a second click that reached the inbox anyway ...
    rep2 = acks_.process_inbox(NOON + H + 180)
    assert rep2.applied == [] and rep2.rejected == ["used"]                          # ... is refused by the runner too
    # 6. the exact error is now held, nothing of it reaches anyone, and the token never appeared in any file
    d2, fk2 = go(ev, now=NOON + 8 * H)
    assert d2.skipped == "acknowledged" and fk2.calls == []
    assert p["t"] not in disk_dump() and th in disk_dump()


# The reference above is written from SPEC5 S8 on purpose. These two run the same chain against the REAL web/app.py (its own validators and
# handlers on a loopback port, the same tmp state), so the two sides cannot drift apart without a test going red.
needs_real_site = pytest.mark.skipif(not wh.have_web(), reason="web/ is not in this tree")


@needs_real_site
@pytest.mark.parametrize("sev,days", [("crit", 90), ("warn", 30), ("info", 365), ("warn", 60), ("crit", 9999), ("warn", 1), ("crit", 7)])
def test_the_real_site_serves_every_link_notify_builds_without_a_redirect_or_an_error(real_acks, tmp_path, sev, days):
    web_key()
    d, fk = go(mk("alert", sev, task="failed_units", summary="1 failed unit(s): nginx.service"), cfg=cfgx(ack={"days": days}))
    url = fk.calls[0].ack_url
    site = wh.Site(wh.load_app(), tmp_path, core.STATE_DIR, core.STATE_DIR / "ack", clock=lambda: NOON + H, gate=False)
    try:
        path = url.removeprefix(f"https://{SITE_HOST}")
        assert d.ok and site.req("GET", path)[0] == 200, (url, site.req("GET", path)[0])             # 200 = valid AND canonical (a 302 would mean `d` is off)
        p = website_parse(url)
        assert p["d"] in WEB_DAYS
        for bad in (path.replace("&s=", "&s=info&x="), path.replace("id=", "id=0"), path.replace("&d=", "&d=x"), path + "&id=" + p["id"],
                    path.replace("/ack?", "/ack/").replace("&t=", "/"), path.split("&t=")[0] + "&d=90&s=crit"):
            assert site.req("GET", bad)[0] != 200, bad
    finally:
        site.stop()


@needs_real_site
def test_the_real_site_confirms_the_link_notify_built_and_the_runner_applies_what_it_queued(real_acks, tmp_path):
    acks_ = real_acks
    key = web_key()
    summary = "1 failed unit(s): nginx.service"
    ev = notify.alert_event("failed_units", "Services", "crit", summary)
    d, fk = go(ev, now=NOON)
    url, fp = fk.calls[0].ack_url, str(acks_.fingerprint("failed_units", summary, "crit"))
    p = website_parse(url)
    site = wh.Site(wh.load_app(), tmp_path, core.STATE_DIR, core.STATE_DIR / "ack", clock=lambda: NOON + H, gate=False)
    try:
        st, body = site.req("POST", "/api/ack/confirm", {"id": p["id"], "t": p["t"], "d": p["d"], "s": p["s"]})
        assert st == 202 and body["state"] == "received" and body["id"] == fp and body["issue"]["severity"] == "crit", (st, body)
        (f,) = [x for x in (core.STATE_DIR / "ack" / "inbox").iterdir() if x.is_file()]
        req = json.loads(f.read_text())
        assert req["source"] == "email" and req["token_hash"] == acks_.token_hash(p["t"]) and p["t"] not in f.read_text()
        assert hmac.compare_digest(req["sig"], acks_.sign(req, key))                                 # signed with the key the runner reads
        rep = acks_.process_inbox(NOON + H + 60)
        assert rep.applied == [fp] and rep.rejected == []
        st, status = site.req("GET", f"/api/ack/status?th={acks_.token_hash(p['t'])[:16]}")
        assert st == 200 and status["state"] == "applied"                                            # the page polls this and flips to "Applied"
        assert site.req("POST", "/api/ack/confirm", {"id": p["id"], "t": p["t"], "d": p["d"], "s": p["s"]})[0] == 404       # the spent link: one answer for "invalid"
    finally:
        site.stop()
    d2, fk2 = go(ev, now=NOON + 8 * H)
    assert d2.skipped == "acknowledged" and fk2.calls == []                                          # the exact error is held


@pytest.mark.parametrize("kind", ["alert", "incident_open"])
@pytest.mark.parametrize("sev,s", [("crit", "crit"), ("warn", "warn"), ("info", "warn")])
@pytest.mark.parametrize("days", [1, 7, 29, 30, 60, 90, 91, 365, 9999])
def test_every_link_notify_can_build_passes_the_sites_rules_whatever_the_config(real_acks, kind, sev, s, days):
    web_key()
    d, fk = go(mk(kind, sev, task="failed_units", summary="1 failed unit(s): nginx.service"), cfg=cfgx(ack={"days": days}))
    m = fk.calls[0]
    p = website_parse(m.ack_url)
    assert d.ok and p["s"] == s and p["d"] in WEB_DAYS and (p["d"] <= days or p["d"] == 7)     # rounded DOWN; the site's shortest when asked for less
    assert f"Acknowledge for {p['d']} days" in m.html and f"Acknowledge ({p['d']} days): " in m.plain and f"{p['d']} days" in T.button_label(None, p["d"])
    assert website_lookup(p, core.STATE_DIR / "ack" / "tokens.json", NOON)["fp"] == p["id"]


def test_the_fresh_button_of_an_expiry_notice_passes_the_sites_rules_too(real_acks):
    acks_ = real_acks
    summary = "1 failed unit(s): nginx.service"
    ev = notify.alert_event("failed_units", "Services", "warn", summary)
    fp = parse_link(go(ev, now=NOON)[1].calls[0].ack_url)["id"]
    acks_.add(fp, days=7, by="email", now=NOON)
    end = NOON + 7 * D + 1
    (core.STATE_DIR / "status.json").write_text(json.dumps({"tasks": {"failed_units": {"status": "warn", "summary": summary, "title": "Services", "fp": fp}}}))
    got = notify.notify_expired(end, cfgx(), expired=acks_.expire(end), transport=(fk := Fake()))
    p = website_parse(fk.calls[0].ack_url)
    assert got[0].ok and p["id"] == fp and p["s"] == "warn" and p["d"] == 90 and f"Issue ID: {fp}" in fk.calls[0].plain
    assert website_lookup(p, core.STATE_DIR / "ack" / "tokens.json", end)["fp"] == fp


@pytest.mark.parametrize("bad", [
    "http://maintainer.ohmzhomelab.ca/ack?id={i}&t={t}&d=90&s=crit",              # not https
    "https://maintainer.ohmzhomelab.ca/ack/{t}",                                  # the earlier path shape
    "https://maintainer.ohmzhomelab.ca/ack?id={i}&t={t}&d=91&s=crit",             # a length the site does not offer
    "https://maintainer.ohmzhomelab.ca/ack?id={i}&t={t}&d=90&s=info",             # a severity that is not an acknowledgement ceiling
    "https://maintainer.ohmzhomelab.ca/ack?id={i}&t={t}&d=90&s=crit&x=1",         # an extra parameter
    "https://maintainer.ohmzhomelab.ca/ack?id={i}&id={i}&t={t}&d=90&s=crit",      # a repeated one
    "https://maintainer.ohmzhomelab.ca/ack?id=ABCDEF0123456789&t={t}&d=90&s=crit",   # upper-case id
    "https://maintainer.ohmzhomelab.ca/ack?id={i}&t={t}x&d=90&s=crit",            # a 44-character token
    "https://evil.example/ack?id={i}&t={t}&d=90&s=crit",                           # another host
    "https://maintainer.ohmzhomelab.ca/ack?id={i}&t={t}&d=90&s=crit#x",           # a fragment
])
def test_the_site_validator_in_this_file_really_refuses_what_it_should(bad):
    """The mirror above is only worth something if it is strict: each of these is a link the site must NOT honour."""
    i, t = "0123456789abcdef", "A" * 43
    with pytest.raises(ValueError):
        website_parse(bad.format(i=i, t=t))
    assert website_parse(f"https://{SITE_HOST}/ack?id={i}&t={t}&d=90&s=crit") == {"id": i, "t": t, "d": 90, "s": "crit"}


def test_the_link_never_asks_for_more_days_than_the_runner_accepts(real_acks):
    web_key()
    (core.CONF_DIR / "ack.toml").write_text("[ack]\nmax_days = 30\n")                # the owner shortened the cap on the host (acks.py's own clamp: days = 30)
    ev = mk("alert", "crit", task="failed_units", summary="1 failed unit(s): nginx.service")
    d, fk = go(ev, now=NOON)                                                         # notify.toml still says 90
    m = fk.calls[0]
    p = website_parse(m.ack_url, max_days=30)
    assert d.ok and p["d"] == 30 and "Acknowledge for 30 days" in m.html and "Acknowledge (30 days): " in m.plain and "90 days" not in m.plain
    assert website_lookup(p, core.STATE_DIR / "ack" / "tokens.json", NOON)["fp"] == p["id"]
    # a cap below the shortest length the site offers: no length fits, so there is no link (the request would be refused every time) and no token
    (core.CONF_DIR / "ack.toml").write_text("[ack]\nmax_days = 5\n")
    before = json.loads((core.STATE_DIR / "ack" / "tokens.json").read_text())
    d2, fk2 = go(mk("alert", "crit", task="failed_units", summary="1 failed unit(s): kavita.service"), now=NOON + 8 * H)
    m2 = fk2.calls[0]
    assert d2.ok and m2.ack_url == "" and "Acknowledge for" not in m2.html and "/ack?" not in m2.plain and "Issue ID: " in m2.plain
    assert json.loads((core.STATE_DIR / "ack" / "tokens.json").read_text()) == before and "ack link unavailable" in d2.note
    # the expiry notice words the same number the button carries
    (core.CONF_DIR / "ack.toml").write_text("[ack]\nmax_days = 30\n")
    ev3 = notify.ack_expired_event({"fp": p["id"], "task": "failed_units", "title": "Services", "summary": "x", "severity": "warn", "acked_at": NOON,
                                    "until": NOON + 7 * D, "by": "email", "still_failing": True}, button_days=30)
    assert "another 30 days" in ev3.details["todo"][0]


def test_the_sms_the_log_and_the_audit_never_carry_the_link_the_token_or_the_issue_id_after_a_full_chain(real_acks):
    key = web_key()
    ev = notify.alert_event("failed_units", "Services", "crit", "1 failed unit(s): nginx.service")
    d, fk = go(ev, now=NOON)
    p = website_parse(fk.calls[0].ack_url)
    assert "ack?" not in fk.calls[0].sms
    website_enqueue(p, website_lookup(p, core.STATE_DIR / "ack" / "tokens.json", NOON), NOON + 1, key)
    real_acks.process_inbox(NOON + 2)
    dump = disk_dump()
    assert p["t"] not in dump and "/ack?id=" not in dump and "&t=" not in dump           # no URL with a token in any state, log, audit or export file
    for row in log_rows() + audit_rows():
        assert p["t"] not in json.dumps(row) and "&t=" not in json.dumps(row)


@pytest.mark.skipif(_chrome() is None, reason="no headless Chrome here (set HOMELAB_MAINT_NO_BROWSER_TESTS=1 to skip on purpose)")
def test_the_real_email_button_in_a_browser_points_at_the_exact_site_url_at_desktop_and_phone_widths(real_acks, tmp_path, monkeypatch):
    """The DOM, not the string: in headless Chrome at a desktop and two phone widths the button is ONE link, its `href` is byte-for-byte the URL
    in the plain part and passes the site's rules, it is inside the card and >= 44 px tall, and the page does not scroll sideways."""
    import html as htmllib
    web_key()
    d, fk = go(mk("alert", "crit", task="failed_units", summary="1 failed unit(s): nginx.service"), now=NOON)
    m = fk.calls[0]
    script = """<pre id="out"></pre><script>addEventListener('load', () => setTimeout(() => { const f = document.querySelector('iframe'), doc = f.contentDocument;
      const panel = doc.querySelector('table[width="600"]').getBoundingClientRect(), all = [...doc.querySelectorAll('a')].filter(x => x.href.includes('/ack?'));
      const btn = all.find(x => /Acknowledge for/.test(x.textContent)), b = btn.getBoundingClientRect();
      document.getElementById('out').textContent = 'RESULT' + JSON.stringify({scroll: doc.documentElement.scrollWidth, n: all.length, hrefs: all.map(x => x.getAttribute('href')),
        btnH: Math.round(btn.parentElement.getBoundingClientRect().height), inside: b.left >= panel.left && b.right <= panel.right, label: btn.textContent.trim()}) + 'END'; }, 300));</script>"""
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    for width in (1100, 390, 320):
        page = tmp_path / f"btn{width}.html"
        page.write_text(f'<!doctype html><html><body><iframe style="width:{width}px;height:1400px;border:0;display:block" '
                        f'srcdoc="{htmllib.escape(m.html, quote=True)}"></iframe>{script}</body></html>')
        r = _REAL_RUN([_chrome(), "--headless=new", "--no-sandbox", "--disable-gpu", f"--user-data-dir={tmp_path}/pb{width}", "--virtual-time-budget=8000",
                       "--dump-dom", page.as_uri()], capture_output=True, text=True, timeout=90)
        got = re.search(r"RESULT(.*?)END", r.stdout, re.S)
        if not got:
            pytest.skip(f"headless Chrome produced no result (rc={r.returncode}): {r.stderr[-120:]!r}")
        res = json.loads(htmllib.unescape(got[1]))
        assert res["n"] == 2 and set(res["hrefs"]) == {m.ack_url}, (width, res)          # the button and the text link: the very same exact URL
        assert website_parse(res["hrefs"][0])["d"] == 90 and res["label"].startswith("Acknowledge for 90 days")
        assert res["scroll"] <= width and res["btnH"] >= 44 and res["inside"], (width, res)
