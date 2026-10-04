"""Tests for legacy.py: the retirement inventory, the cutover/rollback executor, parity checks and the runbook.

Nothing here touches the host: systemctl and crontab are a FakeSystem (a dict of unit states and crontab texts that records every
mutating call), the filesystem is a tmp dir used as Host(root=...), audit/notify are recorders, and time is a variable.
"""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import contextlib
import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from homelab_maint import core, legacy
from homelab_maint.legacy import (Check, Host, InventoryError, Migrator, Parity, Sources, cron_comment, cron_find, cron_only_changed,
                                  cron_uncomment, order_items, parse_inventory, run_check)

ROOT = Path(__file__).resolve().parent.parent
REAL_TOML = ROOT / "etc" / "legacy-retirement.toml"
NOW = 1_800_000_000.0
LEG = "/usr/local/lib/homelab-maint/legacy"


def cp(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


# --------------------------------------------------------------------------- the fake host
class FakeSystem:
    """systemctl (system and `runuser -u U -- env X=Y systemctl --user`) plus crontab, as dicts. `calls` = mutating argv only."""

    def __init__(self):
        self.units: dict[tuple[str, str], dict] = {}
        self.cron: dict[str, str | None] = {}
        self.calls: list[list[str]] = []
        self.log: list[list[str]] = []
        self.hook = None                       # callable(argv) -> CompletedProcess | None, to inject failures
        self.persistent: set[tuple[str, str]] = set()      # (scope, unit) timers that report Persistent=yes

    def add_unit(self, unit, scope="system", enabled="enabled", active="active", type_=""):
        self.units[(scope, unit)] = {"LoadState": "loaded", "UnitFileState": enabled, "ActiveState": active, "SubState": "x", "Type": type_}

    def state(self, unit, scope="system"):
        return dict(self.units[(scope, unit)])

    def __call__(self, argv, input_=None, timeout=60):
        self.log.append(list(argv))
        if self.hook and (r := self.hook(argv)) is not None:
            return r
        if argv[0] == "crontab":
            user = argv[argv.index("-u") + 1]
            if argv[1] == "-l":
                t = self.cron.get(user)
                return cp(argv, 0, t) if t is not None else cp(argv, 1, "", f"no crontab for {user}")
            self.calls.append(list(argv))
            self.cron[user] = input_
            return cp(argv)
        if argv[0] == "runuser":
            scope = "user:" + argv[2]
            assert "XDG_RUNTIME_DIR=/run/user/1000" in argv and "--user" in argv, argv
            args = argv[argv.index("--user") + 1:]
        else:
            assert argv[0] == "systemctl", argv
            scope, args = "system", argv[1:]
        verb, unit = args[0], args[-1]
        if verb == "show":
            u = self.units.get((scope, unit))
            if u is None:
                return cp(argv, 0, "LoadState=not-found\nUnitFileState=\nActiveState=inactive\nSubState=dead\nType=\n")
            return cp(argv, 0, "".join(f"{k}={v}\n" for k, v in u.items()) + (f"Persistent={'yes' if (scope, unit) in self.persistent else 'no'}\n"))
        self.calls.append(list(argv))
        u = self.units.get((scope, unit))
        if verb == "daemon-reload":
            return cp(argv)
        if u is None:
            return cp(argv, 5, "", f"Unit {unit} not found")
        if verb == "disable":
            u["UnitFileState"] = "disabled"
            if "--now" in args:
                u["ActiveState"] = "inactive"
        elif verb == "enable":
            u["UnitFileState"] = "enabled-runtime" if "--runtime" in args else "enabled"
        elif verb == "start":
            u["ActiveState"] = "active"
        elif verb == "stop":
            u["ActiveState"] = "inactive"
        return cp(argv)


class FakeJobs:
    """The scheduler's job table + job-modes.json override file (legacy.JobsApi without importing jobs.py)."""

    def __init__(self, known=None, attrs=None):
        self.known_modes = dict(known or {})
        self.overrides: dict[str, str] = {}
        self.attrs = dict(attrs or {})
        self.calls: list[tuple] = []

    def known(self):
        return dict(self.known_modes)

    def override(self, name):
        return self.overrides.get(name)

    def set(self, name, mode):
        self.calls.append((name, mode))
        if mode is None:
            self.overrides.pop(name, None)
        else:
            self.overrides[name] = mode

    def attr(self, name, dotted):
        return self.attrs[(name, dotted)]


class Rig:
    """A Migrator wired to FakeSystem + FakeJobs + tmp dirs, with recorders for audit and notifications."""

    def __init__(self, tmp_path, items=None, inv=None, system=None, jobs=None, **src):
        self.tmp = tmp_path
        self.sys = system or FakeSystem()
        self.root = tmp_path / "root"
        self.root.mkdir(exist_ok=True)
        self.inv0 = inv or parse_inventory({"meta": {"legacy_root": LEG}, "item": items})
        self.jobs = jobs or FakeJobs({i.job: "observe" for i in self.inv0.items if i.job})
        self.host = Host(run=self.sys, root=self.root, uids={"ohmz": 1000}, jobs=self.jobs)
        self.audits: list[tuple] = []
        self.notes: list[dict] = []
        self.now = NOW
        self.conf = tmp_path / "conf"
        self.conf.mkdir(exist_ok=True)
        self.inv = self.inv0
        self.running_jobs: dict[str, str] = {}                  # job -> why: what `job_running` reports as running under the tick
        self.locks: list[str] = []                              # held /run/lock/backup-*.lock files
        self.tick_events: list[str] = []
        src.setdefault("job_running", lambda name: (name in self.running_jobs, self.running_jobs.get(name, "")))
        src.setdefault("locks", lambda: (list(self.locks), ""))
        src.setdefault("job_history", lambda name, since: [])
        src.setdefault("job_last", lambda name: None)
        self.m = Migrator(self.inv, self.host, Sources(now=lambda: self.now, **src), audit=lambda *a: self.audits.append(a),
                          notifier=self.notes.append, paused=self.paused, now=lambda: self.now, state_dir=tmp_path / "state",
                          run_dir=tmp_path / "run", conf_dir=self.conf, user="tester", tick_lock=self.tick_lock)

    @contextlib.contextmanager
    def tick_lock(self):
        self.tick_events.append("hold")
        try:
            yield
        finally:
            self.tick_events.append("release")

    def paused(self, name=None):
        return (self.conf / "PAUSE").exists() or bool(name and (self.conf / f"PAUSE.{name}").exists())

    def put(self, path, text="#!/bin/sh\necho hi\n", mode=0o750):
        p = self.host.p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        os.chmod(p, mode)
        return p

    def exists(self, path):
        return os.path.lexists(self.host.p(path))

    def outcomes(self):
        return [a[4] for a in self.audits]

    def state(self):
        return core.read_json(self.m.state_path, {})


def item(name, mode="port", wave=3, actions=None, **kw):
    d = {"name": name, "kind": "timer", "location": f"{name}.timer", "mode": mode, "wave": wave,
         "parity_check": [{"kind": "none", "note": "test"}],
         "retire_actions": [{"do": "disable", "unit": f"{name}.timer"}] if actions is None else actions}
    d.update(kw)
    return d


@pytest.fixture
def rig(tmp_path):
    def make(items, **kw):
        return Rig(tmp_path, items, **kw)
    return make


# =========================================================================== inventory (the real file)
def real():
    return parse_inventory(tomllib.load(open(REAL_TOML, "rb")))


def test_real_inventory_parses_and_is_consistent():
    inv = real()
    names = [i.name for i in inv.items]
    assert len(names) == len(set(names)) and len(names) >= 50
    assert {i.mode for i in inv.items} == set(legacy.MODES)
    by = {i.name: i for i in inv.items}
    for it in inv.items:
        assert all(by[d].wave <= it.wave for d in it.depends_on), it.name
    assert len(order_items(inv.items)) == len(inv.items)
    assert inv.meta["parity_defaults"] == {"green": 8, "min_hours": 48, "fresh_h": 2}


def test_real_inventory_covers_everything_found_on_the_host():
    names = {i.name for i in real().items}
    must = {"backup-system", "backup-immich", "docker-prune", "prune-openwebui-media", "comfyui-idle-vram", "notebook-db-alert",
            "immich-server-recycle", "immich-recycle-gate-dropin", "mem-guard", "smartd-alert-hook", "stack-backup", "stack-watchdog",
            "search-canary", "cron-bazarr-subtitle-rules", "cron-tunarr-weekly-sync", "backup-failure-hook", "backup-notify-wrapper",
            "stack-alert-hook", "user-launchpadlib-cache-clean", "os-cron-shims", "os-cron-d", "boot-nvidia-cdi-refresh",
            "boot-nvidia-tdp", "boot-tunarr-autostart", "lmstudio", "dashboards-and-sensors", "kuma-monitors", "notify-route",
            "diun-mail", "purge-public-guests", "hermes-stack", "app-schedulers", "platform-daemons", "cloudflared-update",
            "cron-root-journal-vacuum", "cron-root-find-tmp", "cron-root-logrotate", "cron-root-old-disabled",
            "os-apt-daily", "os-apt-daily-upgrade", "os-unattended-upgrades", "os-logrotate", "os-tmpfiles-clean", "os-fstrim",
            "os-e2scrub", "os-fwupd-refresh", "os-man-db", "os-sysstat-collect", "os-sysstat-summary", "os-dpkg-db-backup",
            "os-snapd-refresh", "os-certbot", "os-ubuntu-misc-timers", "user-firmware-notifier"}
    assert must <= names, must - names


def test_real_inventory_backups_come_last_and_depend_on_the_rest():
    by = {i.name: i for i in real().items}
    assert by["backup-system"].wave == 5 and by["backup-immich"].wave == 5
    assert by["backup-notify-wrapper"].wave == 6 and by["backup-failure-hook"].wave == 6
    assert {"backup-system", "backup-immich"} <= set(by["backup-notify-wrapper"].depends_on)
    assert by["backup-system"].soak_days >= 14                       # two weekly cycles before anything depends on it
    order = [i.name for i in order_items(list(by.values()))]
    assert order.index("backup-notify-wrapper") > order.index("backup-immich") > order.index("backup-system") > order.index("stack-backup")
    assert all(by[n].mode in ("observe", "keep") for n in order if by[n].wave <= 1)


def test_real_inventory_never_touches_the_umbrella_transport_or_platform():
    inv = real()
    moved = {a["src"] for i in inv.items for a in i.actions if a["do"] == "move"}
    disabled = {a["unit"] for i in inv.items for a in i.actions if a["do"] == "disable"}
    # the umbrella's own transport and palette (notify.py / core.Notifier import these) and the backup scripts adapters run
    for keep in ("/usr/local/sbin/backup-notify-hermes.py", "/usr/local/sbin/backup_report_html.py", "/usr/local/sbin/backup-common.sh",
                 "/usr/local/sbin/backup-system.sh", "/usr/local/sbin/backup-immich.sh", "/usr/local/sbin/homelab-maint",
                 "/usr/local/sbin/nvidia-cdi-refresh", "/usr/local/sbin/smart-bridge.py"):
        assert keep not in moved
    assert not any(legacy.NEVER_DISABLE.match(u) for u in disabled)
    # of the umbrella's own units only the three runner timers the tick replaces may ever be disabled: NOT the check timer, which is the one
    # runner that does not depend on the tick and so the one that notices when the tick dies or silently schedules nothing
    assert {u for u in disabled if u.startswith("homelab-maint")} == {f"homelab-maint-{n}.timer" for n in ("daily", "weekly", "metrics")}
    assert "homelab-maint-check.timer" not in disabled
    for u in disabled:
        if u.startswith("homelab-maint"):
            assert (ROOT / "systemd" / u).exists(), u
    assert "plexmediaserver.service" not in disabled and "sensor-exporter.service" not in disabled


def test_real_inventory_task_replacements_exist_in_the_registry():
    from homelab_maint import cli
    cli.load_tasks()
    try:
        from homelab_maint import routine  # noqa: F401  registers routine_* tasks at import
    except Exception:  # noqa: BLE001 (routine is another stream's file; skip its names if it is not importable yet)
        pass
    have = set(core.REGISTRY)
    for it in real().items:
        if it.via == "task":
            for n in it.replaced_by:
                if n.startswith("routine_") and n not in have:
                    continue
                assert n in have or n == "smart_event", f"{it.name}: unknown task {n}"
        for p in it.parity:
            if p["kind"] == "task" and not p["name"].startswith("routine_"):
                assert p["name"] in have, f"{it.name}: parity task {p['name']} is not registered"
            if p["kind"] == "config":
                key = p["key"].split(".")
                if key[0] == "tasks":
                    assert key[1] in have, f"{it.name}: config task {key[1]}"


def test_real_inventory_dropin_matches_the_shipped_systemd_file():
    drop = next(i for i in real().items if i.name == "immich-recycle-gate-dropin").actions[0]["src"]
    shipped = ROOT / "systemd" / "dropins" / "immich-server-recycle.service.d" / Path(drop).name
    assert shipped.exists() and drop == f"/etc/systemd/system/immich-server-recycle.service.d/{shipped.name}"


def test_ports_only_count_if_the_replacement_can_act():
    """A port that can only REPORT is not a replacement: each port whose legacy thing changes the host needs task_applies, which
    also proves that something passes --apply (the check-tier service ships without it)."""
    by = {i.name: i for i in real().items}
    want = {"comfyui-idle-vram": {("comfyui_idle_reclaim", "tier-check")}, "immich-server-recycle": {("immich_recycle", "tier-check")},
            "prune-openwebui-media": {("openwebui_media_prune", "tier-daily")},
            "docker-prune": {("docker_cache", "tier-daily"), ("docker_images", "tier-daily"), ("docker_containers_prune", "tier-weekly")}}
    for n, pairs in want.items():
        got = {(p["name"], p["tier_job"]) for p in by[n].parity if p["kind"] == "task_applies"}
        assert got == pairs, n
    assert any(p["kind"] == "task" and p["name"] == "docker_prune_parity" for p in by["docker-prune"].parity)


def test_the_tier_jobs_named_by_task_applies_exist_and_the_daily_and_weekly_ones_pass_apply():
    jobs_ = real_jobs()
    for it in real().items:
        for p in it.parity:
            if p["kind"] == "task_applies":
                assert p["tier_job"] in jobs_, f"{it.name}: tier job {p['tier_job']} is not in jobs.toml"
                assert p["tier_job"] in ("tier-check", "tier-daily", "tier-weekly")
                assert jobs_[p["tier_job"]].retire, "the legacy tier service is found through the job's retire list"
    for n in ("tier-daily", "tier-weekly"):
        assert "--apply" in jobs_[n].command, n
    shipped = {n: next(ln for ln in (ROOT / "systemd" / f"homelab-maint-{n}.service").read_text().splitlines() if ln.startswith("ExecStart="))
               for n in ("check", "daily", "weekly")}                       # the ExecStart line only: the check unit's COMMENT says "no --apply"
    assert "--apply" in shipped["daily"] and "--apply" in shipped["weekly"]
    # The finding that motivated the check: today neither the check service nor the tier-check job passes --apply, so the two
    # check-tier C1 ports (comfyui_idle_reclaim, immich_recycle) cannot act until the lead adds it. This test records the state; when the
    # glue lands (both gain --apply) it flips and the assertion below must be updated, which is the reminder that the gate is now green.
    assert ("--apply" in shipped["check"]) == ("--apply" in jobs_["tier-check"].command)


def test_adapters_wait_for_a_proven_notification_path_and_a_live_status_file():
    by = {i.name: i for i in real().items}
    for n in ("backup-system", "backup-immich", "stack-backup", "stack-watchdog", "search-canary"):
        kinds = {p["kind"] for p in by[n].parity}
        assert {"scheduled", "notify"} <= kinds, n
        assert by[n].require_idle, f"{n} must refuse to be cut over while it runs"
    assert any(p["kind"] == "status_json" for p in by["backup-system"].parity)
    assert by["backup-system"].scope == "system" and by["stack-backup"].scope == "user:ohmz"


def test_real_inventory_loader_trust_rules(tmp_path):
    p = tmp_path / "inv.toml"
    p.write_text(REAL_TOML.read_text())
    os.chmod(p, 0o666)
    with pytest.raises(InventoryError, match="group/world writable"):
        legacy.load_inventory(p)
    os.chmod(p, 0o644)
    assert len(legacy.load_inventory(p).items) >= 50
    with pytest.raises(InventoryError):
        legacy.load_inventory(tmp_path / "missing.toml")


# =========================================================================== inventory validation (typos must not become outages)
def bad(raw_items, meta=None):
    with pytest.raises(InventoryError) as e:
        parse_inventory({"meta": meta or {}, "item": raw_items})
    return " | ".join(e.value.problems)


def test_validation_refuses_to_disable_the_platform():
    for u in ("docker.service", "sshd.service", "cloudflared.service", "homelab-maint-tick.timer", "homelab-maint-www.service",
              "homelab-maint-live.service", "homelab-maint-nonsense.timer", "systemd-journald.service", "tailscaled.service",
              "homelab-maint-check.timer", "homelab-maint-check.service",
              "cron.service", "smartmontools.service", "NetworkManager.service"):
        assert "platform unit" in bad([item("x", actions=[{"do": "disable", "unit": u}])]), u
    parse_inventory({"item": [item("x", actions=[{"do": "disable", "unit": "docker-prune.timer"}])]})   # not a prefix match
    for u in ("homelab-maint-daily.timer", "homelab-maint-weekly.timer", "homelab-maint-metrics.timer"):
        parse_inventory({"item": [item("x", actions=[{"do": "disable", "unit": u}])]})                  # the three runner timers the tick replaces


def test_validation_move_and_stub_paths_are_confined():
    for src in ("/etc/passwd", "/usr/local/sbin/../../etc/shadow", "relative.sh", "/usr/local/sbin/", "/etc/systemd/system/x.service"):
        assert "move src" in bad([item("x", actions=[{"do": "move", "src": src}])]) or "drop-in" in bad([item("x", actions=[{"do": "move", "src": src}])])
    assert "stub path" in bad([item("x", actions=[{"do": "stub", "path": "/etc/cron.d/x", "content": "#!/bin/sh\n"}])])
    assert "starting with #!" in bad([item("x", actions=[{"do": "stub", "path": "/usr/local/sbin/x", "content": "echo hi\n"}])])
    assert "writable" in bad([item("x", actions=[{"do": "stub", "path": "/usr/local/sbin/x", "content": "#!/bin/sh\n", "mode": "0777"}])])


def test_validation_structure_errors():
    assert "unknown action" in bad([item("x", actions=[{"do": "rm", "path": "/"}])])
    assert "must not change the host" in bad([item("x", mode="observe", actions=[{"do": "disable", "unit": "a.timer"}])])
    assert "exactly one of tag / match" in bad([item("x", actions=[{"do": "cron_comment", "user": "ohmz"}])])
    assert "at least 8" in bad([item("x", actions=[{"do": "cron_comment", "user": "ohmz", "match": "short"}])])
    assert "duplicate" in bad([item("x"), item("x")])
    assert "unknown item" in bad([item("x", depends_on=["nope"])])
    assert "LATER wave" in bad([item("x", wave=1, depends_on=["y"]), item("y", wave=2)])
    assert "cycle" in bad([item("a", depends_on=["b"]), item("b", depends_on=["a"])])
    assert "needs at least one retire action" in bad([item("x", actions=[])])
    assert "no parity_check" in bad([{**item("x"), "parity_check": None}])
    assert "unmonitored" in bad([item("x", unmonitored="why")])               # port items cannot skip parity
    assert "bad name" in bad([item("Bad Name")])
    assert "pre_retired" in bad([item("x", pre_retired="yesterday")])
    assert "argv" in bad([item("x", parity_check=[{"kind": "command", "argv": "rm -rf /"}])])


def test_validation_lists_every_problem_not_just_the_first():
    with pytest.raises(InventoryError) as e:
        parse_inventory({"item": [item("a", mode="nope"), item("b", kind="nope"), item("c", scope="root")]})
    assert len(e.value.problems) >= 3


def test_unmonitored_observe_item_has_a_trivially_green_parity():
    inv = parse_inventory({"item": [{"name": "o", "kind": "timer", "location": "x.timer", "mode": "observe", "wave": 0, "unmonitored": "cosmetic"}]})
    assert inv.items[0].parity == [{"kind": "none", "note": "cosmetic"}]


def test_parity_defaults_and_derivation():
    inv = parse_inventory({"meta": {"parity_defaults": {"green": 5}}, "item": [
        {"name": "t", "kind": "timer", "location": "x", "mode": "keep", "wave": 0, "replaced_by": ["a", "b"], "via": "probe"},
        item("p", parity_check=[{"kind": "task", "name": "z"}])]})
    t = inv.get("t").parity
    assert t == [{"kind": "probe", "names": ["a", "b"], "green": 5, "min_hours": 48, "fresh_h": 2}]
    assert inv.get("p").parity[0]["green"] == 5 and inv.get("p").parity[0]["fresh_h"] == 2


# =========================================================================== crontab text: never lose an unrelated line
CRON = ("# m h dom mon dow command\n\nSHELL=/bin/sh\n"
        "30 2 * * * /usr/bin/python3 /home/o/b.py >> /home/o/b.log 2>&1 # bazarr-nightly-subtitle-rule\n"
        "15 4 * * 1 /home/o/tunarr-sync/run.sh # tunarr-weekly-channel-sync\n"
        "#0 3 * * 3 /media/x/run_kometa.sh\n"
        "0 5 * * * echo '#not a comment' # keepme\n\t \n")


def test_cron_find_comment_uncomment_roundtrip_is_exact():
    hits = cron_find(CRON, "bazarr-nightly-subtitle-rule", None)
    assert len(hits) == 1
    new = cron_comment(CRON, "bazarr", hits[0])
    assert new != CRON and cron_find(new, "bazarr-nightly-subtitle-rule", None) == []
    assert cron_only_changed(CRON, new, hits, lambda ln: f"{legacy.CRON_MARK}bazarr] {ln}")
    back, hit = cron_uncomment(new, "bazarr")
    assert back == CRON and hit == hits
    # every other line byte-identical, including blank/whitespace lines and the missing-newline-free tail
    a, b = CRON.split("\n"), new.split("\n")
    assert [x for i, (x, y) in enumerate(zip(a, b)) if x == y] and sum(x != y for x, y in zip(a, b)) == 1


def test_cron_find_ignores_comments_and_other_items():
    assert cron_find(CRON, "keepme", None) != []
    assert cron_find(CRON, None, "run_kometa.sh") == []                   # already commented: not an active line
    assert cron_find(CRON, "bazarr-nightly", None) == []                  # tags match whole words only
    assert cron_find(CRON, None, "tunarr-sync/run.sh") == [cron_find(CRON, "tunarr-weekly-channel-sync", None)[0]]
    t = cron_comment(CRON, "one", cron_find(CRON, "keepme", None)[0])
    assert cron_uncomment(t, "other")[1] == []                            # another item's marker is never stripped


def test_cron_only_changed_catches_collateral_damage():
    new = cron_comment(CRON, "i", 3)
    assert not cron_only_changed(CRON, new.replace("SHELL=/bin/sh", ""), [3], lambda ln: f"{legacy.CRON_MARK}i] {ln}")
    assert not cron_only_changed(CRON, new + "extra\n", [3], lambda ln: f"{legacy.CRON_MARK}i] {ln}")


def cron_item(**kw):
    return item("bz", mode="adapter", kind="cron", actions=[{"do": "cron_comment", "user": "ohmz", "tag": "bazarr-nightly-subtitle-rule"}], **kw)


def test_cron_cutover_edits_only_the_tagged_line_and_rollback_restores_it_exactly(rig):
    r = rig([cron_item()])
    r.sys.cron["ohmz"] = CRON
    out = r.m.cutover("bz", apply=True)
    assert out.ok, out.lines
    new = r.sys.cron["ohmz"]
    assert new.count(f"{legacy.CRON_MARK}bz] 30 2 * * *") == 1 and len(new.split("\n")) == len(CRON.split("\n"))
    assert [ln for ln in CRON.split("\n") if "bazarr" not in ln] == [ln for ln in new.split("\n") if "bazarr" not in ln]
    backups = list((r.tmp / "state" / "migration" / "bz").glob("crontab-ohmz-*.before"))
    assert len(backups) == 1 and backups[0].read_text() == CRON and stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    out = r.m.rollback("bz", apply=True)
    assert out.ok and r.sys.cron["ohmz"] == CRON


def test_cron_cutover_refuses_ambiguous_or_missing_and_writes_nothing(rig):
    r = rig([cron_item()])
    r.sys.cron["ohmz"] = CRON + "30 3 * * * /x/other # bazarr-nightly-subtitle-rule\n"
    out = r.m.cutover("bz", apply=True)
    assert out.refused and "expected exactly one" in out.refused and r.sys.calls == []
    r.sys.cron["ohmz"] = None                                           # no crontab at all: the line is simply gone => satisfied
    assert r.m.cutover("bz", apply=True, force=True, reason="line already gone").ok
    assert r.sys.calls == []


def test_cron_edit_aborts_if_the_crontab_changed_underneath_us(rig):
    r = rig([cron_item()])
    r.sys.cron["ohmz"] = CRON
    reads = {"n": 0}
    orig = r.sys.__call__

    def hook(argv):
        if argv[:2] == ["crontab", "-l"]:
            reads["n"] += 1
            if reads["n"] == 3:                                          # 1: plan, 2: fresh probe in execute, 3: just before the write
                r.sys.cron["ohmz"] += "5 5 * * * /added/by/someone/else\n"
        return None
    r.sys.hook = hook
    out = r.m.cutover("bz", apply=True)
    assert out.rc == 4 and "changed while the cutover was running" in "\n".join(out.lines)
    assert r.sys.calls == [] and "added/by/someone/else" in r.sys.cron["ohmz"] and legacy.CRON_MARK not in r.sys.cron["ohmz"]
    assert r.state()["items"]["bz"]["state"] == "failed"


def test_cron_edit_restores_the_original_if_it_does_not_read_back(rig):
    r = rig([cron_item()])
    r.sys.cron["ohmz"] = CRON
    orig_call = FakeSystem.__call__
    state = {"writes": 0}

    def hook(argv):
        if argv[0] == "crontab" and argv[1] == "-u":
            state["writes"] += 1
            if state["writes"] == 1:                                    # cron "accepts" the write but stores something else
                r.sys.calls.append(list(argv))
                r.sys.cron["ohmz"] = "garbage\n"
                return cp(argv)
        return None
    r.sys.hook = hook
    out = r.m.cutover("bz", apply=True)
    assert out.rc == 4 and r.sys.cron["ohmz"] == CRON and state["writes"] == 2


def test_cron_rollback_refuses_when_the_marker_line_was_removed_by_the_owner(rig):
    r = rig([cron_item()])
    r.sys.cron["ohmz"] = CRON
    assert r.m.cutover("bz", apply=True).ok
    r.sys.cron["ohmz"] = "\n".join(ln for ln in r.sys.cron["ohmz"].split("\n") if "HM-RETIRED" not in ln)
    out = r.m.rollback("bz", apply=True)
    assert out.rc == 4 and "no longer in" in "\n".join(out.lines) and r.state()["items"]["bz"]["state"] == "attention"


def test_cron_line_already_commented_by_hand_is_not_touched_on_rollback(rig):
    r = rig([cron_item()])
    r.sys.cron["ohmz"] = CRON.replace("30 2 * * *", "#30 2 * * *")        # the owner commented it already
    out = r.m.cutover("bz", apply=True)
    assert out.ok and r.sys.calls == []
    assert [a["status"] for a in r.state()["items"]["bz"]["actions"]] == ["pre_satisfied"]
    assert r.m.rollback("bz", apply=True).ok and r.sys.calls == [] and "#30 2 * * *" in r.sys.cron["ohmz"]


# =========================================================================== disable action
def timer_item(name="nb", **kw):
    return item(name, **kw)


def test_disable_records_enablement_and_active_state_and_restores_exactly(rig):
    for enabled, active in (("enabled", "active"), ("enabled-runtime", "active"), ("enabled", "inactive"), ("disabled", "active")):
        r = rig([timer_item()])
        r.sys.add_unit("nb.timer", enabled=enabled, active=active)
        r.sys.calls.clear()
        out = r.m.cutover("nb", apply=True)
        assert out.ok, (enabled, active, out.lines)
        u = r.sys.state("nb.timer")
        assert u["UnitFileState"] in ("disabled",) or enabled == "disabled"
        assert u["ActiveState"] == "inactive"
        assert r.m.rollback("nb", apply=True).ok
        assert (r.sys.state("nb.timer")["UnitFileState"], r.sys.state("nb.timer")["ActiveState"]) == (enabled, active), (enabled, active)
        (r.tmp / "state" / "migration.json").unlink()
        for f in (r.tmp / "conf").glob("PAUSE.*"):
            f.unlink()


def test_disable_uses_runtime_flag_for_runtime_enablement(rig):
    r = rig([timer_item()])
    r.sys.add_unit("nb.timer", enabled="enabled-runtime")
    r.m.cutover("nb", apply=True)
    assert ["systemctl", "disable", "--runtime", "--now", "nb.timer"] in r.sys.calls
    r.m.rollback("nb", apply=True)
    assert ["systemctl", "enable", "--runtime", "nb.timer"] in r.sys.calls


def test_disable_unit_already_gone_or_disabled_is_a_noop_and_never_mutates(rig):
    r = rig([timer_item()])
    assert r.m.cutover("nb", apply=True).ok                              # unit does not exist: nothing to retire
    assert r.sys.calls == []
    r.sys.add_unit("nb.timer", enabled="disabled", active="inactive")
    assert r.m.cutover("nb", apply=True).ok and r.sys.calls == []


def test_disable_never_starts_a_oneshot_service_on_rollback(rig):
    r = rig([item("svc", actions=[{"do": "disable", "unit": "svc.service"}], kind="unit")])
    r.sys.add_unit("svc.service", enabled="static", active="active", type_="oneshot")
    assert r.m.cutover("svc", apply=True).ok and r.sys.state("svc.service")["ActiveState"] == "inactive"
    assert r.m.rollback("svc", apply=True).ok
    assert not any(c[1] == "start" for c in r.sys.calls), "starting a oneshot would RUN the job"


def test_disable_refuses_a_linked_unit_that_cannot_be_restored_exactly(rig):
    r = rig([timer_item()])
    r.sys.add_unit("nb.timer", enabled="linked")
    out = r.m.cutover("nb", apply=True)
    assert out.refused and "linked" in out.refused and r.sys.calls == []


def test_user_scope_commands_run_as_the_user_with_the_runtime_dir(rig):
    r = rig([item("uw", scope="user:ohmz", actions=[{"do": "disable", "unit": "uw.timer"}])])
    r.sys.add_unit("uw.timer", scope="user:ohmz")
    assert r.m.cutover("uw", apply=True).ok
    assert r.sys.calls == [["runuser", "-u", "ohmz", "--", "env", "XDG_RUNTIME_DIR=/run/user/1000", "systemctl", "--user", "disable", "--now", "uw.timer"]]
    assert r.sys.state("uw.timer", "user:ohmz")["UnitFileState"] == "disabled"
    assert Host(uids={"ohmz": 1000}).ctl_argv("user:ohmz", ["show", "x"])[-3:] == ["--user", "show", "x"]


def test_unreadable_unit_state_fails_closed(rig):
    r = rig([timer_item()])
    r.sys.add_unit("nb.timer")
    r.sys.hook = lambda argv: cp(argv, 1, "", "boom") if "show" in argv else None
    out = r.m.cutover("nb", apply=True)
    assert out.refused and "cannot read the state" in out.refused and r.sys.calls == []


# =========================================================================== move + stub
def script_item(**kw):
    return item("sc", actions=[{"do": "disable", "unit": "sc.timer"}, {"do": "move", "src": "/usr/local/sbin/sc.sh"}], **kw)


def test_move_goes_to_the_legacy_dir_with_a_readme_and_rollback_is_byte_exact(rig):
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    p = r.put("/usr/local/sbin/sc.sh", "#!/bin/sh\nexit 7\n", 0o750)
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    assert r.m.cutover("sc", apply=True).ok
    dst = r.host.p(f"{LEG}/sc/sc.sh")
    assert not p.exists() and dst.exists() and hashlib.sha256(dst.read_bytes()).hexdigest() == digest
    readme = r.host.p(f"{LEG}/sc/README.md").read_text()
    assert "homelab-maint migrate rollback sc --apply" in readme and "/usr/local/sbin/sc.sh" in readme and "mv " in readme
    assert r.m.rollback("sc", apply=True).ok
    assert p.read_bytes() == b"#!/bin/sh\nexit 7\n" and stat.S_IMODE(p.stat().st_mode) == 0o750 and not dst.exists()
    assert "put back by `migrate rollback`" in r.host.p(f"{LEG}/sc/README.md").read_text()      # nothing is deleted, even the README


def test_move_cross_device_copies_verifies_then_unlinks(rig, monkeypatch):
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    p = r.put("/usr/local/sbin/sc.sh")
    real_rename = os.rename
    calls = {"n": 0}

    def rename(a, b):
        calls["n"] += 1
        if str(a).endswith("sc.sh"):
            raise OSError(errno.EXDEV, "cross-device")
        return real_rename(a, b)
    monkeypatch.setattr(os, "rename", rename)
    assert r.m.cutover("sc", apply=True).ok and not p.exists() and r.host.p(f"{LEG}/sc/sc.sh").exists()
    assert r.m.rollback("sc", apply=True).ok and p.exists() and stat.S_IMODE(p.stat().st_mode) == 0o750


def test_move_cross_device_keeps_the_source_if_the_copy_does_not_verify(rig, monkeypatch):
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    p = r.put("/usr/local/sbin/sc.sh")
    monkeypatch.setattr(os, "rename", lambda a, b: (_ for _ in ()).throw(OSError(errno.EXDEV, "x")) if str(a).endswith("sc.sh") else os.replace(a, b))
    import shutil
    monkeypatch.setattr(shutil, "copy2", lambda s, d, follow_symlinks=True: Path(d).write_text("CORRUPT"))
    out = r.m.cutover("sc", apply=True)
    assert out.rc == 4 and p.read_text() == "#!/bin/sh\necho hi\n" and not r.host.p(f"{LEG}/sc/sc.sh").exists()
    assert r.sys.state("sc.timer")["UnitFileState"] == "enabled"          # and the disable that already ran was undone


def test_move_refuses_to_overwrite_a_legacy_copy_or_move_a_directory(rig):
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    r.put("/usr/local/sbin/sc.sh")
    r.put(f"{LEG}/sc/sc.sh", "old copy")
    out = r.m.cutover("sc", apply=True)
    assert out.refused and "already exists" in out.refused and r.sys.calls == [] and r.exists("/usr/local/sbin/sc.sh")
    r.host.p(f"{LEG}/sc/sc.sh").unlink()
    r.host.p("/usr/local/sbin/sc.sh").unlink()
    r.host.p("/usr/local/sbin/sc.sh").mkdir(parents=True)
    assert "directory" in r.m.cutover("sc", apply=True).refused


def test_move_a_symlink_moves_the_link_itself(rig):
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    tgt = r.put("/opt/real.sh")
    link = r.host.p("/usr/local/sbin/sc.sh")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to("/opt/real.sh")
    assert r.m.cutover("sc", apply=True).ok
    assert os.path.islink(r.host.p(f"{LEG}/sc/sc.sh")) and tgt.exists() and not os.path.lexists(link)
    assert r.m.rollback("sc", apply=True).ok and os.readlink(link) == "/opt/real.sh"


def test_rollback_refuses_when_something_new_sits_at_the_old_path_or_the_copy_changed(rig):
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    r.put("/usr/local/sbin/sc.sh")
    assert r.m.cutover("sc", apply=True).ok
    r.put("/usr/local/sbin/sc.sh", "somebody's new script")
    out = r.m.rollback("sc", apply=True)
    assert out.rc == 4 and "not overwriting" in "\n".join(out.lines) and r.host.p("/usr/local/sbin/sc.sh").read_text() == "somebody's new script"
    assert r.sys.state("sc.timer")["UnitFileState"] == "disabled"         # stopped before touching anything else: no half restore
    r.host.p("/usr/local/sbin/sc.sh").unlink()
    r.host.p(f"{LEG}/sc/sc.sh").write_text("edited")
    out = r.m.rollback("sc", apply=True)
    assert out.rc == 4 and "modified" in "\n".join(out.lines)


def stub_item(**kw):
    return item("st", kind="hook", actions=[{"do": "move", "src": "/usr/local/sbin/h.sh"},
                                            {"do": "stub", "path": "/usr/local/sbin/h.sh", "mode": "0755",
                                             "content": "#!/bin/sh\nexec {legacy_dir}/h.sh \"$@\"\n"}], **kw)


def test_stub_forwards_from_the_old_path_and_rollback_removes_only_our_stub(rig):
    r = rig([stub_item()])
    r.put("/usr/local/sbin/h.sh", "#!/bin/sh\necho legacy\n", 0o700)
    out = r.m.cutover("st", apply=True)
    assert out.ok, out.lines
    stub = r.host.p("/usr/local/sbin/h.sh")
    assert stub.read_text() == f"#!/bin/sh\nexec {LEG}/st/h.sh \"$@\"\n" and stat.S_IMODE(stub.stat().st_mode) == 0o755
    assert r.m.rollback("st", apply=True).ok
    assert stub.read_text() == "#!/bin/sh\necho legacy\n" and stat.S_IMODE(stub.stat().st_mode) == 0o700


def test_stub_dry_run_plans_move_then_stub_without_tripping_over_the_occupied_path(rig):
    r = rig([stub_item()])
    r.put("/usr/local/sbin/h.sh")
    out = r.m.cutover("st")
    text = "\n".join(out.lines)
    assert out.ok and "write stub /usr/local/sbin/h.sh" in text and text.index("cp -p ") < text.index("write stub"), \
        "a script that something still calls is COPIED first and replaced in one step, never moved away"
    assert r.exists("/usr/local/sbin/h.sh") and not r.exists(f"{LEG}/st/h.sh")


def test_stub_rollback_refuses_to_delete_a_modified_stub(rig):
    r = rig([stub_item()])
    r.put("/usr/local/sbin/h.sh")
    assert r.m.cutover("st", apply=True).ok
    r.host.p("/usr/local/sbin/h.sh").write_text("#!/bin/sh\n# owner edited the stub\n")
    out = r.m.rollback("st", apply=True)
    assert out.rc == 4 and "was modified" in "\n".join(out.lines) and "owner edited" in r.host.p("/usr/local/sbin/h.sh").read_text()


def test_stub_refuses_an_occupied_path_that_is_not_ours(rig):
    r = rig([item("o", kind="hook", actions=[{"do": "stub", "path": "/usr/local/sbin/x.sh", "content": "#!/bin/sh\n"}])])
    r.put("/usr/local/sbin/x.sh", "something else")
    out = r.m.cutover("o", apply=True)
    assert out.refused and "occupied" in out.refused and r.host.p("/usr/local/sbin/x.sh").read_text() == "something else"


def test_dropin_move_reloads_systemd_both_ways(rig):
    src = "/etc/systemd/system/x.service.d/10-gate.conf"
    r = rig([item("dp", kind="unit", actions=[{"do": "move", "src": src, "reload": "system"}])])
    r.put(src, "[Service]\nExecCondition=/bin/true\n", 0o644)
    assert r.m.cutover("dp", apply=True).ok and r.sys.calls == [["systemctl", "daemon-reload"]]
    assert r.m.rollback("dp", apply=True).ok and r.sys.calls == [["systemctl", "daemon-reload"]] * 2
    assert r.host.p(src).read_text().startswith("[Service]")


# =========================================================================== ordering, dry run, idempotence
def test_actions_run_in_order_and_dry_run_prints_the_exact_commands_and_changes_nothing(rig):
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    r.put("/usr/local/sbin/sc.sh")
    out = r.m.cutover("sc")                                              # dry run is the default
    text = "\n".join(out.lines)
    assert out.ok and "DRY RUN" in text
    assert "$ systemctl disable --now sc.timer" in text
    assert f"$ mv /usr/local/sbin/sc.sh {LEG}/sc/sc.sh" in text
    assert text.index("systemctl disable") < text.index("$ mv ")
    assert r.sys.calls == [] and r.exists("/usr/local/sbin/sc.sh") and not r.exists(LEG) and not r.m.state_path.exists()
    assert [a[4] for a in r.audits] == ["dry-run"] * 4                   # mkdir, README, mv, disable: each audited as would-do
    assert not r.notes and not (r.tmp / "state" / "maintenance-journal.jsonl").exists()


def test_apply_runs_in_declared_order(rig):
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    r.put("/usr/local/sbin/sc.sh")
    order = []
    r.sys.hook = lambda argv: order.append(("ctl", argv[1])) and None
    orig = r.host.safe_move
    r.host.safe_move = lambda s, d: (order.append(("mv", s)), orig(s, d))[1]
    assert r.m.cutover("sc", apply=True).ok
    assert order.index(("ctl", "disable")) < order.index(("mv", "/usr/local/sbin/sc.sh"))
    whats = [a[1] for a in r.audits if a[4] == "done"]
    assert whats[0].endswith(":disable") and whats[-1].endswith(":move") and all(a[0] == "migrate" for a in r.audits)


def test_cutover_is_idempotent(rig):
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    r.put("/usr/local/sbin/sc.sh")
    assert r.m.cutover("sc", apply=True).ok
    before, calls, st = len(r.audits), list(r.sys.calls), r.state()
    out = r.m.cutover("sc", apply=True)
    assert out.ok and "already retired" in "\n".join(out.lines)
    assert r.sys.calls == calls and len(r.audits) == before and r.state() == st
    assert len(r.notes) == 1                                             # and it did not announce itself twice


def test_partial_retirement_by_hand_finishes_only_what_is_left(rig):
    r = rig([script_item()])
    r.sys.add_unit("sc.timer", enabled="disabled", active="inactive")    # the owner already disabled the timer
    r.put("/usr/local/sbin/sc.sh")
    assert r.m.cutover("sc", apply=True).ok
    assert not any(c[1] == "disable" for c in r.sys.calls)
    acts = r.state()["items"]["sc"]["actions"]
    assert [a["status"] for a in acts] == ["pre_satisfied", "done"]
    assert r.m.rollback("sc", apply=True).ok
    assert r.sys.state("sc.timer")["UnitFileState"] == "disabled" and r.exists("/usr/local/sbin/sc.sh")   # the pre-existing state stays


# =========================================================================== gates: parity, dependencies, soak, busy, PAUSE
def red_parity():
    return {"parity_check": [{"kind": "config", "key": "tasks.x.mode", "equals": "apply"}]}


def test_parity_not_green_refuses_and_changes_nothing(rig):
    r = rig([item("g", **red_parity())], config=lambda: {"tasks": {"x": {"mode": "report"}}})
    r.sys.add_unit("g.timer")
    out = r.m.cutover("g", apply=True)
    assert out.refused and "parity is not green" in out.refused and "tasks.x.mode = 'report'" in "\n".join(out.lines)
    assert r.sys.calls == [] and not r.m.state_path.exists() and "dry-run" not in r.outcomes()


def test_force_needs_a_reason_and_is_recorded(rig):
    r = rig([item("g", **red_parity())], config=lambda: {"tasks": {"x": {"mode": "report"}}})
    r.sys.add_unit("g.timer")
    out = r.m.cutover("g", apply=True, force=True)
    assert out.refused and "--reason" in out.refused and r.sys.calls == []
    out = r.m.cutover("g", apply=True, force=True, reason="owner accepted the risk")
    assert out.ok and r.state()["items"]["g"]["forced"] is True
    j = json.loads((r.tmp / "state" / "maintenance-journal.jsonl").read_text().splitlines()[-1])
    assert "FORCED: owner accepted the risk" in j["detail"] and j["tag"] == "migrate"


def test_green_parity_passes(rig):
    r = rig([item("g", **red_parity())], config=lambda: {"tasks": {"x": {"mode": "apply"}}})
    r.sys.add_unit("g.timer")
    assert r.m.cutover("g", apply=True).ok and r.state()["items"]["g"]["parity"]["ok"] is True


def test_dependencies_must_be_retired_and_soaked(rig):
    r = rig([item("a", soak_days=7), item("b", depends_on=["a"], wave=4)])
    r.sys.add_unit("a.timer")
    r.sys.add_unit("b.timer")
    out = r.m.cutover("b", apply=True)
    assert out.refused and "a is pending; cut it over first" in "\n".join(out.lines)
    assert r.m.cutover("a", apply=True).ok
    out = r.m.cutover("b", apply=True)
    assert out.refused and "still soaking" in "\n".join(out.lines) and "7.0 more day(s)" in "\n".join(out.lines)
    r.now += 6.5 * 86400
    assert r.m.cutover("b", apply=True).refused
    r.now += 0.6 * 86400
    assert r.m.cutover("b", apply=True).ok
    assert r.sys.state("b.timer")["UnitFileState"] == "disabled"


def test_a_running_unit_blocks_the_cutover_even_with_force(rig):
    r = rig([item("bk", require_idle=["bk.service"], mode="adapter")])
    r.sys.add_unit("bk.timer")
    r.sys.add_unit("bk.service", enabled="static", active="active")
    out = r.m.cutover("bk", apply=True, force=True, reason="really want it")
    assert out.refused and "bk.service is running right now" in out.refused and r.sys.calls == []
    r.sys.units[("system", "bk.service")]["ActiveState"] = "inactive"
    assert r.m.cutover("bk", apply=True).ok
    r2 = rig([item("bk2", require_idle=["nothere.service"], mode="adapter")])           # an unreadable state counts as busy
    r2.sys.add_unit("bk2.timer")
    r2.sys.hook = lambda argv: cp(argv, 1, "", "dbus down") if "nothere.service" in argv else None
    assert "busy" in r2.m.cutover("bk2", apply=True, force=True, reason="forced anyway").refused


@pytest.mark.parametrize("pause_file", ["PAUSE", "PAUSE.migrate"])
def test_pause_blocks_apply_but_not_the_dry_run(rig, pause_file):
    r = rig([timer_item()])
    r.sys.add_unit("nb.timer")
    (r.conf / pause_file).write_text("")
    out = r.m.cutover("nb", apply=True)
    assert out.refused and "PAUSE" in out.refused and r.sys.calls == [] and "refused-paused" in r.outcomes()
    assert r.m.cutover("nb").ok                                          # a dry run is read-only: still allowed
    (r.conf / pause_file).unlink()
    assert r.m.cutover("nb", apply=True).ok


def test_pause_that_appears_mid_run_stops_it_and_undoes_what_was_done(rig):
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    r.put("/usr/local/sbin/sc.sh")
    orig = r.sys.__call__

    def hook(argv):
        if argv[:2] == ["systemctl", "disable"]:                         # the first mutation lands, then somebody drops PAUSE
            r.sys.calls.append(list(argv))
            r.sys.units[("system", "sc.timer")].update(UnitFileState="disabled", ActiveState="inactive")
            (r.conf / "PAUSE").write_text("")
            return cp(argv)
        return None
    r.sys.hook = hook
    out = r.m.cutover("sc", apply=True)
    assert out.rc == 4 and "refused-paused" in r.outcomes()
    assert r.exists("/usr/local/sbin/sc.sh")
    u = r.sys.state("sc.timer")
    assert (u["UnitFileState"], u["ActiveState"]) == ("enabled", "active"), "the in-flight undo restores the status quo despite PAUSE"
    assert r.state()["items"]["sc"]["state"] == "failed"


def test_rollback_while_paused_needs_force(rig):
    r = rig([timer_item()])
    r.sys.add_unit("nb.timer")
    assert r.m.cutover("nb", apply=True).ok
    (r.conf / "PAUSE").write_text("")
    out = r.m.rollback("nb", apply=True)
    assert out.refused and "--force" in out.refused and r.sys.state("nb.timer")["UnitFileState"] == "disabled"
    assert r.m.rollback("nb", apply=True, force=True, reason="incident: umbrella misbehaving").ok
    assert r.sys.state("nb.timer")["UnitFileState"] == "enabled"


def test_failure_midway_undoes_the_earlier_actions_and_records_it(rig):
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    p = r.put("/usr/local/sbin/sc.sh")
    r.host.safe_move = lambda s, d: (_ for _ in ()).throw(OSError(errno.EIO, "disk on fire"))
    out = r.m.cutover("sc", apply=True)
    assert out.rc == 4 and "disk on fire" in "\n".join(out.lines) and "undone" in out.lines[-1]
    u = r.sys.state("sc.timer")
    assert (u["UnitFileState"], u["ActiveState"]) == ("enabled", "active") and p.exists()
    assert r.state()["items"]["sc"]["state"] == "failed"
    assert any(o.startswith("failed:") for o in r.outcomes())
    j = [json.loads(x) for x in (r.tmp / "state" / "maintenance-journal.jsonl").read_text().splitlines()]
    assert j[-1]["title"].startswith("Legacy cutover failed")
    assert r.notes[-1]["significant"] is True                            # a failed cutover is worth a text


def test_an_undo_that_cannot_finish_ends_in_attention_with_a_precise_message(rig):
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    r.put("/usr/local/sbin/sc.sh")
    r.host.safe_move = lambda s, d: (_ for _ in ()).throw(OSError(errno.EIO, "boom"))
    n = {"enable": 0}

    def hook(argv):
        if argv[:2] == ["systemctl", "enable"]:
            return cp(argv, 1, "", "cannot enable")
        return None
    r.sys.hook = hook
    out = r.m.cutover("sc", apply=True)
    assert out.rc == 4 and "could not restore disable sc.timer" in "\n".join(out.lines)
    assert r.state()["items"]["sc"]["state"] == "attention"


def test_lock_prevents_two_concurrent_migrations(rig):
    import fcntl
    r = rig([timer_item()])
    r.sys.add_unit("nb.timer")
    (r.tmp / "run").mkdir(exist_ok=True)
    with open(r.tmp / "run" / "migrate.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out = r.m.cutover("nb", apply=True)
        assert out.refused and "another migrate run" in out.refused and r.sys.calls == []
        assert r.m.cutover("nb").ok                                      # a dry run does not need the lock


# =========================================================================== rollback behaviour
def test_rollback_restores_everything_and_pauses_the_replacement_task(rig):
    r = rig([item("j", mode="port", job="j-job", pause_on_rollback=["j_task"], via="task", replaced_by="j_task")])
    r.sys.add_unit("j.timer")
    assert r.m.cutover("j", apply=True).ok
    assert r.jobs.overrides == {"j-job": "retired"}
    out = r.m.rollback("j", apply=True)
    assert out.ok and r.sys.state("j.timer")["UnitFileState"] == "enabled" and r.jobs.overrides == {}
    assert (r.conf / "PAUSE.j_task").exists(), "the restored legacy job and a mutating native task must never both act"
    assert r.state()["items"]["j"]["state"] == "rolled_back" and r.state()["items"]["j"]["paused_replacements"] == ["j_task"]
    assert r.notes[-1]["significant"] is True and "legacy state restored exactly" in r.notes[-1]["summary"]
    assert r.m.cutover("j", apply=True).ok                               # re-cutover resumes what the rollback paused
    assert not (r.conf / "PAUSE.j_task").exists()


def test_rollback_does_not_remove_a_pause_the_owner_set(rig):
    r = rig([item("j", pause_on_rollback=["j_task"])])
    r.sys.add_unit("j.timer")
    (r.conf / "PAUSE.j_task").write_text("owner")
    assert r.m.cutover("j", apply=True).ok
    r.m.rollback("j", apply=True)
    r.m.cutover("j", apply=True)
    assert (r.conf / "PAUSE.j_task").read_text() == "owner"


def test_rollback_refused_while_dependents_are_retired(rig):
    r = rig([item("a"), item("b", wave=4, depends_on=["a"])])
    r.sys.add_unit("a.timer")
    r.sys.add_unit("b.timer")
    assert r.m.cutover("a", apply=True).ok and r.m.cutover("b", apply=True).ok
    out = r.m.rollback("a", apply=True)
    assert out.refused and "b depend on a" in out.refused and r.sys.state("a.timer")["UnitFileState"] == "disabled"
    assert r.m.rollback("b", apply=True).ok and r.m.rollback("a", apply=True).ok


def test_rollback_of_an_untouched_or_prehistoric_item_says_so(rig):
    r = rig([timer_item(), item("old", kind="cron", pre_retired="2026-10-01", actions=[{"do": "cron_comment", "user": "root", "match": "journalctl --vacuum"}])])
    out = r.m.rollback("nb", apply=True)
    assert out.ok and "nothing to roll back" in "\n".join(out.lines)
    out = r.m.rollback("old", apply=True)
    assert out.refused and "before this tool existed" in out.refused


def test_rollback_dry_run_prints_the_inverse_commands(rig):
    r = rig([script_item(via="task", replaced_by="sc_task", mode="port", job="sc-job", pause_on_rollback=["sc_task"])])
    r.sys.add_unit("sc.timer")
    r.put("/usr/local/sbin/sc.sh")
    assert r.m.cutover("sc", apply=True).ok
    n = len(r.sys.calls)
    out = r.m.rollback("sc")
    text = "\n".join(out.lines)
    assert "$ systemctl enable sc.timer" in text and "$ systemctl start sc.timer" in text and f"$ mv {LEG}/sc/sc.sh /usr/local/sbin/sc.sh" in text
    assert text.index("job mode sc-job reset") < text.index("mv ") < text.index("systemctl enable"), "reverse order: scheduler first"
    assert "pause sc_task" in text and len(r.sys.calls) == n and not (r.conf / "PAUSE.sc_task").exists() and r.jobs.overrides == {"sc-job": "retired"}


def test_rollback_after_the_inventory_changed_refuses_rather_than_guessing(rig):
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    r.put("/usr/local/sbin/sc.sh")
    assert r.m.cutover("sc", apply=True).ok
    r.m.inv = parse_inventory({"item": [item("sc", actions=[{"do": "move", "src": "/usr/local/sbin/sc.sh"}, {"do": "disable", "unit": "sc.timer"}])]})
    out = r.m.rollback("sc", apply=True)
    assert out.refused and "inventory changed" in out.refused


# =========================================================================== journal / change log / notification
def test_cutover_writes_journal_change_log_audit_and_one_notification(rig):
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    r.put("/usr/local/sbin/sc.sh")
    assert r.m.cutover("sc", apply=True).ok
    sdir = r.tmp / "state"
    j = [json.loads(x) for x in (sdir / "maintenance-journal.jsonl").read_text().splitlines()]
    c = [json.loads(x) for x in (sdir / "changes.jsonl").read_text().splitlines()]
    assert j[0]["title"].startswith("Legacy cutover:") and "2 action(s) done" in j[0]["detail"] and j[0]["ts"][-5] in "+-"
    assert c[0]["task"] == "migrate" and c[0]["kind"] == "config" and c[0]["outcome"] == "done" and c[0]["verified"] is True
    assert set(c[0]) == {"ts", "task", "kind", "detail", "bytes", "outcome", "verified"} and c[0]["ts"] == NOW and len(c[0]["detail"]) <= 200
    assert len(r.notes) == 1 and r.notes[0]["task"] == "migrate" and r.notes[0]["significant"] is False
    assert all(a[0] == "migrate" and a[1].startswith("sc:") for a in r.audits)
    assert r.m.journal(5)[-1]["item"] == "sc"
    assert "Legacy cutover" in "\n".join(f"{e['title']}" for e in r.m.journal(5))


def test_a_failing_notifier_never_breaks_the_cutover(rig):
    r = rig([timer_item()])
    r.sys.add_unit("nb.timer")
    r.m._notifier = lambda ev: (_ for _ in ()).throw(RuntimeError("smtp down"))
    assert r.m.cutover("nb", apply=True).ok and r.state()["items"]["nb"]["state"] == "retired"


def test_default_notifier_is_a_noop_under_pytest():
    legacy._default_notify({"task": "migrate", "title": "t", "summary": "s", "done": ["x"], "significant": True, "facts": {}})


def test_state_file_is_private_and_holds_no_bulk_text(rig):
    r = rig([cron_item()])
    r.sys.cron["ohmz"] = CRON
    assert r.m.cutover("bz", apply=True).ok
    assert stat.S_IMODE(r.m.state_path.stat().st_mode) == 0o600
    raw = r.m.state_path.read_text()
    assert "SHELL=/bin/sh" not in raw and "keepme" not in raw            # the crontab copy lives in its own 0600 file, not in the state


# =========================================================================== parity checks
def src(**kw):
    return Sources(now=lambda: NOW, **kw)


def hist(task, statuses, span_h=60, last_age_h=0.1):
    n = len(statuses)
    ts = [NOW - last_age_h * 3600 - (n - 1 - i) * (span_h * 3600 / max(n - 1, 1)) for i in range(n)]
    return [{"t": t, "kind": "task", "task": task, "status": s} for t, s in zip(ts, statuses)]


def history_of(recs):
    return lambda since, kind=None: [r for r in recs if r.get("kind") == kind and r["t"] >= NOW - since]


def chk(spec, **kw):
    return run_check(spec, src(**kw))[0]


def task_spec(**kw):
    return {"kind": "task", "name": "t", "green": 8, "min_hours": 48, "fresh_h": 2, **kw}


def test_task_parity_needs_a_long_enough_unbroken_green_streak():
    ok = chk(task_spec(), history=history_of(hist("t", ["ok"] * 12)))
    assert ok.ok is True and "12 green runs over 60.0 h" in ok.detail
    assert chk(task_spec(), history=history_of(hist("t", ["ok"] * 12, span_h=20))).ok is False            # green but too short a soak
    assert chk(task_spec(), history=history_of(hist("t", ["ok"] * 4))).ok is False                        # too few runs (4 over 60 h)
    broken = chk(task_spec(), history=history_of(hist("t", ["ok"] * 6 + ["warn"] + ["ok"] * 5, span_h=110)))
    assert broken.ok is False and "5 green runs" in broken.detail                                        # the warn resets the streak
    assert chk(task_spec(), history=history_of(hist("t", ["ok"] * 11 + ["crit"]))).ok is False
    assert chk(task_spec(), history=history_of(hist("t", ["ok"] * 12 + ["info"]))).ok is True            # info counts as green
    assert chk(task_spec(), history=history_of(hist("t", ["ok"] * 11 + ["skipped"]))).ok is False       # skipped proves nothing
    assert chk(task_spec(), history=history_of(hist("t", ["error"] * 12))).ok is False


def test_task_parity_is_fail_closed_on_stale_missing_or_foreign_history():
    assert chk(task_spec(), history=history_of([])).detail == "no runs recorded yet"
    stale = chk(task_spec(), history=history_of(hist("t", ["ok"] * 12, last_age_h=5)))
    assert stale.ok is False and "5.0 h ago" in stale.detail
    assert chk(task_spec(), history=history_of(hist("other", ["ok"] * 12))).ok is False
    assert chk(task_spec(fresh_h=30, green=3, min_hours=2), history=history_of(hist("t", ["ok"] * 4, span_h=3, last_age_h=20))).ok is True
    assert chk(task_spec(), history=lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))).ok is False


def prow(name, state="up", age_h=0.1):
    return {"name": name, "state": state, "last_run": NOW - age_h * 3600, "detail": ""}


def test_probe_parity_requires_a_defined_probe_and_no_bad_marks():
    rows = lambda: [prow("radarr"), prow("sonarr")]                                                  # noqa: E731
    recs = [{"t": NOW - 3600 * (60 - i * 5), "kind": "probe", "n": 2, "up": 2, "bad": {}} for i in range(13)]
    spec = {"kind": "probe", "names": ["radarr", "sonarr"], "green": 8, "min_hours": 48, "fresh_h": 2}
    res = run_check(spec, src(history=history_of(recs), probe_rows=rows))
    assert [c.ok for c in res] == [True, True]
    res = run_check({**spec, "names": ["radarr", "typo-probe"]}, src(history=history_of(recs), probe_rows=rows))
    assert [c.ok for c in res] == [True, False] and "not defined" in res[1].detail
    recs[-3]["bad"] = {"sonarr": "d"}
    res = run_check(spec, src(history=history_of(recs), probe_rows=rows))
    assert res[0].ok is True and res[1].ok is False


def test_probe_parity_is_not_vacuous_for_a_probe_that_never_ran_or_is_not_up():
    """A probe that is paused, skipped (optional and never seen up) or never evaluated has no 'bad' mark in history, which used to read as
    green ('120 green runs over 59.5 h') as long as ANY probe record existed."""
    recs = [{"t": NOW - 3600 * (60 - i * 5), "kind": "probe", "n": 2, "up": 2, "bad": {}} for i in range(13)]
    spec = {"kind": "probe", "names": ["radarr"], "green": 8, "min_hours": 48, "fresh_h": 2}
    for state in ("skipped", "paused", "unknown", "down", "warn"):
        c = run_check(spec, src(history=history_of(recs), probe_rows=lambda st=state: [prow("radarr", st)]))[0]
        assert c.ok is False and f"'{state}'" in c.detail, state
    stale = run_check(spec, src(history=history_of(recs), probe_rows=lambda: [prow("radarr", age_h=5)]))[0]
    assert stale.ok is False and "5.0 h ago" in stale.detail
    never = run_check(spec, src(history=history_of(recs), probe_rows=lambda: [{"name": "radarr", "state": "up", "last_run": None}]))[0]
    assert never.ok is False and "never evaluated" in never.detail
    quiet = [{**r, "n": 0} for r in recs]                                                              # runs that evaluated nothing prove nothing
    assert run_check(spec, src(history=history_of(quiet), probe_rows=lambda: [prow("radarr")]))[0].ok is False
    assert run_check(spec, src(history=history_of(recs), probe_rows=lambda: [prow("radarr")]))[0].ok is True


def test_probe_parity_honours_a_per_run_evaluated_list_when_probes_py_writes_one():
    """probes._history may add "ev": [names evaluated]: then a run that did not evaluate THIS probe is no evidence for it."""
    spec = {"kind": "probe", "names": ["radarr", "sonarr"], "green": 8, "min_hours": 48, "fresh_h": 2}
    recs = [{"t": NOW - 3600 * (60 - i * 5), "kind": "probe", "n": 1, "up": 1, "bad": {}, "ev": ["radarr"]} for i in range(13)]
    res = run_check(spec, src(history=history_of(recs), probe_rows=lambda: [prow("radarr"), prow("sonarr")]))
    assert [c.ok for c in res] == [True, False] and res[1].detail == "no runs recorded yet"


def test_config_status_file_and_command_checks(tmp_path):
    cfg = lambda: {"tasks": {"a": {"mode": "apply", "n": 3}}}                                             # noqa: E731
    assert chk({"kind": "config", "key": "tasks.a.mode", "equals": "apply"}, config=cfg).ok is True
    assert chk({"kind": "config", "key": "tasks.a.mode", "equals": "report"}, config=cfg).ok is False
    assert chk({"kind": "config", "key": "tasks.zz.mode", "equals": "apply"}, config=cfg).ok is False
    assert chk({"kind": "config", "key": "tasks.a.n", "in": [1, 3]}, config=cfg).ok is True
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"result": "ok"}))
    os.utime(f, (NOW - 3600, NOW - 3600))
    spec = {"kind": "status_json", "path": str(f), "max_age_hours": 2}
    assert chk(spec).ok is True
    assert chk({**spec, "max_age_hours": 0.5}).ok is False and "old" in chk({**spec, "max_age_hours": 0.5}).detail
    f.write_text(json.dumps({"result": "failed"}))
    os.utime(f, (NOW - 3600, NOW - 3600))
    assert chk(spec).ok is False
    assert chk({"kind": "file_age", "path": str(f), "max_age_s": 7200}).ok is True
    assert chk({"kind": "file_age", "path": str(tmp_path / "nope"), "max_age_s": 7200}).ok is False
    run = lambda argv, input_=None, timeout=60: cp(argv, 0, "385.00\n")                                    # noqa: E731
    assert chk({"kind": "command", "argv": ["x"], "regex": "^385\\."}, run=run).ok is True
    assert chk({"kind": "command", "argv": ["x"], "regex": "^300"}, run=run).ok is False
    assert chk({"kind": "command", "argv": ["x"], "ok_codes": [3]}, run=run).ok is False
    assert chk({"kind": "command", "argv": ["x"]}, run=lambda *a, **k: cp(["x"], 1)).ok is False
    why = chk({"kind": "command", "argv": ["x"]}, run=lambda *a, **k: cp(["x"], 1, "", "ImportError: no module named native\nmore")).detail
    assert why == "rc=1: ImportError: no module named native"                        # a failing check says why, in one short line


def applies(cfg, job_mode="managed", command=("{self}", "run", "--tier", "check"), svc_out="{ path=/usr/local/sbin/homelab-maint ; argv[]=/usr/local/sbin/homelab-maint run --tier check ; }",
            retire=("system:homelab-maint-check.timer",)):
    calls = []

    def run(argv, input_=None, timeout=60):
        calls.append(argv)
        return cp(argv, 0, svc_out)
    attrs = {"mode": job_mode, "command": list(command), "retire": list(retire)}
    c = chk({"kind": "task_applies", "name": "t", "tier_job": "tier-check"}, config=lambda: {"tasks": {"t": cfg}} if cfg is not None else {"tasks": {}},
            job_attr=lambda job, attr: attrs[attr], run=run)
    return c, calls


def test_task_applies_needs_mode_apply_and_something_that_passes_apply():
    c, _ = applies({"mode": "report"})
    assert c.ok is False and "not 'apply'" in c.detail
    assert applies(None)[0].ok is False and applies({})[0].ok is False                       # no config at all = report mode
    c, _ = applies({"mode": "apply"})                                                         # managed tier job, no --apply: cannot act
    assert c.ok is False and "nothing runs it with --apply" in c.detail and "tier-check" in c.detail and "schedule" in c.detail
    c, _ = applies({"mode": "apply"}, command=("{self}", "run", "--tier", "check", "--apply"))
    assert c.ok is True and "tier-check starts the tier with --apply" in c.detail
    c, calls = applies({"mode": "apply", "schedule": "*/5 * * * *"})                          # its own cron line: the tick adds --apply itself
    assert c.ok is True and "own schedule" in c.detail and calls == []


def test_task_applies_reads_the_legacy_service_while_the_tier_job_is_not_managed_yet():
    c, calls = applies({"mode": "apply"}, job_mode="observe")                                 # the legacy service still drives the tier
    assert c.ok is False and calls == [["systemctl", "show", "-p", "ExecStart", "--value", "homelab-maint-check.service"]]
    assert "homelab-maint-check.service has none" in c.detail
    c, _ = applies({"mode": "apply"}, job_mode="observe", svc_out="{ argv[]=/usr/local/sbin/homelab-maint run --tier check --apply ; }")
    assert c.ok is True and "homelab-maint-check.service starts the tier with --apply" in c.detail
    c, _ = applies({"mode": "apply"}, job_mode="observe", retire=())                          # cannot find the service: not green
    assert c.ok is False
    c, _ = applies({"mode": "apply"}, job_mode="observe", svc_out="")
    assert c.ok is False


def test_job_attr_contains_and_its_validation():
    attrs = {("tier-check", "command"): ["{self}", "run", "--tier", "check", "--apply"]}
    get = lambda job, attr: attrs[(job, attr)]                                                    # noqa: E731
    assert chk({"kind": "job_attr", "job": "tier-check", "attr": "command", "contains": "--apply"}, job_attr=get).ok is True
    assert chk({"kind": "job_attr", "job": "tier-check", "attr": "command", "contains": "--nope"}, job_attr=get).ok is False
    assert "exactly one of equals / contains" in bad([item("x", parity_check=[{"kind": "job_attr", "job": "a", "attr": "b", "equals": 1, "contains": 2}])])
    assert "task_applies needs tier_job" in bad([item("x", parity_check=[{"kind": "task_applies", "name": "a"}])])


def test_notify_scheduled_os_job_manual_and_none_checks():
    n = [{"ts": NOW - 5 * 86400, "kind": "test", "ok": True}, {"ts": NOW - 40 * 86400, "kind": "maintenance", "ok": True},
         {"ts": NOW - 1 * 86400, "kind": "test", "ok": False}]
    assert chk({"kind": "notify", "kinds": ["test"], "max_age_days": 30}, notifications=lambda: n).ok is True
    assert chk({"kind": "notify", "kinds": ["test"], "max_age_days": 3}, notifications=lambda: n).ok is False      # too old / failed
    assert chk({"kind": "notify", "kinds": ["maintenance"], "max_age_days": 30}, notifications=lambda: n).ok is False
    ex = lambda now: [{"job": "backup-system", "next_due": NOW + 100}, {"job": "idle", "next_due": None}]          # noqa: E731
    assert chk({"kind": "scheduled", "job": "backup-system"}, explain=ex).ok is True
    assert chk({"kind": "scheduled", "job": "idle"}, explain=ex).ok is False
    assert chk({"kind": "scheduled", "job": "unknown"}, explain=ex).ok is False
    assert chk({"kind": "scheduled", "job": "x"}, explain=lambda now: (_ for _ in ()).throw(ImportError("no scheduler"))).ok is False
    recs = history_of(hist("os_jobs", ["ok"] * 12))
    spec = {"kind": "os_job", "name": "fstrim", "green": 8, "min_hours": 48, "fresh_h": 2}
    assert chk(spec, os_names=lambda: ["fstrim"], history=recs).ok is True
    miss = chk(spec, os_names=lambda: ["logrotate"], history=recs)
    assert miss.ok is False and "extra_jobs" in miss.detail
    assert chk({"kind": "manual", "note": "look at it"}).ok is None
    assert chk({"kind": "none", "note": "inert"}).ok is True
    assert not Parity([Check("manual", "m", None, "")]).ok and not Parity([]).ok
    assert Parity([Check("a", "a", True, ""), Check("b", "b", True, "")]).ok


def test_a_crashing_check_is_a_failed_check_never_a_pass():
    boom = lambda *a, **k: (_ for _ in ()).throw(KeyError("x"))                                            # noqa: E731
    for spec in ({"kind": "config", "key": "a"}, {"kind": "command", "argv": ["x"]}, {"kind": "notify"}):
        res = chk(spec, config=boom, run=boom, notifications=boom)
        assert res.ok is False and "check failed" in res.detail


# =========================================================================== status / export
def test_status_rows_show_blocked_soaking_drift_and_manual(rig):
    r = rig([item("a", soak_days=4), item("b", wave=4, depends_on=["a"]),
             item("m", mode="adapter", actions=[{"do": "manual", "note": "edit the compose file"}]),
             {"name": "k", "kind": "unit", "location": "x.service", "mode": "keep", "wave": 0, "unmonitored": "n/a"}])
    for u in ("a.timer", "b.timer"):
        r.sys.add_unit(u)
    rows = {x["name"]: x for x in r.m.rows()}
    assert rows["b"]["blocked_by"] == ["a"] and rows["a"]["state"] == "pending" and rows["m"]["manual"] == 1 and rows["m"]["retirable"] is False
    assert rows["a"]["satisfied"] == "0/1" and rows["k"]["state"] == "pending"
    r.m.cutover("a", apply=True)
    r.now += 1 * 86400
    rows = {x["name"]: x for x in r.m.rows()}
    assert rows["a"]["state"] == "retired" and rows["a"]["soak_left_d"] == 3.0 and rows["b"]["blocked_by"] == ["a"]
    r.sys.units[("system", "a.timer")]["UnitFileState"] = "enabled"                       # somebody re-enabled it behind our back
    assert {x["name"]: x for x in r.m.rows()}["a"]["drift"] is True
    assert "DRIFT" in legacy.render_status(r.m.rows())


def test_a_thing_retired_by_hand_is_reported_as_such(rig):
    r = rig([item("h")])
    r.sys.add_unit("h.timer", enabled="disabled", active="inactive")
    assert {x["name"]: x for x in r.m.rows()}["h"]["state"] == "retired-by-hand"


def test_adopt_observe_and_keep_items_without_touching_anything(rig):
    r = rig([{"name": "k", "kind": "unit", "location": "x.service", "mode": "keep", "wave": 0, "unmonitored": "inert"}])
    out = r.m.cutover("k", apply=True)
    assert out.ok and r.state()["items"]["k"]["state"] == "adopted" and r.sys.calls == []
    assert "already adopted" in "\n".join(r.m.cutover("k", apply=True).lines)


def test_export_counts_next_and_remaining(rig):
    r = rig([item("a"), item("b", wave=4, depends_on=["a"]), {"name": "k", "kind": "unit", "location": "x", "mode": "keep", "wave": 0, "unmonitored": "n"}])
    r.sys.add_unit("a.timer")
    r.sys.add_unit("b.timer")
    e = r.m.export()
    assert (e["total"], e["retirable"], e["retired"], e["next"], e["complete"]) == (3, 2, 0, "a", False) and e["remaining"] == ["a", "b"]
    r.m.cutover("a", apply=True)
    e = r.m.export()
    assert e["retired"] == 1 and e["remaining"] == ["b"] and e["next"] == "b"             # a has no soak, so b is free to go
    assert json.dumps(e) and not any("text" in str(x) for x in e["items"])


def test_export_next_is_none_when_complete_and_has_no_secrets(rig):
    r = rig([item("a")])
    r.sys.add_unit("a.timer")
    r.m.cutover("a", apply=True)
    e = r.m.export()
    assert e["complete"] is True and e["next"] is None and e["remaining"] == []


# =========================================================================== runbook + docs stay in sync with the inventory
def test_runbook_lists_every_item_with_commands_and_rules():
    inv = real()
    md = legacy.runbook(inv)
    for it in inv.items:
        assert f"`{it.name}`" in md
    assert md.index("### Wave 0") < md.index("### Wave 3") < md.index("### Wave 5") < md.index("### Wave 6")
    assert "`systemctl disable --now backup-system.timer`" in md
    assert f"`mv /usr/local/sbin/notebook-db-alert.sh {LEG}/notebook-db-alert/notebook-db-alert.sh`" in md
    assert "`systemctl --user disable --now stack-backup.timer   # as ohmz`" in md
    assert "Do not do this until: `backup-system` is cut over, the tick has recorded 2 green run(s) of its job and it has soaked 14 days (counted from its first green run)" in md
    assert "`homelab-maint migrate cutover backup-system`" in md and "while running: `backup-system.service`, job `backup-system` under the tick, any held `/run/lock/backup-*.lock`" in md
    assert "MANUAL: comment out the DIUN_NOTIF_MAIL_*" in md and "/home/ohmz/docker-container-data/diun/diun.env" in md
    assert "Already retired by hand on 2026-10-01" in md


def test_migration_doc_embeds_the_current_generated_runbook():
    doc = (ROOT / "docs" / "MIGRATION.md").read_text()
    b, e = "<!-- BEGIN GENERATED RUNBOOK -->", "<!-- END GENERATED RUNBOOK -->"
    assert b in doc and e in doc
    assert doc.split(b)[1].split(e)[0].strip() == legacy.runbook(real()).strip(), "run: python3 -m homelab_maint.legacy runbook --write docs/MIGRATION.md"


def test_update_doc_replaces_only_the_generated_block(tmp_path):
    d = tmp_path / "M.md"
    d.write_text("intro\n<!-- BEGIN GENERATED RUNBOOK -->\nSTALE-BLOCK\n<!-- END GENERATED RUNBOOK -->\noutro\n")
    assert legacy.update_doc(d, real()) is True and legacy.update_doc(d, real()) is False
    t = d.read_text()
    assert t.startswith("intro\n") and t.endswith("outro\n") and "STALE-BLOCK" not in t and "### Wave 0" in t


# =========================================================================== the whole real inventory against a fake world
def build_world(rig_, inv):
    """Create, for every item, the legacy state its actions would retire (enabled timers, scripts, cron lines)."""
    crons: dict[str, list[str]] = {}
    for it in inv.items:
        for u in it.require_idle:
            rig_.sys.add_unit(u, scope=it.scope, enabled="static", active="inactive")
        for a in it.actions:
            if a["do"] == "disable":
                rig_.sys.add_unit(a["unit"], scope=a.get("scope") or it.scope)
            elif a["do"] == "move":
                rig_.put(a["src"], f"#!/bin/sh\n# {it.name}\n", 0o640 if "systemd" in a["src"] else 0o750)
            elif a["do"] == "cron_comment":
                line = f"5 4 * * 1 /opt/job --x {a['match']}" if a.get("match") else f"5 4 * * 1 /opt/job # {a['tag']}"
                crons.setdefault(a["user"], []).append(("# " + line) if it.pre_retired else line)
    for user, lines in crons.items():
        rig_.sys.cron[user] = "# header\nSHELL=/bin/sh\n" + "\n".join(lines) + "\n0 1 * * * /usr/bin/unrelated\n"


def world_snapshot(rig_):
    files = {}
    for p in sorted(rig_.root.rglob("*")):
        if p.is_file() or p.is_symlink():
            rel = str(p.relative_to(rig_.root))
            if "/legacy/" in rel:
                continue
            files[rel] = (hashlib.sha256(p.read_bytes()).hexdigest(), stat.S_IMODE(p.lstat().st_mode))
    return {"units": {k: dict(v) for k, v in rig_.sys.units.items()}, "cron": dict(rig_.sys.cron), "files": files,
            "job_overrides": dict(rig_.jobs.overrides)}


def test_every_retirable_item_of_the_real_inventory_cuts_over_and_rolls_back_exactly(tmp_path):
    inv = real()
    rig_ = Rig(tmp_path, inv=inv)
    build_world(rig_, inv)
    before = world_snapshot(rig_)
    order = [i for i in order_items(inv.items) if i.retirable]
    assert len(order) >= 20
    for it in order:
        rig_.now += 30 * 86400                                           # soaks elapse; parity is overridden, the rest is the real flow
        out = rig_.m.cutover(it.name, apply=True, force=True, reason="test world: parity is not under test here")
        assert out.ok, (it.name, out.lines)
        rows = {r["name"]: r for r in rig_.m.rows()}
        assert rows[it.name]["state"] == "retired" and "drift" not in rows[it.name], it.name
    after = world_snapshot(rig_)
    assert after != before, "something must have been retired"
    assert not any(c[0] == "systemctl" and c[1] in ("start", "enable") for c in rig_.sys.calls)
    ov = dict(rig_.jobs.overrides)
    assert ov["backup-system"] == "managed" and ov["stack-backup"] == "managed" and "tier-check" not in ov, \
        "the check tier stays on its own timer: no mode flip, and its timer is never disabled"
    assert ["systemctl", "disable", "--now", "homelab-maint-check.timer"] not in rig_.sys.calls
    assert ov["docker-prune"] == "retired" and ov["mem-guard"] == "retired" and ov["notebook-db-alert"] == "retired"
    # nothing was deleted: every moved script exists under the legacy root
    legacy_files = [p for p in (rig_.root / LEG.lstrip("/")).rglob("*") if p.is_file() and p.name != "README.md"]
    assert len(legacy_files) >= 9
    for it in reversed(order):
        out = rig_.m.rollback(it.name, apply=True)
        pre = it.pre_retired
        assert out.ok or pre, (it.name, out.lines)
    final = world_snapshot(rig_)
    assert final["units"] == before["units"], "exact unit enablement/activity state"
    assert final["cron"] == before["cron"], "crontabs byte for byte"
    assert final["files"] == before["files"], "script locations, contents and modes"
    assert final["job_overrides"] == before["job_overrides"] == {}, "the scheduler's per-job modes are back to what they were"
    states = {n: r["state"] for n, r in rig_.state()["items"].items()}
    assert all(s == "rolled_back" for n, s in states.items() if not inv.get(n).pre_retired)


def test_pre_retired_cron_items_are_retired_and_monitored_for_drift(tmp_path):
    inv = real()
    rig_ = Rig(tmp_path, inv=inv)
    build_world(rig_, inv)
    rows = {r["name"]: r for r in rig_.m.rows()}
    for n in ("cron-root-journal-vacuum", "cron-root-find-tmp", "cron-root-logrotate", "cron-root-old-disabled"):
        assert rows[n]["state"] == "retired" and "drift" not in rows[n], n
    rig_.sys.cron["root"] = rig_.sys.cron["root"].replace("# 5 4 * * 1 /opt/job --x journalctl --vacuum-time=7d", "5 4 * * 1 /opt/job --x journalctl --vacuum-time=7d")
    assert {r["name"]: r for r in rig_.m.rows()}["cron-root-journal-vacuum"].get("drift") is True


def test_two_items_sharing_one_crontab_do_not_disturb_each_other(tmp_path):
    inv = real()
    rig_ = Rig(tmp_path, inv=inv)
    build_world(rig_, inv)
    ohmz0 = rig_.sys.cron["ohmz"]
    assert rig_.m.cutover("notify-route", apply=True, force=True, reason="adopt for the test").ok
    rig_.now += 30 * 86400
    assert rig_.m.cutover("cron-bazarr-subtitle-rules", apply=True, force=True, reason="test world: cutover").ok
    mid = rig_.sys.cron["ohmz"]
    assert rig_.m.cutover("cron-tunarr-weekly-sync", apply=True, force=True, reason="test world: cutover").ok
    both = rig_.sys.cron["ohmz"]
    assert both.count("#HM-RETIRED[") == 2 and mid.count("#HM-RETIRED[") == 1
    assert rig_.m.rollback("cron-bazarr-subtitle-rules", apply=True).ok
    after = rig_.sys.cron["ohmz"]
    assert after.count("#HM-RETIRED[") == 1 and "#HM-RETIRED[cron-tunarr-weekly-sync]" in after
    assert rig_.m.rollback("cron-tunarr-weekly-sync", apply=True).ok and rig_.sys.cron["ohmz"] == ohmz0


# =========================================================================== CLI
def run_cli(rig_, *argv):
    import io
    from contextlib import redirect_stdout, redirect_stderr
    o, e = io.StringIO(), io.StringIO()
    with redirect_stdout(o), redirect_stderr(e):
        rc = legacy.main(list(argv), migrator=rig_.m)
    return rc, o.getvalue(), e.getvalue()


def test_cli_dry_run_apply_and_exit_codes(rig):
    r = rig([timer_item()])
    r.sys.add_unit("nb.timer")
    rc, out, _ = run_cli(r, "cutover", "nb")
    assert rc == 0 and "DRY RUN" in out and "$ systemctl disable --now nb.timer" in out and r.sys.calls == []
    rc, out, _ = run_cli(r, "cutover", "nb", "--apply")
    assert rc == 0 and "retired" in out and r.sys.state("nb.timer")["UnitFileState"] == "disabled"
    rc, out, _ = run_cli(r, "status", "--fast")
    assert rc == 0 and "nb" in out and "retired" in out and "1/1 retirable items retired" in out
    rc, out, _ = run_cli(r, "rollback", "nb")
    assert rc == 0 and "$ systemctl enable nb.timer" in out and r.sys.state("nb.timer")["UnitFileState"] == "disabled"
    rc, out, _ = run_cli(r, "rollback", "nb", "--apply")
    assert rc == 0 and r.sys.state("nb.timer")["UnitFileState"] == "enabled"
    rc, out, err = run_cli(r, "cutover", "nope")
    assert rc == 2 and "unknown item" in err
    rc, out, _ = run_cli(r, "journal", "-n", "5")
    assert rc == 0 and "Legacy cutover" in out and "Legacy rollback" in out


def test_cli_check_plan_export_and_force(rig):
    r = rig([item("g", **red_parity())], config=lambda: {"tasks": {"x": {"mode": "report"}}})
    r.sys.add_unit("g.timer")
    rc, out, _ = run_cli(r, "check", "g")
    assert rc == 1 and "NOT green" in out
    rc, out, _ = run_cli(r, "cutover", "g", "--apply")
    assert rc == 1 and "REFUSED" in out
    rc, out, _ = run_cli(r, "cutover", "g", "--apply", "--force")
    assert rc == 1 and "--reason" in out
    rc, out, _ = run_cli(r, "plan", "g")
    assert rc == 0 and "$ systemctl disable --now g.timer" in out
    rc, out, _ = run_cli(r, "cutover", "g", "--apply", "--force", "--reason", "owner decision 2026-10-02")
    assert rc == 0
    rc, out, _ = run_cli(r, "export")
    assert json.loads(out)["retired"] == 1


def test_cli_validate_and_runbook_with_the_real_inventory(capsys):
    assert legacy.main(["validate"]) == 0
    assert "inventory ok" in capsys.readouterr().out
    assert legacy.main(["runbook"]) == 0
    assert "### Wave 5" in capsys.readouterr().out


def test_cli_reports_an_unusable_inventory(tmp_path, monkeypatch, capsys):
    bad_file = tmp_path / "x.toml"
    bad_file.write_text('[[item]]\nname = "x"\nmode = "nope"\n')
    monkeypatch.setattr(legacy, "inventory_path", lambda: (bad_file, False))
    assert legacy.main(["validate"]) == 2
    assert "inventory error" in capsys.readouterr().err


# =========================================================================== the scheduler hand-over (jobs.py job modes)
def job_item(name="jb", mode="adapter", **kw):
    kw.setdefault("job", "jb-job")
    return item(name, mode=mode, **kw)


def test_job_link_appends_the_scheduler_mode_flip_as_the_last_action():
    inv = parse_inventory({"item": [job_item(), job_item("p", mode="port", job="p-job"), item("n", job="n-job", job_mode="observe")]})
    assert inv.get("jb").actions[-1] == {"do": "job_mode", "job": "jb-job", "mode": "managed"}
    assert inv.get("p").actions[-1] == {"do": "job_mode", "job": "p-job", "mode": "retired"}
    assert inv.get("n").actions[-1]["mode"] == "observe" and inv.get("jb").retirable
    assert "job" in bad([{"name": "o", "kind": "unit", "location": "x", "mode": "keep", "wave": 0, "unmonitored": "n", "job": "x"}])
    assert "job_mode needs" in bad([item("x", actions=[{"do": "disable", "unit": "x.timer"}, {"do": "job_mode", "job": "Bad Name", "mode": "managed"}])])
    assert "job_mode needs" in bad([item("x", actions=[{"do": "disable", "unit": "x.timer"}, {"do": "job_mode", "job": "ok", "mode": "turbo"}])])


def test_cutover_disables_the_legacy_driver_first_and_hands_the_job_to_the_tick_last(rig):
    r = rig([job_item()])
    r.sys.add_unit("jb.timer")
    order = []
    r.sys.hook = lambda argv: order.append(("systemctl", argv[1])) and None
    orig = r.jobs.set
    r.jobs.set = lambda n, m: (order.append(("job-mode", m)), orig(n, m))[1]
    assert r.m.cutover("jb", apply=True).ok
    assert order.index(("systemctl", "disable")) < order.index(("job-mode", "managed"))
    assert r.jobs.overrides == {"jb-job": "managed"} and r.state()["items"]["jb"]["actions"][-1]["pre"] == {"shipped": "observe", "override": None, "effective": "observe"}
    out = r.m.cutover("jb")                                                   # dry run of an already retired item
    assert "already retired" in "\n".join(out.lines)


def test_rollback_takes_the_job_back_from_the_tick_before_the_legacy_driver_returns(rig):
    r = rig([job_item()])
    r.sys.add_unit("jb.timer")
    assert r.m.cutover("jb", apply=True).ok
    order = []
    r.sys.hook = lambda argv: order.append(("systemctl", argv[1])) and None
    orig = r.jobs.set
    r.jobs.set = lambda n, m: (order.append(("job-mode", m)), orig(n, m))[1]
    assert r.m.rollback("jb", apply=True).ok
    assert order[0] == ("job-mode", None) and ("systemctl", "enable") in order and r.jobs.overrides == {}


def test_a_preexisting_job_mode_override_is_restored_exactly(rig):
    r = rig([job_item()])
    r.sys.add_unit("jb.timer")
    r.jobs.overrides["jb-job"] = "retired"                                     # the owner had parked the job
    assert r.m.cutover("jb", apply=True).ok and r.jobs.overrides["jb-job"] == "managed"
    assert r.m.rollback("jb", apply=True).ok and r.jobs.overrides == {"jb-job": "retired"}


def test_a_job_already_in_the_wanted_mode_is_left_alone(rig):
    r = rig([job_item()])
    r.sys.add_unit("jb.timer")
    r.jobs.known_modes["jb-job"] = "managed"                                   # jobs.toml already ships it managed
    assert r.m.cutover("jb", apply=True).ok and r.jobs.calls == []
    assert r.m.rollback("jb", apply=True).ok and r.jobs.calls == []


def test_an_undefined_job_refuses_the_whole_cutover_before_anything_changes(rig):
    r = rig([job_item()])
    r.sys.add_unit("jb.timer")
    r.jobs.known_modes.clear()
    out = r.m.cutover("jb", apply=True)
    assert out.refused and "not defined in jobs.toml" in out.refused and r.sys.calls == [] and r.jobs.calls == []
    assert r.sys.state("jb.timer")["UnitFileState"] == "enabled"


def test_a_failing_job_mode_write_undoes_the_disable(rig):
    r = rig([job_item()])
    r.sys.add_unit("jb.timer")
    r.jobs.set = lambda n, m: (_ for _ in ()).throw(OSError(errno.EROFS, "read-only file system"))
    out = r.m.cutover("jb", apply=True)
    assert out.rc == 4 and r.sys.state("jb.timer")["UnitFileState"] == "enabled" and r.state()["items"]["jb"]["state"] == "failed"


def test_job_mode_is_part_of_the_dry_run_and_the_runbook(rig):
    r = rig([job_item()])
    r.sys.add_unit("jb.timer")
    out = r.m.cutover("jb")
    assert "$ homelab-maint job mode jb-job managed" in "\n".join(out.lines) and r.jobs.calls == []
    md = legacy.runbook(r.inv)
    assert "`homelab-maint job mode jb-job managed`" in md and "`homelab-maint job mode jb-job reset" in md


# --------------------------------------------------------------------------- the two new parity kinds
def test_scheduler_health_parity():
    for st, ok in (("ok", True), ("warn", False), ("crit", False)):
        assert chk({"kind": "scheduler_health"}, health=lambda now, st=st: (st, "msg")).ok is ok
    assert chk({"kind": "scheduler_health"}, health=lambda now: (_ for _ in ()).throw(ImportError("x"))).ok is False


def test_job_attr_parity_reads_the_effective_job_configuration():
    attrs = {("backup-system", "self_notifies"): False, ("backup-system", "notify.on_failure"): "alert", ("backup-immich", "self_notifies"): True}
    get = lambda job, attr: attrs[(job, attr)]                                                    # noqa: E731
    assert chk({"kind": "job_attr", "job": "backup-system", "attr": "self_notifies", "equals": False}, job_attr=get).ok is True
    assert chk({"kind": "job_attr", "job": "backup-immich", "attr": "self_notifies", "equals": False}, job_attr=get).ok is False
    assert chk({"kind": "job_attr", "job": "backup-system", "attr": "notify.on_failure", "equals": "alert"}, job_attr=get).ok is True
    assert chk({"kind": "job_attr", "job": "nope", "attr": "x", "equals": 1}, job_attr=get).ok is False       # KeyError => not green
    assert "exactly one of equals / contains" in bad([item("x", parity_check=[{"kind": "job_attr", "job": "a", "attr": "b"}])])


def test_the_default_jobs_api_reads_the_real_jobs_module(tmp_path, monkeypatch):
    from homelab_maint import jobs
    toml = tmp_path / "jobs.toml"
    toml.write_text('[[job]]\nname = "demo-job"\ncommand = ["/usr/bin/true"]\nuser = "root"\nschedule = "0 4 * * *"\nmode = "observe"\nself_notifies = true\n'
                    'retire = ["system:demo.timer"]\n[job.notify]\non_failure = "alert"\n')
    monkeypatch.setattr(core, "CONF_DIR", tmp_path)
    api = legacy.JobsApi()
    assert api.known() == {"demo-job": "observe"} and api.override("demo-job") is None
    assert api.attr("demo-job", "self_notifies") is True and api.attr("demo-job", "notify.on_failure") == "alert"
    api.set("demo-job", "managed")
    assert api.override("demo-job") == "managed" and jobs.load_modes() == {"demo-job": "managed"}
    api.set("demo-job", None)
    assert api.override("demo-job") is None


# =========================================================================== the inventory agrees with the other streams' files
def real_jobs():
    from homelab_maint import jobs
    cfg = jobs.load(ROOT / "etc" / "jobs.toml", apply_modes=False)
    assert cfg.errors == [], cfg.errors
    return cfg.jobs


def retire_refs(it):
    """The scheduler's `retire` interlock refs this item's actions correspond to."""
    out = set()
    for a in it.actions:
        if a["do"] == "disable":
            sc = a.get("scope") or it.scope
            out.add(f"system:{a['unit']}" if sc == "system" else f"user:{sc.split(':', 1)[1]}:{a['unit']}")
        elif a["do"] == "cron_comment" and a.get("tag"):
            out.add(f"cron:{a['user']}:{a['tag']}")
    return out


def test_every_linked_job_exists_and_its_interlock_names_exactly_what_the_cutover_disables():
    jobs_ = real_jobs()
    linked = [i for i in real().items if i.job]
    assert len(linked) >= 17
    for it in linked:
        assert it.job in jobs_, f"{it.name}: job {it.job} is not in etc/jobs.toml"
        assert set(jobs_[it.job].retire) == retire_refs(it), f"{it.name}: jobs.toml retire {jobs_[it.job].retire} != cutover actions {retire_refs(it)}"
        want = "retired" if it.mode == "retire" else "observe"           # mem-guard ships retired: the cutover then has no mode flip to do
        assert jobs_[it.job].mode in (want, "observe"), "everything ships in observe mode (or already retired): the cutover is what moves it"


def test_every_job_that_has_a_retire_interlock_has_a_migration_item():
    items = [i for i in real().items if i.job or i.keeps_job]
    covered = {i.job or i.keeps_job for i in items}
    for name, j in real_jobs().items():
        if j.retire:
            assert name in covered, f"job {name} (retire {j.retire}) has no item in legacy-retirement.toml"
    kept = {i.keeps_job for i in real().items if i.keeps_job}
    assert kept == {"tier-check"}, "the check tier is the only job whose legacy timer is deliberately kept"


def test_backup_cutover_ordering_matches_the_jobs_toml_safety_notes():
    jobs_, by = real_jobs(), {i.name: i for i in real().items}
    for n in ("backup-system", "backup-immich"):
        assert jobs_[n].self_notifies is True, "shipped true: the script keeps notifying until the wrapper item flips it"
        assert "/usr/local/sbin/backup-failed.sh" in jobs_[n].hooks["on_failure"]
        assert jobs_[n].notify.get("on_failure") == "alert"
    wrapper, hook = by["backup-notify-wrapper"], by["backup-failure-hook"]
    needs = {(p["job"], p["attr"], p["equals"]) for p in wrapper.parity if p["kind"] == "job_attr"}
    assert {("backup-system", "self_notifies", False), ("backup-immich", "self_notifies", False),
            ("backup-system", "notify.on_failure", "alert"), ("backup-immich", "notify.on_failure", "alert")} <= needs
    assert wrapper.name in hook.depends_on and by["tier-daily"].name in by["backup-system"].depends_on
    # the failure hook keeps a stub at the old path because jobs.toml still names it in hooks.on_failure
    assert any(a["do"] == "stub" and a["path"] == "/usr/local/sbin/backup-failed.sh" for a in hook.actions)


def test_probe_names_in_the_inventory_exist_in_probes_toml():
    from homelab_maint import probes
    doc = tomllib.load(open(ROOT / "etc" / "probes.toml", "rb"))
    _d, plist, errs = probes.parse(doc)
    names = {p.name for p in plist}
    assert errs == [] and len(names) > 50
    for it in real().items:
        for p in it.parity:
            if p["kind"] == "probe":
                for n in legacy._strs(p.get("names") or p.get("name")):
                    assert n in names, f"{it.name}: probe {n} is not in etc/probes.toml"
        if it.via == "probe":
            for n in it.replaced_by:
                assert n in names, f"{it.name}: replaced_by probe {n} is not in etc/probes.toml"


def test_os_job_replacements_exist_in_the_os_jobs_task():
    from homelab_maint.tasks import native
    rows = {j["name"] for j in native.DEFAULT_OS_JOBS}
    for it in real().items:
        if it.via == "os_job":
            for n in it.replaced_by:
                assert n in rows, f"{it.name}: os_jobs has no row {n}"


def test_pause_on_rollback_names_real_mutating_tasks():
    from homelab_maint import cli
    cli.load_tasks()
    for it in real().items:
        for n in it.pause_on_rollback:
            assert n in core.REGISTRY and core.REGISTRY[n].klass in ("C1", "C2"), f"{it.name}: {n}"
    assert {n for it in real().items for n in it.pause_on_rollback} >= {"docker_cache", "docker_images", "docker_containers_prune", "comfyui_idle_reclaim", "immich_recycle"}


# =========================================================================== what the audit of the real host added
def test_an_unreadable_file_is_a_refused_step_not_a_traceback(rig, monkeypatch):
    """backup-notify.sh is 0750 root: a non-root `migrate status` used to crash on it. Now it is a refusal that says why."""
    r = rig([item("sc", actions=[{"do": "disable", "unit": "sc.timer"}, {"do": "move", "src": "/usr/local/sbin/sc.sh"}])])
    r.sys.add_unit("sc.timer")
    r.put("/usr/local/sbin/sc.sh")
    monkeypatch.setattr(legacy, "sha256_file", lambda p: (_ for _ in ()).throw(PermissionError(13, "denied")))
    steps = r.m.plan_steps(r.inv.get("sc"))
    assert [s.state for s in steps] == ["todo", "refused"] and "permission denied" in steps[1].note and "as root" in steps[1].note
    rows = r.m.rows()                                                                 # must not raise either
    assert rows[0]["name"] == "sc"
    out = r.m.cutover("sc", apply=True)
    assert out.refused and r.sys.calls == [] and r.exists("/usr/local/sbin/sc.sh")


def test_retired_targets_are_what_a_reinstall_must_not_undo(rig):
    r = rig([item("t1", actions=[{"do": "disable", "unit": "t1.timer"}, {"do": "move", "src": "/usr/local/sbin/t1.sh"}]),
             item("d1", mode="retire", actions=[{"do": "move", "src": "/etc/systemd/system/x.service.d/10-gate.conf", "reload": "system"}]),
             item("u1", actions=[{"do": "disable", "unit": "u1.timer", "scope": "user:ohmz"}]),
             item("c1", mode="retire", pre_retired="2026-10-01", actions=[{"do": "cron_comment", "user": "root", "match": "journalctl --vacuum"}])])
    r.sys.add_unit("t1.timer")
    r.sys.add_unit("u1.timer", scope="user:ohmz")
    r.put("/usr/local/sbin/t1.sh")
    r.put("/etc/systemd/system/x.service.d/10-gate.conf", "[Service]\n", 0o644)
    assert r.m.retired_targets() == []                                                # nothing recorded yet; a pre-retired cron line has no unit
    for n in ("t1", "d1", "u1"):
        assert r.m.cutover(n, apply=True).ok
    got = {(t["kind"], t["scope"], t["ref"], t["item"]) for t in r.m.retired_targets()}
    assert got == {("unit", "system", "t1.timer", "t1"), ("path", "system", "/usr/local/sbin/t1.sh", "t1"),
                   ("path", "system", "/etc/systemd/system/x.service.d/10-gate.conf", "d1"), ("unit", "user:ohmz", "u1.timer", "u1")}
    assert r.m.rollback("t1", apply=True).ok
    assert {t["item"] for t in r.m.retired_targets()} == {"d1", "u1"}                 # a rolled-back item is no longer something to protect


def test_cli_retired_prints_one_ref_per_line_for_install_sh(rig, capsys):
    r = rig([item("t1", actions=[{"do": "disable", "unit": "t1.timer"}]), item("u1", actions=[{"do": "disable", "unit": "u1.timer", "scope": "user:ohmz"}]),
             item("d1", mode="retire", actions=[{"do": "move", "src": "/etc/systemd/system/x.service.d/10-gate.conf"}])])
    r.sys.add_unit("t1.timer")
    r.sys.add_unit("u1.timer", scope="user:ohmz")
    r.put("/etc/systemd/system/x.service.d/10-gate.conf", "[Service]\n", 0o644)
    for n in ("t1", "u1", "d1"):
        r.m.cutover(n, apply=True)
    capsys.readouterr()
    assert legacy.main(["retired", "--kind", "unit"], migrator=r.m) == 0
    assert capsys.readouterr().out == "t1.timer\n"                                    # system scope only: install.sh does not manage user units
    assert legacy.main(["retired", "--kind", "path"], migrator=r.m) == 0
    assert capsys.readouterr().out == "/etc/systemd/system/x.service.d/10-gate.conf\n"
    assert legacy.main(["retired"], migrator=r.m) == 0
    assert "unit\tuser:ohmz\tu1.timer\tu1" in capsys.readouterr().out


def test_cli_apply_needs_root_but_a_dry_run_does_not(monkeypatch, capsys):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert legacy.main(["cutover", "backup-system", "--apply"]) == 2
    assert "run it as root" in capsys.readouterr().err
    assert legacy.main(["rollback", "backup-system", "--apply"]) == 2
    capsys.readouterr()
    assert legacy.main(["cutover", "no-such-item"]) == 2                             # a dry run is allowed: it fails on the name instead
    assert "unknown item" in capsys.readouterr().err


def test_export_public_never_raises_and_carries_no_script_paths_or_commands(tmp_path, monkeypatch):
    e = legacy.export_public(NOW)
    assert e and e["generated_at"] == NOW and e["total"] >= 60 and e["retirable"] >= 20 and e["items"] and e["complete"] is False
    blob = json.dumps(e)
    for leak in ("/usr/local", "/home/", "/var/", ".env", "password", "token", "argv", "systemctl", "crontab -"):
        assert leak not in blob, leak
    bad_file = tmp_path / "bad.toml"
    bad_file.write_text("[[item]]\nname = 1\n")
    monkeypatch.setattr(legacy, "inventory_path", lambda: (bad_file, False))
    assert legacy.export_public(NOW) is None


def test_every_external_unit_of_jobs_toml_is_accounted_for_in_the_inventory():
    text = REAL_TOML.read_text()
    ext = tomllib.load(open(ROOT / "etc" / "jobs.toml", "rb")).get("external", [])
    assert len(ext) >= 20
    for e in ext:
        needle = (e.get("unit") or e["name"]).split(" ")[-1].rsplit("/", 1)[-1]      # "smartd -> /usr/local/sbin/smart-alert.sh" -> smart-alert.sh
        assert needle in text, f"external {e['name']} ({needle}) is not in legacy-retirement.toml"


def test_the_umbrellas_own_services_and_the_stray_daemons_are_inventoried_and_never_retired():
    by = {i.name: i for i in real().items}
    for n in ("umbrella-services", "other-services", "platform-daemons", "dashboards-and-sensors"):
        assert by[n].mode == "keep" and by[n].actions == [] and not by[n].retirable
    assert "homelab-maint-www.service" in by["umbrella-services"].location and "homelab-maint-tick.timer" in by["umbrella-services"].location
    assert "fancontrol" in by["other-services"].location and "teamviewerd" in by["other-services"].location
    text = REAL_TOML.read_text()
    for u in ("systemd-sysupdate.timer", "x11vnc.service", "lucebox-dflash"):
        assert u in text


def test_the_backup_wrappers_never_move_under_a_running_backup():
    by = {i.name: i for i in real().items}
    for n in ("backup-notify-wrapper", "backup-failure-hook"):
        assert {"backup-system.service", "backup-immich.service"} <= set(by[n].require_idle), n


def test_rollback_warns_that_a_persistent_timer_may_catch_up_at_once(rig):
    r = rig([item("pt", actions=[{"do": "disable", "unit": "pt.timer"}]), item("np", actions=[{"do": "disable", "unit": "np.timer"}])])
    r.sys.add_unit("pt.timer")
    r.sys.add_unit("np.timer")
    r.sys.persistent.add(("system", "pt.timer"))
    assert r.m.cutover("pt", apply=True).ok and r.m.cutover("np", apply=True).ok
    assert r.state()["items"]["pt"]["actions"][0]["pre"]["persistent"] is True
    dry = "\n".join(r.m.rollback("pt").lines)                                      # dry run says so too
    assert "pt.timer is Persistent=yes" in dry and "missed" in dry
    assert "Persistent" not in "\n".join(r.m.rollback("np").lines)
    out = r.m.rollback("pt", apply=True)
    assert out.ok and "Persistent=yes" in "\n".join(out.lines) and r.sys.state("pt.timer")["UnitFileState"] == "enabled"


# =========================================================================== audit: nothing may schedule itself outside the inventory
class AuditHost:
    """list-unit-files / crontab -l as dicts, plus real cron.* directories under a tmp root."""

    def __init__(self, tmp_path, timers=(), user_timers=(), cron=None, files=None, fail=()):
        self.timers, self.user_timers, self.cron, self.fail = list(timers), list(user_timers), dict(cron or {}), set(fail)
        self.root = tmp_path / "auditroot"
        self.root.mkdir(exist_ok=True)
        for d, names in (files or {}).items():
            (self.root / d.lstrip("/")).mkdir(parents=True, exist_ok=True)
            for n in names:
                (self.root / d.lstrip("/") / n).write_text("x")
        self.host = Host(run=self, root=self.root, uids={"ohmz": 1000})

    def __call__(self, argv, input_=None, timeout=60):
        if argv[0] == "crontab":
            user = argv[argv.index("-u") + 1]
            if ("cron", user) in self.fail:
                return cp(argv, 1, "", "must be privileged")
            return cp(argv, 0, self.cron[user]) if user in self.cron else cp(argv, 1, "", f"no crontab for {user}")
        scope = "user" if argv[0] == "runuser" else "system"
        if (scope, "list") in self.fail:
            return cp(argv, 1, "", "boom")
        assert "list-unit-files" in argv and "--type=timer" in argv, argv
        names = self.user_timers if scope == "user" else self.timers
        return cp(argv, 0, "".join(f"{n} enabled enabled\n" for n in names))


AUDIT_ITEMS = [
    {"name": "os-x", "kind": "timer", "location": "apt-daily.timer, fstrim.timer", "mode": "observe", "wave": 0, "unmonitored": "n"},
    {"name": "u-x", "kind": "timer", "location": "snap.thing.notifier.timer (ohmz user manager)", "mode": "observe", "wave": 0, "scope": "user:ohmz", "unmonitored": "n"},
    {"name": "cr", "kind": "cron", "location": "ohmz crontab", "mode": "retire", "wave": 3, "parity_check": [{"kind": "none", "note": "t"}],
     "retire_actions": [{"do": "cron_comment", "user": "ohmz", "tag": "nightly-thing"}]},
    {"name": "shims", "kind": "script", "location": "/etc/cron.daily/{0anacron,apport,logrotate}, /etc/cron.d/{anacron,sysstat}", "mode": "observe", "wave": 0, "unmonitored": "n"},
]


def audit_of(tmp_path, **kw):
    ah = AuditHost(tmp_path, **kw)
    return legacy.audit(ah.host, parse_inventory({"item": AUDIT_ITEMS}), users=("ohmz",))


def test_audit_accepts_exactly_what_the_inventory_names(tmp_path):
    a = audit_of(tmp_path, timers=["apt-daily.timer", "fstrim.timer"], user_timers=["snap.thing.notifier.timer"],
                 cron={"root": "# only comments\nMAILTO=me\n", "ohmz": "30 2 * * * /x/y.sh # nightly-thing\n"},
                 files={"/etc/cron.daily": ["0anacron", "apport", ".placeholder"], "/etc/cron.d": ["anacron", "sysstat"]})
    assert a == {"timers": [], "cron": [], "files": [], "errors": [], "ok": True}


def test_audit_reports_a_new_timer_cron_line_or_cron_file_nobody_accounted_for(tmp_path):
    a = audit_of(tmp_path, timers=["apt-daily.timer", "sneaky.timer"], user_timers=["snap.thing.notifier.timer", "mine.timer"],
                 cron={"root": "0 3 * * * /usr/local/bin/new-cleanup.sh\n# 0 4 * * * commented.sh\nSHELL=/bin/sh\n",
                       "ohmz": "30 2 * * * /x/y.sh # nightly-thing\n5 5 * * * /home/ohmz/other.sh\n"},
                 files={"/etc/cron.daily": ["apport", "brand-new"], "/etc/cron.weekly": [".placeholder"]})
    assert a["ok"] is False
    assert a["timers"] == [{"scope": "system", "unit": "sneaky.timer"}, {"scope": "user:ohmz", "unit": "mine.timer"}]
    assert a["cron"] == [{"user": "root", "line": "0 3 * * * /usr/local/bin/new-cleanup.sh"}, {"user": "ohmz", "line": "5 5 * * * /home/ohmz/other.sh"}]
    assert a["files"] == ["/etc/cron.daily/brand-new"]


def test_audit_matches_whole_names_not_substrings(tmp_path):
    a = audit_of(tmp_path, timers=["daily.timer", "apt.timer", "sneaky-apt-daily.timer"])
    assert [t["unit"] for t in a["timers"]] == ["daily.timer", "apt.timer", "sneaky-apt-daily.timer"]


def test_audit_a_command_that_fails_is_reported_not_raised_and_not_ok(tmp_path):
    a = audit_of(tmp_path, timers=["apt-daily.timer"], fail={("user", "list"), ("cron", "root")})
    assert a["ok"] is False and len(a["errors"]) == 2 and any("user:ohmz" in e for e in a["errors"]) and any("root" in e for e in a["errors"])
    assert audit_of(tmp_path, timers=["apt-daily.timer"], cron={})["ok"] is True          # "no crontab for X" is an empty crontab, not an error


def test_audit_result_is_a_short_ascii_warning_or_ok(tmp_path, monkeypatch):
    ah = AuditHost(tmp_path, timers=["apt-daily.timer", "sneaky.timer"] + [f"extra{i}.timer" for i in range(20)])
    monkeypatch.setattr(legacy, "load_inventory", lambda *a, **k: parse_inventory({"item": AUDIT_ITEMS}))
    res = legacy.audit_result(ah.host)
    assert res.status == "warn" and res.alert is False and res.summary.isascii() and len(res.summary) <= 140 and "sneaky.timer" in res.summary
    assert res.metrics == {"unaccounted": 21} and len(res.items) == 12 and res.items[0]["kind"] == "timer"
    assert legacy.audit_result(AuditHost(tmp_path, timers=["apt-daily.timer"], cron={"ohmz": ""}).host).status == "ok"
    assert legacy.audit_result(AuditHost(tmp_path, fail={("system", "list")}).host).status == "info"
    monkeypatch.setattr(legacy, "load_inventory", lambda *a, **k: (_ for _ in ()).throw(InventoryError(["broken"])))
    bad = legacy.audit_result(ah.host)
    assert bad.status == "warn" and "could not run" in bad.summary


def test_cli_audit_prints_what_is_unaccounted_and_sets_the_exit_code(tmp_path, capsys):
    ah = AuditHost(tmp_path, timers=["apt-daily.timer", "sneaky.timer"], cron={"root": "1 1 * * * /new.sh\n"})
    m = Migrator(parse_inventory({"item": AUDIT_ITEMS}), ah.host, audit=lambda *a: None, notifier=lambda e: None, state_dir=tmp_path / "s", run_dir=tmp_path / "r")
    assert legacy.main(["audit"], migrator=m) == 1
    out = capsys.readouterr().out
    assert "UNACCOUNTED timer  system     sneaky.timer" in out and "UNACCOUNTED cron   root       1 1 * * * /new.sh" in out and "add an [[item]]" in out
    assert legacy.main(["audit", "--json"], migrator=m) == 1 and json.loads(capsys.readouterr().out)["timers"][0]["unit"] == "sneaky.timer"
    (tmp_path / "ok").mkdir()
    ok = AuditHost(tmp_path / "ok", timers=["apt-daily.timer"])
    m2 = Migrator(parse_inventory({"item": AUDIT_ITEMS}), ok.host, audit=lambda *a: None, notifier=lambda e: None, state_dir=tmp_path / "s", run_dir=tmp_path / "r")
    assert legacy.main(["audit"], migrator=m2) == 0 and "everything on the host is in the inventory" in capsys.readouterr().out


def test_the_real_inventory_names_every_timer_the_scheduler_declares_external():
    known = legacy._corpus(real())
    ext = tomllib.load(open(ROOT / "etc" / "jobs.toml", "rb")).get("external", [])
    units = [e["unit"] for e in ext if str(e.get("unit", "")).endswith(".timer")]
    assert len(units) >= 15
    for u in units:
        assert u in known, f"{u} is declared external in jobs.toml but no inventory item names it"
    for u in ("systemd-sysupdate.timer", "systemd-sysupdate-reboot.timer", "backup-system.timer", "stack-backup.timer", "launchpadlib-cache-clean.timer",
              "0anacron", "apport", "google-chrome", "sysstat", "certbot"):
        assert u in known, u


# =========================================================================== the docs name only things that exist
def test_docs_name_only_real_migrate_subcommands_scaffold_commands_and_cli_modules():
    import importlib
    import re
    subs = {"status", "plan", "check", "cutover", "rollback", "journal", "retired", "audit", "validate", "export", "runbook"}
    docs = [ROOT / "docs" / "EXTENDING.md", ROOT / "docs" / "MIGRATION.md"]
    for f in docs:
        text = f.read_text()
        for m in re.finditer(r"homelab-maint migrate (\w+)", text):
            assert m.group(1) in subs, f"{f.name}: `homelab-maint migrate {m.group(1)}` is not a command"
        for m in re.finditer(r"python3 -m homelab_maint\.([a-z_]+)", text):
            assert hasattr(importlib.import_module(f"homelab_maint.{m.group(1)}"), "main"), f"{f.name}: homelab_maint.{m.group(1)} has no main()"
        for m in re.finditer(r"homelab_maint\.scaffold new (\w+)", text):
            assert m.group(1) in ("task", "job", "probe")
    # every `--flag` the docs give to `migrate` is accepted by the parser
    ap_flags = {"--apply", "--force", "--reason", "--json", "--fast", "--kind", "--write", "-n"}
    for f in docs:
        for line in f.read_text().splitlines():
            if "homelab-maint migrate" in line and not line.lstrip().startswith("|"):
                for flag in re.findall(r"(?<![\w-])(--[a-z-]+|-n)\b", line.split("#")[0]):
                    assert flag in ap_flags | {"--dry-run"}, f"{f.name}: unknown migrate flag {flag}: {line.strip()[:80]}"


def test_the_docs_examples_are_the_ones_the_loaders_accept(tmp_path):
    """The job and probe snippets printed in docs/EXTENDING.md must load in the real scheduler and probe engine."""
    import re
    from homelab_maint import jobs, probes, scheduler
    text = (ROOT / "docs" / "EXTENDING.md").read_text()
    blocks = re.findall(r"```toml\n(.*?)```", text, re.S)
    job_blocks = [b for b in blocks if b.lstrip().startswith("[[job]]")]
    probe_blocks = [b for b in blocks if "[[probe]]" in b]
    assert job_blocks and probe_blocks
    for b in blocks:
        tomllib.loads(b)                                     # every toml example at least parses (routine, notify, jobs, probes)
    for b in job_blocks:
        f = tmp_path / "jobs.toml"
        f.write_text(b)
        cfg = jobs.load(f, mcfg={"tasks": {}})
        assert cfg.errors == [] and cfg.jobs, cfg.errors
        assert [p for p in scheduler.validate(cfg) if "is not an executable" not in p and "does not exist" not in p] == []
    for b in probe_blocks:
        _d, plist, errs = probes.parse(tomllib.loads(b))
        assert errs == [] and plist


def test_the_check_example_in_the_docs_runs_as_documented(tmp_path):
    import re
    text = (ROOT / "docs" / "EXTENDING.md").read_text()
    (code,) = [b for b in re.findall(r"```python\n(.*?)```", text, re.S) if "inode_watch" in b]
    saved = dict(core.REGISTRY)
    try:
        exec(compile(code, "inode_watch.py", "exec"), {"__name__": "inode_watch_doc"})
        t = core.REGISTRY["inode_watch"]
        assert (t.klass, t.tier) == ("C0", "check")
        cfg = {"tasks": {"inode_watch": {"mounts": [str(tmp_path), "/definitely/not/mounted"], "warn_used_pct": 0.0}}, "caps": {}, "protected": {"patterns": []}}
        res, _ = core.run_task(t, cfg, apply=True)
        assert res.status == "warn" and res.summary.isascii() and len(res.summary) <= 140 and res.items[0]["mount"] == str(tmp_path)
        none, _ = core.run_task(t, {"tasks": {"inode_watch": {"mounts": ["/definitely/not/mounted"]}}, "caps": {}, "protected": {"patterns": []}}, apply=True)
        assert none.status == "skipped" and none.alert is False                         # unreadable mounts never page and never read as "fine"
    finally:
        core.REGISTRY.clear()
        core.REGISTRY.update(saved)


# =========================================================================== re-running a cutover must not lose what the first one did
def test_a_second_cutover_after_drift_keeps_the_original_record_so_rollback_still_restores_the_script(rig):
    """install.sh re-enables a retired timer (drift). Repairing it with another cutover finds the script move already in effect; the
    rollback afterwards must still put the SCRIPT back, not only the timer, or the restored timer would run a script that is gone."""
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    p = r.put("/usr/local/sbin/sc.sh", "#!/bin/sh\nexit 7\n", 0o750)
    assert r.m.cutover("sc", apply=True).ok and not p.exists()
    r.sys.add_unit("sc.timer")                                                      # drift: something re-enabled the timer
    assert r.m.rows()[0]["drift"] is True
    out = r.m.cutover("sc", apply=True)
    assert out.ok and r.sys.state("sc.timer")["UnitFileState"] == "disabled"
    acts = r.state()["items"]["sc"]["actions"]
    assert [a["status"] for a in acts] == ["done", "done"] and acts[1]["pre"]["src"]["sha"], "the move keeps the record of the first cutover"
    assert r.m.rollback("sc", apply=True).ok
    assert p.read_bytes() == b"#!/bin/sh\nexit 7\n" and stat.S_IMODE(p.stat().st_mode) == 0o750
    assert r.sys.state("sc.timer")["UnitFileState"] == "enabled" and r.sys.state("sc.timer")["ActiveState"] == "active"


def test_a_cutover_after_a_rollback_starts_from_a_clean_record(rig):
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    p = r.put("/usr/local/sbin/sc.sh")
    assert r.m.cutover("sc", apply=True).ok and r.m.rollback("sc", apply=True).ok and p.exists()
    assert r.m.cutover("sc", apply=True).ok and not p.exists()
    assert [a["status"] for a in r.state()["items"]["sc"]["actions"]] == ["done", "done"]
    assert r.m.rollback("sc", apply=True).ok and p.exists() and r.sys.state("sc.timer")["UnitFileState"] == "enabled"


def test_a_failed_repair_undoes_only_what_that_run_did(rig):
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    p = r.put("/usr/local/sbin/sc.sh")
    assert r.m.cutover("sc", apply=True).ok
    r.sys.add_unit("sc.timer")                                                      # drift again
    r.sys.hook = lambda argv: cp(argv, 1, "", "boom") if argv[:2] == ["systemctl", "disable"] else None
    out = r.m.cutover("sc", apply=True)
    assert out.rc == 4 and not p.exists(), "the script the FIRST cutover moved stays moved while the repair fails"
    assert r.state()["items"]["sc"]["state"] == "retired"                            # as it was before the repair: retired and drifting
    r.sys.hook = None
    assert r.m.rollback("sc", apply=True).ok and p.exists() and r.sys.state("sc.timer")["UnitFileState"] == "enabled"   # nothing was lost


# =========================================================================== owner additions survive an upgrade: legacy-retirement.d/
def test_owner_items_in_the_d_directory_join_the_inventory_and_untrusted_ones_refuse_all_of_it(tmp_path, monkeypatch):
    conf = tmp_path / "conf2"
    (conf / "legacy-retirement.d").mkdir(parents=True)
    os.chmod(conf, 0o755)
    os.chmod(conf / "legacy-retirement.d", 0o755)
    main_toml = conf / "legacy-retirement.toml"
    main_toml.write_text(REAL_TOML.read_text())
    os.chmod(main_toml, 0o644)
    mine = conf / "legacy-retirement.d" / "10-mine.toml"
    mine.write_text('[[item]]\nname = "my-own-timer"\nkind = "timer"\nlocation = "mine.timer"\nmode = "port"\nwave = 3\n'
                    'parity_check = [{ kind = "none", note = "owner says so" }]\nretire_actions = [{ do = "disable", unit = "mine.timer" }]\n')
    os.chmod(mine, 0o644)
    monkeypatch.setattr(core, "CONF_DIR", conf)
    inv = legacy.load_inventory()
    assert inv.get("my-own-timer") and inv.get("backup-system") and len(inv.items) == len(real().items) + 1
    assert legacy.main(["validate"]) == 0
    os.chmod(mine, 0o666)                                                          # group/world writable: the whole inventory is refused
    with pytest.raises(InventoryError, match="10-mine.toml"):
        legacy.load_inventory()
    os.chmod(mine, 0o644)
    os.chmod(conf / "legacy-retirement.d", 0o777)
    with pytest.raises(InventoryError, match="legacy-retirement.d"):
        legacy.load_inventory()
    os.chmod(conf / "legacy-retirement.d", 0o755)
    (conf / "legacy-retirement.d" / "20-dup.toml").write_text('[[item]]\nname = "backup-system"\nkind = "timer"\nlocation = "x.timer"\nmode = "keep"\nwave = 1\nunmonitored = "n"\n')
    os.chmod(conf / "legacy-retirement.d" / "20-dup.toml", 0o644)
    with pytest.raises(InventoryError, match="duplicate name"):
        legacy.load_inventory()
    (conf / "legacy-retirement.d" / "20-dup.toml").write_text("this is [not toml")
    with pytest.raises(InventoryError, match="unreadable"):
        legacy.load_inventory()
    inv2 = legacy.load_inventory(main_toml)                                        # an explicit path never reads the .d directory
    assert inv2.get("my-own-timer") is None


# =========================================================================== REVIEW FIX 1: a re-run must never lose the records of what an earlier run did
class Crash(BaseException):
    """A hard kill in the middle of a step: not caught by the executor (which handles Refused/OpError/OSError), exactly like SIGKILL."""


def at_rest(r, unit="sc.timer"):
    u = r.sys.state(unit)
    return u["UnitFileState"], u["ActiveState"]


def test_a_rerun_after_a_crash_keeps_the_records_so_rollback_still_undoes_the_first_step(rig):
    """Run 1 is killed after the timer was disabled and before the script moved. Run 2 finds the disable satisfied: it used to record it as
    pre_satisfied, so the rollback left the timer disabled and reported 'rolled back' (the weekly backup would then never be scheduled)."""
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    p = r.put("/usr/local/sbin/sc.sh", "#!/bin/sh\nexit 7\n", 0o750)
    real_move = r.host.safe_move
    r.host.safe_move = lambda s, d: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        r.m.cutover("sc", apply=True)
    assert at_rest(r) == ("disabled", "inactive") and p.exists() and r.state()["items"]["sc"]["state"] == "partial"
    r.host.safe_move = real_move
    assert r.m.cutover("sc", apply=True).ok
    assert [a["status"] for a in r.state()["items"]["sc"]["actions"]] == ["done", "done"], "the first step is carried, not 'pre_satisfied'"
    assert not p.exists()
    assert r.m.rollback("sc", apply=True).ok
    assert at_rest(r) == ("enabled", "active"), "rollback must undo the disable the interrupted run did"
    assert p.read_bytes() == b"#!/bin/sh\nexit 7\n" and stat.S_IMODE(p.stat().st_mode) == 0o750
    assert r.state()["items"]["sc"]["state"] == "rolled_back"


def test_attention_then_rerun_then_rollback_restores_the_original(rig):
    """An undo that could not finish leaves state 'attention'. docs/MIGRATION.md says 'fix the cause, run it again': that second run must keep
    the first run's records, or the rollback after it leaves the timer disabled."""
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    p = r.put("/usr/local/sbin/sc.sh")
    real_move = r.host.safe_move
    r.host.safe_move = lambda s, d: (_ for _ in ()).throw(OSError(errno.EIO, "boom"))
    r.sys.hook = lambda argv: cp(argv, 1, "", "cannot enable") if argv[:2] == ["systemctl", "enable"] else None
    out = r.m.cutover("sc", apply=True)
    assert out.rc == 4 and r.state()["items"]["sc"]["state"] == "attention" and at_rest(r)[0] == "disabled"
    r.host.safe_move, r.sys.hook = real_move, None                                       # "fix the cause"
    assert r.m.cutover("sc", apply=True).ok and not p.exists()
    assert [a["status"] for a in r.state()["items"]["sc"]["actions"]] == ["done", "done"]
    assert r.m.rollback("sc", apply=True).ok
    assert at_rest(r) == ("enabled", "active") and p.exists()


def test_a_backup_cutover_that_crashed_at_the_job_handover_rolls_back_to_timer_enabled_and_job_not_managed(rig):
    """The reviewer's backup-system scenario: [disable timer, job_mode]. Killed at the hand-over, re-run, then rolled back: the timer must
    be enabled again AND the job back at its prior mode, or nothing at all schedules the weekly backup."""
    r = rig([job_item()])
    r.sys.add_unit("jb.timer")
    real_set = r.jobs.set
    r.jobs.set = lambda n, m: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        r.m.cutover("jb", apply=True)
    assert at_rest(r, "jb.timer") == ("disabled", "inactive") and r.jobs.overrides == {}
    r.jobs.set = real_set
    assert r.m.cutover("jb", apply=True).ok and r.jobs.overrides == {"jb-job": "managed"}
    assert r.m.rollback("jb", apply=True).ok
    assert at_rest(r, "jb.timer") == ("enabled", "active") and r.jobs.overrides == {}


def test_an_interrupted_attempt_keeps_the_state_from_before_it_touched_anything(rig):
    """`disable --now` killed between the disable and the stop: the unit is now disabled but still active. The record written BEFORE the
    attempt (enabled + active) is what rollback must restore, not what the half-changed probe sees on the second run."""
    r = rig([timer_item()])
    r.sys.add_unit("nb.timer")

    def hook(argv):
        if argv[:2] == ["systemctl", "disable"]:
            r.sys.calls.append(list(argv))
            r.sys.units[("system", "nb.timer")]["UnitFileState"] = "disabled"            # only the first half landed
            raise Crash()
    r.sys.hook = hook
    with pytest.raises(Crash):
        r.m.cutover("nb", apply=True)
    assert at_rest(r, "nb.timer") == ("disabled", "active")
    r.sys.hook = None
    assert r.m.cutover("nb", apply=True).ok and at_rest(r, "nb.timer") == ("disabled", "inactive")
    rec = r.state()["items"]["nb"]["actions"][0]
    assert rec["status"] == "done" and rec["pre"]["enabled"] == "enabled" and rec["pre"]["active"] == "active"
    assert r.m.rollback("nb", apply=True).ok
    assert at_rest(r, "nb.timer") == ("enabled", "active")


def test_a_started_record_whose_effect_landed_is_promoted_to_done_keeping_its_pre(rig):
    r = rig([timer_item()])
    r.sys.add_unit("nb.timer")
    assert r.m.cutover("nb", apply=True).ok
    st = r.state()
    st["items"]["nb"].update(state="partial")
    st["items"]["nb"]["actions"][0]["status"] = "started"                                # killed before 'done' was written
    core.write_json_atomic(r.m.state_path, st, 0o600)
    assert r.m.cutover("nb", apply=True).ok
    rec = r.state()["items"]["nb"]["actions"][0]
    assert rec["status"] == "done" and rec["pre"]["enabled"] == "enabled"
    assert r.m.rollback("nb", apply=True).ok and at_rest(r, "nb.timer") == ("enabled", "active")


def test_a_rerun_that_fails_again_after_a_crash_is_attention_not_failed(rig):
    """The crashed run's disable is still in effect, so 'failed (fully undone)' would be a lie; rollback must still be able to undo it."""
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    p = r.put("/usr/local/sbin/sc.sh")
    real_move = r.host.safe_move
    r.host.safe_move = lambda s, d: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        r.m.cutover("sc", apply=True)
    r.host.safe_move = lambda s, d: (_ for _ in ()).throw(OSError(errno.EIO, "boom"))
    out = r.m.cutover("sc", apply=True)
    assert out.rc == 4 and r.state()["items"]["sc"]["state"] == "attention" and at_rest(r)[0] == "disabled" and p.exists()
    assert r.m.rollback("sc", apply=True).ok and at_rest(r) == ("enabled", "active")
    r.host.safe_move = real_move


def test_what_a_failed_run_fully_undid_is_never_carried_into_the_next_run(rig):
    """Run 1 fails and undoes everything (records 'undone'). The owner then disables the timer by hand. Run 2 finds it satisfied: that
    step was NOT done by the tool, so rollback must leave the owner's change alone."""
    r = rig([script_item()])
    r.sys.add_unit("sc.timer")
    p = r.put("/usr/local/sbin/sc.sh")
    real_move = r.host.safe_move
    r.host.safe_move = lambda s, d: (_ for _ in ()).throw(OSError(errno.EIO, "boom"))
    assert r.m.cutover("sc", apply=True).rc == 4 and r.state()["items"]["sc"]["state"] == "failed"
    assert [a["status"] for a in r.state()["items"]["sc"]["actions"]] == ["undone", "undone"]
    r.sys.units[("system", "sc.timer")].update(UnitFileState="disabled", ActiveState="inactive")     # the owner did this
    r.host.safe_move = real_move
    assert r.m.cutover("sc", apply=True).ok
    assert [a["status"] for a in r.state()["items"]["sc"]["actions"]] == ["pre_satisfied", "done"]
    assert r.m.rollback("sc", apply=True).ok and p.exists()
    assert at_rest(r) == ("disabled", "inactive"), "the owner's own change is not ours to undo"


# =========================================================================== REVIEW FIX 6: smartd must never find its hook missing
def test_the_swap_never_leaves_the_old_path_empty_in_either_direction(rig, monkeypatch):
    """smartd calls /usr/local/sbin/smart-alert.sh once per event and does not retry. Move-then-write-stub left the path absent between the
    two steps (and again during a rollback). Now: verified copy, then ONE rename of the stub over the original; rollback is one rename
    back. Spy on every rename/replace/unlink/open-for-write that touches the path: it exists before and after each one."""
    r = rig([stub_item()])
    path = r.host.p("/usr/local/sbin/h.sh")
    r.put("/usr/local/sbin/h.sh", "#!/bin/sh\necho legacy\n", 0o700)
    seen = []
    for name in ("rename", "replace", "unlink"):
        real = getattr(os, name)

        def spy(*a, real=real, name=name, **k):
            before = os.path.lexists(path)
            res = real(*a, **k)
            if str(a[0]) == str(path) or (len(a) > 1 and str(a[1]) == str(path)):
                seen.append((name, before, os.path.lexists(path)))
            return res
        monkeypatch.setattr(os, name, spy)
    assert r.m.cutover("st", apply=True).ok
    assert r.m.rollback("st", apply=True).ok
    touched = [x for x in seen]
    assert touched and all(before and after for _n, before, after in touched), touched       # never absent, in both directions
    assert path.read_text() == "#!/bin/sh\necho legacy\n" and stat.S_IMODE(path.stat().st_mode) == 0o700
    assert not r.exists(f"{LEG}/st/h.sh"), "the verified copy was renamed back: no duplicate left behind"


def test_the_swap_copies_first_and_the_original_is_still_there_until_the_stub_lands(rig):
    r = rig([stub_item()])
    r.put("/usr/local/sbin/h.sh", "#!/bin/sh\necho legacy\n", 0o700)
    states = []
    real_write = r.host.write_atomic

    def write(path, text, mode):
        if path == "/usr/local/sbin/h.sh":                                   # the moment before the stub replaces the original
            states.append((r.host.p("/usr/local/sbin/h.sh").read_text(), r.host.p(f"{LEG}/st/h.sh").read_text()))
        return real_write(path, text, mode)
    r.host.write_atomic = write
    assert r.m.cutover("st", apply=True).ok
    assert states == [("#!/bin/sh\necho legacy\n", "#!/bin/sh\necho legacy\n")], "original in place AND an identical verified copy"
    assert r.m.cutover("st").ok and "already retired" in "\n".join(r.m.cutover("st").lines)


def test_a_crash_between_the_copy_and_the_stub_loses_nothing_and_a_rerun_finishes(rig):
    r = rig([stub_item()])
    r.put("/usr/local/sbin/h.sh", "#!/bin/sh\necho legacy\n", 0o700)
    real_write = r.host.write_atomic
    r.host.write_atomic = lambda *a: (_ for _ in ()).throw(Crash()) if a[0] == "/usr/local/sbin/h.sh" else real_write(*a)
    with pytest.raises(Crash):
        r.m.cutover("st", apply=True)
    h = r.host.p("/usr/local/sbin/h.sh")
    assert h.read_text() == "#!/bin/sh\necho legacy\n", "the hook is still the real script: smartd would have worked at any instant"
    assert r.host.p(f"{LEG}/st/h.sh").exists() and r.state()["items"]["st"]["state"] == "partial"
    r.host.write_atomic = real_write
    assert r.m.cutover("st", apply=True).ok and h.read_text().startswith(f"#!/bin/sh\nexec {LEG}/st/h.sh")
    assert [a["status"] for a in r.state()["items"]["st"]["actions"]] == ["done", "done"]
    assert r.m.rollback("st", apply=True).ok
    assert h.read_text() == "#!/bin/sh\necho legacy\n" and stat.S_IMODE(h.stat().st_mode) == 0o700


def test_rolling_back_a_cutover_that_crashed_before_the_stub_leaves_the_original_alone(rig):
    r = rig([stub_item()])
    r.put("/usr/local/sbin/h.sh", "#!/bin/sh\necho legacy\n", 0o700)
    real_write = r.host.write_atomic
    r.host.write_atomic = lambda *a: (_ for _ in ()).throw(Crash()) if a[0] == "/usr/local/sbin/h.sh" else real_write(*a)
    with pytest.raises(Crash):
        r.m.cutover("st", apply=True)
    r.host.write_atomic = real_write
    out = r.m.rollback("st", apply=True)
    assert out.ok and r.host.p("/usr/local/sbin/h.sh").read_text() == "#!/bin/sh\necho legacy\n"


def test_the_stub_refuses_to_replace_a_script_that_changed_after_it_was_copied(rig):
    """Replacing the original is only safe while the verified copy still equals it; otherwise the edit made in between would be lost."""
    r = rig([stub_item()])
    h = r.put("/usr/local/sbin/h.sh", "#!/bin/sh\necho legacy\n", 0o700)
    real_copy = r.host.safe_copy

    def copy_then_edit(s, d):
        real_copy(s, d)
        h.write_text("#!/bin/sh\necho edited by the owner a moment later\n")
    r.host.safe_copy = copy_then_edit
    out = r.m.cutover("st", apply=True)
    assert out.rc == 4 and "no longer matches its legacy copy" in "\n".join(out.lines)
    assert h.read_text() == "#!/bin/sh\necho edited by the owner a moment later\n", "the edit survived"


def test_swap_rollback_refuses_when_the_legacy_copy_changed_or_vanished_and_keeps_the_stub(rig):
    r = rig([stub_item()])
    r.put("/usr/local/sbin/h.sh", "#!/bin/sh\necho legacy\n", 0o700)
    assert r.m.cutover("st", apply=True).ok
    r.host.p(f"{LEG}/st/h.sh").write_text("edited")
    out = r.m.rollback("st", apply=True)
    assert out.rc == 4 and "was modified since it was made" in "\n".join(out.lines)
    assert r.host.p("/usr/local/sbin/h.sh").read_text().startswith("#!/bin/sh\nexec ")             # the forwarding stub still works
    r.host.p(f"{LEG}/st/h.sh").unlink()
    assert "is gone" in "\n".join(r.m.rollback("st", apply=True).lines)


def test_replace_from_and_safe_copy_work_across_filesystems_and_never_overwrite(rig, monkeypatch):
    r = rig([stub_item()])
    r.put("/usr/local/sbin/h.sh", "#!/bin/sh\necho legacy\n", 0o700)
    real_rename = os.rename

    def rename(a, b):
        if str(b).endswith("/usr/local/sbin/h.sh"):
            raise OSError(errno.EXDEV, "cross-device")
        return real_rename(a, b)
    monkeypatch.setattr(os, "rename", rename)
    assert r.m.cutover("st", apply=True).ok
    assert r.m.rollback("st", apply=True).ok                                    # copy beside the destination, verify, os.replace, then drop the duplicate
    h = r.host.p("/usr/local/sbin/h.sh")
    assert h.read_text() == "#!/bin/sh\necho legacy\n" and stat.S_IMODE(h.stat().st_mode) == 0o700 and not r.exists(f"{LEG}/st/h.sh")
    with pytest.raises(legacy.OpError, match="destination exists"):
        r.host.p(f"{LEG}/st").mkdir(parents=True, exist_ok=True)
        r.host.p(f"{LEG}/st/x").write_text("x")
        r.host.safe_copy("/usr/local/sbin/h.sh", f"{LEG}/st/x")


def test_the_real_smartd_and_backup_failure_stubs_are_atomic_swaps():
    by = {i.name: i for i in real().items}
    for n, path in (("smartd-alert-hook", "/usr/local/sbin/smart-alert.sh"), ("backup-failure-hook", "/usr/local/sbin/backup-failed.sh")):
        env = legacy.Env(Host(), by[n], f"{LEG}/{n}", Path("/x"), 0.0)
        acts = {a.kind: a for a in (legacy.ACTIONS[x["do"]](x, env) for x in by[n].actions)}
        assert acts["move"].swap() and acts["stub"].twin() == f"{LEG}/{n}/{os.path.basename(path)}", n
    md = legacy.runbook(real())
    assert f"`cp -p /usr/local/sbin/smart-alert.sh {LEG}/smartd-alert-hook/smart-alert.sh   # verified copy; the original stays until the stub replaces it`" in md
    assert "`mv /usr/local/lib/homelab-maint/legacy/smartd-alert-hook/smart-alert.sh /usr/local/sbin/smart-alert.sh   # one atomic rename over our stub`" in md


def test_the_smartd_parity_runs_the_real_entry_point_without_sending_or_writing(tmp_path):
    """The shipped dry-run command, with the installed path swapped for this checkout: it exercises native.smart_event for real (parse,
    log line, notify event) with the sender replaced and the log at /dev/null, and prints the sentinel the parity regex wants."""
    argv = list(next(p for p in real().get("smartd-alert-hook").parity if p["kind"] == "command" and p["argv"][0].endswith("python3"))["argv"])
    argv[2] = argv[2].replace("/usr/local/lib/homelab-maint", str(ROOT))
    env = {**os.environ, "HOMELAB_MAINT_STATE": str(tmp_path / "s"), "HOMELAB_MAINT_LOG": str(tmp_path / "l"),
           "HOMELAB_MAINT_RUN": str(tmp_path / "r"), "HOMELAB_MAINT_CONF": str(tmp_path / "c")}
    res = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)
    assert res.returncode == 0 and res.stdout.strip() == "smart_event dry run ok", res.stderr[-300:]
    assert not (tmp_path / "s").exists() and not (tmp_path / "l").exists(), "a dry run writes nothing"
    broken = [*argv[:2], argv[2].replace("sent[0]['kind'] == 'test'", "sent[0]['kind'] == 'nope'")]
    assert subprocess.run(broken, capture_output=True, text=True, timeout=60, env=env).returncode == 1
    spec = next(p for p in real().get("smartd-alert-hook").parity if p["kind"] == "command" and p["argv"][0].endswith("python3"))
    assert chk(spec, run=lambda a, input_=None, timeout=60: cp(a, 0, "smart_event dry run FAILED\n")).ok is False        # the regex is the gate


# =========================================================================== REVIEW FIX 2: backups only after PROVEN runs
def jrun(t, status="ok"):
    return {"t": t, "status": status}


def test_job_parity_reads_the_ticks_job_history_not_task_history():
    """kind = "job" read history kind "task" while scheduler.py writes kind "job": it could never go green."""
    recs = [{"t": NOW - 3600 * (60 - i * 5), "kind": "job", "task": "backup-system", "status": "ok", "rc": 0} for i in range(13)]
    spec = {"kind": "job", "name": "backup-system", "green": 8, "min_hours": 48, "fresh_h": 2}
    c = chk(spec, history=history_of(recs))
    assert c.ok is True and "13 green runs" in c.detail
    tasks_only = [{**r, "kind": "task"} for r in recs]
    assert chk(spec, history=history_of(tasks_only)).ok is False                           # a task of the same name is not a job run
    recs[-2]["status"] = "crit"
    assert chk(spec, history=history_of(recs)).ok is False
    expired = [{**r, "status": "skipped"} for r in recs]                                   # an expired occurrence proves nothing
    assert chk(spec, history=history_of(expired)).ok is False


def retired_pair(rig, **kw):
    """a: hands job a-job to the tick (soak 2 d, 2 green runs needed); b depends on it."""
    r = rig([job_item("a", job="a-job", soak_days=2, verified_runs=2), item("b", wave=4, depends_on=["a"])], **kw)
    r.sys.add_unit("a.timer")
    r.sys.add_unit("b.timer")
    return r


def dep_line(r, name="b"):
    return next(c for c in r.m.deps(r.inv.get(name)))


def test_a_dependency_without_a_recorded_green_run_blocks_its_dependents_however_long_it_has_been(rig):
    """backup-system cut over, the tick never launches it (pressure, a bad mount, a jobs.toml it ignores): 15 days later backup-immich used
    to see 'retired for 15.0 d' and go ahead with zero umbrella-run backups ever recorded."""
    runs: list[dict] = []
    r = retired_pair(rig, job_history=lambda name, since: list(runs))
    assert r.m.cutover("a", apply=True).ok
    r.now += 15 * 86400
    c = dep_line(r)
    assert c.ok is False and "unproven" in c.detail and "0 of 2 green run(s)" in c.detail and "homelab-maint job run a-job" in c.detail
    out = r.m.cutover("b", apply=True)
    assert out.refused and "parity is not green" in out.refused and r.sys.state("b.timer")["UnitFileState"] == "enabled"
    runs.append(jrun(r.now - 3600))                                                        # one green run is not the two that were asked for
    assert dep_line(r).ok is False and "1 of 2" in dep_line(r).detail


def test_the_soak_starts_at_the_first_green_run_not_at_the_cutover(rig):
    runs: list[dict] = []
    r = retired_pair(rig, job_history=lambda name, since: list(runs))
    assert r.m.cutover("a", apply=True).ok
    first = r.now + 5 * 86400                                                              # the tick's first green run is five days late
    runs += [jrun(first), jrun(first + 86400)]
    r.now = first + 86400 + 60
    c = dep_line(r)
    assert c.ok is False and "still soaking" in c.detail and "1.0 more day(s)" in c.detail and "2 green run(s)" in c.detail
    assert [x for x in r.m.rows() if x["name"] == "a"][0]["soak_left_d"] == 1.0
    r.now = first + 2 * 86400 + 60
    c = dep_line(r)
    assert c.ok is True and "2 green run(s) of job a-job recorded since the cutover" in c.detail
    assert r.m.cutover("b", apply=True).ok


def test_only_green_runs_after_the_cutover_count(rig):
    runs: list[dict] = []
    r = retired_pair(rig, job_history=lambda name, since: list(runs))
    assert r.m.cutover("a", apply=True).ok
    runs += [jrun(NOW - 86400), jrun(NOW + 60, "warn"), jrun(NOW + 120, "crit"), jrun(NOW + 180, "skipped"), jrun(NOW + 240, "error")]
    r.now += 3 * 86400
    assert dep_line(r).ok is False and "0 of 2" in dep_line(r).detail
    runs.append(jrun(NOW + 300))
    assert "1 of 2" in dep_line(r).detail


def test_a_quiet_monitor_job_is_proven_by_the_schedulers_own_last_run_record(rig):
    """Monitors record only failures in history.jsonl, so the proof of life is sched.json's last run (one run), and the soak counts from the cutover."""
    last = {}
    r = rig([job_item("a", job="a-job", soak_days=2), item("b", wave=4, depends_on=["a"])], job_last=lambda name: last.get(name))
    r.sys.add_unit("a.timer")
    r.sys.add_unit("b.timer")
    assert r.m.cutover("a", apply=True).ok
    r.now += 3 * 86400
    assert dep_line(r).ok is False and "0 of 1" in dep_line(r).detail
    last["a-job"] = {"t": r.now - 120, "status": "ok", "bad": False}
    assert dep_line(r).ok is True and "last ran green" in dep_line(r).detail                # soak (2 d) counted from the cutover: long over
    last["a-job"] = {"t": r.now - 120, "status": "ok", "bad": True}
    assert dep_line(r).ok is False
    last["a-job"] = {"t": NOW - 5, "status": "ok", "bad": False}                            # a run from BEFORE the cutover proves nothing
    assert dep_line(r).ok is False


def test_unreadable_run_evidence_is_unproven_not_green(rig):
    r = retired_pair(rig, job_history=lambda name, since: (_ for _ in ()).throw(OSError("history unreadable")))
    assert r.m.cutover("a", apply=True).ok
    r.now += 30 * 86400
    assert dep_line(r).ok is False and "cannot read the run history" in dep_line(r).detail


def test_items_that_hand_over_nothing_to_the_tick_keep_the_plain_calendar_soak(rig):
    r = rig([item("a", soak_days=2), item("b", wave=4, depends_on=["a"])])
    r.sys.add_unit("a.timer")
    r.sys.add_unit("b.timer")
    assert r.m.cutover("a", apply=True).ok
    r.now += 2.1 * 86400
    assert dep_line(r).ok is True and r.m.cutover("b", apply=True).ok


def test_status_says_unproven_and_the_default_sources_read_history_and_the_scheduler_state(rig, tmp_path, monkeypatch):
    r = retired_pair(rig, job_history=lambda name, since: [])
    assert r.m.cutover("a", apply=True).ok
    rows = {x["name"]: x for x in r.m.rows()}
    assert "0 of 2" in rows["a"]["unproven"] and rows["a"]["soak_left_d"] == 2.0 and "UNPROVEN" in legacy.render_status(r.m.rows())
    # the default seams: history.jsonl (kind job) and sched.json
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "st")
    (tmp_path / "st").mkdir()
    core.write_json_atomic(tmp_path / "st" / "sched.json", {"schema": 1, "meta": {}, "jobs": {"x-job": {"last_end": NOW - 60, "last_status": "ok", "last_bad": False}}})
    s = Sources(now=lambda: NOW, history=history_of([{"t": NOW - 100, "kind": "job", "task": "x-job", "status": "ok"},
                                                    {"t": NOW - 100, "kind": "job", "task": "other", "status": "ok"},
                                                    {"t": NOW - 100, "kind": "task", "task": "x-job", "status": "ok"}]))
    assert s.job_history("x-job", 86400) == [{"t": NOW - 100, "status": "ok"}]
    assert s.job_last("x-job") == {"t": NOW - 60, "status": "ok", "bad": False} and s.job_last("nope") is None


def test_the_cutover_tells_the_owner_to_run_the_first_job_attended(rig):
    r = rig([job_item("a", job="a-job", verified_runs=2)])
    r.sys.add_unit("a.timer")
    dry = "\n".join(r.m.cutover("a").lines)
    out = r.m.cutover("a", apply=True)
    text = "\n".join(out.lines)
    assert "homelab-maint job run a-job" in text and "ATTENDED" in text and "2 green run(s)" in text
    assert "homelab-maint job run a-job" in r.notes[-1]["summary"] or "homelab-maint job run a-job" in r.notes[-1]["done"][0]
    assert "ATTENDED" not in dry                                                           # nothing was done in a dry run


# --------------------------------------------------------------------------- unit_equiv: the proof run that cannot exist before the cutover
def show(**kw):
    base = {"ExecStart": "{ path=/usr/local/sbin/backup-system.sh ; argv[]=/usr/local/sbin/backup-system.sh ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }",
            "User": "", "Nice": "10", "IOSchedulingClass": "2", "IOSchedulingPriority": "7", "TimeoutStartUSec": "infinity",
            "RequiresMountsFor": "/mnt/backup/system", "WorkingDirectory": "", "Environment": ""}
    base.update(kw)
    return "".join(f"{k}={v}\n" for k, v in base.items())


JOBV = {"command": ["/usr/local/sbin/backup-system.sh"], "user": "root", "nice": 10, "ionice_class": 2, "ionice_prio": 7, "timeout_s": 0,
        "requires_mounts": ["/mnt/backup/system"], "workdir": "", "env": {}}


def equiv(show_text=None, scope="system", **job):
    jv = {**JOBV, **job}
    calls = []

    def run(argv, input_=None, timeout=60):
        calls.append(argv)
        return cp(argv, 0, show_text if show_text is not None else show())
    c = chk({"kind": "unit_equiv", "job": "backup-system", "unit": "backup-system.service", "scope": scope}, run=run,
            job_attr=lambda j, a: jv[a], ctl=lambda sc, args: (["systemctl", *args] if sc == "system" else ["systemctl", "--user", *args]))
    return c, calls


def test_unit_equiv_passes_when_the_job_is_exactly_the_unit():
    c, calls = equiv()
    assert c.ok is True and calls == [["systemctl", "show", "--no-pager", "-p", "ExecStart,User,Nice,IOSchedulingClass,IOSchedulingPriority,TimeoutStartUSec,RequiresMountsFor,WorkingDirectory,Environment", "backup-system.service"]]


def test_unit_equiv_fails_on_every_difference_and_says_which():
    cases = [(dict(command=["/usr/local/sbin/backup-system.sh", "--fast"]), "command differs"), (dict(user="ohmz"), "user: unit root, job ohmz"),
             (dict(nice=0), "nice: unit 10, job 0"), (dict(ionice_class=3), "ionice class"), (dict(ionice_prio=4), "ionice priority"),
             (dict(timeout_s=3600), "timeout: unit infinity, job 3600"), (dict(requires_mounts=[]), "RequiresMountsFor"),
             (dict(requires_mounts=["/mnt/backup/system", "/media/Immich"]), "RequiresMountsFor"), (dict(workdir="/x"), None)]
    for job, want in cases[:-1]:
        c, _ = equiv(**job)
        assert c.ok is False and want in c.detail, (job, c.detail)
    c, _ = equiv(show(WorkingDirectory="/srv/backup"))
    assert c.ok is False and "working directory" in c.detail
    c, _ = equiv(show(WorkingDirectory="!/home/ohmz"))                                      # systemd's own "!" = the user's home: not a difference
    assert c.ok is True
    c, _ = equiv(show(TimeoutStartUSec="30min"), timeout_s=1800)
    assert c.ok is True
    c, _ = equiv(show(ExecStart=show().split("\n")[0].split("=", 1)[1] + "\nExecStart={ path=/y ; argv[]=/y ; ignore_errors=no }"))
    assert c.ok is False and "2 ExecStart lines" in c.detail
    c, _ = equiv("")
    assert c.ok is False
    c, _ = equiv(show(TimeoutStartUSec="whenever"))
    assert c.ok is False and "timeout" in c.detail


def test_unit_equiv_environment_differences_name_the_variable_but_never_print_a_value():
    c, _ = equiv(show(Environment="HOME=/home/ohmz API_TOKEN=hunter2-secret"), env={"HOME": "/home/ohmz"})
    assert c.ok is False and "API_TOKEN" in c.detail and "hunter2" not in c.detail
    c, _ = equiv(show(Environment="HOME=/home/ohmz"), env={"HOME": "/home/ohmz", "EXTRA": "fine"})                     # a superset is fine
    assert c.ok is True
    c, _ = equiv(show(Environment="HOME=/elsewhere"), env={"HOME": "/home/ohmz"})
    assert c.ok is False and "HOME" in c.detail and "/elsewhere" not in c.detail


def test_unit_equiv_reads_user_units_through_the_user_manager_and_fails_closed():
    c, calls = equiv(scope="user:ohmz", user="ohmz")
    assert calls[0][:2] == ["systemctl", "--user"] and c.ok is True                     # User= is empty in the user manager: the job's user must be that user
    assert equiv(scope="user:ohmz")[0].ok is False
    c = chk({"kind": "unit_equiv", "job": "j", "unit": "x.service"}, run=lambda a, input_=None, timeout=60: cp(a, 1, "", "boom"),
            job_attr=lambda j, a: JOBV[a], ctl=lambda sc, args: ["systemctl", *args])
    assert c.ok is False and "cannot read" in c.detail
    assert "unit_equiv needs job" in bad([item("x", parity_check=[{"kind": "unit_equiv", "unit": "a.service"}])])
    assert "valid unit" in bad([item("x", parity_check=[{"kind": "unit_equiv", "job": "a", "unit": "not a unit"}])])


# What `systemctl show` printed for the five legacy services on this host (2026-10-02, read only). The shipped jobs.toml must equal them.
LIVE_UNITS = {
    ("backup-system", "backup-system.service", "system"): show(),
    ("backup-immich", "backup-immich.service", "system"): show(
        ExecStart="{ path=/usr/local/sbin/backup-immich.sh ; argv[]=/usr/local/sbin/backup-immich.sh ; ignore_errors=no }", RequiresMountsFor="/mnt/backup/immich /media/Immich"),
    ("stack-backup", "stack-backup.service", "user:ohmz"): show(
        ExecStart="{ path=/home/ohmz/StudioProjects/ai-stack/scripts/stack_backup.sh ; argv[]=/home/ohmz/StudioProjects/ai-stack/scripts/stack_backup.sh ; ignore_errors=no }",
        IOSchedulingClass="3", IOSchedulingPriority="4", TimeoutStartUSec="30min", RequiresMountsFor="", WorkingDirectory="!/home/ohmz"),
    ("stack-watchdog", "stack-watchdog.service", "user:ohmz"): show(
        ExecStart="{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 /home/ohmz/StudioProjects/ai-stack/scripts/stack_watchdog.py ; ignore_errors=no }",
        Nice="0", IOSchedulingClass="2", IOSchedulingPriority="4", TimeoutStartUSec="2min", RequiresMountsFor="", WorkingDirectory="!/home/ohmz", Environment="HOME=/home/ohmz"),
    ("search-canary", "search-canary.service", "user:ohmz"): show(
        ExecStart="{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 /home/ohmz/StudioProjects/ai-stack/scripts/search_canary.py ; ignore_errors=no }",
        Nice="0", IOSchedulingClass="2", IOSchedulingPriority="4", TimeoutStartUSec="5min", RequiresMountsFor="", WorkingDirectory="!/home/ohmz", Environment="HOME=/home/ohmz"),
}


def test_the_shipped_adapter_jobs_equal_the_legacy_units_as_captured_on_the_host():
    jobs_ = real_jobs()
    specs = {(p["job"], p["unit"], p.get("scope", "system")): p for it in real().items for p in it.parity if p["kind"] == "unit_equiv"}
    assert set(specs) == set(LIVE_UNITS), "every unit_equiv in the inventory has a capture here, and every capture is in the inventory"
    for key, text in LIVE_UNITS.items():
        c = chk(specs[key], run=lambda a, input_=None, timeout=60, text=text: cp(a, 0, text), job_attr=lambda j, a: getattr(jobs_[j], a),
                ctl=lambda sc, args: ["systemctl", *args])
        assert c.ok is True, (key, c.detail)


# --------------------------------------------------------------------------- scheduler_validate: one jobs.toml typo must not silence every job
def test_scheduler_validate_parity_is_per_job_and_catches_global_config_trouble():
    probs = ["job backup-system: /usr/local/sbin/backup-system.sh is not an executable file", "job other: unknown key 'x' ignored",
             "jobs.toml unreadable: TOMLDecodeError: bad"]
    spec = {"kind": "scheduler_validate", "job": "backup-system"}
    c = chk(spec, validate=lambda: probs)
    assert c.ok is False and "2 problem(s)" in c.detail                                     # its own + the global one; 'other' is not its business
    assert chk({"kind": "scheduler_validate", "job": "tier-daily"}, validate=lambda: probs).ok is False       # the global one blocks every job
    assert chk({"kind": "scheduler_validate", "job": "tier-daily"}, validate=lambda: probs[1:2]).ok is True
    assert chk({"kind": "scheduler_validate", "jobs": ["a", "b"]}, validate=lambda: ["job b: x"]).ok is False
    assert chk({"kind": "scheduler_validate"}, validate=lambda: probs[1:2]).ok is False         # no job named: everything counts
    assert chk(spec, validate=lambda: []).ok is True
    assert chk(spec, validate=lambda: (_ for _ in ()).throw(ImportError("no scheduler"))).ok is False
    assert "job names" in bad([item("x", parity_check=[{"kind": "scheduler_validate", "job": "Bad Name"}])])


def test_every_adapter_that_hands_a_job_over_gets_scheduler_validate_automatically():
    inv = parse_inventory({"item": [job_item(), job_item("p", mode="port", job="p-job"), job_item("e", parity_check=[
        {"kind": "scheduler_validate", "job": "e-job"}], job="e-job")]})
    assert {"kind": "scheduler_validate", "job": "jb-job"} in inv.get("jb").parity
    assert not any(p["kind"] == "scheduler_validate" for p in inv.get("p").parity)             # a retired job never runs: nothing to validate
    assert sum(p["kind"] == "scheduler_validate" for p in inv.get("e").parity) == 1             # not added twice
    for it in real().items:
        if any(a["do"] == "job_mode" and a["mode"] == "managed" for a in it.actions):
            assert any(p["kind"] == "scheduler_validate" and p.get("job") == it.job for p in it.parity), it.name


def test_a_cutover_is_refused_while_scheduler_validate_is_red_and_says_why(rig):
    r = rig([job_item()], validate=lambda: ["jobs.toml ignored, nothing will be scheduled from it: the file is not owned by root"])
    r.sys.add_unit("jb.timer")
    out = r.m.cutover("jb", apply=True)
    assert out.refused and "parity is not green" in out.refused and "scheduler_validate" in "\n".join(out.lines) and r.sys.calls == []


def test_the_scheduled_parity_is_honest_that_it_only_proves_the_scheduler_knows_the_job():
    ex = lambda now: [{"job": "backup-system", "mode": "observe", "next_due": NOW + 100}]        # noqa: E731
    c = chk({"kind": "scheduled", "job": "backup-system"}, explain=ex)
    assert c.ok is True and "mode observe" in c.detail and "not proof that it runs" in c.detail


def test_the_real_backups_are_gated_on_equivalence_verified_runs_and_the_kill_switch():
    by = {i.name: i for i in real().items}
    for n in ("backup-system", "backup-immich", "stack-backup"):
        kinds = [(p["kind"], p.get("attr")) for p in by[n].parity]
        assert ("unit_equiv", None) in kinds and ("job_attr", "pausable") in kinds and ("scheduler_validate", None) in kinds, n
        assert by[n].verified_runs == 2, n
    for n in ("backup-system", "backup-immich"):
        assert by[n].backup and by[n].soak_days == 14
    # the failure hook now waits for BOTH backups directly (it only depended on the wrapper, which depends on them)
    assert {"backup-notify-wrapper", "backup-system", "backup-immich"} <= set(by["backup-failure-hook"].depends_on)
    md = legacy.runbook(real())
    sect = md.split("#### `backup-system`")[1].split("#### ")[0]
    assert "start the first run attended, `homelab-maint job run backup-system`" in sect and "waits for 2 green run(s) recorded by the tick" in sect
    assert "`unit_equiv`" not in sect and "launches exactly what `backup-system.service` launches" in sect


# =========================================================================== REVIEW FIX 3: the alarm chain must not hang off the tick alone
def test_the_check_timer_is_never_retired_and_the_tick_handovers_all_wait_for_its_adoption():
    by = {i.name: i for i in real().items}
    tc = by["tier-check"]
    assert tc.mode == "keep" and tc.actions == [] and not tc.retirable and tc.keeps_job == "tier-check" and not tc.job
    kinds = {p["kind"] for p in tc.parity}
    assert {"scheduler_health", "scheduler_validate", "command", "probe", "notify"} <= kinds
    assert any(p["kind"] == "probe" and p["names"] == ["umbrella-tick"] for p in tc.parity), "the probe that judges tick.json is the alarm for a dead tick"
    assert any(p["kind"] == "command" and p["argv"][:2] == ["systemctl", "is-enabled"] and "homelab-maint-check.timer" in p["argv"] for p in tc.parity)
    # everything the tick takes over sits behind it: the harmless job first, then the rest transitively
    assert "tier-check" in by["search-canary"].depends_on
    chain = {"search-canary": "tier-check", "stack-watchdog": "search-canary", "stack-backup": "stack-watchdog", "tier-daily": "stack-watchdog",
             "metrics-sample": "stack-watchdog", "tier-weekly": "tier-daily", "backup-system": "stack-backup", "backup-immich": "backup-system"}
    for child, parent in chain.items():
        assert parent in by[child].depends_on, (child, parent)

    def ancestors(n, seen=()):
        out = set()
        for d in by[n].depends_on:
            out |= {d} | ancestors(d)
        return out
    for n in ("stack-watchdog", "stack-backup", "tier-daily", "tier-weekly", "metrics-sample", "backup-system", "backup-immich"):
        assert "tier-check" in ancestors(n), n
    disabled = {a["unit"] for i in by.values() for a in i.actions if a["do"] == "disable"}
    assert "homelab-maint-check.timer" not in disabled and legacy.NEVER_DISABLE.match("homelab-maint-check.timer")
    assert "retire = " not in legacy.RUNNER_TIMERS.pattern and not legacy.RUNNER_TIMERS.fullmatch("homelab-maint-check.timer")


def test_keeps_job_is_only_for_keep_items_and_excludes_a_handover():
    ok = {"name": "k", "kind": "timer", "location": "x.timer", "mode": "keep", "wave": 0, "unmonitored": "n", "keeps_job": "a-job"}
    assert parse_inventory({"item": [ok]}).get("k").keeps_job == "a-job"
    assert "keeps_job" in bad([{**ok, "mode": "port", "unmonitored": None, "retire_actions": [{"do": "disable", "unit": "x.timer"}],
                                "parity_check": [{"kind": "none", "note": "t"}]}])
    assert "keeps_job" in bad([{**ok, "keeps_job": "Bad Name"}])
    assert "keeps_job" in bad([item("x", keeps_job="a-job")])


def test_the_check_tier_adoption_records_without_touching_a_unit_and_goes_through_parity(rig):
    r = rig([{"name": "tc", "kind": "timer", "location": "homelab-maint-check.timer", "mode": "keep", "wave": 4, "keeps_job": "tc-job",
              "parity_check": [{"kind": "scheduler_validate"}, {"kind": "scheduler_health"}]}], validate=lambda: ["jobs.toml unreadable: x"],
           health=lambda now: ("ok", "tick ran 3s ago"))
    out = r.m.cutover("tc", apply=True)
    assert out.refused and r.sys.calls == [] and "scheduler_validate" in "\n".join(out.lines)         # a typo in jobs.toml: the tick would schedule nothing
    r2 = Rig(r.tmp / "again", [{"name": "tc", "kind": "timer", "location": "homelab-maint-check.timer", "mode": "keep", "wave": 4, "keeps_job": "tc-job",
                                "parity_check": [{"kind": "scheduler_validate"}, {"kind": "scheduler_health"}]}], validate=lambda: [],
             health=lambda now: ("ok", "tick ran 3s ago")) if (r.tmp / "again").mkdir() is None else None
    assert r2.m.cutover("tc", apply=True).ok and r2.sys.calls == [] and r2.state()["items"]["tc"]["state"] == "adopted"


def test_a_rollback_works_when_jobs_toml_no_longer_loads(rig):
    """The tick silences itself exactly when jobs.toml breaks, and that is when the owner needs the job back. The undo of a job hand-over
    only touches job-modes.json, so it must not need a valid jobs.toml."""
    r = rig([job_item()])
    r.sys.add_unit("jb.timer")
    assert r.m.cutover("jb", apply=True).ok and r.jobs.overrides == {"jb-job": "managed"}
    r.jobs.known = lambda: (_ for _ in ()).throw(RuntimeError("jobs.toml is broken"))               # every jobs.toml read now fails
    out = r.m.rollback("jb", apply=True)
    assert out.ok, out.lines
    assert r.jobs.overrides == {} and at_rest(r, "jb.timer") == ("enabled", "active")


def test_the_shipped_check_job_stays_in_observe_with_its_timer_as_driver():
    j = real_jobs()["tier-check"]
    assert j.mode == "observe" and j.retire == ["system:homelab-maint-check.timer"]
    shipped = (ROOT / "systemd" / "homelab-maint-check.timer").read_text()
    assert "OnCalendar" in shipped or "OnUnitActiveSec" in shipped


# =========================================================================== REVIEW FIX 4: never act under a backup the TICK is running
def test_a_job_the_tick_is_running_blocks_cutover_and_rollback_even_with_force(rig):
    """Once backup-system is a tick job it is never backup-system.service again, so the unit check alone always said 'idle'."""
    r = rig([job_item(require_idle=["jb.service"])])
    r.sys.add_unit("jb.timer")
    r.sys.add_unit("jb.service", enabled="static", active="inactive")
    r.running_jobs["jb-job"] = "pid 4242, since 2026-10-02T01:00:00-0400"
    out = r.m.cutover("jb", apply=True, force=True, reason="really want it now")
    assert out.refused and "job jb-job is running under the tick (pid 4242" in out.refused and r.sys.calls == [] and r.jobs.calls == []
    del r.running_jobs["jb-job"]
    assert r.m.cutover("jb", apply=True).ok
    r.running_jobs["jb-job"] = "pid 4243"
    calls = len(r.sys.calls)
    out = r.m.rollback("jb", apply=True, force=True, reason="incident: umbrella misbehaving")
    assert out.refused and "never roll back under a running job" in out.refused and len(r.sys.calls) == calls and r.jobs.overrides == {"jb-job": "managed"}
    assert at_rest(r, "jb.timer") == ("disabled", "inactive"), "the legacy timer did not come back under a running backup"
    del r.running_jobs["jb-job"]
    assert r.m.rollback("jb", apply=True).ok and at_rest(r, "jb.timer") == ("enabled", "active")


def test_the_rollback_dry_run_reports_the_busy_job_but_still_prints_the_plan(rig):
    r = rig([job_item()])
    r.sys.add_unit("jb.timer")
    assert r.m.cutover("jb", apply=True).ok
    r.running_jobs["jb-job"] = "pid 1"
    out = r.m.rollback("jb")
    text = "\n".join(out.lines)
    assert out.ok and "NOTE: --apply would be refused right now: job jb-job is running under the tick" in text and "$ systemctl enable jb.timer" in text


def test_a_held_backup_lock_blocks_backup_items_only(rig):
    """A backup started by hand (or by the legacy unit, or by a tick job of another item) holds /run/lock/backup-*.lock."""
    r = rig([item("bk", mode="adapter", backup=True), item("other")])
    r.sys.add_unit("bk.timer")
    r.sys.add_unit("other.timer")
    r.locks.append("/run/lock/backup-system.lock")
    out = r.m.cutover("bk", apply=True, force=True, reason="forced anyway ok")
    assert out.refused and "a backup is running: backup-system.lock is held" in out.refused and r.sys.calls == []
    assert r.m.cutover("other", apply=True).ok                      # an unrelated item does not care about backup locks
    r.locks.clear()
    assert r.m.cutover("bk", apply=True).ok
    r.locks.append("/run/lock/backup-immich.lock")
    assert "backup-immich.lock" in (r.m.rollback("bk", apply=True).refused or "")


def test_the_wrappers_are_blocked_by_both_backup_jobs_through_their_job_names(rig):
    r = rig([item("wrap", mode="retire", kind="script", actions=[{"do": "move", "src": "/usr/local/sbin/w.sh"}],
                  require_idle_jobs=["backup-system", "backup-immich"], backup=True)])
    r.put("/usr/local/sbin/w.sh")
    r.running_jobs["backup-immich"] = "pid 77"
    out = r.m.cutover("wrap", apply=True)
    assert out.refused and "job backup-immich is running under the tick" in out.refused and r.exists("/usr/local/sbin/w.sh")
    del r.running_jobs["backup-immich"]
    assert r.m.cutover("wrap", apply=True).ok
    r.running_jobs["backup-system"] = "pid 78"
    assert "job backup-system is running" in r.m.rollback("wrap", apply=True).refused and not r.exists("/usr/local/sbin/w.sh")
    for n in ("backup-notify-wrapper", "backup-failure-hook"):
        it = {i.name: i for i in real().items}[n]
        assert set(it.require_idle_jobs) == {"backup-system", "backup-immich"} and it.backup, n


def test_an_unknowable_state_counts_as_busy(rig):
    r = rig([job_item()], job_running=lambda name: (_ for _ in ()).throw(OSError("sched.json unreadable")))
    r.sys.add_unit("jb.timer")
    out = r.m.cutover("jb", apply=True)
    assert out.refused and "cannot tell" in out.refused and r.sys.calls == []
    r2 = Rig(r.tmp / "b", [item("bk", mode="adapter", backup=True)], locks=lambda: ([], "cannot read /proc/locks (OSError)")) if (r.tmp / "b").mkdir() is None else None
    r2.sys.add_unit("bk.timer")
    assert "cannot read /proc/locks" in r2.m.cutover("bk", apply=True).refused


def test_the_default_job_running_probe_needs_a_live_process_not_just_a_record(tmp_path, monkeypatch):
    """A record left behind by a tick that died must not block a rollback forever; a live supervisor must."""
    from homelab_maint import jobs
    monkeypatch.setattr(core, "STATE_DIR", tmp_path)
    live = {"pid": os.getpid(), "sup_start": jobs.proc_start(os.getpid()), "started": NOW - 5, "run": None}
    write = lambda r: core.write_json_atomic(tmp_path / "sched.json", {"schema": 1, "meta": {}, "jobs": {"x-job": {"running": r}} if r else {"x-job": {}}})   # noqa: E731
    s = Sources(now=lambda: NOW)
    write(None)
    assert s.job_running("x-job") == (False, "")
    write(live)
    running, why = s.job_running("x-job")
    assert running is True and f"pid {os.getpid()}" in why
    write({**live, "pid": 4_194_303, "sup_start": 1})                                       # nobody lives there
    assert s.job_running("x-job") == (False, "")
    write({**live, "pid": None, "started": NOW - 3})                                        # intent saved, not spawned yet: busy
    assert s.job_running("x-job")[0] is True
    write({**live, "pid": None, "started": NOW - 300})
    assert s.job_running("x-job")[0] is False
    assert s.job_running("unknown-job") == (False, "")


def test_the_tick_is_held_while_a_job_is_handed_over_or_taken_back(rig):
    """The tick must not launch the job between the idle check and the switch."""
    r = rig([job_item(), item("plain")])
    for u in ("jb.timer", "plain.timer"):
        r.sys.add_unit(u)
    r.m.cutover("jb")                                                                       # a dry run holds nothing
    assert r.tick_events == []
    assert r.m.cutover("jb", apply=True).ok
    assert r.tick_events == ["hold", "release"]
    assert r.m.rollback("jb", apply=True).ok
    assert r.tick_events == ["hold", "release", "hold", "release"]
    assert r.m.cutover("plain", apply=True).ok and len(r.tick_events) == 4                  # nothing to hand over: nothing to hold


def test_a_tick_that_is_mid_run_makes_the_cutover_wait_then_refuse(tmp_path):
    """The real tick.lock (flock in RUN_DIR): held by another descriptor, the cutover refuses without changing anything."""
    import fcntl
    r = Rig(tmp_path, [job_item()])
    r.m._tick_lock = None                                                                     # use scheduler.tick_lock for real
    r.m.tick_wait_s = 0.3
    r.sys.add_unit("jb.timer")
    core.RUN_DIR.mkdir(parents=True, exist_ok=True)
    with open(core.RUN_DIR / "tick.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out = r.m.cutover("jb", apply=True)
        assert out.refused and "tick holds its lock" in out.refused and r.sys.calls == [] and r.jobs.calls == []
    assert r.m.cutover("jb", apply=True).ok                                                   # released: goes through
    assert r.m.rollback("jb", apply=True).ok


# =========================================================================== REVIEW FIX 5: PAUSE must not silently stop the backups
def test_the_status_report_shouts_when_a_kill_switch_is_on(rig, capsys):
    r = rig([job_item(), item("plain")])
    r.sys.add_unit("jb.timer")
    r.sys.add_unit("plain.timer")
    assert r.m.cutover("jb", apply=True).ok
    assert r.m.pauses() == [] and legacy.main(["status", "--fast"], migrator=r.m) == 0
    assert "!!" not in capsys.readouterr().out
    (r.conf / "PAUSE").write_text("")
    (r.conf / "PAUSE.jb-job").write_text("")
    assert r.m.pauses() == ["PAUSE", "PAUSE.jb-job"]
    assert legacy.main(["status", "--fast"], migrator=r.m) == 0
    out = capsys.readouterr().out
    assert "!! PAUSE, PAUSE.jb-job present" in out and "backups included unless the job has pausable = false" in out
    rows = {x["name"]: x for x in r.m.rows(live=False)}
    assert rows["jb"].get("paused") is True and "paused" not in rows["plain"]
    assert "PAUSED: the tick will not start it" in legacy.render_status(r.m.rows(live=False))
    assert r.m.export()["paused"] is True
    (r.conf / "PAUSE").unlink()
    (r.conf / "PAUSE.jb-job").unlink()
    assert r.m.export()["paused"] is False and "paused" not in {x["name"]: x for x in r.m.rows(live=False)}["jb"]
    assert legacy.main(["status", "--json"], migrator=r.m) == 0 and json.loads(capsys.readouterr().out.split("\n", 0)[0])[0]["name"]


def test_every_real_backup_item_requires_the_job_to_ignore_the_global_kill_switch():
    for n in ("backup-system", "backup-immich", "stack-backup"):
        it = {i.name: i for i in real().items}[n]
        want = [p for p in it.parity if p["kind"] == "job_attr" and p["attr"] == "pausable"]
        assert want == [{"kind": "job_attr", "job": n, "attr": "pausable", "equals": False}], n
    # the check is real: jobs.toml ships pausable (the default) for them, so the cutover stays refused until the scheduler glue sets false
    jobs_ = real_jobs()
    assert {n: jobs_[n].pausable for n in ("backup-system", "backup-immich", "stack-backup")} == {"backup-system": True, "backup-immich": True, "stack-backup": True} \
        or all(jobs_[n].pausable is False for n in ("backup-system", "backup-immich", "stack-backup"))
    spec = {"kind": "job_attr", "job": "backup-system", "attr": "pausable", "equals": False}
    assert chk(spec, job_attr=lambda j, a: True).ok is False and chk(spec, job_attr=lambda j, a: False).ok is True


def test_the_backup_lock_reader_sees_a_really_held_flock_and_ignores_a_free_one(tmp_path):
    import fcntl
    lock = tmp_path / "backup-demo.lock"
    lock.write_text("")
    pat = (str(tmp_path / "backup-*.lock"),)
    assert legacy._locked_backup_files(pat) == ([], "")
    with open(lock) as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert legacy._locked_backup_files(pat) == ([str(lock)], "")
    assert legacy._locked_backup_files(pat) == ([], "")
    assert legacy._locked_backup_files((str(tmp_path / "none-*"),)) == ([], "")


def test_a_rollback_still_works_when_the_scheduler_module_does_not_import(rig, monkeypatch):
    """A bad deploy of scheduler.py is exactly when the owner needs the job back. The busy checks and the tick hold read sched.json and
    /proc themselves, and no tick can run without that module, so there is nothing to hold."""
    r = rig([job_item()])
    r.sys.add_unit("jb.timer")
    assert r.m.cutover("jb", apply=True).ok
    r.m._tick_lock = None
    r.m.src._job_running = r.m.src._locks = None                       # the default readers, not the injected fakes
    monkeypatch.setitem(sys.modules, "homelab_maint.scheduler", None)  # `from . import scheduler` now raises ImportError
    out = r.m.rollback("jb", apply=True)
    assert out.ok, out.lines
    assert at_rest(r, "jb.timer") == ("enabled", "active") and r.jobs.overrides == {}


def test_export_names_an_adoptable_keep_item_others_wait_for_as_the_next_step(rig):
    """Everything in wave 4 waits for `tier-check` (a keep item): the website must not say 'next: nothing' while that is the only step available."""
    r = rig([{"name": "gate", "kind": "timer", "location": "g.timer", "mode": "keep", "wave": 1, "unmonitored": "n"}, item("a", wave=4, depends_on=["gate"])])
    r.sys.add_unit("a.timer")
    assert r.m.export()["next"] == "gate"
    assert r.m.cutover("gate", apply=True).ok
    assert r.m.export()["next"] == "a"
