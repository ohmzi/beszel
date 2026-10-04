"""Tests for scaffold.py: the task/job/probe generators and the secure, isolated plugin discovery.

Plugins are real files in tmp dirs. The test user is not root, so "owned by root" is exercised by pointing os.geteuid at 0 (then
only uid 0 is trusted and files owned by this user are refused). Every test restores core.REGISTRY and the loader's memory.
"""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import os
import signal
import stat
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

from homelab_maint import core, probes, scaffold

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    from homelab_maint import cli
    cli.load_tasks()
    saved, mods = dict(core.REGISTRY), set(sys.modules)
    scaffold.reset_loaded()
    monkeypatch.setattr(core, "sh", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))     # ctx.act audits through `logger`
    yield
    core.REGISTRY.clear()
    core.REGISTRY.update(saved)
    for m in set(sys.modules) - mods:
        if m.startswith("homelab_plugin_"):
            del sys.modules[m]
    scaffold.reset_loaded()


@pytest.fixture
def pdir(tmp_path):
    d = tmp_path / "plugins.d"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    return d


def plug(d: Path, name: str, body: str, mode: int = 0o644) -> Path:
    p = d / name
    p.write_text(textwrap.dedent(body))
    os.chmod(p, mode)
    return p


GOOD = '''
    from homelab_maint.core import Ctx, Result, task

    @task("{name}", klass="C0", tier="check", title="Good {name}", timeout=30)
    def run(ctx: Ctx) -> Result:
        return Result("ok", "{name} is fine")
'''


def good(d, stem, task=None):
    return plug(d, f"{stem}.py", GOOD.format(name=task or stem))


def load(d, **kw):
    kw.setdefault("check_ancestors", False)
    return scaffold.load_plugins(d, **kw)


# =========================================================================== generators
@pytest.mark.parametrize("klass", scaffold.KLASSES)
@pytest.mark.parametrize("tier", scaffold.TIERS)
def test_every_task_template_compiles_and_has_no_leftover_placeholders(klass, tier):
    src = scaffold.render_task("my_check", klass, tier, "My check")
    test = scaffold.render_task_test("my_check", klass, tier)
    for text in (src, test):
        compile(text, "x.py", "exec")
        assert "$" not in text
    assert f'klass="{klass}", tier="{tier}"' in src and "/etc/homelab-maint/plugins.d/my_check.py" in src
    assert f'("{klass}", "{tier}")' in test


def test_task_templates_teach_the_safety_contract():
    c0, c1, c2 = (scaffold.render_task("t_x", k) for k in scaffold.KLASSES)
    assert "forces apply off" in c0 and "fail closed" in c0
    assert "ctx.act(" in c1 and 'mode = "apply"' in c1 and "selects NOTHING" in c1 and "protect_names" in c1
    assert "Result(plan=" in c2 or "plan=plan" in c2
    assert "core.approved(" in c2 and "homelab-maint approve" in c2
    assert all("140" in t for t in (c0, c1, c2))


@pytest.mark.parametrize("bad", ["", "A", "Bad", "1abc", "has-dash", "../x", "a/b", "x" * 41, "a b", 'q"uote', "x;y", "é", None, 5])
def test_task_names_are_validated(bad):
    with pytest.raises(ValueError):
        scaffold.render_task(bad)


@pytest.mark.parametrize("bad", ["", "-x", "A", "a/b", "../x", "x" * 49, "a b", 'a"b', "a\nb"])
def test_job_and_probe_names_are_validated(bad):
    with pytest.raises(ValueError):
        scaffold.render_job(bad)
    with pytest.raises(ValueError):
        scaffold.render_probe(bad)


def test_a_hostile_title_cannot_inject_code_or_toml():
    import ast
    evil = 'x"""\nimport os; os.system("id")  #\\\n[[probe]]\nname = "pwn"'
    safe = scaffold._title("t_x", evil)
    assert all(c.isalnum() or c in " ._()/+-" for c in safe) and '"' not in safe and "\n" not in safe and "[" not in safe
    for klass in scaffold.KLASSES:
        tree = ast.parse(scaffold.render_task("t_x", klass, "daily", evil))
        assert not any(isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "system" for n in ast.walk(tree))
        assert not any(isinstance(n, ast.Import) and any(a.name == "subprocess" for a in n.names) for n in ast.walk(tree))
    for text in (scaffold.render_job("j-x", evil), scaffold.render_probe("p-x", "http", evil)):
        doc = tomllib.loads(text)
        (row,) = doc.get("job", doc.get("probe"))
        assert row["name"] in ("j-x", "p-x") and "pwn" not in str(doc.keys())
    assert scaffold._title("my_name", None) == "My name" and scaffold._title("x", "") == "X"


def test_job_snippet_has_the_scheduler_contract_fields():
    doc = tomllib.loads(scaffold.render_job("nightly-x", "Nightly X"))
    (j,) = doc["job"]
    assert j["name"] == "nightly-x" and isinstance(j["command"], list) and j["user"] == "root" and j["class"] == "P3"
    assert {"schedule", "heavy", "timeout_s", "gates", "success", "notify", "mode"} <= set(j)
    assert j["notify"] == {"on_failure": "alert", "on_success": "none"} and j["mode"] == "managed"
    text = scaffold.render_job("nightly-x")
    assert "argv list" in text and "never a shell string" in text and '# retire = ["system:' in text      # retire is a LIST of refs


def test_the_job_snippet_loads_in_the_scheduler_and_will_actually_run(tmp_path):
    """Regression: jobs.py defaults a job without `mode` to observe (shown, never run), and a string `retire` is silently dropped."""
    from homelab_maint import jobs, schedule
    f = tmp_path / "jobs.toml"
    f.write_text(scaffold.render_job("nightly-x", "Nightly X"))
    cfg = jobs.load(f, mcfg={"tasks": {}})
    assert cfg.errors == [] and list(cfg.jobs) == ["nightly-x"]
    j = cfg.jobs["nightly-x"]
    assert j.mode == "managed" and j.cls == "P3" and j.command == ["/usr/bin/true"] and j.notify["on_failure"] == "alert" and j.retire == []
    assert schedule.validate(j.schedule) is None
    from homelab_maint import scheduler
    assert scheduler.validate(cfg) == [], "`schedule validate` must not flag a freshly generated job (managed + adapter + no retire is a problem)"
    two = tmp_path / "two.toml"                                            # the generated snippets can be appended to each other
    two.write_text(scaffold.render_job("job-a") + "\n" + scaffold.render_job("job-b"))
    assert sorted(jobs.load(two, mcfg={"tasks": {}}).jobs) == ["job-a", "job-b"]


@pytest.mark.parametrize("ptype", scaffold.PROBE_TYPES)
def test_every_probe_snippet_is_accepted_by_the_probe_engine(ptype):
    (raw,) = tomllib.loads(scaffold.render_probe("my-probe", ptype, "My probe"))["probe"]
    p, errs = probes.build(raw, dict(probes.DEFAULTS))
    assert errs == [] and p is not None and p.name == "my-probe" and p.type == ptype


def test_probe_type_is_validated():
    with pytest.raises(ValueError):
        scaffold.render_probe("x", "ssh")
    with pytest.raises(ValueError):
        scaffold.render_task("x_y", "C9")
    with pytest.raises(ValueError):
        scaffold.render_task("x_y", "C0", "hourly")


# --------------------------------------------------------------------------- the generated tasks really work
def write_plugin(tmp_path, name, klass, tier="check"):
    d = tmp_path / "plugins.d"
    d.mkdir(exist_ok=True, mode=0o755)
    made = scaffold.create("task", name, d, klass=klass, tier=tier, conf_dir=tmp_path / "conf")
    return d, made


def test_generated_tasks_load_through_plugin_discovery_with_the_right_contract(tmp_path):
    for klass, tier in (("C0", "check"), ("C1", "daily"), ("C2", "weekly")):
        d, made = write_plugin(tmp_path, f"gen_{klass.lower()}", klass, tier)
        assert [p.name for p in made] == [f"gen_{klass.lower()}.py", f"test_gen_{klass.lower()}.py"]
    # the generated files are plain 0644 files in a 0755 dir: discovery accepts them (the test stubs are not .py plugins)
    for p in d.glob("test_*.py"):
        p.unlink()
    rep = load(d)
    assert rep.rejected == {} and set(rep.loaded) == {"gen_c0", "gen_c1", "gen_c2"}
    for klass, tier in (("C0", "check"), ("C1", "daily"), ("C2", "weekly")):
        t = core.REGISTRY[f"gen_{klass.lower()}"]
        assert (t.klass, t.tier) == (klass, tier)


def test_generated_c0_runs_and_reports_one_ascii_line(tmp_path):
    d, _ = write_plugin(tmp_path, "gen_load", "C0")
    (d / "test_gen_load.py").unlink()
    load(d)
    res, _dur = core.run_task(core.REGISTRY["gen_load"], core.load_config(), apply=True)
    assert res.status in ("ok", "warn") and res.summary.isascii() and len(res.summary) <= 140 and res.summary.startswith("Gen load: load ")


def test_generated_c1_selects_nothing_unconfigured_reports_in_report_mode_and_deletes_only_when_apply(tmp_path):
    d, _ = write_plugin(tmp_path, "gen_clean", "C1", "daily")
    (d / "test_gen_clean.py").unlink()
    load(d)
    t = core.REGISTRY["gen_clean"]
    assert core.run_task(t, {"tasks": {}, "caps": {}, "protected": {"patterns": []}}, apply=True)[0].status == "skipped"
    victim = tmp_path / "data" / "old.log"
    victim.parent.mkdir()
    victim.write_text("x" * 100)
    os.utime(victim, (0, 0))
    cfg = {"tasks": {"gen_clean": {"path": str(victim.parent), "max_age_days": 1, "mode": "report"}}, "caps": {}, "protected": {"patterns": []}}
    res, _ = core.run_task(t, cfg, apply=True)
    assert victim.exists() and res.reclaimed_bytes == 0 and "would remove 1 file(s)" in res.summary
    cfg["tasks"]["gen_clean"]["mode"] = "apply"
    res, _ = core.run_task(t, cfg, apply=True)
    assert not victim.exists() and res.reclaimed_bytes == 100 and "removed 1 file(s)" in res.summary
    victim.write_text("again")
    os.utime(victim, (0, 0))
    (core.CONF_DIR).mkdir(parents=True, exist_ok=True)
    (core.CONF_DIR / "PAUSE").write_text("")
    try:
        core.run_task(t, cfg, apply=True)
        assert victim.exists(), "the kill switch stops the generated cleaner too"
    finally:
        (core.CONF_DIR / "PAUSE").unlink()


def test_generated_c2_plans_and_only_applies_with_an_approval_file(tmp_path):
    d, _ = write_plugin(tmp_path, "gen_plan", "C2", "weekly")
    (d / "test_gen_plan.py").unlink()
    load(d)
    t = core.REGISTRY["gen_plan"]
    f = tmp_path / "x.bin"
    f.write_bytes(b"1234")
    cfg = {"tasks": {"gen_plan": {"candidates": [str(f)], "mode": "apply"}}, "caps": {}, "protected": {"patterns": []}}
    res, _ = core.run_task(t, cfg, apply=True)
    assert res.plan and f.exists() and "candidate(s); plan " in res.summary
    h = core.plan_hash(res.plan)
    assert res.plan == core.run_task(t, cfg, apply=False)[0].plan, "stable ordering => stable hash"
    (core.STATE_DIR / "approvals").mkdir(parents=True, exist_ok=True)
    (core.STATE_DIR / "approvals" / f"gen_plan.{h}").write_text("1")
    core.run_task(t, cfg, apply=True)
    assert not f.exists()


@pytest.mark.parametrize("klass,tier", [("C0", "check"), ("C1", "daily"), ("C2", "weekly")])
def test_generated_test_stubs_pass_out_of_the_box(tmp_path, klass, tier):
    name = f"gen_{klass.lower()}"
    scaffold.create("task", name, tmp_path, klass=klass, tier=tier, conf_dir=tmp_path / "conf")
    (tmp_path / "conftest.py").write_text((ROOT / "tests" / "conftest.py").read_text())
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path / f"test_{name}.py")],
                       capture_output=True, text=True, cwd=tmp_path, env=env, timeout=120)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-500:]
    assert "3 passed" in r.stdout


# --------------------------------------------------------------------------- create(): files are written safely
def test_create_writes_new_files_only_and_never_overwrites(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    made = scaffold.create("task", "my_check", out, conf_dir=tmp_path / "c")
    assert {p.name for p in made} == {"my_check.py", "test_my_check.py"}
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o644 for p in made)
    with pytest.raises(FileExistsError):
        scaffold.create("task", "my_check", out, conf_dir=tmp_path / "c")                  # same name again
    (out / "sentinel.toml").write_text("keep")
    scaffold.create("probe", "p-one", out, ptype="tcp", conf_dir=tmp_path / "c")
    assert (out / "sentinel.toml").read_text() == "keep"


def test_create_refuses_names_already_taken_by_builtins_plugins_jobs_and_probes(tmp_path):
    conf = tmp_path / "conf"
    (conf / "plugins.d").mkdir(parents=True)
    (conf / "plugins.d" / "mine.py").write_text('@task("mine_task", klass="C0", tier="check")\ndef r(c): pass\n')
    (conf / "jobs.toml").write_text('[[job]]\nname = "nightly"\n')
    (conf / "probes.d").mkdir()
    (conf / "probes.d" / "a.toml").write_text('[[probe]]\nname = "plexy"\n')
    out = tmp_path / "o"
    out.mkdir()
    for kind, name in (("task", "disk_forecast"), ("task", "mine_task"), ("task", "routine_image_updates"), ("job", "nightly"), ("probe", "plexy")):
        with pytest.raises(FileExistsError):
            scaffold.create(kind, name, out, conf_dir=conf)
    assert list(out.iterdir()) == []
    assert "disk_forecast" in scaffold.known_task_names(conf) and "pressure_state" in scaffold.known_task_names(conf)


def test_create_never_follows_a_planted_symlink(tmp_path):
    out, target = tmp_path / "out", tmp_path / "victim.txt"
    out.mkdir()
    target.write_text("precious")
    (out / "nightly.toml").symlink_to(target)
    with pytest.raises(OSError):
        scaffold.create("job", "nightly", out, conf_dir=tmp_path / "c")
    assert target.read_text() == "precious"


def test_create_is_all_or_nothing(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "test_half_done.py").write_text("already here")                                # the second file cannot be created
    with pytest.raises(OSError):
        scaffold.create("task", "half_done", out, conf_dir=tmp_path / "c")
    assert not (out / "half_done.py").exists() and (out / "test_half_done.py").read_text() == "already here"


def test_create_out_dir_must_be_a_real_directory(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    for bad in (tmp_path / "link", tmp_path / "nope", tmp_path / "file"):
        (tmp_path / "file").write_text("x")
        with pytest.raises(NotADirectoryError):
            scaffold.create("job", "x-job", bad, conf_dir=tmp_path / "c")


def test_install_targets_the_conf_dirs_and_refuses_an_insecure_plugins_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(scaffold, "loader_reads", lambda kind: True)                 # the loaders are exercised in the next tests
    conf = tmp_path / "conf"
    conf.mkdir()
    (p,) = scaffold.create("task", "inst_task", ".", install=True, conf_dir=conf)
    assert p == conf / "plugins.d" / "inst_task.py" and stat.S_IMODE((conf / "plugins.d").stat().st_mode) == 0o755
    assert not (conf / "plugins.d" / "test_inst_task.py").exists(), "the test stub belongs in the repository"
    (j,) = scaffold.create("job", "inst-job", ".", install=True, conf_dir=conf)
    (q,) = scaffold.create("probe", "inst-probe", ".", install=True, conf_dir=conf)
    assert j == conf / "jobs.d" / "inst-job.toml" and q == conf / "probes.d" / "inst-probe.toml"
    os.chmod(conf / "plugins.d", 0o777)
    with pytest.raises(PermissionError, match="writable"):
        scaffold.create("task", "inst_two", ".", install=True, conf_dir=conf)


def test_install_is_refused_when_nothing_would_read_the_directory(tmp_path, monkeypatch):
    """A job in jobs.d/ that jobs.py never reads would look installed and never run: refuse instead, and say what to do."""
    conf = tmp_path / "conf"
    conf.mkdir()
    monkeypatch.setattr(scaffold, "loader_reads", lambda kind: kind == "probe")
    with pytest.raises(NotImplementedError, match="append the snippet to /etc/homelab-maint/jobs.toml"):
        scaffold.create("job", "inst-job", ".", install=True, conf_dir=conf)
    with pytest.raises(NotImplementedError, match="load_plugins"):
        scaffold.create("task", "inst_task", ".", install=True, conf_dir=conf)
    assert not (conf / "jobs.d").exists() and not (conf / "plugins.d").exists()
    scaffold.create("probe", "inst-probe", ".", install=True, conf_dir=conf)          # probes.py does read probes.d
    assert (conf / "probes.d" / "inst-probe.toml").exists()


def test_cli_maps_a_refused_install_to_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scaffold, "loader_reads", lambda kind: False)
    monkeypatch.setattr(core, "CONF_DIR", tmp_path / "conf")
    assert scaffold.main(["new", "job", "cli-job", "--install"]) == 2
    assert "does not read jobs.d/ yet" in capsys.readouterr().err and not (tmp_path / "conf").exists()


def test_loader_reads_looks_at_the_real_consumers():
    assert scaffold.loader_reads("probe") is True                                    # probes.py reads probes.d/*.toml
    assert all(isinstance(scaffold.loader_reads(k), bool) for k in ("task", "job", "probe"))
    pkg = Path(scaffold.__file__).resolve().parent
    assert ("jobs.d" in (pkg / "jobs.py").read_text()) is scaffold.loader_reads("job")
    assert ("load_plugins" in (pkg / "cli.py").read_text()) is scaffold.loader_reads("task")


def test_cli_new_prints_what_it_made_and_exits_2_on_trouble(tmp_path, capsys):
    assert scaffold.main(["new", "task", "cli_task", "--klass", "C1", "--tier", "daily", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "created" in out and "cli_task.py" in out and "next:" in out and (tmp_path / "cli_task.py").exists()
    assert scaffold.main(["new", "task", "cli_task", "--out", str(tmp_path)]) == 2
    assert "already exists" in capsys.readouterr().err
    assert scaffold.main(["new", "task", "Bad-Name", "--out", str(tmp_path)]) == 2
    assert scaffold.main(["new", "probe", "cli-probe", "--type", "docker", "--out", str(tmp_path)]) == 0
    assert 'type = "docker"' in (tmp_path / "cli-probe.toml").read_text()


# =========================================================================== plugin discovery: the happy path
def test_missing_plugins_dir_is_the_normal_case(tmp_path):
    rep = scaffold.load_plugins(tmp_path / "nope")
    assert (rep.loaded, rep.rejected, rep.error_tasks) == ({}, {}, [])


def test_good_plugins_load_in_sorted_order_and_run_through_the_runner(pdir):
    good(pdir, "b_two")
    good(pdir, "a_one")
    plug(pdir, "notes.txt", "ignored")
    plug(pdir, ".hidden.py", "raise SystemExit(1)")
    (pdir / "__pycache__").mkdir()
    rep = load(pdir)
    assert list(rep.loaded) == ["a_one", "b_two"] and rep.rejected == {} and rep.error_tasks == []
    res, _ = core.run_task(core.REGISTRY["a_one"], core.load_config(), apply=False)
    assert res.status == "ok" and res.summary == "a_one is fine"


def test_one_plugin_can_register_several_tasks_and_dataclasses_work(pdir):
    plug(pdir, "multi.py", '''
        from dataclasses import dataclass
        from homelab_maint.core import task, Result

        @dataclass
        class Row:
            n: int

        for i in (1, 2):
            task(f"multi_{i}", klass="C0", tier="check")(lambda ctx, i=i: Result("ok", str(Row(i).n)))
    ''')
    rep = load(pdir)
    assert rep.loaded == {"multi": ["multi_1", "multi_2"]}


def test_discovery_is_idempotent_and_never_execs_twice(pdir, tmp_path):
    log = tmp_path / "execs.log"
    plug(pdir, "once.py", f'''
        from homelab_maint.core import task, Result
        open({str(log)!r}, "a").write("x")
        task("once_task", klass="C0", tier="check")(lambda ctx: Result("ok", "o"))
    ''')
    r1, r2 = load(pdir), load(pdir)
    assert r1.loaded == r2.loaded == {"once": ["once_task"]} and r2.skipped == ["once"] and r2.rejected == {}
    assert log.read_text() == "x"


def test_disabled_plugins_are_skipped(pdir):
    good(pdir, "keep_me")
    good(pdir, "turn_off")
    rep = load(pdir, disabled=("turn_off",))
    assert "turn_off" not in core.REGISTRY and rep.skipped == ["turn_off"] and "keep_me" in core.REGISTRY


# =========================================================================== isolation: a broken plugin breaks only itself
BROKEN = {
    "syntax_err": "def broken(:\n    pass\n",
    "raises": "raise RuntimeError('boom at import')\n",
    "missing_dep": "import a_module_that_does_not_exist_xyz\n",
    "exits": "import sys\nsys.exit(3)\n",
    "name_err": "x = undefined_name\n",
}


@pytest.mark.parametrize("stem", sorted(BROKEN))
def test_a_broken_plugin_is_reported_as_an_error_task_and_others_still_load(pdir, stem):
    plug(pdir, f"{stem}.py", BROKEN[stem])
    good(pdir, "zz_healthy")
    rep = load(pdir)
    assert "zz_healthy" in rep.loaded and "zz_healthy" in core.REGISTRY
    assert stem in rep.rejected and rep.error_tasks == [f"plugin_{stem}"]
    t = core.REGISTRY[f"plugin_{stem}"]
    assert (t.klass, t.tier) == ("C0", "check")
    res, _ = core.run_task(t, core.load_config(), apply=False)
    assert res.status == "error" and res.summary.startswith(f"plugin {stem}.py: ") and res.summary.isascii() and len(res.summary) <= 140
    assert "Traceback" not in res.summary and str(pdir) not in res.summary and "traceback" not in res.metrics
    assert core.run_task(core.REGISTRY["zz_healthy"], core.load_config(), apply=False)[0].status == "ok"


def test_syntax_error_report_names_the_line(pdir):
    plug(pdir, "bad.py", "x = 1\ny = (\n")
    rep = load(pdir)
    assert "SyntaxError" in rep.rejected["bad"] and "line" in rep.rejected["bad"]


def test_failed_plugin_registrations_are_rolled_back(pdir):
    plug(pdir, "half.py", '''
        from homelab_maint.core import task, Result
        task("half_one", klass="C0", tier="check")(lambda ctx: Result("ok", "1"))
        raise RuntimeError("dies after registering")
    ''')
    rep = load(pdir)
    assert "half_one" not in core.REGISTRY and "homelab_plugin_half" not in sys.modules and "half" in rep.rejected


def test_a_hung_import_is_cut_off_by_the_timeout(pdir):
    plug(pdir, "hang.py", "import time\ntime.sleep(30)\n")
    good(pdir, "zz_after")
    prev_handler = signal.getsignal(signal.SIGALRM)
    rep = load(pdir, timeout_s=0.3)
    assert rep.rejected["hang"] == "import timed out" and "zz_after" in rep.loaded
    assert signal.getsignal(signal.SIGALRM) == prev_handler and signal.getitimer(signal.ITIMER_REAL)[0] == 0


def test_a_plugin_that_swallows_exceptions_still_gets_the_timeout(pdir):
    plug(pdir, "stubborn.py", "import time\ntry:\n    time.sleep(30)\nexcept Exception:\n    pass\n")
    assert load(pdir, timeout_s=0.3).rejected["stubborn"] == "import timed out"


def test_a_plugin_cannot_replace_or_remove_a_builtin(pdir):
    builtin = core.REGISTRY["disk_forecast"]
    plug(pdir, "evil.py", '''
        from homelab_maint.core import task, Result
        task("disk_forecast", klass="C0", tier="check")(lambda ctx: Result("ok", "all clear, nothing to see"))
    ''')
    plug(pdir, "evil2.py", "from homelab_maint import core\ncore.REGISTRY.pop('failed_units')\n")
    fu = core.REGISTRY["failed_units"]
    rep = load(pdir)
    assert core.REGISTRY["disk_forecast"] is builtin and core.REGISTRY["failed_units"] is fu
    assert "replace or remove" in rep.rejected["evil"] and "failed_units" in rep.rejected["evil2"]
    assert core.run_task(core.REGISTRY["plugin_evil"], core.load_config(), apply=False)[0].status == "error"


def test_two_plugins_cannot_fight_over_one_task_name(pdir):
    good(pdir, "a_first", task="shared_name")
    good(pdir, "b_second", task="shared_name")
    first = None
    rep = load(pdir)
    assert rep.loaded == {"a_first": ["shared_name"]} and "b_second" in rep.rejected
    assert core.run_task(core.REGISTRY["shared_name"], core.load_config(), apply=False)[0].summary == "shared_name is fine"


@pytest.mark.parametrize("decl", [
    'task("Bad-Name", klass="C0", tier="check")',
    'task("ok_name", klass="C9", tier="check")',
    'task("ok_name", klass="C0", tier="hourly")',
    'task("ok_name", klass="C0", tier="check", timeout=0)',
    'task("ok_name", klass="C0", tier="check", timeout=99999)',
])
def test_invalid_task_registrations_are_rejected_and_removed(pdir, decl):
    plug(pdir, "inv.py", f"from homelab_maint.core import task, Result\n{decl}(lambda ctx: Result('ok', 'x'))\n")
    rep = load(pdir)
    assert "inv" in rep.rejected and "ok_name" not in core.REGISTRY and "Bad-Name" not in core.REGISTRY


def test_a_non_callable_registration_is_rejected(pdir):
    plug(pdir, "nc.py", "from homelab_maint import core\ncore.REGISTRY['nc_task'] = core.Task('nc_task', 'C0', 'check', 'not callable')\n")
    assert "nc" in load(pdir).rejected and "nc_task" not in core.REGISTRY


def test_error_placeholders_never_shadow_a_real_task(pdir):
    plug(pdir, "x.py", "raise RuntimeError('no')\n")
    core.REGISTRY["plugin_x"] = core.REGISTRY["disk_forecast"]
    rep = load(pdir)
    assert core.REGISTRY["plugin_x"] is core.REGISTRY["disk_forecast"] and rep.error_tasks == []


def test_register_errors_false_reports_without_registering(pdir):
    plug(pdir, "bad.py", "raise RuntimeError('no')\n")
    rep = load(pdir, register_errors=False)
    assert "bad" in rep.rejected and "plugin_bad" not in core.REGISTRY and rep.error_tasks == []


# =========================================================================== security: files
def test_group_and_world_writable_files_are_refused_and_not_executed(pdir, tmp_path):
    marker = tmp_path / "ran"
    body = f"open({str(marker)!r}, 'w').write('x')\n"
    for mode in (0o664, 0o646, 0o666, 0o620, 0o602):
        plug(pdir, f"w{mode:o}.py", body, mode)
    rep = load(pdir)
    assert not marker.exists() and rep.loaded == {}
    assert all("writable" in why for why in rep.rejected.values()) and len(rep.rejected) == 5
    assert all(core.run_task(core.REGISTRY[f"plugin_{s}"], core.load_config(), False)[0].status == "error" for s in rep.rejected)


def test_symlinks_are_refused_even_to_a_perfectly_good_file(pdir, tmp_path):
    real = good(tmp_path, "real_one")
    (pdir / "linked.py").symlink_to(real)
    rep = load(pdir)
    assert "symlink" in rep.rejected["linked"] and "real_one" not in core.REGISTRY


def test_only_root_owned_files_run(pdir, monkeypatch):
    p = good(pdir, "mine")
    monkeypatch.setattr(os, "geteuid", lambda: 0)                       # now only uid 0 is trusted; this file is owned by the test user
    assert "owned by uid" in scaffold.file_problem(p.lstat(), "mine.py") and "not root" in scaffold.file_problem(p.lstat(), "mine.py")
    assert scaffold.file_problem(p.lstat(), "mine.py", trusted_uids={os.getuid()}) is None
    rep = load(pdir)                                                    # the directory is ours too, so nothing at all is executed
    assert "not root" in rep.rejected["(directory)"] and "mine" not in core.REGISTRY


def test_explicitly_trusted_uid_is_honoured(pdir, monkeypatch):
    good(pdir, "mine")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    rep = load(pdir, trusted_uids={os.getuid()})
    assert "mine" in rep.loaded


def test_non_regular_files_oversize_files_and_bad_names_are_refused(pdir):
    os.mkfifo(pdir / "fifo.py", 0o644)
    plug(pdir, "big.py", "x = 1\n" + "# pad\n" * 60000)
    plug(pdir, "has-dash.py", "x = 1\n")
    plug(pdir, "1starts_with_digit.py", "x = 1\n")
    plug(pdir, "with space.py", "x = 1\n")
    rep = load(pdir, timeout_s=1)
    assert "not a regular file" in rep.rejected["fifo"] and "larger than 256 KiB" in rep.rejected["big"]
    assert all("plain module name" in rep.rejected[s] for s in ("has-dash", "1starts_with_digit", "with space"))
    assert rep.loaded == {}


def test_the_bytes_executed_are_the_bytes_that_were_checked(pdir, tmp_path, monkeypatch):
    """Check-then-open race: the path is swapped for another file between lstat and open. fstat on the opened fd must notice."""
    good(pdir, "victim")
    evil = plug(tmp_path, "evil_src.py", "from homelab_maint.core import task, Result\ntask('pwned', klass='C0', tier='check')(lambda c: Result('ok','x'))\n")
    real_open = os.open

    def swapped(path, flags, *a, **k):
        if str(path).endswith("victim.py"):
            return real_open(evil, flags, *a, **k)
        return real_open(path, flags, *a, **k)
    monkeypatch.setattr(os, "open", swapped)
    rep = load(pdir)
    assert "pwned" not in core.REGISTRY and "replaced while being opened" in rep.rejected["victim"]


def test_open_uses_nofollow(pdir, monkeypatch):
    good(pdir, "p")
    flags = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda path, fl, *a, **k: (flags.append(fl), real_open(path, fl, *a, **k))[1])
    load(pdir)
    assert flags and all(f & os.O_NOFOLLOW for f in flags)


# =========================================================================== security: directories
def test_group_writable_plugin_dir_refuses_everything_and_says_so(pdir, tmp_path):
    marker = tmp_path / "ran"
    plug(pdir, "p.py", f"open({str(marker)!r}, 'w').write('x')\n")
    os.chmod(pdir, 0o775)
    rep = load(pdir)
    assert not marker.exists() and rep.loaded == {} and "writable" in rep.rejected["(directory)"] and rep.error_tasks == ["plugins_dir"]
    res, _ = core.run_task(core.REGISTRY["plugins_dir"], core.load_config(), apply=False)
    assert res.status == "error" and "plugins.d ignored" in res.summary and "writable" in res.summary
    os.chmod(pdir, 0o757)
    assert "writable" in load(pdir).rejected["(directory)"]


def test_symlinked_plugin_dir_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o755)
    good(real, "p")
    (tmp_path / "plugins.d").symlink_to(real)
    rep = scaffold.load_plugins(tmp_path / "plugins.d", check_ancestors=False)
    assert "symlink" in rep.rejected["(directory)"] and "p" not in core.REGISTRY


def test_plugin_dir_owned_by_someone_else_is_refused(pdir, monkeypatch):
    good(pdir, "p")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert "not root" in load(pdir).rejected["(directory)"]


def test_a_world_writable_ancestor_blocks_discovery_unless_it_is_sticky(tmp_path):
    outer = tmp_path / "outer"
    outer.mkdir()
    inner = outer / "etc"
    inner.mkdir(mode=0o755)
    pd = inner / "plugins.d"
    pd.mkdir(mode=0o755)
    good(pd, "pp")
    os.chmod(outer, 0o777)
    assert "ancestor" in (scaffold.dir_problem(pd) or "") and "writable" in scaffold.dir_problem(pd)
    rep = scaffold.load_plugins(pd)
    assert "ancestor" in rep.rejected["(directory)"] and "pp" not in core.REGISTRY
    os.chmod(outer, 0o1777)                                              # like /tmp: others cannot rename our directories
    assert scaffold.dir_problem(pd) is None and "pp" in scaffold.load_plugins(pd).loaded
    os.chmod(outer, 0o755)


def test_dir_problem_reports_each_defect(tmp_path):
    assert "cannot stat" in scaffold.dir_problem(tmp_path / "nope")
    f = tmp_path / "f"
    f.write_text("x")
    assert scaffold.dir_problem(f) == "is not a directory"
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    assert scaffold.dir_problem(d, check_ancestors=False) is None


def test_listing_failure_never_raises(pdir, monkeypatch):
    monkeypatch.setattr(os, "listdir", lambda p: (_ for _ in ()).throw(PermissionError(13, "denied")))
    rep = load(pdir)
    assert "cannot list" in rep.rejected["(directory)"]


def test_the_runner_wiring_still_runs_everything_else(pdir):
    """What cli.load_tasks does: builtins first, then plugins; a broken plugin shows up as an error task next to them."""
    plug(pdir, "oops.py", "raise ImportError('x')\n")
    good(pdir, "fine")
    load(pdir)
    assert {"disk_forecast", "fine", "plugin_oops"} <= set(core.REGISTRY)
    statuses = {n: core.run_task(core.REGISTRY[n], core.load_config(), apply=False)[0].status for n in ("fine", "plugin_oops")}
    assert statuses == {"fine": "ok", "plugin_oops": "error"}


def test_cli_plugins_command_lists_loaded_and_refused(pdir, monkeypatch, capsys):
    good(pdir, "fine")
    plug(pdir, "writable.py", "x = 1\n", 0o666)
    real_load = scaffold.load_plugins
    monkeypatch.setattr(scaffold, "load_plugins", lambda: real_load(pdir, check_ancestors=False))
    assert scaffold.main(["plugins"]) == 1
    out = capsys.readouterr().out
    assert "loaded    fine.py: fine" in out and "REFUSED   writable" in out
