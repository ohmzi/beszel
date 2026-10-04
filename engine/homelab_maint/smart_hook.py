"""smart_hook: the smartd `-M exec` hook, `homelab-maint smart-event` (replaces /usr/local/sbin/smart-alert.sh).

STDLIB ONLY, AND NOTHING FROM THIS PACKAGE IS IMPORTED AT MODULE LEVEL. This is deliberate: a disk-failure alert must survive a
bad deploy of any other module (a syntax error in cleaners.py, core.py or notify.py used to make `tasks.native` unimportable,
so the hook died before it could even write its local line). Order of work, each step independent of the next:
  1. the local record in /var/log/smart-alert.log (alert_path_health reads it) -- plain file append, no imports;
  2. the page: `from . import notify` happens lazily inside try/except (BaseException), so a notify that cannot be imported or
     that raises is logged as "ALERT SEND FAILED rc=127|1 ..." and reported through the exit status, never as a traceback;
  3. the exit status: 0 = delivered or deliberately held back by policy (dedupe window, quiet hours, mute); 1 = the owner was
     NOT told. smartd only records an executable's exit status in syslog, so a non-zero status is harmless to it, and it lets the
     forwarding stub that replaces smart-alert.sh fall back to the legacy script (etc/legacy-retirement.toml, smartd-alert-hook):
         /usr/local/sbin/homelab-maint smart-event "$@" && exit 0
         exec <legacy_dir>/smart-alert.sh "$@"
     (`python3 -m homelab_maint.smart_hook "$@"` is the same entry without cli.py, for a stub that wants even fewer imports.)
The hook prints NOTHING: smartd treats any output of a hook as a hook failure.

=====================================================================================================================
PARITY.md  smart_event  (smart-alert.sh)
=====================================================================================================================
Checked identical (sandboxed script vs port, log lines compared after masking the timestamp): device/message from the
SMARTD_* environment with the positional fallback ($2/$3), the local record is written BEFORE any send, line formats
"<iso> device=.. type=.. failtype=.. <msg>", "<iso> alert sent for <dev>", "<iso> ALERT SEND FAILED rc=<n> for <dev>:
<err tail>" (parsed by checks_health.alert_path_health), subject "SMART <failtype> on <host>: <device>", and the LAST 400
characters of an error text (tail -c 400).
Intentional differences: delivery is notify.send (kind=alert) through `_notify`, never the bridge script; a missing or broken
notify module logs "ALERT SEND FAILED rc=127 ... notify module unavailable (<ErrorType>)" (script: "no bridge at"); a failure
with no error text says "no reason logged" (script: a dangling ": "); a send that notify skipped on purpose (dedupe window,
quiet hours, muted) logs "alert not sent for <dev>: <why>", which alert_path_health reads as neither failure nor success;
newlines in the smartd message are folded to spaces (the script wrote raw newlines into a one-event-per-line log) and phone
numbers, e-mail addresses and secrets in error text are redacted (the log is world readable); severity is graded
(health/sector/self-test failures crit, temperature/usage warn, EmailTest = test event) and repeats share a dedupe_key.
EXIT STATUS: the script always exited 0; this exits 1 when nothing was delivered (see 3 above) so the stub can fall back.
"""
from __future__ import annotations

import dataclasses
import os
import re
import socket
import sys
import time
from datetime import datetime
from typing import Any

SMART_LOG = "/var/log/smart-alert.log"
_SMART_CRIT = {"Health", "FailedHealthCheck", "FailedReadSmartData", "FailedReadSmartErrorLog",
               "FailedReadSmartSelfTestLog", "FailedOpenDevice", "CurrentPendingSector",
               "OfflineUncorrectableSector", "SelfTest", "ErrorCount"}
_SMART_WARN = {"Usage", "Temperature"}

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w.])\+?\d[\d ().-]{8,}\d(?![\w.])")
_SECRET = re.compile(r"(?i)\b(pass(?:word|wd)?|token|secret|api[_-]?key|authorization)\b[ \t]*[=:][ \t]*\S+")


def ascii_line(s: Any, n: int = 140) -> str:
    """Summaries can become an SMS: one line, ASCII only, bounded."""
    return re.sub(r"[^\x20-\x7e]", "?", " ".join(str(s).split()))[:n]


def scrub(text: Any, n: int = 200, tail: bool = False) -> str:
    """Logs and notes may be world readable: drop secrets, e-mail addresses and phone numbers (BEFORE cutting, so a
    secret is never cut in half and leaked). tail=True keeps the END of long text (the reason is usually last)."""
    s = _SECRET.sub(r"\1=<redacted>", " ".join(str(text).split()))
    s = _EMAIL.sub("<email>", s)
    s = _PHONE.sub(lambda m: "<number>" if sum(c.isdigit() for c in m.group()) >= 10 else m.group(), s)
    return ascii_line(s[-n:] if tail else s, n)


def _iso_now(now: float | None) -> str:
    """`date -Is`: 2026-10-01T19:04:25-04:00."""
    return datetime.fromtimestamp(time.time() if now is None else now).astimezone().isoformat(timespec="seconds")


def _log_line(s: Any, n: int = 1000) -> str:
    """One event = one line: fold whitespace/newlines and drop control characters."""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", " ".join(str(s).split()))[:n]


def _audit_failed(event: dict, why: str) -> None:
    """Best effort: the audit trail is a bonus, the local log line is the record. Never raises."""
    try:
        from . import core
        core.audit("notify", "send", str(event.get("title", ""))[:80], 0, why)
    except BaseException:                                       # noqa: BLE001
        pass


def _notify(event: dict) -> dict:
    """Thin bridge to notify.send, the ONE notification path. Imports notify lazily and survives anything it does.
    Returns {"ok", "handled", "rc", "note"}: ok = delivered; handled = delivered OR intentionally not sent by policy
    (dedupe window, quiet hours, muted), which is not a failure; rc 127 = the notify module could not be imported."""
    try:
        from . import notify
    except BaseException as exc:                                # noqa: BLE001  (ImportError, SyntaxError in a sibling, ...)
        why = f"failed rc=127 notify module unavailable ({type(exc).__name__})"
        _audit_failed(event, why)
        return {"ok": False, "handled": False, "rc": 127, "note": "notify module unavailable"}
    try:
        cls = notify.Event
        names = {f.name for f in dataclasses.fields(cls)} if dataclasses.is_dataclass(cls) else set(event)
        d = notify.send(cls(**{k: v for k, v in event.items() if k in names}))
        get = (lambda k, dflt=None: d.get(k, dflt)) if isinstance(d, dict) else (lambda k, dflt=None: getattr(d, k, dflt))
        ok = bool(get("ok", d if isinstance(d, bool) else False))
        note = get("note", "") or get("skipped", "") or ""
        return {"ok": ok, "handled": ok or bool(get("handled", False)), "rc": 0 if ok else 1, "note": scrub(note, 400, tail=True)}
    except BaseException as exc:                                # noqa: BLE001  (a hook must never raise into smartd)
        return {"ok": False, "handled": False, "rc": 1, "note": scrub(f"{type(exc).__name__}: {exc}")}


def smart_event(env: dict | None = None, argv: list[str] | None = None, *, log_path: str | None = None,
                now: float | None = None) -> int:
    """smartd hook. `argv` is the argument list smartd passed, i.e. the script's [$1, $2, $3]: argv[1] ($2) and argv[2]
    ($3) are only a fallback for SMARTD_DEVICE / SMARTD_MESSAGE. The local record is written FIRST. Returns 0 when the
    owner was told (or policy held the message back on purpose), 1 when not; never raises, prints nothing."""
    path = log_path or SMART_LOG
    dev_l = "unknown"

    def log(line: str) -> None:
        try:
            with open(path, "a") as f:
                f.write(f"{_iso_now(now)} {line}\n")
        except BaseException:                                   # noqa: BLE001  (the alert must still go out if the log cannot be written)
            pass

    try:
        env = os.environ if env is None else env
        argv = list(argv or [])
        device = env.get("SMARTD_DEVICE") or (argv[1] if len(argv) > 1 else "unknown")
        message = env.get("SMARTD_MESSAGE") or (argv[2] if len(argv) > 2 else "no message")
        failtype = env.get("SMARTD_FAILTYPE") or "unknown"
        dtype = env.get("SMARTD_DEVICETYPE") or "unknown"
        dev_l, msg_l = _log_line(device, 200), _log_line(message)
        log(f"device={dev_l} type={_log_line(dtype, 40)} failtype={_log_line(failtype, 60)} {msg_l}")      # 1. the record, first
        host = socket.gethostname()
        sev = "crit" if failtype in _SMART_CRIT else ("warn" if failtype in _SMART_WARN else ("info" if failtype == "EmailTest" else "crit"))
        res = _notify({"kind": "test" if failtype == "EmailTest" else "alert", "severity": sev,
                       "title": f"SMART {failtype} on {host}: {dev_l}", "summary": scrub(message, 130),
                       "details": scrub(env.get("SMARTD_FULLMESSAGE") or message, 1500), "task": "smart_event",
                       "dedupe_key": f"smart:{dev_l}:{failtype}",
                       "facts": {"device": dev_l, "type": dtype, "failtype": failtype, "host": host}})     # 2. the page
        if res.get("ok"):
            log(f"alert sent for {dev_l}")
            return 0
        if res.get("handled"):
            # notify decided not to send (dedupe window, quiet hours, muted): not a failure, so it must not read as one
            log(f"alert not sent for {dev_l}: {scrub(res.get('note') or 'suppressed by notification policy', 200)}")
            return 0
        log(f"ALERT SEND FAILED rc={res.get('rc', 1)} for {dev_l}: {scrub(res.get('note') or 'no reason logged', 400, tail=True)}")
        return 1                                                                                            # 3. tell the stub
    except BaseException as exc:                                # noqa: BLE001  (nothing may escape into smartd)
        log(f"ALERT SEND FAILED rc=1 for {dev_l}: {scrub(f'{type(exc).__name__}: {exc}', 400, tail=True)}")
        return 1


def smart_event_main(argv: list[str] | None = None) -> int:
    """`homelab-maint smart-event [address subject ...]` and `python3 -m homelab_maint.smart_hook ...`: the cli glue calls
    this. argv defaults to the process arguments."""
    try:
        return smart_event(argv=list(sys.argv[1:] if argv is None else argv))
    except BaseException:                                       # noqa: BLE001
        return 1


main = smart_event_main

if __name__ == "__main__":
    sys.exit(smart_event_main())
