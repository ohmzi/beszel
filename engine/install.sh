#!/usr/bin/env bash
# install.sh - install or upgrade homelab-maint on this host.
#
#   sudo ./install.sh [--dry-run] [--no-start] [--first-check] [--adopt-rules]
#
#   --dry-run       print what would change; write nothing (does not need root)
#   --no-start      enable the timers and daemons but do not start them now
#   --first-check   also run the check tier once, in the background (the same run its timer does)
#   --adopt-rules   also run `homelab-maint rules sync --adopt` once the registry is installed (see "RULES REGISTRY" below)
#
# The dashboard (the OhmzMaintainer beszel hub + agent, the site that replaced the old maintenance-web container) is NOT deployed
# by this script: it is a separate install (supplemental/systemd/README.md). This script only lays down the engine beside it.
#
# ONE UMBRELLA. Enables and starts: the check/daily/weekly tier timers, the 1-minute metrics sampler timer, the 1-minute
# scheduler tick timer (it drives the routine, the probes, the acknowledge inbox and, after a cutover, the legacy jobs: there
# is no routine timer), the 1-minute pipeline self-health refresher, the www status server and the live monitor. Legacy units
# (backups, docker-prune, ...) are never touched: `homelab-maint migrate` retires them one at a time, and this script never puts
# back what a cutover retired (docker-prune.timer is in no list here and stays exactly as the owner left it).
#
# ACKNOWLEDGE (state/ack): the dashboard is a postbox, not a writer. ack/ is 0750 root:10001 (the same as
# `homelab-maint ack init --group`); ack/inbox is 1730 root:10001 (it may CREATE a request file there, not list or read the
# others); ack/web.key is 0640 root:10001 (the HMAC key the hub and the runner both sign with); ack/bootstrap.secret is 0600 root
# (first-run login; `sudo homelab-maint web bootstrap` shows it once). The key and the bootstrap secret are generated ONLY when
# absent and never overwritten or printed. The group is the number 10001 (no group of that name is needed); HM_WEB_GID overrides it, and
# without it the group web.key already has wins, so a host that deployed another gid keeps it.
# The e-mail "Acknowledge" button stays off until ack/web_ready exists (notify.toml [ack] button = "auto"): a link to a site that is not up is
# worse than none. The dashboard's own deploy creates it once the hub answers; by hand, once the site answers on its public hostname:
#   sudo touch STATE/ack/web_ready
#
# RULES REGISTRY (config/rules.d): once the release ships registry content, rules.d is installed beside the config (00-baseline-
# invariants.toml is release data and always replaced; the other files only when absent). The tick runs `homelab-maint rules sync`;
# an installed hand-maintained config file that differs from what the registry compiles is reported as "blocked" and NEVER replaced.
# This script prints the exact command to adopt it (sudo homelab-maint rules sync --adopt; the originals are kept under
# state/rules/orig) and only runs it itself with --adopt-rules; the notice is printed by --dry-run too. Adopt in the same sitting as the
# install: until then the check tier warns "not adopted yet" (e-mail, then a text) because the owner's older config predates the release floor.
#
# Safe to re-run: every step compares before it writes, and existing files under
# /etc/homelab-maint are NEVER overwritten (a changed shipped default is saved next to
# them as NAME.dist, and the tables your copy lacks are listed; the one exception is
# legacy-retirement.toml, which is release data). Nothing here starts a job that deletes: the tier
# and tick services are only ever started by their timers, and what those runs may do is decided
# by "mode" in /etc/homelab-maint/maint.toml, which ships as "report" for every cleaner (the one
# thing that ships on is the spike ladder's non-destructive reclaim rung).
#
# HM_ROOT=/some/dir stages the whole install under that prefix and skips systemd
# (used by tests/test_packaging.py); HM_PYTHON overrides /usr/bin/python3.
set -euo pipefail
umask 022

usage() { sed -n '2,/^# HM_ROOT/p' "$0" | sed 's/^# \{0,1\}//' | sed '$d'; }

DRY=0
START=1
FIRST_CHECK=0
ADOPT_RULES=0
while (($#)); do
  case $1 in
    --dry-run) DRY=1 ;;
    --no-start) START=0 ;;
    --first-check) FIRST_CHECK=1 ;;
    --adopt-rules) ADOPT_RULES=1 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "install.sh: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
ROOT=${HM_ROOT:-}
ROOT=${ROOT%/}
PY=${HM_PYTHON:-/usr/bin/python3}

LIVE_LIB=/usr/local/lib/homelab-maint      # what compiled tracebacks should name
LIB=$ROOT$LIVE_LIB
ENTRY=$ROOT/usr/local/sbin/homelab-maint
CONF=$ROOT/etc/homelab-maint
STATE=$ROOT/var/lib/homelab-maint
STATE_DIR_LIVE=/var/lib/homelab-maint      # the path to print for the owner (STATE carries the staging prefix)
LOGD=$ROOT/var/log/homelab-maint
UNITS=$ROOT/etc/systemd/system

# Only these are enabled (and started). The tier, metrics and tick SERVICES are started by their timers, never here.
# There is deliberately no routine timer: the routine is a job of the one-minute tick (SPEC4, one umbrella).
TIMERS=(homelab-maint-check.timer homelab-maint-daily.timer homelab-maint-weekly.timer
        homelab-maint-metrics.timer homelab-maint-tick.timer homelab-maint-selfhealth.timer)
WWW=homelab-maint-www.service
LIVE=homelab-maint-live.service
# Unit -> the module that must exist in the source tree, else the unit would only fail every run.
declare -A NEEDS=([$WWW]=server.py [$LIVE]=live.py [homelab-maint-metrics.timer]=metrics_ring.py [homelab-maint-tick.timer]=scheduler.py
                  [homelab-maint-selfhealth.timer]=tasks/self_health.py)
# Config that is release data rather than owner config: replaced on every install (the owner's own items live in legacy-retirement.d/).
ALWAYS=(legacy-retirement.toml)
# The dashboard's gid. Numeric on purpose: no group of that name exists on the host. Not set by the
# owner (HM_WEB_GID): the group an existing web.key already has (what the runner's web_gid() trusts too), else 10001.
WEB_GID=${HM_WEB_GID:-}
if [[ -z $WEB_GID && -f $STATE/ack/web.key && ! -L $STATE/ack/web.key ]]; then
  WEB_GID=$(stat -c %g "$STATE/ack/web.key")
  if [[ $WEB_GID == 0 ]]; then WEB_GID=; fi             # root says nothing about the website
fi
WEB_GID=${WEB_GID:-10001}
BASELINE=00-baseline-invariants.toml     # rules.d: the safety floor's mirror, release data like legacy-retirement.toml

shopt -s nullglob
CHANGES=0
CODE_CHANGED=0
UNITS_CHANGED=0
NEW_DIRS=()          # bind-mounted directories this run had to CREATE (a running container still holds the old inode)

say()  { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
note() { printf '  %-14s %s\n' "$1" "$2"; }
verb() { if ((DRY)); then printf 'would-%s' "$1"; else printf '%s' "$1"; fi; }
changed() { CHANGES=$((CHANGES + 1)); }

# Every mutation of the target system goes through run(), so --dry-run is exact.
run() {
  if ((DRY)); then printf '    + %s\n' "$*"; else "$@"; fi
}

# systemctl is skipped entirely when staging under HM_ROOT.
ctl() {
  if [[ -n $ROOT ]]; then note skipped "systemctl $* (staging root)"; else run systemctl "$@"; fi
}

# put SRC DEST MODE: install SRC at DEST (root:root, MODE) unless it is already identical.
# Sets PUT=same|changed so callers can track what moved.
put() {
  local src=$1 dest=$2 mode=$3
  if [[ -f $dest ]] && cmp -s -- "$src" "$dest" \
    && [[ $(stat -c '%a %U:%G' "$dest") == "${mode#0} root:root" ]]; then
    note same "$dest"
    PUT=same
    return 0
  fi
  run install -m "$mode" -o root -g root -- "$src" "$dest"
  note "$(verb install)" "$dest"
  changed
  PUT=changed
}

# ensure_dir DIR MODE [GID]: create DIR (root, group GID or root, MODE) or correct its mode and group. Never deletes or re-creates.
ensure_dir() {
  local d=$1 mode=$2 gid=${3:-0}
  [[ -d $d || $d != "$STATE/public"* && $d != "$STATE/ack"* ]] || NEW_DIRS+=("$d")      # only what the website container mounts
  if [[ -d $d ]] && [[ $(stat -c '%a %u:%g' "$d") == "${mode#0} 0:$gid" ]]; then
    note same "$d/"
    return 0
  fi
  run install -d -m "$mode" -o root -g "$([[ $gid == 0 ]] && echo root || echo "$gid")" -- "$d"
  note "$(verb mkdir)" "$d/"
  changed
}

# pick_web_gid: sets AGID, the group the dashboard's ack files (web.key, the inbox) use. A process that cannot chgrp to it (a
# user-namespace staging run maps one gid) gets group root instead: closed, never open. On a real host that is a loud warning,
# because the dashboard could then not post its requests.
pick_web_gid() {
  : >"$STAGE/.gidprobe"
  if ((DRY)) || chgrp "$WEB_GID" "$STAGE/.gidprobe" 2>/dev/null; then
    AGID=$WEB_GID
  else
    AGID=0
    if [[ -z $ROOT ]]; then warn "cannot use group $WEB_GID: the acknowledge buttons cannot post until ack/inbox and ack/web.key belong to it"; fi
  fi
}

# new_secret hex|url: a fresh random value on stdout (hex: 64 characters, the HMAC key; url: 43 characters). Used only by ensure_secret.
new_secret() { "$PY" -c 'import secrets, sys; print(secrets.token_hex(32) if sys.argv[1] == "hex" else secrets.token_urlsafe(32))' "$1"; }

# ensure_secret DEST MODE GID KIND: create DEST holding a fresh random value ONLY if it is absent. An existing file is NEVER rewritten or
# printed; only its mode and group are put right. The value passes through the 0700 stage dir (umask 077), never through argv or output.
ensure_secret() {
  local dest=$1 mode=$2 gid=$3 kind=$4
  if [[ -L $dest ]]; then
    warn "$dest is a symlink: left alone"
  elif [[ -e $dest ]]; then
    if [[ $(stat -c '%a %u:%g' "$dest") == "${mode#0} 0:$gid" ]]; then
      note kept "$dest"
    else
      run chown "0:$gid" -- "$dest"
      run chmod "$mode" -- "$dest"
      note "$(verb fix)" "$dest (mode and group put back to $mode root:$gid; content untouched)"
      changed
    fi
  else
    ( umask 077; new_secret "$kind" >"$STAGE/new.value" )
    run install -m "$mode" -o root -g "$([[ $gid == 0 ]] && echo root || echo "$gid")" -- "$STAGE/new.value" "$dest"
    note "$(verb create)" "$dest (random $kind value, $mode)"
    changed
  fi
}

# has_registry DIR: does this rules.d hold registry CONTENT (NN-name.toml other than the baseline and the owner's overrides)?
# A rules.d with only the baseline would compile to a config without any protection and be refused as invalid: it is not installed.
has_registry() {
  local f n
  for f in "$1"/[0-9][0-9]-*.toml; do
    n=${f##*/}
    [[ $n == "$BASELINE" || $n == 99-owner-overrides.toml ]] || return 0
  done
  return 1
}

toml_ok() { "$PY" -c 'import sys, tomllib; tomllib.load(open(sys.argv[1], "rb"))' "$1" 2>/dev/null; }

# unit_exists UNIT: is there a real (non drop-in-only) unit file by that name?
unit_exists() {
  if [[ -n $ROOT ]]; then
    [[ -f $UNITS/$1 || -f $ROOT/usr/lib/systemd/system/$1 || -f $ROOT/lib/systemd/system/$1 ]]
  else
    [[ -n $(systemctl show -p FragmentPath --value "$1" 2>/dev/null || true) ]]
  fi
}

# retired KIND (unit|path): what a `homelab-maint migrate cutover` retired on purpose, one per line. Asked of the INSTALLED runner
# (read-only); empty when it cannot answer (first install, an older runner, staging). HM_RETIRED_UNITS / HM_RETIRED_PATHS replace
# the lookup (tests). This script must never put back a timer or drop-in that a cutover took away.
retired() {
  local var=HM_RETIRED_${1^^}S
  if [[ -n ${!var+x} ]]; then printf '%s\n' "${!var}"; return 0; fi
  if [[ -z $ROOT && -x $ENTRY ]]; then "$ENTRY" migrate retired --kind "$1" 2>/dev/null || true; fi
}
is_retired() { grep -qxF -- "$2" <<<"$1"; }          # is_retired LIST NAME

# missing_tables SHIPPED INSTALLED: the tables / named entries the shipped config has and the installed copy lacks (read-only).
missing_tables() {
  "$PY" - "$1" "$2" 2>/dev/null <<'PYEOF' || true
import sys, tomllib
def keys(path):
    with open(path, "rb") as f:
        d = tomllib.load(f)
    k = {f"[{n}]" for n, v in d.items() if isinstance(v, dict) and n != "tasks"}
    k |= {f"[tasks.{n}]" for n in d.get("tasks", {})}
    for n, v in d.items():
        if isinstance(v, list):
            k |= {f"{n} {e['name']}" for e in v if isinstance(e, dict) and "name" in e}
    return k
new = sorted(keys(sys.argv[1]) - keys(sys.argv[2]))
if new:
    print(f"{len(new)}: " + ", ".join(new[:8]) + (" ..." if len(new) > 8 else ""))
PYEOF
}

# ------------------------------------------------------------------ 0. refuse unless root
if ((!DRY)) && [[ $EUID -ne 0 ]]; then
  die "must run as root (try: sudo $0); use --dry-run to preview without root"
fi
if ((DRY)); then say "DRY RUN: nothing will be written."; fi
if [[ -n $ROOT ]]; then say "Staging under $ROOT (systemd untouched)."; fi
if [[ -z $ROOT ]]; then
  command -v systemctl >/dev/null || die "systemctl not found; this installer targets a systemd host"
fi

# One installer at a time. The lock lives in tmpfs and goes away with the process.
if ((!DRY)); then
  mkdir -p "$ROOT/run/lock"
  exec 9>"$ROOT/run/lock/homelab-maint-install.lock"
  flock -n 9 || die "another install.sh is already running"
fi

# ------------------------------------------------------------------ 1. preflight (read-only)
say "Preflight"
[[ -f $SRC/homelab_maint/cli.py && -f $SRC/homelab_maint/core.py && -f $SRC/homelab-maint ]] \
  || die "incomplete source tree in $SRC (need homelab_maint/{cli,core}.py and the homelab-maint entry script)"
[[ -x $PY ]] || die "$PY not found or not executable"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "$PY is older than 3.11 (tomllib is required)"
for f in "$SRC"/etc/*.toml "$SRC"/etc/rules.d/*.toml "$SRC"/homelab_maint/data/*.toml; do     # data/*.toml: the playbook baseline inside the package
  toml_ok "$f" || die "shipped config does not parse: $f"
done
note ok "python $("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])'), shipped config parses"

STAGE=$(mktemp -d "${TMPDIR:-/tmp}/homelab-maint-install.XXXXXX")
trap 'rm -rf -- "$STAGE"' EXIT

# ------------------------------------------------------------------ 2. python package
say "Package -> $LIB"
mkdir "$STAGE/pkg"
tar -C "$SRC" --exclude=__pycache__ --exclude='*.pyc' -cf - homelab_maint | tar -C "$STAGE/pkg" -xf -
# Compile in the staging copy first: a syntax error aborts here, before the live install is touched.
# -d makes tracebacks name the installed path rather than the temp dir.
"$PY" -m compileall -q -d "$LIVE_LIB/homelab_maint" "$STAGE/pkg/homelab_maint" >/dev/null \
  || die "python source does not compile; nothing was installed"
# compileall only proves the syntax. A module-level NameError/ImportError compiles fine and then breaks every
# tier run (cli.load_tasks imports every task module) and `homelab-maint gate`, whose crash exit code 1 is what
# systemd reads as "condition not met": the Immich recycle would be skipped silently, never failed. So import
# what the runner imports (cli plus every task module) from the staged copy, before the live tree is touched.
# The same goes for the other entry points (live, tick/scheduler, metrics_ring, routine, probes, notify, publish ...): every top-level
# module is imported too, so a broken daemon is caught here and not as a restart loop after the swap. They have no import-time
# side effects (their main()s are guarded). The state/log/run/conf dirs point into the stage so one could not reach the host.
rc=0
env HOMELAB_MAINT_STATE="$STAGE/state" HOMELAB_MAINT_LOG="$STAGE/log" HOMELAB_MAINT_RUN="$STAGE/run" \
  HOMELAB_MAINT_CONF="$STAGE/conf" "$PY" -B - "$STAGE/pkg" >"$STAGE/import.log" 2>&1 <<'PYEOF' || rc=$?
import importlib, pkgutil, sys
sys.path.insert(0, sys.argv[1])    # the staged copy must win over any homelab_maint on PYTHONPATH or in the cwd
cli = importlib.import_module("homelab_maint.cli")
assert cli.__file__.startswith(sys.argv[1]), f"imported {cli.__file__}, not the staged tree"
pkg = importlib.import_module("homelab_maint")
for m in pkgutil.iter_modules(pkg.__path__):
    importlib.import_module(f"homelab_maint.{m.name}")
tasks = importlib.import_module("homelab_maint.tasks")
for m in pkgutil.iter_modules(tasks.__path__):
    importlib.import_module(f"homelab_maint.tasks.{m.name}")
PYEOF
if ((rc)); then
  tail -n 8 "$STAGE/import.log" >&2
  die "python package does not import cleanly (see above); nothing was installed"
fi
if ((!DRY)); then chown -R root:root "$STAGE/pkg"; fi   # the copy keeps this owner
find "$STAGE/pkg" -type d -exec chmod 0755 {} +
find "$STAGE/pkg" -type f -exec chmod 0644 {} +

ensure_dir "$LIB" 0755
if [[ -d $LIB/homelab_maint ]] && diff -rq -x __pycache__ "$STAGE/pkg/homelab_maint" "$LIB/homelab_maint" >/dev/null 2>&1; then
  note same "$LIB/homelab_maint/"
else
  # Build next to the destination and swap with rename(2), so a half-copied tree is never live.
  run rm -rf -- "$LIB/.homelab_maint.new" "$LIB/.homelab_maint.old"
  run cp -a -- "$STAGE/pkg/homelab_maint" "$LIB/.homelab_maint.new"
  if [[ -d $LIB/homelab_maint ]]; then
    run mv -T -- "$LIB/homelab_maint" "$LIB/.homelab_maint.old"
  fi
  run mv -T -- "$LIB/.homelab_maint.new" "$LIB/homelab_maint"
  run rm -rf -- "$LIB/.homelab_maint.old"
  note "$(verb install)" "$LIB/homelab_maint/"
  changed
  CODE_CHANGED=1
fi

# ------------------------------------------------------------------ 3. entry script
say "Entry script"
ensure_dir "$(dirname "$ENTRY")" 0755
put "$SRC/homelab-maint" "$ENTRY" 0755
if [[ $PUT == changed ]]; then CODE_CHANGED=1; fi

# ------------------------------------------------------------------ 4. state and log dirs
say "State and logs"
ensure_dir "$STATE" 0755     # status.json inside is world-readable on purpose (www runs as a dynamic user)
ensure_dir "$LOGD" 0755      # audit.jsonl: every mutation attempt, written by root
# What the dashboard reads, read-only (files inside are 0644, written by publish.py and live.py). Created here, before the dashboard,
# and only ever created or chmod-ed: never delete and re-create it, a bind mount follows the directory's inode and the dashboard would
# see an empty directory until it is restarted.
ensure_dir "$STATE/public" 0755
ensure_dir "$STATE/public/reports" 0755
# SPEC5 acknowledgements: the dashboard posts requests here and reads what the runner publishes for it; it can write nothing else.
# Modes are documented in the header. Only created or chmod-ed, never deleted and re-created (same bind-mount rule as public/).
pick_web_gid
ensure_dir "$STATE/ack" 0750 "$AGID"                                      # group only: root and the website's gid (a group root = closed, never open)
ensure_dir "$STATE/ack/inbox" 1730 "$AGID"
ensure_dir "$STATE/ack/inbox/rejected" 0700
ensure_secret "$STATE/ack/web.key" 0640 "$AGID" hex                       # HMAC key (hex text; the runner and the container both read it)
ensure_secret "$STATE/ack/bootstrap.secret" 0600 0 url                    # first-run login: root only; read it with sudo, once

# ------------------------------------------------------------------ 5. config (only if absent)
say "Config -> $CONF"
ensure_dir "$CONF" 0755
for f in "$SRC"/etc/*; do
  [[ -f $f ]] || continue
  name=${f##*/}
  if [[ $name == kuma.toml ]]; then
    warn "not installing a shipped $name: push tokens are secrets and belong only in $CONF/kuma.toml (0600)"
    continue
  fi
  dest=$CONF/$name
  if [[ " ${ALWAYS[*]} " == *" $name "* ]]; then       # release data (not owner config): always the shipped copy, root:root 0644
    put "$f" "$dest" 0644
  elif [[ -e $dest || -L $dest ]]; then
    note kept "$dest (existing config left untouched)"
    if ! cmp -s -- "$f" "$dest"; then
      put "$f" "$dest.dist" 0644
      if [[ $PUT == changed ]]; then
        say "                 shipped defaults changed or you edited it; compare: diff $dest $dest.dist"
      fi
      if [[ $name == *.toml ]] && lack=$(missing_tables "$f" "$dest") && [[ -n $lack ]]; then
        say "                 shipped tables your copy lacks ($lack); merge them from $name.dist"
      fi
    fi
  else
    put "$f" "$dest" 0644
  fi
done
# A broken file is not fatal here, but say which: the tick ignores a bad jobs.toml (schedules nothing), a bad routine.toml holds every
# disruptive step to report-only, a bad maint.toml fails the runner.
for c in "$CONF"/*.toml; do
  if ! toml_ok "$c"; then
    warn "$c does not parse as TOML; the runner or the part that reads ${c##*/} will fail until it is fixed"
  fi
done
if [[ -e $CONF/PAUSE ]]; then warn "kill switch $CONF/PAUSE is present: cleaners will only report"; fi
# Where the owner's own extensions go (all root-owned and not writable by anyone else, or the loaders refuse them): plugins.d/*.py
# (tasks), probes.d/*.toml (probes), legacy-retirement.d/*.toml (legacy items). Empty is the normal state.
for d in plugins.d probes.d legacy-retirement.d; do ensure_dir "$CONF/$d" 0755; done

# The rules registry (SPEC6): rules.d is the one place that defines what the script does; the config files above become generated
# from it. Installed only when the release ships registry content (see has_registry); root-owned 0755 with 0644 files, or the
# registry refuses it. The baseline is release data (it mirrors the floor pinned in the code: a stale copy would block every sync),
# the rest is the owner's after the first install and is never overwritten.
RULES_SRC=$SRC/etc/rules.d
if has_registry "$RULES_SRC"; then
  ensure_dir "$CONF/rules.d" 0755
  for f in "$RULES_SRC"/*.toml; do
    name=${f##*/}
    dest=$CONF/rules.d/$name
    if [[ $name == "$BASELINE" ]]; then
      put "$f" "$dest" 0644
    elif [[ -e $dest || -L $dest ]]; then
      note kept "$dest (existing rule file left untouched; compare with $f)"
    else
      put "$f" "$dest" 0644
    fi
  done
elif [[ -d $RULES_SRC ]]; then
  note skipped "rules.d (this release ships the safety baseline only: the config files stay hand-maintained until it ships rules)"
fi

# What a `homelab-maint migrate cutover` retired on purpose (empty on a first install): this script must not put it back.
RETIRED_UNITS=$(retired unit)
RETIRED_PATHS=$(retired path)

# ------------------------------------------------------------------ 6. systemd units
say "systemd units -> $UNITS"
ensure_dir "$UNITS" 0755
# Every unit file is installed (that is only a file); which ones are ENABLED is decided in step 7. There is deliberately no
# homelab-maint-routine.timer: the routine is a job of the one-minute tick.
for u in "$SRC"/systemd/*.service "$SRC"/systemd/*.timer; do
  put "$u" "$UNITS/${u##*/}" 0644
  if [[ $PUT == changed ]]; then UNITS_CHANGED=1; fi
done

# Drop-ins live at systemd/dropins/<unit>.d/*.conf and are installed beside the matching unit.
# The original unit is never edited, and a drop-in is skipped when its unit does not exist or a cutover retired it.
for d in "$SRC"/systemd/dropins/*.d; do
  dir=${d##*/}
  unit=${dir%.d}
  if ! unit_exists "$unit"; then
    note skipped "$dir (no $unit on this host)"
    continue
  fi
  for c in "$d"/*.conf; do
    if is_retired "$RETIRED_PATHS" "${UNITS#"$ROOT"}/$dir/${c##*/}"; then
      note retired "$dir/${c##*/} (a cutover retired it; not put back)"
      continue
    fi
    ensure_dir "$UNITS/$dir" 0755
    put "$c" "$UNITS/$dir/${c##*/}" 0644
    if [[ $PUT == changed ]]; then UNITS_CHANGED=1; fi
  done
done

# ------------------------------------------------------------------ 7. enable (never start a tier service)
say "Enable"
if [[ -n $ROOT ]]; then
  note skipped "systemd (staging root)"
else
  # Reload when we changed a unit, or when an earlier interrupted run left one changed on disk.
  reload=$UNITS_CHANGED
  for u in "${TIMERS[@]}" "$WWW" "$LIVE" immich-server-recycle.service; do
    if [[ $(systemctl show -p NeedDaemonReload --value "$u" 2>/dev/null || true) == yes ]]; then reload=1; fi
  done
  if ((reload)); then
    ctl daemon-reload
    note "$(verb reloaded)" "systemd manager configuration"
  else
    note same "systemd units unchanged; daemon-reload not needed"
  fi

  # A unit is enabled only if the module it runs is in the source tree (else it would just fail every run), and never if a
  # cutover retired it: re-running this script must not undo `homelab-maint migrate cutover`.
  eligible() {          # eligible UNIT: may it be enabled? says why not
    local mod=${NEEDS[$1]:-}
    if [[ -n $mod && ! -f $SRC/homelab_maint/$mod ]]; then
      warn "homelab_maint/$mod is missing from the source tree: $1 not enabled (re-run install.sh once it exists)"
    elif is_retired "$RETIRED_UNITS" "$1"; then
      note retired "$1 (a cutover retired it; left as it is)"
    else
      return 0
    fi
    return 1
  }
  to_enable=()
  for u in "${TIMERS[@]}"; do eligible "$u" && to_enable+=("$u"); done
  eligible "$WWW" && to_enable+=("$WWW")
  eligible "$LIVE" && to_enable+=("$LIVE")

  for u in "${to_enable[@]}"; do
    if [[ $(systemctl is-enabled "$u" 2>/dev/null || true) == enabled ]]; then
      note same "$u enabled"
    else
      ctl enable "$u"
      note "$(verb enable)" "$u"
      changed
    fi
    ((START)) || continue
    if systemctl is-active --quiet "$u" 2>/dev/null; then
      if [[ $u == "$WWW" || $u == "$LIVE" ]] && ((CODE_CHANGED || UNITS_CHANGED)); then
        ctl restart "$u"            # a daemon: pick up new code or a new sandbox profile (the live monitor saves its history first)
        note "$(verb restart)" "$u (code or unit changed)"
        changed
      else
        note same "$u active"
      fi
    else
      ctl start "$u"                # timers and the two read-only daemons only; the services they trigger start themselves
      note "$(verb start)" "$u"
      changed
    fi
  done

  if ((FIRST_CHECK)); then
    ctl start --no-block homelab-maint-check.service   # the same run the timer does (read-only checks; only the reclaim rung may act)
    note "$(verb start)" "homelab-maint-check.service (first check, background)"
  fi
fi

# ------------------------------------------------------------------ 8. report
say "Cleaners that can mutate when their timer fires (mode = \"apply\"):"
cfg=$CONF/maint.toml
if [[ ! -f $cfg ]]; then cfg=$SRC/etc/maint.toml; fi   # a dry run on a fresh host reads the shipped one
if [[ -f $cfg ]]; then
  "$PY" - "$cfg" <<'PYEOF' || true
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    tasks = tomllib.load(f).get("tasks", {})
# pressure_response (the spike ladder) is a master switch with per-rung modes; with no table at all it defaults to apply for the
# non-destructive reclaim rung only, so an older maint.toml that lacks the table still has that one rung on.
tasks.setdefault("pressure_response", {})
on = []
for n, t in sorted(tasks.items()):
    if not isinstance(t, dict) or t.get("mode", "apply" if n == "pressure_response" else "report") != "apply":
        continue
    if n == "pressure_response":
        rungs = {"reclaim": "apply", "throttle": "report", "restart": "report", "emergency": "report", **{k: t[k] for k in t if k != "mode"}}
        n += " (rungs: " + (", ".join(k for k, v in rungs.items() if v == "apply") or "none") + ")"
    on.append(n)
print("  " + (", ".join(on) if on else "none (all report-only)"))
PYEOF
else
  note none "(no maint.toml yet)"
fi

if ((!DRY)) && [[ -z $ROOT ]]; then
  say "Self-check (homelab-maint doctor)"
  # A fresh install shows FAIL for the sampler, the live monitor and the tick until their first run (under a minute): re-run it then.
  "$ENTRY" doctor || warn "doctor reported problems (see above); the install itself is complete. The metrics sampler, live monitor and tick need about a minute for their first run: re-run 'homelab-maint doctor'"
  # The two files whose mistakes are silent: a bad jobs.toml makes the tick schedule nothing, a bad routine.toml (or a tick that is
  # not enabled) leaves deferred steps and the monthly window unserved.
  "$ENTRY" job validate || warn "jobs.toml has problems (see above): the tick schedules nothing until they are fixed"
  "$ENTRY" routine check || warn "routine check reported problems (see above)"
fi

# ------------------------------------------------------------------ 8b. rules registry: first adoption (never silent)
# The tick runs `rules sync`. A registry's first sync REPLACES the generated config files only where their data equals what the registry
# compiles; a hand-maintained file that differs is reported as "blocked" and left alone. Adopting it is the owner's decision: the exact
# command is printed (the originals are kept under state/rules/orig), and run here only on --adopt-rules.
# A dry run has not written rules.d yet, so it decides from the shipped copy: the notice must not depend on --dry-run.
ADOPT_PENDING=0
if { { [[ -d $CONF/rules.d ]] && has_registry "$CONF/rules.d"; } || { ((DRY)) && has_registry "$RULES_SRC"; }; } && [[ ! -e $STATE/rules/current.json ]]; then
  ADOPT_PENDING=1
  tense=is; ((DRY)) && tense="would be"
  say "Rules registry: rules.d $tense installed; the config files in $CONF are not generated from it yet."
  if ((ADOPT_RULES)); then
    if [[ -n $ROOT ]]; then
      note skipped "rules sync --adopt (staging root)"
    else
      run "$ENTRY" rules sync --adopt || warn "rules sync --adopt failed (see above); the config files are unchanged. Retry: sudo homelab-maint rules sync --adopt"
    fi
  else
    say "  The tick reports 'blocked' (and replaces nothing) while an installed file differs from the registry. Review, then adopt:"
    say "      homelab-maint rules diff"
    say "      sudo homelab-maint rules sync --adopt        (originals are kept under $STATE_DIR_LIVE/rules/orig; or re-run install.sh --adopt-rules)"
    say "  Do it in this sitting, before the first check run: until then the check tier warns 'not adopted yet' on every run (e-mail, then a text)."
  fi
elif ((ADOPT_RULES)); then
  note skipped "--adopt-rules: nothing to adopt (rules.d is not installed or already adopted)"
fi

# Heads-up (read-only, also in --dry-run): the one legacy job that DELETES what the natives keep. docker-prune.timer is never touched here, and
# its script removes every container CREATED over a week ago, stopped on purpose or not, then the image only that container used. The first
# check run pages about it (docker_prune_exposure, with its playbook); this says so while the owner is at the terminal.
if [[ -z $ROOT ]] && systemctl is-enabled --quiet docker-prune.timer 2>/dev/null; then
  stopped=$(timeout 10 docker ps -a --filter status=exited --format '{{.Names}}' 2>/dev/null | head -n 6 | paste -sd' ' - || true)
  if [[ -n $stopped ]]; then
    warn "docker-prune.timer is enabled and these containers are stopped: $stopped. Any of them created more than 7 days ago is deleted at its next run, with the image only it uses (next: $(systemctl list-timers docker-prune.timer --no-legend 2>/dev/null | awk '{print $1, $2, $3}' | head -n 1 || true))."
    warn "Before then: docker start NAME (a running container is never pruned), or systemctl stop docker-prune.timer. homelab-maint changes neither; its alert docker_prune_exposure explains it."
  fi
fi

say
if ((DRY)); then
  say "Dry run: $CHANGES change(s) would be made; nothing was written."
elif ((CHANGES == 0)); then
  say "Nothing to do: already up to date."
else
  say "Done: $CHANGES change(s) made."
fi
if [[ -z $ROOT ]]; then
  if ((ADOPT_PENDING && !ADOPT_RULES)); then
    say "Next:  adopt the rules registry first (see above):  homelab-maint rules diff   then   sudo homelab-maint rules sync --adopt"
    say "       homelab-maint run --tier check && homelab-maint status"
  else
    say "Next:  homelab-maint run --tier check && homelab-maint status"
  fi
  say "       systemctl list-timers 'homelab-maint-*'      homelab-maint schedule   (everything that runs, one list)"
  say "       enable a cleaner: set mode = \"apply\" under [tasks.NAME] in $CONF/maint.toml"
  say "       kill switch:      homelab-maint pause   (resume with: homelab-maint resume)"
  say "       legacy timers:    untouched; see docs/MIGRATION.md and 'homelab-maint migrate status'"
  say "       sensor ring:      after a minute, GPU columns must be filled (a hardened unit can hide the GPU):"
  say "                         homelab-maint metrics-export | python3 -c \"import json,sys; print(json.load(sys.stdin)['current'])\""
  say "       dashboard:        the OhmzMaintainer hub + agent are a separate deploy: supplemental/systemd/README.md"
  say "       acknowledge:      the e-mail button stays off until ack/web_ready exists; the dashboard's deploy creates it,"
  say "                         or by hand once the site answers on its hostname:   sudo touch $STATE_DIR_LIVE/ack/web_ready"
  say "                         first-run web login secret (shown once, on this terminal):   sudo homelab-maint web bootstrap"
fi
