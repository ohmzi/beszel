#!/usr/bin/env bash
# uninstall.sh - remove homelab-maint from this host.
#
#   sudo ./uninstall.sh [--dry-run] [--force] [--purge --yes]
#
#   --dry-run    print what would be removed; write nothing (does not need root)
#   --force      go on past the refusals described below (a tier run in progress is stopped; scheduler jobs are not)
#   --purge      ALSO delete config (/etc/homelab-maint), state (/var/lib/homelab-maint)
#                and the audit log (/var/log/homelab-maint); needs --yes
#   --yes        confirm --purge
#
# By default config, state and logs are kept so a later install.sh picks up where this left
# off. That includes the acknowledgements (state/acks.json, state/ack/ with its HMAC key and bootstrap secret) and the rules
# registry (config/rules.d, state/rules/ with its history and the originals of every adopted config file): none of it is
# removed without --purge, and nothing here puts a generated config file back to its pre-registry original (the copies are in
# state/rules/orig). The immich-server-recycle drop-in is removed with the rest, which means the Immich
# recycle timer goes back to restarting immich_server without asking the gate first. The dashboard (the OhmzMaintainer hub +
# agent) is a separate install and is never touched here.
#
# Refuses (unless --force) while a tier run is in progress, while a job the scheduler tick started is still alive (a backup runs
# detached from every unit), and while a `homelab-maint migrate cutover` has retired a legacy timer, drop-in or script: removing
# the umbrella would leave that job running nowhere. Roll those back first (`homelab-maint migrate rollback ITEM --apply`).
#
# HM_ROOT=/some/dir works on a staged tree and skips systemd (used by the tests).
set -euo pipefail
umask 022

usage() { sed -n '2,/^# HM_ROOT/p' "$0" | sed 's/^# \{0,1\}//' | sed '$d'; }

DRY=0
FORCE=0
PURGE=0
YES=0
while (($#)); do
  case $1 in
    --dry-run) DRY=1 ;;
    --force) FORCE=1 ;;
    --purge) PURGE=1 ;;
    --yes) YES=1 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "uninstall.sh: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

ROOT=${HM_ROOT:-}
ROOT=${ROOT%/}
LIB=$ROOT/usr/local/lib/homelab-maint
ENTRY=$ROOT/usr/local/sbin/homelab-maint
CONF=$ROOT/etc/homelab-maint
STATE=$ROOT/var/lib/homelab-maint
LOGD=$ROOT/var/log/homelab-maint
RUNDIR=$ROOT/run/homelab-maint
UNITS=$ROOT/etc/systemd/system
PY=${HM_PYTHON:-/usr/bin/python3}

TIERS=(homelab-maint-check homelab-maint-daily homelab-maint-weekly)
# Timers come first so nothing new is triggered while the services below them are stopped; then the two daemons.
UNIT_NAMES=(
  homelab-maint-check.timer homelab-maint-daily.timer homelab-maint-weekly.timer
  homelab-maint-metrics.timer homelab-maint-tick.timer homelab-maint-selfhealth.timer
  homelab-maint-check.service homelab-maint-daily.service homelab-maint-weekly.service
  homelab-maint-metrics.service homelab-maint-tick.service homelab-maint-selfhealth.service
  homelab-maint-live.service homelab-maint-www.service
)
# Drop-ins this project installed: <unit dir>/<file>. Only these exact files are removed.
DROPINS=(immich-server-recycle.service.d/10-homelab-gate.conf)

CHANGES=0
say()  { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'uninstall.sh: %s\n' "$*" >&2; exit 1; }
note() { printf '  %-14s %s\n' "$1" "$2"; }
verb() { if ((DRY)); then printf 'would-%s' "$1"; else printf '%s' "$1"; fi; }

# Every mutation goes through run(), so --dry-run is exact.
run() {
  if ((DRY)); then printf '    + %s\n' "$*"; else "$@"; fi
}
ctl() {
  if [[ -n $ROOT ]]; then note skipped "systemctl $* (staging root)"; else run systemctl "$@"; fi
}

# rm_path PATH [quiet]: remove a file/dir tree if present (quiet: say nothing when it is absent).
rm_path() {
  if [[ -e $1 || -L $1 ]]; then
    run rm -rf -- "$1"
    note "$(verb remove)" "$1"
    CHANGES=$((CHANGES + 1))
  elif [[ ${2:-} != quiet ]]; then
    note absent "$1"
  fi
}

# ------------------------------------------------------------------ guards
if ((PURGE)) && ((!YES)); then
  die "--purge deletes config, state and the audit log; add --yes to confirm"
fi
if ((!DRY)) && [[ $EUID -ne 0 ]]; then
  die "must run as root (try: sudo $0); use --dry-run to preview without root"
fi
if ((DRY)); then say "DRY RUN: nothing will be removed."; fi
if [[ -n $ROOT ]]; then say "Staging under $ROOT (systemd untouched)."; fi
if [[ -z $ROOT ]]; then command -v systemctl >/dev/null || die "systemctl not found"; fi

# unit_state UNIT: its ActiveState, empty if systemd gives no answer. This is NOT `systemctl is-active
# --quiet`: the tier services are Type=oneshot without RemainAfterExit, so for the whole run they are
# "activating" and never "active", and is-active exits 3 for them while they are mid-prune.
unit_state() { systemctl show -p ActiveState --value "$1" 2>/dev/null || true; }

# running_jobs: "job:pid" for every job the scheduler tick started that is still alive (state: sched.json, written by the tick). The tick
# launches jobs (backups!) detached, each in its own session, so no unit reflects them; removing the program files under one would
# break the supervisor that records its result. HM_RUNNING_JOBS replaces the lookup (tests).
running_jobs() {
  if [[ -n ${HM_RUNNING_JOBS+x} ]]; then printf '%s\n' "$HM_RUNNING_JOBS"; return 0; fi
  [[ -r $STATE/sched.json ]] || return 0
  "$PY" - "$STATE/sched.json" 2>/dev/null <<'PYEOF' || true
import json, os, sys
try:
    jobs = json.load(open(sys.argv[1])).get("jobs", {})
except Exception:
    sys.exit(0)
for name, j in sorted(jobs.items()):
    r = j.get("running") if isinstance(j, dict) else None
    pid = r.get("pid") if isinstance(r, dict) else None
    if isinstance(pid, int) and pid > 1 and os.path.exists(f"/proc/{pid}"):
        print(f"{name}:{pid}")
PYEOF
}

# retired KIND (unit|path): what a `migrate cutover` retired, asked of the INSTALLED runner (read-only; empty when it cannot answer).
# HM_RETIRED_UNITS / HM_RETIRED_PATHS replace the lookup (tests).
retired() {
  local var=HM_RETIRED_${1^^}S
  if [[ -n ${!var+x} ]]; then printf '%s\n' "${!var}"; return 0; fi
  if [[ -z $ROOT && -x $ENTRY ]]; then "$ENTRY" migrate retired --kind "$1" 2>/dev/null || true; fi
}

# Do not kill a cleaner halfway through a prune unless asked to. SIGTERM skips the runner's `finally`, so a
# killed run loses its status, ledger and alert state and leaves its mutations half done. Only "inactive"
# and "failed" mean not running; anything else (activating, active, deactivating, an unknown or empty
# answer) counts as running.
if [[ -z $ROOT ]]; then
  for t in "${TIERS[@]}"; do
    state=$(unit_state "$t.service")
    case $state in
      inactive | failed) ;;
      *)
        if ((FORCE)); then
          warn "$t.service is running and will be stopped (--force; state: ${state:-unknown})"
        else
          die "$t.service is running (state: ${state:-unknown}); wait for it to finish or re-run with --force"
        fi
        ;;
    esac
  done
fi

# A job the tick started (a backup, say) outlives every unit: it is not stopped here, and its supervisor needs the program files.
jobs_running=$(running_jobs | grep -v '^$' || true)
if [[ -n $jobs_running ]]; then
  if ((FORCE)); then
    warn "scheduler jobs are running (${jobs_running//$'\n'/ }); --force: they are not stopped, their results may not be recorded"
  else
    die "scheduler jobs are running (${jobs_running//$'\n'/ }); wait for them to finish or re-run with --force"
  fi
fi

# Never leave a legacy job running nowhere: if a cutover retired its timer, drop-in or script, the umbrella is what runs it now.
left=$( { retired unit; retired path; } | grep -v '^$' || true)
if [[ -n $left ]]; then
  if ((FORCE)); then
    warn "a cutover retired these legacy items and nothing will run them after this: ${left//$'\n'/ }"
  else
    die "a cutover retired legacy items that only homelab-maint runs now (${left//$'\n'/ }); roll them back first ('homelab-maint migrate rollback ITEM --apply') or re-run with --force"
  fi
fi

# ------------------------------------------------------------------ stop and disable
say "Stop and disable"
for u in "${UNIT_NAMES[@]}"; do
  if [[ -n $ROOT ]]; then
    note skipped "$u (staging root)"
  elif [[ -f $UNITS/$u ]] || systemctl is-enabled "$u" >/dev/null 2>&1; then
    ctl disable --now "$u"
    note "$(verb disable)" "$u"
  else
    note absent "$u"
  fi
done

# ------------------------------------------------------------------ unit files and drop-ins
say "Remove units -> $UNITS"
for u in "${UNIT_NAMES[@]}"; do rm_path "$UNITS/$u"; done
for d in "${DROPINS[@]}"; do
  rm_path "$UNITS/$d"
  dir=$UNITS/${d%/*}
  # Remove the .d directory only if we left it empty; it may hold someone else's drop-ins.
  if [[ -d $dir ]] && [[ -z $(ls -A -- "$dir") ]]; then
    run rmdir -- "$dir"
    note "$(verb rmdir)" "$dir/"
  fi
done
if [[ -z $ROOT ]]; then
  ctl daemon-reload
  ctl reset-failed "${UNIT_NAMES[@]}" 2>/dev/null || true
fi

# ------------------------------------------------------------------ program files
say "Remove program files"
rm_path "$ENTRY"
rm_path "$LIB/homelab_maint"
rm_path "$LIB/.homelab_maint.new" quiet     # leftovers of an interrupted upgrade
rm_path "$LIB/.homelab_maint.old" quiet
if [[ -d $LIB ]] && [[ -z $(ls -A -- "$LIB") ]]; then
  run rmdir -- "$LIB"
  note "$(verb rmdir)" "$LIB/"
fi
rm_path "$RUNDIR"

# ------------------------------------------------------------------ purge (explicit only)
if ((PURGE)); then
  say "Purge"
  rm_path "$CONF"
  rm_path "$STATE"
  rm_path "$LOGD"
else
  say "Kept (use --purge --yes to delete): $CONF  $STATE  $LOGD   (the registry, the acknowledgements and their keys are inside)"
fi

say
if ((DRY)); then
  say "Dry run: $((CHANGES)) path(s) would be removed; nothing was removed."
else
  say "Done: $CHANGES path(s) removed."
fi
