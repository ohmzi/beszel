"""Tests for tasks/cleaners.py: mocked command output, a fake /proc, tmp dirs only. Nothing here touches the host:
`sh` is replaced (an unmocked command returns rc 127), core.sh is stubbed so `logger` never reaches the journal,
and every path lives under tmp_path."""
import json
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint is imported)
import pytest

from homelab_maint import core
from homelab_maint.core import GIB
from homelab_maint.tasks import cleaners as cl

REAL_BUSY = cl._busy                                    # the autouse fixture replaces cl._busy per test
REAL_ARCHIVE_PROBLEM = cl._archive_target_problem        # ... and the cold-disk check (tmp dirs live on /)
NOW = 1_800_000_000.0
DAY = 86400
PROTECTED = {"patterns": ["immich", "plexmediaserver", "postgres", "tunarr", "kometa", "/mnt/backup", "buildkitd"]}

MUTATING = ("docker image rm", "docker buildx prune", "docker builder prune", "docker update", "docker rm",
            "snap remove", "snap set", "apt-get", "rsync -aHSAX --", "rm ", "kill")


# --------------------------------------------------------------------------- harness
class FakeSh:
    """`sh` stand-in. rows: (prefix, response); response = (rc, stdout, stderr) or a callable(cmd_str) -> that."""

    def __init__(self, *rows):
        self.rows = list(rows)
        self.calls: list[str] = []

    def __call__(self, cmd, timeout=60, **kw):
        key = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.calls.append(key)
        for prefix, resp in self.rows:
            if key.startswith(prefix):
                rc, out, err = resp(key) if callable(resp) else resp
                return subprocess.CompletedProcess(cmd, rc, out, err)
        return subprocess.CompletedProcess(cmd, 127, "", "unmocked: " + key)

    def mutating(self) -> list[str]:
        return [c for c in self.calls if c.startswith(MUTATING)]


def ok(out=""):
    return (0, out, "")


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(core, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(core, "CONF_DIR", tmp_path / "conf")
    (tmp_path / "conf").mkdir()
    # core.audit shells out to `logger`; stub it so tests never write to the host journal
    monkeypatch.setattr(core, "sh", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(cl, "_busy", lambda name: (False, "idle"))
    monkeypatch.setattr(cl, "_euid", lambda: 0)         # c2 apply needs root; tests exercise the non-root path explicitly
    monkeypatch.setattr(cl, "_archive_target_problem", lambda arch, src: "")
    monkeypatch.setattr(cl, "sh", FakeSh())            # default: every command is "not found"
    clock = [1000.0]                                   # fake monotonic clock that only moves when the code sleeps
    monkeypatch.setattr(cl, "_mono", lambda: clock[0])
    monkeypatch.setattr(cl, "_sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    yield clock


def use_sh(monkeypatch, *rows) -> FakeSh:
    f = FakeSh(*rows)
    monkeypatch.setattr(cl, "sh", f)
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


def mkfile(path, age_s=10 * DAY, size=10, now=NOW):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (now - age_s, now - age_s))
    return path


# =========================================================================== registry / helpers
def test_tasks_registered_with_spec_classes():
    from homelab_maint.core import REGISTRY
    for n in ("docker_cache", "docker_images", "apt_clean", "snap_revisions", "retention", "trash",
              "gradle_reaper", "caps"):
        assert REGISTRY[n].klass == "C1" and REGISTRY[n].tier == "daily", n
    assert REGISTRY["c2_candidates"].klass == "C2" and REGISTRY["c2_candidates"].tier == "weekly"


@pytest.mark.parametrize("text,want", [("8.192kB", 8192), ("22.25GB", 22_250_000_000), ("0B", 0), ("1.5GiB", 1610612736),
                                       ("12", 12), ("711.8MB", 711_800_000), ("N/A", None), ("", None), ("-3GB", None)])
def test_parse_size(text, want):
    assert cl._parse_size(text) == want


@pytest.mark.parametrize("v", [True, False, "3", None, math.nan, math.inf, [1]])
def test_num_rejects_non_numbers(v):
    assert cl._num(v) is None


# =========================================================================== retention
def retention_ctx(tmp_path, rules, apply=False, roots=None, **opts):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return mk("retention", apply=apply, rules=rules,
              allowed_roots=[str(data)] if roots is None else roots, **opts)


def rule(tmp_path, **kw):
    r = {"name": "r", "path": str(tmp_path / "data" / "logs"), "glob": "*.log", "files_only": True}
    r.update(kw)
    return r


def present(d):
    return sorted(p.name for p in d.iterdir())


def test_dry_run_mutates_nothing_and_apply_does_exactly_what_dry_run_listed(tmp_path, monkeypatch):
    sh = use_sh(monkeypatch)
    logs = tmp_path / "data" / "logs"
    for i in range(1, 6):
        mkfile(logs / f"k{i}.log", age_s=i * 2 * DAY)          # 2,4,6,8,10 days old
    mkfile(logs / "fresh.log", age_s=1 * DAY)
    r = rule(tmp_path, max_age_days=5)

    dry = cl.retention(retention_ctx(tmp_path, [r]))
    assert present(logs) == sorted(["k1.log", "k2.log", "k3.log", "k4.log", "k5.log", "fresh.log"])
    assert dry.metrics["mode"] == "report" and dry.metrics["selected"] == 3 and dry.status == "info"
    assert set(outcomes(tmp_path)) == {"dry-run"}
    listed = {a["target"] for a in audit_rows(tmp_path)}
    assert listed == {str(logs / n) for n in ("k3.log", "k4.log", "k5.log")}
    assert dry.items[0]["matched"] == 3 and dry.items[0]["size"] == "30 B"
    assert sh.calls == []                                       # no command of any kind
    ascii_ok(dry)

    res = cl.retention(retention_ctx(tmp_path, [r], apply=True))
    assert present(logs) == ["fresh.log", "k1.log", "k2.log"]
    assert {a["target"] for a in audit_rows(tmp_path) if a["outcome"] == "done"} == listed   # same set
    assert res.status == "ok" and res.reclaimed_bytes == 30
    ascii_ok(res)


def test_report_mode_or_kill_switch_never_deletes_even_when_apply_requested(tmp_path):
    logs = tmp_path / "data" / "logs"
    mkfile(logs / "old.log")
    cfg = {"global": {}, "caps": {}, "protected": PROTECTED,
           "tasks": {"retention": {"mode": "report", "rules": [rule(tmp_path, max_age_days=1)],
                                   "allowed_roots": [str(tmp_path / "data")]}}}
    cl.retention(core.Ctx(cfg, "retention", True, NOW))          # --apply but mode=report
    assert present(logs) == ["old.log"]
    cfg["tasks"]["retention"]["mode"] = "apply"
    (tmp_path / "conf" / "PAUSE").write_text("x")                # kill switch
    cl.retention(core.Ctx(cfg, "retention", True, NOW))
    assert present(logs) == ["old.log"]
    (tmp_path / "conf" / "PAUSE").unlink()
    (tmp_path / "conf" / "PAUSE.retention").write_text("x")      # per-task kill switch
    cl.retention(core.Ctx(cfg, "retention", True, NOW))
    assert present(logs) == ["old.log"]


BAD_SELECTORS = [
    {},                                                     # no selector at all
    {"max_age_days": 0}, {"max_age_days": -3}, {"max_age_days": "7"}, {"max_age_days": True},
    {"max_age_days": math.nan}, {"max_age_days": None},
    {"keep_newest": 0}, {"keep_newest": -1}, {"keep_newest": "3"}, {"keep_newest": 2.5}, {"keep_newest": True},
    {"keep_newest": None},
    {"max_age_days": 1, "keep_newest": 0},                  # one valid + one invalid selector => nothing
    {"max_age_days": "x", "keep_newest": 1},
]


@pytest.mark.parametrize("sel", BAD_SELECTORS, ids=[json.dumps(s, default=str) for s in BAD_SELECTORS])
def test_missing_or_invalid_selector_selects_nothing(tmp_path, sel):
    logs = tmp_path / "data" / "logs"
    for i in range(4):
        mkfile(logs / f"f{i}.log", age_s=(20 + i) * DAY)
    r = rule(tmp_path)
    r.update(sel)
    res = cl.retention(retention_ctx(tmp_path, [r], apply=True))
    assert len(present(logs)) == 4 and res.metrics["selected"] == 0
    assert outcomes(tmp_path) == []                          # act was never even asked


@pytest.mark.parametrize("glob", [None, "", "   ", "*.nomatch", "/abs/*", "../*", "a/../b", "x//y"])
def test_empty_unmatched_or_unsafe_glob_selects_nothing(tmp_path, glob):
    logs = tmp_path / "data" / "logs"
    mkfile(logs / "a.log", age_s=30 * DAY)
    r = rule(tmp_path, max_age_days=1)
    r["glob"] = glob
    if glob is None:
        del r["glob"]
    res = cl.retention(retention_ctx(tmp_path, [r], apply=True))
    assert present(logs) == ["a.log"] and res.metrics["selected"] == 0


def test_no_rules_or_bad_rule_shapes_select_nothing(tmp_path):
    logs = tmp_path / "data" / "logs"
    mkfile(logs / "a.log", age_s=30 * DAY)
    for rules in ([], None, "oops", [None, 5, "x", {}]):
        cl.retention(retention_ctx(tmp_path, rules, apply=True))
    assert present(logs) == ["a.log"]


def test_unmatched_selector_with_valid_rule_leaves_everything(tmp_path):
    logs = tmp_path / "data" / "logs"
    mkfile(logs / "a.log", age_s=1 * DAY)
    res = cl.retention(retention_ctx(tmp_path, [rule(tmp_path, max_age_days=30)], apply=True))
    assert present(logs) == ["a.log"] and res.metrics["selected"] == 0 and res.status == "ok"


def test_path_outside_allowed_roots_is_refused(tmp_path):
    other = tmp_path / "elsewhere"
    mkfile(other / "a.log", age_s=30 * DAY)
    r = rule(tmp_path, path=str(other), max_age_days=1)
    res = cl.retention(retention_ctx(tmp_path, [r], apply=True))
    assert present(other) == ["a.log"]
    assert res.items[0]["state"] == "refused: outside allowed_roots"
    assert "refused: outside allowed_roots" in outcomes(tmp_path)


def test_sibling_prefix_dotdot_and_symlinked_rule_path_cannot_escape_allowed_roots(tmp_path):
    data = tmp_path / "data"
    evil = tmp_path / "data-evil"                          # shares the string prefix of the allowed root
    mkfile(evil / "a.log", age_s=30 * DAY)
    outside = tmp_path / "outside"
    mkfile(outside / "b.log", age_s=30 * DAY)
    data.mkdir()
    (data / "link").symlink_to(outside)                     # rule path is a symlink out of the root
    rules = [rule(tmp_path, name="prefix", path=str(evil), max_age_days=1),
             rule(tmp_path, name="dotdot", path=str(data / ".." / "outside"), max_age_days=1),
             rule(tmp_path, name="link", path=str(data / "link"), max_age_days=1)]
    res = cl.retention(retention_ctx(tmp_path, rules, apply=True))
    assert present(evil) == ["a.log"] and present(outside) == ["b.log"]
    assert all(i["state"] == "refused: outside allowed_roots" for i in res.items[:3])


def test_no_allowed_roots_refuses_everything(tmp_path):
    logs = tmp_path / "data" / "logs"
    mkfile(logs / "a.log", age_s=30 * DAY)
    for roots in ([], ["/"], "notalist", [5]):
        res = cl.retention(retention_ctx(tmp_path, [rule(tmp_path, max_age_days=1)], apply=True, roots=roots))
        assert present(logs) == ["a.log"], roots
        assert res.items[0]["state"].startswith("refused")


def test_protected_rule_root_and_protected_file_names_are_refused(tmp_path):
    imm = tmp_path / "data" / "immich-logs"                  # root path matches a protected pattern
    mkfile(imm / "a.log", age_s=30 * DAY)
    logs = tmp_path / "data" / "logs"
    mkfile(logs / "postgres.log", age_s=30 * DAY)           # file name matches a protected pattern
    mkfile(logs / "plain.log", age_s=30 * DAY)
    rules = [rule(tmp_path, name="imm", path=str(imm), max_age_days=1), rule(tmp_path, max_age_days=1)]
    res = cl.retention(retention_ctx(tmp_path, rules, apply=True))
    assert present(imm) == ["a.log"]
    assert present(logs) == ["postgres.log"]
    assert res.items[0]["state"] == "refused: protected path"
    assert res.metrics["protected"] == 1
    assert "refused: protected path" in outcomes(tmp_path)


def test_protected_defence_in_depth_act_still_refuses(tmp_path, monkeypatch):
    """Even if the task's own pre-check were skipped, ctx.act refuses a protected target."""
    logs = tmp_path / "data" / "logs"
    victim = mkfile(logs / "postgres.log", age_s=30 * DAY)
    ctx = retention_ctx(tmp_path, [rule(tmp_path, max_age_days=1)], apply=True)
    assert ctx.act("retention-delete", str(victim), 10, lambda: victim.unlink()) is False
    assert victim.exists() and "refused-protected" in outcomes(tmp_path)


def test_a_recursive_glob_never_deletes_inside_a_never_touch_tree(tmp_path):
    """registry.analyze cannot prove a `**` glob against the unanchored never-touch regexes, so retention tests every match."""
    logs = tmp_path / "data" / "logs"
    keep = mkfile(logs / "ai-stack" / "deep" / "old.log", age_s=30 * DAY)
    gone = mkfile(logs / "app" / "old.log", age_s=30 * DAY)
    r = rule(tmp_path, glob="**/*.log", max_age_days=1)
    dry = cl.retention(retention_ctx(tmp_path, [r]))
    assert dry.metrics["selected"] == 1 and dry.metrics["protected"] == 1
    res = cl.retention(retention_ctx(tmp_path, [r], apply=True))
    assert keep.exists() and not gone.exists() and res.reclaimed_bytes == 10
    assert "refused-never-touch" in outcomes(tmp_path) and {a["target"] for a in audit_rows(tmp_path) if a["outcome"] == "done"} == {str(gone)}


def test_the_never_touch_list_equals_the_registry_floor():
    from homelab_maint import registry
    assert [rx.pattern for rx in cl.NEVER_TOUCH] == registry.NEVER_TOUCH


def test_caps_stop_a_run_by_items_and_dry_run_simulates_the_same_stop(tmp_path):
    logs = tmp_path / "data" / "logs"
    for i in range(5):
        mkfile(logs / f"f{i}.log", age_s=(10 + i) * DAY)   # f4 oldest ... f0 newest
    r = rule(tmp_path, max_age_days=1)
    dry = cl.retention(retention_ctx(tmp_path, [r], max_items_per_run=2))
    assert dry.metrics["selected"] == 2 and dry.metrics["deferred"] == 3 and dry.metrics["capped"] is True
    would = {a["target"] for a in audit_rows(tmp_path)}
    res = cl.retention(retention_ctx(tmp_path, [r], apply=True, max_items_per_run=2))
    assert present(logs) == ["f0.log", "f1.log", "f2.log"]       # oldest two went first
    assert {a["target"] for a in audit_rows(tmp_path) if a["outcome"] == "done"} == would
    assert "refused-cap" in outcomes(tmp_path)
    assert res.status == "info" and res.metrics["capped"] and res.metrics["deferred"] == 3
    assert res.reclaimed_bytes == 20
    ascii_ok(res)


def test_byte_cap_stops_run_and_oversize_item_does_not_block_the_rest(tmp_path):
    logs = tmp_path / "data" / "logs"
    mkfile(logs / "huge.log", age_s=30 * DAY, size=1000)       # alone bigger than the cap
    for i in range(4):
        mkfile(logs / f"s{i}.log", age_s=(10 + i) * DAY, size=10)
    r = rule(tmp_path, max_age_days=1)
    cap = 25 / GIB                                             # 25 bytes
    res = cl.retention(retention_ctx(tmp_path, [r], apply=True, max_gib_per_run=cap))
    assert (logs / "huge.log").exists()                        # never deleted: it can never fit
    assert res.metrics["oversize"] == 1
    assert len([p for p in logs.iterdir() if p.name.startswith("s")]) == 2   # 2 x 10 B fit, the rest deferred
    assert res.reclaimed_bytes == 20


def test_symlink_escapes_are_not_followed(tmp_path):
    logs = tmp_path / "data" / "logs"
    outside = tmp_path / "outside"
    outer_file = mkfile(outside / "secret.log", age_s=60 * DAY)
    mkfile(outside / "deep" / "more.log", age_s=60 * DAY)
    mkfile(logs / "real.log", age_s=30 * DAY)
    mkfile(logs / "sub" / "inner.log", age_s=30 * DAY)
    (logs / "linkdir").symlink_to(outside)                     # dir symlink: must not be entered
    (logs / "linkfile.log").symlink_to(outer_file)             # file symlink: skipped, not unlinked
    (logs / "sub" / "deeplink").symlink_to(outside)
    r = rule(tmp_path, glob="**/*.log", max_age_days=1)
    dry = cl.retention(retention_ctx(tmp_path, [r]))
    assert "2 symlinks skipped" in dry.items[0]["state"] or "1 symlinks skipped" in dry.items[0]["state"]
    res = cl.retention(retention_ctx(tmp_path, [r], apply=True))
    assert present(outside) == ["deep", "secret.log"] and (outside / "deep" / "more.log").exists()
    assert (logs / "linkdir").is_symlink() and (logs / "linkfile.log").is_symlink()
    assert (logs / "sub" / "deeplink").is_symlink()
    assert not (logs / "real.log").exists() and not (logs / "sub" / "inner.log").exists()
    assert res.metrics["selected"] == 2


def test_remove_entry_refuses_a_directory_swapped_for_a_symlink_after_the_scan(tmp_path):
    root = tmp_path / "data" / "logs"
    mkfile(root / "sub" / "old.log", age_s=30 * DAY)
    ents, _, complete = cl._scan(str(root), ["sub", "*.log"])
    assert complete and [e.rel for e in ents] == ["sub/old.log"]
    outside = tmp_path / "outside"
    keep = mkfile(outside / "old.log", age_s=30 * DAY)
    (root / "sub" / "old.log").unlink()
    (root / "sub").rmdir()
    (root / "sub").symlink_to(outside)                        # the swap
    with pytest.raises(OSError):
        cl._remove_entry(str(root), ents[0])
    assert keep.exists()


def test_remove_entry_refuses_a_file_modified_after_the_scan(tmp_path):
    root = tmp_path / "data" / "logs"
    f = mkfile(root / "a.log", age_s=30 * DAY)
    ents, _, _ = cl._scan(str(root), ["*.log"])
    os.utime(f, (NOW, NOW))
    with pytest.raises(cl._Changed):
        cl._remove_entry(str(root), ents[0])
    assert f.exists()


def test_recent_files_are_never_deleted(tmp_path):
    logs = tmp_path / "data" / "logs"
    mkfile(logs / "five_min.log", age_s=300)
    mkfile(logs / "eleven_min.log", age_s=660)
    mkfile(logs / "future.log", age_s=-3600)                    # clock skew: mtime in the future
    res = cl.retention(retention_ctx(tmp_path, [rule(tmp_path, max_age_days=0.0001)], apply=True))
    assert present(logs) == ["five_min.log", "future.log"]
    assert "2 recent" in res.items[0]["state"] or "1 recent" in res.items[0]["state"]


def test_max_age_boundary_is_strict(tmp_path):
    logs = tmp_path / "data" / "logs"
    mkfile(logs / "exact.log", age_s=3 * DAY)
    mkfile(logs / "older.log", age_s=3 * DAY + 1)
    cl.retention(retention_ctx(tmp_path, [rule(tmp_path, max_age_days=3)], apply=True))
    assert present(logs) == ["exact.log"]


def test_directories_only_removed_when_empty_and_never_recursively(tmp_path):
    logs = tmp_path / "data" / "logs"
    (logs / "empty").mkdir(parents=True)
    os.utime(logs / "empty", (NOW - 30 * DAY,) * 2)
    mkfile(logs / "full" / "x.bin", age_s=30 * DAY)
    os.utime(logs / "full", (NOW - 30 * DAY,) * 2)
    r = rule(tmp_path, glob="*", max_age_days=1, files_only=False)
    cl.retention(retention_ctx(tmp_path, [r], apply=True))
    assert present(logs) == ["full"] and (logs / "full" / "x.bin").exists()
    r["files_only"] = True
    (logs / "empty2").mkdir()
    os.utime(logs / "empty2", (NOW - 30 * DAY,) * 2)
    cl.retention(retention_ctx(tmp_path, [r], apply=True))
    assert (logs / "empty2").exists()                          # files_only keeps directories


def test_globstar_walks_subdirs_but_plain_glob_stays_at_top_level(tmp_path):
    logs = tmp_path / "data" / "logs"
    mkfile(logs / "a.log", age_s=30 * DAY)
    mkfile(logs / "sub" / "b.log", age_s=30 * DAY)
    mkfile(logs / "sub" / "deep" / "c.log", age_s=30 * DAY)
    cl.retention(retention_ctx(tmp_path, [rule(tmp_path, glob="*.log", max_age_days=1)], apply=True))
    assert (logs / "sub" / "b.log").exists() and not (logs / "a.log").exists()
    cl.retention(retention_ctx(tmp_path, [rule(tmp_path, glob="**/*.log", max_age_days=1)], apply=True))
    assert not (logs / "sub" / "b.log").exists() and not (logs / "sub" / "deep" / "c.log").exists()


def _keep_case(files, sel, deleted):
    return pytest.param(files, sel, deleted, id=json.dumps(sel, default=str) + "-" + ",".join(sorted(deleted)))


B5 = {f"b{i}.zip": i * DAY for i in range(1, 6)}               # b1 newest (1 d) ... b5 oldest (5 d)
KEEP_NEWEST_CASES = [
    # 1-4: the core semantics
    _keep_case(B5, {"keep_newest": 3}, {"b4.zip", "b5.zip"}),
    _keep_case(B5, {"keep_newest": 5}, set()),                  # exactly as many as exist
    _keep_case(B5, {"keep_newest": 50}, set()),                 # more than exist
    _keep_case(B5, {"keep_newest": 1}, {"b2.zip", "b3.zip", "b4.zip", "b5.zip"}),
    # 5: both selectors must hold (beyond the newest 2 AND older than 3.5 d)
    _keep_case(B5, {"keep_newest": 2, "max_age_days": 3.5}, {"b4.zip", "b5.zip"}),
    # 6: ... and a young file beyond the newest N survives max_age
    _keep_case(B5, {"keep_newest": 1, "max_age_days": 100}, set()),
    # 7: ties on mtime: exactly N survive, the larger names count as newer
    _keep_case({f"t{i}.zip": 5 * DAY for i in range(1, 6)}, {"keep_newest": 2}, {"t1.zip", "t2.zip", "t3.zip"}),
    # 8: a very recent file occupies a keep slot but is never deleted itself
    _keep_case({"new.zip": 60, "b1.zip": DAY, "b2.zip": 2 * DAY, "b3.zip": 3 * DAY}, {"keep_newest": 2},
               {"b2.zip", "b3.zip"}),
    # 9: recent files beyond the keep window are skipped, older ones still go
    _keep_case({"new.zip": 60, "r.zip": 300, "b1.zip": DAY}, {"keep_newest": 1}, {"b1.zip"}),
    # 10: files the glob does not match neither count nor get deleted
    _keep_case({**B5, "other.txt": 30 * DAY, "newer.txt": 10}, {"keep_newest": 3}, {"b4.zip", "b5.zip"}),
    # 11: keep_newest alone, a single file is always kept
    _keep_case({"only.zip": 99 * DAY}, {"keep_newest": 1}, set()),
    # 12: no files at all
    _keep_case({}, {"keep_newest": 3}, set()),
]


@pytest.mark.parametrize("files,sel,deleted", KEEP_NEWEST_CASES)
def test_keep_newest_semantics(tmp_path, files, sel, deleted):
    logs = tmp_path / "data" / "logs"
    logs.mkdir(parents=True)
    for name, age in files.items():
        mkfile(logs / name, age_s=age)
    r = rule(tmp_path, glob="*.zip")
    r.update(sel)
    cl.retention(retention_ctx(tmp_path, [r], apply=True))
    assert set(files) - set(present(logs)) == deleted


def test_keep_newest_symlinks_neither_take_a_slot_nor_get_deleted(tmp_path):
    logs = tmp_path / "data" / "logs"
    for n, age in (("b1.zip", DAY), ("b2.zip", 2 * DAY), ("b3.zip", 3 * DAY)):
        mkfile(logs / n, age_s=age)
    (logs / "link.zip").symlink_to(logs / "b3.zip")
    os.utime(logs / "link.zip", (NOW - 10, NOW - 10), follow_symlinks=False)
    cl.retention(retention_ctx(tmp_path, [rule(tmp_path, glob="*.zip", keep_newest=2)], apply=True))
    assert present(logs) == ["b1.zip", "b2.zip", "link.zip"]


def test_real_kavita_shaped_rules(tmp_path):
    """Backups: keep the newest 3 zips; logs: drop kavita*.log older than 3 days; the active day is untouched."""
    cfgdir = tmp_path / "data" / "kavita"
    for d in range(20):
        mkfile(cfgdir / "backups" / f"kavita_backup_09_{d:02d}.zip", age_s=(d + 1) * DAY - 3600 * 5)
        mkfile(cfgdir / "logs" / f"kavita202609{d:02d}.log", age_s=d * DAY + 7200)
    rules = [{"name": "kavita-logs", "path": str(cfgdir / "logs"), "glob": "kavita*.log", "max_age_days": 3},
             {"name": "kavita-backups", "path": str(cfgdir / "backups"), "glob": "*.zip", "keep_newest": 3}]
    res = cl.retention(retention_ctx(tmp_path, rules, apply=True))
    assert len(present(cfgdir / "backups")) == 3
    assert present(cfgdir / "logs") == [f"kavita202609{d:02d}.log" for d in range(3)]
    assert res.metrics["selected"] == 17 + 17
    ascii_ok(res)


# =========================================================================== docker_cache
LS_JSON = "\n".join(json.dumps(o) for o in (
    {"Name": "immaculaterr-builder", "Driver": "docker-container", "Nodes": [{"Name": "x0", "Status": "running"}]},
    {"Name": "default", "Driver": "docker", "Nodes": [{"Name": "default", "Status": "running"}]},
    {"Name": "asleep", "Driver": "docker-container", "Nodes": [{"Name": "a0", "Status": "inactive"}]},
    {"Name": "bad name;rm", "Driver": "docker", "Nodes": [{"Status": "running"}]}))


def du_text(total):
    return ("ID\t\t\t\t\t\tRECLAIMABLE\tSIZE\t\tLAST ACCESSED\n"
            "abc*                                    \ttrue \t\t8.192kB   \t16 hours ago\n"
            f"Shared:\t\t1.088GB\nPrivate:\t21.16GB\nReclaimable:\t{total}\nTotal:\t\t{total}\n")


def cache_sh(monkeypatch, sizes, prune_rc=0):
    """sizes: {builder: 'NN.NGB'}; a prune drops that builder's size to 7GB."""
    state = dict(sizes)

    def du(cmd):
        b = cmd.split("--builder ")[1].split()[0]
        return ok(du_text(state[b])) if b in state else (1, "", f'ERROR: no builder "{b}" found')

    def prune(cmd):
        if prune_rc == 0:
            state[cmd.split("--builder ")[1].split()[0]] = "7GB"
        return (prune_rc, "", "boom" if prune_rc else "")

    return use_sh(monkeypatch, ("docker buildx ls --format json", ok(LS_JSON)), ("docker buildx du", du),
                  ("docker buildx prune", prune))


def test_docker_cache_dry_run_lists_prune_but_never_runs_it(tmp_path, monkeypatch):
    sh = cache_sh(monkeypatch, {"default": "22.25GB", "immaculaterr-builder": "9.53GB"})
    res = cl.docker_cache(mk("docker_cache", high_gib=15, low_gib=8))
    assert sh.mutating() == [] and res.status == "info"
    assert [a["target"] for a in audit_rows(tmp_path)] == ["builder:default"]
    assert set(outcomes(tmp_path)) == {"dry-run"}
    assert res.metrics["selected"] == 1 and res.items[0]["state"] == "would"
    ascii_ok(res)


def test_docker_cache_apply_prunes_lru_down_to_low_per_builder(tmp_path, monkeypatch):
    sh = cache_sh(monkeypatch, {"default": "22.25GB", "immaculaterr-builder": "9.53GB"})
    res = cl.docker_cache(mk("docker_cache", apply=True, high_gib=15, low_gib=8))
    assert sh.mutating() == [f"docker buildx prune --builder default -f --max-used-space {8 * GIB}"]
    assert res.status == "ok" and res.metrics["actual_freed_h"].startswith("14.")   # 22.25 GB -> 7 GB
    assert res.reclaimed_bytes > 0
    assert not any("volume" in c for c in sh.calls)             # never touches volumes
    ascii_ok(res)


def test_docker_cache_under_high_threshold_does_nothing(tmp_path, monkeypatch):
    sh = cache_sh(monkeypatch, {"default": "9GB", "immaculaterr-builder": "1GB"})
    res = cl.docker_cache(mk("docker_cache", apply=True, high_gib=15, low_gib=8))
    assert sh.mutating() == [] and res.status == "ok" and "under" in res.summary


def test_docker_cache_skips_while_a_build_is_running(tmp_path, monkeypatch):
    sh = cache_sh(monkeypatch, {"default": "30GB"})
    monkeypatch.setattr(cl, "_busy", lambda n: (n == "docker_build", "buildkitd step"))
    res = cl.docker_cache(mk("docker_cache", apply=True))
    assert res.status == "skipped" and sh.calls == []


@pytest.mark.parametrize("opts", [{"high_gib": 8, "low_gib": 8}, {"high_gib": 5, "low_gib": 9},
                                  {"high_gib": "15"}, {"low_gib": -1}, {"high_gib": 0}])
def test_docker_cache_bad_thresholds_fail_closed(tmp_path, monkeypatch, opts):
    sh = cache_sh(monkeypatch, {"default": "30GB"})
    res = cl.docker_cache(mk("docker_cache", apply=True, **opts))
    assert res.status == "skipped" and sh.calls == []


def test_docker_cache_unmeasurable_builder_or_missing_docker_is_left_alone(tmp_path, monkeypatch):
    sh = cache_sh(monkeypatch, {"default": "30GB"})             # immaculaterr-builder du => error
    res = cl.docker_cache(mk("docker_cache", apply=True, high_gib=15, low_gib=8))
    assert [c for c in sh.mutating()] == [f"docker buildx prune --builder default -f --max-used-space {8 * GIB}"]
    assert res.metrics["unmeasured"] == 1
    use_sh(monkeypatch)                                          # docker not installed
    assert cl.docker_cache(mk("docker_cache", apply=True)).status == "skipped"


def test_docker_cache_failed_prune_is_reported_not_raised(tmp_path, monkeypatch):
    cache_sh(monkeypatch, {"default": "30GB"}, prune_rc=1)
    res = cl.docker_cache(mk("docker_cache", apply=True, high_gib=15, low_gib=8))
    assert res.status == "warn" and "failed" in res.summary and res.reclaimed_bytes == 0
    assert "failed" in outcomes(tmp_path)[-1]


def test_docker_cache_byte_cap_skips_oversized_prune(tmp_path, monkeypatch):
    sh = cache_sh(monkeypatch, {"default": "22GB"})
    res = cl.docker_cache(mk("docker_cache", apply=True, high_gib=15, low_gib=8, max_gib_per_run=5))
    assert sh.mutating() == [] and res.metrics["oversize"] == 1


# =========================================================================== docker_images
def iid(n):
    return "sha256:" + f"{n:064x}"


def df_json(images):
    """images: [(id, repo, tag, containers, unique)]"""
    return json.dumps({"Images": [{"ID": i, "Repository": r, "Tag": t, "Containers": str(c), "UniqueSize": u,
                                   "Size": "500MB"} for i, r, t, c, u in images],
                       "Containers": [], "Volumes": [], "BuildCache": []})


def images_env(monkeypatch, tmp_path, images, referenced=(), ledger=None, unref_age_days=20, ledger_age=40,
               ledger_seen=None, rm_rc=0):
    """Wire fake docker + ledger + task state. unref_age_days: how long THIS task already saw them unreferenced."""
    refs = list(referenced)
    sh = use_sh(monkeypatch,
                ("docker system df -v", ok(df_json(images))),
                ("docker ps -a -q --no-trunc", ok("".join(f"c{i}\n" for i in range(len(refs))))),
                ("docker container inspect", lambda c: ok("".join(r + "\n" for r in refs))),
                ("docker image rm", (rm_rc, "", "conflict" if rm_rc else "")))
    if ledger is not False:
        led = {"version": 1, "created": NOW - ledger_age * DAY, "updated": NOW - 600,
               "images": {k: {"first_seen": NOW - 30 * DAY, "last_seen": v, "names": ["x"]}
                          for k, v in (ledger_seen or {}).items()}}
        core.write_json_atomic(core.STATE_DIR / "ledger" / "images.json", led)
    if unref_age_days is not None:
        core.write_json_atomic(core.STATE_DIR / "tasks" / "docker_images.json",
                               {"unref_since": {i: NOW - unref_age_days * DAY for i, *_ in images}})
    return sh


def test_docker_images_selects_only_long_unused_and_removes_by_id_without_force(tmp_path, monkeypatch):
    imgs = [(iid(1), "<none>", "<none>", 0, "300MB"),          # dangling, unused: candidate
            (iid(2), "old/thing", "1.0", 0, "120MB"),          # unused: candidate
            (iid(3), "app/web", "latest", 1, "50MB"),          # used by a container
            (iid(4), "app/api", "v2", 0, "80MB")]              # referenced via inspect list only
    sh = images_env(monkeypatch, tmp_path, imgs, referenced=[iid(3), iid(4)])
    dry = cl.docker_images(mk("docker_images"))
    assert sh.mutating() == [] and dry.metrics["selected"] == 2 and set(outcomes(tmp_path)) == {"dry-run"}
    res = cl.docker_images(mk("docker_images", apply=True))
    rms = sh.mutating()
    assert sorted(rms) == sorted([f"docker image rm {iid(1)}", f"docker image rm {iid(2)}"])
    assert not any(" -f" in c or "prune" in c for c in sh.calls)
    assert res.status == "ok" and res.reclaimed_bytes == 420_000_000
    ascii_ok(res)


def test_docker_images_in_use_by_exited_container_or_df_count_is_never_selected(tmp_path, monkeypatch):
    imgs = [(iid(1), "a/b", "1", 0, "10MB"), (iid(2), "c/d", "2", 1, "10MB"), (iid(3), "e/f", "3", 0, "10MB")]
    sh = images_env(monkeypatch, tmp_path, imgs, referenced=[iid(1)])        # 1: inspect says used; 2: df says used
    cl.docker_images(mk("docker_images", apply=True))
    assert sh.mutating() == [f"docker image rm {iid(3)}"]


def test_docker_images_soak_period_protects_fresh_images_and_state_tracks_first_sight(tmp_path, monkeypatch):
    imgs = [(iid(1), "fresh/pull", "1", 0, "10MB")]
    sh = images_env(monkeypatch, tmp_path, imgs, unref_age_days=None)         # never seen unreferenced before
    ctx = mk("docker_images", apply=True)
    res = cl.docker_images(ctx)
    assert sh.mutating() == [] and ctx.state["unref_since"] == {iid(1): NOW}
    sh2 = images_env(monkeypatch, tmp_path, imgs, unref_age_days=13)          # 13 d < 14 d
    cl.docker_images(mk("docker_images", apply=True))
    assert sh2.mutating() == []
    assert "no image unused" in res.summary


def test_docker_images_in_use_again_resets_the_soak_clock(tmp_path, monkeypatch):
    imgs = [(iid(1), "a/b", "1", 0, "10MB")]
    images_env(monkeypatch, tmp_path, imgs, referenced=[iid(1)], unref_age_days=30)
    ctx = mk("docker_images", apply=True)
    cl.docker_images(ctx)
    assert ctx.state["unref_since"] == {}


def test_docker_images_ledger_rules(tmp_path, monkeypatch):
    imgs = [(iid(1), "a/b", "1", 0, "10MB"), (iid(2), "c/d", "2", 0, "10MB")]
    # seen 3 days ago by a container => kept; id 2 never seen => removed
    sh = images_env(monkeypatch, tmp_path, imgs, ledger_seen={iid(1): NOW - 3 * DAY})
    cl.docker_images(mk("docker_images", apply=True))
    assert sh.mutating() == [f"docker image rm {iid(2)}"]
    # ledger younger than unused_days: absence proves nothing
    sh = images_env(monkeypatch, tmp_path, imgs, ledger_age=5)
    res = cl.docker_images(mk("docker_images", apply=True))
    assert sh.mutating() == [] and res.metrics["candidates"] == 0
    # missing ledger / stale ledger / odd entry: fail closed
    sh = images_env(monkeypatch, tmp_path, imgs, ledger=False)
    (core.STATE_DIR / "ledger" / "images.json").unlink(missing_ok=True)
    assert cl.docker_images(mk("docker_images", apply=True)).status == "skipped" and sh.mutating() == []
    images_env(monkeypatch, tmp_path, imgs)
    led = core.read_json(core.STATE_DIR / "ledger" / "images.json")
    led["updated"] = NOW - 5 * DAY
    core.write_json_atomic(core.STATE_DIR / "ledger" / "images.json", led)
    assert cl.docker_images(mk("docker_images", apply=True)).status == "skipped"
    led["updated"] = NOW
    led["images"][iid(1)] = {"last_seen": "yesterday"}
    core.write_json_atomic(core.STATE_DIR / "ledger" / "images.json", led)
    sh = images_env(monkeypatch, tmp_path, imgs)
    core.write_json_atomic(core.STATE_DIR / "ledger" / "images.json", led)
    cl.docker_images(mk("docker_images", apply=True))
    assert sh.mutating() == [f"docker image rm {iid(2)}"]


def test_docker_images_docker_failures_select_nothing(tmp_path, monkeypatch):
    imgs = [(iid(1), "a/b", "1", 0, "10MB")]
    for broken in ("docker system df -v", "docker ps -a -q", "docker container inspect"):
        sh = images_env(monkeypatch, tmp_path, imgs, referenced=[iid(9)])
        sh.rows.insert(0, (broken, (1, "", "daemon down")))
        res = cl.docker_images(mk("docker_images", apply=True))
        assert res.status == "skipped" and sh.mutating() == [], broken
    # inspect returns fewer lines than containers: fail closed
    sh = images_env(monkeypatch, tmp_path, imgs, referenced=[iid(9)])
    sh.rows.insert(0, ("docker container inspect", ok("")))
    assert cl.docker_images(mk("docker_images", apply=True)).status == "skipped"
    # unparsable inventory
    sh = images_env(monkeypatch, tmp_path, imgs)
    sh.rows.insert(0, ("docker system df -v", ok("not json")))
    assert cl.docker_images(mk("docker_images", apply=True)).status == "skipped"


def test_docker_images_protected_names_are_never_removed(tmp_path, monkeypatch):
    imgs = [(iid(1), "ghcr.io/immich-app/immich-server", "v2", 0, "900MB"), (iid(2), "x/plain", "1", 0, "10MB")]
    sh = images_env(monkeypatch, tmp_path, imgs)
    res = cl.docker_images(mk("docker_images", apply=True))
    assert sh.mutating() == [f"docker image rm {iid(2)}"] and res.metrics["protected"] == 1


def test_docker_images_multi_tag_removed_by_name_and_any_protected_tag_blocks_it(tmp_path, monkeypatch):
    imgs = [(iid(1), "x/one", "a", 0, "10MB"), (iid(1), "x/one", "b", 0, "10MB"),
            (iid(2), "y/two", "a", 0, "10MB"), (iid(2), "immich/two", "b", 0, "10MB")]
    sh = images_env(monkeypatch, tmp_path, imgs)
    cl.docker_images(mk("docker_images", apply=True))
    assert sh.mutating() == ["docker image rm x/one:a x/one:b"]


def test_docker_images_caps_limit_items_per_run(tmp_path, monkeypatch):
    imgs = [(iid(i), f"x/n{i}", "1", 0, "10MB") for i in range(1, 6)]
    sh = images_env(monkeypatch, tmp_path, imgs)
    res = cl.docker_images(mk("docker_images", apply=True, max_items_per_run=2))
    assert len(sh.mutating()) == 2 and res.metrics["deferred"] == 3 and res.status == "info"


def test_docker_images_failed_rm_does_not_abort_and_three_failures_halt(tmp_path, monkeypatch):
    imgs = [(iid(i), f"x/n{i}", "1", 0, "10MB") for i in range(1, 7)]
    sh = images_env(monkeypatch, tmp_path, imgs, rm_rc=1)
    res = cl.docker_images(mk("docker_images", apply=True))
    assert len(sh.mutating()) == 3 and res.status == "warn" and res.metrics["failed"] == 3


def test_docker_images_bad_config_fails_closed(tmp_path, monkeypatch):
    sh = images_env(monkeypatch, tmp_path, [(iid(1), "a/b", "1", 0, "10MB")])
    for bad in (0, -5, "14", True, None):
        assert cl.docker_images(mk("docker_images", apply=True, unused_days=bad)).status == "skipped"
    assert sh.mutating() == []


# =========================================================================== apt_clean
def apt_dir(tmp_path):
    d = tmp_path / "apt"
    (d / "partial").mkdir(parents=True)
    (d / "lock").write_bytes(b"")
    (d / "a.deb").write_bytes(b"x" * 1000)
    (d / "b.deb").write_bytes(b"x" * 500)
    return d


def test_apt_clean_dry_run_then_apply(tmp_path, monkeypatch):
    d = apt_dir(tmp_path)
    monkeypatch.setattr(cl, "_apt_lock_state", lambda paths=None: "free")
    sh = use_sh(monkeypatch, ("apt-get clean", ok()))
    dry = cl.apt_clean(mk("apt_clean", cache_dir=str(d)))
    assert sh.calls == [] and dry.metrics["selected"] == 1 and dry.status == "info"
    assert dry.metrics["cache_h"] == "1.5 KiB"
    res = cl.apt_clean(mk("apt_clean", apply=True, cache_dir=str(d)))
    assert sh.calls == ["apt-get clean"] and res.reclaimed_bytes == 1500 and res.status == "ok"
    assert not any("autoremove" in c for c in sh.calls)


@pytest.mark.parametrize("gate,lock,apply,runs", [(True, "free", True, False), (False, "busy", True, False),
                                                  (False, "unknown", True, False), (False, "unknown", False, True)])
def test_apt_clean_is_blocked_by_gate_lock_or_unverifiable_lock(tmp_path, monkeypatch, gate, lock, apply, runs):
    d = apt_dir(tmp_path)
    monkeypatch.setattr(cl, "_busy", lambda n: (gate, "apt running"))
    monkeypatch.setattr(cl, "_apt_lock_state", lambda paths=None: lock)
    sh = use_sh(monkeypatch, ("apt-get clean", ok()))
    res = cl.apt_clean(mk("apt_clean", apply=apply, cache_dir=str(d)))
    assert sh.calls == []                                        # a dry run never runs apt-get either
    assert (res.status == "skipped") == (not runs)
    if runs:
        assert res.metrics["selected"] == 1 and "without root" in res.summary


def test_apt_clean_failure_is_reported(tmp_path, monkeypatch):
    d = apt_dir(tmp_path)
    monkeypatch.setattr(cl, "_apt_lock_state", lambda paths=None: "free")
    use_sh(monkeypatch, ("apt-get clean", (100, "", "E: Could not get lock")))
    res = cl.apt_clean(mk("apt_clean", apply=True, cache_dir=str(d)))
    assert res.status == "warn" and res.reclaimed_bytes == 0


def test_apt_clean_empty_cache_is_a_noop(tmp_path, monkeypatch):
    d = tmp_path / "apt"
    d.mkdir()
    monkeypatch.setattr(cl, "_apt_lock_state", lambda paths=None: "free")
    sh = use_sh(monkeypatch)
    assert cl.apt_clean(mk("apt_clean", apply=True, cache_dir=str(d))).status == "ok" and sh.calls == []


def test_apt_lock_probe_sees_a_real_posix_lock_held_by_another_process(tmp_path):
    lock = tmp_path / "lock-frontend"
    lock.write_bytes(b"")
    assert cl._apt_lock_state((str(lock),)) == "free"
    assert cl._apt_lock_state((str(tmp_path / "absent"),)) == "free"
    code = ("import fcntl,sys,os\nf=open(sys.argv[1],'r+')\nfcntl.lockf(f, fcntl.LOCK_EX)\n"
            "print('held',flush=True)\nsys.stdin.read()\n")
    p = subprocess.Popen([sys.executable, "-c", code, str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        assert cl._apt_lock_state((str(lock),)) == "busy"
    finally:
        p.stdin.close()
        p.wait(timeout=10)
    assert cl._apt_lock_state((str(lock),)) == "free"
    os.chmod(lock, 0)
    if os.geteuid() != 0:
        assert cl._apt_lock_state((str(lock),)) == "unknown"     # unreadable lock file: cannot say


# =========================================================================== snap_revisions
SNAP_LIST = """Name              Version                 Rev    Tracking         Publisher    Notes
bare              1.0                     5      latest/stable    canonical**  base
code              07f806f9                267    latest/stable    vscode**     classic
code              04c0d99f                266    latest/stable    vscode**     disabled,classic
core24            20260410                1643   latest/stable    canonical**  base,disabled
core24            20260824                2124   latest/stable    canonical**  base
firefox           156.0-1                 8929   latest/stable/…  mozilla**    disabled
firefox           157.0-1                 8995   latest/stable/…  mozilla**    -
oldie             1                       3      latest/stable    someone      disabled
plexmediaserver   1.43.3.10896-cb3ebc72d  536    latest/stable    plexinc**    disabled
plexmediaserver   1.43.4.10903-e5521bd8c  540    latest/stable    plexinc**    -
snapd             2.77.1                  28254  latest/stable    canonical**  snapd
snapd             2.76.3                  27738  latest/stable    canonical**  snapd,disabled
three             3                       30     latest/stable    x            disabled
three             3                       31     latest/stable    x            disabled
three             3                       32     latest/stable    x            -
"""
CHANGES_DONE = "ID   Status  Spawn  Ready  Summary\n549  Done  yesterday  yesterday  Auto-refresh snap \"thunderbird\"\n"


def snap_sh(monkeypatch, retain=None, changes=CHANGES_DONE, lst=SNAP_LIST):
    get = (0, f"{retain}\n", "") if retain is not None else \
          (1, "", 'error: snap "core" has no "refresh.retain" configuration option')
    return use_sh(monkeypatch, ("snap changes", ok(changes)), ("snap list --all", ok(lst)),
                  ("snap get system refresh.retain", get), ("snap set", ok()), ("snap remove", ok()))


def test_snap_revisions_dry_run_changes_nothing(tmp_path, monkeypatch):
    sh = snap_sh(monkeypatch)
    res = cl.snap_revisions(mk("snap_revisions", snap_dir=str(tmp_path)))
    assert sh.mutating() == [] and set(outcomes(tmp_path)) == {"dry-run"}
    targets = {a["target"] for a in audit_rows(tmp_path)}
    assert "system refresh.retain=2" in targets and "code rev 266" in targets and "firefox rev 8929" in targets
    assert res.metrics["protected"] == 1                         # plexmediaserver rev 536
    ascii_ok(res)


def test_snap_revisions_report_shows_the_refresh_retain_change_first(tmp_path, monkeypatch):
    many = "Name Version Rev Tracking Publisher Notes\n" + "".join(
        f"s{i:02d} 1 {i + 1} latest/stable x disabled\ns{i:02d} 1 {i + 2} latest/stable x -\n" for i in range(20))
    for i in range(20):
        (tmp_path / f"s{i:02d}_{i + 1}.snap").write_bytes(b"x" * (100 + i))     # sizes sort the retain row (0 B) last
    snap_sh(monkeypatch, lst=many)
    res = cl.snap_revisions(mk("snap_revisions", snap_dir=str(tmp_path)))
    assert res.items[0]["name"] == "refresh.retain unset -> 2" and len(res.items) == 12


def test_snap_revisions_apply_removes_only_disabled_revisions_never_active_or_protected(tmp_path, monkeypatch):
    for n, sz in (("code_266.snap", 5000), ("firefox_8929.snap", 700)):
        (tmp_path / n).write_bytes(b"x" * sz)
    sh = snap_sh(monkeypatch)
    res = cl.snap_revisions(mk("snap_revisions", apply=True, snap_dir=str(tmp_path)))
    cmds = sh.mutating()
    assert cmds[0] == "snap set system refresh.retain=2"
    removed = sorted(c for c in cmds if c.startswith("snap remove"))
    assert removed == sorted(["snap remove code --revision=266", "snap remove core24 --revision=1643",
                              "snap remove firefox --revision=8929", "snap remove snapd --revision=27738",
                              "snap remove three --revision=30", "snap remove three --revision=31"])
    for active in ("267", "2124", "8995", "28254", "32", "540", "5"):          # the enabled revisions
        assert not any(c.endswith(f"--revision={active}") for c in removed), active
    assert not any("plexmediaserver" in c for c in cmds)         # protected
    assert not any("oldie" in c for c in cmds)                   # snap with no active revision: untouched
    assert res.reclaimed_bytes == 5700


def test_snap_revisions_keep_disabled_and_retain_idempotence(tmp_path, monkeypatch):
    sh = snap_sh(monkeypatch, retain=2)
    cl.snap_revisions(mk("snap_revisions", apply=True, snap_dir=str(tmp_path), keep_disabled=1))
    cmds = sh.mutating()
    assert "snap set system refresh.retain=2" not in cmds        # already 2
    assert cmds == ["snap remove three --revision=30"]           # one disabled kept per snap, newest kept
    sh = snap_sh(monkeypatch, retain=3)
    cl.snap_revisions(mk("snap_revisions", apply=True, snap_dir=str(tmp_path), keep_disabled=5))
    assert sh.mutating() == ["snap set system refresh.retain=2"]


def test_snap_revisions_waits_for_in_progress_changes_and_unreadable_state(tmp_path, monkeypatch):
    busy = CHANGES_DONE + '550  Doing  today  -  Auto-refresh snaps "code"\n'
    for changes, lst in ((busy, SNAP_LIST), (CHANGES_DONE, "garbage"), (CHANGES_DONE, "")):
        sh = snap_sh(monkeypatch, changes=changes, lst=lst)
        res = cl.snap_revisions(mk("snap_revisions", apply=True))
        assert res.status == "skipped" and sh.mutating() == []
    use_sh(monkeypatch, ("snap changes", (1, "", "x")))
    assert cl.snap_revisions(mk("snap_revisions", apply=True)).status == "skipped"
    use_sh(monkeypatch)                                          # snap not installed
    assert cl.snap_revisions(mk("snap_revisions", apply=True)).status == "skipped"


def test_snap_revisions_unreadable_retain_does_not_guess_and_bad_config_fails_closed(tmp_path, monkeypatch):
    sh = snap_sh(monkeypatch)
    sh.rows.insert(0, ("snap get", (1, "", "error: something else")))
    res = cl.snap_revisions(mk("snap_revisions", apply=True, snap_dir=str(tmp_path)))
    assert "snap set system" not in " ".join(sh.mutating()) and "unreadable" in res.summary
    for bad in ({"retain": 1}, {"retain": "2"}, {"retain": 99}, {"keep_disabled": -1}):
        sh = snap_sh(monkeypatch)
        assert cl.snap_revisions(mk("snap_revisions", apply=True, **bad)).status == "skipped"
        assert sh.mutating() == []


# =========================================================================== trash
def fake_trash(tmp_path, monkeypatch, uid=None):
    home = tmp_path / "home"
    t = home / ".local" / "share" / "Trash"
    (t / "files").mkdir(parents=True, exist_ok=True)
    (t / "info").mkdir(exist_ok=True)
    monkeypatch.setattr(cl, "_home_of", lambda user: (str(home), os.getuid() if uid is None else uid)
                        if user == "ohmz" else None)
    return t


def put(t, name, age_days, payload="file", orig=None, date=None):
    f = t / "files" / name
    if payload == "file":
        f.write_bytes(b"x" * 100)
    elif payload == "dir":
        (f / "nested").mkdir(parents=True)
        (f / "nested" / "a.bin").write_bytes(b"x" * 50)
        (f / "nested" / "b.bin").write_bytes(b"x" * 50)
    d = date or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW - age_days * DAY))
    (t / "info" / f"{name}.trashinfo").write_text(f"[Trash Info]\nPath={orig or '/home/ohmz/' + name}\nDeletionDate={d}\n")
    return f


def test_trash_removes_old_entries_files_dirs_and_info_and_keeps_recent(tmp_path, monkeypatch):
    t = fake_trash(tmp_path, monkeypatch)
    put(t, "old.txt", 40)
    put(t, "olddir", 45, payload="dir")
    put(t, "recent.txt", 5)
    put(t, "edge.txt", 29)
    before = sorted(p.name for p in (t / "info").iterdir())
    dry = cl.trash(mk("trash", users=["ohmz"], max_age_days=30))
    assert sorted(p.name for p in (t / "info").iterdir()) == before and (t / "files" / "olddir").exists()
    assert dry.metrics["selected"] == 2 and dry.metrics["kept"] == 2 and set(outcomes(tmp_path)) == {"dry-run"}
    res = cl.trash(mk("trash", apply=True, users=["ohmz"], max_age_days=30))
    assert sorted(p.name for p in (t / "files").iterdir()) == ["edge.txt", "recent.txt"]
    assert sorted(p.name for p in (t / "info").iterdir()) == ["edge.txt.trashinfo", "recent.txt.trashinfo"]
    assert res.reclaimed_bytes >= 200 and res.status == "ok"           # 100 B file + the dir tree (>= 100 B of files)
    ascii_ok(res)


def test_trash_symlink_payload_is_unlinked_not_followed_and_nested_symlinks_survive(tmp_path, monkeypatch):
    t = fake_trash(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    keep = mkfile(outside / "precious.txt")
    link = t / "files" / "lnk"
    link.symlink_to(outside)
    (t / "info" / "lnk.trashinfo").write_text(
        f"[Trash Info]\nPath=/x\nDeletionDate={time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(NOW - 90 * DAY))}\n")
    d = put(t, "d", 90, payload="dir")
    (d / "nested" / "out").symlink_to(outside)                # a symlink inside a trashed dir
    cl.trash(mk("trash", apply=True, users=["ohmz"], max_age_days=30))
    assert keep.exists() and not link.is_symlink() and not d.exists()


def test_trash_orphan_info_removed_but_unparsable_or_missing_dates_and_odd_names_kept(tmp_path, monkeypatch):
    t = fake_trash(tmp_path, monkeypatch)
    (t / "info" / "ghost.trashinfo").write_text(
        f"[Trash Info]\nPath=/x\nDeletionDate={time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(NOW - 90 * DAY))}\n")
    put(t, "baddate", 0, date="yesterday-ish")
    f = t / "files" / "nodate"
    f.write_bytes(b"x")
    (t / "info" / "nodate.trashinfo").write_text("[Trash Info]\nPath=/x\n")
    (t / "info" / "..trashinfo").write_text("[Trash Info]\n")
    res = cl.trash(mk("trash", apply=True, users=["ohmz"], max_age_days=30))
    assert not (t / "info" / "ghost.trashinfo").exists()
    assert (t / "files" / "baddate").exists() and f.exists()
    assert res.metrics["skipped"] >= 3


def test_trash_protected_names_and_original_paths_are_kept(tmp_path, monkeypatch):
    t = fake_trash(tmp_path, monkeypatch)
    put(t, "immich-import-logs", 90)
    put(t, "innocent", 90, orig="/mnt/backup/stuff")             # original path is protected
    put(t, "plain", 90)
    res = cl.trash(mk("trash", apply=True, users=["ohmz"], max_age_days=30))
    assert sorted(p.name for p in (t / "files").iterdir()) == ["immich-import-logs", "innocent"]
    assert res.metrics["protected"] == 2


def test_trash_unknown_user_symlinked_trash_or_foreign_owner_selects_nothing(tmp_path, monkeypatch):
    t = fake_trash(tmp_path, monkeypatch)
    put(t, "old", 90)
    assert cl.trash(mk("trash", apply=True, users=["nobody-here", "../etc", ""], max_age_days=30)).metrics["selected"] == 0
    fake_trash(tmp_path, monkeypatch, uid=os.getuid() + 1)       # trash owned by someone else
    assert cl.trash(mk("trash", apply=True, users=["ohmz"], max_age_days=30)).metrics["selected"] == 0
    assert (t / "files" / "old").exists()
    # the Trash dir itself replaced by a symlink to elsewhere
    t2 = fake_trash(tmp_path, monkeypatch)
    real = tmp_path / "real_trash"
    t2.rename(real)
    t2.symlink_to(real)
    assert cl.trash(mk("trash", apply=True, users=["ohmz"], max_age_days=30)).metrics["selected"] == 0
    assert (real / "files" / "old").exists()
    for users in ([], None, "ohmz"):
        assert cl.trash(mk("trash", apply=True, users=users, max_age_days=30)).metrics["selected"] == 0
    assert (real / "files" / "old").exists()
    # a symlinked ancestor (~/.local -> elsewhere) is refused too
    home2 = tmp_path / "home2"
    (tmp_path / "elsewhere" / "share" / "Trash" / "files").mkdir(parents=True)
    (tmp_path / "elsewhere" / "share" / "Trash" / "info").mkdir()
    victim = tmp_path / "elsewhere" / "share" / "Trash" / "files" / "old"
    victim.write_bytes(b"x")
    (tmp_path / "elsewhere" / "share" / "Trash" / "info" / "old.trashinfo").write_text(
        "[Trash Info]\nPath=/x\nDeletionDate=2000-01-01T00:00:00\n")
    home2.mkdir()
    (home2 / ".local").symlink_to(tmp_path / "elsewhere")
    monkeypatch.setattr(cl, "_home_of", lambda user: (str(home2), os.getuid()))
    assert cl.trash(mk("trash", apply=True, users=["ohmz"], max_age_days=30)).metrics["selected"] == 0
    assert victim.exists()
    assert cl.trash(mk("trash", apply=True, users=["ohmz"], max_age_days=0)).status == "skipped"


def test_trash_caps_stop_the_run(tmp_path, monkeypatch):
    t = fake_trash(tmp_path, monkeypatch)
    for i in range(5):
        put(t, f"n{i}", 90 + i)
    res = cl.trash(mk("trash", apply=True, users=["ohmz"], max_age_days=30, max_items_per_run=2))
    assert len(list((t / "files").iterdir())) == 3 and res.metrics["capped"] and res.status == "info"


# =========================================================================== gradle_reaper
DAEMON_ARGV = ["/home/ohmz/.gradle/jdks/jdk-21/bin/java", "-Xmx2g", "-cp", "/x/gradle-daemon-main-9.8.0.jar",
               "org.gradle.launcher.daemon.bootstrap.GradleDaemon", "9.8.0"]


def mkproc(proc, pid, argv, ticks=100, start=5000, ppid=1, state="S", comm="java"):
    d = proc / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "stat").write_text(f"{pid} ({comm}) {state} {ppid} 1 1 0 -1 0 0 0 0 0 {ticks} 0 0 0 20 0 1 0 {start} 0 0 0\n")
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")


KILLS: list = []


@pytest.fixture
def fake_proc(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(cl, "PROC", proc)
    KILLS.clear()
    monkeypatch.setattr(cl, "_kill", lambda pid, sig: KILLS.append((pid, sig)))
    return proc


def reaper(now, apply=False, ticks=None, proc=None, **opts):
    ctx = mk("gradle_reaper", apply=apply, now=now, idle_minutes=120, min_samples=3, **opts)
    res = cl.gradle_reaper(ctx)
    ctx.save_state()
    return res


def run_days(proc, days, ticks_per_day=0, **kw):
    """Run the reaper once per simulated day against a daemon whose CPU counter grows by ticks_per_day."""
    res = None
    for d in range(days):
        mkproc(proc, 4242, DAEMON_ARGV, ticks=100 + d * ticks_per_day)
        res = reaper(NOW + d * DAY, **kw)
    return res


def test_gradle_reaper_needs_enough_idle_samples_then_terminates_only_in_apply(fake_proc, monkeypatch):
    res = run_days(fake_proc, 2)
    assert KILLS == [] and "none idle long enough" in res.summary
    res = run_days(fake_proc, 3)                                      # third sample: 3 samples over 2 days, no CPU
    assert res.metrics["idle"] == 1 and res.metrics["selected"] == 1 and KILLS == []   # dry-run: no kill
    # apply: SIGTERM, process exits at once => no SIGKILL
    def term(pid, sig):
        KILLS.append((pid, sig))
        import shutil
        shutil.rmtree(fake_proc / str(pid))
    monkeypatch.setattr(cl, "_kill", term)
    mkproc(fake_proc, 4242, DAEMON_ARGV, ticks=100)
    res = reaper(NOW + 3 * DAY, apply=True)
    assert KILLS == [(4242, signal.SIGTERM)] and res.status == "ok"
    ascii_ok(res)


def test_gradle_reaper_sigkill_only_if_still_alive_after_30s(fake_proc, monkeypatch):
    run_days(fake_proc, 3)
    t0 = cl._mono()
    mkproc(fake_proc, 4242, DAEMON_ARGV, ticks=100)
    reaper(NOW + 3 * DAY, apply=True)
    assert KILLS == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert cl._mono() - t0 >= 30


def test_gradle_reaper_refuses_to_signal_a_recycled_pid(fake_proc):
    mkproc(fake_proc, 4242, DAEMON_ARGV, start=5000)
    with pytest.raises(RuntimeError):
        cl._terminate(4242, 9999)                                     # different start time => different process
    with pytest.raises(RuntimeError):
        cl._terminate(777, 1)                                         # gone
    assert KILLS == []


def test_gradle_reaper_cpu_growth_or_pid_reuse_resets_idleness(fake_proc):
    res = run_days(fake_proc, 4, ticks_per_day=100_000)               # ~1.2 % of a core per day > 0.05 %
    assert res.metrics["idle"] == 0 and KILLS == []
    assert any("cpu" in i["state"] for i in res.items)
    # same pid but a different start time: samples start over
    run_days(fake_proc, 3)
    mkproc(fake_proc, 4242, DAEMON_ARGV, ticks=100, start=7777)
    res = reaper(NOW + 3 * DAY, apply=True)
    assert KILLS == [] and res.metrics["idle"] == 0


def test_gradle_reaper_never_touches_daemons_while_a_build_runs(fake_proc, monkeypatch):
    run_days(fake_proc, 3)
    for argv in (["/jdk/bin/java", "worker.org.gradle.process.internal.worker.GradleWorkerMain", "'Gradle Test Executor 3'"],
                 ["/jdk/bin/java", "-cp", "gradle-launcher.jar", "org.gradle.launcher.GradleMain", "build"],
                 ["/jdk/bin/java", "org.gradle.wrapper.GradleWrapperMain", "assemble"],
                 ["/proj/gradlew", "test"]):
        mkproc(fake_proc, 5555, argv, ppid=4242)
        mkproc(fake_proc, 4242, DAEMON_ARGV, ticks=100)
        res = reaper(NOW + 3 * DAY, apply=True)
        assert KILLS == [] and res.status == "info" and "build active" in res.summary, argv
        import shutil
        shutil.rmtree(fake_proc / "5555")
    monkeypatch.setattr(cl, "_busy", lambda n: (True, "daemon cpu 40%"))     # the gate says a build is active
    assert reaper(NOW + 3 * DAY, apply=True).status == "info" and KILLS == []


def test_gradle_reaper_ignores_shell_commands_that_merely_mention_gradle(fake_proc):
    mkproc(fake_proc, 900, ["bash", "-c", "pgrep -af GradleDaemon; ./gradlew --status"], comm="bash")
    mkproc(fake_proc, 901, ["grep", "Gradle Test Executor"], comm="grep")
    res = reaper(NOW, apply=True)
    assert res.status == "ok" and res.metrics["daemons"] == 0 and KILLS == []


def test_gradle_reaper_recognises_kotlin_daemons_and_protects_protected_cmdlines(fake_proc):
    kotlin = ["/jdk/bin/java", "-cp", "/x/kotlin-compiler-embeddable-2.0.jar",
              "org.jetbrains.kotlin.daemon.KotlinCompileDaemon", "--daemon-runFilesPath"]
    secret = DAEMON_ARGV + ["-Dproject=/srv/immich"]                  # cmdline hits a protected pattern
    for d in range(3):
        mkproc(fake_proc, 100, kotlin, ticks=7)
        mkproc(fake_proc, 200, secret, ticks=7)
        res = reaper(NOW + d * DAY, apply=True)
    from homelab_maint.core import read_json
    st = read_json(core.STATE_DIR / "tasks" / "gradle_reaper.json")
    assert set(st["gradle"]) == {"100", "200"}
    assert KILLS == [(100, signal.SIGTERM), (100, signal.SIGKILL)]      # the fake never exits => escalates
    assert all(pid != 200 for pid, _ in KILLS) and res.metrics["protected"] == 1


def test_gradle_reaper_forgets_vanished_daemons_and_fails_closed_on_bad_config(fake_proc):
    mkproc(fake_proc, 4242, DAEMON_ARGV)
    reaper(NOW)
    import shutil
    shutil.rmtree(fake_proc / "4242")
    ctx = mk("gradle_reaper", now=NOW + 1, idle_minutes=120, min_samples=3)
    cl.gradle_reaper(ctx)
    assert ctx.state["gradle"] == {}
    for bad in ({"idle_minutes": 0}, {"min_samples": 1}, {"idle_cpu_pct": -1}, {"idle_minutes": "x"}):
        assert cl.gradle_reaper(mk("gradle_reaper", apply=True, now=NOW, **bad)).status == "skipped"


def test_gradle_idle_math():
    s = [[0, 0], [3600, 0], [7200, 2]]
    assert cl._idle(s, 3, 7200, 0.05)[0] is True
    assert cl._idle(s, 4, 7200, 0.05)[0] is False                    # not enough samples
    assert cl._idle(s, 3, 9000, 0.05)[0] is False                    # window too short
    assert cl._idle([[0, 5], [7200, 3], [9000, 3]], 3, 3600, 1)[0] is False   # counter went backwards
    assert cl._idle([[0, 0], [10, 0], [7210, 5000]], 3, 3600, 0.05)[0] is False


# =========================================================================== caps
def caps_env(monkeypatch, running=("kavita",), mem="0 0", containers=None):
    ps = "".join(f"{i:012x} {n}\n" for i, n in enumerate(containers or running, 1))
    return use_sh(monkeypatch, ("docker ps --no-trunc", ok(ps)),
                  ("docker inspect --format {{.HostConfig.Memory}}", lambda c: ok(mem + "\n")),
                  ("docker update", ok()))


def seed_samples(name, peak_gib, n=10, swap=0, span_h=96, newest_age_s=900, peak=None):
    """n samples, the newest `newest_age_s` old, spread over `span_h` hours; the highest anon is `peak_gib`."""
    now = time.time()
    step = span_h * 3600 / max(n - 1, 1)
    for i in range(n):
        c = {"anon": int(peak_gib * GIB * (1 - 0.01 * i)), "swap": swap}
        if peak is not None:
            c["peak"] = peak
        core.append_history({"t": now - newest_age_s - step * i, "kind": "sample", "c": {name: c}})


def caps_ctx(apply=False, **opts):
    return mk("caps", apply=apply, now=time.time(), ceilings=opts.pop("ceilings", {"kavita": 20}), **opts)


def test_caps_dry_run_then_apply_exact_command(tmp_path, monkeypatch):
    seed_samples("kavita", 10)
    sh = caps_env(monkeypatch)
    dry = cl.caps(caps_ctx())
    assert sh.mutating() == [] and dry.metrics["selected"] == 1 and set(outcomes(tmp_path)) == {"dry-run"}
    res = cl.caps(caps_ctx(apply=True))
    assert sh.mutating() == [f"docker update --memory {20 * GIB} --memory-swap {25 * GIB} kavita"]
    assert res.status == "ok" and "set 1" in res.summary
    ascii_ok(res)


def test_caps_is_idempotent_and_keeps_stricter_limits(monkeypatch):
    seed_samples("kavita", 10)
    sh = caps_env(monkeypatch, mem=f"{20 * GIB} {25 * GIB}")
    res = cl.caps(caps_ctx(apply=True))
    assert sh.mutating() == [] and "already set" in res.summary
    sh = caps_env(monkeypatch, mem=f"{8 * GIB} {10 * GIB}")
    res = cl.caps(caps_ctx(apply=True))
    assert sh.mutating() == [] and any("stricter" in i["state"] for i in res.items)
    sh = caps_env(monkeypatch, mem=f"{64 * GIB} {64 * GIB}")             # looser limit => tightened
    cl.caps(caps_ctx(apply=True))
    assert len(sh.mutating()) == 1


def test_caps_refuses_ceiling_below_1_25x_observed_peak_or_without_history(monkeypatch):
    seed_samples("kavita", 17)                                           # 1.25 x 17 = 21.25 > 20
    sh = caps_env(monkeypatch)
    res = cl.caps(caps_ctx(apply=True))
    assert sh.mutating() == [] and "1.25x" in res.items[0]["state"]
    seed_samples("kavita", 3, n=3)                                       # few samples: still the big peak wins
    assert cl.caps(caps_ctx(apply=True)).metrics["refused"] == 1
    sh = caps_env(monkeypatch, containers=["kavita"])
    res = cl.caps(caps_ctx(apply=True, ceilings={"kavita": 20}, min_samples=500))
    assert sh.mutating() == [] and "samples" in res.items[0]["state"]


def test_caps_no_history_at_all_refuses(monkeypatch):
    sh = caps_env(monkeypatch)
    res = cl.caps(caps_ctx(apply=True))
    assert sh.mutating() == [] and res.metrics["refused"] == 1 and "0/8" in res.items[0]["state"]


def test_caps_counts_swap_in_the_observed_peak(monkeypatch):
    seed_samples("kavita", 10, swap=8 * GIB)                              # 10 + 8 = 18 GiB; 1.25x = 22.5 > 20
    sh = caps_env(monkeypatch)
    assert cl.caps(caps_ctx(apply=True)).metrics["refused"] == 1 and sh.mutating() == []


def test_caps_skips_containers_that_are_not_running(monkeypatch):
    sh = caps_env(monkeypatch, running=("other",))
    res = cl.caps(caps_ctx(apply=True))
    assert sh.mutating() == [] and res.metrics["stopped"] == 1
    assert not any(c.startswith("docker inspect") for c in sh.calls)


def test_caps_protected_containers_are_never_touched(tmp_path, monkeypatch):
    seed_samples("tunarr-host-net", 5)
    sh = caps_env(monkeypatch, running=("tunarr-host-net",))
    res = cl.caps(caps_ctx(apply=True, ceilings={"tunarr-host-net": 40}))
    assert sh.mutating() == [] and res.metrics["protected"] == 1 and "protected" in res.summary


def test_caps_bad_config_and_docker_failure_fail_closed(monkeypatch):
    sh = caps_env(monkeypatch)
    for ceil in ({"kavita": 0}, {"kavita": -4}, {"kavita": "20"}, {"kavita": True}, {"bad name": 4}, {"": 4}, {}, None):
        res = cl.caps(caps_ctx(apply=True, ceilings=ceil))
        assert sh.mutating() == [], ceil
    assert cl.caps(caps_ctx(apply=True, swap_extra_ratio=-1)).status == "skipped"   # bad ratio: nothing done
    assert sh.mutating() == []
    use_sh(monkeypatch)
    assert cl.caps(caps_ctx(apply=True)).status == "skipped"
    seed_samples("kavita", 5)
    sh = caps_env(monkeypatch, mem="garbage")
    assert cl.caps(caps_ctx(apply=True)).metrics["refused"] == 1 and sh.mutating() == []


def test_caps_item_cap_stops_run(monkeypatch):
    names = ["a1", "a2", "a3"]
    for n in names:
        seed_samples(n, 2)
    sh = caps_env(monkeypatch, running=names)
    res = cl.caps(caps_ctx(apply=True, ceilings={n: 10 for n in names}, max_items_per_run=2))
    assert len(sh.mutating()) == 2 and res.metrics["capped"] and res.status == "info"


# =========================================================================== c2_candidates
def c2_env(monkeypatch, mounts="", lsof_rc=1, lsof_out="", extra=()):
    def du(cmd):
        p = cmd.split(" -- ", 1)[1]
        return ok(f"{sum(f.stat().st_size for f in __import__('pathlib').Path(p).rglob('*') if f.is_file()) if os.path.isdir(p) else os.path.getsize(p)}\t{p}\n")
    return use_sh(monkeypatch, ("docker ps -a -q", ok("c1\n" if mounts else "")),
                  ("docker inspect --format", ok(mounts)), ("du -sxb", du),
                  ("lsof", (lsof_rc, lsof_out, "")), *extra)


def c2_ctx(tmp_path, cands, apply=False, **opts):
    return mk("c2_candidates", apply=apply, candidates=cands, **opts)


def cand_set(tmp_path):
    big = tmp_path / "files" / "big.tar.gz"
    big.parent.mkdir(parents=True)
    big.write_bytes(b"x" * (3 * MIB_ + 17))
    d = tmp_path / "files" / "olddir"
    mkfile(d / "x.bin", size=2 * MIB_ + 5)
    return big, d, [{"name": "zeta-file", "path": str(big), "why": "stale tarball"},
                    {"name": "alpha-dir", "path": str(d), "why": "old env"},
                    {"name": "missing", "path": str(tmp_path / "files" / "nope"), "why": "gone"}]


MIB_ = 1024 * 1024


def test_c2_plan_is_sorted_stable_and_skips_missing(tmp_path, monkeypatch):
    big, d, cands = cand_set(tmp_path)
    sh = c2_env(monkeypatch)
    r1 = cl.c2_candidates(c2_ctx(tmp_path, cands))
    r2 = cl.c2_candidates(c2_ctx(tmp_path, list(reversed(cands))))
    assert [i["name"] for i in r1.plan["items"]] == ["alpha-dir", "zeta-file"]
    assert r1.plan == r2.plan and core.plan_hash(r1.plan) == core.plan_hash(r2.plan)
    assert r1.plan["total_bytes"] == 5 * MIB_ and r1.metrics["missing"] == 1       # sizes floored to MiB
    assert r1.plan["items"][1]["command"] == f"rm -f -- {big}" and r1.alert is False
    assert core.plan_hash(r1.plan) in r1.summary and r1.metrics["plan_hash"] == core.plan_hash(r1.plan)
    assert big.exists() and d.exists() and sh.mutating() == []
    ascii_ok(r1)


def test_c2_plan_hash_changes_when_a_candidate_grows(tmp_path, monkeypatch):
    big, d, cands = cand_set(tmp_path)
    c2_env(monkeypatch)
    h1 = core.plan_hash(cl.c2_candidates(c2_ctx(tmp_path, cands)).plan)
    big.write_bytes(b"x" * (30 * MIB_))
    assert core.plan_hash(cl.c2_candidates(c2_ctx(tmp_path, cands)).plan) != h1


def test_c2_apply_without_a_matching_approval_never_mutates(tmp_path, monkeypatch):
    big, d, cands = cand_set(tmp_path)
    sh = c2_env(monkeypatch)
    res = cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
    assert big.exists() and d.exists() and "awaiting approval" in res.summary and sh.mutating() == []
    ap = core.STATE_DIR / "approvals"
    ap.mkdir(parents=True)
    (ap / "c2_candidates.deadbeef0000").write_text("1")                # approval for some OTHER plan
    (ap / "other_task.deadbeef").write_text("1")
    h = core.plan_hash(res.plan)
    (ap / f"other_task.{h}").write_text("1")                           # right hash, wrong task
    cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
    assert big.exists() and d.exists()
    stale = ap / f"c2_candidates.{h}"
    stale.write_text("1")
    os.utime(stale, (time.time() - 10 * DAY,) * 2)                     # expired approval
    cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
    assert big.exists() and d.exists()


def test_c2_dry_run_ignores_even_a_valid_approval(tmp_path, monkeypatch):
    big, d, cands = cand_set(tmp_path)
    c2_env(monkeypatch)
    h = core.plan_hash(cl.c2_candidates(c2_ctx(tmp_path, cands)).plan)
    (core.STATE_DIR / "approvals").mkdir(parents=True)
    (core.STATE_DIR / "approvals" / f"c2_candidates.{h}").write_text("1")
    cl.c2_candidates(c2_ctx(tmp_path, cands, apply=False))
    assert big.exists() and d.exists()


def test_c2_apply_with_approval_removes_and_consumes_it(tmp_path, monkeypatch):
    big, d, cands = cand_set(tmp_path)
    c2_env(monkeypatch)
    h = core.plan_hash(cl.c2_candidates(c2_ctx(tmp_path, cands)).plan)
    ap = core.STATE_DIR / "approvals"
    ap.mkdir(parents=True)
    (ap / f"c2_candidates.{h}").write_text("1")
    res = cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
    assert not big.exists() and not d.exists() and res.reclaimed_bytes == 5 * MIB_ and res.status == "ok"
    assert list(ap.glob("c2_candidates.*")) == [] and res.plan is not None
    assert {"done"} <= set(outcomes(tmp_path))


def test_c2_manual_check_items_are_refused_unless_explicitly_allowed(tmp_path, monkeypatch):
    big, d, cands = cand_set(tmp_path)
    cands[1]["needs_manual_check"] = True
    c2_env(monkeypatch)
    for allow, gone in ((False, False), (True, True)):
        d.mkdir(exist_ok=True)
        (d / "x.bin").write_bytes(b"x" * (2 * MIB_ + 5))
        plan = cl.c2_candidates(c2_ctx(tmp_path, cands)).plan
        h = core.plan_hash(plan)
        (core.STATE_DIR / "approvals").mkdir(parents=True, exist_ok=True)
        (core.STATE_DIR / "approvals" / f"c2_candidates.{h}").write_text("1")
        cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True, allow_manual_check_items=allow))
        assert (not d.exists()) == gone


def test_c2_protected_symlink_shallow_relative_and_bad_candidates_never_enter_the_plan(tmp_path, monkeypatch):
    big, d, cands = cand_set(tmp_path)
    link = tmp_path / "files" / "link"
    link.symlink_to(d)
    viakink = tmp_path / "files" / "link" / "x.bin"
    protected = tmp_path / "files" / "kometa-backup.tgz"
    protected.write_bytes(b"x" * MIB_)
    c2_env(monkeypatch)
    bad = [{"name": "sym", "path": str(link)}, {"name": "via", "path": str(viakink)},
           {"name": "prot", "path": str(protected)}, {"name": "shallow", "path": "/tmp"},
           {"name": "rel", "path": "relative/x"}, {"path": str(big)}, "junk", None, {"name": "nopath"}]
    res = cl.c2_candidates(c2_ctx(tmp_path, bad))
    assert res.plan["items"] == [] and res.metrics["protected"] == 1
    states = {i["name"]: i["state"] for i in res.items}
    assert states["sym"] == "refused: symlink" and states["via"] == "refused: symlink in path"
    assert states["prot"].startswith("protected") and states["shallow"] == "refused: too shallow"
    assert states["rel"] == "refused: bad candidate config" and states["?"] == "refused: bad candidate config"


def test_c2_in_use_candidates_are_flagged_and_refused_at_apply(tmp_path, monkeypatch):
    big, d, cands = cand_set(tmp_path)
    sh = c2_env(monkeypatch, mounts=f"/web|{d};/etc;\n")                 # a running container mounts the dir
    res = cl.c2_candidates(c2_ctx(tmp_path, cands))
    states = {i["name"]: i["state"] for i in res.items}
    assert states["alpha-dir"].startswith("in use: container") and states["zeta-file"] == "candidate"
    h = core.plan_hash(res.plan)
    (core.STATE_DIR / "approvals").mkdir(parents=True)
    (core.STATE_DIR / "approvals" / f"c2_candidates.{h}").write_text("1")
    cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
    assert d.exists() and not big.exists()
    # open files (lsof reports a pid) block too
    c2_env(monkeypatch, lsof_rc=0, lsof_out="p123\nf4\ntREG\nn/x\n")
    d.mkdir(exist_ok=True)
    (d / "x.bin").write_bytes(b"x" * (2 * MIB_ + 5))
    res = cl.c2_candidates(c2_ctx(tmp_path, cands[:2]))
    assert all("in use: open files" == i["state"] for i in res.items)


def test_c2_container_mount_overlap_rules():
    assert cl._overlap("/a/b", "/a/b") and cl._overlap("/a/b/c", "/a/b") and cl._overlap("/a", "/a/b")
    assert not cl._overlap("/a/bc", "/a/b") and not cl._overlap("/a/b", "/a/bc")


def test_c2_archive_flow_copies_verifies_then_removes_and_keeps_source_on_failed_verify(tmp_path, monkeypatch):
    import shutil
    src = tmp_path / "files" / "bundle"
    mkfile(src / "a.bin", size=2 * MIB_ + 1)
    arch = tmp_path / "cold"
    arch.mkdir()
    cands = [{"name": "bundle", "path": str(src), "why": "cold storage", "archive_to": str(arch)}]
    state = {"verify_clean": True}

    def rsync(cmd):
        if " -n " in cmd:                                             # verification pass
            return ok("" if state["verify_clean"] else ">f+++++++++ a.bin\n")
        a, b = cmd.split(" -- ")[1].split()
        shutil.copytree(a.rstrip("/"), b.rstrip("/"))
        return ok()

    sh = c2_env(monkeypatch, extra=(("rsync", rsync),))
    h = core.plan_hash(cl.c2_candidates(c2_ctx(tmp_path, cands)).plan)
    assert "rsync -aHSAX --" in cl.c2_candidates(c2_ctx(tmp_path, cands)).plan["items"][0]["command"]
    (core.STATE_DIR / "approvals").mkdir(parents=True)
    (core.STATE_DIR / "approvals" / f"c2_candidates.{h}").write_text("1")

    state["verify_clean"] = False
    res = cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
    assert src.exists() and res.status == "warn" and "verification" in res.summary
    assert (arch / "bundle" / "a.bin").exists()
    # a second attempt must not merge into the leftover archive copy
    res = cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
    assert src.exists() and res.status == "warn"
    shutil.rmtree(arch / "bundle")
    state["verify_clean"] = True
    res = cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
    assert not src.exists() and (arch / "bundle" / "a.bin").exists() and res.status == "ok"


def test_c2_archive_dir_missing_symlinked_or_protected_refuses(tmp_path, monkeypatch):
    src = tmp_path / "files" / "bundle.bin"
    src.parent.mkdir()
    src.write_bytes(b"x" * (2 * MIB_))
    c2_env(monkeypatch)
    for arch in (str(tmp_path / "no-such-dir"), "/mnt/backup/archive"):
        cands = [{"name": "bundle", "path": str(src), "archive_to": arch}]
        h = core.plan_hash(cl.c2_candidates(c2_ctx(tmp_path, cands)).plan)
        (core.STATE_DIR / "approvals").mkdir(parents=True, exist_ok=True)
        (core.STATE_DIR / "approvals" / f"c2_candidates.{h}").write_text("1")
        cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
        assert src.exists(), arch


def test_c2_docker_down_means_unverifiable_and_blocks_apply(tmp_path, monkeypatch):
    big, d, cands = cand_set(tmp_path)
    c2_env(monkeypatch, extra=())
    sh = use_sh(monkeypatch, ("docker ps -a -q", (1, "", "down")), ("du -sxb", lambda c: ok(f"{3 * MIB_}\tx")),
                ("lsof", (1, "", "")))
    res = cl.c2_candidates(c2_ctx(tmp_path, cands[:1]))
    h = core.plan_hash(res.plan)
    (core.STATE_DIR / "approvals").mkdir(parents=True)
    (core.STATE_DIR / "approvals" / f"c2_candidates.{h}").write_text("1")
    cl.c2_candidates(c2_ctx(tmp_path, cands[:1], apply=True))
    assert big.exists()


# =========================================================================== extra safety nets
def test_lsof_holders_ignore_directory_handles_but_count_cwd_exe_and_open_files():
    watchers = "p10\nf617\ntDIR\nn/x\nf718\ntDIR\nn/y\n"                    # editors/watchers: not "use"
    assert cl._lsof_holders(watchers) == []
    assert cl._lsof_holders("p11\nfcwd\ntDIR\nn/x\n") == ["11"]                  # a shell sitting in the dir
    assert cl._lsof_holders("p12\nftxt\ntREG\nn/x/bin\nfmem\ntREG\nn/x/lib.so\n") == ["12"]
    assert cl._lsof_holders("p13\nf4r\ntREG\nn/x/f\n") == ["13"]
    assert cl._lsof_holders("p14\nf9\ntFIFO\nn/x/pipe\np15\nf3w\ntREG\nn/x/z\n") == ["15"]
    assert cl._lsof_holders("") == []


def test_scan_does_not_cross_into_other_filesystems(tmp_path):
    root = tmp_path / "data" / "logs"
    mkfile(root / "top.log")
    mkfile(root / "mnt" / "inner.log")
    mkfile(root / "ok" / "inner.log")
    real_dev = os.lstat(root).st_dev
    ents, _, _ = cl._scan(str(root), ["**", "*.log"], dev=real_dev)
    assert sorted(e.rel for e in ents) == ["mnt/inner.log", "ok/inner.log", "top.log"]
    ents, _, _ = cl._scan(str(root), ["**", "*.log"], dev=real_dev + 1)             # every subdir "is a mount"
    assert [e.rel for e in ents] == ["top.log"]


def test_scan_limit_makes_the_rule_fail_closed(tmp_path, monkeypatch):
    logs = tmp_path / "data" / "logs"
    for i in range(6):
        mkfile(logs / f"f{i}.log", age_s=30 * DAY)
    monkeypatch.setattr(cl, "SCAN_LIMIT", 3)
    assert cl._scan(str(logs), ["*.log"])[2] is False
    res = cl.retention(retention_ctx(tmp_path, [rule(tmp_path, max_age_days=1)], apply=True))
    assert res.items[0]["state"] == "refused: scan limit reached" and len(present(logs)) == 6


def test_a_task_timeout_is_never_swallowed_by_act_or_gate_wrappers(tmp_path, monkeypatch):
    acts = cl._Acts(retention_ctx(tmp_path, [], apply=True))

    def boom():
        raise core._Timeout()
    with pytest.raises(core._Timeout):
        acts.run("x", "target-ok", 1, boom)
    import homelab_maint.tasks.gates as gates

    def gates_busy(name):
        raise core._Timeout()
    monkeypatch.setattr(gates, "busy", gates_busy)
    with pytest.raises(core._Timeout):
        REAL_BUSY("apt")


def test_busy_wrapper_fails_closed_on_any_gate_error(monkeypatch):
    import homelab_maint.tasks.gates as gates
    monkeypatch.setattr(gates, "busy", lambda n: (_ for _ in ()).throw(ValueError("x")))
    assert REAL_BUSY("apt")[0] is True
    monkeypatch.setattr(gates, "busy", lambda n: (False, "idle"))
    assert REAL_BUSY("apt") == (False, "idle")


def test_docker_images_soak_clock_starts_even_while_the_ledger_is_unusable(tmp_path, monkeypatch):
    imgs = [(iid(1), "a/b", "1", 0, "10MB")]
    images_env(monkeypatch, tmp_path, imgs, ledger=False, unref_age_days=None)
    ctx = mk("docker_images", apply=True)
    res = cl.docker_images(ctx)
    assert res.status == "skipped" and ctx.state["unref_since"] == {iid(1): NOW}


def test_caps_protected_container_is_reported_as_protected_even_without_history(monkeypatch):
    sh = caps_env(monkeypatch, running=("tunarr-host-net",))
    res = cl.caps(caps_ctx(apply=True, ceilings={"tunarr-host-net": 40}))
    assert res.items[0]["state"] == "protected: never capped" and res.metrics["protected"] == 1
    assert not any(c.startswith("docker inspect") for c in sh.calls) and sh.mutating() == []


def test_c2_apply_refuses_items_whose_size_could_not_be_measured(tmp_path, monkeypatch):
    big, d, cands = cand_set(tmp_path)
    sh = c2_env(monkeypatch)
    sh.rows.insert(0, ("du -sxb", (124, "", "timeout")))             # du timed out: size falls back to lstat
    res = cl.c2_candidates(c2_ctx(tmp_path, cands[:1]))
    assert res.plan["items"][0]["size_exact"] is False and res.items[0]["size"].endswith("?")
    h = core.plan_hash(res.plan)
    (core.STATE_DIR / "approvals").mkdir(parents=True)
    (core.STATE_DIR / "approvals" / f"c2_candidates.{h}").write_text("1")
    cl.c2_candidates(c2_ctx(tmp_path, cands[:1], apply=True))
    assert big.exists()


def test_c2_plan_hash_does_not_depend_on_volatile_probe_results(tmp_path, monkeypatch):
    big, d, cands = cand_set(tmp_path)
    c2_env(monkeypatch, lsof_rc=1)
    h1 = core.plan_hash(cl.c2_candidates(c2_ctx(tmp_path, cands)).plan)
    c2_env(monkeypatch, lsof_rc=0, lsof_out="p1\nfcwd\ntDIR\nn/x\n", mounts=f"/web|{d};\n")   # now "in use"
    h2 = core.plan_hash(cl.c2_candidates(c2_ctx(tmp_path, cands)).plan)
    assert h1 == h2


# =========================================================================== through the real runner / CLI
def write_conf(tmp_path, body):
    (tmp_path / "conf" / "maint.toml").write_text(body)
    (tmp_path / "conf" / "protected.toml").write_text('patterns = ["immich"]\n')


def test_run_task_applies_retention_only_in_apply_mode(tmp_path):
    logs = tmp_path / "data" / "logs"
    mkfile(logs / "old.log", age_s=30 * DAY, now=time.time())
    write_conf(tmp_path, f'[tasks.retention]\nmode = "report"\nallowed_roots = ["{tmp_path}/data"]\n'
                         f'rules = [{{name = "r", path = "{logs}", glob = "*.log", max_age_days = 3}}]\n')
    t = core.REGISTRY["retention"]
    res, _ = core.run_task(t, core.load_config(), apply=True)            # --apply, but mode = report
    assert present(logs) == ["old.log"] and res.status == "info"
    write_conf(tmp_path, (tmp_path / "conf" / "maint.toml").read_text().replace('"report"', '"apply"'))
    res, _ = core.run_task(t, core.load_config(), apply=False)           # mode = apply, but --dry-run
    assert present(logs) == ["old.log"]
    res, _ = core.run_task(t, core.load_config(), apply=True)
    assert present(logs) == [] and res.reclaimed_bytes == 10 and res.status == "ok"
    ascii_ok(res)


def test_run_task_survives_a_cap_hit_as_a_normal_result(tmp_path):
    logs = tmp_path / "data" / "logs"
    for i in range(4):
        mkfile(logs / f"f{i}.log", age_s=30 * DAY, now=time.time())
    write_conf(tmp_path, f'[tasks.retention]\nmode = "apply"\nmax_items_per_run = 1\nallowed_roots = ["{tmp_path}/data"]\n'
                         f'rules = [{{name = "r", path = "{logs}", glob = "*.log", max_age_days = 3}}]\n')
    res, _ = core.run_task(core.REGISTRY["retention"], core.load_config(), apply=True)
    assert len(present(logs)) == 3 and res.status == "info" and res.reclaimed_bytes == 10


def test_cli_approve_flow_end_to_end_for_c2(tmp_path, monkeypatch, capsys):
    import argparse
    from homelab_maint import cli
    big, d, cands = cand_set(tmp_path)
    c2_env(monkeypatch)
    write_conf(tmp_path, '[tasks.c2_candidates]\nmode = "report"\ncandidates = [\n'
                         f'  {{name = "zeta-file", path = "{big}", why = "stale"}},\n'
                         f'  {{name = "alpha-dir", path = "{d}", why = "old", needs_manual_check = true}},\n]\n')
    monkeypatch.setattr(cli, "STATE_DIR", core.STATE_DIR)
    plan_res, _ = core.run_task(core.REGISTRY["c2_candidates"], core.load_config(), apply=False)
    h = core.plan_hash(plan_res.plan)
    assert cli.cmd_approve(argparse.Namespace(task="c2_candidates", hash="000000000000")) == 4   # wrong hash
    assert big.exists() and d.exists() and not (core.STATE_DIR / "approvals").exists()
    assert cli.cmd_approve(argparse.Namespace(task="c2_candidates", hash=h)) == 0
    assert not big.exists() and d.exists()                    # manual-check item still refused
    assert "freed" in capsys.readouterr().out


def test_remove_path_refuses_symlinked_ancestors_and_symlink_candidates(tmp_path):
    real = tmp_path / "real"
    mkfile(real / "f.bin")
    (tmp_path / "alias").symlink_to(real)
    with pytest.raises(RuntimeError):
        cl._remove_path(str(tmp_path / "alias" / "f.bin"))
    with pytest.raises(RuntimeError):
        cl._remove_path(str(tmp_path / "alias"))
    assert (real / "f.bin").exists()
    cl._remove_path(str(real / "f.bin"))
    assert not (real / "f.bin").exists()


# =========================================================================== review fixes (round 2)
# ---- c2: the in-use checks must fail closed, with a fresh view, at the last moment
def approve_plan(tmp_path, cands, **opts):
    h = core.plan_hash(cl.c2_candidates(c2_ctx(tmp_path, cands, **opts)).plan)
    ap = core.STATE_DIR / "approvals"
    ap.mkdir(parents=True, exist_ok=True)
    (ap / f"c2_candidates.{h}").write_text("1")
    return h


@pytest.mark.parametrize("rc", [124, 127, 2])
def test_c2_apply_keeps_the_data_when_lsof_times_out_is_missing_or_fails(tmp_path, monkeypatch, rc):
    big, d, cands = cand_set(tmp_path)
    c2_env(monkeypatch, lsof_rc=rc)                       # 124 = timeout, 127 = not installed
    plan = cl.c2_candidates(c2_ctx(tmp_path, cands))
    assert {i["state"] for i in plan.items} == {"unverified (open-file check incomplete)"}
    approve_plan(tmp_path, cands)
    res = cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
    assert big.exists() and d.exists() and res.reclaimed_bytes == 0
    assert all("open-file check incomplete" in i["name"] for i in res.items) and "refused" in res.summary


def test_c2_apply_keeps_the_data_when_lsof_prints_errors_or_the_run_is_not_root(tmp_path, monkeypatch):
    big, d, cands = cand_set(tmp_path)
    c2_env(monkeypatch)
    sh = cl.sh
    sh.rows.insert(0, ("lsof", (1, "", "lsof: status error on /x: Permission denied")))
    approve_plan(tmp_path, cands)
    cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
    assert big.exists() and d.exists()
    # silent lsof but not root: it cannot see other users' processes, so silence proves nothing
    c2_env(monkeypatch)
    monkeypatch.setattr(cl, "_euid", lambda: 1000)
    assert {i["state"] for i in cl.c2_candidates(c2_ctx(tmp_path, cands)).items} == \
        {"unverified (open-file check incomplete)"}


def test_c2_apply_needs_root_and_says_so(tmp_path, monkeypatch):
    big, d, cands = cand_set(tmp_path)
    c2_env(monkeypatch)
    h = approve_plan(tmp_path, cands)
    monkeypatch.setattr(cl, "_euid", lambda: 1000)
    res = cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
    assert big.exists() and d.exists() and res.status == "warn" and "needs root" in res.summary
    assert res.plan is not None and res.alert is False and "refused-not-root" in outcomes(tmp_path)
    assert (core.STATE_DIR / "approvals" / f"c2_candidates.{h}").exists()       # not consumed: nothing was done
    ascii_ok(res)


def test_c2_apply_refuses_when_lsof_finds_a_holder_at_apply_time(tmp_path, monkeypatch):
    big, d, cands = cand_set(tmp_path)
    sh = c2_env(monkeypatch)                              # at plan time nothing holds the files
    plan = cl.c2_candidates(c2_ctx(tmp_path, cands))
    assert {i["state"] for i in plan.items} == {"candidate"}
    approve_plan(tmp_path, cands)
    sh.rows.insert(0, ("lsof", (0, "p4242\nfcwd\ntDIR\nn/x\n", "")))             # a shell cd'd in, a process opened it
    res = cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
    assert big.exists() and d.exists() and res.reclaimed_bytes == 0
    assert all("open files" in i["name"] for i in res.items)
    sh.rows.insert(0, ("lsof", (0, "p77\nf5\ntREG\nn/x/big\n", "")))
    cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
    assert big.exists() and d.exists()


def test_c2_stopped_containers_count_as_users_of_a_candidate(tmp_path, monkeypatch):
    big, d, cands = cand_set(tmp_path)
    sh = c2_env(monkeypatch, mounts=f"/comfy|{d};\n")     # `docker ps -a` lists it; `docker ps` (running only) would not
    states = {i["name"]: i["state"] for i in cl.c2_candidates(c2_ctx(tmp_path, cands)).items}
    assert states["alpha-dir"] == "in use: container comfy"
    assert any(c.startswith("docker ps -a -q") for c in sh.calls)
    assert not any(c.startswith("docker ps -q") for c in sh.calls)
    approve_plan(tmp_path, cands)
    cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
    assert d.exists() and not big.exists()


def test_c2_mounts_through_symlinks_are_recognised(tmp_path, monkeypatch):
    big, d, cands = cand_set(tmp_path)
    (tmp_path / "alias-dir").symlink_to(d)                          # a source that is a symlink to the candidate dir
    (tmp_path / "alias-parent").symlink_to(big.parent)              # ... and one to its parent directory
    for src in (tmp_path / "alias-dir", tmp_path / "alias-parent"):
        c2_env(monkeypatch, mounts=f"/web|{src};\n")
        assert os.path.realpath(src) in cl._container_mounts()["web"] and str(src) in cl._container_mounts()["web"]
        states = {i["name"]: i["state"] for i in cl.c2_candidates(c2_ctx(tmp_path, cands)).items}
        assert states["alpha-dir"].startswith("in use: container web"), src
    # a mount of "/" itself is not treated as 'uses everything'; sources that are not absolute paths are dropped
    c2_env(monkeypatch, mounts="/web|/;volume-name;\n")
    assert cl._container_mounts() == {"web": ["/"]}


def test_c2_container_mounts_are_read_again_at_apply_not_reused_from_planning(tmp_path, monkeypatch):
    big, d, cands = cand_set(tmp_path)
    sh = c2_env(monkeypatch, mounts="seed")                         # ps lists one container
    approve_plan(tmp_path, cands)
    seen = {"n": 0}

    def inspect(cmd):
        seen["n"] += 1
        return ok(f"/web|{d};\n" if seen["n"] > 2 else "/web|/etc;\n")    # 2 candidates planned first, then apply
    sh.rows.insert(0, ("docker inspect --format", inspect))
    cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
    assert d.exists() and not big.exists() and seen["n"] >= 4       # the dir got a container only after planning


def test_c2_archive_rechecks_for_users_after_the_long_copy(tmp_path, monkeypatch):
    import shutil
    src = tmp_path / "files" / "bundle"
    mkfile(src / "a.bin", size=2 * MIB_ + 1)
    arch = tmp_path / "cold"
    arch.mkdir()
    cands = [{"name": "bundle", "path": str(src), "archive_to": str(arch)}]
    state = {"copied": False}

    def rsync(cmd):
        if " -n " not in cmd:
            a, b = cmd.split(" -- ")[1].split()
            shutil.copytree(a.rstrip("/"), b.rstrip("/"))
            state["copied"] = True                                  # ... and now somebody opens the source
        return ok()

    c2_env(monkeypatch, extra=(("rsync", rsync),))
    cl.sh.rows.insert(0, ("lsof", lambda c: (0, "p9\nfcwd\ntDIR\nn/x\n", "") if state["copied"] else (1, "", "")))
    approve_plan(tmp_path, cands)
    state["copied"] = False
    res = cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
    assert src.exists() and (arch / "bundle" / "a.bin").exists()    # source kept, verified copy left behind
    assert res.metrics["gone"] == 1 and res.metrics["failed"] == 0 and res.status == "ok"
    assert any("in use after copy" in a["outcome"] for a in audit_rows(tmp_path))


def test_c2_present_but_unusable_archive_to_never_degrades_to_a_plain_delete(tmp_path, monkeypatch):
    big, d, cands = cand_set(tmp_path)
    c2_env(monkeypatch)
    for bad in ("cold", "./cold", "", 5, ["x"], True):
        one = [{"name": "zeta-file", "path": str(big), "archive_to": bad}]
        res = cl.c2_candidates(c2_ctx(tmp_path, one))
        assert res.plan["items"] == [] and "archive_to must be an absolute path" in res.items[0]["state"], bad
        approve_plan(tmp_path, one)
        cl.c2_candidates(c2_ctx(tmp_path, one, apply=True))
        assert big.exists(), bad


# ---- c2: the archive target and the copy
def _real_archive(src, dest_root, is_dir, **kw):
    """_archive_and_remove with the real cold-disk check restored."""
    import unittest.mock as um
    with um.patch.object(cl, "_archive_target_problem", REAL_ARCHIVE_PROBLEM):
        return cl._archive_and_remove(src, dest_root, is_dir, **kw)


def tempfile_dir(parent):
    import tempfile
    return tempfile.mkdtemp(prefix="hm-cold-", dir=parent)


def shutil_rmtree(p):
    import shutil
    shutil.rmtree(p, ignore_errors=True)



def test_archive_target_problem_checks(tmp_path):
    shm = "/dev/shm"
    if not os.path.isdir(shm) or os.stat(shm).st_dev == os.stat("/").st_dev:
        pytest.skip("needs a second filesystem (/dev/shm)")
    src = mkfile(tmp_path / "src" / "f.bin")
    cold, other = tempfile_dir(shm), tempfile_dir(shm)
    try:
        chk = REAL_ARCHIVE_PROBLEM
        if os.stat(src).st_dev == os.stat("/").st_dev:
            assert chk(cold, str(src)) == ""                                      # a different disk: fine
        assert "absolute" in chk("cold", str(src)) and "absolute" in chk("", str(src))
        assert "missing" in chk(cold + "/nope", str(src))
        link = tmp_path / "linkcold"
        link.symlink_to(cold)
        assert "symlink" in chk(str(link), str(src))
        (Path(cold) / "plain").write_text("x")
        assert "not a directory" in chk(str(Path(cold) / "plain"), str(src))
        Path(other, "x").write_text("1")
        assert "same filesystem as the source" in chk(cold, os.path.join(other, "x"))
    finally:
        shutil_rmtree(cold)
        shutil_rmtree(other)


def test_archive_dir_on_the_root_filesystem_is_refused_for_real(tmp_path):
    """A stub 'archive' dir left on / after the cold disk was unmounted must not be filled."""
    if os.stat(tmp_path).st_dev != os.stat("/").st_dev:
        pytest.skip("tmp_path is not on the root filesystem here")
    src = mkfile(tmp_path / "src" / "f.bin")
    stub = tmp_path / "media" / "cold" / "archive"
    stub.mkdir(parents=True)
    assert "root filesystem" in REAL_ARCHIVE_PROBLEM(str(stub), str(src))
    with pytest.raises(RuntimeError, match="archive target refused"):
        _real_archive(str(src), str(stub), False)
    assert src.exists() and list(stub.iterdir()) == []


def test_archive_never_merges_into_an_existing_destination(tmp_path, monkeypatch):
    src = mkfile(tmp_path / "s" / "bundle.bin", size=64)
    arch = tmp_path / "cold"
    (arch / "bundle.bin").parent.mkdir(parents=True)
    (arch / "bundle.bin").write_bytes(b"older copy")
    sh = use_sh(monkeypatch, ("rsync", ok()))                 # an rsync that would happily merge/overwrite
    with pytest.raises(RuntimeError, match="not overwriting"):
        cl._archive_and_remove(str(src), str(arch), False)
    assert src.exists() and not any(c.startswith("rsync") for c in sh.calls)
    d = mkfile(tmp_path / "s2" / "tree" / "x.bin", size=8).parent
    (arch / "tree").mkdir()                                    # an existing directory of the same name
    with pytest.raises(RuntimeError, match="not overwriting"):
        cl._archive_and_remove(str(d), str(arch), True)
    assert d.exists() and not any(c.startswith("rsync") for c in sh.calls)


def test_c2_archive_refuses_when_the_archive_disk_is_too_small(tmp_path, monkeypatch):
    src = tmp_path / "files" / "bundle.bin"
    src.parent.mkdir()
    src.write_bytes(b"x" * (4 * MIB_))
    arch = tmp_path / "cold"
    arch.mkdir()
    cands = [{"name": "bundle", "path": str(src), "archive_to": str(arch)}]
    sh = c2_env(monkeypatch, extra=(("rsync", ok()),))
    approve_plan(tmp_path, cands)
    free = {"v": int(4 * MIB_ * 1.05) - 1}                    # one byte short of size x 1.05
    monkeypatch.setattr(cl, "_free_bytes", lambda p: free["v"])
    res = cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
    assert src.exists() and not any(c.startswith("rsync") for c in sh.calls)
    assert "too small" in res.items[0]["name"]
    free["v"] += 1                                             # exactly enough: it proceeds (rsync is a no-op here, so
    cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))      # the copy is missing and verification must refuse)
    assert src.exists()


def test_free_bytes_reads_the_real_device_and_fails_to_zero():
    assert cl._free_bytes("/") > 0
    assert cl._free_bytes("/definitely/not/here") == 0


needs_rsync = pytest.mark.skipif(__import__("shutil").which("rsync") is None, reason="rsync not installed")


def real_sh(after_copy=None):
    """`sh` that really runs rsync (everything else is not found). after_copy(cmd) runs after a non-dry rsync."""
    def run(cmd, timeout=60, **kw):
        if cmd[0] != "rsync":
            return subprocess.CompletedProcess(cmd, 127, "", "unmocked")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if after_copy and "-n" not in cmd:
            after_copy(cmd)
        return r
    return run


@needs_rsync
def test_archive_with_real_rsync_keeps_sparse_files_and_hardlinks_then_removes_the_source(tmp_path, monkeypatch):
    src = tmp_path / "tree"
    src.mkdir()
    with open(src / "sparse.img", "wb") as f:
        f.truncate(64 * MIB_)
        f.seek(64 * MIB_ - 1)
        f.write(b"z")
    (src / "one.bin").write_bytes(b"hardlinked" * 100)
    os.link(src / "one.bin", src / "two.bin")
    (src / "sub").mkdir()
    (src / "sub" / "n.txt").write_text("nested")
    assert os.stat(src / "sparse.img").st_blocks * 512 < 1 * MIB_
    arch = tmp_path / "cold"
    arch.mkdir()
    monkeypatch.setattr(cl, "sh", real_sh())
    cl._archive_and_remove(str(src), str(arch), True)
    out = arch / "tree"
    assert not src.exists() and (out / "sub" / "n.txt").read_text() == "nested"
    st = os.stat(out / "sparse.img")
    assert st.st_size == 64 * MIB_ and st.st_blocks * 512 < 8 * MIB_            # still sparse: no 64 MiB of real blocks
    a, b = os.stat(out / "one.bin"), os.stat(out / "two.bin")
    assert a.st_ino == b.st_ino and a.st_nlink == 2                              # hardlink pair preserved


@needs_rsync
def test_archive_verification_is_a_checksum_so_a_same_size_same_mtime_corruption_is_caught(tmp_path, monkeypatch):
    src = tmp_path / "s" / "model.bin"
    src.parent.mkdir()
    src.write_bytes(b"A" * 4096)
    arch = tmp_path / "cold"
    arch.mkdir()

    def corrupt(cmd):                                           # same size, same mtime, different bytes
        dest = arch / "model.bin"
        st = os.stat(dest)
        dest.write_bytes(b"B" * 4096)
        os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
    monkeypatch.setattr(cl, "sh", real_sh(after_copy=corrupt))
    with pytest.raises(RuntimeError, match="verification failed"):
        cl._archive_and_remove(str(src), str(arch), False)
    assert src.exists() and src.read_bytes() == b"A" * 4096
    calls = []
    monkeypatch.setattr(cl, "sh", lambda cmd, timeout=60, **kw: calls.append(cmd) or real_sh()(cmd, timeout))
    shutil_rmtree(arch / "model.bin")
    (arch / "model.bin").unlink(missing_ok=True)
    cl._archive_and_remove(str(src), str(arch), False)                         # clean copy: verified, source removed
    assert not src.exists() and (arch / "model.bin").read_bytes() == b"A" * 4096
    assert calls[0][:2] == ["rsync", "-aHSAX"] and all(f in calls[1] for f in ("-n", "-c", "--itemize-changes"))


# ---- retention / docker_images: benign races are not failures; stuck items do not starve the queue
def _racing_remove(monkeypatch, n, how):
    real, seen = cl._remove_entry, [0]

    def wrapped(root, ent):
        seen[0] += 1
        if seen[0] <= n:
            if how == "gone":
                os.unlink(ent.path)                                  # another process got there first
            else:
                os.utime(ent.path, None)                             # ... or touched it: it changed since the scan
        return real(root, ent)
    monkeypatch.setattr(cl, "_remove_entry", wrapped)


@pytest.mark.parametrize("how", ["gone", "changed"])
def test_files_that_vanish_or_change_after_the_scan_are_skipped_not_failures(tmp_path, monkeypatch, how):
    logs = tmp_path / "data" / "logs"
    for i in range(8):
        mkfile(logs / f"f{i}.log", age_s=(30 + i) * DAY)
    _racing_remove(monkeypatch, 3, how)
    res = cl.retention(retention_ctx(tmp_path, [rule(tmp_path, max_age_days=1)], apply=True))
    left = present(logs)
    assert len(left) == (0 if how == "gone" else 3)                          # the 3 touched files are kept, rest went
    assert res.metrics["failed"] == 0 and res.metrics["gone"] == 3 and res.status == "ok"
    assert "vanished/changed" in res.summary and "failed" not in res.summary and res.metrics["deferred"] == 0


def _stuck_images(monkeypatch, tmp_path, stuck=(1, 2, 3), total=6):
    imgs = [(iid(i), f"x/n{i}", "1", 0, "10MB") for i in range(1, total + 1)]
    sh = images_env(monkeypatch, tmp_path, imgs)
    for i in stuck:
        sh.rows.insert(0, (f"docker image rm {iid(i)}", (1, "", "conflict: image is being used")))
    return sh


def test_persistently_failing_head_of_queue_images_are_backed_off_so_the_queue_advances(tmp_path, monkeypatch):
    sh = _stuck_images(monkeypatch, tmp_path)
    c1 = mk("docker_images", apply=True)
    r1 = cl.docker_images(c1)
    assert len(sh.mutating()) == 3 and r1.status == "warn" and r1.metrics["failed"] == 3     # the 3-failure halt stays
    c1.save_state()
    sh.calls.clear()
    c2 = mk("docker_images", apply=True, now=NOW + DAY)
    r2 = cl.docker_images(c2)
    assert sh.mutating() == [f"docker image rm {iid(i)}" for i in (4, 5, 6)]                  # queue moved on
    assert r2.status == "ok" and r2.metrics["backoff"] == 3 and r2.metrics["failed"] == 0
    assert "in retry backoff" in r2.summary and r2.reclaimed_bytes == 30_000_000
    c2.save_state()
    sh.calls.clear()
    led = core.read_json(core.STATE_DIR / "ledger" / "images.json")
    led["updated"] = NOW + 4 * DAY - 600                                                      # the ledger is kept fresh
    core.write_json_atomic(core.STATE_DIR / "ledger" / "images.json", led)
    c3 = mk("docker_images", apply=True, now=NOW + 4 * DAY)                                   # backoff (3 d) expired
    cl.docker_images(c3)
    assert [c for c in sh.mutating() if c.endswith(iid(1))] == [f"docker image rm {iid(1)}"]


def test_backoff_is_honoured_by_dry_run_so_the_report_matches_what_apply_would_do(tmp_path, monkeypatch):
    _stuck_images(monkeypatch, tmp_path)
    c1 = mk("docker_images", apply=True)
    cl.docker_images(c1)
    c1.save_state()
    dry = cl.docker_images(mk("docker_images", now=NOW + DAY))
    assert dry.metrics["selected"] == 3 and dry.metrics["backoff"] == 3 and dry.status == "info"
    assert {a["target"] for a in audit_rows(tmp_path) if a["outcome"] == "dry-run"} == {f"x/n{i}:1" for i in (4, 5, 6)}


def test_retry_after_days_is_configurable_and_zero_means_retry_next_run(tmp_path, monkeypatch):
    sh = _stuck_images(monkeypatch, tmp_path)
    c1 = mk("docker_images", apply=True, retry_after_days=0)
    cl.docker_images(c1)
    c1.save_state()
    sh.calls.clear()
    cl.docker_images(mk("docker_images", apply=True, now=NOW + DAY, retry_after_days=0))
    assert [c for c in sh.mutating() if c.endswith(iid(1))] == [f"docker image rm {iid(1)}"]   # head of queue again


def test_a_malformed_backoff_state_is_ignored(tmp_path):
    for junk in ("junk", [1], {"a|b": "soon", "c": None, 5: 1}, None):
        c = mk("docker_images", apply=True)
        c.state["fail_until"] = junk
        assert cl._Acts(c)._fail_until == {} and c.state["fail_until"] == {}


def test_backoff_table_is_bounded_and_expired_entries_are_dropped(tmp_path):
    c = mk("trash", apply=True)
    c.state["fail_until"] = {"fresh": NOW + 100, "old": NOW - 1, "now": NOW}                # expiry == now: expired
    assert cl._Acts(c)._fail_until == {"fresh": NOW + 100}
    c.state["fail_until"] = {f"k{i}": NOW + 100 + i for i in range(1500)}
    a = cl._Acts(c)
    assert len(a._fail_until) == 1000 and "k0" not in a._fail_until and "k1499" in a._fail_until   # newest kept


# ---- trash: the payload inode re-check
@pytest.mark.parametrize("kind", ["file", "dir"])
def test_trash_payload_replaced_between_scan_and_delete_is_left_alone(tmp_path, monkeypatch, kind):
    t = fake_trash(tmp_path, monkeypatch)
    put(t, "victim", 90, payload=kind)
    payload = t / "files" / "victim"
    real_run = cl._Acts.run

    def swap_then_run(self, *a, **kw):                             # between the scan and the delete
        if kind == "file":
            fresh = t / "files" / "victim.new"
            fresh.write_bytes(b"NEW PRECIOUS DATA")
            os.replace(fresh, payload)                              # new inode while the old one still exists
        else:
            os.rename(payload, t / "files" / "aside")
            payload.mkdir()
            (payload / "precious.txt").write_text("NEW PRECIOUS DATA")
        return real_run(self, *a, **kw)
    monkeypatch.setattr(cl._Acts, "run", swap_then_run)
    res = cl.trash(mk("trash", apply=True, users=["ohmz"], max_age_days=30))
    assert (payload.read_bytes() if kind == "file" else (payload / "precious.txt").read_bytes()) == b"NEW PRECIOUS DATA"
    assert (t / "info" / "victim.trashinfo").exists()                # info kept: the entry was not trashed by us
    assert res.metrics["gone"] == 1 and res.metrics["failed"] == 0 and res.reclaimed_bytes == 0
    assert any(r["outcome"] == "failed: payload changed since scan" for r in audit_rows(tmp_path))


# ---- caps: a ceiling needs a trustworthy history
def test_caps_refuses_eight_samples_that_span_only_two_hours(monkeypatch):
    seed_samples("kavita", 2, n=8, span_h=2)                         # 8 samples = the old minimum, but ~2 h of data
    sh = caps_env(monkeypatch)
    res = cl.caps(caps_ctx(apply=True))
    assert sh.mutating() == [] and res.metrics["refused"] == 1
    assert res.items[0]["state"] == "refused: 2 h of history, need 72"
    ascii_ok(res)
    seed_samples("kavita", 2, n=8, span_h=80)                        # now there are also 80 h of samples
    res = cl.caps(caps_ctx(apply=True))
    assert len(sh.mutating()) == 1 and res.metrics["refused"] == 0


def test_caps_span_threshold_is_configurable_and_bad_values_fail_closed(monkeypatch):
    seed_samples("kavita", 2, n=10, span_h=30)
    sh = caps_env(monkeypatch)
    assert cl.caps(caps_ctx(apply=True)).metrics["refused"] == 1 and sh.mutating() == []        # 30 h < 72 h default
    assert cl.caps(caps_ctx(apply=True, min_span_hours=24)).metrics["refused"] == 0
    assert len(sh.mutating()) == 1
    for bad in ("x", -1, True, None, float("nan")):
        sh = caps_env(monkeypatch)
        assert cl.caps(caps_ctx(apply=True, min_span_hours=bad)).status == "skipped", bad
        assert sh.mutating() == []
    for bad in (0, "1", -3):
        assert cl.caps(caps_ctx(apply=True, max_sample_age_hours=bad)).status == "skipped", bad


def test_caps_sampler_memory_peak_field_raises_the_floor(monkeypatch):
    hist = core.STATE_DIR / "history.jsonl"
    seed_samples("kavita", 2, peak=18 * GIB)                         # point samples say 2 GiB, the cgroup peak says 18
    sh = caps_env(monkeypatch)
    res = cl.caps(caps_ctx(apply=True))
    assert sh.mutating() == [] and "1.25x peak 18.0 GiB" in res.items[0]["state"]     # 22.5 GiB > 20 GiB ceiling
    hist.unlink()
    seed_samples("kavita", 2, peak=10 * GIB)                         # 12.5 GiB <= 20 GiB ceiling
    cl.caps(caps_ctx(apply=True))
    assert len(sh.mutating()) == 1
    hist.unlink()
    seed_samples("kavita", 2, peak="lots")                           # a junk peak is ignored, not trusted or fatal
    sh = caps_env(monkeypatch)
    cl.caps(caps_ctx(apply=True))
    assert len(sh.mutating()) == 1


def test_caps_refuses_when_the_sampler_has_gone_quiet(monkeypatch):
    seed_samples("kavita", 2, newest_age_s=5 * 3600)                 # the newest sample is 5 h old: sampler dead?
    sh = caps_env(monkeypatch)
    res = cl.caps(caps_ctx(apply=True))
    assert sh.mutating() == [] and res.items[0]["state"].startswith("refused: sampler silent for 5.0 h")
    cl.caps(caps_ctx(apply=True, max_sample_age_hours=6))            # explicitly tolerated
    assert len(sh.mutating()) == 1


def test_c2_users_of_resolves_a_candidate_reached_through_a_symlink(tmp_path):
    big, d, _ = cand_set(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(d)
    assert cl._users_of(str(alias), {"web": [str(d)]}, True, 5)[0] == ["web"]


def test_c2_archive_problems_are_a_refusal_not_a_failure_and_never_reach_rsync(tmp_path, monkeypatch):
    import unittest.mock as um
    if os.stat(tmp_path).st_dev != os.stat("/").st_dev:
        pytest.skip("tmp_path is not on the root filesystem here")
    src = tmp_path / "files" / "bundle.bin"
    src.parent.mkdir()
    src.write_bytes(b"x" * (2 * MIB_))
    stub = tmp_path / "media" / "cold" / "archive"                  # the cold disk is not mounted: a stub dir on /
    stub.mkdir(parents=True)
    cands = [{"name": "bundle", "path": str(src), "archive_to": str(stub)}]
    sh = c2_env(monkeypatch, extra=(("rsync", ok()),))
    with um.patch.object(cl, "_archive_target_problem", REAL_ARCHIVE_PROBLEM):
        plan = cl.c2_candidates(c2_ctx(tmp_path, cands))
        assert "root filesystem" in plan.items[0]["archive"] and plan.items[0]["state"] == "candidate"
        approve_plan(tmp_path, cands)
        res = cl.c2_candidates(c2_ctx(tmp_path, cands, apply=True))
    assert src.exists() and list(stub.iterdir()) == [] and not any(c.startswith("rsync") for c in sh.calls)
    assert res.metrics["failed"] == 0 and "root filesystem" in res.items[0]["name"]


def test_c2_apply_refuses_a_candidate_that_turned_into_a_symlink(tmp_path):
    victim = tmp_path / "elsewhere" / "precious.bin"
    victim.parent.mkdir()
    victim.write_bytes(b"x")
    link = tmp_path / "files" / "cand"
    link.parent.mkdir()
    link.symlink_to(victim)
    item = {"name": "cand", "path": str(link), "why": "", "bytes": 1, "size_exact": True, "mtime": "", "archive_to": None,
            "needs_manual_check": False, "command": ""}
    res = cl._apply_plan(c2_ctx(tmp_path, [], apply=True), core.Result(plan={"items": [item]}), [item], "h")
    assert victim.exists() and link.is_symlink() and "symlink" in res.items[0]["name"] and res.metrics["failed"] == 0
