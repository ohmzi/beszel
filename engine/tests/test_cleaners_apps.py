"""Tests for tasks/cleaners_apps.py (app_cache_trim, log_compress, dangling_images, crash_dumps, apt_cache).

Nothing here touches the host: `sh` is replaced (an unmocked command is rc 127), core.sh is stubbed so `logger` never
reaches the journal, /proc is a fake tree of symlinks under tmp_path (os.stat follows them to the real tmp inodes, exactly
as the real /proc/<pid>/fd links do), "root-owned" is simulated by pointing ca._ROOT_UID at the test's own uid, and the
clock only moves when the code sleeps. Every case is tested both ways: provably unused => selected, any doubt => kept."""
import errno
import gzip
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint is imported)
import pytest

from homelab_maint import core
from homelab_maint import inuse as inuse_mod
from homelab_maint.tasks import cleaners as cl
from homelab_maint.tasks import cleaners_apps as ca

NOW = 1_800_000_000.0
DAY = 86400
MIB = 1024 ** 2
PROTECTED = {"patterns": ["immich", "plexmediaserver", "postgres", "tunarr", "kometa", "/mnt/backup"]}
ME = os.getuid()
REAL_PROC_SCAN = ca._proc_scan


# --------------------------------------------------------------------------- harness
class FakeSh:
    """`sh` stand-in. rows: (prefix, response); response = (rc, stdout, stderr) or a callable(cmd_str) -> that."""

    def __init__(self, *rows):
        # defaults LAST so a test row wins: no container exists (mount discovery), logrotate idle and never run since boot
        self.rows = list(rows) + [("docker ps -a -q --no-trunc", (0, "", "")),
                                  ("systemctl show logrotate.service", (0, "ActiveState=inactive\nExecMainExitTimestampMonotonic=0\n", ""))]
        self.calls: list[str] = []

    def __call__(self, cmd, timeout=60, **kw):
        key = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.calls.append(key)
        for prefix, resp in self.rows:
            if key.startswith(prefix):
                rc, out, err = resp(key) if callable(resp) else resp
                return subprocess.CompletedProcess(cmd, rc, out, err)
        return subprocess.CompletedProcess(cmd, 127, "", "unmocked: " + key)

    def with_prefix(self, *prefixes) -> list[str]:
        return [c for c in self.calls if c.startswith(prefixes)]


def ok(out=""):
    return (0, out, "")


def real_gzip_t(key):
    """Run the REAL `gzip -t` so a good archive passes and a corrupt one fails."""
    r = subprocess.run(key.split(" ", 3)[:3] + [key.split(" -- ", 1)[1]], capture_output=True, text=True)
    return (r.returncode, r.stdout, r.stderr)


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(core, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(core, "CONF_DIR", tmp_path / "conf")
    (tmp_path / "conf").mkdir()
    monkeypatch.setattr(core, "sh", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))   # audit's `logger`
    monkeypatch.setattr(cl, "_busy", lambda name: (False, "idle"))
    for mod in (cl, ca):
        monkeypatch.setattr(mod, "_euid", lambda: 0)
        monkeypatch.setattr(mod, "sh", FakeSh())
    clock = [1000.0]
    for mod in (cl, ca):
        monkeypatch.setattr(mod, "_mono", lambda: clock[0])
        monkeypatch.setattr(mod, "_sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(ca, "_ROOT_UID", 0)
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(ca, "PROC", str(proc))
    monkeypatch.setattr(ca, "_inuse", None)
    monkeypatch.setattr(ca, "_http_ok", lambda url: True)
    yield clock


def use_sh(monkeypatch, *rows) -> FakeSh:
    f = FakeSh(*rows)
    monkeypatch.setattr(cl, "sh", f)
    monkeypatch.setattr(ca, "sh", f)
    return f


def mk(name, *, apply=False, now=NOW, protected=None, **opts):
    cfg = {"global": {}, "caps": {}, "protected": PROTECTED if protected is None else protected,
           "tasks": {name: {"mode": "apply" if apply else "report", **opts}}}
    return core.Ctx(cfg, name, apply, now)


def audit_rows(tmp_path) -> list[dict]:
    p = tmp_path / "log" / "audit.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


def outcomes(tmp_path) -> list[str]:
    return [r["outcome"] for r in audit_rows(tmp_path)]


def ascii_ok(res):
    assert len(res.summary) <= 140 and res.summary.isascii(), res.summary
    assert len(res.items) <= 12
    json.dumps(res.metrics)


def mkfile(path, age_s=10 * DAY, size=10, now=NOW, atime=None, data=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if data is not None else b"x" * size)
    os.utime(path, (now - age_s if atime is None else atime, now - age_s))
    return path


def fake_proc(tmp_path, holders: dict[int, list[Path]] | None = None, cwd: dict[int, Path] | None = None,
              maps: dict[int, list[Path]] | None = None):
    """A /proc tree: pid -> files held open (fd symlinks), cwd and mapped files (maps lines carry dev:inode)."""
    root = tmp_path / "proc"
    for pid in set(holders or {}) | set(cwd or {}) | set(maps or {}):
        d = root / str(pid)
        (d / "fd").mkdir(parents=True, exist_ok=True)
        for i, f in enumerate((holders or {}).get(pid, [])):
            (d / "fd" / str(3 + i)).symlink_to(f)
        if cwd and pid in cwd:
            (d / "cwd").symlink_to(cwd[pid])
        lines = []
        for f in (maps or {}).get(pid, []):
            st = os.stat(f)
            lines.append(f"7f0000000000-7f0000001000 r--p 00000000 {os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x} {st.st_ino} {f}")
        (d / "maps").write_text("\n".join(lines) + "\n")


def tree(d: Path) -> list[str]:
    return sorted(str(p.relative_to(d)) for p in d.rglob("*"))


# =========================================================================== registry / low-level helpers
def test_tasks_registered_c1_daily():
    for n in ("app_cache_trim", "log_compress", "dangling_images", "crash_dumps", "apt_cache"):
        t = core.REGISTRY[n]
        assert t.klass == "C1" and t.tier == "daily" and t.title and t.timeout >= 300, n


@pytest.mark.parametrize("name,want", [
    ("syslog.1", True), ("kern.log.12", True), ("auth.log.1", True), ("app-20260927", True), ("app-20260927.1", True),
    ("old.log.old", True), ("syslog", False), ("syslog.1.gz", False), ("kern.log.2.zst", False), ("plex-media-move.log", False),
    ("mysql-bin.000001", False), ("data.1234", False), ("x.1" + ca._TMP_SUFFIX, False), ("wtmp", False),
    ("syslog.1" + ca._DEL_SUFFIX, False)])
def test_rotated_name_detection(name, want):
    assert ca._is_rotated_log(name) is want


@pytest.mark.parametrize("url,want", [("http://127.0.0.1:8080/health", True), ("http://localhost/x", True),
                                      ("https://[::1]:9/", True), ("http://10.0.0.5/x", False), ("ftp://127.0.0.1/", False),
                                      ("http://127.0.0.1.evil.com/", False), (None, False), (5, False)])
def test_health_url_must_be_loopback(url, want):
    assert ca._loopback_url(url) is want


def test_parse_ts_handles_docker_formats():
    assert ca._parse_ts("2026-10-02T06:31:52.28164864Z") == pytest.approx(1790922712.28, abs=1)
    assert ca._parse_ts("2026-10-01T22:04:23.25795013-04:00") == pytest.approx(1790906663.26, abs=1)
    assert ca._parse_ts("0001-01-01T00:00:00Z") is None and ca._parse_ts("yesterday") is None


def test_proc_scan_sees_fd_cwd_and_maps_by_inode(tmp_path):
    a, b, c, d = (mkfile(tmp_path / n) for n in "abcd")
    fake_proc(tmp_path, holders={10: [a]}, cwd={11: tmp_path}, maps={12: [c]})
    want = {(os.stat(p).st_dev, os.stat(p).st_ino) for p in (a, b, c, d)} | {(os.stat(tmp_path).st_dev, os.stat(tmp_path).st_ino)}
    held, complete = ca._proc_scan(want)
    ids = {p: (os.stat(p).st_dev, os.stat(p).st_ino) for p in (a, b, c, d, tmp_path)}
    assert complete and held == {ids[a], ids[c], ids[tmp_path]}            # b and d are held by nobody


def test_proc_scan_unreadable_process_means_incomplete(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can read every /proc entry")
    f = mkfile(tmp_path / "f")
    fake_proc(tmp_path, holders={10: [f]})
    (tmp_path / "proc" / "10" / "fd").chmod(0)
    try:
        held, complete = ca._proc_scan({(os.stat(f).st_dev, os.stat(f).st_ino)})
    finally:
        (tmp_path / "proc" / "10" / "fd").chmod(0o755)
    assert not complete


def test_proc_scan_unreadable_proc_dir_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "PROC", str(tmp_path / "nope"))
    assert ca._proc_scan({(1, 2)}) == (set(), False)


def test_open_dir_refuses_symlink_anywhere(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    os.close(ca._open_dir(str(tmp_path / "real")))
    with pytest.raises(OSError):
        ca._open_dir(str(tmp_path / "link"))
    with pytest.raises(OSError):
        ca._open_dir(str(tmp_path / "link" / "x"))
    with pytest.raises(ValueError):
        ca._open_dir(str(tmp_path / "real" / ".." / "real"))


# =========================================================================== app_cache_trim
SUBS = "tunarr-cache"          # path component that the PROTECTED list matches ("tunarr")


def cache_ctx(tmp_path, apply=False, rules=None, **opts):
    root = tmp_path / "data"
    root.mkdir(exist_ok=True)
    rule = {"name": "subs", "path": str(root / "tunarr" / "subtitles"), "max_age_days": 30, "by": "mtime",
            "files_only": True, "root_owned_ok": True}
    opts.setdefault("allowed_roots", [str(root)])
    opts.setdefault("unprotect", ["data/tunarr/subtitles"])
    return mk("app_cache_trim", apply=apply, rules=[rule] if rules is None else rules, **opts)


def subs(tmp_path) -> Path:
    return tmp_path / "data" / "tunarr" / "subtitles"


def build_cache(tmp_path):
    """Tunarr-like: hashed two-level dirs, one cache file per leaf. Old = 40 d, mid = 20 d, fresh = 5 min."""
    s = subs(tmp_path)
    mkfile(s / "ab" / "cd" / "old1", 40 * DAY, 100)
    mkfile(s / "ab" / "cd" / "old2", 45 * DAY, 100)
    mkfile(s / "ab" / "ef" / "mid", 20 * DAY, 100)
    mkfile(s / "12" / "34" / "fresh", 300, 100)
    mkfile(s / "12" / "56" / "old3", 90 * DAY, 100)
    # a reader reset the ATIME of an old file: only mtime decides
    mkfile(s / "12" / "56" / "old_but_read", 50 * DAY, 100, atime=NOW - 60)
    return s


def healthy_sh(monkeypatch, state=None):
    st = state if state is not None else {"v": "running|healthy"}
    return use_sh(monkeypatch, ("docker inspect --format {{.State.Status}}", lambda k: ok(st["v"])))


def test_cache_trim_by_mtime_not_atime_dry_run_equals_apply(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    sh = healthy_sh(monkeypatch)
    rule = {"name": "subs", "path": str(s), "max_age_days": 30, "container": "tunarr-host-net", "root_owned_ok": True}
    before = tree(s)
    dry = ca.app_cache_trim(cache_ctx(tmp_path, rules=[rule]))
    assert tree(s) == before and dry.metrics["mode"] == "report" and dry.status == "info"
    assert "1 recent" in dry.items[0]["state"] and "1 newer" in dry.items[0]["state"]    # 5 min old != 20 d old
    assert set(outcomes(tmp_path)) == {"dry-run"} and dry.metrics["files"] == 4
    rows = audit_rows(tmp_path)
    assert [(r["action"], r["target"], r["bytes"]) for r in rows] == [("cache-trim", str(s / "12" / "56"), 400)]   # ONE act; keyed by its first dir
    assert dry.summary.startswith("report: would free 400 B (4 files in 2 dirs)")
    ascii_ok(dry)
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[rule]))
    assert res.status == "ok" and res.reclaimed_bytes == 400 and res.summary.startswith("freed 400 B (4 files)")
    left = {p.name for p in s.rglob("*") if p.is_file()}
    assert left == {"mid", "fresh"}                                  # old by mtime gone (incl. the one just READ)
    assert "docker inspect --format {{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} tunarr-host-net" \
        in sh.with_prefix("docker inspect --format {{.State.Status}}")[0]
    # directories stay (files_only), a second run has nothing left to do
    assert (s / "ab" / "cd").is_dir()
    again = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[rule]))
    assert again.reclaimed_bytes == 0 and again.metrics["files"] == 0


def test_cache_trim_apply_actions_are_the_dry_run_list(tmp_path, monkeypatch):
    build_cache(tmp_path)
    healthy_sh(monkeypatch)
    ca.app_cache_trim(cache_ctx(tmp_path, batch_files=2))
    dry = [(r["action"], r["target"], r["bytes"], r["outcome"]) for r in audit_rows(tmp_path)]
    (tmp_path / "log" / "audit.jsonl").unlink()
    ca.app_cache_trim(cache_ctx(tmp_path, apply=True, batch_files=2))
    done = [(r["action"], r["target"], r["bytes"], r["outcome"]) for r in audit_rows(tmp_path)]
    assert len(dry) == 2 and {x[3] for x in dry} == {"dry-run"} and {x[3] for x in done} == {"done"}
    assert [x[:3] for x in dry] == [x[:3] for x in done]


def test_cache_trim_root_owned_skipped_unless_allowed(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    monkeypatch.setattr(ca, "_ROOT_UID", ME)                          # every file here now counts as root-owned
    strict = {"name": "subs", "path": str(s), "max_age_days": 30}   # root_owned_ok defaults to false
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[strict]))
    assert res.metrics["files"] == 0 and "root-owned skipped" in res.items[0]["state"]
    assert len([p for p in s.rglob("*") if p.is_file()]) == 6
    allowed = ca.app_cache_trim(cache_ctx(tmp_path, apply=True))
    assert allowed.metrics["files"] == 4


def test_cache_trim_unhealthy_after_aborts_remaining_batches_and_rules(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    other = tmp_path / "data" / "other"
    mkfile(other / "x" / "old", 90 * DAY)
    n0 = len([p for p in s.rglob("*") if p.is_file()])
    # healthy until the first file has been deleted, unhealthy afterwards
    use_sh(monkeypatch, ("docker inspect --format {{.State.Status}}", lambda k: ok(
        "running|healthy" if len([p for p in s.rglob("*") if p.is_file()]) == n0 else "running|unhealthy")))
    rules = [{"name": "subs", "path": str(s), "max_age_days": 30, "container": "tunarr-host-net", "root_owned_ok": True},
             {"name": "other", "path": str(other), "max_age_days": 30, "container": "tunarr-host-net"}]
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=rules, check_every=1, health_wait_s=4, batch_files=2))
    assert res.status == "warn" and res.summary.startswith("ABORTED: tunarr-host-net health unhealthy after subs")
    assert res.metrics["aborted"].startswith("tunarr-host-net")
    gone = n0 - len([p for p in s.rglob("*") if p.is_file()])
    assert gone == 2                                     # only the first batch (2 files); the second batch never ran
    assert (other / "x" / "old").exists()                # the second rule never started
    assert res.items[1]["state"].startswith("skipped: aborted")
    assert "ABORTED: tunarr-host-net health unhealthy after subs" in res.items[0]["state"]
    assert "healthy before" not in res.items[0]["state"]
    assert res.metrics["deferred"] >= 1
    ascii_ok(res)


def test_cache_trim_post_rule_check_also_aborts(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    other = tmp_path / "data" / "other"
    mkfile(other / "x" / "old", 90 * DAY)
    use_sh(monkeypatch, ("docker inspect --format {{.State.Status}}", lambda k: ok(
        "running|healthy" if (other / "x" / "old").exists() and (s / "ab" / "cd" / "old1").exists() else "exited|none")))
    rules = [{"name": "subs", "path": str(s), "max_age_days": 30, "container": "c1", "root_owned_ok": True},
             {"name": "other", "path": str(other), "max_age_days": 30, "container": "c2"}]
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=rules, check_every=1000, health_wait_s=0, settle_s=3))
    assert res.status == "warn" and "ABORTED" in res.summary and (other / "x" / "old").exists()


def test_cache_trim_unhealthy_or_unknown_before_skips_the_rule(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    rule = {"name": "subs", "path": str(s), "max_age_days": 30, "container": "tunarr-host-net", "root_owned_ok": True}
    for resp in (ok("running|unhealthy"), ok("exited|none"), ok("running|starting"), (1, "", "No such object"), ok("garbage")):
        use_sh(monkeypatch, ("docker inspect --format {{.State.Status}}", resp))
        res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[rule]))
        assert res.items[0]["state"].startswith("skipped: tunarr-host-net not healthy"), resp
        assert len([p for p in s.rglob("*") if p.is_file()]) == 6
    use_sh(monkeypatch)                                  # docker missing entirely (rc 127)
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[rule]))
    assert "not healthy (docker inspect failed)" in res.items[0]["state"] and res.metrics["files"] == 0


def test_cache_trim_health_url_failure_is_unhealthy(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    healthy_sh(monkeypatch)
    monkeypatch.setattr(ca, "_http_ok", lambda url: False)
    rule = {"name": "subs", "path": str(s), "max_age_days": 30, "container": "c", "health_url": "http://127.0.0.1:8000/health",
            "root_owned_ok": True}
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[rule]))
    assert "health_url failed" in res.items[0]["state"] and len([p for p in s.rglob("*") if p.is_file()]) == 6


def test_cache_trim_health_recovers_within_wait(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    seen = []

    def resp(k):
        seen.append(k)
        return ok("running|starting" if len(seen) == 2 else "running|healthy")   # post-check: one slow poll, then healthy

    use_sh(monkeypatch, ("docker inspect --format {{.State.Status}}", resp))
    rule = {"name": "subs", "path": str(s), "max_age_days": 30, "container": "c", "root_owned_ok": True}
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[rule], health_wait_s=10))
    assert res.status == "ok" and "c healthy" in res.items[0]["state"] and res.metrics["files"] == 4


def test_cache_trim_open_file_is_refused_per_file(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    held = s / "ab" / "cd" / "old1"
    fake_proc(tmp_path, holders={4242: [held]})                      # e.g. the container reading it right now
    dry = ca.app_cache_trim(cache_ctx(tmp_path))
    assert dry.metrics["files"] == 3 and dry.metrics["open_skipped"] == 1 and "1 open skipped" in dry.items[0]["state"]
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True))
    assert held.exists() and not (s / "ab" / "cd" / "old2").exists() and res.metrics["files"] == 3


def test_cache_trim_open_file_found_only_in_the_refresh_is_kept(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    target = s / "12" / "56" / "old3"
    key = (os.stat(target).st_dev, os.stat(target).st_ino)
    calls = []

    def scan(want):
        calls.append(1)
        return ({key} if len(calls) > 1 else set()), True              # opened AFTER the first snapshot

    monkeypatch.setattr(ca, "_proc_scan", scan)
    monkeypatch.setattr(ca, "HOLD_REFRESH_S", -1)                       # every batch retakes the snapshot
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, batch_files=2))
    assert target.exists()                                               # opened after the first snapshot: kept
    assert not (s / "ab" / "cd" / "old1").exists() and not (s / "12" / "56" / "old_but_read").exists()
    assert len(calls) >= 3 and res.metrics["failed"] == 0


def test_cache_trim_incomplete_proof_without_root_degrades_to_report(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    monkeypatch.setattr(ca, "_proc_scan", lambda want: (set(), False))
    dry = ca.app_cache_trim(cache_ctx(tmp_path))
    assert dry.metrics["files"] == 4                                   # report still shows what it would do
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True))
    assert res.items[0]["state"].startswith("refused: needs root") and res.metrics["files"] == 0
    assert len([p for p in s.rglob("*") if p.is_file()]) == 6


def test_cache_trim_not_root_dirs_degrade_in_apply_and_say_so_in_report(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    for mod in (ca,):
        monkeypatch.setattr(mod, "_euid", lambda: 1000)
    monkeypatch.setattr(ca.os, "access", lambda p, m: False)           # the (container-owned) dirs are not writable
    dry = ca.app_cache_trim(cache_ctx(tmp_path))
    assert dry.metrics["needs_root"] == 2 and "need root to apply" in dry.summary and dry.metrics["files"] == 4
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True))
    assert res.metrics["needs_root"] == 2 and res.reclaimed_bytes == 0 and "need root" in res.summary
    assert len([p for p in s.rglob("*") if p.is_file()]) == 6


@pytest.mark.parametrize("patch,why", [
    ({"max_age_days": 0}, "nothing"), ({"max_age_days": -5}, "nothing"), ({"max_age_days": "30"}, "nothing"),
    ({"max_age_days": None}, "nothing"), ({"by": "atime"}, "refused"), ({"by": "ctime"}, "refused"),
    ({"files_only": "yes"}, "refused"), ({"container": "a b"}, "refused"), ({"container": "c", "health_url": "http://evil.example/x"}, "refused"),
    ({"health_url": "http://127.0.0.1/x"}, "refused"), ({"gate": "Rm -rf"}, "refused"), ({"path": "relative/dir"}, "refused"),
    ({"path": "/"}, "refused"), ({"name": ""}, "refused")])
def test_cache_trim_bad_rules_select_nothing(tmp_path, monkeypatch, patch, why):
    s = build_cache(tmp_path)
    healthy_sh(monkeypatch)
    rule = {"name": "subs", "path": str(s), "max_age_days": 30, "root_owned_ok": True, **patch}
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[rule]))
    assert res.items[0]["state"].startswith(why), res.items
    assert res.reclaimed_bytes == 0 and len([p for p in s.rglob("*") if p.is_file()]) == 6


def test_cache_trim_confinement(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    healthy_sh(monkeypatch)
    base = {"name": "r", "max_age_days": 30, "root_owned_ok": True}

    def run(path, **opts):
        return ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[{**base, "path": str(path)}], **opts)).items[0]["state"]

    outside = tmp_path / "elsewhere" / "deep" / "dir"
    mkfile(outside / "old", 90 * DAY)
    assert run(outside).startswith("refused: outside allowed_roots") and (outside / "old").exists()
    assert run(s, allowed_roots=[]).startswith("refused: no allowed_roots")
    (tmp_path / "data" / "link").symlink_to(s)
    assert run(tmp_path / "data" / "link").startswith("refused: symlink in path")
    # protected without an unprotect exemption
    assert run(s, unprotect=[]).startswith("refused: protected path")
    for bad in ("/var/lib/docker/volumes/x/_data", "/mnt/backup/system/cache", "/media/Immich/thumbs/old", "/home/me/.config/Cursor/cache",
                "/home/me/.cursor/projects", "/home/me/models/hermes/blobs", "/home/me/ai-stack/data/x", "/srv/surreal_data/db/old",
                "/var/lib/libvirt/images/a", "/usr/share/ollama/.ollama/models", "/volume1/docker/comfyui/models/x"):
        assert run(bad, allowed_roots=[os.path.dirname(bad)], unprotect=[".*"]) == "refused: never-touch path", bad
    assert len([p for p in s.rglob("*") if p.is_file()]) == 6 and outside.exists()


def test_cache_trim_never_follows_symlinks(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    healthy_sh(monkeypatch)
    victim = mkfile(tmp_path / "victim" / "precious", 400 * DAY)
    (s / "ab" / "cd" / "evil_link").symlink_to(victim)
    (s / "ab" / "dirlink").symlink_to(tmp_path / "victim")
    os.utime(s / "ab" / "cd" / "evil_link", (NOW - 90 * DAY, NOW - 90 * DAY), follow_symlinks=False)
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True))
    assert victim.exists() and (s / "ab" / "cd" / "evil_link").is_symlink() and (s / "ab" / "dirlink").is_symlink()
    assert res.metrics["files"] == 4 and "2 symlinks skipped" in res.items[0]["state"]


def test_cache_trim_file_changed_after_scan_is_skipped(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    real = ca._walk_cache

    def scan_then_touch(*a, **kw):
        out = real(*a, **kw)
        os.utime(s / "ab" / "cd" / "old1", (NOW, NOW))                  # the app rewrote it between scan and delete
        return out

    monkeypatch.setattr(ca, "_walk_cache", scan_then_touch)
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True))
    assert (s / "ab" / "cd" / "old1").exists() and not (s / "ab" / "cd" / "old2").exists() and res.metrics["failed"] == 0


def test_cache_trim_directory_swapped_for_symlink_after_scan_is_not_followed(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    victim = mkfile(tmp_path / "victim" / "old1", 90 * DAY)
    real = ca._walk_cache

    def scan_then_swap(*a, **kw):
        out = real(*a, **kw)
        for p in (s / "ab" / "cd").iterdir():
            p.unlink()
        (s / "ab" / "cd").rmdir()
        (s / "ab" / "cd").symlink_to(tmp_path / "victim")               # same name, now a symlink to somewhere else
        return out

    monkeypatch.setattr(ca, "_walk_cache", scan_then_swap)
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True))
    assert victim.exists() and res.metrics["failed"] == 0 and res.status == "ok"       # skipped as a benign race, not an error
    assert not (s / "12" / "56" / "old3").exists()                                     # the rest of the batch still ran


def test_cache_trim_caps_defer_the_rest(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, max_items_per_run=1, batch_files=2))
    assert res.metrics["deferred"] >= 1 and res.metrics["capped"] and res.metrics["files"] == 2
    left = [p.name for p in s.rglob("*") if p.is_file()]
    assert len(left) == 4
    res2 = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, max_items_per_run=1, batch_files=2))
    assert len([p for p in s.rglob("*") if p.is_file()]) == 2            # the next run continues where the cap stopped


def test_cache_trim_large_cache_costs_few_actions_not_one_per_file(tmp_path, monkeypatch):
    s = subs(tmp_path)
    for i in range(2500):                                      # Tunarr layout: two hash levels, ~1 file per leaf dir
        mkfile(s / f"{i % 16:02x}" / f"{i // 16:03x}" / f"f{i}", 60 * DAY, 4)
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, batch_files=1000))
    assert res.metrics["selected"] == 3 and res.metrics["files"] == 2500 and res.reclaimed_bytes == 10000
    assert len(audit_rows(tmp_path)) == 3 and not list(s.rglob("f*"))


def test_cache_trim_age_boundary_is_strictly_older(tmp_path, monkeypatch):
    s = subs(tmp_path)
    exact = mkfile(s / "a" / "exact", 30 * DAY)
    older = mkfile(s / "a" / "older", 30 * DAY + 1)
    ca.app_cache_trim(cache_ctx(tmp_path, apply=True))
    assert exact.exists() and not older.exists()


def test_cache_trim_batches_large_directories(tmp_path, monkeypatch):
    s = subs(tmp_path)
    for i in range(7):
        mkfile(s / "d" / f"f{i}", 60 * DAY, 10)
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, batch_files=3))
    assert res.metrics["selected"] == 3 and res.metrics["files"] == 7 and not list(s.rglob("f*"))


def test_cache_trim_empty_old_dirs_only_when_not_files_only(tmp_path, monkeypatch):
    s = subs(tmp_path)
    mkfile(s / "keep" / "young", 5 * DAY)
    (s / "emptyold").mkdir(parents=True)
    os.utime(s / "emptyold", (NOW - 90 * DAY, NOW - 90 * DAY))
    (s / "emptynew").mkdir()
    os.utime(s / "emptynew", (NOW - 3600, NOW - 3600))
    rule = {"name": "subs", "path": str(s), "max_age_days": 30, "files_only": True}
    ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[rule]))
    assert (s / "emptyold").exists()                                      # files_only: directories are never removed
    ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[{**rule, "files_only": False}]))
    assert not (s / "emptyold").exists() and (s / "emptynew").exists() and (s / "keep").exists()


def test_cache_trim_scan_limit_refuses_the_rule(tmp_path, monkeypatch):
    s = subs(tmp_path)
    for i in range(1100):
        mkfile(s / f"{i % 10}" / f"f{i}", 90 * DAY, 1)
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, scan_limit=1000))
    assert res.items[0]["state"].startswith("refused: scan limit") and len(list(s.rglob("f*"))) == 1100


def test_cache_trim_pause_and_report_mode_never_delete(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    (tmp_path / "conf" / "PAUSE").write_text("")
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True))
    assert res.metrics["mode"] == "report" and len([p for p in s.rglob("*") if p.is_file()]) == 6
    (tmp_path / "conf" / "PAUSE").unlink()
    ctx = cache_ctx(tmp_path, apply=True)
    ctx.tcfg["mode"] = "report"
    ctx.apply = False
    assert ca.app_cache_trim(ctx).reclaimed_bytes == 0 and len([p for p in s.rglob("*") if p.is_file()]) == 6


def test_cache_trim_busy_gate_skips_rule(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    monkeypatch.setattr(cl, "_busy", lambda name: (True, f"{name} busy"))
    rule = {"name": "subs", "path": str(s), "max_age_days": 30, "root_owned_ok": True, "gate": "plex"}
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[rule]))
    assert res.items[0]["state"] == "skipped: plex busy" and len([p for p in s.rglob("*") if p.is_file()]) == 6


def test_cache_trim_reports_unreadable_dirs(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root can read everything")
    s = build_cache(tmp_path)
    (s / "ab").chmod(0)
    try:
        res = ca.app_cache_trim(cache_ctx(tmp_path))
    finally:
        (s / "ab").chmod(0o755)
    assert "dirs unreadable (need root)" in res.items[0]["state"] and res.metrics["files"] == 2


def test_cache_trim_second_rule_example_without_container_and_empty_config(tmp_path, monkeypatch):
    other = tmp_path / "data" / "kavita" / "cache"
    mkfile(other / "a", 20 * DAY)
    mkfile(other / "b", 3 * DAY)
    rule = {"name": "kav", "path": str(other), "max_age_days": 14}
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[rule]))
    assert not (other / "a").exists() and (other / "b").exists() and res.metrics["files"] == 1
    assert ca.app_cache_trim(cache_ctx(tmp_path, rules=[])).summary == "no cache rules configured"
    assert ca.app_cache_trim(cache_ctx(tmp_path, scan_limit=1)).status == "skipped"


def test_cache_trim_protected_target_is_never_acted_on(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    ctx = cache_ctx(tmp_path, apply=True, unprotect=["data/tunarr/subtitles$"])      # exempts the root only, not the leaf dirs
    res = ca.app_cache_trim(ctx)
    assert res.metrics["protected"] == 2 and res.reclaimed_bytes == 0
    assert len([p for p in s.rglob("*") if p.is_file()]) == 6 and "done" not in outcomes(tmp_path)


# =========================================================================== log_compress
def log_ctx(tmp_path, apply=False, **opts):
    return mk("log_compress", apply=apply, roots=[str(tmp_path / "log_root")], min_mib=1, **opts)


def logs(tmp_path) -> Path:
    return tmp_path / "log_root"


def big(n_mib=2) -> bytes:
    line = b"Oct  2 12:00:00 host svc[1]: some repetitive log line number 42\n"
    return (line * (n_mib * MIB // len(line) + 1))[: n_mib * MIB]


def test_log_compress_gzips_rotated_verifies_and_removes_original(tmp_path, monkeypatch):
    d = logs(tmp_path)
    data = big(3)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=data)
    os.chmod(rot, 0o640)
    live = mkfile(d / "syslog", 3 * DAY, data=big(3))               # the LIVE log: big and old, but not rotated
    small = mkfile(d / "kern.log.1", 3 * DAY, size=1000)
    young = mkfile(d / "auth.log.1", 600, data=big(2))
    done = mkfile(d / "ufw.log.2.gz", 3 * DAY, data=gzip.compress(big(2)))
    sh = use_sh(monkeypatch, ("gzip -t -- ", real_gzip_t))
    dry = ca.log_compress(log_ctx(tmp_path))
    assert rot.exists() and not (d / "syslog.1.gz").exists() and dry.metrics["selected"] == 1
    assert dry.summary.startswith("report: would compress 1 logs (3.0 MiB raw)") and not sh.calls
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    gz = d / "syslog.1.gz"
    assert not rot.exists() and gz.exists() and gzip.decompress(gz.read_bytes()) == data
    assert res.reclaimed_bytes == len(data) - gz.stat().st_size > 0 and res.summary.startswith("compressed 1 logs, freed")
    assert oct(gz.stat().st_mode & 0o777) == "0o640" and int(gz.stat().st_mtime) == int(NOW - 3 * DAY)
    assert live.exists() and small.exists() and young.exists() and done.exists()      # nothing else touched
    assert not list(d.glob("*hm-tmp")) and sh.with_prefix("gzip -t -- ")
    ascii_ok(res)
    again = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert again.reclaimed_bytes == 0 and again.metrics["candidates"] == 0


def test_log_compress_gzip_t_failure_leaves_original(tmp_path, monkeypatch):
    d = logs(tmp_path)
    data = big(2)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=data)
    sh = use_sh(monkeypatch, ("gzip -t -- ", (1, "", "gzip: invalid compressed data--crc error")))
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert rot.read_bytes() == data and not (d / "syslog.1.gz").exists() and not list(d.glob("*hm-tmp"))
    assert res.metrics["failed"] == 1 and res.status == "warn" and "gzip -t failed" in res.summary and res.reclaimed_bytes == 0
    assert sh.with_prefix("gzip -t -- ")


def test_log_compress_length_mismatch_leaves_original(tmp_path, monkeypatch):
    d = logs(tmp_path)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    use_sh(monkeypatch, ("gzip -t -- ", ok()))                                          # gzip -t says fine ...
    monkeypatch.setattr(ca, "struct", SimpleNamespace(unpack=lambda fmt, b: (123,)))    # ... but the stored length is wrong
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert rot.exists() and not (d / "syslog.1.gz").exists() and not list(d.glob("*hm-tmp"))
    assert "length check failed" in res.summary and res.metrics["failed"] == 1


def test_log_compress_open_file_is_refused(tmp_path, monkeypatch):
    d = logs(tmp_path)
    held = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    free = mkfile(d / "kern.log.1", 3 * DAY, data=big(2))
    fake_proc(tmp_path, holders={999: [held]})                          # rsyslog never reopened after rotation
    sh = use_sh(monkeypatch, ("gzip -t -- ", real_gzip_t))
    dry = ca.log_compress(log_ctx(tmp_path))
    assert dry.metrics["selected"] == 1 and dry.metrics["open_kept"] == 1 and "open kept" in dry.summary
    assert any(i["state"] == "kept: open by a process" for i in dry.items)
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert held.exists() and not (d / "syslog.1.gz").exists() and not free.exists() and (d / "kern.log.1.gz").exists()
    assert all("syslog.1" not in c for c in sh.calls)


def test_log_compress_file_opened_between_task_scan_and_compress_start_is_kept(tmp_path, monkeypatch):
    d = logs(tmp_path)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    key = (os.stat(rot).st_dev, os.stat(rot).st_ino)
    n = []

    def scan(want):
        n.append(1)
        return (set() if len(n) == 1 else {key}), True                  # scan 1 = task selection, scan 2 = just before gzip

    monkeypatch.setattr(ca, "_proc_scan", scan)
    sh = use_sh(monkeypatch, ("gzip -t -- ", real_gzip_t))
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert rot.exists() and res.metrics["gone"] == 1 and not sh.with_prefix("gzip") and not list(d.glob("*.gz*")) and len(n) == 2


def test_log_compress_cwd_and_mapping_count_as_open_too(tmp_path, monkeypatch):
    d = logs(tmp_path)
    mapped = mkfile(d / "a.log.1", 3 * DAY, data=big(2))
    fake_proc(tmp_path, maps={5: [mapped]})
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert mapped.exists() and res.metrics["open_kept"] == 1


def test_log_compress_file_opened_while_compressing_is_kept(tmp_path, monkeypatch):
    d = logs(tmp_path)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    key = (os.stat(rot).st_dev, os.stat(rot).st_ino)
    n = []

    def scan(want):
        n.append(1)
        return (set() if len(n) <= 2 else {key}), True                  # scan 1 = task, 2 = before compress, 3 = after

    monkeypatch.setattr(ca, "_proc_scan", scan)
    use_sh(monkeypatch, ("gzip -t -- ", real_gzip_t))
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert rot.exists() and not (d / "syslog.1.gz").exists() and not list(d.glob("*hm-tmp")) and len(n) == 3
    assert res.metrics["gone"] == 1 and res.reclaimed_bytes == 0


def test_log_compress_source_grew_after_scan_is_kept(tmp_path, monkeypatch):
    d = logs(tmp_path)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    real = ca._scan_logs

    def scan_then_append(*a, **kw):
        out = real(*a, **kw)
        with open(rot, "ab") as f:
            f.write(b"late line\n")
        return out

    monkeypatch.setattr(ca, "_scan_logs", scan_then_append)
    use_sh(monkeypatch, ("gzip -t -- ", real_gzip_t))
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert rot.exists() and not (d / "syslog.1.gz").exists() and res.metrics["gone"] == 1


def test_log_compress_existing_gz_is_never_overwritten(tmp_path, monkeypatch):
    d = logs(tmp_path)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    prior = mkfile(d / "syslog.1.gz", 5 * DAY, data=b"precious earlier archive")
    sh = use_sh(monkeypatch, ("gzip -t -- ", real_gzip_t))
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert rot.exists() and prior.read_bytes() == b"precious earlier archive" and res.metrics["failed"] == 1
    assert "target .gz exists" in res.summary and not [c for c in sh.calls if c.startswith("gzip")]   # refused before any work


def test_log_compress_symlink_and_hardlink_are_skipped(tmp_path, monkeypatch):
    d = logs(tmp_path)
    victim = mkfile(tmp_path / "elsewhere" / "data.log", 3 * DAY, data=big(2))
    d.mkdir(parents=True, exist_ok=True)
    (d / "syslog.1").symlink_to(victim)
    hl = mkfile(d / "other.log.1", 3 * DAY, data=big(2))
    os.link(hl, d / "other.log.hardlink-copy")
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert victim.exists() and hl.exists() and res.metrics["candidates"] == 0


def test_log_compress_not_enough_free_space_keeps_original(tmp_path, monkeypatch):
    d = logs(tmp_path)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    monkeypatch.setattr(ca.os, "statvfs", lambda p: SimpleNamespace(f_bavail=1, f_frsize=4096))
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert rot.exists() and "not enough free space" in res.summary


def test_log_compress_without_root_apply_is_refused_dry_run_is_partial(tmp_path, monkeypatch):
    d = logs(tmp_path)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    monkeypatch.setattr(ca, "_proc_scan", lambda want: (set(), False))
    dry = ca.log_compress(log_ctx(tmp_path))
    assert dry.metrics["selected"] == 1 and "open-file proof incomplete" in dry.summary and dry.metrics["proof"].startswith("partial")
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert rot.exists() and res.metrics["needs_root"] == 1 and res.reclaimed_bytes == 0 and "need root" in res.summary


def test_log_compress_protected_and_never_touch_and_depth(tmp_path, monkeypatch):
    d = logs(tmp_path)
    pg = mkfile(d / "postgresql" / "postgresql-16-main.log.1", 3 * DAY, data=big(2))     # matches the protected list
    deep = mkfile(d / "a" / "b" / "c" / "x.log.1", 3 * DAY, data=big(2))
    jr = mkfile(d / "journal" / "system.log.1", 3 * DAY, data=big(2))
    use_sh(monkeypatch, ("gzip -t -- ", real_gzip_t))
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert pg.exists() and deep.exists() and jr.exists() and res.metrics["protected"] == 1
    deeper = ca.log_compress(log_ctx(tmp_path, apply=True, max_depth=4, exclude_dirs=[]))
    assert not deep.exists() and not jr.exists() and pg.exists()
    bad = mk("log_compress", apply=True, roots=["/var/lib/docker/volumes"], min_mib=1)
    assert ca.log_compress(bad).metrics["roots"] == 1 and ca.log_compress(bad).summary.startswith("no rotated log")
    assert ca.log_compress(mk("log_compress", roots=[], min_mib=1)).status == "skipped"
    assert ca.log_compress(mk("log_compress", roots=[str(d)], min_mib=-1)).status == "skipped"


def test_log_compress_caps_and_oversize(tmp_path, monkeypatch):
    d = logs(tmp_path)
    for i in range(3):
        mkfile(d / f"l{i}.log.1", (3 + i) * DAY, data=big(2))
    use_sh(monkeypatch, ("gzip -t -- ", real_gzip_t))
    res = ca.log_compress(log_ctx(tmp_path, apply=True, max_items_per_run=1))
    assert res.metrics["deferred"] == 2 and len(list(d.glob("*.gz"))) == 1
    over = ca.log_compress(log_ctx(tmp_path, apply=True, max_gib_per_run=0.001))        # 1 MiB cap < 2 MiB logs
    assert over.metrics["oversize"] == 2 and len(list(d.glob("*.gz"))) == 1


def test_log_compress_dry_run_is_exactly_the_apply_list(tmp_path, monkeypatch):
    d = logs(tmp_path)
    for i in range(3):
        mkfile(d / f"l{i}.log.1", (3 + i) * DAY, data=big(2))
    use_sh(monkeypatch, ("gzip -t -- ", real_gzip_t))
    ca.log_compress(log_ctx(tmp_path))
    dry = [r["target"] for r in audit_rows(tmp_path)]
    (tmp_path / "log" / "audit.jsonl").unlink()
    ca.log_compress(log_ctx(tmp_path, apply=True))
    assert dry == [r["target"] for r in audit_rows(tmp_path)] and len(dry) == 3


# =========================================================================== dangling_images
def sha(c: str) -> str:
    return "sha256:" + c * 64


A, B, C, D, E = sha("a"), sha("b"), sha("c"), sha("d"), sha("e")


def iso(age_h: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(NOW - age_h * 3600)) + ".123456789Z"


def docker_rows(imgs, referenced=(), digests=None, df_used=()):
    """FakeSh rows for: dangling listing + inspect, `docker system df -v` (UniqueSize = imgs[i][1]; the inspect Size is
    the 10x VIRTUAL size), container list + container image refs. Digests default to one (a re-pullable image)."""
    digests = {i: ["repo/x@sha256:" + "9" * 64] for i in imgs} | (digests or {})

    def inspect(k):
        ids = [x for x in k.split() if x.startswith("sha256:")]
        return ok("\n".join(f"{i}|{iso(imgs[i][0])}|{imgs[i][1] * 10}|{imgs[i][2] if len(imgs[i]) > 2 else 0}|{json.dumps(digests.get(i, []))}" for i in ids))

    def df(k):
        return ok(json.dumps({"Images": [{"ID": i, "Repository": "<none>", "Tag": "<none>", "Size": f"{v[1] * 10}B",
                                          "UniqueSize": f"{v[1]}B", "Containers": "1" if i in df_used else "0"} for i, v in imgs.items()]}))

    return [("docker image ls --filter dangling=true", lambda k: ok("\n".join(imgs))),
            ("docker image inspect", inspect), ("docker system df -v", df),
            ("docker ps -a -q --no-trunc", lambda k: ok("\n".join(f"{'c' * 12}{n}" for n in range(len(referenced))))),
            ("docker container inspect", lambda k: ok("\n".join(referenced))),
            ("docker image rm", ok())]


def seed_since(tmp_path, ids, age_h=30, now=NOW):
    """The task's OWN first-seen clock (state/tasks/dangling_images.json): these images were first seen `age_h` ago."""
    p = tmp_path / "state" / "tasks" / "dangling_images.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"dangling_since": {i: now - age_h * 3600 for i in ids}}))


def write_ledger(tmp_path, images: dict, created=NOW - 10 * DAY, updated=NOW - 600):
    p = tmp_path / "state" / "ledger" / "images.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"created": created, "updated": updated, "version": 1, "images": images}))


def test_dangling_selects_only_old_unreferenced_unused_images(tmp_path, monkeypatch):
    imgs = {A: (72, 100), B: (72, 200), C: (5, 300), D: (72, 400)}
    sh = use_sh(monkeypatch, *docker_rows(imgs, referenced=[B]))
    seed_since(tmp_path, [A, B, C, D])
    write_ledger(tmp_path, {D[:19]: {"last_seen": NOW - 2 * 3600, "names": ["x"]}})   # 12-char prefix key: still matches D
    dry = ca.dangling_images(mk("dangling_images"))
    states = {i["name"].split()[1]: i["state"] for i in dry.items}
    assert dry.metrics["selected"] == 1 and dry.status == "info"
    assert states["bbbbbbbbbbbb"] == "kept: referenced by a container"
    assert states["dddddddddddd"].startswith("kept: ledger: used 2 h ago")
    assert dry.metrics["young"] == 1 and dry.metrics["referenced"] == 1 and dry.metrics["recent_use"] == 1
    assert "14" not in dry.summary and not sh.with_prefix("docker image rm")
    ascii_ok(dry)
    res = ca.dangling_images(mk("dangling_images", apply=True))
    assert sh.with_prefix("docker image rm") == [f"docker image rm {A}"]              # exactly A, by id, no -f, no prune, no -a
    assert not sh.with_prefix("docker image prune", "docker system prune") and not any(" -f" in c or " -a" in c for c in sh.calls if "rm" in c)
    assert res.summary.startswith("freed 100 B (1 images)") and res.status == "ok"


def test_dangling_without_a_usable_ledger_selects_nothing_even_after_the_own_clock_ran_out(tmp_path, monkeypatch):
    sh = use_sh(monkeypatch, *docker_rows({A: (72, 100)}))
    first = mk("dangling_images", apply=True)
    res = ca.dangling_images(first)                                   # no ledger at all
    assert res.status == "skipped" and "image ledger missing" in res.summary and not sh.with_prefix("docker image rm")
    first.save_state()
    later = mk("dangling_images", apply=True, now=NOW + 25 * 3600)    # own clock is now 25 h old: still not enough alone
    res2 = ca.dangling_images(later)
    assert res2.status == "skipped" and not sh.with_prefix("docker image rm")
    assert core.read_json(first.state_path)["dangling_since"][A] == NOW             # the clock itself keeps running


def test_dangling_stale_ledger_proves_nothing(tmp_path, monkeypatch):
    sh = use_sh(monkeypatch, *docker_rows({A: (72, 100)}))
    seed_since(tmp_path, [A])
    write_ledger(tmp_path, {}, updated=NOW - 5 * DAY)                                  # sampler dead: ledger proves nothing
    res = ca.dangling_images(mk("dangling_images", apply=True))
    assert not sh.with_prefix("docker image rm") and res.status == "skipped" and "ledger stale" in res.summary


def test_dangling_young_ledger_cannot_prove_an_absent_image_unused(tmp_path, monkeypatch):
    sh = use_sh(monkeypatch, *docker_rows({A: (72, 100)}))
    seed_since(tmp_path, [A])
    write_ledger(tmp_path, {}, created=NOW - 3600)                                     # ledger only exists for 1 h
    res = ca.dangling_images(mk("dangling_images", apply=True))
    assert not sh.with_prefix("docker image rm") and "ledger too young" in res.items[0]["state"]


def test_dangling_odd_ledger_entry_keeps_the_image(tmp_path, monkeypatch):
    sh = use_sh(monkeypatch, *docker_rows({A: (72, 100)}))
    seed_since(tmp_path, [A])
    write_ledger(tmp_path, {A: {"last_seen": "yesterday"}})
    res = ca.dangling_images(mk("dangling_images", apply=True))
    assert not sh.with_prefix("docker image rm") and "ledger entry unreadable" in res.items[0]["state"]


@pytest.mark.parametrize("breakage", ["ls_fails", "inspect_fails", "ps_fails", "container_inspect_fails", "bad_id", "short_inspect",
                                      "tagged_image", "bad_date", "bad_json"])
def test_dangling_any_probe_doubt_selects_nothing(tmp_path, monkeypatch, breakage):
    rows2 = list(docker_rows({A: (72, 100)}, referenced=[sha("f")]))      # one unrelated container so its inspect is exercised
    bad = {"ls_fails": ("docker image ls", (1, "", "boom")), "inspect_fails": ("docker image inspect", (1, "", "boom")),
           "ps_fails": ("docker ps -a", (1, "", "boom")), "container_inspect_fails": ("docker container inspect", (1, "", "x")),
           "bad_id": ("docker image ls", ok("not-an-id")), "short_inspect": ("docker image inspect", ok("")),
           "tagged_image": ("docker image inspect", ok(f"{A}|{iso(72)}|100|1|[]")),
           "bad_date": ("docker image inspect", ok(f"{A}|whenever|100|0|[]")),
           "bad_json": ("docker image inspect", ok(f"{A}|{iso(72)}|100|0|nope"))}[breakage]
    sh = use_sh(monkeypatch, bad, *rows2)
    write_ledger(tmp_path, {})
    res = ca.dangling_images(mk("dangling_images", apply=True))
    assert res.status == "skipped" and not sh.with_prefix("docker image rm")


def test_dangling_null_digests_are_tolerated_and_wait_the_long_local_build_window(tmp_path, monkeypatch):
    def rows(age_h):
        r = docker_rows({A: (age_h, 100)})
        r[1] = ("docker image inspect", ok(f"{A}|{iso(age_h)}|1000|0|null"))      # Go prints a nil slice as null
        return r

    sh = use_sh(monkeypatch, *rows(10 * 24))
    seed_since(tmp_path, [A], age_h=30)                       # 30 h unreferenced: plenty for a re-pullable image ...
    write_ledger(tmp_path, {})
    res = ca.dangling_images(mk("dangling_images", apply=True))
    assert not sh.with_prefix("docker image rm") and "unreferenced only 30 h of 168" in res.items[0]["state"]    # ... not for a local build
    seed_since(tmp_path, [A], age_h=8 * 24)
    write_ledger(tmp_path, {}, created=NOW - 20 * DAY)
    res = ca.dangling_images(mk("dangling_images", apply=True))
    assert res.metrics["selected"] == 1 and sh.with_prefix("docker image rm") == [f"docker image rm {A}"]
    assert "local build" in " ".join(i["name"] + i["state"] for i in res.items)


def test_dangling_never_seen_image_is_not_selected_on_its_first_sighting(tmp_path, monkeypatch):
    """REVIEW (high): `docker pull`/`docker load` of an image whose Created is 40 d old (upstream build date), ledger 10 d old
    with no entry for it (the ledger only knows images a container used). The old code removed it on the FIRST run."""
    sh = use_sh(monkeypatch, *docker_rows({A: (40 * 24, 100)}))
    write_ledger(tmp_path, {}, created=NOW - 10 * DAY, updated=NOW - 600)
    first = mk("dangling_images", apply=True)
    res = ca.dangling_images(first)
    assert res.metrics["selected"] == 0 and not sh.with_prefix("docker image rm") and "unreferenced only 0 h of 24" in res.items[0]["state"]
    first.save_state()
    # 25 h later (ledger kept fresh by the sampler, still never saw it in use): both proofs hold now
    write_ledger(tmp_path, {}, created=NOW - 10 * DAY, updated=NOW + 25 * 3600 - 600)
    later = mk("dangling_images", apply=True, now=NOW + 25 * 3600)
    res2 = ca.dangling_images(later)
    assert res2.metrics["selected"] == 1 and sh.with_prefix("docker image rm") == [f"docker image rm {A}"]


def test_dangling_own_clock_restarts_when_an_image_was_referenced_in_between(tmp_path, monkeypatch):
    use_sh(monkeypatch, *docker_rows({A: (72, 100)}, referenced=[A]))
    seed_since(tmp_path, [A], age_h=300)
    write_ledger(tmp_path, {})
    ctx = mk("dangling_images", apply=True)
    ca.dangling_images(ctx)
    assert A not in ctx.state["dangling_since"]                          # a container used it: the clock is gone
    sh = use_sh(monkeypatch, *docker_rows({A: (72, 100)}))              # container removed again, image dangling again
    res = ca.dangling_images(ctx)
    assert res.metrics["selected"] == 0 and not sh.with_prefix("docker image rm")


def test_dangling_books_unique_size_not_the_virtual_size(tmp_path, monkeypatch):
    """REVIEW (medium): inspect .Size counts layers shared with images that stay; UniqueSize is what rm really frees."""
    imgs = {A: (72, 100), B: (72, 0)}                                    # B shares every layer with a running image
    use_sh(monkeypatch, *docker_rows(imgs))
    seed_since(tmp_path, [A, B])
    write_ledger(tmp_path, {})
    dry = ca.dangling_images(mk("dangling_images"))
    assert dry.summary.startswith("report: would free 100 B (2 images)")        # NOT 1000 B + 0 (the 10x virtual sizes)
    res = ca.dangling_images(mk("dangling_images", apply=True))
    assert res.reclaimed_bytes == 100 and sorted(r["bytes"] for r in audit_rows(tmp_path) if r["outcome"] == "done") == [0, 100]


def test_dangling_df_inventory_disagreeing_with_the_container_list_keeps_the_image(tmp_path, monkeypatch):
    sh = use_sh(monkeypatch, *docker_rows({A: (72, 100)}, df_used=[A]))         # `docker system df` says a container uses it
    seed_since(tmp_path, [A])
    write_ledger(tmp_path, {})
    res = ca.dangling_images(mk("dangling_images", apply=True))
    assert not sh.with_prefix("docker image rm") and res.items[0]["state"] == "kept: referenced by a container"
    for bad in ((1, "", "x"), ok("not json"), ok(json.dumps({"Images": [{"ID": A, "UniqueSize": "??", "Containers": "0"}]})),
                ok(json.dumps({"Images": []}))):                                # unknown / unparsable / image missing from the df: kept
        rows = docker_rows({A: (72, 100)})
        rows[2] = ("docker system df -v", bad)
        sh = use_sh(monkeypatch, *rows)
        res = ca.dangling_images(mk("dangling_images", apply=True))
        assert not sh.with_prefix("docker image rm"), bad


def test_dangling_rechecks_references_and_the_build_gate_right_before_each_rm(tmp_path, monkeypatch):
    """REVIEW (high): the gate was read once at the start, the references once; an image can be started (or a build begin)
    in the seconds before the delete."""
    seed_since(tmp_path, [A, B])
    write_ledger(tmp_path, {})
    state = {"refs": [], "busy": False}
    rows = docker_rows({A: (72, 100), B: (72, 100)})
    rows[3] = ("docker ps -a -q --no-trunc", lambda k: ok("c" * 12 if state["refs"] else ""))
    rows[4] = ("docker container inspect", lambda k: ok("\n".join(state["refs"])))
    sh = use_sh(monkeypatch, *rows)
    monkeypatch.setattr(cl, "_busy", lambda name: (state["busy"], "build"))
    # the world changes between selection and the first delete: a container starts from A (references are read again then)
    first_call = {"n": 0}
    orig = cl._referenced_images

    def refs_now():
        first_call["n"] += 1
        if first_call["n"] == 2:                              # 1 = selection, 2 = re-check before the first rm
            state["refs"] = [A]
        return orig()

    monkeypatch.setattr(cl, "_referenced_images", refs_now)
    res = ca.dangling_images(mk("dangling_images", apply=True))
    assert sh.with_prefix("docker image rm") == [f"docker image rm {B}"]            # A was started meanwhile: never removed
    assert res.metrics["gone"] == 1
    # and a docker build that starts mid-run stops the deletes too
    first_call["n"] = 0
    state["refs"] = []
    monkeypatch.setattr(cl, "_referenced_images", orig)
    sh2 = use_sh(monkeypatch, *docker_rows({A: (72, 100)}))
    busy_calls = []

    def busy(name):
        busy_calls.append(name)
        return (len(busy_calls) > 1, "docker build process")        # idle at the start, busy at the re-check

    monkeypatch.setattr(cl, "_busy", busy)
    ca.dangling_images(mk("dangling_images", apply=True))
    assert not sh2.with_prefix("docker image rm") and len(busy_calls) == 2


def test_dangling_reference_probe_failing_at_the_last_moment_keeps_the_image(tmp_path, monkeypatch):
    seed_since(tmp_path, [A])
    write_ledger(tmp_path, {})
    sh = use_sh(monkeypatch, *docker_rows({A: (72, 100)}))
    n = {"c": 0}
    orig = cl._referenced_images

    def refs():
        n["c"] += 1
        return orig() if n["c"] == 1 else None
    monkeypatch.setattr(cl, "_referenced_images", refs)
    res = ca.dangling_images(mk("dangling_images", apply=True))
    assert not sh.with_prefix("docker image rm") and res.metrics["failed"] == 1 and res.status == "warn"


def test_dangling_skipped_while_docker_builds(tmp_path, monkeypatch):
    sh = use_sh(monkeypatch, *docker_rows({A: (72, 100)}))
    write_ledger(tmp_path, {})
    monkeypatch.setattr(cl, "_busy", lambda name: (name == "docker_build", "docker build process"))
    res = ca.dangling_images(mk("dangling_images", apply=True))
    assert res.status == "skipped" and "docker build active" in res.summary and not sh.calls


def test_dangling_protected_digest_is_never_removed(tmp_path, monkeypatch):
    sh = use_sh(monkeypatch, *docker_rows({A: (72, 100)}, digests={A: ["ghcr.io/immich-app/immich-server@sha256:" + "1" * 64]}))
    seed_since(tmp_path, [A])
    write_ledger(tmp_path, {})
    res = ca.dangling_images(mk("dangling_images", apply=True))
    assert not sh.with_prefix("docker image rm") and res.metrics["protected"] == 1 and res.reclaimed_bytes == 0


def test_dangling_rm_failure_is_reported_not_swallowed(tmp_path, monkeypatch):
    rows = docker_rows({A: (72, 100)})
    rows[rows.index(("docker image rm", ok()))] = ("docker image rm", (1, "", "Error: conflict: unable to delete (cannot be forced) - image is being used by running container"))
    use_sh(monkeypatch, *rows)
    seed_since(tmp_path, [A])
    write_ledger(tmp_path, {})
    res = ca.dangling_images(mk("dangling_images", apply=True))
    assert res.status == "warn" and res.metrics["failed"] == 1 and res.reclaimed_bytes == 0


def test_dangling_bad_config_and_pause(tmp_path, monkeypatch):
    sh = use_sh(monkeypatch, *docker_rows({A: (72, 100)}))
    seed_since(tmp_path, [A])
    write_ledger(tmp_path, {})
    assert ca.dangling_images(mk("dangling_images", min_age_hours=0)).status == "skipped"
    (tmp_path / "conf" / "PAUSE").write_text("")
    res = ca.dangling_images(mk("dangling_images", apply=True))
    assert res.metrics["mode"] == "report" and not sh.with_prefix("docker image rm")


# =========================================================================== crash_dumps
def crash_ctx(tmp_path, apply=False, **opts):
    return mk("crash_dumps", apply=apply, crash_dir=str(tmp_path / "crash"), coredump_dir=str(tmp_path / "core"), **opts)


def test_crash_dumps_age_rules_and_exclusions(tmp_path, monkeypatch):
    c, k = tmp_path / "crash", tmp_path / "core"
    old = mkfile(c / "_usr_bin_dash.1000.crash", 3 * DAY)
    new = mkfile(c / "_usr_bin_ls.1000.crash", 1 * DAY)
    fresh = mkfile(c / "_usr_bin_cat.1000.crash", 300)
    marker = mkfile(c / "_usr_bin_dash.1000.uploaded", 30 * DAY)
    lock = mkfile(c / "_usr_bin_dash.1000.lock", 30 * DAY)
    (c / "_evil.crash").symlink_to(old)
    os.utime(c / "_evil.crash", (NOW - 30 * DAY, NOW - 30 * DAY), follow_symlinks=False)
    sub = c / "dir.crash"
    sub.mkdir()
    os.utime(sub, (NOW - 30 * DAY, NOW - 30 * DAY))
    core_old = mkfile(k / f"core.bash.1000.{'a' * 32}.77.{int((NOW - 9 * DAY) * 1e6):016d}.zst", 9 * DAY)
    core_rewritten = mkfile(k / f"core.bash.1000.{'a' * 32}.78.{int((NOW - 1 * DAY) * 1e6):016d}.zst", 9 * DAY)   # mtime old, name says 1 d
    core_new = mkfile(k / f"core.bash.1000.{'a' * 32}.79.{int((NOW - 3 * DAY) * 1e6):016d}.zst", 3 * DAY)
    partial = mkfile(k / "core.bash.1000.partial.zst.partial", 30 * DAY)
    other = mkfile(k / "README", 90 * DAY)
    dry = ca.crash_dumps(crash_ctx(tmp_path))
    assert dry.metrics["selected"] == 2 and dry.status == "info" and set(outcomes(tmp_path)) == {"dry-run"}
    assert old.exists() and core_old.exists()
    res = ca.crash_dumps(crash_ctx(tmp_path, apply=True))
    assert not old.exists() and not core_old.exists() and res.metrics["selected"] == 2
    for kept in (new, fresh, marker, lock, core_rewritten, core_new, partial, other, c / "_evil.crash", sub):
        assert os.path.lexists(kept), kept
    assert res.reclaimed_bytes == 20 and res.status == "ok"
    ascii_ok(res)
    assert ca.crash_dumps(crash_ctx(tmp_path, apply=True)).summary.startswith("no crash dump older than")


def test_crash_dumps_open_file_and_missing_dirs(tmp_path, monkeypatch):
    c = tmp_path / "crash"
    held = mkfile(c / "a.crash", 5 * DAY)
    gone = mkfile(c / "b.crash", 5 * DAY)
    fake_proc(tmp_path, holders={7: [held]})
    res = ca.crash_dumps(crash_ctx(tmp_path, apply=True))
    assert held.exists() and not gone.exists() and res.metrics["open_kept"] == 1 and "1 open kept" in res.summary
    assert "1 dirs absent" in res.summary                                          # no coredump dir on this host
    assert ca.crash_dumps(mk("crash_dumps", crash_dir="relative")).status == "skipped"


def test_crash_dumps_incomplete_proof_and_unlink_rights(tmp_path, monkeypatch):
    c = tmp_path / "crash"
    f = mkfile(c / "a.crash", 5 * DAY)
    monkeypatch.setattr(ca, "_proc_scan", lambda want: (set(), False))
    res = ca.crash_dumps(crash_ctx(tmp_path, apply=True))
    assert f.exists() and res.metrics["needs_root"] == 1
    monkeypatch.setattr(ca, "_proc_scan", REAL_PROC_SCAN)
    monkeypatch.setattr(ca, "_euid", lambda: 1000)
    monkeypatch.setattr(ca, "_can_unlink", lambda d, st: False)                     # another user's report in the sticky dir
    res = ca.crash_dumps(crash_ctx(tmp_path, apply=True))
    assert f.exists() and res.metrics["needs_root"] == 1 and res.reclaimed_bytes == 0
    dry = ca.crash_dumps(crash_ctx(tmp_path))
    assert any(i["state"] == "needs root to remove" for i in dry.items)


def test_can_unlink_rules(tmp_path, monkeypatch):
    d = tmp_path / "sticky"
    d.mkdir()
    f = mkfile(d / "x", 5 * DAY)
    monkeypatch.setattr(ca, "_euid", lambda: 0)
    assert ca._can_unlink(str(d), os.lstat(f))                                       # root can
    monkeypatch.setattr(ca, "_euid", lambda: ME)
    assert ca._can_unlink(str(d), os.lstat(f))                                       # owner of a plain directory
    os.chmod(d, 0o1777)                                                              # /var/crash: sticky, world-writable
    monkeypatch.setattr(ca, "_euid", lambda: ME + 1)
    assert not ca._can_unlink(str(d), SimpleNamespace(st_uid=ME + 2)) or ME + 1 == d.stat().st_uid
    assert ca._can_unlink(str(d), SimpleNamespace(st_uid=ME + 1))                    # sticky: our own file may go
    os.chmod(d, 0o755)


def test_crash_dumps_protected_report_is_kept(tmp_path, monkeypatch):
    c = tmp_path / "crash"
    keep = mkfile(c / "_usr_lib_plexmediaserver_x.1000.crash", 30 * DAY)
    res = ca.crash_dumps(crash_ctx(tmp_path, apply=True))
    assert keep.exists() and res.metrics["protected"] == 1


# =========================================================================== apt_cache
def apt_files(d: Path, n=3, size=5 * MIB):
    for i in range(n):
        mkfile(d / f"pkg{i}.deb", 3 * DAY, size)
    (d / "partial").mkdir(exist_ok=True)


def apt_ctx(tmp_path, apply=False, **opts):
    return mk("apt_cache", apply=apply, cache_dir=str(tmp_path / "apt"), **opts)


def test_apt_cache_cleans_when_idle_and_measures_freed(tmp_path, monkeypatch):
    d = tmp_path / "apt"
    apt_files(d)
    monkeypatch.setattr(cl, "_apt_lock_state", lambda: "free")

    def clean(k):
        for p in d.glob("*.deb"):
            p.unlink()
        return ok()

    sh = use_sh(monkeypatch, ("apt-get clean", clean))
    dry = ca.apt_cache(apt_ctx(tmp_path))
    assert dry.summary.startswith("report: would free 15.0 MiB") and not sh.calls and len(list(d.glob("*.deb"))) == 3
    res = ca.apt_cache(apt_ctx(tmp_path, apply=True))
    assert sh.with_prefix("apt-get clean") == ["apt-get clean"] and res.reclaimed_bytes == 15 * MIB
    assert ca.apt_cache(apt_ctx(tmp_path, apply=True)).summary.startswith("apt cache 0 B, under")
    assert sh.calls.count("apt-get clean") == 1                                      # idempotent


def test_apt_cache_threshold_busy_and_lock(tmp_path, monkeypatch):
    d = tmp_path / "apt"
    apt_files(d, n=1, size=2 * MIB)
    sh = use_sh(monkeypatch, ("apt-get clean", ok()))
    monkeypatch.setattr(cl, "_apt_lock_state", lambda: "free")
    assert ca.apt_cache(apt_ctx(tmp_path, apply=True)).summary.startswith("apt cache 2.0 MiB, under 10 MiB") and not sh.calls
    apt_files(d, n=4)
    monkeypatch.setattr(cl, "_busy", lambda name: (True, "dpkg running (pid 5)"))
    assert ca.apt_cache(apt_ctx(tmp_path, apply=True)).status == "skipped"
    monkeypatch.setattr(cl, "_busy", lambda name: (False, "idle"))
    monkeypatch.setattr(cl, "_apt_lock_state", lambda: "busy")
    assert "apt lock busy" in ca.apt_cache(apt_ctx(tmp_path, apply=True)).summary
    monkeypatch.setattr(cl, "_apt_lock_state", lambda: "unknown")                     # non-root: cannot prove the lock is free
    assert ca.apt_cache(apt_ctx(tmp_path, apply=True)).status == "skipped"
    rep = ca.apt_cache(apt_ctx(tmp_path))
    assert rep.status == "info" and "lock state unverifiable" in rep.summary
    assert not sh.calls and ca.apt_cache(apt_ctx(tmp_path, min_mib="x")).status == "skipped"


def test_apt_cache_command_failure_is_reported(tmp_path, monkeypatch):
    apt_files(tmp_path / "apt")
    monkeypatch.setattr(cl, "_apt_lock_state", lambda: "free")
    use_sh(monkeypatch, ("apt-get clean", (100, "", "E: Could not get lock")))
    res = ca.apt_cache(apt_ctx(tmp_path, apply=True))
    assert res.status == "warn" and res.metrics["failed"] == 1 and res.reclaimed_bytes == 0


# =========================================================================== cross-cutting
# ---- the shared inuse module (homelab_maint/inuse.py) as a second, path-based opinion
def stub_inuse(monkeypatch, *answers):
    """A stand-in for inuse: answers are consumed per call of process_cwd_or_open_under (the last one repeats);
    proc_snapshot(refresh=True) calls are counted. An Exception instance in the list is raised."""
    calls = {"probe": 0, "refresh": 0}
    seq = list(answers)

    def probe(path, kinds=None):
        assert tuple(kinds) == ("cwd", "exe", "fd", "map")                # open-state only: argv/env mentions are not 'use'
        a = seq[min(calls["probe"], len(seq) - 1)]
        calls["probe"] += 1
        if isinstance(a, Exception):
            raise a
        return a

    def snap(max_age_s=30, refresh=False):
        calls["refresh"] += bool(refresh)

    monkeypatch.setattr(ca, "_inuse", SimpleNamespace(process_cwd_or_open_under=probe, proc_snapshot=snap))
    return calls


UNUSED = inuse_mod.Proof(False, True, "no cwd/exe/fd/map under it in 300 processes")
USED = inuse_mod.Proof(True, True, "pid 999 (rsyslogd) fd")
UNKNOWN = inuse_mod.Proof(True, False, "unknown: 12 unreadable process entries (not root?)")


def test_inuse_second_opinion_used_keeps_the_log(tmp_path, monkeypatch):
    d = logs(tmp_path)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    stub_inuse(monkeypatch, USED)
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert rot.exists() and res.metrics["open_kept"] == 1 and any("pid 999 (rsyslogd) fd" in i["state"] for i in res.items)


def test_inuse_unknown_or_crashing_refuses_apply_and_marks_the_dry_run_partial(tmp_path, monkeypatch):
    d = logs(tmp_path)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    for ans in (UNKNOWN, RuntimeError("boom")):
        stub_inuse(monkeypatch, ans)
        res = ca.log_compress(log_ctx(tmp_path, apply=True))
        assert rot.exists() and res.metrics["needs_root"] == 1 and res.reclaimed_bytes == 0
        dry = ca.log_compress(log_ctx(tmp_path))
        assert dry.metrics["selected"] == 1 and dry.metrics["proof"].startswith("partial") and "proof incomplete" in dry.summary


def test_inuse_unused_lets_the_compression_run_and_reproves_with_fresh_snapshots(tmp_path, monkeypatch):
    d = logs(tmp_path)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    calls = stub_inuse(monkeypatch, UNUSED)
    use_sh(monkeypatch, ("gzip -t -- ", real_gzip_t))
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert not rot.exists() and (d / "syslog.1.gz").exists() and res.reclaimed_bytes > 0
    assert calls["refresh"] >= 2 and calls["probe"] >= 3                    # task, before compressing, after compressing


@pytest.mark.parametrize("answers", [(UNUSED, USED), (UNUSED, UNUSED, USED), (UNUSED, UNUSED, UNKNOWN)])
def test_inuse_late_answers_keep_the_original_and_remove_the_temp(tmp_path, monkeypatch, answers):
    d = logs(tmp_path)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    stub_inuse(monkeypatch, *answers)
    use_sh(monkeypatch, ("gzip -t -- ", real_gzip_t))
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert rot.exists() and not list(d.glob("*.gz*")) and res.reclaimed_bytes == 0


def test_inuse_applies_to_crash_dumps_too(tmp_path, monkeypatch):
    c = tmp_path / "crash"
    f = mkfile(c / "a.crash", 5 * DAY)
    stub_inuse(monkeypatch, USED)
    assert ca.crash_dumps(crash_ctx(tmp_path, apply=True)).metrics["open_kept"] == 1 and f.exists()
    stub_inuse(monkeypatch, UNUSED, UNKNOWN)                                # fine at selection, unknown at the last moment
    res = ca.crash_dumps(crash_ctx(tmp_path, apply=True))
    assert f.exists() and res.metrics["failed"] == 1
    stub_inuse(monkeypatch, UNUSED)
    ca.crash_dumps(crash_ctx(tmp_path, apply=True))
    assert not f.exists()


def fake_inuse_proc(tmp_path, holders: dict[int, list[Path]]) -> Path:
    """A /proc tree the REAL inuse module accepts: our own pid (it insists on seeing itself) plus the given holders."""
    root = tmp_path / "iproc"
    for pid, files in {os.getpid(): [], **holders}.items():
        d = root / str(pid)
        (d / "fd").mkdir(parents=True)
        (d / "maps").write_text("")
        (d / "cmdline").write_bytes(b"x\0")
        (d / "comm").write_text("fake\n")
        for i, f in enumerate(files):
            (d / "fd" / str(3 + i)).symlink_to(f)
    return root


def test_real_inuse_module_integration(tmp_path, monkeypatch):
    d = logs(tmp_path)
    held = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    free = mkfile(d / "kern.log.1", 3 * DAY, data=big(2))
    monkeypatch.setattr(inuse_mod, "PROC", fake_inuse_proc(tmp_path, {4242: [held]}))   # ca.PROC (our scan) sees nobody
    inuse_mod.reset_caches()
    monkeypatch.setattr(ca, "_inuse", inuse_mod)
    use_sh(monkeypatch, ("gzip -t -- ", real_gzip_t))
    try:
        res = ca.log_compress(log_ctx(tmp_path, apply=True))
    finally:
        inuse_mod.reset_caches()
    assert held.exists() and not free.exists() and (d / "kern.log.1.gz").exists()
    assert res.metrics["open_kept"] == 1 and any("pid 4242 (fake) fd" in i["state"] for i in res.items)


def test_real_inuse_module_not_root_means_unknown(tmp_path, monkeypatch):
    d = logs(tmp_path)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    root = fake_inuse_proc(tmp_path, {})
    (root / str(os.getpid()) / "maps").unlink()
    (root / "99" / "fd").mkdir(parents=True)
    (root / "99" / "maps").mkdir()                                        # open() raises IsADirectoryError, not PermissionError
    monkeypatch.setattr(inuse_mod, "PROC", tmp_path / "does-not-exist")  # /proc unreadable altogether
    inuse_mod.reset_caches()
    monkeypatch.setattr(ca, "_inuse", inuse_mod)
    try:
        res = ca.log_compress(log_ctx(tmp_path, apply=True))
    finally:
        inuse_mod.reset_caches()
    assert rot.exists() and res.metrics["needs_root"] == 1


def test_summaries_are_ascii_and_short_for_every_task(tmp_path, monkeypatch):
    build_cache(tmp_path)
    healthy_sh(monkeypatch)
    for res in (ca.app_cache_trim(cache_ctx(tmp_path)), ca.log_compress(log_ctx(tmp_path)),
                ca.crash_dumps(crash_ctx(tmp_path)), ca.apt_cache(apt_ctx(tmp_path))):
        ascii_ok(res)


# =========================================================================== REVIEW regressions: app_cache_trim
# Each test below is built around a scenario that WOULD have removed (or lost track of) something in use.
def mount_rows(mounts: dict[str, list[str]]):
    """FakeSh rows for `_container_mounts`: ALL containers with their bind-mount sources."""
    names = list(mounts)
    return [("docker ps -a -q --no-trunc", lambda k: ok("\n".join(f"{i:064x}" for i in range(len(names))))),
            ("docker inspect --format {{.Name}}", lambda k: ok("\n".join(f"/{n}|{';'.join(mounts[n])};" for n in names)))]


def cache_state(tmp_path, name="app_cache_trim") -> dict:
    return core.read_json(tmp_path / "state" / "tasks" / f"{name}.json", {})


def n_files(d: Path) -> int:
    return len([p for p in d.rglob("*") if p.is_file()])


# ---- E11: `..` / `.` / `//` / trailing `/` in a configurable path
@pytest.mark.parametrize("suffix", ["/../db", "/./", "/", "//x/..", "/../../.."])
def test_cache_trim_non_canonical_rule_path_is_refused_even_in_a_dry_run(tmp_path, monkeypatch, suffix):
    """The unprotect regex matches the `.../tunarr/subtitles` PREFIX of the raw string while the path really points at
    <data>/tunarr/db: the dry-run used to list files outside the rule root (and apply was saved only by a late ValueError)."""
    build_cache(tmp_path)
    db = mkfile(tmp_path / "data" / "tunarr" / "db" / "db.db", 400 * DAY)
    mkfile(tmp_path / "data" / "tunarr" / "db" / "other", 400 * DAY)
    healthy_sh(monkeypatch)
    rule = {"name": "subs", "path": str(subs(tmp_path)) + suffix, "max_age_days": 30, "root_owned_ok": True}
    for apply in (False, True):
        res = ca.app_cache_trim(cache_ctx(tmp_path, apply=apply, rules=[rule]))
        assert res.items[0]["state"].startswith("refused: path not normalised"), (suffix, res.items[0])
        assert res.metrics["files"] == 0 and res.reclaimed_bytes == 0
    assert db.exists() and not [r for r in audit_rows(tmp_path) if r["action"] == "cache-trim"]


def test_cache_trim_dotdot_cannot_climb_out_of_the_unprotect_exemption(tmp_path, monkeypatch):
    """With the shipped-style unprotect regex a `.../cache/subtitles/../..` string used to look like the exempt cache dir."""
    root = tmp_path / "data" / "tunarr"
    victim = mkfile(root / "db.db", 400 * DAY)
    sub = root / "cache" / "subtitles"
    sub.mkdir(parents=True)
    healthy_sh(monkeypatch)
    rule = {"name": "subs", "path": f"{sub}/../..", "max_age_days": 30, "root_owned_ok": True}
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[rule], unprotect=["data/tunarr/cache/subtitles"]))
    assert res.items[0]["state"].startswith("refused: path not normalised") and victim.exists()


def test_log_and_crash_roots_with_dotdot_are_refused(tmp_path, monkeypatch):
    d = logs(tmp_path)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    (d / "sub").mkdir()
    use_sh(monkeypatch, ("gzip -t -- ", real_gzip_t))
    res = ca.log_compress(mk("log_compress", apply=True, roots=[str(d / "sub" / "..")], min_mib=1))
    assert rot.exists() and res.metrics["roots"] == 1 and res.summary.startswith("no rotated log")
    c = mkfile(tmp_path / "crash" / "a.crash", 5 * DAY)
    (tmp_path / "crash" / "x").mkdir()
    res = ca.crash_dumps(mk("crash_dumps", apply=True, crash_dir=str(tmp_path / "crash" / "x" / ".."), coredump_dir=str(tmp_path / "core")))
    assert c.exists() and res.metrics["found"] == 0 and res.reclaimed_bytes == 0


# ---- E9: confinement
def kavita_tree(tmp_path):
    cfg = tmp_path / "data" / "kavita" / "config"
    return cfg, {"db": mkfile(cfg / "kavita.db", 400 * DAY), "settings": mkfile(cfg / "appsettings.json", 400 * DAY),
                 "cover": mkfile(cfg / "covers" / "a.png", 210 * DAY), "cachefile": mkfile(cfg / "cache" / "x", 40 * DAY)}


def test_cache_trim_rule_on_an_allowed_root_deletes_nothing(tmp_path, monkeypatch):
    """REVIEW: `path == allowed root` (the copy-pasted Kavita example) removed kavita.db and appsettings.json (mtime 400 d)."""
    cfg, f = kavita_tree(tmp_path)
    healthy_sh(monkeypatch)
    rule = {"name": "kav", "path": str(cfg), "max_age_days": 30, "container": "kavita"}
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[rule], allowed_roots=[str(cfg)], unprotect=[]))
    assert res.items[0]["state"].startswith("refused: rule path is an allowed root itself")
    assert all(p.exists() for p in f.values()) and res.reclaimed_bytes == 0


def test_cache_trim_allowed_roots_narrowed_to_the_cache_dir_block_the_config_dir(tmp_path, monkeypatch):
    cfg, f = kavita_tree(tmp_path)
    healthy_sh(monkeypatch)
    rule = {"name": "kav", "path": str(cfg), "max_age_days": 30, "container": "kavita"}
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[rule], allowed_roots=[str(cfg / "cache")], unprotect=[]))
    assert res.items[0]["state"].startswith("refused: outside allowed_roots") and all(p.exists() for p in f.values())
    ok_rule = {**rule, "path": str(cfg / "cache")}                         # the narrowed, intended rule still works
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[ok_rule], allowed_roots=[str(cfg)], unprotect=[]))
    assert not f["cachefile"].exists() and f["db"].exists() and f["settings"].exists() and res.metrics["files"] == 1


@pytest.mark.parametrize("name,age", [("cache.db", 400), ("cache.db-wal", 400), ("cache.db-shm", 5), ("x.bak", 400), ("db-2025.bak", 90),
                                      ("lib.sqlite", 400), ("lib.sqlite3", 400), ("state.vscdb", 400), ("a.ldb", 400), ("t.sql", 400)])
def test_cache_trim_refuses_a_tree_that_holds_database_files(tmp_path, monkeypatch, name, age):
    """A `*.db` (also young ones: it WILL age) or its -wal/-shm/.bak sidecar means this is not a disposable cache."""
    cfg, f = kavita_tree(tmp_path)
    dbf = mkfile(cfg / "cache" / "deep" / name, age * DAY)
    healthy_sh(monkeypatch)
    rule = {"name": "kav", "path": str(cfg / "cache"), "max_age_days": 30, "container": "kavita"}
    for apply in (False, True):
        res = ca.app_cache_trim(cache_ctx(tmp_path, apply=apply, rules=[rule], allowed_roots=[str(cfg)], unprotect=[]))
        assert res.items[0]["state"].startswith("refused: tree holds database files") and name in res.items[0]["state"]
    assert dbf.exists() and f["cachefile"].exists() and res.reclaimed_bytes == 0 and res.metrics["files"] == 0


def test_cache_trim_container_that_mounts_the_path_must_be_named(tmp_path, monkeypatch):
    """REVIEW: a rule without `container` ran with the app stopped / without its health guard."""
    s = build_cache(tmp_path)
    health = ("docker inspect --format {{.State.Status}}", ok("running|healthy"))
    mount_src = str(tmp_path / "data" / "tunarr")                                      # compose mounts the app dir, not just the cache
    use_sh(monkeypatch, *mount_rows({"tunarr-host-net": [mount_src]}), health)
    base = {"name": "subs", "path": str(s), "max_age_days": 30, "root_owned_ok": True}
    for rule, why in (({}, "refused: mounted by tunarr-host-net"), ({"container": "someone-else"}, "refused: mounted by tunarr-host-net")):
        res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[{**base, **rule}]))
        assert res.items[0]["state"].startswith(why) and n_files(s) == 6
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[{**base, "container": "tunarr-host-net"}]))
    assert res.metrics["files"] == 4 and n_files(s) == 2
    use_sh(monkeypatch, ("docker ps -a", (1, "", "daemon down")), health)              # docker cannot say: fail closed
    mkfile(s / "z" / "old", 90 * DAY)
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[{**base, "container": "tunarr-host-net"}]))
    assert "docker mounts unknown" in res.items[0]["state"] and (s / "z" / "old").exists()


def test_cache_trim_app_dir_under_volume1_docker_needs_a_container(tmp_path, monkeypatch):
    healthy_sh(monkeypatch)
    rule = {"name": "kav", "path": "/volume1/docker/kavita/config/cache", "max_age_days": 30}
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[rule], allowed_roots=["/volume1/docker/kavita/config"], unprotect=[]))
    assert res.items[0]["state"].startswith("refused: an app dir under /volume1/docker needs a container")


def test_never_touch_covers_app_state_dirs_but_not_their_cache():
    for bad in ("/volume1/docker/kavita/config", "/volume1/docker/kavita/config/covers", "/volume1/docker/kavita/config/cache-long",
                "/volume1/docker/kavita/config/cache.db", "/volume1/docker/radarr/config/MediaCover", "/volume1/docker/sonarr",
                "/home/ohmz/StudioProjects/tunarr/.docker-data", "/home/ohmz/StudioProjects/tunarr/.docker-data/tunarr",
                "/home/ohmz/StudioProjects/tunarr/.docker-data/tunarr/db", "/home/ohmz/StudioProjects/tunarr/.docker-data/tunarr/cache"):
        assert ca._never(bad), bad
    for fine in ("/volume1/docker/kavita/config/cache", "/volume1/docker/kavita/config/cache/x/y",
                 "/home/ohmz/StudioProjects/tunarr/.docker-data/tunarr/cache/subtitles", "/home/ohmz/StudioProjects/other/data"):
        assert not ca._never(fine), fine


# ---- empty directories
def old_empty(s: Path, name="emptyold", age=90 * DAY) -> Path:
    (s / name).mkdir(parents=True, exist_ok=True)
    os.utime(s / name, (NOW - age, NOW - age))
    return s / name


def dir_rule(tmp_path, **extra):
    return {"name": "subs", "path": str(subs(tmp_path)), "max_age_days": 30, "files_only": False, **extra}


def test_cache_trim_empty_dir_touched_after_the_scan_is_kept(tmp_path, monkeypatch):
    """REVIEW E3: an empty dir that qualified at scan time was still rmdir'd after the app had used it again."""
    s = subs(tmp_path)
    mkfile(s / "keep" / "young", 5 * DAY)
    gone, touched = old_empty(s, "gone"), old_empty(s, "touched")
    real = ca._walk_cache

    def scan_then_touch(*a, **kw):
        out = real(*a, **kw)
        os.utime(touched, (NOW, NOW))                                  # the app made and removed a temp file in it
        return out

    monkeypatch.setattr(ca, "_walk_cache", scan_then_touch)
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[dir_rule(tmp_path)]))
    assert touched.exists() and not gone.exists() and res.metrics["empty_dirs"] == 1


def test_cache_trim_empty_dir_held_by_a_process_is_kept(tmp_path, monkeypatch):
    """The held check inside the batch had no test (mutant `rmdir ignores held` survived): cwd of a process, seen at the
    planning snapshot AND only in the refreshed batch snapshot."""
    s = subs(tmp_path)
    mkfile(s / "keep" / "young", 5 * DAY)
    cwd, late = old_empty(s, "cwd"), old_empty(s, "late")
    fake_proc(tmp_path, cwd={4242: cwd})
    dry = ca.app_cache_trim(cache_ctx(tmp_path, rules=[dir_rule(tmp_path)]))
    assert dry.metrics["empty_dirs"] == 1 and "1 open skipped" in dry.items[0]["state"]          # only `late` is planned
    key = (os.stat(late).st_dev, os.stat(late).st_ino)
    n = []
    monkeypatch.setattr(ca, "HOLD_REFRESH_S", -1)

    def scan(want):
        n.append(1)
        held, complete = REAL_PROC_SCAN(want)
        return (held | {key} if len(n) > 1 else held), complete        # a process chdir'ed into it after the first snapshot

    monkeypatch.setattr(ca, "_proc_scan", scan)
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[dir_rule(tmp_path)]))
    assert late.exists() and cwd.exists() and res.metrics["empty_dirs"] == 0 and len(n) >= 2


def test_cache_trim_empty_dir_that_is_a_container_bind_mount_source_is_kept(tmp_path, monkeypatch):
    s = subs(tmp_path)
    mkfile(s / "keep" / "young", 5 * DAY)
    mounted, plain = old_empty(s, "mounted"), old_empty(s, "plain")
    use_sh(monkeypatch, *mount_rows({"app": [str(mounted)]}), ("docker inspect --format {{.State.Status}}", ok("running|healthy")))
    rule = dir_rule(tmp_path, container="app")
    dry = ca.app_cache_trim(cache_ctx(tmp_path, rules=[rule]))
    assert dry.metrics["empty_dirs"] == 1 and "1 mount-source dirs kept" in dry.items[0]["state"]
    ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[rule]))
    assert mounted.exists() and not plain.exists()


def test_cache_trim_container_started_after_validation_protects_its_mount_dir_at_delete_time(tmp_path, monkeypatch):
    s = subs(tmp_path)
    mkfile(s / "keep" / "young", 5 * DAY)
    mounted = old_empty(s, "mounted")
    n = []

    def ps(k):
        n.append(1)
        return ok("" if len(n) == 1 else "0" * 64)                       # validation sees no container; the batch sees one

    use_sh(monkeypatch, ("docker ps -a -q --no-trunc", ps), ("docker inspect --format {{.Name}}", ok(f"/new|{mounted};")))
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[dir_rule(tmp_path)]))
    assert mounted.exists() and len(n) == 2 and res.metrics["empty_dirs"] == 0


# ---- E5: per-file failures
def test_cache_trim_one_stuck_file_does_not_disable_the_rule(tmp_path, monkeypatch):
    """REVIEW: one EPERM raised out of the batch AFTER earlier files were unlinked; the partial deletion was not counted and
    the act key (the rule root) put the WHOLE rule into 3-day backoff, so later batches starved forever."""
    s = subs(tmp_path)
    for d in range(6):
        mkfile(s / f"d{d}" / "f", 60 * DAY, 100)
    stuck = mkfile(s / "d0" / "stuck", 60 * DAY, 100)
    real = os.unlink

    def unlink(path, *a, **kw):
        if kw.get("dir_fd") is not None and path == "stuck":
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real(path, *a, **kw)

    monkeypatch.setattr(os, "unlink", unlink)
    ctx = cache_ctx(tmp_path, apply=True, batch_files=2)
    res = ca.app_cache_trim(ctx)
    assert n_files(s) == 1 and stuck.exists()                               # d1..d5 (later batches) AND d0/f are gone
    assert res.status == "warn" and res.metrics["unlink_errors"] == 1 and res.metrics["failed"] == 0
    assert res.reclaimed_bytes == 600 and res.summary.startswith("freed 600 B (6 files") and "1 unlink errors (d0/stuck: Operation not permitted)" in res.summary
    assert not ctx.state.get("fail_until")                                  # no backoff for the rule
    ctx.save_state()
    mkfile(s / "d9" / "late", 60 * DAY, 100)                                # next run (3.5 d later): the queue still advances
    res2 = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, now=NOW + 3.5 * DAY, batch_files=2))
    assert not (s / "d9" / "late").exists() and res2.metrics["unlink_errors"] == 1


def test_cache_trim_a_failing_filesystem_stops_the_batch_after_a_few_errors(tmp_path, monkeypatch):
    s = subs(tmp_path)
    for i in range(40):
        mkfile(s / "d" / f"f{i}", 60 * DAY, 1)
    real = os.unlink

    def unlink(path, *a, **kw):
        if kw.get("dir_fd") is not None:
            raise OSError(errno.EROFS, "Read-only file system")
        return real(path, *a, **kw)

    monkeypatch.setattr(os, "unlink", unlink)
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True))
    assert res.metrics["unlink_errors"] == ca.MAX_UNLINK_ERRORS and res.status == "warn" and "STOPPED" in res.items[0]["state"]
    assert n_files(s) == 40 and res.reclaimed_bytes == 0


# ---- E4: the health safeguard
def flip_health(monkeypatch, s: Path, threshold: int):
    """Healthy until fewer than `threshold` cache files are left; records how many were left at each probe."""
    seen: list[int] = []

    def resp(k):
        n = n_files(s)
        seen.append(n)
        return ok("running|healthy" if n >= threshold else "running|unhealthy")

    use_sh(monkeypatch, ("docker inspect --format {{.State.Status}}", resp))
    return seen


@pytest.mark.parametrize("canary,expect", [(None, 200), (50, 50)])
def test_cache_trim_canary_batch_is_checked_before_the_bulk_is_deleted(tmp_path, monkeypatch, canary, expect):
    """REVIEW: the first mid-rule check came after 25 batches (the whole 131k-file Tunarr cache was already gone)."""
    s = subs(tmp_path)
    for i in range(450):
        mkfile(s / "a" / f"f{i}", 60 * DAY, 4)
    flip_health(monkeypatch, s, 450)                                        # unhealthy as soon as ONE file is gone
    rule = {"name": "subs", "path": str(s), "max_age_days": 30, "container": "c", "root_owned_ok": True}
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[rule], **({"canary_files": canary} if canary else {})))
    assert 450 - n_files(s) == expect and res.status == "warn" and res.summary.startswith("ABORTED: c health unhealthy")


def test_cache_trim_default_check_interval_is_small(tmp_path, monkeypatch):
    s = subs(tmp_path)
    for i in range(60):
        mkfile(s / f"d{i % 6}" / f"f{i}", 60 * DAY, 4)
    flip_health(monkeypatch, s, 52)                                         # the app trips after 8 files are gone
    rule = {"name": "subs", "path": str(s), "max_age_days": 30, "container": "c", "root_owned_ok": True}
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[rule], batch_files=1))
    gone = 60 - n_files(s)
    assert 8 <= gone <= 10 and res.status == "warn"                          # canary + every 5th batch, not every 100th


def test_cache_trim_health_is_also_checked_on_wall_clock_time(tmp_path, monkeypatch, sandbox):
    s = subs(tmp_path)
    for i in range(60):
        mkfile(s / f"d{i % 6}" / f"f{i}", 60 * DAY, 4)
    flip_health(monkeypatch, s, 58)                                         # unhealthy once 3 files are gone
    real = ca._trim_batch

    def slow(*a, **kw):
        sandbox[0] += 11                                                    # each batch "takes" 11 s
        return real(*a, **kw)

    monkeypatch.setattr(ca, "_trim_batch", slow)
    rule = {"name": "subs", "path": str(s), "max_age_days": 30, "container": "c", "root_owned_ok": True}
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, rules=[rule], batch_files=1, check_every=1000))
    assert 3 <= 60 - n_files(s) <= 4 and res.status == "warn"


# ---- result honesty
def test_cache_trim_result_counts_what_was_removed_not_what_was_selected(tmp_path, monkeypatch):
    s = subs(tmp_path)
    for i in range(5):
        mkfile(s / "d" / f"f{i}", 60 * DAY, 10)
    real = ca._walk_cache

    def scan_then_touch(*a, **kw):
        out = real(*a, **kw)
        for p in (s / "d").iterdir():
            os.utime(p, (NOW, NOW))                                         # every file rewritten after the scan
        return out

    monkeypatch.setattr(ca, "_walk_cache", scan_then_touch)
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True))
    assert res.summary.startswith("freed 0 B (0 files, 5 skipped)") and res.metrics["files"] == 0 and res.metrics["skipped"] == 5
    assert n_files(s) == 5 and res.reclaimed_bytes == 0


def test_cache_trim_pause_in_the_middle_is_reported_as_paused_not_as_a_report(tmp_path, monkeypatch):
    """REVIEW: PAUSE after batch 1 flipped ctx.apply, the summary became 'report: would free ...' after a REAL deletion."""
    s = build_cache(tmp_path)
    real = ca._trim_batch
    n = []

    def pause_after_first(*a, **kw):
        out = real(*a, **kw)
        n.append(1)
        (tmp_path / "conf" / "PAUSE").write_text("")
        return out

    monkeypatch.setattr(ca, "_trim_batch", pause_after_first)
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, batch_files=2))
    assert res.summary.startswith("PAUSED mid-run: freed 200 B (2 files") and res.status == "warn"
    assert res.metrics["mode"] == "apply" and res.metrics["paused"] is True and res.reclaimed_bytes == 200
    assert n_files(s) == 4 and len(n) == 1 and "PAUSED mid-run" in res.items[0]["state"]
    assert outcomes(tmp_path).count("done") == 1 and "dry-run" not in outcomes(tmp_path)    # never carried on as a dry-run


def test_cache_trim_report_without_root_says_the_proof_is_partial(tmp_path, monkeypatch):
    build_cache(tmp_path)
    monkeypatch.setattr(ca, "_proc_scan", lambda want: (set(), False))
    dry = ca.app_cache_trim(cache_ctx(tmp_path))
    assert dry.summary.endswith("; open-file proof incomplete (not root)") and dry.metrics["proof"] == "partial (not root)"
    ascii_ok(dry)


def test_cache_trim_manifest_lists_every_file_really_removed(tmp_path, monkeypatch):
    """REVIEW: the audit row was (cache-trim, rule root, bytes): after a bad day nobody could say which files went."""
    s = build_cache(tmp_path)
    real = ca._walk_cache

    def scan_then_touch(*a, **kw):
        out = real(*a, **kw)
        os.utime(s / "12" / "56" / "old3", (NOW, NOW))                      # skipped at delete time: must NOT be in the manifest
        return out

    monkeypatch.setattr(ca, "_walk_cache", scan_then_touch)
    dry = ca.app_cache_trim(cache_ctx(tmp_path))
    assert not (tmp_path / "state" / "manifests").exists() and dry.metrics["manifest"] == ""
    old_m = tmp_path / "state" / "manifests" / f"app_cache_trim.{int(NOW - 40 * DAY)}.jsonl"
    keep_m = tmp_path / "state" / "manifests" / f"app_cache_trim.{int(NOW - 10 * DAY)}.jsonl"
    old_m.parent.mkdir(parents=True)
    old_m.write_text("{}\n")
    keep_m.write_text("{}\n")
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True))
    mp = Path(res.metrics["manifest"])
    rows = [json.loads(ln) for ln in mp.read_text().splitlines()]
    assert mp.name == f"app_cache_trim.{int(NOW)}.jsonl" and oct(mp.stat().st_mode & 0o777) == "0o600"
    assert sorted((r["dir"], r["name"]) for r in rows) == [("12/56", "old_but_read"), ("ab/cd", "old1"), ("ab/cd", "old2")]
    assert all(r["rule"] == "subs" and r["size"] == 100 and r["mtime"] > 0 for r in rows) and res.metrics["files"] == 3
    assert not old_m.exists() and keep_m.exists()                           # old manifests age out by the timestamp in the name


def test_cache_trim_without_a_writable_manifest_nothing_is_deleted(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the state dir should be")
    monkeypatch.setattr(core, "STATE_DIR", blocker / "state")
    res = ca.app_cache_trim(cache_ctx(tmp_path, apply=True))
    assert res.items[0]["state"] == "refused: cannot write the deletion manifest" and n_files(s) == 6 and res.reclaimed_bytes == 0


# ---- E4/E8: silent failure
def run_persisted(tmp_path, **kw):
    ctx = cache_ctx(tmp_path, **kw)
    res = ca.app_cache_trim(ctx)
    ctx.save_state()
    return res


def test_cache_trim_rule_that_keeps_failing_makes_the_task_warn(tmp_path, monkeypatch):
    """REVIEW: the Tunarr path renamed (or its container down for a week) = status info forever while the cache regrows."""
    healthy_sh(monkeypatch)
    res1 = run_persisted(tmp_path)                                           # <tmp>/data/tunarr/subtitles does not exist yet
    assert res1.items[0]["state"] == "nothing: path missing" and res1.status == "ok"
    res2 = run_persisted(tmp_path)
    assert res2.status == "warn" and "WARN subs failing 2x" in res2.summary and res2.metrics["sick_rules"] == 1
    build_cache(tmp_path)
    res3 = run_persisted(tmp_path)                                           # healthy again: the counter resets
    assert res3.status != "warn" and cache_state(tmp_path)["rules"]["subs"]["bad_runs"] == 0


def test_cache_trim_unhealthy_container_twice_in_a_row_warns_but_a_busy_gate_does_not(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    rule = {"name": "subs", "path": str(s), "max_age_days": 30, "container": "c", "root_owned_ok": True}
    use_sh(monkeypatch, ("docker inspect --format {{.State.Status}}", ok("exited|none")))
    assert run_persisted(tmp_path, rules=[rule]).status != "warn"
    res = run_persisted(tmp_path, rules=[rule])
    assert res.status == "warn" and "subs failing 2x" in res.summary
    gated = {**rule, "gate": "plex"}
    monkeypatch.setattr(cl, "_busy", lambda name: (True, "plex busy"))
    for _ in range(4):
        r = run_persisted(tmp_path, rules=[{"name": "g", "path": str(s), "max_age_days": 30, "root_owned_ok": True, "gate": "plex"}])
    assert r.status != "warn" and r.items[0]["state"] == "skipped: plex busy"


def test_cache_trim_rule_that_selects_files_but_removes_none_three_times_warns(tmp_path, monkeypatch):
    s = build_cache(tmp_path)
    held = [p for p in s.rglob("*") if p.is_file() and p.name.startswith("old")]
    fake_proc(tmp_path, holders={4242: held})                                # a reader keeps every candidate open
    for i in range(3):
        res = run_persisted(tmp_path, apply=True)
        assert (res.status == "warn") == (i == 2), (i, res.summary)
    assert "subs stalled 3x" in res.summary and n_files(s) == 6


# =========================================================================== REVIEW regressions: log_compress
def rotate(d: Path, base="syslog", live_data=b"NEW LIVE LOG: a week of lines\n"):
    """What logrotate / RotatingFileHandler does: .1 -> .2, live -> .1 (the NEW .1 is the old live log), fresh live log."""
    os.rename(d / f"{base}.1", d / f"{base}.2")
    (d / f"{base}.1").write_bytes(live_data)


def test_log_compress_rotation_during_the_slow_reproof_never_costs_the_new_live_file(tmp_path, monkeypatch):
    """REVIEW: ~0.5 s of /proc scans sat between the last identity check and `unlink(name)`; a rotator running in it
    made the task unlink the NEW .1 (the up-to-a-week-old live log) under the old name."""
    d = logs(tmp_path)
    old = big(2)
    mkfile(d / "syslog.1", 3 * DAY, data=old)
    use_sh(monkeypatch, ("gzip -t -- ", real_gzip_t))
    calls = []

    def second_opinion(path, refresh=False):
        calls.append(refresh)
        if len(calls) == 3:                                         # task scan, before compress, the slow re-proof AFTER it
            rotate(d)
        return "", ""

    monkeypatch.setattr(ca, "_second_opinion", second_opinion)
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert (d / "syslog.1").read_bytes().startswith(b"NEW LIVE LOG") and (d / "syslog.2").read_bytes() == old
    assert not list(d.glob("*.gz*")) and not list(d.glob("*hm-*")) and res.metrics["gone"] == 1 and res.reclaimed_bytes == 0


def test_log_compress_rotation_between_the_identity_check_and_the_unlink_is_undone(tmp_path, monkeypatch):
    """The residual window: the original is taken out by an atomic rename, its inode verified, and put back on a mismatch."""
    d = logs(tmp_path)
    old = big(2)
    mkfile(d / "syslog.1", 3 * DAY, data=old)
    use_sh(monkeypatch, ("gzip -t -- ", real_gzip_t))
    real, fired = os.rename, []

    def rename(src, dst, **kw):
        if str(dst).endswith(ca._DEL_SUFFIX) and not fired:
            fired.append(1)
            rotate(d)                                               # the rotator wins the race, right before our take-out
        return real(src, dst, **kw)

    monkeypatch.setattr(os, "rename", rename)
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert fired and (d / "syslog.1").read_bytes().startswith(b"NEW LIVE LOG") and (d / "syslog.2").read_bytes() == old
    assert not list(d.glob("*.gz*")) and not list(d.glob("*hm-*")) and res.metrics["gone"] == 1


def test_log_compress_take_out_that_cannot_be_put_back_keeps_the_data_and_is_loud(tmp_path, monkeypatch):
    d = logs(tmp_path)
    mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    use_sh(monkeypatch, ("gzip -t -- ", real_gzip_t))
    real_rename, real_link, fired = os.rename, os.link, []

    def rename(src, dst, **kw):
        if str(dst).endswith(ca._DEL_SUFFIX) and not fired:
            fired.append(1)
            rotate(d)
        return real_rename(src, dst, **kw)

    def link(src, dst, **kw):
        if str(src).endswith(ca._DEL_SUFFIX):
            raise FileExistsError(errno.EEXIST, "a newer file took the name")
        return real_link(src, dst, **kw)

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "link", link)
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert res.metrics["failed"] == 1 and res.status == "warn" and "kept as syslog.1.hm-del" in res.summary
    assert (d / "syslog.1.hm-del").read_bytes().startswith(b"NEW LIVE LOG")             # nothing was unlinked


def test_log_compress_stale_take_out_name_is_never_overwritten(tmp_path, monkeypatch):
    d = logs(tmp_path)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    stale = mkfile(d / "syslog.1.hm-del", 5 * DAY, data=b"original of a crashed earlier run")
    use_sh(monkeypatch, ("gzip -t -- ", real_gzip_t))
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert rot.exists() and stale.read_bytes() == b"original of a crashed earlier run" and "stale syslog.1.hm-del" in res.summary
    assert not (d / "syslog.1.gz").exists() and not list(d.glob("*hm-tmp"))


@pytest.mark.parametrize("probe,ok_expected", [
    ("ActiveState=active\nExecMainExitTimestampMonotonic=0\n", False),                       # logrotate.service is running
    ("ActiveState=activating\nExecMainExitTimestampMonotonic=0\n", False),
    ("ActiveState=inactive\nExecMainExitTimestampMonotonic=900000000\n", False),             # finished 100 s ago (clock = 1000 s)
    ("ActiveState=failed\nExecMainExitTimestampMonotonic=900000000\n", False),
    ("ActiveState=inactive\nExecMainExitTimestampMonotonic=300000000\n", True),              # 700 s ago: quiet long enough
    ("ActiveState=inactive\nExecMainExitTimestampMonotonic=0\n", True),                      # never ran since boot
    ("ActiveState=inactive\nExecMainExitTimestampMonotonic=soon\n", False),                  # unreadable: fail closed
    ("garbage", False), ("", False)])
def test_log_compress_waits_for_logrotate(tmp_path, monkeypatch, probe, ok_expected):
    """REVIEW: a size-based or timer-driven rotator renaming files while we hold a stale identity is how a live log is lost."""
    d = logs(tmp_path)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    monkeypatch.setattr(ca, "_boot_mono", lambda: 1000.0)
    sh = use_sh(monkeypatch, ("systemctl show logrotate.service", ok(probe)), ("gzip -t -- ", real_gzip_t))
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert (not rot.exists()) == ok_expected, res.summary
    if not ok_expected:
        assert res.status == "skipped" and "deferred" in res.summary and not sh.with_prefix("gzip") and rot.exists()


def test_log_compress_probe_failure_is_fail_closed_and_dry_run_needs_no_probe(tmp_path, monkeypatch):
    d = logs(tmp_path)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    sh = use_sh(monkeypatch, ("systemctl show logrotate.service", (127, "", "systemctl: not found")))
    dry = ca.log_compress(log_ctx(tmp_path))
    assert dry.metrics["selected"] == 1 and not sh.calls                                     # report mode runs nothing at all
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert res.status == "skipped" and "cannot tell whether logrotate is running" in res.summary and rot.exists()


def test_log_compress_logrotate_starting_after_selection_keeps_the_original(tmp_path, monkeypatch):
    d = logs(tmp_path)
    rot = mkfile(d / "syslog.1", 3 * DAY, data=big(2))
    probes = []

    def probe(k):
        probes.append(1)                                                                      # 1 = task start, 2 = before the take-out
        return ok(f"ActiveState={'inactive' if len(probes) == 1 else 'active'}\nExecMainExitTimestampMonotonic=0\n")

    use_sh(monkeypatch, ("systemctl show logrotate.service", probe), ("gzip -t -- ", real_gzip_t))
    res = ca.log_compress(log_ctx(tmp_path, apply=True))
    assert rot.exists() and not list(d.glob("*.gz*")) and not list(d.glob("*hm-*")) and res.metrics["gone"] == 1 and len(probes) == 2


def test_cache_trim_failed_batch_backs_off_alone_and_is_visible(tmp_path, monkeypatch):
    """REVIEW: the act key was the rule root, so ONE failing batch put the whole rule into 3-day backoff (summary silent)."""
    s = build_cache(tmp_path)
    real = ca._trim_batch

    def fail_first(root, root_id, kind, groups, holders, acc, keep=frozenset()):
        if groups[0][0] == "12/56":
            raise RuntimeError("boom")                                       # a structural failure of ONE batch
        return real(root, root_id, kind, groups, holders, acc, keep)

    monkeypatch.setattr(ca, "_trim_batch", fail_first)
    ctx = cache_ctx(tmp_path, apply=True, batch_files=2)
    res = ca.app_cache_trim(ctx)
    assert res.metrics["failed"] == 1 and res.status == "warn" and "1 failed" in res.summary
    assert not (s / "ab" / "cd" / "old1").exists() and (s / "12" / "56" / "old3").exists()   # the other batch still ran
    ctx.save_state()
    res2 = ca.app_cache_trim(cache_ctx(tmp_path, apply=True, batch_files=2, now=NOW + DAY))
    assert res2.metrics["backoff"] == 1 and "1 in backoff" in res2.summary and res2.status == "warn"
