#!/usr/bin/env python3
"""container-audit - container health and log-error audit.

Added 2026-10-04 during the /home/ohmz migration (see docs/MIGRATION.md), where
every container was moved to a new bind-mount root and recreated. This is the
standing version of the check done by hand at the end of that migration, so a
container that quietly wedges on a path move is caught without anyone looking.

What it checks, per container:
  * state        - not running, or exited, is a finding
  * health       - a healthcheck reporting unhealthy is a finding
  * restarts     - a non-zero RestartCount is reported (crash-loop signal)
  * logs         - lines since the previous audit matching an error signature

Read-only. It never starts, stops, restarts or removes anything: an audit that
mutates the thing it measures is not an audit.

Writes a status JSON that homelab-maint reads via success.status_json:
    /var/lib/homelab-maint/container-audit.json
    {"result": "ok"|"warn"|"fail", "checked": N, "issues": N, ...}

Exit codes: 0 clean/warn, 1 at least one container failed.

  container-audit.py [--since-hours H] [--json PATH] [--quiet]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

STATE_DIR = "/var/lib/homelab-maint"
STATUS_JSON = os.path.join(STATE_DIR, "container-audit.json")
LAST_RUN = os.path.join(STATE_DIR, "container-audit.last")

# One container name per line, '#' comments allowed. For containers that are
# deliberately not running (on-demand stacks such as ComfyUI): without this a
# stopped-but-intentional container would alert on every run.
IGNORE_FILE = "/etc/homelab-maint/container-audit.ignore"

# Lines matching STRONG are reported. WEAK are counted but only surfaced when a
# container has no strong findings, so a chatty app does not drown a real fault.
STRONG = re.compile(
    r"\b(fatal|panic|segfault|oom[- ]?killed|traceback \(most recent call last\)"
    r"|unhandled exception|connection refused|permission denied"
    r"|no such file or directory|cannot open|mount source path does not exist"
    r"|bind source path does not exist)\b",
    re.IGNORECASE,
)
WEAK = re.compile(
    r"\b(error|failed|failure|exception|critical|denied|cannot|refused)\b",
    re.IGNORECASE,
)

# Benign by construction: a log line that merely NAMES an error statistic, or an
# INFO/DEBUG line, is not a fault. Kept deliberately short and literal.
#
# The latter group are host-specific false positives measured on 2026-10-04 and
# confirmed by log inspection to be harmless. Each is a WEAK-grade "real" string
# emitted by something that is working:
#   * postgres "terminating connection due to administrator command" and "the
#     database system is shutting down" -- the normal FATAL a postgres backend
#     logs when its container is stopped. Appears on every restart.
#   * pg_isready probes with no -d/-U: the probe asks for a database or role that
#     does not exist, logs FATAL, and still exits 0 because the server IS up.
#     A sloppy healthcheck (afsaane-test-db, tday_db), not a database fault.
#   * haproxy "Can't open global server state file" -- docker-socket-proxy
#     starting before its state file exists; it proceeds regardless.
#   * fsverity "operation not supported" -- the host filesystem lacks fsverity;
#     buildx reports it and carries on.
BENIGN = re.compile(
    r"(level=(info|debug|trace))"
    r"|(\b(INFO|DEBUG|TRACE)\b)"
    r"|(error[_-]?(count|rate|counter|loop))"
    r"|(\b0 errors?\b)"
    r"|(no errors?\b)"
    r"|(error_?handler\s+(registered|installed|added))"
    r"|(terminating connection due to administrator command)"
    r"|(the database system is shutting down)"
    r"|(received fast shutdown request)"
    r"|(role \".*\" does not exist)"
    r"|(database \".*\" does not exist)"
    r"|(Can't open global server state file)"
    r"|(fsverity)",
    re.IGNORECASE,
)

MAX_LINES_PER_CONTAINER = 4000


def run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, errors="replace"
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return 1, f"<command failed: {exc}>"


def since_timestamp(hours: float) -> str:
    """Window start: previous run (minus a small overlap) or `hours` ago."""
    try:
        with open(LAST_RUN) as fh:
            prev = datetime.fromisoformat(fh.read().strip())
        if prev.tzinfo is None:
            prev = prev.replace(tzinfo=timezone.utc)
        prev -= timedelta(minutes=5)  # overlap: never leave a gap between runs
        return prev.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OSError, ValueError):
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )


def load_ignore() -> set[str]:
    """Container names that are allowed to be stopped (or fully ignored)."""
    try:
        with open(IGNORE_FILE) as fh:
            return {
                line.split("#", 1)[0].strip()
                for line in fh
                if line.split("#", 1)[0].strip()
            }
    except OSError:
        return set()


def write_atomic(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def inspect(name: str) -> dict:
    fmt = (
        "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}"
        "{{else}}-{{end}}|{{.RestartCount}}|{{.State.ExitCode}}|{{.State.StartedAt}}"
    )
    rc, out = run(["docker", "inspect", name, "--format", fmt])
    if rc != 0:
        return {"state": "unknown", "health": "-", "restarts": 0, "exit": -1, "started": ""}
    parts = out.strip().split("|")
    if len(parts) != 5:
        return {"state": "unknown", "health": "-", "restarts": 0, "exit": -1, "started": ""}
    state, health, restarts, exit_code, started = parts
    return {
        "state": state,
        "health": health,
        "restarts": int(restarts) if restarts.isdigit() else 0,
        "exit": int(exit_code) if exit_code.lstrip("-").isdigit() else -1,
        "started": started,
    }


def started_within(started: str, since: str) -> bool:
    """True if the container (re)started inside the audit window.

    RestartCount is cumulative for a container's whole life, so a single restart
    from weeks ago would otherwise alert forever. Only a restart inside the
    window means the container is crashing NOW.
    """
    try:
        s = datetime.fromisoformat(started.replace("Z", "+00:00"))
        w = datetime.fromisoformat(since.replace("Z", "+00:00"))
        return s >= w
    except (ValueError, AttributeError):
        return False


def scan_logs(name: str, since: str) -> tuple[list[str], int]:
    rc, out = run(
        ["docker", "logs", "--since", since, "--tail", str(MAX_LINES_PER_CONTAINER), name],
        timeout=120,
    )
    strong: list[str] = []
    weak = 0
    for line in out.splitlines():
        line = line.strip()
        if not line or BENIGN.search(line):
            continue
        if STRONG.search(line):
            strong.append(line[:400])
        elif WEAK.search(line):
            weak += 1
    return strong, weak


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-hours", type=float, default=6.0)
    ap.add_argument("--json", default=STATUS_JSON)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rc, out = run(["docker", "ps", "-a", "--format", "{{.Names}}"])
    if rc != 0:
        payload = {
            "result": "fail",
            "checked": 0,
            "issues": 0,
            "error": "docker not reachable",
        }
        write_atomic(args.json, json.dumps(payload, indent=2))
        print("container-audit: docker not reachable", file=sys.stderr)
        return 1

    ignore = load_ignore()
    names = [n for n in out.split() if n]
    skipped = sorted(n for n in names if n in ignore)
    names = [n for n in names if n not in ignore]
    since = since_timestamp(args.since_hours)
    findings: list[dict] = []
    report: list[str] = []
    if skipped:
        report.append(f"  skipped (in {IGNORE_FILE}): {', '.join(skipped)}")

    for name in sorted(names):
        info = inspect(name)
        problems: list[str] = []
        if info["state"] != "running":
            problems.append(f"state={info['state']} (exit {info['exit']})")
        if info["health"] == "unhealthy":
            problems.append("health=unhealthy")
        if info["restarts"] > 0 and started_within(info["started"], since):
            # Restarted inside the window: crash-looping or crash-recovered now.
            problems.append(f"restarted in window (total {info['restarts']})")
        elif info["restarts"] > 0:
            report.append(
                f"  note  {name}: {info['restarts']} historical restart(s), stable now"
            )

        strong, weak = scan_logs(name, since)
        if strong:
            problems.append(f"{len(strong)} error line(s)")
        elif weak and info["state"] == "running" and info["health"] != "unhealthy":
            # Weak-only noise on a healthy container: note it, do not fail on it.
            report.append(f"  note  {name}: {weak} weak match(es), none strong")

        if problems:
            findings.append(
                {
                    "container": name,
                    "problems": problems,
                    "lines": strong[:5],
                    "weak": weak,
                }
            )
            report.append(f"  FAIL  {name}: {'; '.join(problems)}")
            for line in strong[:3]:
                report.append(f"          {line}")

    result = "fail" if findings else "ok"
    payload = {
        "result": result,
        "checked": len(names),
        "issues": len(findings),
        "since": since,
        "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "findings": findings,
    }
    write_atomic(args.json, json.dumps(payload, indent=2))
    write_atomic(LAST_RUN, datetime.now(timezone.utc).isoformat(timespec="seconds"))

    if not args.quiet:
        print(f"container-audit: {len(names)} containers, {len(findings)} with issues")
        for line in report:
            print(line)
        if not findings:
            print("  all containers running, no error signatures since " + since)

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
