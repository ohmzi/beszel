"""Tests for homelab_maint.tasks.checks_health: smart_trend, alert_path_health, growth_watch, config_drift.

Formats in the fixtures were copied from the real host (attrlog rows, smart-alert.log, tune2fs, mountinfo).
Nothing here touches the live system: every path is a tmp dir and `sh` is replaced by a recording fake.
"""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint is imported)

import json
import os
import subprocess
import time
from datetime import datetime

import pytest

from homelab_maint import core
from homelab_maint.tasks import checks_health as ch

DAY = 86400


# --------------------------------------------------------------------------- helpers
def mkctx(name, now=None, glob=None, **opts):
    cfg = {"global": glob or {}, "tasks": {name: opts}, "caps": {}, "protected": {}}
    return core.Ctx(cfg, name, apply=False, now=now if now is not None else time.time())


def check_summary(res):
    assert len(res.summary) <= 140 and res.summary.isascii() and "\n" not in res.summary
    return res


def fake_sh(table, calls=None):
    """table: {('cmd','prefix'): (rc, stdout, stderr)}; unknown commands behave like "not installed"."""
    def _sh(cmd, timeout=60, **kw):
        if calls is not None:
            calls.append(list(cmd))
        for prefix, (rc, out, err) in table.items():
            if tuple(cmd[:len(prefix)]) == prefix:
                return subprocess.CompletedProcess(cmd, rc, out, err)
        return subprocess.CompletedProcess(cmd, 127, "", "not found (test)")
    return _sh


def attrlog_row(ts, attrs, temp=None):
    t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    return f"{t};" + "".join(f"\t{i};100;{raw};" for i, raw in attrs.items()) + \
        (f"\ttemperature;{temp};" if temp is not None else "")


def write_attrlog(d, ident, now, attrs_at, days=8, step=1800, temp=40, last_age=600):
    """attrs_at(age_days) -> {attr: raw}. Rows every `step` s over `days`, newest `last_age` s old."""
    rows, t = [], now - last_age - days * DAY
    while t <= now - last_age:
        rows.append(attrlog_row(t, attrs_at((now - last_age - t) / DAY), temp))
        t += step
    (d / f"attrlog.{ident}.csv").write_text("\n".join(rows) + "\n")


# --------------------------------------------------------------------------- registry
def test_tasks_registered_with_contract():
    assert {n: (core.REGISTRY[n].klass, core.REGISTRY[n].tier) for n in
            ("smart_trend", "alert_path_health", "growth_watch", "config_drift")} == {
        "smart_trend": ("C0", "check"), "alert_path_health": ("C0", "check"),
        "growth_watch": ("C0", "check"), "config_drift": ("C0", "weekly")}


# =========================================================================== smart_trend
REAL_ROW = (
    "2026-10-01 18:57:33;\t1;100;0;\t5;100;0;\t9;100;2010;\t12;100;90;\t171;100;0;\t172;100;0;\t173;99;8;"
    "\t174;100;64;\t180;100;80;\t183;100;4;\t184;100;0;\t187;100;0;\t194;70;214749806622;\t196;100;0;"
    "\t197;100;0;\t198;100;0;\t199;100;51;\t202;99;1;\t206;100;0;\t210;100;0;\t246;100;18857481885;"
    "\t247;100;589296308;\t248;100;1195652608;\t249;100;0;\t250;100;104;\t251;100;1884443122;"
    "\t252;100;31;\t253;100;0;\t254;100;21739815840;\t255;100;73869190144;\ttemperature;28;")


def test_parse_real_attrlog_row():
    ts, raw, temp = ch._parse_attr_row(REAL_ROW)
    assert time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) == "2026-10-01 18:57:33"
    assert len(raw) == 30 and raw[5] == 0 and raw[199] == 51 and raw[194] == 214749806622 and temp == 28


def test_parse_row_without_temperature_and_malformed():
    ts, raw, temp = ch._parse_attr_row("2026-09-20 20:47:53;\t1;100;0;\t5;100;7;\t199;200;37;")
    assert raw == {1: 0, 5: 7, 199: 37} and temp is None
    assert ch._parse_attr_row("garbage;;;") is None
    assert ch._parse_attr_row("") is None
    assert ch._parse_attr_row("2026-09-20 20:47:53;\t5;100;xx;")[1] == {}   # bad number: row kept, attr dropped


@pytest.fixture
def smart_env(tmp_path, monkeypatch):
    (tmp_path / "attr").mkdir()
    (tmp_path / "hwmon").mkdir()
    (tmp_path / "byid").mkdir()
    monkeypatch.setattr(ch, "HWMON_DIR", tmp_path / "hwmon")
    monkeypatch.setattr(ch, "BYID_DIR", tmp_path / "byid")
    return tmp_path


def run_smart(env, now, **opts):
    return check_summary(ch.smart_trend(mkctx("smart_trend", now, attrlog_dir=str(env / "attr"), **opts)))


def test_smart_flat_is_ok_and_maps_kernel_name(smart_env):
    now = time.time()
    flat = lambda age: {5: 0, 9: 31000 - int(age * 24), 197: 0, 198: 0, 199: 37}
    write_attrlog(smart_env / "attr", "ST16000NE000_2RW103-ZL2P7TV6.ata", now, flat, temp=39)
    os.symlink("../../sdc", smart_env / "byid" / "ata-ST16000NE000-2RW103_ZL2P7TV6")
    r = run_smart(smart_env, now)
    assert r.status == "ok" and "SMART ok" in r.summary
    d = r.metrics["devices"][0]
    assert (d["dev"], d["temp_c"], d["realloc"], d["pending"], d["crc"]) == ("sdc", 39, 0, 0, 37)
    assert d["power_on_h"] == 31000 - int(600 / DAY * 24) and d["model"].endswith("7TV6")
    assert r.metrics["hottest_dev"] == "sdc" and r.metrics["n_problems"] == 0


def test_smart_increase_in_realloc_pending_crc_warns(smart_env):
    now = time.time()
    # values 7 days ago: 0/0/0/37; now 3/2/0/42 (+ attr 9 and 188 move but are not watched)
    grow = lambda age: ({5: 0, 197: 0, 198: 0, 199: 37, 9: 100} if age > 6.5
                        else {5: 3, 197: 2, 198: 0, 199: 42, 9: 200, 188: 9})
    write_attrlog(smart_env / "attr", "WDC_WD181KFGX_68AFPN0-4YHXY35P.ata", now, grow)
    r = run_smart(smart_env, now)
    assert r.status == "warn" and r.alert is True
    assert "realloc +3" in r.summary and "pending +2" in r.summary and "CRC +5" in r.summary
    assert r.metrics["n_problems"] == 1 and r.items[0]["level"] == "warn"


def test_smart_decrease_and_nonzero_steady_values_are_fine(smart_env):
    now = time.time()
    write_attrlog(smart_env / "attr", "A_B-SER1.ata", now, lambda age: {5: 1, 197: 4 if age > 6.5 else 0, 199: 9})
    assert run_smart(smart_env, now).status == "ok"          # pending went 4 -> 0, realloc steady at 1


def test_smart_short_history_gives_no_trend(smart_env):
    now = time.time()
    # only 6 h of history: below min_span_hours, an apparent jump must not be reported as a trend
    write_attrlog(smart_env / "attr", "A_B-SER1.ata", now, lambda age: {5: 0 if age > 0.2 else 9}, days=0.25)
    assert run_smart(smart_env, now).status == "ok"


def test_smart_attr_missing_on_one_side_is_ignored(smart_env):
    now = time.time()   # SanDisk-style drive: no 197/198 at all
    write_attrlog(smart_env / "attr", "SD_Ultra-0005.ata", now, lambda age: {5: 0, 199: 49})
    r = run_smart(smart_env, now)
    d = r.metrics["devices"][0]
    assert r.status == "ok" and d["pending"] is None and d["crc"] == 49


def test_smart_ata_temperature_and_fallback(smart_env):
    now = time.time()
    write_attrlog(smart_env / "attr", "HOT_ONE-SER1.ata", now, lambda a: {5: 0}, temp=70)
    r = run_smart(smart_env, now)
    assert r.status == "warn" and "70C" in r.summary
    r = run_smart(smart_env, now, ata_temp_warn_c=75)         # configurable; stock smartd 55 C is not used
    assert r.status == "ok"
    # no explicit temperature column: low byte of attribute 194
    write_attrlog(smart_env / "attr", "HOT_ONE-SER1.ata", now, lambda a: {5: 0, 194: 73014444071}, temp=None)
    assert run_smart(smart_env, now).metrics["devices"][0]["temp_c"] == 73014444071 & 0xFF


def test_smart_stale_attrlog_warns(smart_env):
    now = time.time()
    write_attrlog(smart_env / "attr", "A_B-SER1.ata", now, lambda a: {5: 0}, last_age=10 * 3600)
    r = run_smart(smart_env, now)
    assert r.status == "warn" and "old" in r.summary


def fake_nvme(env, temp_milli, name="nvme", model="Samsung SSD 980 PRO with Heatsink 2TB   ", serial="S6WRNJ0WA00960A"):
    ctrl = env / "ctrl" / "nvme0"
    ctrl.mkdir(parents=True, exist_ok=True)
    (ctrl / "model").write_text(model + "\n")
    (ctrl / "serial").write_text(serial + "     \n")
    h = env / "hwmon" / f"hwmon{len(list((env / 'hwmon').iterdir()))}"
    h.mkdir()
    (h / "name").write_text(name + "\n")
    (h / "temp1_input").write_text(f"{temp_milli}\n")
    os.symlink(ctrl, h / "device")


def test_smart_nvme_uses_hwmon_and_nvme_threshold(smart_env):
    now = time.time()
    fake_nvme(smart_env, 63850)
    fake_nvme(smart_env, 99000, name="coretemp")              # not an NVMe: ignored
    r = run_smart(smart_env, now, nvme_temp_warn_c=70)        # no attrlogs at all, NVMe alone is enough
    d = r.metrics["devices"][0]
    assert r.status == "ok" and (d["dev"], d["temp_c"]) == ("nvme0", 64) and d["model"] == "Samsung SSD 980 PRO 960A"
    r = run_smart(smart_env, now, nvme_temp_warn_c=60)
    assert r.status == "warn" and "nvme0 64C" in r.summary


def test_smart_no_data_at_all_warns(smart_env):
    assert run_smart(smart_env, time.time()).status == "warn"


def test_smart_never_calls_a_command(smart_env, monkeypatch):
    calls = []
    monkeypatch.setattr(ch, "sh", fake_sh({}, calls))
    now = time.time()
    write_attrlog(smart_env / "attr", "A_B-SER1.ata", now, lambda a: {5: 0})
    run_smart(smart_env, now)
    assert calls == []                                        # no smartctl, ever


# =========================================================================== alert_path_health
NOW = datetime.fromisoformat("2026-10-01T19:12:00-04:00").timestamp()
# trimmed copy of the real /var/log/smart-alert.log: old-format failures, then the post-fix test send
REAL_LOG = """\
2026-09-30T07:27:33-04:00 device=SMART error (Temperature) detected on host: ohmz-homelab type=auto failtype=Temperature root
2026-09-30T07:27:33-04:00 ALERT SEND FAILED for SMART error (Temperature) detected on host: ohmz-homelab
2026-10-01T07:27:33-04:00 device=SMART error (Temperature) detected on host: ohmz-homelab type=auto failtype=Temperature root
2026-10-01T07:27:33-04:00 ALERT SEND FAILED for SMART error (Temperature) detected on host: ohmz-homelab
2026-10-01T18:57:33-04:00 device=SMART error (Temperature) detected on host: ohmz-homelab type=auto failtype=Temperature root
2026-10-01T18:57:33-04:00 ALERT SEND FAILED for SMART error (Temperature) detected on host: ohmz-homelab
2026-10-01T19:04:25-04:00 device=/dev/TEST type=test failtype=Test TEST ONLY alert path check after hook fix - no action needed
2026-10-01T19:04:28-04:00 alert sent for /dev/TEST
"""


@pytest.fixture
def alert_env(tmp_path, monkeypatch):
    bridge, hook = tmp_path / "bridge.py", tmp_path / "smart-alert.sh"
    for f in (bridge, hook):
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
    conf = tmp_path / "smartd.conf"
    conf.write_text(f"# comment -M exec /nonexistent\nDEVICESCAN -a -n standby,q -W 4,45,55 -m root -M exec {hook}\n")
    monkeypatch.setattr(ch, "SMARTD_CONF", conf)
    monkeypatch.setattr(ch, "AUDIT_LOG", tmp_path / "audit.jsonl")
    (tmp_path / "smart-alert.log").write_text(REAL_LOG)
    return tmp_path


def run_alert(env, now=NOW, **opts):
    opts.setdefault("smart_log", str(env / "smart-alert.log"))
    return check_summary(ch.alert_path_health(
        mkctx("alert_path_health", now, glob={"bridge": str(env / "bridge.py")}, **opts)))


def audit_line(ts, action, outcome, task="notify"):
    return json.dumps({"ts": datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "task": task, "action": action, "target": "x", "bytes": 0, "outcome": outcome}) + "\n"


def test_alert_recovered_after_fix_is_ok(alert_env):
    r = run_alert(alert_env)
    assert r.status == "ok" and "recovered" in r.summary and "10-01 19:04" in r.summary
    m = r.metrics
    assert m["smart_fail_24h"] == 2 and m["smart_broken"] is False and m["bridge_ok"] and m["hook_ok"]
    assert m["smart_last_ok_age_min"] == 8


def test_alert_failure_without_later_success_warns_with_error_text(alert_env):
    log = alert_env / "smart-alert.log"
    log.write_text(REAL_LOG + "2026-10-01T19:08:00-04:00 ALERT SEND FAILED rc=1 for /dev/sdc [SAT]: "
                   "smtp auth failed for ohmz@example.com: Name or service not known\n")
    r = run_alert(alert_env)
    assert r.status == "warn" and r.alert is True
    assert "rc=1" in r.summary and "Name or service not known" in r.summary
    assert "@" not in r.summary and "example.com" not in json.dumps(r.items + [r.metrics])  # addresses scrubbed
    assert r.metrics["smart_broken"] is True and "Name or service" in r.metrics["last_error"]


def test_alert_old_failures_outside_24h_are_ignored(alert_env):
    (alert_env / "smart-alert.log").write_text(REAL_LOG.split("\n", 2)[1] + "\n")   # only the 36 h old failure
    r = run_alert(alert_env)
    assert r.status == "ok" and r.metrics["smart_fail_24h"] == 0


def test_alert_failure_before_success_is_recovered_but_after_is_not(alert_env):
    log = alert_env / "smart-alert.log"
    ok = "2026-10-01T12:00:00-04:00 alert sent for /dev/x\n"
    early_bad = "2026-10-01T11:00:00-04:00 ALERT SEND FAILED rc=2 for /dev/x: boom\n"
    late_bad = "2026-10-01T13:00:00-04:00 ALERT SEND FAILED rc=2 for /dev/x: boom\n"
    log.write_text(late_bad + ok + early_bad)                  # file order is irrelevant, timestamps decide
    r = run_alert(alert_env)
    assert r.status == "warn" and "boom" in r.summary
    log.write_text(early_bad + ok)
    assert run_alert(alert_env).status == "ok"


def test_alert_missing_smart_log_is_not_a_problem(alert_env):
    (alert_env / "smart-alert.log").unlink()
    r = run_alert(alert_env)
    assert r.status == "ok" and r.metrics["smart_last_ok_age_min"] is None


def test_alert_bridge_missing_or_not_executable_is_crit(alert_env):
    (alert_env / "bridge.py").chmod(0o644)
    r = run_alert(alert_env)
    assert r.status == "crit" and "bridge" in r.summary and r.metrics["bridge_ok"] is False
    (alert_env / "bridge.py").unlink()
    assert run_alert(alert_env).status == "crit"


def test_alert_hook_problems_warn(alert_env, monkeypatch):
    (alert_env / "smart-alert.sh").chmod(0o644)
    r = run_alert(alert_env)
    assert r.status == "warn" and "hook" in r.summary
    (alert_env / "smartd.conf").write_text("DEVICESCAN -a -m root\n")      # mail to a box without an MTA
    r = run_alert(alert_env)
    assert r.status == "warn" and any("no -M exec" in i["detail"] for i in r.items)
    monkeypatch.setattr(ch, "SMARTD_CONF", alert_env / "nope.conf")        # unreadable conf: fall back to config path
    (alert_env / "smart-alert.sh").chmod(0o755)
    assert run_alert(alert_env, smart_hook=str(alert_env / "smart-alert.sh")).status == "ok"


def test_alert_notifier_failures_from_audit_log(alert_env):
    audit = alert_env / "audit.jsonl"
    audit.write_text(audit_line(NOW - 3600, "dry-run", "dry-run", task="docker_cache")
                     + audit_line(NOW - 3000, "send", "failed rc=1 Traceback token=abc123secret", task="notify")
                     + "not json at all\n")
    r = run_alert(alert_env)
    assert r.status == "warn" and "notifier" in r.summary and "abc123secret" not in r.summary
    assert r.metrics["notify_fail_24h"] == 1 and r.metrics["notify_broken"] is True
    audit.write_text(audit.read_text() + audit_line(NOW - 600, "send", "sent"))          # later success clears it
    r = run_alert(alert_env)
    assert r.status == "ok" and r.metrics["notify_broken"] is False and r.metrics["notify_fail_24h"] == 1


def test_alert_dropped_alerts_are_info_only(alert_env):
    (alert_env / "audit.jsonl").write_text(audit_line(NOW - 100, "budget-exhausted", "dropped"))
    r = run_alert(alert_env)
    assert r.status == "ok" and r.metrics["notify_dropped_24h"] == 1
    assert any(i["what"] == "notifier budget" and i["level"] == "info" for i in r.items)


def test_alert_unreadable_log_is_reported_not_hidden(alert_env):
    (alert_env / "smart-alert.log").unlink()
    (alert_env / "smart-alert.log").mkdir()                   # open() fails with IsADirectoryError (an OSError)
    r = run_alert(alert_env)
    assert r.status == "warn" and "unreadable" in r.summary


def test_alert_summary_is_bounded_ascii_and_sends_nothing(alert_env, monkeypatch):
    calls = []
    monkeypatch.setattr(ch, "sh", fake_sh({}, calls))
    log = alert_env / "smart-alert.log"
    log.write_text("2026-10-01T19:08:00-04:00 ALERT SEND FAILED rc=1 for /dev/sdc: " + "café " * 80 + "\n")
    r = run_alert(alert_env)
    assert r.status == "warn" and len(r.summary) <= 140
    assert calls == []                                        # a meta-monitor must not send anything itself


def test_scrub_redacts_addresses_and_tokens_but_keeps_paths():
    s = ch._scrub('File "/usr/local/sbin/backup-notify-hermes.py", line 3 to a@b.co Bearer abcdef0123456789abcdef0123456789xyz')
    assert "/usr/local/sbin/backup-notify-hermes.py" in s and "<addr>" in s and "abcdef0123456789" not in s


def test_scrub_does_not_invert_the_real_gmail_auth_error():
    # Regression: a bare space used to count as a separator, so the most likely real bridge failure became
    # "Password=<redacted> accepted", which reads as success on the dashboard and in the SMS.
    s = ch._scrub("(535, b'5.7.8 Username and Password not accepted. For more information, go to')")
    assert "Username and Password not accepted" in s and "redacted" not in s
    assert ch._scrub("Password not accepted") == "Password not accepted"


@pytest.mark.parametrize("text, leaked", [
    ("Authorization: Bearer shorttok", "shorttok"),                   # the scheme word used to be 'the secret'
    ("authorization=Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("password: abcd efgh ijkl mnop", "efgh"),                        # a password may contain spaces: to end of line
    ("PASSWORD=hunter2 for user", "hunter2"),
    ("passphrase = correct horse battery", "battery"),
    ("--password hunter2 --host x", "hunter2"),                       # CLI flag with a space
    ('{"token": "abc123", "x": 1}', "abc123"),                        # quoted key (exception reprs)
    ("api_key=k-12345 failed", "k-12345"),
    ("sent with id 123e4567-e89b-12d3-a456-426614174000 rejected", "123e4567"),   # UUID-style token
    ("Bearer shorttok12", "shorttok12"),                              # bare scheme, no 'Authorization:'
])
def test_scrub_redacts_secret_shapes(text, leaked):
    out = ch._scrub(text)
    assert leaked not in out and "redacted" in out and out.isascii()


def test_scrub_keeps_harmless_text_readable():
    s = ch._scrub("smtp connect to smtp.example.net:587 timed out after 30s (rc=1) host-name_01")
    assert "rc=1" in s and "timed out after 30s" in s and "redacted" not in s


def test_alert_gmail_auth_failure_stays_readable_on_the_dashboard(alert_env):
    (alert_env / "smart-alert.log").write_text(
        "2026-10-01T19:08:00-04:00 ALERT SEND FAILED rc=1 for /dev/sdc [SAT]: "
        "smtplib.SMTPAuthenticationError: (535, b'5.7.8 Username and Password not accepted.')\n")
    r = run_alert(alert_env)
    assert r.status == "warn" and "Password not accepted" in r.summary and "redacted" not in r.summary
    assert "Password not accepted" in r.metrics["last_error"]


# =========================================================================== growth_watch
def history_size(path, t, nbytes):
    core.append_history({"t": t, "kind": "size", "path": path, "bytes": nbytes})


def sparse(path, size):
    with open(path, "wb") as f:
        f.truncate(size)


def test_du_sums_regular_files_without_following_symlinks(tmp_path):
    root, other = tmp_path / "root", tmp_path / "other"
    (root / "a" / "b").mkdir(parents=True)
    other.mkdir()
    (root / "f1").write_bytes(b"x" * 100)
    (root / "a" / "b" / "f2").write_bytes(b"y" * 200)
    (other / "big").write_bytes(b"z" * 5000)
    os.symlink(other, root / "link_dir")                      # must not be followed
    os.symlink(other / "big", root / "link_file")             # nor counted
    assert ch._du(str(root), 5)[:3] == (300, True, 0)
    assert ch._du(str(root / "f1"), 5)[:3] == (100, True, 0)  # a single file works too
    with pytest.raises(FileNotFoundError):
        ch._du(str(root / "nope"), 5)


def test_du_stops_at_budget(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_bytes(b"x" * 10)
    w = ch._du(str(tmp_path), 0)
    assert w.complete is False and w.size == 0 and w.files is None     # a partial walk yields no ledger either


def test_du_unreadable_subdir_counted_root_unreadable_raises(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "f").write_bytes(b"x" * 10)
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "f").write_bytes(b"x" * 99)
    locked.chmod(0)
    try:
        assert ch._du(str(tmp_path), 5)[:3] == (10, True, 1)
        with pytest.raises(PermissionError):
            ch._du(str(locked), 5)
    finally:
        locked.chmod(0o755)


def run_growth(paths, now=None, state=None, **opts):
    """`state` (a dict the caller keeps) plays the role of the task's persisted ctx.state across runs."""
    ctx = mkctx("growth_watch", now, paths=paths, **opts)
    if state:
        ctx.state.update(state)
    res = check_summary(ch.growth_watch(ctx))
    if state is not None:
        state.update(ctx.state)
    return res, ctx


def test_growth_runaway_warns_with_path_and_rate(tmp_path):
    p = tmp_path / "kavita_logs"
    p.mkdir()
    now = time.time()
    sparse(p / "kavita.log", 2 * 1024 ** 3)                   # apparent size 2 GiB, 24 h after an empty dir
    history_size(str(p), now - DAY, 0)
    r, _ = run_growth([{"path": str(p), "warn_gib_per_day": 0.5}], now)
    assert r.status == "warn" and r.alert is True
    assert "kavita_logs" in r.summary and "+2.00 GiB/d" in r.summary
    row = r.metrics["paths"][0]
    assert row["level"] == "warn" and row["size"] == "2.0 GiB" and r.metrics["worst_path"] == str(p)
    assert r.items[0]["level"] == "warn"


def test_growth_slow_growth_is_ok(tmp_path):
    p = tmp_path / "logs"
    p.mkdir()
    now = time.time()
    sparse(p / "f", 2 * 1024 ** 3)
    history_size(str(p), now - DAY, 2 * 1024 ** 3 - 10 * 1024 ** 2)   # +10 MiB/day
    r, _ = run_growth([{"path": str(p), "warn_gib_per_day": 0.5}], now)
    assert r.status == "ok" and "growth ok" in r.summary


def test_growth_shrinking_is_never_a_warning(tmp_path):
    p = tmp_path / "logs"
    p.mkdir()
    now = time.time()
    sparse(p / "f", 10 * 1024 ** 2)
    history_size(str(p), now - DAY, 9 * 1024 ** 3)
    r, _ = run_growth([{"path": str(p), "warn_gib_per_day": 0.5}], now)
    assert r.status == "ok" and r.metrics["paths"][0]["rate"].startswith("-")


def test_growth_first_run_measures_and_records_history(tmp_path):
    p = tmp_path / "fresh"
    p.mkdir()
    (p / "f").write_bytes(b"x" * 1000)
    now = time.time()
    r, ctx = run_growth([{"path": str(p)}], now)
    assert r.status == "ok" and "measuring" in r.summary
    recs = [h for h in core.read_history(60, "size") if h["path"] == str(p)]
    assert recs and recs[-1]["bytes"] == 1000 and recs[-1]["kind"] == "size"
    assert ctx.state["last"][str(p)]["bytes"] == 1000


def test_growth_too_short_a_span_gives_no_rate(tmp_path):
    p = tmp_path / "logs"
    p.mkdir()
    now = time.time()
    sparse(p / "f", 5 * 1024 ** 3)
    history_size(str(p), now - 2 * 3600, 0)                   # 2 h ago < min_span_hours (12): too noisy to extrapolate
    r, _ = run_growth([{"path": str(p)}], now)
    assert r.status == "ok" and r.metrics["paths"][0]["rate"] == "n/a"


def test_growth_picks_baseline_closest_to_24h(tmp_path):
    p = tmp_path / "logs"
    p.mkdir()
    now = time.time()
    sparse(p / "f", 3 * 1024 ** 3)
    history_size(str(p), now - 40 * 3600, 3 * 1024 ** 3)      # 40 h ago (flat then)
    history_size(str(p), now - 24.5 * 3600, 3 * 1024 ** 3 - 100 * 1024 ** 2)
    history_size(str(p), now - 1 * 3600, 3 * 1024 ** 3)       # too recent to use
    r, _ = run_growth([{"path": str(p), "warn_gib_per_day": 0.5}], now)
    assert r.status == "ok"                                   # ~+100 MiB/day, not +3 GiB or 0


def test_growth_cut_off_walk_is_unmeasured_never_rated_from_the_cache(tmp_path):
    # Regression (reviewer's reproduction): the path really grew 5 -> 30 GiB, but the walk is cut off. The old
    # code substituted the cached 5 GiB, computed "+0.00 GiB/d" against the 24 h baseline and reported ok.
    p = tmp_path / "logs"
    p.mkdir()
    sparse(p / "f", 30 * 1024 ** 3)
    now = time.time()
    history_size(str(p), now - DAY, 5 * 1024 ** 3)
    state = {"last": {str(p): {"t": now - 900, "bytes": 5 * 1024 ** 3}}}
    r, ctx = run_growth([{"path": str(p)}], now, state=state, walk_budget_s=0)
    row = r.items[0]
    assert row["level"] == "info"
    assert row["size"] == "n/a" and row["rate"] == "n/a" and row["intake"] == "n/a"      # no rate from stale data
    assert "unmeasured" in row["note"] and "walk cut off" in row["note"] and "5.0 GiB" in row["note"]
    assert r.status == "skipped" and "1/1 paths unmeasured" in r.summary and "cut off" in r.summary
    assert r.metrics["n_unmeasured"] == 1
    assert not [h for h in core.read_history(60, "size") if h["path"] == str(p)]    # no partial value in history
    assert ctx.state["last"][str(p)]["bytes"] == 5 * 1024 ** 3                      # cache untouched
    r, _ = run_growth([{"path": str(p)}], now, walk_budget_s=0)                      # no cache either
    assert r.status == "skipped" and r.items[0]["size"] == "n/a" and "unmeasured" in r.items[0]["note"]


def partial_du(real_du, unreadable_for=("bad",), missing_for=()):
    """Wrap _du: paths whose name contains a marker are cut off (or missing), the rest are walked for real."""
    def _du(path, budget):
        if any(m in path for m in missing_for):
            raise FileNotFoundError(2, "No such file or directory")
        return real_du(path, 0 if any(m in path for m in unreadable_for) else budget)
    return _du


def test_growth_unmeasured_paths_reach_summary_and_status_but_do_not_page(tmp_path, monkeypatch):
    now = time.time()
    good, bad = tmp_path / "good", tmp_path / "bad"
    for d in (good, bad):
        d.mkdir()
        sparse(d / "f", 1024 ** 3)
    history_size(str(good), now - DAY, 1024 ** 3)
    monkeypatch.setattr(ch, "_du", partial_du(ch._du))
    r, _ = run_growth([{"path": str(good)}, {"path": str(bad)}], now)
    # Regression: this used to be status "ok", "growth ok: 1 paths, ..." with the other path only in items.
    assert r.status == "info" and r.alert is True and "growth ok: 1 paths" in r.summary    # info never pages (LEVELS)
    assert "1/2 paths unmeasured (missing/no access/cut off)" in r.summary
    assert r.metrics["n_unmeasured"] == 1 and core.LEVELS[r.status] == 0
    rows = {i["path"]: i for i in r.items}
    assert rows[str(bad)]["level"] == "info" and "unmeasured" in rows[str(bad)]["note"]
    assert rows[str(good)]["level"] == "ok"
    assert r.items[0]["path"] == str(bad)                      # unmeasured rows sort ahead of healthy ones


def test_growth_renamed_dir_is_named_as_unmeasured(tmp_path, monkeypatch):
    # A renamed Kavita dir used to give an 'info/missing' row that the summary never mentioned.
    now = time.time()
    ok = tmp_path / "ok"
    ok.mkdir()
    (ok / "f").write_bytes(b"1")
    gone = tmp_path / "kavita_logs"
    r, _ = run_growth([{"path": str(ok)}, {"path": str(gone)}], now)
    assert r.status == "info" and "1/2 paths unmeasured" in r.summary
    row = [i for i in r.items if i["path"] == str(gone)][0]
    assert row["level"] == "info" and row["note"] == "unmeasured: missing"


def test_growth_blind_for_too_long_warns_and_recovery_clears_it(tmp_path):
    now = time.time()
    p = tmp_path / "gone"
    state = {"unmeasured_since": {str(p): now - 30 * 3600}}
    r, _ = run_growth([{"path": str(p)}], now, state=state)
    assert r.status == "warn" and r.alert is True and "blind >24h" in r.summary and "1/1 paths unmeasured" in r.summary
    assert r.items[0]["level"] == "warn" and "blind 30h" in r.items[0]["note"]
    r, _ = run_growth([{"path": str(p)}], now, state={"unmeasured_since": {str(p): now - 30 * 3600}},
                      unmeasured_warn_hours=0)                      # 0 switches the escalation off
    assert r.status == "skipped"
    r, _ = run_growth([{"path": str(p)}], now, state={"unmeasured_since": {str(p): now - 3600}})
    assert r.status == "skipped"                                     # blind for 1 h only: not yet a page
    p.mkdir()
    (p / "f").write_bytes(b"1")
    r, _ = run_growth([{"path": str(p)}], now, state=state)          # measurable again: the blind clock resets
    assert r.status == "ok" and str(p) not in state["unmeasured_since"]


def test_growth_blind_clock_starts_on_the_first_unmeasured_run(tmp_path):
    now = time.time()
    p = tmp_path / "gone"
    state = {}
    run_growth([{"path": str(p)}], now - 2 * 3600, state=state)
    assert state["unmeasured_since"][str(p)] == now - 2 * 3600
    run_growth([{"path": str(p)}], now, state=state)
    assert state["unmeasured_since"][str(p)] == now - 2 * 3600       # not reset by later runs


def test_growth_runaway_still_wins_over_unmeasured_and_both_are_in_the_summary(tmp_path, monkeypatch):
    now = time.time()
    loud, bad = tmp_path / "loud", tmp_path / "bad"
    for d in (loud, bad):
        d.mkdir()
    sparse(loud / "f", 3 * 1024 ** 3)
    history_size(str(loud), now - DAY, 0)
    monkeypatch.setattr(ch, "_du", partial_du(ch._du))
    r, _ = run_growth([{"path": str(loud)}, {"path": str(bad)}], now)
    assert r.status == "warn" and "loud +3.00 GiB/d" in r.summary and "1/2 paths unmeasured" in r.summary


def test_growth_walk_budget_is_one_total_shared_fairly(tmp_path, monkeypatch):
    # Regression: 20 s PER path meant many slow paths could exceed the task timeout (150 s => "error").
    paths = []
    for n in range(4):
        d = tmp_path / f"p{n}"
        d.mkdir()
        paths.append({"path": str(d)})
    shares = []

    def slow_du(path, budget):                                       # a tree that always eats its whole share
        shares.append(budget)
        time.sleep(budget)
        return ch._Walk(0, False, 0, None)
    monkeypatch.setattr(ch, "_du", slow_du)
    t0 = time.monotonic()
    r, _ = run_growth(paths, time.time(), walk_budget_s=0.4)
    assert time.monotonic() - t0 < 0.4 + 0.25                        # whole task stayed inside the single budget
    assert len(shares) == 4 and max(shares) - min(shares) < 0.05     # each path got its share, none was starved
    assert abs(sum(shares) - 0.4) < 0.1
    assert r.status == "skipped" and "4/4 paths unmeasured" in r.summary


def test_growth_walk_budget_is_clamped_below_the_task_timeout(tmp_path, monkeypatch):
    d = tmp_path / "d"
    d.mkdir()
    seen = []
    monkeypatch.setattr(ch, "_du", lambda p, b: seen.append(b) or ch._Walk(0, True, 0, {}))
    run_growth([{"path": str(d)}], time.time(), walk_budget_s=100000)
    assert seen and seen[0] <= 120 < core.REGISTRY["growth_watch"].timeout


def test_growth_missing_and_unconfigured_paths_do_nothing(tmp_path):
    r, _ = run_growth([{"path": str(tmp_path / "gone")}])
    assert r.status == "skipped" and r.items[0]["note"] == "unmeasured: missing"
    for bad in ([], [{"path": ""}], [{"nopath": 1}], [{"path": "relative/dir"}], ["/abs/string/not/dict"]):
        r, _ = run_growth(bad)
        assert r.status == "skipped" and "no paths" in r.summary


def test_growth_one_runaway_among_many_is_named_first(tmp_path):
    now = time.time()
    quiet, loud = tmp_path / "quiet", tmp_path / "loud"
    for d in (quiet, loud):
        d.mkdir()
    sparse(quiet / "f", 1024 ** 3)
    sparse(loud / "f", 3 * 1024 ** 3)
    history_size(str(quiet), now - DAY, 1024 ** 3)
    history_size(str(loud), now - DAY, 0)
    r, _ = run_growth([{"path": str(quiet)}, {"path": str(loud), "warn_gib_per_day": 1}], now)
    assert r.status == "warn" and "loud" in r.summary and "quiet" not in r.summary
    assert r.items[0]["path"] == str(loud) and r.metrics["n_over"] == 1


def test_growth_never_mutates_or_shells_out(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ch, "sh", fake_sh({}, calls))
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_bytes(b"1")
    run_growth([{"path": str(tmp_path / "d")}])
    assert calls == [] and (tmp_path / "d" / "f").read_bytes() == b"1"


# --- written-bytes ("intake") figure: a cleaner or rotation must not hide a runaway writer ---------------
MIB = 1024 ** 2
QUARTER_GIB = 1024 ** 3 // 4


def size_records(p):
    return [h for h in core.read_history(2 * DAY, "size") if h["path"] == str(p)]


def test_growth_cleaner_cannot_hide_a_runaway_writer(tmp_path):
    # Regression: Kavita writes 1 GiB/day and the retention cleaner (now in apply mode) deletes the oldest file as
    # fast. size(now) - size(24 h ago) is ~0, so the net-only detector never fired.
    p = tmp_path / "kavita_logs"
    p.mkdir()
    T, state = time.time(), {}
    for k in range(3):
        sparse(p / f"old{k}.log", QUARTER_GIB)
    for step in range(5):                                      # runs every 6 h over the last 24 h
        if step:
            sparse(p / f"new{step}.log", QUARTER_GIB)          # +0.25 GiB written ...
            os.unlink(p / (f"old{step - 1}.log" if step <= 3 else "new1.log"))   # ... and 0.25 GiB cleaned
        r, _ = run_growth([{"path": str(p), "warn_gib_per_day": 0.5}], T - DAY + step * 6 * 3600, state=state)
    row = r.metrics["paths"][0]
    assert row["rate"] == "+0.00 GiB/d"                        # net really is flat
    assert row["intake"] == "1.00 GiB/d" and row["level"] == "warn"
    assert r.status == "warn" and r.alert is True
    assert "kavita_logs" in r.summary and "1.00 GiB/d written" in r.summary
    assert r.metrics["worst_path"] == str(p) and r.metrics["worst_gib_day"] == 1.0
    assert size_records(p)[-1]["fresh_bytes"] == QUARTER_GIB   # history carries the per-run increment


def test_growth_big_static_file_is_not_intake(tmp_path):
    # A large file that was merely touched recently (or a slowly appended 600 MiB docker log) must not read as
    # "written in the last 24 h": only bytes that actually arrived count.
    p = tmp_path / "logs"
    p.mkdir()
    sparse(p / "app.log", 2 * 1024 ** 3)
    T, state = time.time(), {}
    for step in range(3):
        r, _ = run_growth([{"path": str(p), "warn_gib_per_day": 0.5}], T - DAY + step * 12 * 3600, state=state)
    assert r.status == "ok" and r.metrics["paths"][0]["intake"] == "0.00 GiB/d"


def test_growth_no_intake_figure_on_first_run_or_short_span(tmp_path):
    p = tmp_path / "logs"
    p.mkdir()
    sparse(p / "f", 3 * 1024 ** 3)                              # a big file the first walk has never seen
    T, state = time.time(), {}
    r, _ = run_growth([{"path": str(p)}], T - 3600, state=state)
    assert r.metrics["paths"][0]["intake"] == "n/a" and "fresh_bytes" not in size_records(p)[-1]
    sparse(p / "g", 1024 ** 3)
    r, _ = run_growth([{"path": str(p)}], T, state=state)       # 1 h of coverage < min_span_hours (12)
    assert r.metrics["paths"][0]["intake"] == "n/a" and r.status == "ok"
    assert size_records(p)[-1]["fresh_bytes"] == 1024 ** 3      # recorded for later runs to add up


def test_growth_logrotate_rename_is_not_new_data(tmp_path):
    p = tmp_path / "logs"
    p.mkdir()
    sparse(p / "syslog", 1024 ** 3)
    T, state = time.time(), {}
    run_growth([{"path": str(p)}], T - 3600, state=state)
    os.rename(p / "syslog", p / "syslog.1")                     # same inode: rotation, not intake
    sparse(p / "syslog", 100 * 1024)                            # fresh small file, below the 1 MiB ledger floor
    run_growth([{"path": str(p)}], T, state=state)
    assert size_records(p)[-1]["fresh_bytes"] == 0


def test_fresh_bytes_rules():
    led = {"t": 1000.0, "sizes": {"1": 100 * MIB, "2": 50 * MIB}}
    files = {1: (150 * MIB, 2000.0),     # grew by 50
             2: (10 * MIB, 2000.0),      # truncated: never negative
             3: (7 * MIB, 1500.0),       # new, written since the last walk: counts in full
             4: (9 * MIB, 500.0)}        # unknown inode with an old mtime: moved in, not written
    assert ch._fresh_bytes(files, led) == 57 * MIB
    assert ch._fresh_bytes(None, led) is None                   # too many files / cut off
    assert ch._fresh_bytes(files, None) is None                 # no ledger yet
    assert ch._fresh_bytes(files, {}) is None and ch._fresh_bytes(files, {"t": "x", "sizes": {}}) is None   # damaged state


def test_intake_per_day_spans_gaps_between_runs():
    now = 1_000_000.0
    mk = lambda **kw: {"kind": "size", "path": "/p", **kw}
    one = [mk(t=now - 3600, fresh_bytes=6 * 1024 ** 3, since=now - 3 * DAY)]   # first walk after 3 days of cut-offs
    assert abs(ch._intake_per_day(one, "/p", now, 12 * 3600) / 1024 ** 3 - 6 * DAY / (3 * DAY + 0)) < 0.01
    assert ch._intake_per_day(one, "/other", now, 12 * 3600) is None
    short = [mk(t=now - 600, fresh_bytes=5, since=now - 3600)]
    assert ch._intake_per_day(short, "/p", now, 12 * 3600) is None               # 1 h of coverage
    old = [mk(t=now - 2 * DAY, fresh_bytes=5, since=now - 3 * DAY), mk(t="junk", fresh_bytes=1, since=0)]
    assert ch._intake_per_day(old, "/p", now, 12 * 3600) is None                 # outside 24 h / malformed rows skipped


def test_growth_ledger_survives_a_cut_off_run_and_the_gap_counts_as_time(tmp_path, monkeypatch):
    p = tmp_path / "logs"
    p.mkdir()
    sparse(p / "a", 1024 ** 3)
    T, state = time.time(), {}
    run_growth([{"path": str(p), "warn_gib_per_day": 0.5}], T - 20 * 3600, state=state)
    real = ch._du
    monkeypatch.setattr(ch, "_du", lambda path, b: real(path, 0))          # a run whose walk is cut off
    run_growth([{"path": str(p)}], T - 10 * 3600, state=state)
    monkeypatch.setattr(ch, "_du", real)
    sparse(p / "b", QUARTER_GIB * 2)                                       # 0.5 GiB arrived meanwhile
    r, _ = run_growth([{"path": str(p), "warn_gib_per_day": 0.5}], T, state=state)
    rec = size_records(p)[-1]
    assert rec["fresh_bytes"] == QUARTER_GIB * 2 and abs(rec["since"] - (T - 20 * 3600)) < 1
    # 0.5 GiB over the 20 h the two complete walks span = 0.6 GiB/d > 0.5
    assert r.status == "warn" and r.metrics["paths"][0]["intake"] == "0.60 GiB/d"


def test_growth_too_many_large_files_switches_intake_off_but_keeps_net(tmp_path, monkeypatch):
    p = tmp_path / "many"
    p.mkdir()
    for k in range(3):
        sparse(p / f"f{k}", 2 * MIB)
    monkeypatch.setattr(ch, "LEDGER_MAX_FILES", 2)
    w = ch._du(str(p), 5)
    assert w.complete and w.size == 6 * MIB and w.files is None
    now = time.time()
    state = {"ledger": {str(p): {"t": now - 3600, "sizes": {"1": 1}}}}
    r, _ = run_growth([{"path": str(p)}], now, state=state)
    assert str(p) not in state["ledger"]                                   # nothing unbounded is persisted
    assert "figure off" in r.items[0]["note"] and r.items[0]["size"] == "6.0 MiB"
    assert "fresh_bytes" not in size_records(p)[-1]


def test_du_collects_large_files_for_the_ledger(tmp_path):
    (tmp_path / "sub").mkdir()
    sparse(tmp_path / "sub" / "big", 3 * MIB)
    (tmp_path / "small").write_bytes(b"x" * 1000)
    os.symlink(tmp_path / "sub" / "big", tmp_path / "lnk")                 # symlinks are not counted twice
    w = ch._du(str(tmp_path), 5)
    st = os.stat(tmp_path / "sub" / "big")
    assert w.files == {st.st_ino: (3 * MIB, st.st_mtime)} and w.size == 3 * MIB + 1000
    assert ch._du(str(tmp_path / "sub" / "big"), 5).files == {st.st_ino: (3 * MIB, st.st_mtime)}   # single-file path


def test_growth_state_forgets_paths_removed_from_config(tmp_path):
    p = tmp_path / "d"
    p.mkdir()
    state = {"last": {"/old": {"t": 1, "bytes": 1}}, "ledger": {"/old": {"t": 1, "sizes": {}}},
             "unmeasured_since": {"/old": 1}}
    run_growth([{"path": str(p)}], state=state)
    assert list(state["last"]) == [str(p)] and list(state["ledger"]) == [str(p)] and state["unmeasured_since"] == {}


# =========================================================================== config_drift
MOUNTINFO = """\
30 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw
165 30 8:113 / /media/SandiskSSD rw,relatime shared:138 - ext4 /dev/sdh1 rw
300 30 8:113 /plex /var/snap/plex/Media rw,relatime shared:138 - ext4 /dev/sdh1 rw
173 30 8:98 / /mnt/backup/immich ro,noatime shared:146 - ext4 /dev/sdg2 ro
210 30 0:55 / /media/seagate18tb rw,relatime shared:200 - fuseblk /dev/sdc1 rw
26 30 0:26 / /proc rw,nosuid,nodev,noexec,relatime shared:13 - proc proc rw
"""


def tune2fs_out(blocks, reserved):
    return (f"Filesystem state:         clean\nBlock count:              {blocks}\n"
            f"Reserved block count:     {reserved}\nBlock size:               4096\n")


HEALTHY_SH = {
    ("crontab", "-l", "-u", "root"): (0, "# m h dom mon dow command\n17 3 * * * /usr/local/bin/other-job\n", ""),
    ("snap", "get", "-d", "system", "refresh.retain"): (0, '{\n\t"refresh.retain": 2\n}\n', "WARNING: output will change\n"),
    ("docker", "ps", "-aq"): (0, "aaa\nbbb\n", ""),
    ("docker", "inspect"): (0, '/web\tjson-file\t{"max-file":"3","max-size":"10m"}\n/db\tjson-file\t{"max-size":"1m"}\n', ""),
    ("systemd-tmpfiles", "--cat-config"): (0, "# /usr/lib/tmpfiles.d/tmp.conf\n# comment\nD /tmp 1777 root root 7d\n#q /var/tmp 1777 root root 30d\n", ""),
    ("tune2fs", "-l", "/dev/nvme0n1p2"): (0, tune2fs_out(488102912, 4881029), ""),
    ("tune2fs", "-l", "/dev/sdh1"): (0, tune2fs_out(488378368, 4883783), ""),
    ("tune2fs", "-l", "/dev/sdg2"): (0, tune2fs_out(2147483648, 0), ""),
}


@pytest.fixture
def drift_env(tmp_path, monkeypatch):
    (tmp_path / "usr").mkdir()
    (tmp_path / "run").mkdir()
    (tmp_path / "etc").mkdir()
    (tmp_path / "journald.conf").write_text("[Journal]\n#SystemMaxUse=\n")
    (tmp_path / "etc" / "10-homelab.conf").write_text("[Journal]\nSystemMaxUse=1G\n")
    (tmp_path / "daemon.json").write_text(json.dumps({"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "3"}}))
    (tmp_path / "mountinfo").write_text(MOUNTINFO)
    monkeypatch.setattr(ch, "JOURNALD_MAIN", tmp_path / "journald.conf")
    monkeypatch.setattr(ch, "JOURNALD_DIRS", [tmp_path / "usr", tmp_path / "run", tmp_path / "etc"])
    monkeypatch.setattr(ch, "DOCKER_DAEMON_JSON", tmp_path / "daemon.json")
    monkeypatch.setattr(ch, "MOUNTINFO", tmp_path / "mountinfo")
    monkeypatch.setattr(ch, "_is_root", lambda: True)
    table, calls = dict(HEALTHY_SH), []
    monkeypatch.setattr(ch, "sh", fake_sh(table, calls))
    return tmp_path, table, calls


def run_drift(snap_retain=None, **opts):
    cfg = {"global": {}, "tasks": {"config_drift": opts, "snap_revisions": {"retain": snap_retain}} if snap_retain else
           {"config_drift": opts}, "caps": {}, "protected": {}}
    return check_summary(ch.config_drift(core.Ctx(cfg, "config_drift", apply=False)))


def whats(r):
    return [i["what"] for i in r.items]


def test_drift_healthy_host_is_ok_and_silent(drift_env):
    r = run_drift()
    assert r.status == "ok" and r.alert is False and r.items == [] and r.metrics == {"drifts": 0, "skipped": [], "kinds": []}
    assert r.summary == "no config drift"


def test_drift_never_pages_even_with_findings(drift_env):
    tmp, table, _ = drift_env
    (tmp / "etc" / "10-homelab.conf").unlink()
    r = run_drift()
    assert r.status == "info" and r.alert is False and whats(r) == ["journald cap"]
    assert "SystemMaxUse=unset" in r.items[0]["actual"] and "10-homelab.conf" in r.items[0]["fix"]


@pytest.mark.parametrize("setup, drifted", [
    (lambda t: (t / "etc" / "10-homelab.conf").write_text("[Journal]\nSystemMaxUse=1024M\n"), False),    # same size, other unit
    (lambda t: (t / "etc" / "10-homelab.conf").write_text("[Journal]\nSystemMaxUse=2G\n"), True),
    (lambda t: (t / "etc" / "10-homelab.conf").write_text("SystemMaxUse=1G\n"), True),                  # outside [Journal]
    (lambda t: (t / "etc" / "10-homelab.conf").write_text("[Journal]\n#SystemMaxUse=1G\n"), True),       # commented out
    (lambda t: (t / "etc" / "10-homelab.conf").write_text("[Journal]\nSystemMaxUse=\n"), True),          # reset to default
    (lambda t: ((t / "etc" / "10-homelab.conf").unlink(), (t / "usr" / "50-vendor.conf").write_text("[Journal]\nSystemMaxUse=1G\n")), False),
    (lambda t: (t / "etc" / "90-late.conf").write_text("[Journal]\nSystemMaxUse=4G\n"), True),          # later name wins
    (lambda t: ((t / "usr" / "10-homelab.conf").write_text("[Journal]\nSystemMaxUse=4G\n")), False),   # /etc copy overrides /usr/lib
    (lambda t: (t / "journald.conf").write_text("[Journal]\nSystemMaxUse=3G\n"), False),                # drop-in beats main file
])
def test_drift_journald_effective_value(drift_env, setup, drifted):
    setup(drift_env[0])
    assert ("journald cap" in whats(run_drift())) is drifted


def test_drift_journald_expectation_is_configurable(drift_env):
    assert "journald cap" in whats(run_drift(expect_journald_system_max_use="500M"))


def test_drift_root_cron_lines_listed(drift_env):
    _, table, _ = drift_env
    table[("crontab", "-l", "-u", "root")] = (0, (
        "# find /tmp -atime +7 is a comment\n"
        "30 5 * * 0 /usr/bin/journalctl --vacuum-time=7d\n"
        "0 6 * * 0 /usr/bin/find /tmp   -type f -atime +7 -delete\n"
        "30 6 * * 0 /usr/sbin/logrotate /etc/logrotate.conf\n"
        "SECRET_TOKEN=hunter2\n17 3 * * * /usr/local/bin/other-job\n"), "")
    r = run_drift()
    assert whats(r) == ["root cron"] * 3 and r.metrics["drifts"] == 3
    assert "journalctl --vacuum" in r.items[0]["actual"] and "find /tmp -type f" in r.items[1]["actual"]
    assert "hunter2" not in json.dumps(r.items) and "root cron x3" in r.summary


def test_drift_root_only_checks_are_skipped_for_non_root(drift_env, monkeypatch):
    _, table, calls = drift_env
    monkeypatch.setattr(ch, "_is_root", lambda: False)
    table[("tune2fs", "-l", "/dev/sdh1")] = (0, tune2fs_out(488378368, 99999999), "")
    r = run_drift()
    assert r.status == "ok" and r.metrics["skipped"] == ["root cron", "reserved blocks"]
    assert "skipped" in r.summary
    assert not [c for c in calls if c[0] in ("crontab", "tune2fs")]


def test_drift_snap_retain(drift_env):
    _, table, _ = drift_env
    key = ("snap", "get", "-d", "system", "refresh.retain")
    table[key] = (1, "", 'error: snap "core" has no "refresh.retain" configuration option\n')   # real unset output
    r = run_drift()
    assert whats(r) == ["snap retain"] and "unset" in r.items[0]["actual"] and r.items[0]["fix"] == "snap set system refresh.retain=2"
    table[key] = (0, '{"refresh.retain": 3}', "")
    assert whats(run_drift()) == ["snap retain"]
    table[key] = (0, '{"refresh.retain": 4}', "")
    assert run_drift(snap_retain=4).status == "ok"                   # default follows [tasks.snap_revisions] retain
    assert run_drift(expect_snap_retain=4).status == "ok"
    table[key] = (127, "", "not found")                               # snap not installed: skip, do not guess
    r = run_drift()
    assert r.status == "ok" and r.metrics["skipped"] == ["snap"]
    table[key] = (0, "garbage", "")
    assert run_drift().metrics["skipped"] == ["snap"]


def test_drift_docker_daemon_json(drift_env):
    tmp, _, _ = drift_env
    dj = tmp / "daemon.json"
    dj.write_text(json.dumps({"runtimes": {"nvidia": {}}}))
    r = run_drift()
    assert "docker log-opts" in whats(r)
    dj.write_text(json.dumps({"log-driver": "json-file", "log-opts": {"max-file": "3"}}))
    assert "docker log-opts" in whats(run_drift())                    # max-file alone does not bound size
    dj.write_text(json.dumps({"log-driver": "local"}))
    assert "docker log-opts" not in whats(run_drift())                # `local` rotates by default
    dj.write_text("{ not json")
    assert whats(run_drift())[0] == "docker log-opts"
    dj.unlink()
    assert "docker log-opts" in whats(run_drift())


def test_drift_containers_without_log_rotation(drift_env):
    _, table, _ = drift_env
    table[("docker", "inspect")] = (0, ('/web\tjson-file\t{"max-size":"10m"}\n'
                                        '/buildx_buildkit_x0\tjson-file\t{}\n'
                                        '/old\tjson-file\tnull\n'
                                        '/sysl\tlocal\t{}\n'), "")
    r = run_drift()
    assert whats(r) == ["container logs"]
    assert "2 container(s)" in r.items[0]["actual"] and "buildx_buildkit_x0" in r.items[0]["actual"]
    table[("docker", "ps", "-aq")] = (1, "", "permission denied")       # docker unreachable: nothing to report
    assert run_drift().status == "ok"


@pytest.mark.parametrize("out, drifted", [
    ("D /tmp 1777 root root 7d\n", False),
    ("D /tmp 1777 root root 30d\n", True),                             # the stock Ubuntu value
    ("D! /tmp 1777 root root 1w\n", False),                            # modifier char, other unit
    ("q /tmp 1777 root root 12h\n", False),
    ("D /tmp 1777 root root 7d\nD /tmp 1777 root root 90d\n", False),  # duplicate line is ignored by tmpfiles: first wins
    ("D /tmp 1777 root root -\n", True),
    ("d /var/tmp 1777 root root 7d\n", True),                          # nothing about /tmp at all
    ("D /tmp 1777 root root 1M\n", True),                              # unparsable unit: report, do not guess
])
def test_drift_tmpfiles_age(drift_env, out, drifted):
    _, table, _ = drift_env
    table[("systemd-tmpfiles", "--cat-config")] = (0, out, "")
    assert ("/tmp age" in whats(run_drift())) is drifted


def test_drift_tmpfiles_probe_failure_is_skipped(drift_env):
    _, table, _ = drift_env
    table[("systemd-tmpfiles", "--cat-config")] = (127, "", "")
    assert run_drift().metrics["skipped"] == ["tmpfiles"]


def test_drift_reserved_blocks(drift_env):
    _, table, calls = drift_env
    table[("tune2fs", "-l", "/dev/sdh1")] = (0, tune2fs_out(2125640879, 106282043), "")     # 5.0 %
    r = run_drift()
    assert whats(r) == ["reserved blocks"]
    it = r.items[0]
    assert "5.0% reserved on /media/SandiskSSD" in it["actual"] and it["fix"] == "tune2fs -m 1 /dev/sdh1"
    assert [c[2] for c in calls if c[0] == "tune2fs"].count("/dev/sdh1") == 1       # bind mount of same device deduped
    assert not [c for c in calls if c[:3] == ["tune2fs", "-l", "/dev/sdc1"]]        # fuseblk / non-ext4 ignored
    assert run_drift(expect_reserved_pct=5).status == "ok"
    table[("tune2fs", "-l", "/dev/sdh1")] = (1, "", "permission denied")             # probe fails: no claim either way
    assert run_drift().status == "ok"


def test_drift_one_failing_check_does_not_hide_the_others(drift_env, monkeypatch):
    tmp, _, _ = drift_env
    (tmp / "etc" / "10-homelab.conf").unlink()
    monkeypatch.setattr(ch, "_chk_snap", lambda ctx: 1 / 0)
    r = run_drift()
    assert whats(r) == ["journald cap"] and r.metrics["skipped"] == ["snap"]


def test_drift_everything_wrong_summary_and_row_cap(drift_env):
    tmp, table, _ = drift_env
    (tmp / "etc" / "10-homelab.conf").unlink()
    table[("crontab", "-l", "-u", "root")] = (0, "".join(f"{i} 6 * * 0 /usr/sbin/logrotate /x{i}\n" for i in range(20)), "")
    table[("snap", "get", "-d", "system", "refresh.retain")] = (1, "", 'error: snap "core" has no "refresh.retain" configuration option')
    table[("systemd-tmpfiles", "--cat-config")] = (0, "D /tmp 1777 root root 30d\n", "")
    table[("tune2fs", "-l", "/dev/sdg2")] = (0, tune2fs_out(1000, 100), "")
    r = run_drift()
    assert r.status == "info" and r.metrics["drifts"] == 24 and len(r.items) == 12
    assert r.metrics["kinds"] == ["/tmp age", "journald cap", "reserved blocks", "root cron", "snap retain"]


def test_drift_only_runs_read_only_commands(drift_env):
    tmp, table, calls = drift_env
    (tmp / "etc" / "10-homelab.conf").unlink()
    run_drift()
    read_only = {("crontab", "-l"), ("snap", "get"), ("docker", "ps"), ("docker", "inspect"),
                 ("systemd-tmpfiles", "--cat-config"), ("tune2fs", "-l")}
    assert calls and all((c[0], c[1]) in read_only for c in calls), calls


# =========================================================================== cross-task guarantees
def test_all_results_fit_the_contract(tmp_path, smart_env, alert_env, drift_env):
    now = time.time()
    write_attrlog(smart_env / "attr", "A_B-SER1.ata", now, lambda a: {5: 0})
    results = [run_smart(smart_env, now), run_alert(alert_env), run_growth([{"path": str(tmp_path)}])[0], run_drift()]
    for r in results:
        assert isinstance(r, core.Result) and len(r.items) <= 12
        assert len(json.dumps(r.metrics)) < 4000                    # shipped to the browser on every poll
        json.dumps([r.metrics, r.items])                            # JSON-serialisable


# =========================================================================== SPEC5 issue_key for smart_trend: disk identity + attribute + DECADE
def smart_fp(res):
    from homelab_maint import acks
    fp = acks.fingerprint("smart_trend", res, res.status)
    assert fp.mode == "explicit" and fp.ackable and res.issue_key
    return str(fp)


def grow_realloc(n, temp=40):
    return lambda age: ({5: 0, 197: 0, 198: 0, 199: 37, 9: 100} if age > 6.5 else {5: n, 197: 0, 198: 0, 199: 37, 9: 200})


def test_smart_key_is_per_disk_with_the_decade_of_the_growth_and_never_the_temperature(smart_env):
    now = time.time()
    write_attrlog(smart_env / "attr", "WDC_WD181KFGX_68AFPN0-4YHXY35P.ata", now, grow_realloc(3), temp=41)
    a = run_smart(smart_env, now)
    assert a.issue_key == f"smart:{a.metrics['devices'][0]['model']}=realloc:b1"                      # model + serial tail, the attribute, the decade
    write_attrlog(smart_env / "attr", "WDC_WD181KFGX_68AFPN0-4YHXY35P.ata", now, grow_realloc(7), temp=44)         # +3 -> +7, 41 C -> 44 C
    b = run_smart(smart_env, now)
    assert b.issue_key == a.issue_key and smart_fp(a) == smart_fp(b)                                    # the same error: volatile numbers
    write_attrlog(smart_env / "attr", "WDC_WD181KFGX_68AFPN0-4YHXY35P.ata", now, grow_realloc(8000), temp=44)      # +8000 is "warn" too: but another error
    c = run_smart(smart_env, now)
    assert c.status == "warn" and c.issue_key.endswith("realloc:b4") and smart_fp(c) != smart_fp(a)


def test_smart_key_covers_every_failing_disk_and_survives_a_kernel_name_swap(smart_env):
    now = time.time()
    ids = ["A_ONE-SER1111.ata", "B_TWO-SER2222.ata", "C_THREE-SER3333.ata", "D_FOUR-SER4444.ata"]
    for i in ids:
        write_attrlog(smart_env / "attr", i, now, grow_realloc(2))
    a = run_smart(smart_env, now)
    assert a.summary.endswith("(+1 more)") and a.issue_key.count("realloc:b1") == 4                     # four disks, the summary names three
    os.symlink("../../sdb", smart_env / "byid" / "ata-A_ONE-SER1111")                                   # the kernel names it sdb now (it was sda last boot) ...
    assert run_smart(smart_env, now).issue_key == a.issue_key                                           # ... the identity is model + serial tail
    (smart_env / "attr" / "attrlog.D_FOUR-SER4444.ata.csv").unlink()                                    # one disk recovered / went away
    write_attrlog(smart_env / "attr", "E_FIVE-SER5555.ata", now, grow_realloc(2))                       # and another failing took its place: same count, same shape
    assert smart_fp(run_smart(smart_env, now)) != smart_fp(a)


def test_smart_key_words_for_hot_stale_and_unreadable_disks():
    bad = [{"model": "M1 0001", "note": "realloc +12, CRC +1, 72C"}, {"model": "M2 0002", "note": "smartd data 9h old"},
           {"model": "M3 0003", "note": "unreadable: Permission denied"}, {"model": "M4 0004", "note": "no parsable rows"},
           {"model": "N0", "note": "71C"}, {"model": "M5", "note": ""}]
    assert ch._smart_key(bad) == ("smart:M1 0001=CRC:b1,M1 0001=hot,M1 0001=realloc:b2,M2 0002=stale,M3 0003=unreadable,M4 0004=no-parsable-rows,N0=hot")
    assert ch._smart_key([]) is None and ch._smart_key([{"model": "M", "note": ""}]) is None
