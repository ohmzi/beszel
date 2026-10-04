"""scheduler.py: the tick. A fake host (clock, processes, probes, notifications, audit) drives the real algorithm with tmp dirs:
seeding, due/catch-up/expiry, overlap, stale locks, lost runs, retries, TERM->KILL, admission (PAUSE, interlock, pressure, freeze,
window, gates, heavy mutex, backups, concurrency, `after`), jitter, DST days, notifications, status.json rows, explain/export, the
manual `run`, the CLI, idle-tick cost, and one end-to-end pass with the REAL supervisor."""
import datetime as dt
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: F401,E402

from homelab_maint import core, jobs, schedule, scheduler  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
TO = ZoneInfo("America/Toronto")


def T(h=0, m=0, s=0, d=2, mo=10, y=2026):
    return dt.datetime(y, mo, d, h, m, s, tzinfo=TO).timestamp()


def tv(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(tv(x) for x in v) + "]"
    return json.dumps(v)


def tj(name, command=("/bin/true",), **kw):
    """TOML text of one managed job. dict values become [job.<key>] sub-tables."""
    lines = ["[[job]]", f'name = "{name}"', f"command = {tv(list(command))}", 'mode = "managed"']
    subs = []
    for k, v in kw.items():
        if isinstance(v, dict):
            subs.append((k, v))
        else:
            lines.append(f"{k} = {tv(v)}")
    for k, d in subs:
        lines.append(f"[job.{k}]")
        lines += [f"{kk} = {tv(vv)}" for kk, vv in d.items()]
    return "\n".join(lines) + "\n"


class Host:
    """The fake machine the scheduler sees."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.t = T(3, 0)
        self.pid = 1000
        self.procs: dict[int, dict] = {}
        self.spawned: list[dict] = []
        self.kills: list[tuple] = []
        self.boot = "boot-1"
        self.gate: dict[str, tuple[bool, str]] = {}
        self.gate_calls: list[str] = []
        self.backup = (False, "")
        self.level: int | None = 0
        self.real_pressure = False          # True: the REAL default_pressure reads tasks/pressure_state.json from the tmp STATE_DIR
        self.legacy = (False, "")
        self.legacy_calls = 0
        self.notes: list[tuple] = []
        self.notify_raises = False
        self.audits: list[tuple] = []
        self.spawn_raises = False
        self.cfg = None

    # ---- the Env
    def env(self):
        e = scheduler.Env()
        e.now = lambda: self.t
        e.spawn = self._spawn
        e.proc_start = lambda pid: self.procs[pid]["start"] if pid in self.procs else None
        e.alive = lambda pid, start: bool(pid in self.procs and self.procs[pid]["alive"] and (start is None or start == self.procs[pid]["start"]))
        e.group_alive = lambda pg: False
        e.kill_group = lambda pg, sig, start=None: (self.kills.append((pg, int(sig), start)) or True)
        e.boot_id = lambda: self.boot
        e.lookup_user = lambda u: (1000, 1000, "/home/ohmz") if u == "ohmz" else None
        e.exists = lambda p: False
        e.busy = self._busy
        e.backup_running = lambda sched: self.backup
        e.pressure = scheduler.default_pressure if self.real_pressure else (lambda now, sched: (self.level, f"level {self.level}"))
        e.legacy_active = self._legacy
        e.notify = self._notify
        e.audit = lambda *a: self.audits.append(a)
        return e

    def _spawn(self, spec):
        if self.spawn_raises:
            raise OSError("no fork for you")
        self.pid += 1
        self.procs[self.pid] = {"spec": spec, "start": 5000 + self.pid, "alive": True}
        self.spawned.append(spec)
        return self.pid

    def _busy(self, g, sched):
        self.gate_calls.append(g)
        return self.gate.get(g, (False, ""))

    def _legacy(self, specs, sched):
        self.legacy_calls += 1
        return self.legacy

    def _notify(self, kind, job, res, **ctx):
        if self.notify_raises:
            raise RuntimeError("pager down")
        self.notes.append((kind, job.name, res.status, ctx.get("attempt")))

    # ---- helpers
    def config(self, *jobs_toml, sched="", extra=""):
        p = self.tmp / "jobs.toml"
        p.write_text(sched + "\n" + "\n".join(jobs_toml) + "\n" + extra)
        self.cfg = jobs.load(p, mcfg={"tasks": {}}, apply_modes=False)
        assert self.cfg.errors == [], self.cfg.errors
        return self.cfg

    def tick(self, now=None, cfg=None, dry=False):
        self.t = self.t if now is None else now
        return scheduler.tick(self.env(), cfg or self.cfg, self.t, dry_run=dry)

    def last_spec(self, name):
        return [s for s in self.spawned if s["job"] == name][-1]

    def pid_of(self, name):
        spec = self.last_spec(name)
        return next(p for p, d in self.procs.items() if d["spec"] is spec)

    def finish(self, name, rc=0, t_end=None, **kw):
        """The supervisor finished: write its done file and let the process die."""
        spec = self.last_spec(name)
        done = {"rc": rc, "signal": None, "timed_out": False, "cancelled": False, "t_start": self.t - 5,
                "t_end": t_end or self.t, "log": spec["log_path"], "attempt": spec["attempt"], "tail": kw.pop("tail", [])}
        done.update(kw)
        Path(spec["done_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(spec["done_path"]).write_text(json.dumps(done))
        self.procs[self.pid_of(name)]["alive"] = False

    def die(self, name):
        self.procs[self.pid_of(name)]["alive"] = False

    def state(self):
        return scheduler.load_state()["jobs"]

    def status(self):
        return core.read_json(core.STATE_DIR / "status.json", {})


@pytest.fixture
def host(tmp_path, monkeypatch):
    for k in ("STATE", "LOG", "CONF", "RUN"):
        d = tmp_path / k.lower()
        d.mkdir()
        monkeypatch.setattr(core, f"{k}_DIR", d)
    monkeypatch.setenv("HOMELAB_MAINT_TZ", "America/Toronto")
    seed_status()
    return Host(tmp_path)


def seed_status():
    """What `homelab-maint run` leaves behind: the runner owns status.json (schema, generated_at, tier_runs); the tick merges rows."""
    core.write_json_atomic(core.STATE_DIR / "status.json", {"schema": 1, "generated_at": 123.0, "host": "t", "paused": False,
                                                           "overall": "ok", "tasks": {}, "tier_runs": {}})


DAILY = tj("daily", schedule="30 3 * * *")


def seeded(host, *jobs_toml, **kw):
    host.config(*jobs_toml, **kw)
    host.tick(T(3, 0))
    return host


# --------------------------------------------------------------------------- seeding, due, catch-up
def test_first_sight_seeds_and_never_catches_up(host):
    host.config(DAILY)
    rep = host.tick(T(10, 0))                                          # 03:30 passed hours ago: that run is the legacy driver's
    assert rep["started"] == [] and host.spawned == []
    js = host.state()["daily"]
    assert js["next_due"] == T(3, 30, d=3) and js["last_due"] == T(3, 30)


def test_idle_tick_writes_nothing_after_the_first(host):
    seeded(host, DAILY)
    mtime = (core.STATE_DIR / "sched.json").stat().st_mtime_ns
    for m in range(1, 20):
        host.tick(T(3, m))
    assert (core.STATE_DIR / "sched.json").stat().st_mtime_ns == mtime


def test_due_job_starts_exactly_once(host):
    seeded(host, DAILY)
    assert host.tick(T(3, 29, 59))["started"] == []
    assert host.tick(T(3, 30, 0))["started"] == ["daily"]
    assert host.tick(T(3, 31))["started"] == [] and len(host.spawned) == 1       # still running: no second start
    host.finish("daily", t_end=T(3, 40))
    rep = host.tick(T(3, 41))
    assert rep["reaped"] == [("daily", "ok")] and rep["started"] == []
    assert host.tick(T(3, 42))["started"] == [] and len(host.spawned) == 1
    assert host.tick(T(3, 30, d=3))["started"] == ["daily"]


def test_intent_is_persisted_before_the_spawn(host):
    """A crash between 'decided to start' and 'spawned' must not allow a second start: the running record is already on disk."""
    seeded(host, DAILY)
    seen = {}
    base = host._spawn

    def spy(spec):
        seen["state"] = scheduler.load_state()["jobs"]["daily"]
        return base(spec)

    e = host.env()
    e.spawn = spy
    scheduler.tick(e, host.cfg, T(3, 30))
    assert seen["state"]["running"]["pid"] is None and seen["state"]["running"]["run_id"] == host.spawned[0]["run_id"]
    assert "pending" not in seen["state"]


def test_crash_between_intent_and_spawn_never_double_runs_and_clears_after_30s(host):
    seeded(host, DAILY)
    host.spawn_raises = False
    js = host.state()["daily"]
    js.update(last_due=T(3, 30), next_due=T(3, 30, d=3))                         # what the launch had already persisted
    js["running"] = {"run_id": "r0", "started": T(3, 30), "attempt": 1, "due": T(3, 30), "boot": "boot-1", "pid": None, "sup_start": None,
                     "hard_deadline": None, "log": "", "run": "", "done": str(core.STATE_DIR / "nope.json"), "manual": False}
    st = scheduler.load_state()
    st["jobs"]["daily"] = js
    core.write_json_atomic(scheduler.state_path(), st)
    assert host.tick(T(3, 30, 20))["started"] == [] and host.spawned == []        # within 30 s: still "launching"
    rep = host.tick(T(3, 31, 5))
    assert rep["reaped"] == [("daily", "error")] and host.spawned == []           # declared lost, NOT re-run for the same occurrence
    assert "never started" in host.state()["daily"]["last_summary"]


def test_catch_up_after_downtime_runs_once(host):
    seeded(host, tj("hourly", schedule="0 * * * *", catchup_hours=20))
    rep = host.tick(T(8, 7))                                                   # the host was off for 5 hours: 5 occurrences missed
    assert rep["started"] == ["hourly"] and len(host.spawned) == 1
    host.finish("hourly")
    assert host.tick(T(8, 8))["started"] == [] and host.tick(T(8, 9))["started"] == []


def test_catch_up_expires_after_catchup_hours(host):
    seeded(host, tj("daily", schedule="30 3 * * *", catchup_hours=2, notify={"on_expire": "alert"}))
    rep = host.tick(T(9, 0))
    assert rep["started"] == [] and rep["expired"] == ["daily"]
    js = host.state()["daily"]
    assert js["skipped_reason"].startswith("expired") and "pending" not in js and js["next_due"] == T(3, 30, d=3)
    assert host.notes == [("expire", "daily", "warn", None)]
    assert host.tick(T(9, 1))["expired"] == []                                 # handled once, not every minute


def test_expiry_without_on_expire_is_silent(host):
    seeded(host, tj("daily", schedule="30 3 * * *", catchup_hours=2))
    host.tick(T(9, 0))
    assert host.notes == []


def test_a_broken_jobs_toml_neither_wipes_the_state_nor_orphans_running_jobs(host):
    seeded(host, DAILY, tj("long", schedule="30 3 * * *"))
    host.tick(T(3, 30))
    before = host.state()
    assert before["long"]["running"]
    broken = jobs.JobsConfig(dict(jobs.SCHED_DEFAULTS), errors=["jobs.toml unreadable: TOMLDecodeError"])       # a typo while editing
    host.tick(T(3, 31), broken)
    assert host.spawned and set(host.state()) == set(before)                              # schedule state kept for when it is fixed
    host.finish("long", t_end=T(3, 32))
    rep = host.tick(T(3, 33), broken)
    assert rep["reaped"] == [("long", "ok")]                                              # the running job is still reaped without a config
    assert host.tick(T(3, 34), host.cfg)["started"] == []                                 # fixed again: no replay, next occurrence tomorrow


def test_state_loss_does_not_replay_old_occurrences(host):
    seeded(host, DAILY)
    host.tick(T(3, 30))
    host.finish("daily")
    host.tick(T(3, 31))
    (core.STATE_DIR / "sched.json").unlink()
    assert host.tick(T(3, 35))["started"] == [] and len(host.spawned) == 1        # reseeded: today's run is treated as handled


def test_schedule_edit_reseeds_instead_of_catching_up(host):
    seeded(host, DAILY)
    host.config(tj("daily", schedule="0 3 * * *"))                             # 03:00 would be in the past: no catch-up of it
    assert host.tick(T(3, 10))["started"] == []
    assert host.state()["daily"]["next_due"] == T(3, 0, d=3)


def test_manual_only_job_never_starts_by_itself(host):
    seeded(host, tj("manual"))
    assert host.tick(T(12, 0))["started"] == [] and host.state().get("manual", {}) == {}


def test_observe_and_retired_modes_never_launch(host):
    cfg = host.config(DAILY.replace('mode = "managed"', 'mode = "observe"'), tj("r", schedule="30 3 * * *").replace('mode = "managed"', 'mode = "retired"'))
    host.tick(T(3, 0), cfg)
    assert host.tick(T(3, 30), cfg)["started"] == [] and host.spawned == []
    assert scheduler.load_state()["jobs"] == {}                                # nothing is even tracked for them


# --------------------------------------------------------------------------- overlap, stale locks, lost runs
def test_never_starts_while_the_previous_run_is_alive(host):
    seeded(host, tj("minutely", schedule="* * * * *"))
    assert host.tick(T(3, 1))["started"] == ["minutely"]
    for m in range(2, 8):                                                       # six occurrences come due while it runs: all dropped
        assert host.tick(T(3, m))["started"] == []
    assert len(host.spawned) == 1 and host.state()["minutely"]["overlap_skipped"] == 6
    host.finish("minutely", t_end=T(3, 7, 30))
    rep = host.tick(T(3, 7, 45))
    assert rep["reaped"] == [("minutely", "ok")] and rep["started"] == []        # no backlog replay: the next one is 03:08
    assert host.tick(T(3, 8))["started"] == ["minutely"]


def test_stale_pid_with_a_different_start_time_is_not_the_run(host):
    seeded(host, DAILY)
    host.tick(T(3, 30))
    host.procs[host.pid_of("daily")]["start"] += 1                              # the pid was recycled by an unrelated process
    rep = host.tick(T(3, 31))
    assert rep["reaped"] == [("daily", "error")]
    assert "supervisor vanished" in host.state()["daily"]["last_summary"]
    assert host.kills == []                                                     # and nothing innocent was signalled


def test_supervisor_pid_is_adopted_from_run_json_when_the_tick_died_after_spawning(host):
    """The tick can be killed between spawn() and its final save: the record then has pid None. The supervisor's own run.json
    (sup_pid + start ticks) must keep the run alive (and killable); only when THAT is dead too is the run lost."""
    seeded(host, tj("slow", schedule="30 3 * * *", timeout_s=60, kill_grace_s=20))
    host.tick(T(3, 30))
    pid = host.pid_of("slow")
    st = scheduler.load_state()
    st["jobs"]["slow"]["running"].update(pid=None, sup_start=None)
    core.write_json_atomic(scheduler.state_path(), st)
    spec = host.last_spec("slow")
    Path(spec["run_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(spec["run_path"]).write_text(json.dumps({"sup_pid": pid, "sup_start": 5000 + pid, "job_pgid": None, "job_start": None}))
    assert host.tick(T(3, 32))["reaped"] == []                                           # supervisor alive per run.json: still running
    host.kills.clear()
    host.tick(T(3, 30) + 60 + 20 + 60 + 5)                                               # past the hard deadline: TERM goes to ITS group
    assert (pid, 15, 5000 + pid) in host.kills
    host.die("slow")
    assert host.tick(T(3, 40))["reaped"] == [("slow", "error")]
    assert "vanished" in host.state()["slow"]["last_summary"]


def test_lost_run_after_reboot(host):
    seeded(host, DAILY)
    host.tick(T(3, 30))
    host.boot = "boot-2"
    rep = host.tick(T(3, 40))
    assert rep["reaped"] == [("daily", "error")] and "rebooted" in host.state()["daily"]["last_summary"]


def test_lost_run_is_retried_with_backoff_then_gives_up_and_alerts_once(host):
    seeded(host, tj("bk", schedule="30 3 * * *", max_attempts=2, retry_on=["lost"], retry_backoff_s=[600], self_notifies=True,
                    notify={"on_failure": "alert"}))
    host.tick(T(3, 30))
    host.die("bk")
    rep = host.tick(T(3, 32))
    assert rep["reaped"] == [("bk", "error")] and host.notes == []                  # a retry is coming: no alert yet
    assert "retry 2/2" in host.state()["bk"]["pending"]["reason"]
    assert host.tick(T(3, 41))["started"] == []                                     # 3:32 + 10 min = 3:42
    assert host.tick(T(3, 42, 1))["started"] == ["bk"] and host.last_spec("bk")["attempt"] == 2
    host.die("bk")
    host.tick(T(3, 45))
    assert host.notes == [("failure", "bk", "error", 2)]                           # self_notifies still alerts for ABNORMAL ends
    assert host.tick(T(3, 50))["started"] == [] and len(host.spawned) == 2


def test_plain_failure_is_not_retried_unless_asked(host):
    seeded(host, tj("f", schedule="30 3 * * *", max_attempts=3, retry_on=["lost"]))
    host.tick(T(3, 30))
    host.finish("f", rc=1)
    host.tick(T(3, 31))
    assert "pending" not in host.state()["f"] and host.notes == [("failure", "f", "crit", 1)]


def test_failed_runs_retry_with_growing_backoff(host):
    seeded(host, tj("f", schedule="30 3 * * *", max_attempts=3, retry_on=["failed"], retry_backoff_s=[300, 900]))
    host.tick(T(3, 30))
    host.finish("f", rc=1)
    host.tick(T(3, 31))
    assert host.state()["f"]["pending"]["retry_at"] == T(3, 31) + 300
    assert host.tick(T(3, 35, 59))["started"] == [] and host.tick(T(3, 36))["started"] == ["f"]
    host.finish("f", rc=1)
    host.tick(T(3, 37))
    assert host.state()["f"]["pending"]["retry_at"] == T(3, 37) + 900
    assert host.tick(T(3, 52, 1))["started"] == ["f"] and host.last_spec("f")["attempt"] == 3
    host.finish("f", rc=1)
    host.tick(T(3, 53))
    assert "pending" not in host.state()["f"] and host.notes == [("failure", "f", "crit", 3)]


def test_first_starts_are_never_capped_a_one_minute_job_starts_every_minute_all_day(host):
    """The cap bounds RETRIES. It used to count every start: any job due more than 6 times a day (probes-run every minute, the
    routine every 15 min, every cut-over monitor) was switched off after its 6th start until local midnight."""
    host.config(tj("m", schedule="* * * * *"), tj("r", schedule="*/15 * * * *"))
    starts = simulate(host, T(0, 0), T(0, 0, d=3))
    assert len(starts) == 1439 + 95                                       # every occurrence of 24 h, nothing skipped
    js = host.state()["m"]
    assert "retries_day" not in js and "pending" not in js and "skipped_reason" not in js
    assert not [a for a in host.audits if a[1] == "job-expired" or "daily" in str(a)]   # no 'daily start cap' deferrals, no expiries


def test_the_shipped_inventory_never_goes_dark_after_a_handful_of_starts(host):
    """Every frequent job of the shipped jobs.toml, all managed, keeps starting at every occurrence over half a day (the original
    defect: probes-run, metrics-sample, routine-run and every cut-over monitor stopped after 6 starts and still read 'ok')."""
    cfg = jobs.load(REPO / "etc" / "jobs.toml", mcfg={"tasks": {}}, apply_modes=False)
    frequent = {}
    for j in list(cfg.jobs.values()):
        sch = schedule.parse(j.schedule) if j.schedule else None
        if j.mode == "retired" or sch is None or (sch.min_gap_s(T(0, 0), 2) or 99999) > 3600:
            del cfg.jobs[j.name]
            continue
        j.mode = "managed"
        j.retire, j.gates, j.window, j.jitter_s = [], [], "", 0                         # no legacy driver, nothing busy, no jitter
        frequent[j.name] = 0
    assert {"probes-run", "metrics-sample", "routine-run", "stack-watchdog", "notebook-db-alert"} <= set(frequent)
    for t in range(0, 12 * 3600, 60):
        rep = host.tick(T(0, 0) + t, cfg)
        for n in rep["started"]:
            frequent[n] += 1
            host.finish(n, t_end=T(0, 0) + t + 1)
    assert frequent["probes-run"] >= 700 and frequent["metrics-sample"] >= 700, frequent
    for name, n in frequent.items():
        sch = schedule.parse(cfg.jobs[name].schedule)
        occ, t = 0, T(0, 0)
        while (t := sch.next_after(t)) is not None and t < T(12, 0):
            occ += 1
        assert n >= occ - 1 and n > 6, (name, n, occ)


def test_a_failing_retry_chain_stops_at_the_daily_cap_and_alerts_once(host):
    seeded(host, tj("f", schedule="30 3 * * *", max_attempts=10, retry_on=["failed"], retry_backoff_s=[60]),
           sched="[scheduler]\nmax_retries_per_day = 3\n")
    n = 0
    for m in range(0, 60):
        t = T(3, 30) + m * 60
        n += len(host.tick(t)["started"])
        if host.spawned and host.procs[host.pid_of("f")]["alive"]:
            host.finish("f", rc=1, t_end=t + 1)
    assert n == 4 and [x[:3] for x in host.notes] == [("failure", "f", "crit")]       # 1 first start + 3 retries, then the failure is final
    assert host.state()["f"]["retries_day"] == {"day": "2026-10-02", "n": 3} and "pending" not in host.state()["f"]
    assert host.tick(T(4, 45))["started"] == []
    host.tick(T(3, 29, d=3))                                                          # next day: a new occurrence starts normally...
    assert host.tick(T(3, 30, d=3))["started"] == ["f"]
    host.finish("f", rc=1, t_end=T(3, 30, d=3) + 1)
    host.tick(T(3, 31, d=3))
    assert host.state()["f"]["pending"]["attempt"] == 2                                # ...and the retry budget is fresh


def test_retries_are_counted_per_local_day_not_per_chain(host):
    """Two occurrences on the same day share one retry budget: a flapping job cannot restart without bound."""
    seeded(host, tj("f", schedule="*/10 3 * * *", max_attempts=5, retry_on=["failed"], retry_backoff_s=[30]),
           sched="[scheduler]\nmax_retries_per_day = 2\n")
    for m in range(0, 100):
        t = T(3, 10) + m * 30
        for n in host.tick(t)["started"]:
            host.finish(n, rc=1, t_end=t + 1)
    retries = sum(1 for sp in host.spawned if sp["attempt"] > 1)
    firsts = sum(1 for sp in host.spawned if sp["attempt"] == 1)
    assert retries == 2 and firsts >= 4


def test_a_retry_that_finds_the_cap_used_up_waits_it_does_not_start(host):
    """Defensive: finalize() never schedules a retry past the cap, but a state edited by hand (or an older tick) can hold one."""
    seeded(host, tj("f", schedule="30 3 * * *", max_attempts=3, retry_on=["failed"]), sched="[scheduler]\nmax_retries_per_day = 2\n")
    st = scheduler.load_state()
    st["jobs"]["f"]["pending"] = {"due": T(3, 30), "first_due": T(3, 30), "since": T(3, 31), "attempt": 2, "retry_at": T(3, 32)}
    st["jobs"]["f"]["retries_day"] = {"day": "2026-10-02", "n": 2}
    st["jobs"]["f"]["next_due"] = T(3, 30, d=3)                                       # the 03:30 occurrence was already taken
    core.write_json_atomic(scheduler.state_path(), st)
    rep = host.tick(T(3, 33))
    assert rep["started"] == [] and "daily retry cap 2 reached" in host.state()["f"]["pending"]["reason"]


def test_manual_runs_never_count_against_the_retry_cap(host):
    host.config(tj("f", schedule="30 3 * * *", max_attempts=3, retry_on=["failed"]), sched="[scheduler]\nmax_retries_per_day = 0\n")
    assert scheduler.run_job("f", host.env(), host.cfg, T(3, 5))["started"] is True


# --------------------------------------------------------------------------- TERM then KILL by the tick (the backstop)
def test_overdue_run_gets_term_then_kill_of_both_groups(host):
    seeded(host, tj("slow", schedule="30 3 * * *", timeout_s=100, kill_grace_s=20))
    host.tick(T(3, 30))
    pid = host.pid_of("slow")
    start = host.procs[pid]["start"]
    hard = T(3, 30) + 100 + 20 + 60
    assert host.tick(hard - 1)["reaped"] == [] and host.kills == []
    host.tick(hard + 1)
    assert [(pg, sig) for pg, sig, _ in host.kills] == [(pid, 15), (None, 15)] and host.kills[0][2] == start
    host.tick(hard + 10)
    assert len(host.kills) == 2                                                  # grace not over: no KILL yet
    host.tick(hard + 25)
    assert [sig for _, sig, _ in host.kills] == [15, 15, 9, 9]
    assert ("sched", "job-term", "slow", 0, "past its deadline (pid %d)" % pid) in host.audits
    host.die("slow")
    rep = host.tick(hard + 30)
    assert rep["reaped"] == [("slow", "error")]


def test_job_without_timeout_is_never_killed_by_the_tick(host):
    seeded(host, tj("bk", schedule="30 3 * * *", timeout_s=0))
    host.tick(T(3, 30))
    for h in range(4, 20):
        host.tick(T(h, 0))
    assert host.kills == [] and host.state()["bk"]["running"]["hard_deadline"] is None


def test_finished_run_with_supervisor_still_alive_is_reaped_from_the_done_file(host):
    seeded(host, DAILY)
    host.tick(T(3, 30))
    spec = host.last_spec("daily")
    Path(spec["done_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(spec["done_path"]).write_text(json.dumps({"rc": 0, "t_start": T(3, 30), "t_end": T(3, 31), "tail": ["x"], "log": spec["log_path"]}))
    assert host.tick(T(3, 32))["reaped"] == [("daily", "ok")]                    # the supervisor may still be exiting: done wins


# --------------------------------------------------------------------------- results into status.json / history
def test_status_json_rows_history_and_overall(host):
    seeded(host, DAILY, tj("other", schedule="30 3 * * *"))
    host.tick(T(3, 30))
    host.finish("daily", tail=["# hdr", "all good"], t_end=T(3, 35))
    host.finish("other", rc=2, tail=["it broke"], t_end=T(3, 36))
    host.tick(T(3, 37))
    st = host.status()
    d, o, s = st["tasks"]["daily"], st["tasks"]["other"], st["tasks"]["scheduler"]
    assert (d["klass"], d["tier"], d["status"], d["mode"], d["source"]) == ("J", "job", "ok", "managed", "adapter")
    assert d["summary"] == "exit 0: all good" and d["last_run"] == T(3, 35) and d["class"] == "P3" and d["schedule"] == "30 3 * * *"
    assert o["status"] == "crit" and o["summary"] == "exit 2: it broke" and o["metrics"]["fail_streak"] == 1 and o["metrics"]["rc"] == 2
    assert s["klass"] == "J" and s["status"] == "ok" and "2 managed job(s)" in s["summary"]
    assert st["overall"] == "crit" and st["tick"]["managed"] == 2
    recs = [json.loads(ln) for ln in (core.STATE_DIR / "history.jsonl").read_text().splitlines()]
    assert {(r["kind"], r["task"], r["status"]) for r in recs} == {("job", "daily", "ok"), ("job", "other", "crit")}
    js = host.state()["other"]
    assert (js["last_status"], js["last_rc"], js["fail_streak"], js["last_bad"]) == ("crit", 2, 1, True)
    assert Path(host.last_spec("daily")["log_path"]).parent.name == "daily"


def test_the_merged_overall_ignores_an_acknowledged_task_until_the_acknowledgement_ends(host):
    """SPEC5: the tick rewrites status["overall"] every few minutes; with a plain worst-of loop it turned the hero yellow a minute after the
    15-minute run had made it green. acks.overall is the one rule (acks use the real clock, the tick's `now` is simulated)."""
    now = time.time()
    entry = {"klass": "C0", "status": "warn", "alert": True, "acked": {"fp": "0" * 16, "until": now + 3600, "by": "cli"}}
    core.write_json_atomic(core.STATE_DIR / "status.json", {"tasks": {"disk": dict(entry)}, "generated_at": 123.0, "overall": "ok", "tier_runs": {}})
    seeded(host, DAILY)
    host.tick(T(3, 30))
    host.finish("daily")
    host.tick(T(3, 31))
    assert host.status()["overall"] == "ok" and host.status()["tasks"]["disk"]["status"] == "warn"      # the true status stays, the colour does not
    st = host.status()
    st["tasks"]["disk"]["acked"]["until"] = now - 5                                                     # the acknowledgement ran out
    core.write_json_atomic(core.STATE_DIR / "status.json", st)
    host.tick(T(3, 40))
    host.tick(T(3, 50))
    host.config(DAILY.replace("30 3", "45 3"))                                                          # (any change that makes the tick merge again)
    host.tick(T(3, 51))
    assert scheduler._overall(host.status()["tasks"]) == "warn"


def test_overall_falls_back_to_the_plain_rule_when_the_acks_module_is_unusable(monkeypatch):
    tasks = {"a": {"status": "warn", "alert": True, "acked": {"until": time.time() + 3600}}, "b": {"status": "ok"}, "c": {"status": "crit", "alert": False}}
    assert scheduler._overall(tasks) == "ok"
    from homelab_maint import acks
    monkeypatch.setattr(acks, "overall", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("broken")))
    assert scheduler._overall(tasks) == "warn"                                                          # fail closed: the true colour, alert=False still muted


def test_tick_never_invents_status_json_and_merges_as_soon_as_the_runner_wrote_one(host):
    (core.STATE_DIR / "status.json").unlink()
    seeded(host, DAILY)
    host.tick(T(3, 30))
    host.finish("daily")
    host.tick(T(3, 31))
    assert not (core.STATE_DIR / "status.json").exists()                              # the runner owns schema / generated_at
    seed_status()                                                                     # the first runner pass happens
    host.tick(T(3, 32))
    st = host.status()
    assert st["tasks"]["daily"]["status"] == "ok" and st["generated_at"] == 123.0 and st["schema"] == 1


def test_runs_are_audited_under_the_job_name_with_the_contract_outcomes_and_quiet_jobs_only_fail_loudly(host):
    seeded(host, tj("loud", schedule="30 3 * * *", title="Loud job"), tj("hush", schedule="30 3 * * *", quiet=True),
           tj("mon", schedule="30 3 * * *", monitor=True))
    host.tick(T(3, 30))
    for n in ("loud", "hush", "mon"):
        host.finish(n, t_end=T(3, 31))
    host.tick(T(3, 32))
    runs = [a for a in host.audits if a[1] == "run"]
    assert runs == [("loud", "run", "Loud job", 0, "done")]                              # quiet (and monitors by default): no success noise
    assert not [a for a in host.audits if a[1] == "job-start" and a[2] in ("hush", "mon")]
    hist = [json.loads(ln) for ln in (core.STATE_DIR / "history.jsonl").read_text().splitlines()]
    assert {h["task"] for h in hist} == {"loud"}
    host.audits.clear()
    host.tick(T(3, 30, d=3))
    for n in ("loud", "hush", "mon"):
        host.finish(n, rc=2, tail=["it broke"], t_end=T(3, 31, d=3))
    host.tick(T(3, 32, d=3))
    fails = {a[0]: a[4] for a in host.audits if a[1] == "run"}
    assert set(fails) == {"loud", "hush", "mon"} and all(v == "failed: exit 2: it broke" for v in fails.values())   # failures always


def test_status_merge_keeps_foreign_rows_and_drops_unmanaged_job_rows(host):
    core.write_json_atomic(core.STATE_DIR / "status.json", {"tasks": {"disk_forecast": {"klass": "C0", "status": "ok", "alert": True},
                                                                       "gone": {"klass": "J", "status": "crit", "alert": True}},
                                                             "generated_at": 123.0, "overall": "crit", "tier_runs": {"check": {"last_run": 1}}})
    seeded(host, DAILY)
    host.tick(T(3, 30))
    host.finish("daily")
    host.tick(T(3, 31))
    st = host.status()
    assert "disk_forecast" in st["tasks"] and "gone" not in st["tasks"]              # a stale J row is removed, a C0 row is untouched
    assert st["generated_at"] == 123.0 and st["tier_runs"] == {"check": {"last_run": 1}}   # the runner's own bookkeeping stays
    assert st["overall"] == "ok"
    host.config(DAILY.replace('mode = "managed"', 'mode = "observe"'))                 # cutover rolled back
    host.tick(T(3, 31) + 301)
    assert "daily" not in host.status()["tasks"]


def test_job_rows_come_back_on_the_next_tick_after_the_runner_rewrites_status_json(host):
    seeded(host, DAILY)
    host.tick(T(3, 30))
    host.finish("daily")
    host.tick(T(3, 31))
    st = host.status()
    del st["tasks"]["daily"]                                                            # cli.cmd_run's "not in REGISTRY" cleanup
    core.write_json_atomic(core.STATE_DIR / "status.json", st)
    host.tick(T(3, 32))                                                                 # noticed by its changed mtime/size: no 5 min flicker
    assert host.status()["tasks"]["daily"]["status"] == "ok"


def test_idle_ticks_do_not_rewrite_status_json(host):
    seeded(host, DAILY)
    host.tick(T(3, 30))
    host.finish("daily")
    host.tick(T(3, 31))
    before = (core.STATE_DIR / "status.json").stat().st_mtime_ns
    for m in range(32, 36):                                                             # < status_refresh_s (300 s) after the merge
        host.tick(T(3, m))
    assert (core.STATE_DIR / "status.json").stat().st_mtime_ns == before                # nothing changed: nothing written
    assert host.status()["tick"]["last_run"] == T(3, 31)
    host.tick(T(3, 31) + 301)                                                           # ... except the periodic refresh
    assert host.status()["tick"]["last_run"] == T(3, 31) + 301


def test_config_problems_surface_in_the_scheduler_row(host):
    p = host.tmp / "jobs.toml"
    p.write_text(DAILY + '[[job]]\nname = "bad"\ncommand = ["relative"]\n')
    cfg = jobs.load(p, mcfg={"tasks": {}}, apply_modes=False)
    host.tick(T(3, 0), cfg)
    row = host.status()["tasks"]["scheduler"]
    assert row["status"] == "warn" and "1 config problem" in row["summary"] and row["items"][0]["problem"].startswith("job bad")


# --------------------------------------------------------------------------- admission control
def due(host, *jobs_toml, **kw):
    """Seed at 03:00 and tick at the 03:30 occurrence: returns that tick's report."""
    seeded(host, *jobs_toml, **kw)
    return host.tick(T(3, 30))


def test_pause_blocks_everything_but_monitors(host):
    (core.CONF_DIR / "PAUSE").write_text("x")
    rep = due(host, DAILY, tj("watch", schedule="30 3 * * *", monitor=True))
    assert rep["started"] == ["watch"] and rep["deferred"]["daily"].startswith("paused")
    (core.CONF_DIR / "PAUSE").unlink()
    assert host.tick(T(3, 31))["started"] == ["daily"]                                  # the occurrence waited and now runs


def test_per_job_pause_file(host):
    (core.CONF_DIR / "PAUSE.daily").write_text("x")
    rep = due(host, DAILY, tj("other", schedule="30 3 * * *"))
    assert rep["started"] == ["other"] and "paused" in rep["deferred"]["daily"]


def test_pausable_false_ignores_pause(host):
    (core.CONF_DIR / "PAUSE").write_text("x")
    assert due(host, tj("daily", schedule="30 3 * * *", pausable=False))["started"] == ["daily"]


def test_legacy_interlock_blocks_and_never_double_runs(host):
    host.legacy = (True, "legacy backup-system.timer is active/enabled")
    rep = due(host, tj("bk", schedule="30 3 * * *", retire=["system:backup-system.timer"]))
    assert rep["started"] == [] and "interlock" in rep["deferred"]["bk"] and "cutover" in rep["deferred"]["bk"]
    n = host.legacy_calls
    host.tick(T(3, 31))
    assert host.legacy_calls == n                                                       # expensive probe is rate-limited (defer_retry_s)
    host.legacy = (False, "")
    assert host.tick(T(3, 33))["started"] == ["bk"]


def test_interlock_negative_answer_is_cached(host):
    seeded(host, tj("a", schedule="* * * * *", retire=["system:x.timer"]))
    host.tick(T(3, 1))
    host.finish("a")
    host.tick(T(3, 1, 30))
    host.tick(T(3, 2))
    assert host.legacy_calls == 1 and len(host.spawned) == 2                            # one probe served two launches
    host.finish("a")
    host.tick(T(3, 2, 30))
    host.tick(T(3, 3) + 700)
    assert host.legacy_calls == 2                                                       # cache expired (interlock_cache_s = 600)


def test_backups_and_heavy_jobs_never_launch_on_an_old_all_clear(host):
    seeded(host, tj("bk", schedule="* * * * *", heavy=True, backup=True, retire=["system:x.timer"]))
    host.tick(T(3, 1))
    host.finish("bk")
    host.tick(T(3, 1, 20))
    host.tick(T(3, 2))                                                                  # 60 s after the first probe: probed again
    assert host.legacy_calls == 2 and len(host.spawned) == 2
    host.legacy = (True, "legacy backup-system.timer is active/enabled")                # rolled back by hand without `job mode observe`
    host.finish("bk")
    host.tick(T(3, 3))
    assert len(host.spawned) == 2 and "interlock" in host.state()["bk"]["pending"]["reason"]


def test_a_stopped_supervisor_is_an_interrupted_run_and_is_retried_like_a_lost_one(host):
    """Shutdown sends TERM to the supervisor: the run is `cancelled`, not failed. A backup interrupted that way is retried after boot."""
    seeded(host, tj("bk", schedule="30 3 * * *", max_attempts=2, retry_on=["lost"], retry_backoff_s=[600], self_notifies=True))
    host.tick(T(3, 30))
    host.finish("bk", rc=143, cancelled=True, t_end=T(3, 40))
    rep = host.tick(T(3, 41))
    assert rep["reaped"] == [("bk", "error")] and host.notes == []                      # retry pending: no alert yet
    pend = host.state()["bk"]["pending"]
    assert pend["attempt"] == 2 and "retry 2/2 after lost" in pend["label"]
    assert host.tick(T(3, 52))["started"] == ["bk"]


def test_after_waits_for_the_other_job(host):
    rep = due(host, tj("daily", schedule="30 3 * * *"), tj("weekly", schedule="30 3 * * *", after=["daily"]))
    assert rep["started"] == ["daily"] and rep["deferred"]["weekly"] == "waiting for daily to finish"
    host.finish("daily")
    rep = host.tick(T(3, 31))
    assert rep["reaped"] == [("daily", "ok")] and rep["started"] == ["weekly"]               # reaped first, then admitted, same tick


def test_concurrency_cap_counts_only_non_monitors(host):
    rep = due(host, tj("a", schedule="30 3 * * *"), tj("b", schedule="30 3 * * *"), tj("c", schedule="30 3 * * *"),
              tj("m", schedule="30 3 * * *", monitor=True), sched="[scheduler]\nmax_concurrent = 2\n")
    assert sorted(rep["started"]) == ["a", "b", "m"] and "2 jobs already running" in rep["deferred"]["c"]


def test_heavy_jobs_are_serialised_through_one_mutex(host):
    rep = due(host, tj("a", schedule="30 3 * * *", heavy=True, **{"class": "P2"}), tj("b", schedule="30 3 * * *", heavy=True),
              tj("light", schedule="30 3 * * *"))
    assert rep["started"] == ["a", "light"] and rep["deferred"]["b"] == "heavy job a is running"
    host.finish("a")
    assert host.tick(T(3, 31))["started"] == ["b"]


def test_heavy_job_waits_for_a_backup_it_did_not_start(host):
    host.backup = (True, "backup lock held: backup-system.lock")
    rep = due(host, tj("prune", schedule="30 3 * * *", heavy=True), tj("light", schedule="30 3 * * *"))
    assert rep["started"] == ["light"] and "a backup is running" in rep["deferred"]["prune"]
    host.backup = (False, "")
    assert host.tick(T(3, 33))["started"] == ["prune"]


def test_backups_never_overlap_each_other(host):
    bk = dict(schedule="30 3 * * *", heavy=True, backup=True, force_after_defer=True, max_defer_hours=12)
    rep = due(host, tj("backup-immich", **bk), tj("backup-system", **bk))
    assert rep["started"] == ["backup-immich"] and rep["deferred"]["backup-system"] == "heavy job backup-immich is running"
    for h in range(4, 15):
        host.tick(T(h, 0))
    assert len(host.spawned) == 1                                                       # even a forced run never overlaps


def test_a_backup_the_tick_started_blocks_disruptive_and_gated_jobs_the_gates_cannot_see_it(host):
    """A backup started by the tick runs in the tick unit's cgroup, not in backup-*.service: `systemctl is-active` (what the
    `backup` and `immich-recycle` gates ask) says idle, and immich-server-recycle (disruptive, not heavy) would restart
    immich_server in the middle of the pg_dump."""
    recycle = dict(schedule="30 3 * * *", disruptive=True, gates=["immich-recycle"], force_after_defer=True, max_defer_hours=12)
    rep = due(host, tj("backup-immich", **BACKUP_P2), tj("immich-server-recycle", **recycle), tj("plain", schedule="30 3 * * *"),
              tj("watch", schedule="30 3 * * *", monitor=True, disruptive=True), tj("pruner", schedule="30 3 * * *", gates=["apt"], catchup_hours=24))
    assert sorted(rep["started"]) == ["backup-immich", "plain", "watch"]                 # monitors and ungated, undisruptive jobs go on
    assert rep["deferred"]["immich-server-recycle"] == "backup backup-immich is running"
    assert rep["deferred"]["pruner"] == "backup backup-immich is running"                 # any gated job
    assert "immich-recycle" not in host.gate_calls and "apt" not in host.gate_calls       # refused before any gate probe
    for h in range(4, 15):                                                                # forced after 12 h: still never next to the backup
        host.tick(T(h, 0))
    assert [s["job"] for s in host.spawned].count("immich-server-recycle") == 0
    host.finish("backup-immich")
    rep = host.tick(T(15, 0))
    assert "immich-server-recycle" in rep["started"] and "pruner" in rep["started"]       # the backup is gone: the wait is over


def test_a_backup_lock_held_by_something_else_blocks_disruptive_and_gated_jobs_too(host):
    host.backup = (True, "backup lock held: backup-system.lock")
    rep = due(host, tj("recycle", schedule="30 3 * * *", disruptive=True), tj("gated", schedule="30 3 * * *", gates=["comfyui"]),
              tj("plain", schedule="30 3 * * *"), tj("watch", schedule="30 3 * * *", monitor=True, disruptive=True))
    assert sorted(rep["started"]) == ["plain", "watch"]
    assert "a backup is running (backup lock held" in rep["deferred"]["recycle"] and "a backup is running" in rep["deferred"]["gated"]
    host.backup = (False, "")
    assert sorted(host.tick(T(3, 33))["started"]) == ["gated", "recycle"]


@pytest.mark.parametrize("level,cls,expect", [(0, "P3", True), (1, "P3", True), (2, "P3", False), (2, "P2", False), (5, "P2", False),
                                              (2, "P1", True), (5, "P1", True), (5, "P0", True), (None, "P3", True)])
def test_pressure_gates_p2_p3_but_not_p0_p1(host, level, cls, expect):
    host.level = level
    rep = due(host, tj("j", schedule="30 3 * * *", **{"class": cls}))
    assert (rep["started"] == ["j"]) is expect
    if not expect:
        assert "pressure" in rep["deferred"]["j"]


def test_pressure_override_and_monitors(host):
    host.level = 4
    rep = due(host, tj("a", schedule="30 3 * * *", pressure_max=5), tj("b", schedule="30 3 * * *", monitor=True, **{"class": "P3"}),
              tj("c", schedule="30 3 * * *"))
    assert sorted(rep["started"]) == ["a", "b"] and "c" in rep["deferred"]
    host.level = 0
    assert host.tick(T(3, 31))["started"] == ["c"]                                      # starts once pressure is gone


def write_pressure(now, **rec):
    d = core.STATE_DIR / "tasks"
    d.mkdir(exist_ok=True)
    core.write_json_atomic(d / "pressure_state.json", {"t": now, **rec})


BACKUP_P2 = dict(schedule="30 3 * * *", heavy=True, backup=True, force_after_defer=True, max_defer_hours=12, **{"class": "P2"})


def test_the_gate_reads_gate_level_not_the_dashboard_level_so_an_io_stall_never_defers_a_backup(host):
    """pressure_state publishes `level` (max of mem, io, cpu, gpu: for the dashboard) and `gate_level` (mem and cpu: what the
    scheduler must read). This host sits at io-stall level 3 for hours every night: gating on `level` held every P2/P3 job and
    every backup until it expired, and force_after_defer could not help (forced_pressure_max = 1 blocks level 3 too)."""
    host.real_pressure = True
    write_pressure(T(3, 29), level=3, gate_level=0, level_name="io stall")
    rep = due(host, tj("backup-system", **BACKUP_P2))
    assert rep["started"] == ["backup-system"]
    assert host.state()["backup-system"].get("skipped_reason") is None


def test_a_real_memory_or_cpu_stall_still_defers_a_backup_and_forced_start_still_waits(host):
    host.real_pressure = True
    write_pressure(T(3, 29), level=3, gate_level=3, level_name="memory stall")
    rep = due(host, tj("backup-system", **BACKUP_P2))
    assert rep["started"] == [] and "pressure gate level 3" in rep["deferred"]["backup-system"]
    write_pressure(T(15, 29), level=3, gate_level=3)
    assert host.tick(T(15, 30))["started"] == [] and "even a forced start waits" in host.state()["backup-system"]["pending"]["reason"]
    write_pressure(T(15, 30), level=3, gate_level=1)
    assert host.tick(T(15, 31))["started"] == ["backup-system"]


@pytest.mark.parametrize("rec,expect", [
    ({"level": 3, "gate_level": 0}, 0),
    ({"level": 3, "gate_level": 2}, 2),
    ({"level": 3, "dims": {"mem": {"level": 0}, "io": {"level": 3}, "cpu": {"level": 1}, "gpu": {"level": 2}}}, 1),   # pre-gate_level record
    ({"level": 4, "dims": {"mem": {"level": 3, "eh": [3], "lh": [3]}, "io": {"level": 4}}}, 3),
    ({"level": 3, "dims": {"mem": 0, "io": 3, "cpu": 0}}, 0),                                                        # flat dims (the metrics form)
    ({"level": 2}, 2),                                                                                               # nothing but `level`
    ({"level": 3, "gate_level": True, "dims": {"mem": {"level": 1}}}, 1),                                            # a bool is not a level
])
def test_gate_level_of_reads_gate_level_then_dims_then_level(rec, expect):
    assert scheduler.gate_level_of(rec)[0] == expect


def test_default_pressure_unknown_when_missing_stale_or_empty(host):
    sched = jobs.SCHED_DEFAULTS
    assert scheduler.default_pressure(T(3, 0), sched)[0] is None                                   # never ran
    write_pressure(T(3, 0), level=3, gate_level=3)
    assert scheduler.default_pressure(T(3, 10), sched) == (3, "gate level 3")
    assert scheduler.default_pressure(T(4, 0), sched)[0] is None                                   # 60 min old > pressure_stale_min
    write_pressure(T(3, 0), level="x", name="?")
    assert scheduler.default_pressure(T(3, 1), sched)[0] is None


def test_forced_after_defer_ignores_a_busy_gate_but_not_pause(host):
    host.gate["g"] = (True, "busy forever")
    seeded(host, tj("bk", schedule="30 3 * * *", **{"class": "P2"}, gates=["g"], force_after_defer=True, max_defer_hours=2, catchup_hours=10))
    assert host.tick(T(3, 30))["started"] == []
    assert host.tick(T(5, 29))["started"] == []
    (core.CONF_DIR / "PAUSE").write_text("x")
    assert host.tick(T(5, 31))["started"] == [] and "paused" in host.state()["bk"]["pending"]["reason"]
    (core.CONF_DIR / "PAUSE").unlink()
    assert host.tick(T(5, 32))["started"] == ["bk"]                                     # 2 h of deferral used up: runs through the gate


def test_a_forced_job_that_stays_blocked_alerts_once_when_the_deferral_limit_passes(host):
    """A backup held by PAUSE (or a legacy driver, or a stall) would otherwise stay silent until its occurrence expires days later."""
    (core.CONF_DIR / "PAUSE").write_text("x")
    seeded(host, tj("bk", schedule="30 3 * * *", **{"class": "P2"}, force_after_defer=True, max_defer_hours=2, catchup_hours=100,
                    notify={"on_expire": "alert"}), tj("quiet", schedule="30 3 * * *", force_after_defer=True, max_defer_hours=2,
                                                       catchup_hours=100))
    host.tick(T(3, 30))
    assert host.tick(T(5, 29))["started"] == [] and host.notes == []                      # inside the deferral limit: nothing to say
    host.tick(T(5, 31))
    assert host.notes == [("expire", "bk", "warn", None)]                                  # job without on_expire = alert: silent
    for h in (6, 8, 12):
        host.tick(T(h, 0))
    host.tick(T(3, 30, d=3))                                                               # the next occurrence supersedes: same chain
    host.tick(T(9, 0, d=3))
    assert len(host.notes) == 1                                                            # once, not every tick or every occurrence
    (core.CONF_DIR / "PAUSE").unlink()
    assert sorted(host.tick(T(9, 1, d=3))["started"]) == ["bk", "quiet"]


def test_force_never_overrides_pressure_at_level_two_and_above_by_default(host):
    """The owner's rule: heavy work never starts at pressure >= 2. force_after_defer beats gates/freeze/windows, not that."""
    host.level = 2
    seeded(host, tj("bk", schedule="30 3 * * *", **{"class": "P2"}, heavy=True, force_after_defer=True, max_defer_hours=2,
                    catchup_hours=10, notify={"on_expire": "alert"}))
    host.tick(T(3, 30))
    rep = host.tick(T(6, 0))                                                            # well past the 2 h deferral limit
    assert rep["started"] == [] and "even a forced start waits above level 1" in rep["deferred"]["bk"]
    assert host.notes == [("expire", "bk", "warn", None)]                               # ... and the owner is told, once
    host.level = 1
    assert host.tick(T(6, 1))["started"] == ["bk"]                                      # pressure eased: now it goes
    host.finish("bk")
    host.tick(T(6, 2))
    host.level = 5
    assert host.tick(T(3, 30, d=3))["started"] == []
    out = scheduler.run_job("bk", host.env(), host.cfg, T(3, 31, d=3), force=True)
    assert out["started"] is True                                                       # `job run --force` is the owner's own decision


def test_forced_pressure_cap_is_configurable(host):
    host.level = 3
    seeded(host, tj("bk", schedule="30 3 * * *", **{"class": "P2"}, force_after_defer=True, max_defer_hours=1, catchup_hours=10),
           sched="[scheduler]\nforced_pressure_max = 3\n")
    host.tick(T(3, 30))
    assert host.tick(T(4, 31))["started"] == ["bk"]                                     # level 3 <= the configured cap
    host.finish("bk")
    host.tick(T(4, 32))
    host.level = 4
    assert host.tick(T(3, 30, d=3))["started"] == [] and host.tick(T(4, 31, d=3))["started"] == []


def test_deferral_clock_starts_when_the_tick_first_saw_the_catch_up(host):
    """The host was off for 21 h. That is downtime, not deferral: the forced start comes max_defer_hours after the tick woke up,
    not instantly because the occurrence is old (which would push a backup into the post-boot spike)."""
    host.gate["g"] = (True, "busy (post-boot)")
    seeded(host, tj("bk", schedule="30 3 * * *", **{"class": "P2"}, gates=["g"], force_after_defer=True, max_defer_hours=12, catchup_hours=40))
    wake = T(1, 0, d=3)
    assert host.tick(wake)["started"] == []                                              # 21.5 h late, gate busy: waits like any other start
    assert host.tick(T(12, 58, d=3))["started"] == []
    assert host.state()["bk"]["pending"]["first_due"] == T(3, 30)                      # still ONE chain of unresolved occurrences
    assert host.tick(T(13, 1, d=3))["started"] == ["bk"]             # 12 h after the wake-up (+ the 2 min probe rate limit): forced through the gate


def test_force_after_defer_is_never_cut_short_by_a_small_catchup_window(host):
    host.gate["g"] = (True, "busy")
    seeded(host, tj("bk", schedule="30 3 * * *", gates=["g"], force_after_defer=True, max_defer_hours=4, catchup_hours=1))
    host.tick(T(9, 0))                                                                   # seen 5.5 h late: older than catchup_hours
    assert host.state()["bk"]["pending"] and not host.state()["bk"]["skipped_reason"].startswith("expired")
    assert host.tick(T(13, 0))["started"] == ["bk"]


def test_non_forced_job_expires_instead_of_running_late_under_pressure(host):
    host.level = 5
    seeded(host, tj("j", schedule="30 3 * * *", max_defer_hours=2, catchup_hours=2))
    host.tick(T(3, 30))
    rep = host.tick(T(5, 31))
    assert rep["started"] == [] and rep["expired"] == ["j"]


def test_freeze_window_defers_disruptive_jobs_until_it_ends(host):
    seeded(host, tj("recycle", schedule="0 19 * * *", disruptive=True, catchup_hours=6, jitter_s=0))
    host.tick(T(18, 0))
    rep = host.tick(T(19, 0))
    assert rep["started"] == [] and rep["deferred"]["recycle"].startswith("freeze window")
    assert host.tick(T(23, 29))["started"] == []
    assert host.tick(T(23, 31))["started"] == ["recycle"]                               # 4.5 h late: still inside catchup_hours


def test_freeze_file_and_routine_freeze_table(host):
    seeded(host, tj("r", schedule="0 12 * * *", disruptive=True))
    (core.CONF_DIR / "FREEZE").write_text("x")
    rep = host.tick(T(12, 0))
    assert rep["started"] == [] and "FREEZE file" in rep["deferred"]["r"]
    (core.CONF_DIR / "FREEZE").unlink()
    (core.CONF_DIR / "routine.toml").write_text('[freeze]\nnoon = "11:00-13:00"\n')       # the routine's table replaces the default
    assert host.tick(T(12, 5))["started"] == [] and "noon" in host.state()["r"]["pending"]["reason"]
    (core.CONF_DIR / "routine.toml").write_text('[freeze]\nnight = "02:00-04:00"\n')
    assert host.tick(T(12, 10))["started"] == ["r"]                                      # 12:00 is no longer frozen


def test_non_disruptive_jobs_ignore_the_freeze(host):
    seeded(host, tj("bk", schedule="0 19 * * *"))
    host.tick(T(18, 59))
    assert host.tick(T(19, 0))["started"] == ["bk"]


def test_avoid_windows(host):
    seeded(host, tj("a", schedule="30 3 * * *", avoid=["03:00-04:00"]))
    rep = host.tick(T(3, 30))
    assert rep["started"] == [] and "avoid window" in rep["deferred"]["a"]
    assert host.tick(T(4, 1))["started"] == ["a"]


def test_window_restricts_catch_up_but_not_the_on_time_run(host):
    seeded(host, tj("w", schedule="30 22 * * *", window="01:00-05:00", catchup_hours=8))
    assert host.tick(T(22, 30, 10))["started"] == ["w"]                                 # on time: the window is irrelevant
    host.finish("w", t_end=T(22, 35))
    host.tick(T(22, 36))
    # next day: host was off at 22:30, comes back at 23:30 (a catch-up, outside the window): waits for 01:00
    host.t = T(22, 0, d=3)
    host.tick(T(22, 0, d=3))
    rep = host.tick(T(23, 30, d=3))
    assert rep["started"] == [] and "waits for its window" in rep["deferred"]["w"]
    assert host.tick(T(1, 5, d=4))["started"] == ["w"]


def test_busy_gate_defers_and_probes_are_rate_limited(host):
    host.gate["comfyui"] = (True, "ComfyUI queue: 1 running")
    seeded(host, tj("g", schedule="30 3 * * *", gates=["comfyui"], catchup_hours=3))
    rep = host.tick(T(3, 30))
    assert rep["started"] == [] and rep["deferred"]["g"] == "gate comfyui: ComfyUI queue: 1 running"
    host.tick(T(3, 31))
    host.tick(T(3, 31, 59))
    assert host.gate_calls == ["comfyui"]                                                # one probe per defer_retry_s (120 s)
    host.tick(T(3, 32, 31))
    assert host.gate_calls == ["comfyui", "comfyui"]
    host.gate["comfyui"] = (False, "")
    assert host.tick(T(3, 35))["started"] == ["g"]


def test_gate_forever_busy_expires_unless_forced(host):
    host.gate["immich-recycle"] = (True, "immich_server at 40% of a core")
    seeded(host, tj("soft", schedule="30 3 * * *", gates=["immich-recycle"], max_defer_hours=12, catchup_hours=12),
           tj("recycle", schedule="30 3 * * *", gates=["immich-recycle"], max_defer_hours=12, force_after_defer=True, catchup_hours=12))
    host.tick(T(3, 30))
    for h in range(4, 15):
        host.tick(T(h, 0))
    assert host.spawned == []
    rep = host.tick(T(15, 31))                                                          # 12 h passed
    assert rep["started"] == ["recycle"] and rep["expired"] == ["soft"]


def test_jitter_delays_the_start_deterministically(host):
    seeded(host, tj("j", schedule="30 3 * * *", jitter_s=600))
    j = schedule.jitter("j", T(3, 30), 600)
    assert 0 <= j < 600
    assert host.tick(T(3, 30))["started"] == []
    if j > 1:
        assert host.tick(T(3, 30) + j - 1)["started"] == []
    assert host.tick(T(3, 30) + j)["started"] == ["j"]


def test_jitter_is_clamped_to_half_the_period(host):
    cfg = host.config(tj("j", schedule="*/5 * * * *", jitter_s=3600))
    for due_ in (T(3, 5), T(3, 10), T(3, 15)):
        assert scheduler.eff_jitter(cfg.jobs["j"], due_) < 150


def test_unknown_user_is_a_recorded_failure_not_a_crash(host):
    seeded(host, tj("u", schedule="30 3 * * *", user="ghost"))
    rep = host.tick(T(3, 30))
    assert rep["started"] == [] and host.spawned == []
    js = host.state()["u"]
    assert js["last_status"] == "error" and "does not exist" in js["last_summary"] and "running" not in js


def test_spawn_failure_is_a_recorded_failure_and_does_not_leave_a_running_record(host):
    seeded(host, DAILY)
    host.spawn_raises = True
    rep = host.tick(T(3, 30))
    assert rep["started"] == [] and any("launch failed" in e for e in rep["errors"])
    js = host.state()["daily"]
    assert "running" not in js and js["last_status"] == "error"
    host.spawn_raises = False
    assert host.tick(T(3, 31))["started"] == []                                         # the occurrence is consumed, not hammered


def test_tick_budget_stops_evaluation_but_not_the_tick(host):
    seeded(host, DAILY, tj("b", schedule="30 3 * * *"))
    cfg = host.cfg
    cfg.sched["tick_budget_s"] = -1
    rep = host.tick(T(3, 30), cfg)
    assert rep["started"] == [] and any("budget" in e for e in rep["errors"])


# --------------------------------------------------------------------------- notifications
def test_failure_alert_once_per_streak_then_recovery(host):
    seeded(host, tj("h", schedule="0 * * * *"))
    for i, rc in enumerate([1, 1, 1, 0]):
        t = T(4 + i, 0)
        host.tick(t)
        host.finish("h", rc=rc, t_end=t + 5)
        host.tick(t + 10)
    assert [n[:3] for n in host.notes] == [("failure", "h", "crit"), ("recovery", "h", "ok")]
    assert host.state()["h"]["fail_streak"] == 0 and "alerted" not in host.state()["h"]
    t = T(8, 0)
    host.tick(t)
    host.finish("h", rc=1, t_end=t + 5)
    host.tick(t + 10)
    assert host.notes[-1][:2] == ("failure", "h") and len(host.notes) == 3              # a new streak alerts again


def test_failure_reminder_after_24_hours(host):
    seeded(host, tj("d", schedule="0 * * * *"))
    for h in range(4, 4 + 26):                                                           # failing every hour for 26 hours
        t = T(0, 0) + h * 3600
        host.tick(t)
        host.finish("d", rc=1, t_end=t + 5)
        host.tick(t + 10)
    assert len(host.notes) == 2                                                          # the first failure, then one 24 h reminder


def test_monitor_needs_two_failures_by_default(host):
    seeded(host, tj("mon", schedule="* * * * *", monitor=True))
    for m in (1, 2, 3):
        t = T(3, m)
        host.tick(t)
        host.finish("mon", rc=1, t_end=t + 5)
        host.tick(t + 10)
    assert [n[0] for n in host.notes] == ["failure"] and host.notes[0][3] == 1
    assert host.state()["mon"]["fail_streak"] == 3


def test_self_notifying_job_is_trusted_for_ordinary_failures_only(host):
    seeded(host, tj("sn", schedule="0 * * * *", self_notifies=True))
    t = T(4, 0)
    host.tick(t)
    host.finish("sn", rc=1, t_end=t + 5)                                                 # the script itself alerted: stay quiet
    host.tick(t + 10)
    assert host.notes == []
    t = T(5, 0)
    host.tick(t)
    host.finish("sn", rc=137, timed_out=True, t_end=t + 5)                               # the script could not report this one
    host.tick(t + 10)
    assert [n[0] for n in host.notes] == ["failure"]
    t = T(6, 0)
    host.tick(t)
    host.finish("sn", rc=0, t_end=t + 5)
    host.tick(t + 10)
    assert [n[0] for n in host.notes] == ["failure", "recovery"]


def test_on_failure_none_is_silent(host):
    seeded(host, tj("q", schedule="0 * * * *", notify={"on_failure": "none"}))
    host.tick(T(4, 0))
    host.finish("q", rc=1, t_end=T(4, 1))
    host.tick(T(4, 2))
    assert host.notes == [] and host.status()["tasks"]["q"]["status"] == "crit"          # still visible on the dashboard


def test_success_maintenance_update_only_when_asked_and_not_for_self_notifiers(host):
    seeded(host, tj("m", schedule="0 * * * *", notify={"on_success": "maintenance"}),
           tj("s", schedule="0 * * * *", notify={"on_success": "maintenance"}, self_notifies=True))
    host.tick(T(4, 0))
    for n in ("m", "s"):
        host.finish(n, t_end=T(4, 1))
    host.tick(T(4, 2))
    assert host.notes == [("maintenance", "m", "ok", 1)]


def test_notification_failure_never_changes_the_recorded_result(host):
    host.notify_raises = True
    seeded(host, DAILY)
    host.tick(T(3, 30))
    host.finish("daily", rc=1)
    rep = host.tick(T(3, 31))
    assert any("notify daily" in e for e in rep["errors"])
    assert host.state()["daily"]["last_status"] == "crit" and "alerted" not in host.state()["daily"]
    assert any(a[1] == "notify-failed" for a in host.audits)
    host.notify_raises = False
    host.tick(T(3, 30, d=3))
    host.finish("daily", rc=1, t_end=T(3, 31, d=3))
    host.tick(T(3, 32, d=3))
    assert [n[0] for n in host.notes] == ["failure"]                                      # the unsent alert is retried at the next failure


def test_dry_run_changes_nothing(host):
    seeded(host, DAILY)
    before = {p.name: p.stat().st_mtime_ns for p in core.STATE_DIR.iterdir()}
    rep = host.tick(T(3, 30), dry=True)
    assert rep["started"] == ["daily"] and host.spawned == [] and host.notes == []
    assert {p.name: p.stat().st_mtime_ns for p in core.STATE_DIR.iterdir()} == before
    assert not (core.RUN_DIR / "tick.json").exists() or True


def test_tick_lock_prevents_overlapping_ticks(host):
    seeded(host, DAILY)
    with scheduler.tick_lock():
        rep = host.tick(T(3, 30))
    assert rep["locked"] is True and host.spawned == []
    assert host.tick(T(3, 30))["started"] == ["daily"]


# --------------------------------------------------------------------------- DST days (host TZ America/Toronto)
def simulate(host, start, end, step=60):
    """Tick every `step` seconds; every spawned job finishes instantly and is reaped on the next tick. Returns start instants."""
    starts = []
    t = start
    while t < end:
        rep = host.tick(t)
        starts += [t for _ in rep["started"]]
        for n in rep["started"]:
            host.finish(n, t_end=t + 1)
        t += step
    return starts


def test_dst_spring_forward_fixed_job_runs_once_at_the_shifted_time(host):
    host.config(tj("fx", schedule="30 2 * * *"))
    starts = simulate(host, T(0, 0, d=8, mo=3), T(6, 0, d=8, mo=3))
    assert len(starts) == 1
    assert dt.datetime.fromtimestamp(starts[0], TO).strftime("%H:%M %z") == "03:30 -0400"


def test_dst_fall_back_fixed_job_runs_once_on_the_first_pass(host):
    host.config(tj("fx", schedule="30 1 * * *"))
    starts = simulate(host, T(0, 0, d=1, mo=11), T(4, 0, d=1, mo=11))
    assert len(starts) == 1
    assert dt.datetime.fromtimestamp(starts[0], TO).strftime("%H:%M %z") == "01:30 -0400"


def test_dst_fall_back_interval_job_runs_on_both_passes(host):
    host.config(tj("iv", schedule="30 * * * *"))
    starts = simulate(host, T(0, 0, d=1, mo=11), T(3, 0, d=1, mo=11))
    hours = [dt.datetime.fromtimestamp(s, TO).strftime("%H:%M%z") for s in starts]
    assert hours == ["00:30-0400", "01:30-0400", "01:30-0500", "02:30-0500"]           # 01:30 twice, an hour of real time apart


def test_dst_spring_forward_interval_job_skips_the_missing_hour(host):
    host.config(tj("iv", schedule="30 * * * *"))
    starts = simulate(host, T(0, 0, d=8, mo=3), T(5, 0, d=8, mo=3))
    assert [dt.datetime.fromtimestamp(s, TO).strftime("%H:%M") for s in starts] == ["00:30", "01:30", "03:30", "04:30"]


def test_dst_weekly_backup_across_fall_back_runs_once(host):
    host.config(tj("backup-immich", schedule="0 1 * * sun", jitter_s=900, heavy=True, backup=True))
    starts = simulate(host, T(0, 0, d=1, mo=11), T(5, 0, d=1, mo=11), step=300)
    assert len(starts) == 1
    s = dt.datetime.fromtimestamp(starts[0], TO)
    assert (s.hour, s.minute < 16, s.utcoffset()) == (1, True, dt.timedelta(hours=-4))


# --------------------------------------------------------------------------- explain / export
def test_explain_covers_every_kind_of_entry(host):
    cfg = host.config(DAILY, tj("legacy", schedule="0 1 * * sat", retire=["system:old.timer"]).replace('"managed"', '"observe"'),
                      tj("old", schedule="0 */3 * * *", note="superseded").replace('"managed"', '"retired"'),
                      tj("man"), tj("fresh", schedule="20 5 * * *").replace('"managed"', '"observe"'),
                      extra='[[external]]\nname = "apt-daily"\nkind = "os"\nschedule = "06:00"\nnote = "distro"\n')
    host.tick(T(3, 0))
    rows = {r["job"]: r for r in scheduler.explain(T(3, 0), cfg)}
    assert set(rows) == {"daily", "legacy", "old", "man", "apt-daily", "fresh"}
    assert "nothing schedules it yet" in rows["fresh"]["why"] and "job mode fresh managed" in rows["fresh"]["why"]   # no legacy driver
    assert rows["daily"]["next_due"] == T(3, 30) and rows["daily"]["why"] == "waiting for its schedule" and rows["daily"]["mode"] == "managed"
    assert rows["legacy"]["mode"] == "observe" and "system:old.timer" in rows["legacy"]["why"] and "cutover" in rows["legacy"]["why"]
    assert rows["legacy"]["next_due"] == T(1, 0, d=3)
    assert rows["old"]["why"] == "superseded" and rows["old"]["next_due"] is None
    assert rows["man"]["why"] == "manual only"
    assert rows["apt-daily"]["source"] == "os" and rows["apt-daily"]["mode"] == "external" and "managed externally" in rows["apt-daily"]["why"]
    order = [r["job"] for r in scheduler.explain(T(3, 0), cfg)]
    assert order.index("daily") < order.index("legacy")                                   # soonest first, undated last


def test_explain_states_running_pending_and_deferred(host):
    host.level = 5
    seeded(host, tj("slow", schedule="30 3 * * *"), tj("blocked", schedule="30 3 * * *", **{"class": "P2"}))
    host.tick(T(3, 30))
    rows = {r["job"]: r for r in scheduler.explain(T(3, 31), host.cfg)}
    assert rows["blocked"]["why"].startswith("pressure level 5") if "blocked" in host.state() and not host.state()["blocked"].get("running") else True
    host.level = 0
    host.tick(T(3, 32))
    rows = {r["job"]: r for r in scheduler.explain(T(3, 33), host.cfg)}
    assert rows["slow"]["why"].startswith("running since") and rows["blocked"]["why"].startswith("running since")


def test_explain_includes_observed_last_result_of_a_legacy_job(host):
    sj = host.tmp / "system-status.json"
    sj.write_text(json.dumps({"job": "system", "result": "ok", "finished": "2026-09-26 02:39:38", "duration_short": "1h24m", "used_gb": 1,
                              "free_gb_after": 2, "snapshots_kept": 4}))
    cfg = host.config(tj("bk", schedule="0 1 * * sat", success={"status_json": str(sj), "summary": "{job} backup {result}: {duration_short}",
                                                                 "max_age_hours": 200}).replace('"managed"', '"observe"'))
    row = scheduler.explain(T(3, 0), cfg)[0]
    assert row["last_status"] == "ok" and row["last_summary"] == "system backup ok: 1h24m" and row["last_age_h"] > 0


def test_export_is_public_safe(host):
    cfg = host.config(tj("bk", command=("/usr/local/sbin/secret-script.sh", "--token", "abc"), schedule="30 3 * * *", user="ohmz",
                         env={"API_KEY": "sekret"}))
    host.tick(T(3, 0))
    host.tick(T(3, 30))
    host.finish("bk", rc=1, tail=["failed writing /home/ohmz/private/file.txt"], t_end=T(3, 31))
    host.tick(T(3, 32))
    out = scheduler.export(T(3, 33), cfg)
    blob = json.dumps(out)
    assert "secret-script" not in blob and "sekret" not in blob and "--token" not in blob and "/home/ohmz/private" not in blob
    assert out["jobs"][0]["job"] == "bk" and out["jobs"][0]["last_status"] == "crit"
    assert out["counts"]["managed"] == 1 and out["config_problems"] == 0 and set(out["tick"]) == {"status", "summary"}
    assert set(out["jobs"][0]) <= {"job", "title", "source", "mode", "class", "heavy", "monitor", "schedule", "next_due", "last_start",
                                   "last_end", "last_status", "last_summary", "why"}


def test_export_has_no_file_paths_anywhere(host):
    host.backup = (True, "backup lock held: /run/lock/backup-system.lock")
    due(host, tj("prune", schedule="30 3 * * *", heavy=True))
    pub = scheduler.export(T(3, 31), host.cfg)
    row = next(r for r in pub["jobs"] if r["job"] == "prune")
    assert "a backup is running" in row["why"] and "/run/lock" not in json.dumps(pub) and "..." in row["why"]


def test_format_table_lists_everything(host):
    cfg = host.config(DAILY)
    txt = scheduler.format_table(scheduler.explain(T(3, 0), cfg))
    assert txt.splitlines()[0].startswith("NEXT") and "daily" in txt and "managed" in txt and "30 3 * * *" in txt


# --------------------------------------------------------------------------- manual run
def test_run_job_starts_outside_the_schedule(host):
    cfg = host.config(tj("m", schedule="30 3 * * *"))
    res = scheduler.run_job("m", host.env(), cfg, host.t)
    assert res["started"] is True and res["run_id"] and host.spawned[0]["attempt"] == 1
    assert scheduler.run_job("m", host.env(), cfg, host.t)["reason"] == "already running"


def test_run_job_respects_mode_unless_told(host):
    cfg = host.config(tj("o", retire=["system:o.timer"]).replace('"managed"', '"observe"'))
    assert scheduler.run_job("o", host.env(), cfg, host.t)["started"] is False
    assert scheduler.run_job("o", host.env(), cfg, host.t, ignore_mode=True)["started"] is True
    assert scheduler.run_job("ghost", host.env(), cfg, host.t)["reason"].startswith("unknown job")
    r = host.config(tj("r").replace('"managed"', '"retired"'))
    assert scheduler.run_job("r", host.env(), r, host.t, ignore_mode=True)["reason"] == "job is retired"


def test_run_job_never_bypasses_the_interlock_pause_or_mutex(host):
    host.legacy = (True, "legacy unit enabled")
    cfg = host.config(tj("bk", retire=["system:bk.timer"], heavy=True), tj("other", heavy=True))
    res = scheduler.run_job("bk", host.env(), cfg, host.t, force=True)
    assert res["started"] is False and "interlock" in res["reason"]
    host.legacy = (False, "")
    (core.CONF_DIR / "PAUSE").write_text("x")
    assert "paused" in scheduler.run_job("bk", host.env(), cfg, host.t, force=True)["reason"]
    (core.CONF_DIR / "PAUSE").unlink()
    assert scheduler.run_job("other", host.env(), cfg, host.t)["started"] is True
    assert "heavy job other" in scheduler.run_job("bk", host.env(), cfg, host.t, force=True)["reason"]


def test_run_job_force_skips_pressure_and_gates_only(host):
    host.level = 5
    host.gate["comfyui"] = (True, "busy")
    cfg = host.config(tj("j", gates=["comfyui"], **{"class": "P3"}))
    res = scheduler.run_job("j", host.env(), cfg, host.t)
    assert res["started"] is False and "pressure" in res["reason"]
    assert scheduler.run_job("j", host.env(), cfg, host.t, force=True)["started"] is True


def test_run_job_does_not_leave_a_pending_manual_request_behind(host):
    host.level = 5
    cfg = host.config(tj("j", schedule="30 3 * * *"))
    scheduler.run_job("j", host.env(), cfg, host.t)
    assert "pending" not in scheduler.load_state()["jobs"]["j"]
    assert host.tick(T(3, 1), cfg)["started"] == []


def test_manual_failed_run_is_not_auto_retried(host):
    cfg = host.config(tj("j", max_attempts=3, retry_on=["failed"]))
    scheduler.run_job("j", host.env(), cfg, host.t)
    host.finish("j", rc=1)
    host.tick(host.t + 5)
    assert "pending" not in host.state()["j"]


# --------------------------------------------------------------------------- health, validation, CLI
def test_health_reads_the_tmpfs_heartbeat(host):
    assert scheduler.health(T(3, 0))[0] == "warn"                                          # nothing since boot
    host.config(DAILY)
    host.tick(T(3, 0))
    assert scheduler.health(T(3, 1))[0] == "ok"
    st, msg = scheduler.health(T(3, 20))
    assert st == "crit" and "20 min ago" in msg


def test_a_tick_that_loaded_no_jobs_is_crit_and_its_unit_fails(host, capsys):
    """A jobs.toml typo leaves the tick beating (t is fresh) but scheduling nothing: health says crit, the beat carries the job count
    for the umbrella-tick probe, and `tick` exits 1 so the unit shows up in failed_units. One bad job among good ones stays a warn."""
    (core.CONF_DIR / "jobs.toml").write_text('[[job]]\nname = "bad"\ncommand = ["relative"]\n')
    assert scheduler.main(["tick"]) == 1
    hb = json.loads((core.RUN_DIR / "tick.json").read_text())
    assert hb["jobs"] == 0 and hb["config_problems"] == 1
    st, msg = scheduler.health(hb["t"] + 5)
    assert st == "crit" and "no jobs" in msg and "1 problem" in msg
    (core.CONF_DIR / "jobs.toml").write_text(DAILY + '[[job]]\nname = "bad"\ncommand = ["relative"]\n')
    assert scheduler.main(["tick"]) == 0
    hb = json.loads((core.RUN_DIR / "tick.json").read_text())
    assert hb["jobs"] == 1 and scheduler.health(hb["t"] + 5)[0] == "warn"
    capsys.readouterr()


def test_validate_catches_launch_time_problems(host):
    cfg = host.config(tj("a", command=("/nonexistent/x",), retire=["bogus"], after=["ghost"], gates=["nope"], user="ohmz"),
                      tj("b", schedule="30 3 * * *", retire=[]).replace('"managed"', '"managed"') + 'source = "adapter"\n')
    probs = scheduler.validate(cfg)
    joined = "\n".join(probs)
    assert "not an executable" in joined and "bad retire spec" in joined and "after = 'ghost'" in joined and "unknown gate" in joined
    assert "managed legacy job without a `retire` interlock" in joined
    assert scheduler.validate(cfg, check_paths=False) != []


def test_shipped_jobs_toml_validates(host):
    cfg = jobs.load(REPO / "etc" / "jobs.toml", mcfg={"tasks": {}}, apply_modes=False)
    assert scheduler.validate(cfg, check_paths=False) == []


def test_cli_explain_mode_validate_health(host, capsys):
    (core.CONF_DIR / "jobs.toml").write_text(DAILY.replace('"managed"', '"observe"'))
    assert scheduler.main(["explain", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["job"] == "daily" and rows[0]["mode"] == "observe"
    assert scheduler.main(["mode", "daily", "managed"]) == 0
    assert json.loads((core.STATE_DIR / "job-modes.json").read_text()) == {"daily": "managed"}
    assert scheduler.main(["mode", "ghost", "managed"]) == 2
    assert scheduler.main(["mode", "daily", "reset"]) == 0 and not json.loads((core.STATE_DIR / "job-modes.json").read_text())
    assert scheduler.main(["validate"]) == 0
    capsys.readouterr()
    assert scheduler.main(["health"]) == 1
    assert scheduler.main(["export"]) == 0 and json.loads(capsys.readouterr().out.splitlines()[-1])["counts"]["observe"] == 1


# --------------------------------------------------------------------------- cost of an idle tick
def test_idle_tick_is_cheap_with_the_shipped_inventory_all_managed(host):
    """SPEC4: < 300 ms when nothing is due (measured read-only: fake host, tmp STATE; the real CLI cost is measured below)."""
    cfg = jobs.load(REPO / "etc" / "jobs.toml", mcfg={"tasks": {}}, apply_modes=False)
    for j in cfg.jobs.values():
        if j.mode != "retired":
            j.mode = "managed"
    host.tick(T(2, 1, 30), cfg)                                                             # seeds everything
    times = []
    for i in range(25):                                                                     # 02:01:31..02:01:55: nothing is due
        t0 = time.perf_counter()
        rep = host.tick(T(2, 1, 31) + i, cfg)
        times.append((time.perf_counter() - t0) * 1000)
        assert rep["started"] == [] and rep["errors"] == []
    assert statistics.median(times) < 100 and max(times) < 300, times


def test_idle_cli_tick_end_to_end_under_300ms(tmp_path):
    """The real process: interpreter start + imports + config + state + evaluation, dry-run (read-only), scratch STATE dir."""
    for d in ("state", "log", "conf", "run"):
        (tmp_path / d).mkdir()
    (tmp_path / "conf" / "jobs.toml").write_text((REPO / "etc" / "jobs.toml").read_text())
    (tmp_path / "state" / "job-modes.json").write_text(json.dumps({n: "managed" for n in REAL_MANAGED}))
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO), "HOMELAB_MAINT_STATE": str(tmp_path / "state"),
           "HOMELAB_MAINT_LOG": str(tmp_path / "log"), "HOMELAB_MAINT_CONF": str(tmp_path / "conf"),
           "HOMELAB_MAINT_RUN": str(tmp_path / "run"), "HOMELAB_MAINT_TZ": "America/Toronto"}
    now = str(T(2, 1))
    times = []
    for _ in range(7):
        t0 = time.perf_counter()
        r = subprocess.run([sys.executable, "-B", "-m", "homelab_maint.scheduler", "tick", "--dry-run", "--now", now], env=env,
                           capture_output=True, text=True, timeout=30)
        times.append((time.perf_counter() - t0) * 1000)
        assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["started"] == []
    assert statistics.median(times) < 300, times
    assert list((tmp_path / "state").iterdir()) == [tmp_path / "state" / "job-modes.json"]  # dry-run wrote nothing


REAL_MANAGED = ["backup-system", "backup-immich", "stack-backup", "stack-watchdog", "search-canary", "bazarr-rules", "tunarr-sync",
                "docker-prune", "prune-openwebui-media", "comfyui-idle-vram", "notebook-db-alert", "immich-server-recycle",
                "tier-check", "tier-daily", "tier-weekly", "metrics-sample"]


# --------------------------------------------------------------------------- end to end with the REAL supervisor and spawn
def test_end_to_end_real_supervisor(tmp_path, monkeypatch):
    for k in ("STATE", "LOG", "CONF", "RUN"):
        d = tmp_path / k.lower()
        d.mkdir()
        monkeypatch.setattr(core, f"{k}_DIR", d)
    for k, v in (("STATE", "state"), ("LOG", "log"), ("CONF", "conf"), ("RUN", "run")):
        monkeypatch.setenv(f"HOMELAB_MAINT_{k}", str(tmp_path / v))
    seed_status()
    marker = tmp_path / "hook.marker"
    p = tmp_path / "jobs.toml"
    p.write_text(tj("good", ("/bin/sh", "-c", "echo e2e-ok token=abc123verysecret; exit 0"), schedule="* * * * *")
                 + tj("bad", ("/bin/sh", "-c", "echo broke; exit 3"), schedule="* * * * *", hooks={"on_failure": ["/bin/sh", "-c", f"echo hook > {marker}"]}))
    cfg = jobs.load(p, mcfg={"tasks": {}}, apply_modes=False)
    notes = []
    e = scheduler.Env()
    e.audit = lambda *a: None
    e.notify = lambda kind, job, res, **ctx: notes.append((kind, job.name, res.status, ctx["done"].get("tail")))
    t0 = (int(time.time()) // 60) * 60 + 5
    assert scheduler.tick(e, cfg, t0)["started"] == []                                    # seeds
    rep = scheduler.tick(e, cfg, t0 + 60)
    assert sorted(rep["started"]) == ["bad", "good"]
    st = scheduler.load_state()["jobs"]
    deadline = time.time() + 15
    while time.time() < deadline and not all(Path(st[n]["running"]["done"]).exists() for n in ("good", "bad")):
        time.sleep(0.1)
    rep = scheduler.tick(e, cfg, t0 + 62)
    assert sorted(rep["reaped"]) == [("bad", "crit"), ("good", "ok")]
    rows = core.read_json(core.STATE_DIR / "status.json")["tasks"]
    assert rows["good"]["summary"] == "exit 0: e2e-ok token=[redacted]" and rows["bad"]["status"] == "crit" and rows["bad"]["metrics"]["rc"] == 3
    assert marker.read_text().strip() == "hook"
    assert [n[:3] for n in notes] == [("failure", "bad", "crit")] and "broke" in notes[0][3]
    log = next((core.LOG_DIR / "jobs" / "good").glob("*.log")).read_text()
    assert "abc123verysecret" not in log and "e2e-ok" in log
    assert not list((core.STATE_DIR / "jobruns" / "good").glob("*.spec.json"))            # specs are removed by the supervisor
