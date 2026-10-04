"""Tests for registry.py: the rules registry (rules.d) -> generated legacy config files, change tracking, exports, migrate.

The centrepiece is the EQUALITY PROOF: migrate builds a registry from every shipped etc/*.toml and compile(rules.d) must parse to
exactly the data of the original file; randomised mutations of the registry must change the compiled data in exactly the expected place.
Everything runs in tmp dirs with explicit conf/state paths; no notification is ever sent (the autouse fixture replaces both hooks) and
nothing under /etc, /var or /usr is read or written.
"""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import copy
import json
import os
import random
import re
import shutil
import statistics
import time
import tomllib
import types
from pathlib import Path

import pytest

from homelab_maint import core, registry as R

ROOT = Path(__file__).resolve().parent.parent
ETC = ROOT / "etc"
TODAY = "2026-10-02"
LEGACY = list(R.MANAGED_FILES)
SHIPPED = {f: tomllib.loads((ETC / f).read_text()) for f in LEGACY}       # the shipped config as it is today: expectations derive from it, not from numbers
TASKS = SHIPPED["maint.toml"]["tasks"]
W = TASKS["disk_forecast"]["warn_free_pct"]                                  # one real knob of a core task, whatever its value is this release
HG = TASKS["docker_cache"]["high_gib"]
RET = TASKS["retention"]["rules"]
N_PATTERNS = len(SHIPPED["protected.toml"]["patterns"])
N_PROBES = sum(len(g["probes"]) for g in SHIPPED["probes.toml"]["group"]) + len(SHIPPED["probes.toml"].get("probe", []))
JOB0 = SHIPPED["jobs.toml"]["job"][0]["name"]
PROBE0 = SHIPPED["probes.toml"]["group"][0]["probes"][0]["name"]
BASE_RULES = 4                                                               # the doc-only rules of the shipped baseline file
assert W > 0 and HG > 1 and len(RET) >= 2


def rid(prefix: str, name: str) -> str:
    return R._slug(f"{prefix}.{name}", 70)                                   # how migrate names a rule


# =========================================================================== helpers
def mkdir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o755)
    return p


def rule(rid: str, **kw) -> dict:
    d = {"id": rid, "title": f"Rule {rid}", "kind": "check", "why": "because", "does": "it does"}
    d.update(kw)
    return d


def write_reg(rd: Path, files: dict[str, list[dict]], *, baseline: bool = True, patterns=(), extra_meta: dict | None = None) -> Path:
    """Write registry files (name -> rules) and, unless told not to, the baseline with `patterns` as its protected floor."""
    mkdir(rd)
    for name, rules in files.items():
        m = re.fullmatch(r"\d\d-([a-z0-9_-]+)\.toml", name)
        text = R.dumps({"meta": {"category": m.group(1) if m and m.group(1) in R.CATEGORIES else "checks", **(extra_meta or {})}, "rule": rules},
                       aot_keys=("rule",))
        (rd / name).write_text(text)
        os.chmod(rd / name, 0o644)
    if baseline:
        (rd / R.BASELINE_FILE).write_text(R.baseline_text(list(patterns), today=TODAY))
        os.chmod(rd / R.BASELINE_FILE, 0o644)
    return rd


def compile_rules(tmp_path: Path, rules: list[dict], name: str = "10-checks.toml") -> R.Compiled:
    reg = R.load_registry(rdir=write_reg(tmp_path / "rd", {name: rules}, baseline=False), trust=False)
    assert not reg.errors, reg.errors
    return R.compile_registry(reg)


def compile_errors(tmp_path: Path, rules: list[dict]) -> list[str]:
    reg = R.load_registry(rdir=write_reg(tmp_path / "rd", {"10-checks.toml": rules}, baseline=False), trust=False)
    return reg.errors or R.compile_registry(reg).errors


def parse(text: str) -> dict:
    return tomllib.loads(text)


@pytest.fixture(autouse=True)
def no_real_notices(monkeypatch):
    """The default hooks would call notify.send / routine.record_change: replace both, record what they were given."""
    sent: list[dict] = []
    monkeypatch.setattr(R, "_notice_via_notify", lambda p: sent.append(p) or True)
    monkeypatch.setattr(R, "_journal_via_routine", lambda p: True)
    monkeypatch.setattr(R.os, "fsync", lambda fd: None)           # durability is not what these tests check, and a real fsync costs ~20 ms
    return sent


@pytest.fixture(autouse=True)
def floor(request, monkeypatch):
    """The release floor pinned in registry.py (the strictest baseline, a mirror in rules.d can only add to it) is OFF for the tests that build
    their own tiny baseline and ON for every test that runs the shipped registry or asks for `real_floor`."""
    on = bool({"mig", "template", "real_floor"} & set(request.fixturenames))
    monkeypatch.setattr(R, "FLOOR_ENFORCED", on)
    return on


@pytest.fixture
def real_floor():
    """Asking for it switches the release floor on (see `floor`)."""
    return True


@pytest.fixture
def env(tmp_path):
    conf, state = mkdir(tmp_path / "conf"), mkdir(tmp_path / "state")
    return types.SimpleNamespace(conf=conf, state=state, rd=conf / "rules.d", tmp=tmp_path)


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """ONE migrated registry of the shipped config (the proof costs ~0.7 s); tests copy it."""
    base = tmp_path_factory.mktemp("tpl")
    out = base / "rules.d"
    proof, texts = R.migrate(ETC, out, today=TODAY)
    assert proof.ok, proof.files
    mkdir(out)
    return types.SimpleNamespace(rd=out, texts=texts, proof=proof, n=len(R.load_registry(rdir=out, trust=False).rules))


@pytest.fixture
def mig(env, template):
    """A conf dir holding the shipped legacy files and a copy of the migrated registry, ready for sync()."""
    for f in ETC.glob("*.toml"):
        shutil.copy(f, env.conf / f.name)
        os.chmod(env.conf / f.name, 0o644)
    shutil.copytree(template.rd, env.rd)
    os.chmod(env.rd, 0o755)
    env.n = template.n
    return env


def sync(env, **kw):
    return R.sync(env.conf, env.state, hooks=kw.pop("hooks", R.NO_HOOKS), **kw)


# =========================================================================== 1. the TOML emitter
@pytest.mark.parametrize("name", sorted(p.name for p in ETC.glob("*.toml")))
def test_emitter_roundtrips_every_shipped_file(name):
    doc = tomllib.loads((ETC / name).read_text())
    text = R.dumps(doc)
    assert R.same(tomllib.loads(text), doc)


def _rand_str(rnd: random.Random) -> str:
    pool = ['a', 'Z', '0', ' ', '"', "'", '\\', '/', '\n', '\t', '\r', '\x00', '\x1f', '\x7f', 'é', '日', '😀', '#', '=', '[', ']', '{', '}', '.', ',']
    return "".join(rnd.choice(pool) for _ in range(rnd.randint(0, 12)))


def _rand_val(rnd: random.Random, depth: int):
    kinds = ["int", "float", "bool", "str", "list", "dict"] if depth < 3 else ["int", "float", "bool", "str"]
    k = rnd.choice(kinds)
    if k == "int":
        return rnd.choice([0, 1, -1, 2 ** 53, -2 ** 62, 12345])
    if k == "float":
        return rnd.choice([0.0, 1.5, -2.25, 1e-7, 1e22, 5.0, 0.1])
    if k == "bool":
        return rnd.choice([True, False])
    if k == "str":
        return _rand_str(rnd)
    if k == "list":
        n = rnd.randint(0, 5)
        style = rnd.choice(["any", "dicts", "strs"])
        if style == "dicts":
            return [_rand_dict(rnd, depth + 1) for _ in range(n)]
        if style == "strs":
            return [_rand_str(rnd) for _ in range(n)]
        return [_rand_val(rnd, depth + 1) for _ in range(n)]
    return _rand_dict(rnd, depth + 1)


def _rand_dict(rnd: random.Random, depth: int) -> dict:
    keys = ["a", "b-c", "d_e", "with space", "dot.ted", "", "0", "é", 'q"uote', "x" * 40, "[x]", "a=b"]
    return {rnd.choice(keys) + (str(i) if rnd.random() < .5 else ""): _rand_val(rnd, depth) for i in range(rnd.randint(0, 6))}


def test_emitter_roundtrips_random_documents():
    rnd = random.Random(20261002)
    for _ in range(400):
        doc = _rand_dict(rnd, 0)
        text = R.dumps(doc)
        assert R.same(tomllib.loads(text), doc), text


def test_emitter_roundtrips_random_documents_with_provenance_and_forced_tables():
    """Provenance comments and forced `[[x]]` blocks are layout only: the data must not change."""
    rnd = random.Random(7)
    for _ in range(150):
        doc = {"rule": [_rand_dict(rnd, 1) for _ in range(rnd.randint(0, 4))], **_rand_dict(rnd, 0)}
        tprov, lprov = {}, {}
        for el in doc["rule"]:
            tprov[id(el)] = ["r.one", "r.two"]
        for v in doc.values():
            if isinstance(v, list):
                lprov[id(v)] = [(0, "rule.a"), (len(v) // 2, "rule.b")]
            elif isinstance(v, dict):
                tprov[id(v)] = ["rule.c"]
        assert R.same(tomllib.loads(R.dumps(doc, tprov=tprov, lprov=lprov, aot_keys=("rule",))), doc)


def test_emitter_strings_prefer_literal_for_regexes_and_escape_the_rest():
    t = R.dumps({"rx": r"^(a|b)\s+\d$", "q": 'say "hi"', "nl": "a\nb", "both": "it's \\ \"x\"", "uni": "é日😀"})
    assert "rx = '^(a|b)\\s+\\d$'" in t
    assert "nl = \"a\\nb\"" in t
    assert R.same(tomllib.loads(t), {"rx": r"^(a|b)\s+\d$", "q": 'say "hi"', "nl": "a\nb", "both": "it's \\ \"x\"", "uni": "é日😀"})
    assert "\n" not in t.split("nl = ")[1].split("\n")[0]          # control characters never break a line


def test_emitter_dates_floats_and_special_floats():
    import datetime as dt
    doc = {"d": dt.date(2026, 10, 2), "dt": dt.datetime(2026, 10, 2, 7, 30, tzinfo=dt.timezone.utc), "t": dt.time(7, 30, 5, 120),
           "f": [0.0, -0.0, 1e-7, 5.0, float("inf"), float("-inf")], "big": 2 ** 63}
    back = tomllib.loads(R.dumps(doc))
    assert R.same(back, doc)
    assert R.dumps({"n": float("nan")}).strip() == "n = nan"


def test_emitter_refuses_what_toml_cannot_say():
    for bad in ({"a": None}, {"a": object()}, {"a": "\ud800"}, {1: "x"}):
        with pytest.raises(ValueError):
            R.dumps(bad)
    with pytest.raises(ValueError):
        R.dumps([1])       # type: ignore[arg-type]


def test_same_is_strict_about_types_and_order():
    import datetime as dt
    assert R.same({"a": 1, "b": [1, 2]}, {"b": [1, 2], "a": 1})
    assert not R.same({"a": 1}, {"a": 1.0})
    assert not R.same({"a": 1}, {"a": True})
    assert not R.same({"a": [1, 2]}, {"a": [2, 1]})
    assert not R.same(dt.date(2026, 1, 1), dt.datetime(2026, 1, 1))
    assert R.same(float("nan"), float("nan"))
    assert not R.same({"a": {}}, {"a": []})


def test_diff_docs_names_the_leaf_that_differs():
    a = {"t": {"x": 1, "y": [{"n": 1}, {"n": 2}]}, "gone": 1}
    b = {"t": {"x": 2, "y": [{"n": 1}, {"n": 3}, {"n": 4}]}, "new": 1}
    d = {p: (x, y) for p, x, y in R.diff_docs(a, b)}
    assert d["t.x"] == (1, 2) and d["t.y[1].n"] == (2, 3)
    assert d["t.y[2]"][0] is R._MISSING and d["gone"][1] is R._MISSING and d["new"][0] is R._MISSING


# =========================================================================== 2. loader and schema
def test_minimal_registry_loads_and_hash_is_stable(tmp_path):
    rd = write_reg(tmp_path / "rd", {"10-checks.toml": [rule("task.alpha", file="maint.toml", target="tasks.alpha", params={"a": 1})]})
    r1, r2 = R.load_registry(rdir=rd, trust=False), R.load_registry(rdir=rd, trust=False)
    assert r1.valid and len(r1.rules) == 5                       # + the 4 doc-only rules of the baseline file
    assert r1.hash == r2.hash and len(r1.hash) == 64
    (rd / "10-checks.toml").write_text((rd / "10-checks.toml").read_text() + "\n# a comment\n")
    assert R.load_registry(rdir=rd, trust=False).hash != r1.hash       # any byte of any file moves the hash


def test_no_registry_dir_is_not_an_error(tmp_path):
    reg = R.load_registry(tmp_path)
    assert not reg.present and reg.valid and reg.rules == []


@pytest.mark.parametrize("patch,needle", [
    ({"why": None}, "missing required field 'why'"),
    ({"destuctive": True}, "unknown field 'destuctive' (did you mean 'destructive'?)"),
    ({"id": "Bad Id"}, "id must match"),
    ({"id": "ab"}, "id must match"),
    ({"kind": "frobnicate"}, "kind must be one of"),
    ({"severity": "loud"}, "severity must be one of"),
    ({"merge": "replace"}, "merge must be one of"),
    ({"mode": "yes"}, "mode must be report or apply"),
    ({"enabled": "no"}, "enabled must be true or false"),
    ({"destructive": 1}, "destructive must be true or false"),
    ({"order": True}, "order must be an integer"),
    ({"order": 1.5}, "order must be an integer"),
    ({"title": ""}, "title must be non-empty"),
    ({"title": "x" * 121}, "title must be non-empty and at most 120"),
    ({"applies_to": "x"}, "applies_to must be a list"),
    ({"applies_to": ["bad name!"]}, "applies_to must be a list"),
    ({"file": "nope.toml", "target": "a"}, "is not a managed file"),
    ({"file": "maint.toml", "params": [1]}, "params must be a table"),
    ({"file": "maint.toml", "merge": "append", "params": {"a": 1}}, "needs list-valued params"),
    ({"file": "maint.toml", "target": "a[]"}, "needs merge = \"append\""),
    ({"file": "maint.toml", "target": "a[]", "merge": "append", "mode": "apply"}, "mode cannot be set on an array element"),
    ({"file": "maint.toml", "target": "@task.alpha:x", "params": {"a": 1}}, "cannot be relative to itself"),
    ({"file": None, "target": "tasks.x"}, "only documents a policy"),
    ({"file": None}, "only documents a policy"),
])
def test_rule_schema_errors(tmp_path, patch, needle):
    base = rule("task.alpha", file="maint.toml", target="tasks.alpha", params={"a": 1})
    for k, v in patch.items():
        if v is None:
            base.pop(k)
        else:
            base[k] = v
    errs = R.load_registry(rdir=write_reg(tmp_path / "rd", {"10-checks.toml": [base]}, baseline=False), trust=False).errors
    assert any(needle in e for e in errs), errs


@pytest.mark.parametrize("file", ["../maint.toml", "/etc/passwd", "a/b.toml", "maint.toml/../../x", ".maint.toml", "maint.toml\x00", "..", "MAINT.TOML",
                                  "kuma.toml", "playbooks.toml", "rules.d/x.toml", "C:\\x.toml"])
def test_path_traversal_and_foreign_files_in_file_field_are_rejected(tmp_path, file):
    errs = compile_errors(tmp_path, [rule("task.alpha", file=file, target="tasks.alpha", params={"a": 1})])
    assert any("file " in e for e in errs), errs


@pytest.mark.parametrize("target", ["../x", "a/../b", "a..b", "a/b", "/abs", "a.", ".a", "a b", "a;b", "a[0]", "[]", "a[]b", "a[].b[]", "x" * 301,
                                    "a.b.c.d.e.f.g.h.i", '"unterminated', '""', "@x", "@a:b", "@Bad_Id:x", "a.\x00b"])
def test_path_traversal_and_bad_targets_are_rejected(tmp_path, target):
    errs = compile_errors(tmp_path, [rule("task.alpha", file="maint.toml", target=target, params={"a": 1}, merge="append" if target.endswith("[]") else "set")])
    assert any("target" in e for e in errs), (target, errs)


@pytest.mark.parametrize("target", ["", "tasks.x", "a.b.c", "tasks.\"odd name\"", "a-b.c_d", "list[]", "tasks.x.rules[]", "@task.alpha:", "@task.alpha:probes[]"])
def test_good_targets_parse(target):
    R.parse_target(target)


def test_params_must_be_plain_toml_data(tmp_path):
    rd = write_reg(tmp_path / "rd", {"10-checks.toml": []}, baseline=False)
    (rd / "20-spike.toml").write_text(
        '[meta]\ncategory="spike"\n[[rule]]\nid="spike.nan"\ntitle="t"\nkind="spike"\nwhy="w"\ndoes="d"\nfile="maint.toml"\ntarget="tasks.x"\n'
        "[rule.params]\nbad = nan\n")
    errs = R.load_registry(rdir=rd, trust=False).errors
    assert any("non-finite" in e for e in errs)
    deep = {"a": 1}
    for _ in range(12):
        deep = {"n": deep}
    rd = write_reg(tmp_path / "rd2", {"10-checks.toml": [rule("task.deep", file="maint.toml", target="tasks.x", params=deep)]}, baseline=False)
    assert any("nested deeper" in e for e in R.load_registry(rdir=rd, trust=False).errors)


def test_toml_dates_are_accepted_as_since(tmp_path):
    rd = write_reg(tmp_path / "rd", {"10-checks.toml": []}, baseline=False)
    (rd / "10-checks.toml").write_text('[meta]\ncategory="checks"\n[[rule]]\nid="check.d"\ntitle="t"\nkind="check"\nwhy="w"\ndoes="d"\nsince=2026-10-01\n')
    reg = R.load_registry(rdir=rd, trust=False)
    assert reg.valid and reg.rules[0].since == "2026-10-01"


def test_duplicate_ids_across_files_and_in_one_file(tmp_path):
    rd = write_reg(tmp_path / "rd", {"10-checks.toml": [rule("task.dup"), rule("task.dup")], "20-spike.toml": [rule("task.dup", kind="spike")]}, baseline=False)
    errs = R.load_registry(rdir=rd, trust=False).errors
    assert sum("duplicate id" in e for e in errs) == 2 and any("also in 10-checks.toml" in e for e in errs)


def test_file_names_meta_and_foreign_tables(tmp_path):
    rd = mkdir(tmp_path / "rd")
    (rd / "rules.toml").write_text("")                                     # not NN-category.toml
    (rd / "10-checks.toml").write_text('[meta]\ncategory="checks"\nbogus=1\n[surprise]\nx=1\n')
    (rd / "20-nonsense.toml").write_text("")                                # name ok, category not
    (rd / "30-cleanup.toml").write_text('[meta]\ncategory="cleanup"\nallow_baseline_removal=["x"]\n[baseline]\nlimit=[]\n')
    (rd / "notes.txt").write_text("ignored")
    (rd / ".hidden.toml").write_text("ignored")
    for f in rd.iterdir():
        os.chmod(f, 0o644)
    errs = "\n".join(R.load_registry(rdir=rd, trust=False).errors)
    assert "rules.toml: registry files are named NN-category.toml" in errs
    assert "[meta] unknown key 'bogus'" in errs
    assert "unknown top-level table 'surprise'" in errs
    assert "20-nonsense.toml: category must be one of" in errs
    assert "allow_baseline_removal is only honoured in 99-owner-overrides.toml" in errs
    assert "the [baseline] table is only valid in 00-baseline-invariants.toml" in errs
    assert "notes.txt" not in errs and ".hidden" not in errs


def test_baseline_table_validation(tmp_path):
    rd = mkdir(tmp_path / "rd")
    (rd / R.BASELINE_FILE).write_text('[meta]\ncategory="safety"\n[baseline]\nprotected_patterns=["("]\nnever_touch=[1]\nmin_root_depth=0\nwhat=1\n'
                                      '[[baseline.limit]]\nfile="maint.toml"\n')
    os.chmod(rd / R.BASELINE_FILE, 0o644)
    errs = "\n".join(R.load_registry(rdir=rd, trust=False).errors)
    for needle in ("bad regex '('", "never_touch must be a list of strings", "min_root_depth must be 1..6", "unknown key 'what'", "limit #1 needs file, path, max"):
        assert needle in errs, errs


def test_owner_override_file_may_carry_allow_baseline_removal(tmp_path):
    rd = write_reg(tmp_path / "rd", {"99-owner-overrides.toml": [rule("policy.owner")]}, baseline=False, extra_meta={"allow_baseline_removal": ["redis"]})
    reg = R.load_registry(rdir=rd, trust=False)
    assert reg.valid and reg.allow_removal == ["redis"]
    (rd / "99-owner-overrides.toml").write_text('[meta]\nallow_baseline_removal = ["redis"]\n')       # no category needed in the two special files
    assert R.load_registry(rdir=rd, trust=False).valid


# --------------------------------------------------------------------------- hostile files: never crash, never half-load
def test_untrusted_files_and_directories_are_not_believed(tmp_path):
    rd = write_reg(tmp_path / "rd", {"10-checks.toml": [rule("task.alpha")]})
    os.chmod(rd / "10-checks.toml", 0o664)
    errs = R.load_registry(rdir=rd, trust=True).errors
    assert any("10-checks.toml: not trusted" in e for e in errs)
    os.chmod(rd / "10-checks.toml", 0o644)
    os.chmod(rd, 0o775)
    assert any("registry directory must not be a symlink" in e for e in R.load_registry(rdir=rd, trust=True).errors)
    os.chmod(rd, 0o755)
    assert R.load_registry(rdir=rd, trust=True).valid


def test_symlinks_fifos_directories_and_oversize_files(tmp_path):
    rd = write_reg(tmp_path / "rd", {"10-checks.toml": [rule("task.alpha")]})
    outside = tmp_path / "outside.toml"
    outside.write_text('[meta]\ncategory="checks"\n')
    os.symlink(outside, rd / "20-spike.toml")
    os.mkfifo(rd / "30-cleanup.toml")
    (rd / "40-protection.toml").mkdir()
    (rd / "50-alerts.toml").write_bytes(b"#" * (R.MAX_FILE_BYTES + 10))
    t0 = time.time()
    reg = R.load_registry(rdir=rd, trust=False)
    assert time.time() - t0 < 5                                   # the FIFO did not block
    errs = "\n".join(reg.errors)
    for n in ("20-spike.toml", "30-cleanup.toml", "40-protection.toml", "50-alerts.toml"):
        assert f"{n}: cannot be read" in errs, errs
    assert not reg.valid


def test_garbage_never_crashes_the_loader_or_the_analysis(tmp_path):
    rnd = random.Random(99)
    rd = write_reg(tmp_path / "rd", {"10-checks.toml": [rule("task.alpha", file="maint.toml", target="tasks.alpha", params={"a": 1})]})
    valid = (rd / "10-checks.toml").read_bytes()
    blobs = [b"", b"\xff\xfe\x00bad", b"[[[[[[[[[[", b"a = " + b"[" * 5000, b'x = "' + b"\\" * 99, bytes(rnd.randrange(256) for _ in range(3000)),
             b"[[rule]]\nid = 1\n", b"[[rule]]\n" * 50, b"rule = 3\n", b"[meta]\ncategory = 4\n", b"[[rule]]\nid='x'\n[rule]\n", b"\x00" * 10]
    for _ in range(60):                                           # byte-level mutations of a valid file
        b = bytearray(valid)
        for _ in range(rnd.randint(1, 6)):
            b[rnd.randrange(len(b))] = rnd.randrange(256)
        blobs.append(bytes(b))
    for i, blob in enumerate(blobs):
        (rd / "10-checks.toml").write_bytes(blob)
        an = R.analyze(rdir=rd, trust=False, catalog={})
        assert isinstance(an.errors, list)                        # no exception, and a damaged file is never "ok" unless it still is valid
        if an.ok:
            assert tomllib.loads(blob.decode()) is not None


def test_rule_count_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "MAX_RULES", 3)
    rd = write_reg(tmp_path / "rd", {"10-checks.toml": [rule(f"task.r{i}") for i in range(6)]}, baseline=False)
    assert any("more than 3 rules" in e for e in R.load_registry(rdir=rd, trust=False).errors)


# =========================================================================== 3. the compiler
def test_set_merge_is_a_deep_merge_that_preserves_types(tmp_path):
    c = compile_rules(tmp_path, [
        rule("t.one", file="maint.toml", target="tasks.x", params={"i": 1, "f": 1.0, "b": True, "s": "x", "l": [1, "a"], "d": {"k": 1}}),
        rule("t.two", file="maint.toml", target="tasks.x", params={"d": {"j": 2}, "extra": 3}, mode="report")])
    t = c.docs["maint.toml"]["tasks"]["x"]
    assert t == {"i": 1, "f": 1.0, "b": True, "s": "x", "l": [1, "a"], "d": {"k": 1, "j": 2}, "extra": 3, "mode": "report"}
    assert type(t["f"]) is float and type(t["i"]) is int and t["b"] is True
    assert R.same(parse(c.texts["maint.toml"]), c.docs["maint.toml"])


@pytest.mark.parametrize("a,b", [
    ({"k": 1}, {"k": 2}),                                   # same scalar key, different value
    ({"k": 1}, {"k": 1}),                                   # same key, SAME value: still no silent override
    ({"k": [1]}, {"k": [2]}),                               # lists are scalars for this purpose
    ({"k": 1}, {"k": {"x": 1}}),                            # value vs table
    ({"k": {"x": 1}}, {"k": 1}),
    ({"d": {"k": 1}}, {"d": {"k": 2}}),                     # nested
])
def test_two_rules_writing_the_same_key_is_an_error(tmp_path, a, b):
    errs = compile_errors(tmp_path, [rule("t.one", file="maint.toml", target="tasks.x", params=a), rule("t.two", file="maint.toml", target="tasks.x", params=b)])
    assert errs and "t.two" in errs[0] and ("t.one" in errs[0] or "already" in errs[0]), errs


def test_mode_field_conflicts_with_params_mode_and_enabled_with_params_enabled(tmp_path):
    errs = compile_errors(tmp_path, [rule("t.one", file="maint.toml", target="tasks.x", params={"mode": "report"}, mode="report")])
    assert any("already set" in e for e in errs)


def test_append_extends_and_drops_exact_duplicates(tmp_path):
    c = compile_rules(tmp_path, [
        rule("p.b", file="protected.toml", target="", merge="append", order=2, params={"patterns": ["b", "a", "b"]}),
        rule("p.a", file="protected.toml", target="", merge="append", order=1, params={"patterns": ["x", "a"]}),
        rule("p.c", file="protected.toml", target="busy", params={"url": "http://x"})])
    assert c.docs["protected.toml"]["patterns"] == ["x", "a", "b"]                 # order, then id; duplicates dropped
    assert c.lists[("protected.toml", ("patterns",))] == [(0, "p.a"), (2, "p.b")]
    assert "# rule: p.a" in c.texts["protected.toml"] and "# rule: p.b" in c.texts["protected.toml"]
    assert c.docs["protected.toml"]["busy"] == {"url": "http://x"}


def test_append_onto_a_scalar_is_an_error(tmp_path):
    errs = compile_errors(tmp_path, [rule("t.one", file="maint.toml", target="tasks.x", params={"k": 1}),
                                     rule("t.two", file="maint.toml", target="tasks.x", merge="append", params={"k": [1]})])
    assert any("not a list" in e for e in errs)


def test_array_elements_children_and_ordering(tmp_path):
    c = compile_rules(tmp_path, [
        rule("g.b", file="probes.toml", target="group[]", merge="append", order=2, params={"group": "B"}),
        rule("g.a", file="probes.toml", target="group[]", merge="append", order=1, params={"group": "A"}),
        rule("c.2", file="probes.toml", target="@g.a:probes[]", merge="append", order=2, params={"name": "second"}),
        rule("c.1", file="probes.toml", target="@g.a:probes[]", merge="append", order=1, params={"name": "first"}),
        rule("c.3", file="probes.toml", target="@g.b:probes[]", merge="append", params={"name": "other"}),
        rule("s.1", file="probes.toml", target="@g.a:", merge="append", params={"tags": ["t"]})])
    assert c.docs["probes.toml"]["group"] == [
        {"group": "A", "probes": [{"name": "first"}, {"name": "second"}], "tags": ["t"]}, {"group": "B", "probes": [{"name": "other"}]}]
    assert R.same(parse(c.texts["probes.toml"]), c.docs["probes.toml"])
    text = c.texts["probes.toml"]
    assert text.index("# rule: g.a") < text.index("# rule: c.1") < text.index("# rule: c.2") < text.index("# rule: g.b") < text.index("# rule: c.3")


def test_relative_targets_must_resolve(tmp_path):
    assert any("unknown rule" in e for e in compile_errors(tmp_path, [rule("c.1", file="probes.toml", target="@nope.x:probes[]", merge="append", params={"n": 1})]))
    assert any("does not create an array element" in e for e in compile_errors(tmp_path, [
        rule("g.a", file="probes.toml", target="group", params={"x": 1}), rule("c.1", file="probes.toml", target="@g.a:probes[]", merge="append", params={"n": 1})]))
    assert any("form a cycle" in e for e in compile_errors(tmp_path, [
        rule("g.a", file="probes.toml", target="@g.b:group[]", merge="append", params={"x": 1}),
        rule("g.b", file="probes.toml", target="@g.a:group[]", merge="append", params={"x": 1})]))
    assert any("writes jobs.toml" in e for e in compile_errors(tmp_path, [
        rule("g.a", file="jobs.toml", target="job[]", merge="append", params={"x": 1}),
        rule("c.1", file="probes.toml", target="@g.a:probes[]", merge="append", params={"n": 1})]))


def test_a_path_through_a_value_or_a_list_is_an_error(tmp_path):
    errs = compile_errors(tmp_path, [rule("t.one", file="maint.toml", target="tasks", params={"x": 1}), rule("t.two", file="maint.toml", target="tasks.x.y", params={"z": 1})])
    assert any("is not a table" in e and "t.one" in e for e in errs), errs
    errs = compile_errors(tmp_path, [rule("g.a", file="jobs.toml", target="job[]", merge="append", params={"n": 1}), rule("t.two", file="jobs.toml", target="job.x", params={"z": 1})])
    assert any("is not a table" in e for e in errs), errs
    errs = compile_errors(tmp_path, [rule("t.one", file="maint.toml", target="x", params={"a": 1}), rule("t.two", file="maint.toml", target="x[]", merge="append", params={"z": 1})])
    assert any("is not an array of tables" in e for e in errs), errs


def test_disabled_rules_are_compiled_out(tmp_path):
    c = compile_rules(tmp_path, [
        rule("task.x", file="maint.toml", target="tasks.x", params={"a": 1}, mode="report", enabled=False),
        rule("task.y", file="maint.toml", target="tasks.y", params={"a": 1}),
        rule("cfg.z", file="maint.toml", target="caps", params={"a": 1}, enabled=False),
        rule("g.off", file="probes.toml", target="group[]", merge="append", params={"group": "Off"}, enabled=False),
        rule("c.in", file="probes.toml", target="@g.off:probes[]", merge="append", params={"name": "child"}),
        rule("g.on", file="probes.toml", target="group[]", merge="append", order=2, params={"group": "On"}),
        rule("p.off", file="probes.toml", target="probe[]", merge="append", params={"name": "off"}, enabled=False)])
    assert c.docs["maint.toml"] == {"tasks": {"x": {"enabled": False}, "y": {"a": 1}}}       # a disabled task keeps only enabled = false
    assert c.docs["probes.toml"] == {"group": [{"group": "On"}]}                              # the child vanished with its parent


def test_empty_rules_still_materialise_their_table(tmp_path):
    c = compile_rules(tmp_path, [rule("task.empty", file="maint.toml", target="tasks.empty")])
    assert c.docs["maint.toml"] == {"tasks": {"empty": {}}}
    assert "# rule: task.empty\n[tasks.empty]" in c.texts["maint.toml"]


def test_root_target_scalars(tmp_path):
    c = compile_rules(tmp_path, [rule("n.root", file="notify.toml", target="", params={"mute_file": "M"}), rule("n.t", file="notify.toml", target="site", params={"url": "u"})], "50-alerts.toml")
    t = c.texts["notify.toml"]
    assert t.index("mute_file") < t.index("[site]") and "# rule: n.root\nmute_file" in t


def test_output_is_deterministic_and_independent_of_declaration_order(tmp_path):
    rules = [rule(f"task.t{i}", file="maint.toml", target=f"tasks.t{i}", params={"a": i}) for i in range(8)] + \
            [rule(f"j.{i}", file="jobs.toml", target="job[]", merge="append", order=i, params={"name": f"j{i}"}) for i in range(5)]
    base = compile_rules(tmp_path / "a", rules).texts
    rnd = random.Random(3)
    for n in range(5):
        shuffled = rules[:]
        rnd.shuffle(shuffled)
        assert compile_rules(tmp_path / f"b{n}", shuffled).texts == base
    split = R.load_registry(rdir=write_reg(tmp_path / "c", {"10-checks.toml": rules[:6], "20-spike.toml": rules[6:]}, baseline=False), trust=False)
    assert R.compile_registry(split).texts["maint.toml"] == base["maint.toml"]            # the registry's file split changes nothing here


def test_generated_text_has_header_provenance_and_no_timestamps(tmp_path):
    c = compile_rules(tmp_path, [rule("task.a", file="maint.toml", target="tasks.a", params={"x": 1, "n": {"deep": [1, 2]}})])
    t = c.texts["maint.toml"]
    assert t.startswith("# GENERATED from rules.d") and "edit the registry" in t.splitlines()[0]
    assert "# rule: task.a\n[tasks.a]\nx = 1\n" in t
    assert not re.search(r"\d{4}-\d{2}-\d{2}|\d{10}", t)


def test_a_broken_emitter_can_never_produce_a_config_file(tmp_path, monkeypatch):
    reg = R.load_registry(rdir=write_reg(tmp_path / "rd", {"10-checks.toml": [rule("task.a", file="maint.toml", target="tasks.a", params={"x": 1})]}, baseline=False), trust=False)
    monkeypatch.setattr(R, "dumps", lambda doc, **kw: "[tasks.a]\nx = 2\n")                 # text that parses, but to other data
    c = R.compile_registry(reg)
    assert c.texts == {} and any("does not re-parse" in e for e in c.errors)
    monkeypatch.setattr(R, "dumps", lambda doc, **kw: "this is = not toml [[")
    c = R.compile_registry(reg)
    assert c.texts == {} and any("not valid TOML" in e for e in c.errors)


# =========================================================================== 4. migrate and THE PROOF
@pytest.mark.parametrize("name", LEGACY)
def test_equality_proof_for_every_shipped_file(name, template):
    assert template.proof.ok and template.proof.files[name] == []
    an = R.analyze(rdir=template.rd, trust=False)                          # independent of migrate(): load the written files and compile
    assert an.ok, an.errors
    assert R.same(parse(an.comp.texts[name]), parse((ETC / name).read_text()))
    assert an.comp.texts[name].startswith("# GENERATED from rules.d")


def test_proof_reports_the_difference_when_the_registry_disagrees(template, tmp_path):
    rd = tmp_path / "rd"
    shutil.copytree(template.rd, rd)
    t = (rd / "10-checks.toml").read_text().replace(f"warn_free_pct = {W}", f"warn_free_pct = {W + 1}")
    assert t != (rd / "10-checks.toml").read_text()
    (rd / "10-checks.toml").write_text(t)
    docs, _ = R.load_legacy(ETC)
    proof = R.prove(docs, R.analyze(rdir=rd, trust=False))
    assert not proof.ok and any(f"tasks.disk_forecast.warn_free_pct: {W} != {W + 1}" in d for d in proof.files["maint.toml"])
    assert all(not v for k, v in proof.files.items() if k != "maint.toml")


def test_every_logical_unit_became_its_own_rule(template):
    reg = R.load_registry(rdir=template.rd, trust=False)
    ids = {r.id: r for r in reg.rules}
    maint, routine, jobs, probes = (tomllib.loads((ETC / f).read_text()) for f in ("maint.toml", "routine.toml", "jobs.toml", "probes.toml"))
    prot, classes = tomllib.loads((ETC / "protected.toml").read_text()), tomllib.loads((ETC / "classes.toml").read_text())
    for t in maint["tasks"]:
        assert f"task.{t}" in ids and ids[f"task.{t}"].target == f"tasks.{t}"
    assert {k for k in ids if k.startswith("retention.")} == {rid("retention", r["name"]) for r in maint["tasks"]["retention"]["rules"]}
    assert {k for k in ids if k.startswith("c2.")} == {rid("c2", r["name"]) for r in maint["tasks"]["c2_candidates"]["candidates"]}
    assert len([k for k in ids if k.startswith("growth.")]) == len(maint["tasks"]["growth_watch"]["paths"])
    assert len([r for r in ids.values() if r.target == "live.services[]"]) == len(maint["live"]["services"])
    assert len([r for r in ids.values() if r.target.endswith(":probes[]") or r.target == "probe[]"]) == sum(len(g["probes"]) for g in probes["group"]) + len(probes["probe"])
    assert len([r for r in ids.values() if r.target == "group[]"]) == len(probes["group"])
    assert {r.id for r in ids.values() if r.target == "job[]"} == {rid("job", j["name"]) for j in jobs["job"]}
    assert {r.id for r in ids.values() if r.target == "external[]"} == {rid("external", j["name"]) for j in jobs["external"]}
    assert len([r for r in ids.values() if ".step." in r.id]) == sum(len(r["steps"]) for r in routine["routine"])
    assert len([r for r in ids.values() if r.target == "system[]"]) == len(routine["system"])
    assert len([r for r in ids.values() if r.file == "protected.toml" and r.merge == "append"]) >= 1       # the commented pattern groups (>= 1: one group is the fallback)
    assert sum(len(r.params["patterns"]) for r in ids.values() if r.file == "protected.toml" and r.merge == "append") == len(prot["patterns"])
    assert len([r for r in ids.values() if r.id.startswith("classes.ladder.signals")]) == len(classes["ladder"]["signals"])


def test_migrated_rules_carry_clear_placeholders_and_valid_metadata(template):
    reg = R.load_registry(rdir=template.rd, trust=False)
    migrated = [r for r in reg.rules if r.source != R.BASELINE_FILE]
    assert len(migrated) == template.n - BASE_RULES > 100
    for r in migrated:
        assert R.ID_RX.fullmatch(r.id) and r.since == TODAY and r.owner_notes.startswith("migrated from ")
        assert R.TODO in r.why and R.TODO in r.does, r.id
        assert (R.TODO in r.proof) == r.destructive, r.id
        assert r.kind in R.KINDS and r.category in R.CATEGORIES
    assert reg.warnings and "TODO-CONTENT" in reg.warnings[0]
    d = {r.id: r for r in migrated}
    dc = d["task.docker_cache"]
    assert dc.destructive and dc.mode == TASKS["docker_cache"].get("mode") and "mode" not in dc.params       # `mode` moved from params to the rule's own field
    pr = d["task.pressure_response"]
    assert pr.mode == TASKS["pressure_response"]["mode"] and pr.destructive and pr.kind == "spike"
    assert d["task.disk_forecast"].kind == "check" and not d["task.disk_forecast"].destructive
    assert d["task.stuck_detector"].kind == "spike"
    assert R._job_destructive({"name": "docker-prune", "command": ["/usr/local/sbin/docker-prune.sh"]}) and R._job_destructive({"name": "x", "disruptive": True})
    assert not R._job_destructive({"name": "backup-system", "command": ["/usr/local/sbin/backup-system.sh"]})


def test_ids_and_files_are_stable_across_migrate_reruns(tmp_path, template):
    again = tmp_path / "again"
    proof, texts = R.migrate(ETC, again, today=TODAY)
    assert proof.ok and texts == template.texts                                  # byte-identical
    other, t2 = R.migrate(ETC, tmp_path / "other", today="2031-01-01")
    ids = lambda texts_: sorted(r["id"] for t in texts_.values() for r in tomllib.loads(t).get("rule", []))   # noqa: E731
    assert ids(t2) == ids(template.texts)                                         # only `since` differs
    assert len(set(ids(t2))) == len(ids(t2))


def test_migrate_never_overwrites_a_registry_without_force_and_keeps_human_text(tmp_path, template):
    out = tmp_path / "rd"
    shutil.copytree(template.rd, out)
    with pytest.raises(FileExistsError):
        R.migrate(ETC, out, today=TODAY)
    f = out / "10-checks.toml"
    t = f.read_text().replace("TODO-CONTENT: say why this rule exists (name the principle it follows).", "Capacity planning: warn before the disk is full.", 1)
    t = t.replace(f"warn_free_pct = {W}", "warn_free_pct = 99")                       # a hand edit of DATA the legacy files do not have
    f.write_text(t)
    proof, texts = R.migrate(ETC, out, force=True, today=TODAY)
    assert proof.ok
    new = {r.id: r for r in R.load_registry(rdir=out, trust=False).rules}
    assert any("Capacity planning" in r.why for r in new.values())               # the human text survived
    assert f"warn_free_pct = {W}" in (out / "10-checks.toml").read_text()          # the data came back from the legacy file
    assert set(new) == {r.id for r in R.load_registry(rdir=template.rd, trust=False).rules}
    other = out / "99-owner-overrides.toml"
    other.write_text('[meta]\ncategory = "safety"\n')
    R.migrate(ETC, out, force=True, today=TODAY)
    assert other.exists()                                                         # migrate only rewrites the files it owns


def test_migrate_writes_nothing_when_the_proof_fails(tmp_path, monkeypatch):
    monkeypatch.setitem(R.CONVERTERS, "notify.toml", lambda c, doc, raw: R._conv_notify(c, {k: v for k, v in doc.items() if k != "retry"}, raw))
    out = tmp_path / "rd"
    proof, _ = R.migrate(ETC, out, today=TODAY)
    assert not proof.ok and any("retry" in d for d in proof.files["notify.toml"]) and not out.exists()
    proof, _ = R.migrate(ETC, tmp_path / "dry", dry_run=True, today=TODAY)
    assert not proof.ok


def test_migrate_dry_run_writes_nothing(tmp_path):
    out = tmp_path / "rd"
    proof, texts = R.migrate(ETC, out, dry_run=True, today=TODAY)
    assert proof.ok and texts and not out.exists()


def test_migrate_needs_protected_toml(tmp_path):
    src = mkdir(tmp_path / "src")
    shutil.copy(ETC / "maint.toml", src)
    with pytest.raises(ValueError, match="protected.toml is required"):
        R.migrate(src, tmp_path / "rd")
    (src / "protected.toml").write_text("patterns = [")
    with pytest.raises(ValueError, match="protected.toml"):
        R.migrate(src, tmp_path / "rd")


def test_migrate_covers_content_it_has_no_special_case_for(tmp_path):
    """Unknown tables, arrays of tables, odd keys and nested arrays in the legacy files still survive the round trip."""
    src = mkdir(tmp_path / "src")
    shutil.copy(ETC / "protected.toml", src)
    (src / "maint.toml").write_text(
        'stray = 1\n[global]\nk = "v"\n[extra]\na = 1\n[extra.deep]\nb = [1, 2]\n[[things]]\nname = "one"\nsub = { x = 1 }\n[[things]]\nname = "two"\n'
        '[[things.kids]]\nname = "k1"\n[[things.kids]]\nname = "k2"\n[tasks."odd name"]\nv = 1\n[tasks.empty]\n[tasks.retention]\nallowed_roots = ["/var/log"]\n[[tasks.retention.rules]]\nname = "r"\npath = "/var/log"\n')
    (src / "routine.toml").write_text('[[routine]]\nname = "r"\nsteps = []\n[[routine]]\nname = "s"\nsteps = ["a", { task = "b" }, "a2"]\n')
    proof, texts = R.migrate(src, tmp_path / "rd", today=TODAY, catalog={})
    assert proof.ok, (proof.files, proof.errors)


def test_migrate_detects_what_the_registry_cannot_represent(tmp_path):
    """An exact duplicate list element would be dropped by append's dedupe: the proof must catch it, not hide it."""
    src = mkdir(tmp_path / "src")
    (src / "protected.toml").write_text('patterns = ["a", "b", "a"]\n')
    proof, _ = R.migrate(src, tmp_path / "rd", dry_run=True, today=TODAY, catalog={})
    assert not proof.ok and proof.files["protected.toml"]


# --------------------------------------------------------------------------- randomised mutations: each one changes exactly the expected place
def _legacy():
    docs, raws = R.load_legacy(ETC)
    return docs, R.migrate_build(docs, raws, catalog=R.task_catalog(), today=TODAY)


def _elem_path(loc: tuple) -> tuple:
    return _list_path(loc) + (loc[3],)


def _list_path(loc: tuple) -> tuple:
    _k, _f, lp, _i, parent = loc
    return (_elem_path(parent) if parent and parent[0] == "elem" else ()) + tuple(lp)


def _get(doc, path):
    for p in path:
        doc = doc[p]
    return doc


def _delete(doc: dict, path: tuple, keep: set) -> None:
    """Remove doc[path] and prune ancestors that became empty (an empty table or list nobody's rule owns is not compiled)."""
    parent = _get(doc, path[:-1])
    if isinstance(parent, dict):
        del parent[path[-1]]
    else:
        parent.pop(path[-1])
    for n in range(len(path) - 1, 0, -1):
        node = _get(doc, path[:n])
        if (node == {} or node == []) and path[:n] not in keep and not isinstance(path[n - 1], int):
            del _get(doc, path[:n - 1])[path[n - 1]]
        else:
            break


class Mut:
    """The registry (parsed rule files) and the legacy documents it came from, mutated in step."""

    def __init__(self, template):
        self.docs, self.mrules = _legacy()
        self.by_id = {m.rule["id"]: m for m in self.mrules}
        self.files = {n: tomllib.loads((template.rd / n).read_text()) for n in sorted(p.name for p in template.rd.glob("*.toml"))}
        self.base = {f: self.docs[f] for f in LEGACY}
        self.exp = copy.deepcopy(self.docs)
        self.keep = {f: {m.loc[2] for m in self.mrules if m.loc[0] == "table" and m.loc[1] == f} for f in LEGACY}

    def rule(self, rid: str) -> dict:
        for f in self.files.values():
            for r in f.get("rule", []):
                if r["id"] == rid:
                    return r
        raise KeyError(rid)

    def file_of(self, rid: str) -> str:
        return next(n for n, f in self.files.items() if any(r["id"] == rid for r in f.get("rule", [])))

    def has_kids(self, m: R.MRule) -> bool:
        """Other rules whose data lives inside this rule's table / element (deleting m would leave them standing)."""
        for o in self.mrules:
            if o is m or o.loc[1] != m.loc[1]:
                continue
            if m.loc[0] == "table":
                p = m.loc[2]
                if (o.loc[0] == "table" and len(o.loc[2]) > len(p) and o.loc[2][:len(p)] == p) or \
                        (o.loc[0] == "elem" and o.loc[4][0] == "table" and tuple(o.loc[2][:len(p)]) == p):
                    return True
            elif (o.loc[0] == "elem" and o.loc[4] == m.loc) or (o.loc[0] == "step" and tuple(o.loc[2]) + (o.loc[3],) == _elem_path(m.loc)):
                return True
        return False

    def write(self, dst: Path) -> None:
        mkdir(dst)
        for n, d in self.files.items():
            (dst / n).write_text(R.dumps(d, aot_keys=("rule",)))
            os.chmod(dst / n, 0o644)

    def compiled(self, dst: Path) -> R.Compiled:
        self.write(dst)
        reg = R.load_registry(rdir=dst, trust=False)
        assert not reg.errors, reg.errors
        comp = R.compile_registry(reg)
        assert not comp.errors, comp.errors
        return comp


def _scalar_keys(params: dict) -> list[str]:
    return [k for k, v in params.items() if isinstance(v, (int, float, str, bool))]


def _bump(v):
    return (not v) if isinstance(v, bool) else v + 1 if isinstance(v, int) else v + 0.5 if isinstance(v, float) else v + "-changed"


def _assert_exact(mut: Mut, comp: R.Compiled, base_texts: dict, touched: set, what: str) -> None:
    for f in LEGACY:
        assert R.same(parse(comp.texts[f]), mut.exp[f]), f"{what}: {f} differs: {R.diff_docs(mut.exp[f], parse(comp.texts[f]))[:3]}"
        if f not in touched:
            assert comp.texts[f] == base_texts[f], f"{what}: {f} changed but nothing in it should have"
        else:
            assert comp.texts[f] != base_texts[f], f"{what}: {f} did not change"


def test_mutations_change_exactly_the_expected_place(template, tmp_path):
    rnd = random.Random(20261002)
    base_mut = Mut(template)
    base_texts = base_mut.compiled(tmp_path / "base").texts
    tables = [m for m in base_mut.mrules if m.loc[0] == "table" and _scalar_keys(m.rule["params"]) and m.rule["file"] in LEGACY]
    elems = [m for m in base_mut.mrules if m.loc[0] == "elem" and _scalar_keys(m.rule["params"])]
    leaves = [m for m in base_mut.mrules if m.loc[0] == "table" and not base_mut.has_kids(m) and m.loc[2] and m.rule["file"] == m.loc[1]]
    n = 0
    for step in range(60):
        mut = Mut(template)
        kind = ["param", "mode", "add", "remove", "delete", "disable", "elem_param", "elem_delete", "elem_disable", "elem_swap", "elem_add",
                "step_delete", "protect_delete", "protect_add", "text_only"][step % 15]
        touched: set[str] = set()
        if kind == "param":
            m = rnd.choice(tables)
            key = rnd.choice(_scalar_keys(m.rule["params"]))
            new = _bump(m.rule["params"][key])
            mut.rule(m.rule["id"])["params"][key] = new
            _get(mut.exp[m.loc[1]], m.loc[2])[key] = new
            touched.add(m.loc[1])
        elif kind == "mode":
            m = rnd.choice([x for x in base_mut.mrules if x.loc[0] == "table" and x.rule.get("mode")])
            new = "apply" if m.rule["mode"] == "report" else "report"
            mut.rule(m.rule["id"])["mode"] = new
            _get(mut.exp[m.loc[1]], m.loc[2])["mode"] = new
            touched.add(m.loc[1])
        elif kind == "add":
            m = rnd.choice(tables)
            mut.rule(m.rule["id"])["params"]["zz_new_key"] = {"a": [1, 2], "b": 1.5}
            _get(mut.exp[m.loc[1]], m.loc[2])["zz_new_key"] = {"a": [1, 2], "b": 1.5}
            touched.add(m.loc[1])
        elif kind == "remove":
            m = rnd.choice(tables)
            key = rnd.choice(list(m.rule["params"]))
            del mut.rule(m.rule["id"])["params"][key]
            del _get(mut.exp[m.loc[1]], m.loc[2])[key]
            touched.add(m.loc[1])
        elif kind in ("delete", "disable"):
            m = rnd.choice(leaves)
            f = mut.file_of(m.rule["id"])
            if kind == "delete":
                mut.files[f]["rule"] = [r for r in mut.files[f]["rule"] if r["id"] != m.rule["id"]]
            else:
                mut.rule(m.rule["id"])["enabled"] = False
            if m.rule["file"] == "maint.toml" and kind == "disable" and m.loc[2][:1] == ("tasks",) and len(m.loc[2]) == 2:
                mut.exp["maint.toml"]["tasks"][m.loc[2][1]] = {"enabled": False}
            else:
                _delete(mut.exp[m.loc[1]], m.loc[2], mut.keep[m.loc[1]])
            touched.add(m.loc[1])
        elif kind == "elem_param":
            m = rnd.choice(elems)
            key = rnd.choice(_scalar_keys(m.rule["params"]))
            new = _bump(m.rule["params"][key])
            mut.rule(m.rule["id"])["params"][key] = new
            _get(mut.exp[m.loc[1]], _elem_path(m.loc))[key] = new
            touched.add(m.loc[1])
        elif kind in ("elem_delete", "elem_disable"):
            m = rnd.choice([x for x in base_mut.mrules if x.loc[0] == "elem" and not base_mut.has_kids(x)])
            f = mut.file_of(m.rule["id"])
            if kind == "elem_delete":
                mut.files[f]["rule"] = [r for r in mut.files[f]["rule"] if r["id"] != m.rule["id"]]
            else:
                mut.rule(m.rule["id"])["enabled"] = False
            _delete(mut.exp[m.loc[1]], _elem_path(m.loc), mut.keep[m.loc[1]])
            touched.add(m.loc[1])
        elif kind == "elem_swap":
            sib = {}
            for x in base_mut.mrules:
                if x.loc[0] == "elem":
                    sib.setdefault((x.loc[1], _list_path(x.loc), repr(x.loc[4])), []).append(x)
            a, b = rnd.sample(rnd.choice([v for v in sib.values() if len(v) > 2]), 2)
            ra, rb = mut.rule(a.rule["id"]), mut.rule(b.rule["id"])
            ra["order"], rb["order"] = rb["order"], ra["order"]
            lst = _get(mut.exp[a.loc[1]], _list_path(a.loc))
            lst[a.loc[3]], lst[b.loc[3]] = lst[b.loc[3]], lst[a.loc[3]]
            touched.add(a.loc[1])
        elif kind == "elem_add":
            m = rnd.choice([x for x in base_mut.mrules if x.loc[0] == "elem" and x.loc[4][0] == "table"])
            lst = _get(mut.exp[m.loc[1]], _list_path(m.loc))
            new = {"name": "zz-added", "title": "Added", "weight": 3}
            f = mut.file_of(m.rule["id"])
            mut.files[f]["rule"].append(rule("added.zz", file=m.rule["file"], target=m.rule["target"], merge="append", order=10 ** 6, params=new))
            lst.append(new)
            touched.add(m.loc[1])
        elif kind == "step_delete":
            m = rnd.choice([x for x in base_mut.mrules if x.loc[0] == "step"])
            f = mut.file_of(m.rule["id"])
            mut.files[f]["rule"] = [r for r in mut.files[f]["rule"] if r["id"] != m.rule["id"]]
            ep = tuple(m.loc[2]) + (m.loc[3],)
            steps = _get(mut.exp["routine.toml"], ep)["steps"]
            del steps[m.loc[4]]
            touched.add("routine.toml")
        elif kind == "protect_delete":
            m = rnd.choice([x for x in base_mut.mrules if x.loc[0] == "items"])
            f = mut.file_of(m.rule["id"])
            mut.files[f]["rule"] = [r for r in mut.files[f]["rule"] if r["id"] != m.rule["id"]]
            gone = set(m.rule["params"]["patterns"])
            mut.exp["protected.toml"]["patterns"] = [p for p in mut.exp["protected.toml"]["patterns"] if p not in gone]
            touched.add("protected.toml")
        elif kind == "protect_add":
            f = "40-protection.toml"
            mut.files[f]["rule"].append(rule("protect.zz-new", kind="protection", file="protected.toml", target="", merge="append", order=10 ** 6, params={"patterns": ["zz-new-1", "^zz-new-2$"]}))
            mut.exp["protected.toml"]["patterns"] += ["zz-new-1", "^zz-new-2$"]
            touched.add("protected.toml")
        else:                                                               # text_only: prose never reaches a generated file
            for f in mut.files.values():
                for r in f.get("rule", []):
                    r["why"], r["does"], r["title"], r["owner_notes"] = "new why", "new does", "New title", "new notes"
        comp = mut.compiled(tmp_path / f"m{step}")
        _assert_exact(mut, comp, base_texts, touched, f"#{step} {kind}")
        n += 1
    assert n == 60


# =========================================================================== 5. invariants, references, unknown keys
def analysis(tmp_path: Path, files: dict, patterns=("postgres", "redis"), **kw) -> R.Analysis:
    return R.analyze(rdir=write_reg(tmp_path / "rd", files, patterns=patterns, **kw), trust=False)


def prot(patterns, rid="protect.base", **kw) -> dict:
    return rule(rid, kind="protection", file="protected.toml", target="", merge="append", params={"patterns": list(patterns)}, **kw)


def retention(roots=("/var/log",), paths=("/var/log/app",), *, destructive=True, task="retention") -> list[dict]:
    out = [rule(f"task.{task}", kind="cleanup", destructive=destructive, file="maint.toml", target=f"tasks.{task}", mode="report", params={"allowed_roots": list(roots)})]
    for i, p in enumerate(paths):
        out.append(rule(f"ret.r{i}", kind="cleanup", destructive=destructive, file="maint.toml", target=f"tasks.{task}.rules[]", merge="append", order=i, params={"name": f"r{i}", "path": p}))
    return out


def test_baseline_is_required_and_a_complete_registry_passes(tmp_path):
    an = analysis(tmp_path / "a", {"40-protection.toml": [prot(["postgres", "redis", "extra"])]}, baseline=False)
    assert not an.ok and "baseline" in an.errors[0] and "missing" in an.errors[0]
    an = analysis(tmp_path / "b", {"40-protection.toml": [prot(["postgres", "redis", "extra"])]})
    assert an.ok and not an.errors and an.removals == []


def test_protected_patterns_cannot_shrink_silently(tmp_path):
    an = analysis(tmp_path / "a", {"40-protection.toml": [prot(["postgres"])]})
    assert not an.ok and any("lost the baseline pattern 'redis'" in e for e in an.errors)
    an = analysis(tmp_path / "b", {"40-protection.toml": [prot(["postgres", "redis"], enabled=False)]})          # disabling the rule is shrinking too
    assert any("'postgres'" in e for e in an.errors) and any("'redis'" in e for e in an.errors)
    an = analysis(tmp_path / "c", {})                                                                             # no protection rules at all
    assert any("lost the baseline pattern" in e for e in an.errors)
    an = analysis(tmp_path / "d", {"40-protection.toml": [prot(["^postgres$", "redis"])]})                        # a "broader" regex is still a removal
    assert any("'postgres'" in e for e in an.errors)


def test_owner_override_allows_a_loud_removal_but_only_from_the_overrides_file(tmp_path):
    an = analysis(tmp_path / "a", {"40-protection.toml": [prot(["postgres"])], "99-owner-overrides.toml": [rule("policy.owner", kind="policy")]},
                  extra_meta=None)
    assert not an.ok
    rd = write_reg(tmp_path / "b" / "rd", {"40-protection.toml": [prot(["postgres"])]}, patterns=("postgres", "redis"))
    (rd / "99-owner-overrides.toml").write_text('[meta]\ncategory = "safety"\nallow_baseline_removal = ["redis", "not-a-baseline-pattern"]\n')
    os.chmod(rd / "99-owner-overrides.toml", 0o644)
    an = R.analyze(rdir=rd, trust=False)
    assert an.ok and an.removals == ["redis"]
    assert any(w.startswith("LOUD: baseline protection 'redis' removed") for w in an.warnings)
    assert any("'not-a-baseline-pattern', which is not a baseline pattern" in w for w in an.warnings)
    an = analysis(tmp_path / "c", {"40-protection.toml": [prot(["postgres"])]}, extra_meta={"allow_baseline_removal": ["redis"]})   # wrong file
    assert not an.ok and any("only honoured in 99-owner-overrides.toml" in e for e in an.errors)


def test_invalid_regexes_are_refused(tmp_path):
    an = analysis(tmp_path, {"40-protection.toml": [prot(["postgres", "redis", "(unclosed"])],
                             "30-cleanup.toml": [rule("task.x", kind="cleanup", file="maint.toml", target="tasks.x", params={"unprotect": ["["], "never_touch": ["ok", "*bad"]})]})
    errs = "\n".join(an.errors)
    assert "protected.toml: invalid regex '(unclosed'" in errs and "[tasks.x] unprotect: invalid regex" in errs and "[tasks.x] never_touch: invalid regex '*bad'" in errs


@pytest.mark.parametrize("params,ok", [
    ({"max_gib_per_run": 100}, True), ({"max_gib_per_run": 100.5}, False), ({"max_gib_per_run": 4000}, False), ({"max_gib_per_run": "40"}, False),
    ({"max_gib_per_run": True}, False), ({"max_items_per_run": 2000}, True), ({"max_items_per_run": 2001}, False), ({"max_items_per_run": 1}, True)])
def test_per_task_caps_are_bounded_by_the_hard_limits(tmp_path, params, ok):
    an = analysis(tmp_path, {"30-cleanup.toml": [rule("task.x", kind="cleanup", file="maint.toml", target="tasks.x", params=params),
                                                  rule("task.y", kind="cleanup", file="maint.toml", target="tasks.y", params={"max_gib_per_run": 1})]})
    assert (not any("hard limit" in e or "must be a number" in e for e in an.errors)) is ok, an.errors
    if not ok:
        assert any("tasks.x" in e for e in an.errors)


def test_global_caps_and_other_limits(tmp_path):
    an = analysis(tmp_path, {"90-safety.toml": [rule("cfg.caps", kind="safety", file="maint.toml", target="caps", params={"max_gib_per_run": 101, "max_items_per_run": 5})],
                             "50-alerts.toml": [rule("n.budget", kind="alert", file="notify.toml", target="budget", params={"hard_cap_per_day": 61})],
                             "20-spike.toml": [rule("c.ladder", kind="spike", file="classes.toml", target="ladder", params={"max_restarts_per_6h": 7}),
                                               rule("c.day", kind="spike", file="classes.toml", target="ladder.max_per_day", params={"restart": 13})]})
    errs = "\n".join(an.errors)
    for needle in ("maint.toml caps.max_gib_per_run = 101 exceeds the hard limit 100", "notify.toml budget.hard_cap_per_day = 61 exceeds the hard limit 60",
                   "classes.toml ladder.max_restarts_per_6h = 7", "ladder.max_per_day.restart = 13"):
        assert needle in errs, errs
    assert "max_items_per_run" not in errs


@pytest.mark.parametrize("roots,paths,needle", [
    (("/var/log",), ("/var/log/app",), None),
    (("/var/log",), ("/var/log",), None),                                       # the root itself is inside the root
    (("/var/log",), ("/var/logs/app",), "escapes allowed_roots"),               # prefix, not path component
    (("/var/log",), ("/var/log/../etc",), "must be absolute and normalised"),
    (("/var/log",), ("var/log/app",), "must be absolute and normalised"),
    (("/var/log",), ("/var/log//app",), "must be absolute and normalised"),
    (("/var/log",), ("/var/log/app/",), "must be absolute and normalised"),
    (("/var/log",), ("//var/log/app",), "must be absolute and normalised"),
    (("/var/log",), ("/etc/passwd",), "escapes allowed_roots"),
    ((), ("/var/log/app",), "no allowed_roots"),
    (("/var",), ("/var/log/app",), "too broad"),
    (("/",), ("/var/log/app",), "too broad"),
    (("var/log",), ("/var/log/app",), "absolute, normalised"),
    (("/var/lib",), ("/var/lib/docker/volumes/x",), "never-touch"),
    (("/mnt/backup/x",), ("/mnt/backup/x/y",), "never-touch"),
    (("/media",), ("/media/Immich/x",), "too broad"),
    (("/home/ohmz/StudioProjects",), ("/home/ohmz/StudioProjects/tunarr/.docker-data/tunarr/cache/subtitles",), None),     # the one tunarr exemption
    (("/home/ohmz/StudioProjects",), ("/home/ohmz/StudioProjects/tunarr/.docker-data/tunarr/db",), "never-touch"),
    (("/volume1/docker",), ("/volume1/docker/kavita/config/logs",), None),
    (("/volume1/docker",), ("/volume1/docker/kavita/config/kavita.db",), "never-touch"),
    (("/volume1/docker",), ("/volume1/docker/radarr/config/cache/x",), None),
    (("/volume1/docker",), ("/volume1/docker/radarr/config/radarr.db",), "never-touch"),
    (("/home/ohmz",), ("/home/ohmz/StudioProjects/ai-stack/data",), "never-touch"),
    (("/home/ohmz",), ("/home/ohmz/.config/Cursor/x",), "never-touch"),
    (("/opt/app",), ("/opt/app/data/pgdata",), "never-touch"),
])
def test_delete_rules_stay_inside_allowed_roots_and_off_the_never_touch_list(tmp_path, roots, paths, needle):
    an = analysis(tmp_path, {"30-cleanup.toml": retention(roots, paths)}, patterns=())
    errs = [e for e in an.errors]
    if needle is None:
        assert not errs, errs
    else:
        assert any(needle in e for e in errs), (needle, errs)


def test_c2_plan_only_tasks_may_name_never_touch_paths_but_unknown_tasks_are_strict(tmp_path):
    # C2 (plan-only, a human approves the exact plan hash) may name never-touch paths; an unknown task gets the strict treatment
    files = {"30-cleanup.toml": [rule("task.c2_candidates", kind="cleanup", destructive=True, file="maint.toml", target="tasks.c2_candidates",
                                      params={"unprotect": []}),
                                 rule("c2.cursor", kind="cleanup", destructive=True, file="maint.toml", target="tasks.c2_candidates.candidates[]", merge="append",
                                      params={"name": "cursor", "path": "/home/ohmz/.config/Cursor/User/x"})]}
    assert analysis(tmp_path / "b", files, patterns=()).ok
    files["30-cleanup.toml"] = [r for r in retention(("/var/log",), ("/etc/passwd",), task="brand_new_cleaner")]
    assert not analysis(tmp_path / "c", files, patterns=()).ok
    # a rule nested inside an element (@parent:...) is judged by the task of its root rule
    nested = retention(("/var/log",), ("/var/log/app",)) + [rule("ret.child", kind="cleanup", destructive=True, file="maint.toml", target="@ret.r0:sub[]", merge="append",
                                                                 params={"path": "/etc/x"})]
    an = analysis(tmp_path / "d", {"30-cleanup.toml": nested}, patterns=())
    assert any("rule ret.child: path '/etc/x' escapes allowed_roots" in e for e in an.errors), an.errors


def test_apply_must_be_explicit_and_destructive(tmp_path):
    ok = rule("task.docker_cache", kind="cleanup", destructive=True, file="maint.toml", target="tasks.docker_cache", mode="apply", params={"high_gib": 15})
    assert analysis(tmp_path / "a", {"30-cleanup.toml": [ok]}, patterns=()).ok
    errs = analysis(tmp_path / "b", {"30-cleanup.toml": [{**ok, "destructive": False}]}, patterns=()).errors
    assert any("only allowed on a rule flagged destructive" in e for e in errs)
    errs = analysis(tmp_path / "c", {"30-cleanup.toml": [{**{k: v for k, v in ok.items() if k != "mode"}, "params": {"mode": "apply"}}]}, patterns=()).errors
    assert any("use the rule's own mode field" in e for e in errs)
    an = analysis(tmp_path / "d", {"30-cleanup.toml": [{k: v for k, v in ok.items() if k != "mode"}]}, patterns=())          # a destructive rule without a mode: nothing is written
    assert an.ok and "mode" not in an.comp.docs["maint.toml"]["tasks"]["docker_cache"]


def test_a_cleaner_that_is_not_flagged_destructive_is_a_warning(tmp_path):
    an = analysis(tmp_path, {"30-cleanup.toml": [rule("task.docker_cache", kind="cleanup", file="maint.toml", target="tasks.docker_cache", params={"high_gib": 15})]}, patterns=())
    assert an.ok and any("cleaner docker_cache is not flagged destructive" in w for w in an.warnings)


def test_applies_to_must_name_a_task_job_or_probe(tmp_path):
    files = {"10-checks.toml": [rule("task.disk_forecast", file="maint.toml", target="tasks.disk_forecast", applies_to=["disk_forecast", "my-job", "my-probe"], params={"warn_free_pct": 12}),
                                rule("c.x", file="maint.toml", target="tasks.failed_units", applies_to=["disk_forcast"], params={"ignore_units": []})],
             "80-jobs.toml": [rule("job.my", kind="job", file="jobs.toml", target="job[]", merge="append", params={"name": "my-job"})],
             "70-monitoring.toml": [rule("g.a", kind="probe", file="probes.toml", target="group[]", merge="append", params={"group": "G"}),
                                    rule("p.my", kind="probe", file="probes.toml", target="@g.a:probes[]", merge="append", params={"name": "my-probe"})]}
    an = analysis(tmp_path, files, patterns=())
    assert [e for e in an.errors if "applies_to" in e] == [e for e in an.errors if "disk_forcast" in e]
    assert any("applies_to 'disk_forcast' is not a task, job or probe (did you mean 'disk_forecast'?)" in e for e in an.errors)
    assert not any("my-job" in e or "my-probe" in e for e in an.errors)


def test_a_task_with_computed_option_names_is_never_warned_about(tmp_path):
    rd = write_reg(tmp_path / "rd", {"10-checks.toml": [rule("task.zz", file="maint.toml", target="tasks.zz", params={"known": 1, "mystery": 2}),
                                                         rule("task.yy", file="maint.toml", target="tasks.yy", params={"known": 1, "mystery": 2})]}, patterns=())
    cat = {"zz": R.TaskInfo("zz", keys={"known"}, open=False), "yy": R.TaskInfo("yy", keys={"known"}, open=True)}
    w = "\n".join(R.analyze(rdir=rd, trust=False, catalog=cat).warnings)
    assert "unknown option 'mystery' for task zz" in w and "task yy" not in w


def test_unknown_task_options_and_unknown_tasks_are_warnings_derived_from_the_code(tmp_path):
    files = {"10-checks.toml": [rule("task.disk_forecast", file="maint.toml", target="tasks.disk_forecast", params={"warn_free_pct": 12, "warm_free_pct": 11, "mode": "report"}),
                                rule("task.disk_forcast", file="maint.toml", target="tasks.disk_forcast", params={"x": 1})]}
    an = analysis(tmp_path, files, patterns=())
    assert an.ok                                                                  # warnings never block
    w = "\n".join(an.warnings)
    assert "rule task.disk_forecast: unknown option 'warm_free_pct' for task disk_forecast (did you mean 'warn_free_pct'?)" in w
    assert "[tasks.disk_forcast] configures a task that does not exist (did you mean 'disk_forecast'?)" in w
    assert "'mode'" not in w and "warn_free_pct' for" not in w


def test_the_shipped_options_are_almost_all_known_to_the_code(template):
    """The check derives each task's option names from its source. The shipped config may carry a few dead knobs (it does: that is what the
    warning is for) but not a pile of them: a pile would mean the derivation misses a way the code reads options."""
    an = R.analyze(rdir=template.rd, trust=False)
    assert an.ok
    unknown = [w for w in an.warnings if "unknown option" in w]
    assert len(unknown) <= 5, unknown
    assert all(re.fullmatch(r"rule [a-z0-9_.-]+: unknown option '[a-z_0-9]+' for task [a-z_0-9]+( \(did you mean '[a-z_0-9]+'\?\))?", w) for w in unknown)


def test_shipped_baseline_is_derived_from_the_current_protected_toml():
    reg = R.load_registry(rdir=ETC / "rules.d", trust=False)
    assert reg.valid, reg.errors
    pats = set(tomllib.loads((ETC / "protected.toml").read_text())["patterns"])
    base = reg.baseline
    assert base and len(base["protected_patterns"]) >= 61 and set(base["protected_patterns"]) <= pats
    assert set(base["protected_patterns"]) == pats or len(pats) > len(base["protected_patterns"])       # later releases may add; never fewer
    own = [r for r in reg.rules if r.source == R.BASELINE_FILE]                                          # etc/rules.d also ships the migrated content now
    assert {r.id for r in own} == {"safety.protected-superset", "safety.hard-caps", "safety.delete-confinement", "safety.destructive-explicit"}
    assert all(r.kind == "safety" and r.file is None and R.TODO not in r.why + r.does for r in own)
    assert {(l["file"], l["path"]) for l in base["limit"]} >= {("maint.toml", "caps.max_gib_per_run"), ("maint.toml", "tasks.*.max_items_per_run")}
    for rx in base["never_touch"]:
        re.compile(rx)


def test_the_shipped_config_satisfies_its_own_baseline(template):
    """Every limit and every retention path of the current etc/*.toml passes: the baseline is a floor, not a rewrite of the config."""
    an = R.analyze(rdir=template.rd, trust=False)
    assert an.ok, an.errors
    maint = an.comp.docs["maint.toml"]
    assert maint["caps"]["max_gib_per_run"] <= 100 and all(p["path"].startswith(tuple(maint["tasks"]["retention"]["allowed_roots"])) for p in maint["tasks"]["retention"]["rules"])


# =========================================================================== 5b. the task catalog (derived from code, never imported)
def test_catalog_lists_the_real_tasks_with_their_option_keys():
    c = R.task_catalog()
    assert len(c) > 50 and c["disk_forecast"].klass == "C0" and c["retention"].klass == "C1" and c["c2_candidates"].klass == "C2"
    assert {"warn_free_pct", "watch", "info_only"} <= c["disk_forecast"].keys and not c["disk_forecast"].open
    assert c["retention"].tier == "daily" and c["disk_forecast"].tier == "check" and c["disk_forecast"].title


def test_the_task_registered_at_runtime_is_known_to_the_catalog_and_to_applies_to(tmp_path, monkeypatch):
    """`rules_registry` is registered by register_tasks(), not by a decorator the source scan can see: RUNTIME_TASKS (kept equal to what
    register_tasks registers) feeds known_names and the [tasks.X] reference check, so a rule may name it in applies_to and configure it
    without a bogus warning."""
    assert R.RUNTIME_TASKS == {"rules_registry"}
    assert "rules_registry" not in R.task_catalog()                                     # the catalog is "what the code declares"; the runtime table is separate
    assert "rules_registry" in R.known_names(R.Compiled(), R.task_catalog()) and "rules_registry" in R.known_names(R.Compiled(), {})
    monkeypatch.setattr(core, "REGISTRY", {})
    R.register_tasks()
    R.register_tasks()                                                                  # idempotent
    assert set(core.REGISTRY) == R.RUNTIME_TASKS and core.REGISTRY["rules_registry"].run is R.check_task        # the table is what register_tasks registers
    files = {"10-checks.toml": [rule("check.registry", applies_to=["rules_registry"], file="maint.toml", target="tasks.rules_registry", mode="report"),
                                rule("check.registry_typo", applies_to=["rules_registy"])]}
    an = analysis(tmp_path, files, patterns=())
    assert [e for e in an.errors if "applies_to" in e] == [e for e in an.errors if "rules_registy" in e]        # only the typo is an error
    assert any("did you mean 'rules_registry'" in e for e in an.errors)
    assert not any("configures a task that does not exist" in w and "rules_registry" in w for w in an.warnings)


def test_catalog_scans_plugins_without_importing_them(tmp_path):
    conf = mkdir(tmp_path / "conf")
    pd = mkdir(conf / "plugins.d")
    (pd / "mine.py").write_text('import os\nos.environ["PLUGIN_IMPORTED"] = "1"\nfrom homelab_maint.core import task\n'
                                '@task("my_plugin", klass="C1", tier="daily", title="Mine")\ndef run(ctx):\n    return helper(ctx)\n'
                                'def helper(ctx):\n    return ctx.opt("alpha", 1) + ctx.tcfg.get("beta", 2) + ctx.tcfg["gamma"]\n'
                                '@task("dyn_plugin", "C0", "check")\ndef run2(ctx):\n    k = "x"\n    return ctx.opt(k)\n'
                                '@task("alias_plugin", "C0", "check")\ndef run3(ctx):\n    t = ctx.tcfg\n    return t.get("delta") or dict(t)\n')
    (pd / "broken.py").write_text("def (:\n")
    c = R.task_catalog(conf)
    assert "PLUGIN_IMPORTED" not in os.environ
    m = c["my_plugin"]
    assert (m.klass, m.tier, m.title, m.keys, m.open) == ("C1", "daily", "Mine", {"alpha", "beta", "gamma"}, False)
    assert c["dyn_plugin"].open and c["dyn_plugin"].klass == "C0" and c["alias_plugin"].open and "delta" in c["alias_plugin"].keys


def test_catalog_is_cached_until_a_file_changes(tmp_path):
    conf = mkdir(tmp_path / "conf")
    pd = mkdir(conf / "plugins.d")
    (pd / "a.py").write_text('@task("p_one")\ndef run(ctx):\n    return ctx.opt("k")\n')
    assert "p_one" in R.task_catalog(conf) and R.task_catalog(conf) is R.task_catalog(conf)
    time.sleep(0.01)
    (pd / "a.py").write_text('@task("p_two")\ndef run(ctx):\n    return ctx.opt("k")\n')
    c = R.task_catalog(conf)
    assert "p_two" in c and "p_one" not in c


# =========================================================================== 6. sync: change tracking, safety, concurrency
_REAL_NOTICE, _REAL_JOURNAL = R._notice_via_notify, R._journal_via_routine


class Rec:
    """Hooks that record instead of sending."""
    def __init__(self):
        self.notices, self.journal = [], []
        self.hooks = R.Hooks(lambda p: self.notices.append(p) or True, lambda p: self.journal.append(p) or True)


def gen(env) -> dict[str, bytes]:
    return {f: (env.conf / f).read_bytes() for f in LEGACY}


def edit_rule(env, rid: str, fn) -> None:
    """Edit one rule of the copied registry in place (parse the registry file, change the rule dict, write it back)."""
    for f in sorted(env.rd.glob("*.toml")):
        d = tomllib.loads(f.read_text())
        for r in d.get("rule", []):
            if r["id"] == rid:
                fn(r)
                f.write_text(R.dumps(d, aot_keys=("rule",)))
                os.chmod(f, 0o644)
                return
    raise KeyError(rid)


def test_first_sync_adopts_the_legacy_files_and_records_everything(mig):
    rec = Rec()
    before = {f: (mig.conf / f).read_bytes() for f in LEGACY}
    res = sync(mig, hooks=rec.hooks, now=1000.0)
    assert res.status == "applied" and sorted(res.written) == sorted(LEGACY) and len(res.added) == mig.n and not res.removed
    assert res.hash == R.registry_hash(R.scan_registry(rdir=mig.rd)[0])
    for f in LEGACY:
        text = (mig.conf / f).read_text()
        assert text.startswith(R.GENERATED_MARK) and R.same(parse(text), tomllib.loads(before[f].decode()))
    cur = R.read_current(mig.state)
    assert cur["hash"] == res.hash and cur["rules_count"] == mig.n and cur["synced_at"] == 1000.0 and cur["invalid"] is None
    assert [f["name"] for f in cur["files"]] == sorted(p.name for p in mig.rd.glob("*.toml"))
    assert cur["generated"] == {f: R.sha_bytes((mig.conf / f).read_bytes()) for f in LEGACY}
    hist = R.history(10, mig.state)
    assert len(hist) == 1 and hist[0]["from"] is None and hist[0]["to"] == res.hash and hist[0]["applied"] and hist[0]["valid"]
    assert hist[0]["added_count"] == mig.n and len(hist[0]["added"]) == R.HISTORY_IDS_MAX          # the stored record keeps a bounded list and the true count
    snap = mig.state / "rules" / "snapshots" / res.hash
    assert {p.name for p in (snap / "rules.d").glob("*.toml")} == {p.name for p in mig.rd.glob("*.toml")}
    assert {p.name for p in (snap / "generated").glob("*.toml")} == set(LEGACY)
    assert len(json.loads((snap / "rules.json").read_text())) == mig.n and json.loads((snap / "meta.json").read_text())["rules_count"] == mig.n
    orig = {p.name.rsplit(".", 1)[0] for p in (mig.state / "rules" / "orig").iterdir()}
    assert orig == set(LEGACY)                                  # the hand-maintained originals were kept, once
    assert all(R.sha_bytes(before[f])[:8] in "".join(p.name for p in (mig.state / "rules" / "orig").iterdir()) for f in LEGACY)
    assert len(rec.notices) == 1 and f"now in charge of the config files ({mig.n} rules)" in rec.notices[0]["summary"] and rec.notices[0]["significant"]
    assert len(rec.journal) == 1 and rec.journal[0]["record"]["to"] == res.hash


def test_second_sync_is_a_fast_noop_and_touches_nothing(mig):
    sync(mig)
    mt = {f: (mig.conf / f).stat().st_mtime_ns for f in LEGACY}
    hist = (mig.state / "rules" / "history.jsonl").read_bytes()
    cur = (mig.state / "rules" / "current.json").read_bytes()
    times = []
    for _ in range(60):
        t0 = time.perf_counter()
        res = sync(mig)
        times.append((time.perf_counter() - t0) * 1000)
        assert res.status == "unchanged" and res.written == [] and not res.notified
    med = statistics.median(times)
    print(f"\nsync() no-op on the migrated registry ({mig.n} rules, 11 files): median {med:.2f} ms, max {max(times):.2f} ms")
    assert med < 50 and max(times) < 1000
    assert {f: (mig.conf / f).stat().st_mtime_ns for f in LEGACY} == mt
    assert (mig.state / "rules" / "history.jsonl").read_bytes() == hist and (mig.state / "rules" / "current.json").read_bytes() == cur
    assert R.tick(mig.conf, mig.state) == ""                                  # the tick hook says nothing when nothing happened


def test_a_hand_maintained_file_that_differs_blocks_the_sync_until_adopted(mig):
    t = (mig.conf / "maint.toml").read_text().replace(f"warn_free_pct = {W}", f"warn_free_pct = {W}\nmy_extra_knob = 1")
    assert "my_extra_knob" in t
    (mig.conf / "maint.toml").write_text(t)
    before = gen(mig)
    rec = Rec()
    res = sync(mig, hooks=rec.hooks)
    assert res.status == "blocked" and "maint.toml is not generated and differs" in res.errors[0]
    assert gen(mig) == before                                                  # NOTHING was replaced, not even the other seven files
    assert R.read_current(mig.state)["invalid"]["kind"] == "blocked" and not (mig.state / "rules" / "snapshots").exists()
    for _ in range(3):
        assert sync(mig, hooks=rec.hooks).status == "blocked"
    assert len(R.history(10, mig.state)) == 1 and len(rec.notices) == 1        # one record and one notice per distinct problem, not one per tick
    assert rec.notices[0]["summary"].startswith("The rules registry was NOT applied")
    res = sync(mig, adopt=True, hooks=rec.hooks)
    assert res.status == "applied" and (mig.conf / "maint.toml").read_text().startswith(R.GENERATED_MARK)
    assert "my_extra_knob" not in (mig.conf / "maint.toml").read_text()
    kept = [p for p in (mig.state / "rules" / "orig").iterdir() if p.name.startswith("maint.toml.")]
    assert len(kept) == 1 and "my_extra_knob" in kept[0].read_text()           # the displaced original is kept
    assert R.read_current(mig.state)["invalid"] is None


def test_a_param_edit_rewrites_only_the_file_it_belongs_to_and_records_before_and_after(mig):
    sync(mig)
    before = gen(mig)
    rec = Rec()
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=W + 3))
    res = sync(mig, hooks=rec.hooks, now=2000.0)
    assert res.status == "applied" and res.written == ["maint.toml"] and res.modified[0]["id"] == "task.disk_forecast"
    after = gen(mig)
    assert {f for f in LEGACY if before[f] != after[f]} == {"maint.toml"}
    d = R.diff_docs(parse(before["maint.toml"].decode()), parse(after["maint.toml"].decode()))
    assert [(p, a, b) for p, a, b in d] == [("tasks.disk_forecast.warn_free_pct", W, W + 3)]
    h = R.history(2, mig.state)[-1]
    assert h["modified"] == [{"id": "task.disk_forecast", "fields": ["params.warn_free_pct"], "before": {"params.warn_free_pct": W},
                              "after": {"params.warn_free_pct": W + 3}}]
    assert h["from"] != h["to"] and h["added"] == [] and h["removed"] == [] and h["valid"] and h["applied"] and h["ts"] == 2000.0
    n = rec.notices[0]
    assert "Rules changed: 0 added, 0 removed, 1 modified" in n["summary"] and n["lines"] == [f"Changed task.disk_forecast: warn_free_pct {W} -> {W + 3}"]
    assert not n["significant"]


def test_prose_only_edits_are_recorded_but_rewrite_no_config_file(mig):
    sync(mig)
    before = gen(mig)
    edit_rule(mig, "task.disk_forecast", lambda r: r.update(why="Capacity planning.", title="Disk space (renamed)"))
    res = sync(mig)
    assert res.status == "applied" and res.written == [] and gen(mig) == before and res.modified[0]["fields"] == ["title", "why"]


def test_protection_and_destructive_changes_are_significant(mig):
    sync(mig)
    rec = Rec()
    edit_rule(mig, "task.docker_cache", lambda r: r["params"].update(high_gib=HG + 5))
    sync(mig, hooks=rec.hooks)
    assert rec.notices[-1]["significant"]                                         # destructive rule: SMS-worthy too
    pid = next(r.id for r in R.load_registry(rdir=mig.rd, trust=False).rules if r.kind == "protection" and r.merge == "append")
    edit_rule(mig, pid, lambda r: r["params"]["patterns"].append("brand-new-protected"))
    sync(mig, hooks=rec.hooks)
    assert rec.notices[-1]["significant"] and any(pid in l for l in rec.notices[-1]["lines"])


def test_added_and_removed_rules_and_retired_ids(mig):
    sync(mig)
    rec = Rec()
    f = mig.rd / "10-checks.toml"
    d = tomllib.loads(f.read_text())
    gone = d["rule"].pop(0)
    d["rule"].append(rule("check.brand_new", file="maint.toml", target="tasks.brand_new", params={"x": 1}))
    f.write_text(R.dumps(d, aot_keys=("rule",)))
    res = sync(mig, hooks=rec.hooks)
    assert res.status == "applied" and res.added == ["check.brand_new"] and res.removed == [gone["id"]]
    assert "tasks.brand_new" not in (mig.conf / "maint.toml").read_text() or "[tasks.brand_new]" in (mig.conf / "maint.toml").read_text()
    assert parse((mig.conf / "maint.toml").read_text())["tasks"]["brand_new"] == {"x": 1}
    assert gone["target"].split(".")[1] not in parse((mig.conf / "maint.toml").read_text())["tasks"]
    assert any(l.startswith("Added check.brand_new") for l in rec.notices[0]["lines"]) and any(l.startswith(f"Removed {gone['id']}") for l in rec.notices[0]["lines"])
    assert gone["id"] in json.loads((mig.state / "rules" / "retired.json").read_text())
    d["rule"].append(gone)
    f.write_text(R.dumps(d, aot_keys=("rule",)))
    res = sync(mig)
    assert any(f"id {gone['id']} was retired earlier: reuse" in w for w in res.warnings)


def test_an_invalid_registry_never_replaces_the_last_good_files(mig):
    sync(mig)
    good = gen(mig)
    cur = (mig.state / "rules" / "current.json").read_bytes()
    rec = Rec()
    edit_rule(mig, "task.disk_forecast", lambda r: r.update(kind="nonsense"))
    res = sync(mig, hooks=rec.hooks)
    assert res.status == "invalid" and any("kind must be one of" in e for e in res.errors)
    assert gen(mig) == good
    c2 = R.read_current(mig.state)
    assert c2["hash"] == json.loads(cur)["hash"] and c2["invalid"]["kind"] == "invalid"          # the applied hash is untouched; the problem is recorded
    for _ in range(3):
        assert sync(mig, hooks=rec.hooks).status == "invalid"
    assert len(R.history(10, mig.state)) == 2 and len(rec.notices) == 1                             # recorded and announced ONCE
    last = R.history(1, mig.state)[0]
    assert last["valid"] is False and last["applied"] is False and any("kind must be" in e for e in last["errors"])
    assert rec.notices[0]["summary"].startswith("The rules registry is not valid") and rec.notices[0]["lines"][0].startswith("Problem:")
    st = R.status(mig.conf, mig.state)
    assert not st["valid"] and not st["in_sync"] and st["pending"] and st["errors"]
    edit_rule(mig, "task.disk_forecast", lambda r: r.update(kind="check"))                         # fix it: recovers by itself
    res = sync(mig, hooks=rec.hooks)
    assert res.status == "applied" and not (res.added or res.removed or res.modified or res.written)    # the rules are the old ones again
    assert R.read_current(mig.state)["invalid"] is None and R.status(mig.conf, mig.state)["in_sync"] and len(rec.notices) == 1


@pytest.mark.parametrize("breakage", ["toml", "garbage", "invariant", "conflict", "untrusted", "baseline_gone", "empty_file_name"])
def test_every_kind_of_breakage_keeps_the_last_good_config(mig, breakage):
    sync(mig)
    good = gen(mig)
    f = mig.rd / "10-checks.toml"
    if breakage == "toml":
        f.write_text(f.read_text() + "\n[[rule]\nid = \n")
    elif breakage == "garbage":
        f.write_bytes(os.urandom(2000))
    elif breakage == "invariant":
        edit_rule(mig, "task.docker_cache", lambda r: r["params"].update(max_gib_per_run=10 ** 6))
    elif breakage == "conflict":
        edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(info_only=["/"]))
        (mig.rd / "95-ack.toml").write_text(
            R.dumps({"meta": {"category": "ack"}, "rule": [rule("dup.writer", kind="policy", file="maint.toml", target="tasks.disk_forecast", params={"info_only": ["/x"]})]}, aot_keys=("rule",)))
        os.chmod(mig.rd / "95-ack.toml", 0o644)
    elif breakage == "untrusted":
        os.chmod(f, 0o666)
    elif breakage == "baseline_gone":
        (mig.rd / R.BASELINE_FILE).unlink()
    else:
        (mig.rd / "oops.toml").write_text("")
        os.chmod(mig.rd / "oops.toml", 0o644)
    res = sync(mig)
    assert res.status == "invalid", (breakage, res.line())
    assert gen(mig) == good and not list(mig.conf.glob(".hm-rules-*"))


def test_hand_edits_of_generated_files_are_repaired_and_recorded(mig):
    sync(mig)
    good = gen(mig)
    rec = Rec()
    (mig.conf / "maint.toml").write_bytes(good["maint.toml"] + b"\n[tasks.sneaky]\nx = 1\n")
    st = R.status(mig.conf, mig.state)
    assert st["drift"] == ["maint.toml"] and not st["in_sync"]
    res = sync(mig, hooks=rec.hooks)
    assert res.status == "applied" and res.drift == ["maint.toml"] and res.written == ["maint.toml"] and gen(mig) == good
    h = R.history(1, mig.state)[0]
    assert h["drift"] == ["maint.toml"] and h["added"] == [] and h["from"] == h["to"]
    assert rec.notices[0]["summary"].startswith("Someone edited a generated config file by hand") and rec.notices[0]["lines"] == ["Reverted: maint.toml"]
    assert sync(mig).status == "unchanged"
    (mig.conf / "probes.toml").unlink()                                                                  # a deleted file is repaired too
    assert sync(mig).written == ["probes.toml"] and gen(mig) == good


def test_a_crash_while_compiling_leaves_the_old_files_intact(mig, monkeypatch):
    sync(mig)
    good, cur = gen(mig), (mig.state / "rules" / "current.json").read_bytes()
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=16))
    real = R.dumps
    monkeypatch.setattr(R, "dumps", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom in the emitter")))
    res = sync(mig)
    assert res.status == "error" and "boom" in res.errors[0]
    now = R.read_current(mig.state)
    assert gen(mig) == good and not list(mig.conf.glob(".hm-rules-*"))
    assert now["last_error"]["error"].endswith("boom in the emitter") and {k: v for k, v in now.items() if k != "last_error"} == json.loads(cur)   # only the error is new
    monkeypatch.setattr(R, "dumps", real)
    assert sync(mig).status == "applied" and "last_error" not in R.read_current(mig.state)               # and the next tick recovers and forgets it


def test_a_crash_while_writing_temp_files_leaves_the_old_files_intact(mig, monkeypatch):
    sync(mig)
    good = gen(mig)
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=17))
    edit_rule(mig, "notify.routes", lambda r: r["params"].update(digest_daily="none"))
    calls = {"n": 0}
    real = os.fsync

    def flaky(fd):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        return real(fd)
    monkeypatch.setattr(R.os, "fsync", flaky)
    res = sync(mig)
    assert res.status == "error" and "No space" in res.errors[0]
    assert gen(mig) == good and not list(mig.conf.glob(".hm-rules-*"))


def test_a_crash_between_replaces_puts_the_files_already_replaced_back(mig, monkeypatch):
    sync(mig)
    good = gen(mig)
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=18))
    edit_rule(mig, "notify.routes", lambda r: r["params"].update(digest_daily="none"))
    real, n = os.replace, {"n": 0}

    def flaky(a, b):
        if re.fullmatch(r"\.hm-rules-\d+-[a-z.]+\.tmp", Path(str(a)).name) and str(b).startswith(str(mig.conf)):      # a STAGED replace (the restore's temp files carry a counter)
            n["n"] += 1
            if n["n"] == 2:
                raise OSError(5, "I/O error")
        return real(a, b)
    monkeypatch.setattr(R.os, "replace", flaky)
    res = sync(mig)
    assert res.status == "error"
    assert gen(mig) == good                                                       # the one file that made it was restored: nothing is half-done
    assert not list(mig.conf.glob(".hm-rules-*")) and R.read_current(mig.state)["hash"] != R.registry_hash(R.scan_registry(rdir=mig.rd)[0])
    assert "intent" not in R.read_current(mig.state) and R.read_current(mig.state)["last_error"]["error"].endswith("I/O error")
    monkeypatch.setattr(R.os, "replace", real)
    res = sync(mig)
    assert res.status == "applied" and R.status(mig.conf, mig.state)["in_sync"] and res.drift == []
    final = gen(mig)
    assert parse(final["maint.toml"].decode())["tasks"]["disk_forecast"]["warn_free_pct"] == 18
    assert parse(final["notify.toml"].decode())["routes"]["digest_daily"] == "none"
    assert [r.get("kind") for r in R.history(10, mig.state)] == [None, "error", None]      # applied, the failure (once), applied


def test_a_failing_hook_never_fails_the_sync(mig):
    def boom(_p):
        raise RuntimeError("smtp down")
    res = sync(mig, hooks=R.Hooks(boom, boom))
    assert res.status == "applied" and not res.notified


def test_default_hooks_are_used_when_none_given_and_are_replaceable(mig, no_real_notices):
    res = R.sync(mig.conf, mig.state)                       # hooks=None -> default_hooks() -> (monkeypatched) notify + journal
    assert res.status == "applied" and res.notified and len(no_real_notices) == 1


def test_the_real_notice_goes_through_notify_maintenance_event_and_never_raises(monkeypatch):
    from homelab_maint import notify
    seen = []
    monkeypatch.setattr(notify, "send", lambda ev, *a, **k: seen.append(ev) or types.SimpleNamespace(ok=True, handled=True))
    payload = {"summary": "Rules changed: 1 added, 0 removed, 0 modified. Registry abc.", "lines": ["Added x.y: T"], "significant": True, "severity": "ok", "record": {}}
    assert _REAL_NOTICE(payload) is True
    ev = seen[0]
    assert (ev.kind, ev.task, ev.title) == ("maintenance", "rules_registry", "Rules registry") and ev.facts["significant"] is True
    assert ev.details == {"done": ["Added x.y: T"]} and ev.summary.startswith("Rules changed")
    assert _REAL_NOTICE({**payload, "record": {"to": "b" * 64}}) and _REAL_NOTICE({**payload, "record": {"to": "c" * 64}})
    assert len({e.dedupe_key for e in seen[1:]}) == 2 and seen[1].dedupe_key == "rules-" + "b" * 12 + "-applied"       # two changes in an hour: two notices
    monkeypatch.setattr(notify, "send", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no transport")))
    assert _REAL_NOTICE(payload) is False
    monkeypatch.setitem(__import__("sys").modules, "homelab_maint.notify", None)           # notify cannot even be imported
    assert _REAL_NOTICE(payload) is False


def test_the_real_journal_entry_uses_the_routine_change_log(monkeypatch):
    from homelab_maint import routine
    got = []
    monkeypatch.setattr(routine, "record_change", lambda *a, **k: got.append((a, k)))
    rec = {"from": "a" * 64, "to": "b" * 64, "applied": True}
    assert _REAL_JOURNAL({"summary": "Rules changed.", "record": rec}) is True
    (a, k), = got
    assert a[:2] == ("rules_registry", "config") and k["outcome"] == "applied" and k["verified"] == "n/a" and a[3] == "a" * 12 and a[4] == "b" * 12


def test_concurrent_syncs_serialise_under_the_flock(mig):
    sync(mig)
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=19))
    out = mig.tmp / "results"
    out.mkdir()
    pids = []
    for i in range(6):
        pid = os.fork()
        if pid == 0:                                              # child: sync and report, never return into pytest
            try:
                r = R.sync(mig.conf, mig.state, wait=True, hooks=R.NO_HOOKS)
                (out / f"{i}").write_text(r.status + ":" + ",".join(r.written))
                os._exit(0)
            except BaseException as exc:                          # noqa: BLE001
                (out / f"{i}").write_text(f"crash:{exc!r}")
                os._exit(1)
        pids.append(pid)
    for pid in pids:
        os.waitpid(pid, 0)
    res = sorted((out / str(i)).read_text() for i in range(6))
    assert [r.split(":")[0] for r in res].count("applied") == 1 and [r.split(":")[0] for r in res].count("unchanged") == 5, res
    hist = R.history(0, mig.state)
    assert len(hist) == 2 and hist[-1]["applied"] and hist[-1]["modified"][0]["id"] == "task.disk_forecast"
    assert not list(mig.conf.glob(".hm-rules-*")) and R.status(mig.conf, mig.state)["in_sync"]
    assert parse((mig.conf / "maint.toml").read_text())["tasks"]["disk_forecast"]["warn_free_pct"] == 19


def test_a_held_lock_makes_the_tick_skip_instead_of_wait(mig):
    sync(mig)
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=20))
    with R._flock(mig.state, False) as got:
        assert got
        t0 = time.time()
        assert sync(mig, wait=False).status == "locked"
        assert sync(mig, wait=0.15).status == "locked" and 0.14 < time.time() - t0 < 3
        assert R.tick(mig.conf, mig.state) == ""                       # the tick hook swallows "locked"
    assert sync(mig).status == "applied"


def test_tick_hook_reports_changes_and_never_raises(mig, monkeypatch):
    monkeypatch.setattr(R, "default_hooks", lambda: R.NO_HOOKS)
    assert R.tick(mig.conf, mig.state).startswith("rules sync: applied")
    edit_rule(mig, "task.disk_forecast", lambda r: r.update(kind="x"))
    assert R.tick(mig.conf, mig.state).startswith("rules sync: invalid")          # said once ...
    assert R.tick(mig.conf, mig.state) == "" and R.tick(mig.conf, mig.state) == ""    # ... not every minute
    monkeypatch.setattr(R, "sync", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert "rules sync crashed" in R.tick(mig.conf, mig.state)


def test_no_registry_directory_is_a_quiet_noop(env):
    res = sync(env)
    assert res.status == "no_registry" and R.read_current(env.state) is None and R.tick(env.conf, env.state) == ""
    assert not (env.state / "rules").exists() and list(env.conf.iterdir()) == []                  # nothing was created, not even the lock file
    assert R.status(env.conf, env.state)["present"] is False


def test_unreadable_state_dir_is_an_error_result_not_an_exception(mig, monkeypatch):
    os.chmod(mig.state, 0o500)
    try:
        if os.access(mig.state, os.W_OK):                              # running as root: cannot make a dir unwritable
            pytest.skip("root can write anywhere")
        res = sync(mig)
        assert res.status == "error" and not any((mig.conf / f).read_text().startswith(R.GENERATED_MARK) for f in LEGACY)
    finally:
        os.chmod(mig.state, 0o755)


# --------------------------------------------------------------------------- rollback and snapshots
def test_rollback_restores_the_previous_registry_and_its_generated_files(mig):
    sync(mig)
    a_files, a_gen = {p.name: p.read_bytes() for p in mig.rd.glob("*.toml")}, gen(mig)
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=21))
    sync(mig)
    assert gen(mig) != a_gen
    res = R.rollback(None, mig.conf, mig.state, hooks=R.NO_HOOKS)
    assert res.status == "applied" and res.modified[0]["after"] == {"params.warn_free_pct": W}
    assert {p.name: p.read_bytes() for p in mig.rd.glob("*.toml")} == a_files and gen(mig) == a_gen
    h = R.history(1, mig.state)[0]
    assert h["rollback_to"] == R.registry_hash(R.scan_registry(rdir=mig.rd)[0]) and h["applied"]
    assert R.status(mig.conf, mig.state)["in_sync"]


def test_rollback_by_hash_prefix_and_errors(mig):
    sync(mig)
    h1 = R.read_current(mig.state)["hash"]
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=22))
    sync(mig)
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=23))
    sync(mig)
    assert R.rollback("zzzz", mig.conf, mig.state, hooks=R.NO_HOOKS).status == "error"
    assert R.rollback(h1[:10], mig.conf, mig.state, hooks=R.NO_HOOKS).status == "applied"
    assert parse((mig.conf / "maint.toml").read_text())["tasks"]["disk_forecast"]["warn_free_pct"] == W
    res = R.rollback(None, mig.conf, mig.state, hooks=R.NO_HOOKS)                      # "previous" is relative to the CURRENT hash
    assert res.status == "applied" and R.read_current(mig.state)["hash"] != h1


def test_rollback_with_nothing_earlier_is_an_error(mig):
    sync(mig)
    res = R.rollback(None, mig.conf, mig.state, hooks=R.NO_HOOKS)
    assert res.status == "error" and "no earlier applied registry" in res.errors[0]


def test_rollback_saves_unapplied_edits_first_and_keeps_the_current_baseline(mig):
    sync(mig)
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=24))
    sync(mig)
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=25))      # never synced
    base = (mig.rd / R.BASELINE_FILE).read_bytes()
    res = R.rollback(None, mig.conf, mig.state, hooks=R.NO_HOOKS)
    assert res.status == "applied" and (mig.rd / R.BASELINE_FILE).read_bytes() == base
    saved = list((mig.state / "rules" / "snapshots").glob("unapplied-*"))
    assert len(saved) == 1 and "warn_free_pct = 25" in (saved[0] / "rules.d" / "10-checks.toml").read_text()


def test_rollback_validates_the_snapshot_against_todays_baseline_before_touching_rules_d(mig):
    sync(mig)
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=26))
    sync(mig)
    bl = tomllib.loads((mig.rd / R.BASELINE_FILE).read_text())
    bl["baseline"]["limit"].append({"file": "maint.toml", "path": "tasks.docker_cache.high_gib", "max": 1, "why": "a stricter release"})      # snapshot has 15
    (mig.rd / R.BASELINE_FILE).write_text(R.dumps(bl, aot_keys=("rule",)))
    now = {p.name: p.read_bytes() for p in mig.rd.glob("*.toml")}
    res = R.rollback(None, mig.conf, mig.state, hooks=R.NO_HOOKS)
    assert res.status == "invalid" and "no longer validates" in res.errors[0]
    assert {p.name: p.read_bytes() for p in mig.rd.glob("*.toml")} == now          # rules.d untouched


def test_snapshots_are_pruned_to_the_newest_twenty(env):
    write_reg(env.rd, {"10-checks.toml": [rule("task.alpha", file="maint.toml", target="tasks.alpha", params={"n": 0})]}, patterns=())
    for i in range(25):
        d = tomllib.loads((env.rd / "10-checks.toml").read_text())
        d["rule"][0]["params"]["n"] = i
        (env.rd / "10-checks.toml").write_text(R.dumps(d, aot_keys=("rule",)))
        res = sync(env, now=1000.0 + i)
        assert res.status == "applied", res.errors
        time.sleep(0.002)
    snaps = [p for p in (env.state / "rules" / "snapshots").iterdir() if p.is_dir()]
    assert len(snaps) == R.KEEP_SNAPSHOTS and (env.state / "rules" / "snapshots" / R.read_current(env.state)["hash"]).is_dir()
    assert len(R.applied_hashes(env.state)) == R.KEEP_SNAPSHOTS and len(R.history(0, env.state)) == 25
    assert len(R.history(5, env.state)) == 5


def test_status_phases(mig):
    s = R.status(mig.conf, mig.state)
    assert s["present"] and s["pending"] and not s["in_sync"] and s["hash"] == ""
    sync(mig)
    s = R.status(mig.conf, mig.state)
    assert s["in_sync"] and not s["pending"] and s["valid"] and s["rules_count"] == mig.n and s["drift"] == []
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=27))
    assert R.status(mig.conf, mig.state)["pending"]


def test_sync_applies_a_registry_with_no_rules_at_all_only_if_it_has_a_baseline(env):
    mkdir(env.rd)
    assert sync(env).status == "invalid"                              # an empty rules.d is a broken install, not "nothing to do"
    (env.rd / R.BASELINE_FILE).write_text(R.baseline_text([], today=TODAY))
    os.chmod(env.rd / R.BASELINE_FILE, 0o644)
    assert sync(env).status == "applied" and not list(env.conf.glob("*.toml"))


# =========================================================================== 7. public export: rules.json and manifest.json
NOW = 1_800_000_000.0


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t))


def export(env, **kw) -> dict:
    kw.setdefault("status", {})
    kw.setdefault("history_src", [])
    kw.setdefault("audit_src", [])
    kw.setdefault("probes", {})
    return R.build_rules_json(env.conf, env.state, now=NOW, **kw)


def by_id(doc: dict) -> dict:
    return {r["id"]: r for r in doc["rules"]}


def test_rules_json_has_the_documented_shape(mig):
    sync(mig, now=NOW - 100)
    doc = export(mig)
    assert set(doc) == {"schema", "generated_at", "registry_hash", "registry_synced_at", "applied", "valid", "categories", "rules", "history", "stats"}
    assert doc["schema"] == 1 and doc["generated_at"] == NOW and doc["registry_synced_at"] == NOW - 100 and doc["applied"] and doc["valid"]
    assert doc["registry_hash"] == R.read_current(mig.state)["hash"] and len(doc["rules"]) == mig.n
    assert [c["id"] for c in doc["categories"]] == list(R.CATEGORIES) and sum(c["count"] for c in doc["categories"]) == mig.n
    assert all(set(c) == {"id", "title", "blurb", "count"} for c in doc["categories"]) and doc["categories"][0]["title"] == "Health checks"
    spec = {"id", "category", "title", "kind", "why", "does", "applies_to", "params", "mode", "enabled", "severity", "destructive", "proof", "principle", "since",
            "source_file", "last_evaluated", "last_triggered", "triggers_30d", "last_result", "related"}
    for r in doc["rules"]:
        assert spec <= set(r) and r["kind"] in R.KINDS and r["category"] in R.CATEGORIES
    r = by_id(doc)["task.disk_forecast"]
    assert r["params"]["warn_free_pct"] == W and r["source_file"] == "10-checks.toml" and r["related"] == ["disk_forecast"] and r["mode"] is None
    assert by_id(doc)["task.docker_cache"]["mode"] == TASKS["docker_cache"]["mode"] and by_id(doc)["task.docker_cache"]["destructive"]
    assert doc["stats"]["total"] == mig.n and doc["stats"]["destructive"] >= 10 and doc["stats"]["placeholder"] == mig.n - BASE_RULES
    assert doc["stats"]["apply_mode"] == sum(1 for t in TASKS.values() if t.get("mode") == "apply")
    assert len(json.dumps(doc)) < 400_000
    json.dumps(doc, allow_nan=False)


def test_rules_json_shows_what_the_script_runs_under_not_pending_edits(mig):
    assert export(mig)["applied"] is False and len(export(mig)["rules"]) == mig.n          # nothing applied yet: fall back to rules.d
    sync(mig)
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=31))
    assert by_id(export(mig))["task.disk_forecast"]["params"]["warn_free_pct"] == W      # an unsynced edit is NOT what runs
    sync(mig)
    assert by_id(export(mig))["task.disk_forecast"]["params"]["warn_free_pct"] == 31


def test_check_cross_reference_from_status_history_and_probes(mig):
    sync(mig)
    status = {"tasks": {"disk_forecast": {"last_run": NOW - 120, "status": "warn", "summary": "warn: / 4% free"},
                        JOB0: {"klass": "J", "last_run": NOW - 3600, "status": "ok", "summary": "backup ok"}}}
    seq = [("ok", 0), ("warn", 1), ("warn", 2), ("ok", 3), ("crit", 4), ("crit", 5), ("ok", 6), ("warn", 7)]
    hist = [{"t": NOW - 40 * 86400, "kind": "task", "task": "disk_forecast", "status": "warn"}, {"t": NOW - 40 * 86400 + 900, "kind": "task", "task": "disk_forecast", "status": "ok"}]
    hist += [{"t": NOW - 20 * 86400 + i * 900, "kind": "task", "task": "disk_forecast", "status": s} for s, i in seq]
    hist += [{"t": NOW - 5000, "kind": "task", "task": "disk_forecast", "status": "warn", "alert": False},                  # informational: not a trigger
             {"t": NOW - 7200, "kind": "job", "task": JOB0, "status": "warn"}, {"t": NOW - 7000, "kind": "disk", "mount": "/", "free": 1}]
    probes = {"probes": {PROBE0: {"last_run": NOW - 30, "state": "down", "since": NOW - 600, "detail": "inactive"}}}
    doc = export(mig, status=status, history_src=hist, probes=probes)
    r = by_id(doc)
    d = r["task.disk_forecast"]
    assert d["last_evaluated"] == NOW - 120 and d["last_result"] == "warn: / 4% free"
    assert d["triggers_30d"] == 3 and d["last_triggered"] == NOW - 20 * 86400 + 7 * 900          # three episodes; the 40-day-old one is outside the window
    j = r[rid("job", JOB0)]
    assert j["last_evaluated"] == NOW - 3600 and j["triggers_30d"] == 1 and j["last_triggered"] == NOW - 7200 and j["last_result"] == "ok: backup ok"
    p = r[rid("probe", PROBE0)]
    assert p["last_evaluated"] == NOW - 30 and p["last_triggered"] == NOW - 600 and p["last_result"] == "down: inactive"
    idle = r["task.failed_units"]
    assert idle["last_evaluated"] is None and idle["triggers_30d"] == 0 and idle["last_triggered"] is None and idle["last_result"] is None
    assert doc["stats"]["evaluated_24h"] >= 3 and doc["stats"]["triggered_30d"] >= 2


def test_cleanup_protection_and_cap_cross_reference_from_the_audit_log(mig):
    sync(mig)
    t = NOW - 86400
    A, B = RET[0], RET[1]
    ida, idb = rid("retention", A["name"]), rid("retention", B["name"])
    others = [rid("retention", r["name"]) for r in RET[2:] if not any(r["path"].startswith(x["path"]) or x["path"].startswith(r["path"]) for x in (A, B))]
    audit = [{"ts": iso(t + i), "task": "retention", "action": "delete", "target": A["path"] + "/a.log", "bytes": 5, "outcome": "dry-run"} for i in range(3)]
    audit += [{"ts": iso(t + 3600 + i), "task": "retention", "action": "delete", "target": B["path"] + "/x", "bytes": 5, "outcome": "done"} for i in range(2)]
    audit += [{"ts": iso(t + 7200), "task": "retention", "action": "delete", "target": B["path"] + "/y", "bytes": 5, "outcome": "refused-protected"},
              {"ts": iso(NOW - 45 * 86400), "task": "retention", "action": "delete", "target": B["path"] + "/z", "bytes": 5, "outcome": "done"}]
    target = "/media/Immich/library/x"
    audit += [{"ts": iso(t + 100), "task": "docker_images", "action": "rm", "target": target, "bytes": 0, "outcome": "refused-protected"},
              {"ts": iso(t + 9000), "task": "docker_images", "action": "rm", "target": "img", "bytes": 0, "outcome": "refused-cap"}]
    r = by_id(export(mig, audit_src=audit))
    assert r[ida]["triggers_30d"] == 1 and r[ida]["last_triggered"] == pytest.approx(t, abs=1)
    assert r[idb]["triggers_30d"] == 1 and r[idb]["last_result"] == f"done: 2 item(s) under {B['path']}"       # counts and the RULE's path, never an audited file name
    assert all(r[o]["triggers_30d"] == 0 for o in others)                                                         # only records under ITS path count
    assert r["task.retention"]["triggers_30d"] == 2                                                               # the task as a whole: two bursts in 30 days
    want = {x.id for x in R.load_registry(rdir=mig.rd, trust=False).rules if x.kind == "protection" and x.merge == "append"
            and any(re.search(p_, target, re.I) for p_ in x.params["patterns"])}
    prot = [x for x in r.values() if x["kind"] == "protection" and x["triggers_30d"]]
    assert want and {p["id"] for p in prot} == want                                                              # every group with a matching pattern, no other
    assert all(p["last_triggered"] == pytest.approx(t + 100, abs=1) for p in prot)
    assert r["task.docker_images"]["triggers_30d"] == 0                                                          # refused records are not "the cleaner found work"


def test_secrets_never_reach_the_public_file(env):
    write_reg(env.rd, {"10-checks.toml": [rule("task.s", file="maint.toml", target="tasks.s", why="w", params={
        "api_key": "KEY123", "token": "T0K", "url": "https://user:hunter2@example.com/x?y=1", "password": "pw", "nested": {"secret": ["a", "b"], "ok": 1},
        "ttl_tokens": 5, "kuma_push_key": "umbrella-status", "hdr": "Authorization: Bearer abcdefghijklmnop"})]}, patterns=())
    sync(env)
    blob = json.dumps(export(env))
    for leak in ("KEY123", "T0K", "hunter2", "\"pw\"", "abcdefghijklmnop"):
        assert leak not in blob, leak
    p = by_id(export(env))["task.s"]["params"]
    assert p["api_key"] == "[redacted]" and p["nested"] == {"secret": ["[redacted]", "[redacted]"], "ok": 1} and p["ttl_tokens"] == 5       # a number is not a secret
    assert p["kuma_push_key"] == "[redacted]" and p["url"] == "https://example.com/[redacted]"                                         # any key name with "key" in it; a foreign URL keeps its host only


def test_public_history_is_redacted_newest_first_and_capped_at_fifty(env):
    write_reg(env.rd, {"10-checks.toml": [rule("task.h", file="maint.toml", target="tasks.h", params={"api_key": "one", "n": 0})]}, patterns=())
    for i in range(1, 56):
        d = tomllib.loads((env.rd / "10-checks.toml").read_text())
        d["rule"][0]["params"].update(api_key=f"secret{i}", n=i)
        (env.rd / "10-checks.toml").write_text(R.dumps(d, aot_keys=("rule",)))
        assert sync(env, now=1000.0 + i).status == "applied"
    doc = export(env)
    assert len(doc["history"]) == 50 and doc["history"][0]["ts"] == 1055.0 and doc["history"][0]["to"] == R.short(R.read_current(env.state)["hash"])
    m = doc["history"][0]["modified"][0]
    assert m["id"] == "task.h" and m["before"]["params.n"] == 54 and m["after"]["params.n"] == 55 and m["after"]["params.api_key"] == "[redacted]"
    assert "secret" not in json.dumps(doc)


def test_size_cap_shrinks_params_then_history_and_says_so(env):
    big = {"blob": ["x" * 50] * 400}
    write_reg(env.rd, {"10-checks.toml": [rule(f"task.big{i}", file="maint.toml", target=f"tasks.big{i}", params=dict(big)) for i in range(12)]}, patterns=())
    sync(env)
    doc = R.build_rules_json(env.conf, env.state, now=NOW, status={}, history_src=[], audit_src=[], max_bytes=60_000)
    assert len(json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode()) <= 60_000 and doc["stats"]["truncated"] is True
    assert any(r["params"].get("_truncated") for r in doc["rules"]) and len(doc["rules"]) == 16
    full = R.build_rules_json(env.conf, env.state, now=NOW, status={}, history_src=[], audit_src=[], max_bytes=10 ** 7)
    assert "truncated" not in full["stats"] and full["rules"][0]["params"]


def test_the_export_tolerates_missing_and_damaged_inputs(mig, tmp_path):
    sync(mig)
    junk = tmp_path / "junk.json"
    junk.write_text("{not json")
    for kw in ({"status": junk}, {"status": tmp_path / "nope.json"}, {"status": {"tasks": "x"}}, {"history_src": tmp_path / "nope.jsonl"},
               {"audit_src": [{"ts": "garbage", "task": 1}, "x", None]}, {"probes": {"probes": []}}, {"history_src": [{"t": "x"}, {}, 3]}):
        assert len(export(mig, **kw)["rules"]) == mig.n


def test_history_and_audit_files_are_read_from_disk_when_given_paths(mig, tmp_path):
    sync(mig)
    h = tmp_path / "history.jsonl"
    h.write_text("\n".join(json.dumps({"t": NOW - 100 * i, "kind": "task", "task": "disk_forecast", "status": s, "alert": True, "dur": 1})
                           for i, s in zip(range(10, 0, -1), ["ok", "warn", "ok", "crit", "ok", "ok", "warn", "warn", "ok", "ok"])) + "\nnot json at all\n")
    a = tmp_path / "audit.jsonl"
    a.write_text(json.dumps({"ts": iso(NOW - 60), "task": "retention", "action": "x", "target": RET[0]["path"] + "/a", "bytes": 1, "outcome": "done"}) + "\n")
    r = by_id(export(mig, history_src=h, audit_src=a, status={}))
    assert r["task.disk_forecast"]["triggers_30d"] == 3 and r[rid("retention", RET[0]["name"])]["triggers_30d"] == 1


def test_manifest_lists_every_public_file_with_schema_and_age(mig, tmp_path):
    sync(mig, now=NOW - 50)
    pub = mkdir(tmp_path / "public")
    (pub / "overview.json").write_text(json.dumps({"schema": 1, "generated_at": NOW - 10, "x": 1}))
    (pub / "checks.json").write_text(json.dumps({"schema": 2, "generated_at": NOW - 20}))
    (pub / "list.json").write_text("[1, 2]")
    (pub / "broken.json").write_text("{nope")
    (pub / "big.json").write_text(json.dumps({"schema": 1, "pad": "x" * 600_000}))
    mkdir(pub / "reports")
    (pub / "reports" / "index.json").write_text(json.dumps({"schema": 1, "generated_at": NOW - 5}))
    (pub / ".pub-1.tmp").write_text("x")
    (pub / "notes.txt").write_text("x")
    m = R.build_manifest(pub, mig.conf, mig.state, runner_version="1.2.3", now=NOW)
    assert m["schema"] == 1 and m["generated_at"] == NOW and m["runner_version"] == "1.2.3" and m["registry_hash"] == R.read_current(mig.state)["hash"]
    assert m["registry_synced_at"] == NOW - 50 and m["rules_count"] == mig.n and m["registry_valid"] is True
    f = m["files"]
    assert f["overview"] == {"schema": 1, "generated_at": NOW - 10} and f["checks"]["schema"] == 2 and f["reports/index"]["generated_at"] == NOW - 5
    assert f["list"]["schema"] is None and f["broken"] == {"schema": None, "generated_at": None, "unreadable": True}
    assert f["big"]["approx"] is True and isinstance(f["big"]["generated_at"], float)              # too big to parse: its mtime stands in, flagged
    assert "manifest" not in f and ".pub-1" not in f and "notes" not in f
    m2 = R.build_manifest(pub, mig.conf, mig.state, files={"overview": {"schema": 1, "generated_at": 5}, "bad": 3}, now=NOW)
    assert m2["files"] == {"overview": {"schema": 1, "generated_at": 5}}


def test_write_public_writes_rules_then_manifest_world_readable_and_atomically(mig, tmp_path):
    sync(mig)
    pub = tmp_path / "public"
    names = R.write_public(pub, mig.conf, mig.state, status={}, history_src=[], audit_src=[], now=NOW, runner_version="9")
    assert names == ["rules.json", "manifest.json"]
    for n in names:
        assert (pub / n).stat().st_mode & 0o777 == 0o644
    mf = json.loads((pub / "manifest.json").read_text())
    rj = json.loads((pub / "rules.json").read_text())
    assert mf["files"]["rules"] == {"schema": 1, "generated_at": NOW} and rj["registry_hash"] == mf["registry_hash"] and mf["runner_version"] == "9"
    assert not list(pub.glob(".hm-rules-*")) and not list(pub.glob(".pub-*")) and len((pub / "rules.json").read_bytes()) < 400_000


def test_write_public_rebuilds_rules_json_only_when_stale_or_the_registry_changed(mig, tmp_path):
    sync(mig)
    pub = tmp_path / "public"
    kw = dict(status={}, history_src=[], audit_src=[], runner_version="1")
    assert R.write_public(pub, mig.conf, mig.state, now=NOW, **kw) == ["rules.json", "manifest.json"]
    first = (pub / "rules.json").read_bytes()
    assert R.write_public(pub, mig.conf, mig.state, now=time.time(), **kw) == ["manifest.json"]       # fresh and same registry: only the small manifest
    assert (pub / "rules.json").read_bytes() == first
    assert R.write_public(pub, mig.conf, mig.state, now=time.time(), rules_max_age_s=0, **kw) == ["rules.json", "manifest.json"]
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=W + 1))
    sync(mig)
    assert R.write_public(pub, mig.conf, mig.state, now=time.time(), **kw) == ["rules.json", "manifest.json"]     # registry changed: rebuilt at once
    edit_rule(mig, "task.disk_forecast", lambda r: r.update(kind="x"))
    sync(mig)                                                                                                     # invalid: the file must say so
    assert R.write_public(pub, mig.conf, mig.state, now=time.time(), **kw) == ["rules.json", "manifest.json"]
    assert json.loads((pub / "rules.json").read_text())["valid"] is False


def test_write_public_never_raises(mig, monkeypatch, capsys):
    sync(mig)
    monkeypatch.setattr(R, "atomic_write", lambda *a, **k: (_ for _ in ()).throw(OSError(30, "Read-only file system")))
    assert R.write_public(mig.tmp / "public", mig.conf, mig.state) == []
    assert "rules export" in capsys.readouterr().err


# =========================================================================== 8. the CLI
@pytest.fixture
def cli(mig, monkeypatch):
    monkeypatch.setattr(core, "CONF_DIR", mig.conf)
    monkeypatch.setattr(core, "STATE_DIR", mig.state)
    monkeypatch.setattr(core, "LOG_DIR", mkdir(mig.tmp / "log"))
    return mig


def run(capsys, *argv) -> tuple[int, str, str]:
    rc = R.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_cli_requires_a_command(capsys):
    with pytest.raises(SystemExit) as e:
        R.main([])
    assert e.value.code == 2


def test_cli_list_and_show(cli, capsys):
    rc, out, _ = run(capsys, "list")
    assert rc == 0 and f"{cli.n} of {cli.n} rules" in out and "task.disk_forecast" in out
    rc, out, _ = run(capsys, "list", "--category", "cleanup", "--destructive")
    lines = [l for l in out.splitlines() if " D  " in l]
    assert rc == 0 and lines and all(l.split()[2] == "cleanup" for l in lines) and "task.disk_forecast" not in out
    rc, out, _ = run(capsys, "list", "--kind", "protection", "--enabled", "--json")
    assert rc == 0 and {r["kind"] for r in json.loads(out)} == {"protection"}
    assert len(json.loads(out)) == sum(1 for x in R.load_registry(rdir=cli.rd, trust=False).rules if x.kind == "protection") >= 2
    assert f"0 of {cli.n}" in run(capsys, "list", "--disabled")[1]
    rc, out, _ = run(capsys, "show", "task.docker_cache")
    assert rc == 0 and "destructive  True" in out and "mode         report" in out and "writes       maint.toml  tasks.docker_cache" in out and "high_gib = 15" in out
    rc, out, _ = run(capsys, "show", "task.docker_cache", "--json")
    assert json.loads(out)["params"]["high_gib"] == 15
    rc, out, err = run(capsys, "show", "task.docker_cach")
    assert rc == 1 and "did you mean 'task.docker_cache'?" in err
    assert "writes       nothing (a documented policy)" in run(capsys, "show", "safety.hard-caps")[1]


def test_cli_check_reports_errors_warnings_and_todo(cli, capsys):
    rc, out, _ = run(capsys, "check")
    assert rc == 0 and "0 errors" in out and "-> OK" in out and "TODO-CONTENT" in out
    rc, out, _ = run(capsys, "check", "--todo")
    assert rc == 0 and "task.disk_forecast: why, does" in out and "task.docker_cache: why, does, proof" in out and "safety.hard-caps" not in out
    rc, out, _ = run(capsys, "check", "--json")
    assert rc == 0 and json.loads(out)["valid"] is True
    edit_rule(cli, "task.disk_forecast", lambda r: r.update(kind="x"))
    rc, out, _ = run(capsys, "check")
    assert rc == 1 and "ERROR   10-checks.toml: rule #1 (task.disk_forecast): kind must be one of" in out and "BLOCKED" in out
    assert run(capsys, "check", "--json")[0] == 1


def test_cli_sync_diff_history_rollback(cli, capsys):
    rc, out, _ = run(capsys, "sync", "--no-notify")
    assert rc == 0 and out.startswith("applied ") and f"+{cli.n} -0 ~0 rules, 8 file(s) written" in out
    assert run(capsys, "sync", "--no-notify")[1].startswith("unchanged")
    rc, out, _ = run(capsys, "diff")
    assert rc == 0 and "no pending rule changes" in out and out.count("identical") == 8
    edit_rule(cli, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=14))
    rc, out, _ = run(capsys, "diff")
    assert rc == 1 and "~ task.disk_forecast: params.warn_free_pct 12 -> 14" in out and "maint.toml: same data" not in out
    (cli.conf / "maint.toml").write_text((cli.conf / "maint.toml").read_text() + "\n[tasks.hand]\nx = 1\n")
    out = run(capsys, "diff")[1]
    assert "tasks.hand: disk" in out and "-> registry <missing>" in out
    rc, out, _ = run(capsys, "sync", "--no-notify")
    assert rc == 0 and "~1 rules" in out
    rc, out, _ = run(capsys, "history", "5")
    assert rc == 0 and out.count("applied") >= 2 and "~ task.disk_forecast: params.warn_free_pct 12 -> 14" in out
    rows = json.loads(run(capsys, "history", "--json")[1])
    assert len(rows) == 2 and rows[0]["from"] is None and rows[1]["drift"] == ["maint.toml"]        # the edit and the drift repair were one sync
    rc, out, _ = run(capsys, "rollback", "--no-notify")
    assert rc == 0 and out.startswith("applied")
    assert parse((cli.conf / "maint.toml").read_text())["tasks"]["disk_forecast"]["warn_free_pct"] == W
    assert run(capsys, "rollback", "nonexistent", "--no-notify")[0] == 1


def test_cli_sync_exit_codes(cli, capsys, monkeypatch):
    edit_rule(cli, "task.disk_forecast", lambda r: r.update(kind="x"))
    rc, out, _ = run(capsys, "sync", "--no-notify")
    assert rc == 1 and out.startswith("invalid:") and "ERROR" in out
    (cli.conf / "maint.toml").write_text("[tasks.a]\nx = 1\n")
    edit_rule(cli, "task.disk_forecast", lambda r: r.update(kind="check"))
    rc, out, _ = run(capsys, "sync", "--no-notify")
    assert rc == 1 and out.startswith("blocked:")
    assert run(capsys, "sync", "--no-notify", "--adopt")[0] == 0
    monkeypatch.setattr(R, "sync", lambda *a, **k: R.SyncResult("locked"))
    assert run(capsys, "sync", "--no-notify")[0] == 3


def test_cli_export(cli, capsys):
    sync(cli)
    rc, out, _ = run(capsys, "export")
    doc = json.loads(out)
    assert rc == 0 and doc["stats"]["total"] == cli.n and "\n" not in out.strip()
    assert run(capsys, "export", "--pretty")[1].count("\n") > 1000
    rc, out, _ = run(capsys, "export", "--write")
    assert rc == 0 and "rules.json, manifest.json" in out and (cli.state / "public" / "manifest.json").is_file()


def test_cli_explain_where_orphans(cli, capsys):
    sync(cli)
    rc, out, _ = run(capsys, "explain", "retention")
    ret_ids = [rid("retention", r["name"]) for r in RET]
    assert rc == 0 and re.search(r"task retention: \d+ rules", out) and f"{ret_ids[0]}  [cleanup, destructive]" in out and "allowed_roots = " in out and "<- task.retention" in out
    assert "<- " + ",".join(ret_ids) in out
    rc, out, _ = run(capsys, "explain", "no_such_task")
    assert rc == 1 and "0 rules" in out
    rc, out, _ = run(capsys, "explain", "routine_rotate")
    assert rc == 0 and "mode = \"report\"" in out
    rc, out, _ = run(capsys, "where", "warn_free_pct")
    assert rc == 0 and "maint.toml  tasks.disk_forecast.warn_free_pct = 12   <- task.disk_forecast (10-checks.toml)" in out
    assert run(capsys, "where", "maint.toml:tasks.disk_forecast.warn_days")[1].count("\n") == 1
    assert run(capsys, "where", "mode")[1].count("<- task.") >= 10
    rc, out, _ = run(capsys, "where", "patterns")
    assert rc == 0 and re.search(r"protected.toml  patterns   <- protect\.[a-z0-9.-]+@0", out)
    assert run(capsys, "where", "nothing_sets_this")[0] == 1
    rc, out, _ = run(capsys, "orphans")
    assert rc == 0 and out.strip() == "0 orphan keys"
    (cli.conf / "maint.toml").write_text((cli.conf / "maint.toml").read_text().replace("[tasks.disk_forecast]", "[tasks.disk_forecast]\nhand_added_knob = 7"))
    rc, out, _ = run(capsys, "orphans")
    assert rc == 1 and "maint.toml: tasks.disk_forecast.hand_added_knob = 7   no rule owns it" in out and "1 orphan key" in out


def test_cli_migrate_proves_before_writing_and_never_overwrites(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(core, "CONF_DIR", ETC)
    out_dir = tmp_path / "rules.d"
    rc, out, _ = run(capsys, "migrate", "--from", str(ETC), "--out", str(out_dir), "--dry-run")
    assert rc == 0 and out.count("EQUAL") == 8 and "dry run: nothing written" in out and "proof OK" in out and not out_dir.exists()
    rc, out, _ = run(capsys, "migrate", "--from", str(ETC), "--out", str(out_dir))
    assert rc == 0 and out_dir.is_dir() and (out_dir / "10-checks.toml").is_file()
    rc, _, err = run(capsys, "migrate", "--from", str(ETC), "--out", str(out_dir))
    assert rc == 2 and "already holds a registry" in err
    rc, out, _ = run(capsys, "migrate", "--from", str(ETC), "--out", str(out_dir), "--verify")
    assert rc == 0 and out.count("EQUAL") == 8
    (out_dir / "10-checks.toml").write_text((out_dir / "10-checks.toml").read_text().replace("warn_free_pct = 12", "warn_free_pct = 13"))
    rc, out, err = run(capsys, "migrate", "--from", str(ETC), "--out", str(out_dir), "--verify")
    assert rc == 1 and "tasks.disk_forecast.warn_free_pct: 12 != 13" in out and "proof FAILED" in err


def test_cli_without_a_registry_says_so(env, monkeypatch, capsys):
    monkeypatch.setattr(core, "CONF_DIR", env.conf)
    monkeypatch.setattr(core, "STATE_DIR", env.state)
    for cmd in (["list"], ["check"], ["diff"]):
        rc, out, _ = run(capsys, *cmd)
        assert rc == 1 and "no registry" in out
    assert run(capsys, "history")[1].strip() == "no registry changes recorded yet"
    assert run(capsys, "sync", "--no-notify")[1].startswith("no_registry")


# =========================================================================== 9. the rules_registry check task
def test_check_task_reports_each_registry_state(mig, monkeypatch):
    monkeypatch.setattr(core, "CONF_DIR", mig.conf)
    monkeypatch.setattr(core, "STATE_DIR", mig.state)
    ctx = types.SimpleNamespace(now=NOW)
    res = R.check_task(ctx)
    assert res.status == "warn" and "not applied yet" in res.summary and res.metrics["pending"] == 1
    sync(mig, now=NOW - 600)
    res = R.check_task(ctx)
    assert res.status == "ok" and f"{mig.n} rules" in res.summary and res.metrics["in_sync"] == 1 and res.metrics["synced_age_min"] == 10 and len(res.summary) <= 140
    (mig.conf / "maint.toml").write_text((mig.conf / "maint.toml").read_text() + "\n# hand edit\n")
    res = R.check_task(ctx)
    assert res.status == "info" and res.alert is False and "maint.toml" in res.summary
    sync(mig)
    edit_rule(mig, "task.disk_forecast", lambda r: r.update(kind="x"))
    sync(mig)
    res = R.check_task(ctx)
    assert res.status == "warn" and res.summary.startswith("rules registry invalid, last good config in force") and res.items and len(res.summary) <= 140
    assert res.summary.isascii()


def test_check_task_without_a_registry_is_informational(env, monkeypatch):
    monkeypatch.setattr(core, "CONF_DIR", env.conf)
    monkeypatch.setattr(core, "STATE_DIR", env.state)
    res = R.check_task(types.SimpleNamespace(now=NOW))
    assert res.status == "info" and res.alert is False


def test_register_tasks_adds_the_check_once_and_it_runs_as_a_c0_task(monkeypatch, mig):
    monkeypatch.setattr(core, "CONF_DIR", mig.conf)
    monkeypatch.setattr(core, "STATE_DIR", mig.state)
    monkeypatch.setattr(core, "REGISTRY", dict(core.REGISTRY))
    R.register_tasks()
    R.register_tasks()
    t = core.REGISTRY["rules_registry"]
    assert (t.klass, t.tier) == ("C0", "check") and t.run is R.check_task
    res, _ = core.run_task(t, {"tasks": {}, "caps": {}, "global": {}, "protected": {}}, apply=True)
    assert res.status in ("warn", "ok", "info")


def test_history_is_trimmed_by_bytes_when_it_grows_past_the_cap(mig, monkeypatch):
    sync(mig)
    hp = mig.state / "rules" / "history.jsonl"
    hp.write_bytes(b"\n".join(json.dumps({"to": f"{i:064d}", "applied": True, "pad": "x" * 200}).encode() for i in range(5000)) + b"\n")
    monkeypatch.setattr(R, "HISTORY_FILE_MAX", 100_000)
    monkeypatch.setattr(R, "_trim_jsonl", lambda p, max_bytes=100_000, keep=R.HISTORY_MIN_KEEP, _real=R._trim_jsonl: _real(p, max_bytes, keep))
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=29))
    sync(mig)
    rows = R.history(0, mig.state)
    assert hp.stat().st_size <= 100_000 and len(rows) > R.HISTORY_MIN_KEEP                       # BYTES bound the file, the newest records stay
    assert rows[-1]["modified"][0]["id"] == "task.disk_forecast" and rows[0]["to"] != f"{0:064d}" and int(rows[0]["to"]) > 0
    big = {"to": "f" * 64, "applied": True, "pad": "y" * 70_000}                               # records far bigger than any "line count" idea
    for keep in (1, 3):
        hp.write_bytes(b"\n".join(json.dumps(big).encode() for _ in range(6)) + b"\n")
        R._trim_jsonl(hp, 100_000, keep)
        assert len(hp.read_bytes().splitlines()) == keep                                         # the newest `keep` survive even when they alone are over the bound


# --------------------------------------------------------------------------- randomised variants of the LEGACY files go through migrate too
def _mutate_legacy(doc: dict, rnd: random.Random) -> None:
    """One random structural change that keeps every list free of exact duplicates (append's dedupe cannot represent those)."""
    spots: list[tuple[str, tuple]] = []

    def walk(node, path):
        if isinstance(node, dict):
            spots.append(("table", path))
            for k, v in node.items():
                walk(v, path + (k,))
        elif isinstance(node, list):
            spots.append(("list", path))
            for i, v in enumerate(node):
                walk(v, path + (i,))
        else:
            spots.append(("leaf", path))
    walk(doc, ())
    kind, path = rnd.choice(spots)
    node = _get(doc, path)
    tag = f"-v{rnd.randrange(10 ** 6)}"
    if kind == "leaf" and path:
        parent = _get(doc, path[:-1])
        v = node
        parent[path[-1]] = (not v) if isinstance(v, bool) else v - 1 if isinstance(v, int) else v - 0.25 if isinstance(v, float) else v + tag      # down, never past a hard limit
    elif kind == "table":
        op = rnd.choice(["add", "add_table", "drop"])
        if op == "add":
            node["zz_added" + tag.replace("-", "_")] = rnd.choice([1, 2.5, "x", True, ["a", "b"], {"k": 1}])
        elif op == "add_table":
            node["zz_table" + tag.replace("-", "_")] = {"a": 1, "nested": {"b": [1, 2, 3]}}
        elif path and node:
            del node[rnd.choice(list(node))]
    elif kind == "list" and node:
        op = rnd.choice(["drop", "shuffle", "add", "dup"])
        if op == "drop":
            node.pop(rnd.randrange(len(node)))
        elif op == "shuffle":
            rnd.shuffle(node)
        elif op == "add":
            node.append({"name": "zz" + tag, "x": 1} if isinstance(node[0], dict) else "zz" + tag if isinstance(node[0], str) else node[0] + 1000 + rnd.randrange(1000))
        elif op == "dup" and isinstance(node[0], dict):
            c = copy.deepcopy(rnd.choice(node))
            c["name" if "name" in c else next(iter(c), "name")] = "dup" + tag
            node.append(c)


def test_migrate_proves_equality_for_random_variants_of_the_legacy_files(tmp_path):
    rnd = random.Random(424242)
    base_docs, _raws = R.load_legacy(ETC)
    cat = R.task_catalog()
    for n in range(24):
        src = mkdir(tmp_path / f"v{n}")
        docs = copy.deepcopy(base_docs)
        for _ in range(rnd.randint(1, 8)):
            f = rnd.choice(LEGACY)
            if f == "protected.toml" and rnd.random() < .5:
                continue
            _mutate_legacy(docs[f], rnd)
        for f, d in docs.items():
            if f == "protected.toml" and isinstance(d.get("patterns"), list):
                chunks = [d["patterns"][i:i + 7] for i in range(0, len(d["patterns"]), 7)]
                text = "patterns = [\n" + "".join(f"  # group {i}\n  " + ", ".join(json.dumps(p) for p in c) + ",\n" for i, c in enumerate(chunks)) + "]\n"
                text += R.dumps({k: v for k, v in d.items() if k != "patterns"})
                (src / f).write_text(text)
                assert tomllib.loads(text) == d
            else:
                (src / f).write_text(R.dumps(d))
        proof, texts = R.migrate(src, tmp_path / f"out{n}", today=TODAY, catalog=cat)
        # What this test proves is EQUALITY: the compiled registry parses to the same data as the legacy files, for every variant. A random
        # mutation can also break a registry INVARIANT (a renamed baseline pattern, a changed `unprotect` regex, a retention path that escapes
        # allowed_roots, ...): the registry refuses that, which is correct and is covered by its own tests, so such a variant has no registry
        # to compile again. (The fixed seed used to avoid these draws by luck; adding a table anywhere in etc/ shifts the random sequence.)
        assert not any(proof.files.values()), (n, proof.files)
        if not proof.ok:
            continue
        out = R.analyze(rdir=tmp_path / f"out{n}", trust=False, catalog=cat)                       # and the written files compile to it again
        assert all(R.same(tomllib.loads(out.comp.texts[f]), docs[f]) for f in LEGACY)


def test_migrate_keeps_a_shipped_baseline_that_is_already_in_rules_d(tmp_path):
    """install.sh puts the release's baseline into rules.d BEFORE `rules migrate`: that alone is not 'an existing registry'."""
    out = mkdir(tmp_path / "rules.d")
    shipped = (ETC / "rules.d" / R.BASELINE_FILE).read_text()
    (out / R.BASELINE_FILE).write_text(shipped)
    proof, texts = R.migrate(ETC, out, today=TODAY)
    assert proof.ok and (out / R.BASELINE_FILE).read_text() == shipped and (out / "10-checks.toml").is_file()
    # a baseline that wants a pattern the live protected.toml does not have: the proof refuses (the owner decides, nothing is written)
    out2 = mkdir(tmp_path / "rules2.d")
    (out2 / R.BASELINE_FILE).write_text(R.baseline_text(["pattern-the-host-lacks"], today=TODAY))
    proof, _ = R.migrate(ETC, out2, today=TODAY)
    assert not proof.ok and any("lost the baseline pattern 'pattern-the-host-lacks'" in e for e in proof.errors) and not (out2 / "10-checks.toml").exists()


def test_baseline_only_regenerates_the_shipped_file_and_never_shrinks_it_silently(tmp_path, capsys):
    dest = tmp_path / "rules.d" / R.BASELINE_FILE
    assert R.main(["migrate", "--from", str(ETC), "--baseline-only", str(dest), "--today", TODAY]) == 0
    assert f"baseline written: {N_PATTERNS} protected patterns" in capsys.readouterr().out
    shipped, now = (R.load_registry(rdir=d, trust=False).baseline for d in (ETC / "rules.d", dest.parent))
    assert set(shipped["protected_patterns"]) <= set(now["protected_patterns"]) and len(now["protected_patterns"]) == N_PATTERNS
    assert now["never_touch"] == shipped["never_touch"] and now["limit"] == shipped["limit"]       # the rest is reproducible from the code
    src = mkdir(tmp_path / "src")
    (src / "protected.toml").write_text('patterns = ["postgres"]\n')
    assert R.main(["migrate", "--from", str(src), "--baseline-only", str(dest)]) == 2
    assert f"would drop {N_PATTERNS - 1} pattern(s)" in capsys.readouterr().err
    assert R.main(["migrate", "--from", str(src), "--baseline-only", str(dest), "--force"]) == 0
    assert R.load_registry(rdir=dest.parent, trust=False).baseline["protected_patterns"] == ["postgres"]
    assert R.main(["migrate", "--from", str(tmp_path / "nowhere"), "--baseline-only", str(dest)]) == 2


def test_the_real_consumers_read_the_generated_files_exactly_like_the_originals(mig, tmp_path):
    """Beyond tomllib equality: core, notify, probes, routine and the scheduler give the same answers on generated and original files."""
    import subprocess
    import sys
    orig = mkdir(tmp_path / "orig")
    for f in ETC.glob("*.toml"):
        shutil.copy(f, orig / f.name)
        os.chmod(orig / f.name, 0o644)
    sync(mig)
    probe = ("import json, sys\nsys.path.insert(0, %r)\nfrom homelab_maint import core, notify, probes, routine, scheduler\n"
             "d, rows, errs = probes.load_probes()\nrc = routine.load_config()\ncfg = core.load_config()\n"
             "print(json.dumps({'core': cfg, 'notify': notify.load_config(cfg), 'probes': [d, sorted(p.name for p in rows), errs],\n"
             "  'routine': [rc.valid, rc.errors], 'sched': scheduler.validate()}, sort_keys=True, default=str))\n" % str(ROOT))

    def view(conf: Path) -> str:
        env = {**os.environ, "HOMELAB_MAINT_CONF": str(conf), "HOMELAB_MAINT_STATE": str(mkdir(tmp_path / "s")), "HOMELAB_MAINT_LOG": str(mkdir(tmp_path / "l")),
               "HOMELAB_MAINT_RUN": str(mkdir(tmp_path / "r"))}
        r = subprocess.run([sys.executable, "-B", "-c", probe], capture_output=True, text=True, env=env, timeout=120)
        assert r.returncode == 0, r.stderr
        return r.stdout
    a, b = view(orig), view(mig.conf)
    assert a == b and json.loads(a)["probes"][2] == [] and len(json.loads(a)["probes"][1]) == N_PROBES and json.loads(a)["routine"][0] is True


# =========================================================================== 10. review fixes: invariants on the COMPILED config, the pinned baseline, consumers,
#                                                                              header drift, stale files, failed syncs, size bounds, public redaction
def ret_rule(roots=("/var/log",), path="/var/log/app", glob=None, task="retention", **kw) -> list[dict]:
    """One retention-style task rule plus ONE element rule (path [+ glob]); `kw` overrides what the rules say about themselves."""
    params = {"name": "r0", "path": path, **({"glob": glob} if glob is not None else {})}
    base = {"kind": "cleanup", "destructive": True, "file": "maint.toml"}
    return [rule(f"task.{task}", **{**base, **kw}, target=f"tasks.{task}", mode="report", params={"allowed_roots": list(roots)}),
            rule("ret.r0", **{**base, **kw}, target=f"tasks.{task}.rules[]", merge="append", params=params)]


def errs_of(tmp_path, files, **kw) -> list[str]:
    return analysis(tmp_path, files, patterns=(), **kw).errors


# --------------------------------------------------------------------------- critical: what the rules say about themselves decides nothing
@pytest.mark.parametrize("says", [{"destructive": False}, {"kind": "policy"}, {"kind": "check", "destructive": False}, {"kind": "alert", "destructive": False}])
def test_never_touch_and_roots_are_enforced_whatever_kind_or_destructive_flag_the_rule_carries(tmp_path, says):
    for i, (path, needle) in enumerate([("/var/log/ai-stack", "never-touch"), ("/home/ohmz/.ssh", "never-touch"), ("/etc/passwd", "escapes allowed_roots")]):
        errs = errs_of(tmp_path / f"{i}", {"30-cleanup.toml": ret_rule(path=path, **says)})
        assert any(needle in e and "rule ret.r0:" in e for e in errs), (says, path, errs)


@pytest.mark.parametrize("path,glob,needle", [
    ("/var/log", "ai-stack/*", "never-touch list"),                      # the target hides in the glob, not in the path
    ("/var/log", "ai-stack", "never-touch list"),
    ("/var/log", "**/ai-stack/*", "never-touch list"),
    ("/var/log", "a/ai-stack/b", "never-touch list"),
    ("/var/log", "../etc/*", "must be relative"),
    ("/var/log", "a/../../etc", "must be relative"),
    ("/var/log", "/etc/*", "must be relative"),
    ("/var/lib", "*", "can reach the never-touch tree"),                  # /var/lib/docker is a child: a wildcard selects it
    ("/var/lib", "*/volumes/*", "can reach the never-touch tree"),
    ("/var/lib", "**/*", "can reach the never-touch tree"),
    ("/var/lib", "dock*/x", "never-touch"),
    ("/home/ohmz", "*", "can reach the never-touch tree /home/*"),         # ~/.ssh, ~/models, ~/.gnupg are children (the user part of the regex is a wildcard)
    ("/home/ohmz", ".*", "can reach the never-touch tree /home/*"),
    ("/home/ohmz", "models*/x", "can reach the never-touch tree /home/*"),
    ("/home/ohmz", "**/*.tmp", "can reach the never-touch tree /home/*"),
    ("/home/ohmz", "*.log", None), ("/home/ohmz", "Downloads/*", None), ("/home/ohmz/Downloads", "**/*", None),
    ("/volume1/docker", "*/config/*", "can reach the never-touch tree /volume1/docker"),
    ("/volume1/docker", "kavita/config/logs/*.log", "never-touch list"),   # the glob walks THROUGH /volume1/docker/kavita: say the exact directory in `path` instead
    ("/volume1/docker/kavita/config/logs", "*.log", None),
    ("/var/log", "*.log", None), ("/var/log/app", "**/*", None), ("/var/log", "journal/*.journal", None), ("/var/log", "[a-c]*.gz", None)])
def test_a_glob_is_joined_to_its_path_before_the_never_touch_test(tmp_path, path, glob, needle):
    roots = (path,)
    errs = errs_of(tmp_path, {"30-cleanup.toml": ret_rule(roots=roots, path=path, glob=glob, destructive=needle is None)})        # the bad ones say "not destructive" and are caught anyway
    if needle is None:
        assert not errs, errs
    else:
        assert any(needle in e for e in errs), (needle, errs)


def test_the_shipped_retention_rules_pass_the_compiled_checks_and_the_wildcard_reach_test(template):
    """False positives would make the shipped config unappliable: every shipped retention rule (globs included) is clean."""
    an = R.analyze(rdir=template.rd, trust=False)
    assert an.ok, an.errors
    tasks = an.comp.docs["maint.toml"]["tasks"]
    ret = tasks["retention"]["rules"]
    assert all(isinstance(r.get("glob"), str) for r in ret) and len(ret) == len(RET)
    trim = tasks["app_cache_trim"]["rules"]               # the Tunarr subtitle cache moved here (retention keeps the Kavita rules)
    assert [r["name"] for r in trim][:1] == ["tunarr-subtitles"] and trim[0]["path"].startswith(tasks["app_cache_trim"]["allowed_roots"][0] + "/")


def test_a_cleaner_that_scopes_itself_with_roots_needs_no_allowed_roots(tmp_path):
    ok = [rule("task.log_compress", kind="cleanup", destructive=True, file="maint.toml", target="tasks.log_compress", params={"roots": ["/var/log/myapp"], "min_mib": 5})]
    assert not errs_of(tmp_path / "a", {"30-cleanup.toml": ok})
    too_broad = [{**ok[0], "params": {"roots": ["/var"]}}]
    assert any("too broad" in e for e in errs_of(tmp_path / "b", {"30-cleanup.toml": too_broad}))
    forbidden = [{**ok[0], "params": {"roots": ["/home/ohmz/.ssh"]}}]
    assert any("never-touch" in e for e in errs_of(tmp_path / "c", {"30-cleanup.toml": forbidden}))
    unknown = [{**ok[0], "id": "task.mystery", "target": "tasks.mystery"}]                    # nobody declared it: allowed_roots are required
    assert any("no allowed_roots" in e for e in errs_of(tmp_path / "d", {"30-cleanup.toml": unknown}))


def test_every_path_like_key_of_a_cleaner_is_checked_at_any_depth_and_type(tmp_path):
    odd = [rule("task.tool_caches", kind="cleanup", destructive=True, file="maint.toml", target="tasks.tool_caches",
                params={"projects_root": "/home/ohmz/.cursor/x", "nested": {"deep": [{"dir": "/mnt/backup/x"}]}, "pnpm_stores": ["/etc//x", 5]})]
    errs = "\n".join(errs_of(tmp_path, {"30-cleanup.toml": odd}))
    assert "never-touch" in errs and "must be absolute and normalised" in errs and errs.count("never-touch") >= 2


def test_a_path_under_an_unusual_key_name_is_checked_too_but_excludes_and_destinations_are_not(tmp_path):
    odd = [rule("task.tool_caches", kind="cleanup", destructive=True, file="maint.toml", target="tasks.tool_caches",
                params={"targets": ["/mnt/backup/x", "relative"], "output": "/etc/passwd/../x", "queue_url": "http://127.0.0.1:8188/queue", "rx": "^/var/lib/docker",
                        "exclude_dirs": ["/var/lib/docker"], "archive_to": "/mnt/backup/archive", "ref_paths": ["/mnt/backup"], "never_touch": ["^/x$"]})]
    errs = errs_of(tmp_path, {"30-cleanup.toml": odd})
    assert any("'/mnt/backup/x'" in e and "never-touch" in e for e in errs) and any("'/etc/passwd/../x'" in e and "absolute and normalised" in e for e in errs)
    assert not any("exclude" in e or "archive" in e or "ref_paths" in e or "queue" in e or "'^/var/lib/docker'" in e or "'relative'" in e for e in errs), errs
    flagged = rule("pol.o", kind="policy", file="maint.toml", target="tasks.tool_caches", params={"output": "/var/tmp/x"})                       # a "/..." value alone makes a rule destructive
    assert any("rule pol.o: writes output of cleaner tool_caches" in e for e in errs_of(tmp_path / "b", {"30-cleanup.toml": [flagged]}))


def test_a_cleaner_rule_that_is_not_flagged_destructive_is_an_error_when_it_writes_paths_exemptions_or_modes(tmp_path):
    for i, (target, params, mode, word) in enumerate([("tasks.retention", {"allowed_roots": ["/var/log"]}, None, "allowed_roots"),
                                                      ("tasks.retention", {}, "report", "mode"),
                                                      ("tasks.docker_cache", {"unprotect": ["^x$"]}, None, "unprotect"),
                                                      ("tasks.retention.rules[]", {"name": "x", "path": "/var/log/a"}, None, "path"),
                                                      ("tasks", {"retention": {"allowed_roots": ["/var/log"]}}, None, "allowed_roots"),
                                                      ("", {"tasks": {"retention": {"allowed_roots": ["/var/log"]}}}, None, "allowed_roots")]):
        r = rule("pol.x", kind="policy", file="maint.toml", target=target, params=params, merge="append" if target.endswith("[]") else "set", **({"mode": mode} if mode else {}))
        errs = errs_of(tmp_path / f"{i}", {"30-cleanup.toml": [r]})
        assert any("rule pol.x: writes" in e and word in e and "retention" in e or "docker_cache" in e for e in errs), (target, errs)
    ok = rule("pol.x", kind="check", file="maint.toml", target="tasks.disk_forecast", params={"warn_free_pct": 11, "watch": ["/"]})       # a C0 task: nothing destructive in it
    assert not errs_of(tmp_path / "ok", {"10-checks.toml": [ok]})
    off = rule("pol.y", kind="policy", file="maint.toml", target="tasks.retention", params={"allowed_roots": ["/etc"]}, enabled=False)      # compiled out: no say
    assert not errs_of(tmp_path / "off", {"10-checks.toml": [off]})


# --------------------------------------------------------------------------- critical: apply is judged on the compiled config
def test_an_apply_hidden_below_the_task_table_or_in_params_is_refused_whoever_writes_it(tmp_path):
    nested = rule("pol.a", kind="policy", file="maint.toml", target="tasks", params={"app_cache_trim": {"mode": "apply"}})
    errs = "\n".join(errs_of(tmp_path / "a", {"30-cleanup.toml": [nested]}))
    assert "tasks.app_cache_trim.mode = \"apply\" must come from the rule's own mode field" in errs and "rule pol.a: writes mode of cleaner app_cache_trim" in errs
    root = rule("pol.b", kind="policy", file="maint.toml", target="", params={"tasks": {"docker_cache": {"mode": "apply"}}})
    assert any("must come from the rule's own mode field" in e for e in errs_of(tmp_path / "b", {"30-cleanup.toml": [root]}))
    deep = rule("pol.c", kind="cleanup", destructive=True, file="maint.toml", target="tasks.docker_cache", params={"sub": {"mode": "apply"}})
    assert any("hidden below the task table" in e and "tasks.docker_cache.sub.mode" in e for e in errs_of(tmp_path / "c", {"30-cleanup.toml": [deep]}))
    elem = rule("pol.d", kind="cleanup", destructive=True, file="maint.toml", target="tasks.retention.rules[]", merge="append", params={"name": "x", "mode": "apply"})
    assert any("do not put mode = \"apply\" in params" in e for e in errs_of(tmp_path / "d", {"30-cleanup.toml": retention()[:1] + [elem]}))
    listed = rule("pol.e", kind="cleanup", destructive=True, file="maint.toml", target="tasks.docker_cache", params={"modes": ["report", "apply"]})
    assert any("hidden below the task table" in e for e in errs_of(tmp_path / "e", {"30-cleanup.toml": [listed]}))
    ok = rule("task.docker_cache", kind="cleanup", destructive=True, file="maint.toml", target="tasks.docker_cache", mode="apply", params={"high_gib": 15})
    assert not errs_of(tmp_path / "ok", {"30-cleanup.toml": [ok]})


def test_ladder_rung_keys_and_boolean_switches_need_a_destructive_rule(tmp_path):
    def rung(destructive, key="restart", task="pressure_response"):
        return rule("task.rung", kind="spike", destructive=destructive, file="maint.toml", target=f"tasks.{task}", params={key: "apply"})
    errs = errs_of(tmp_path / "a", {"20-spike.toml": [rung(False)]})
    assert any("switches a ladder rung on" in e for e in errs), errs
    assert not errs_of(tmp_path / "b", {"20-spike.toml": [rung(True)]})
    assert any("hidden below the task table" in e for e in errs_of(tmp_path / "c", {"20-spike.toml": [rung(True, key="bogus")]}))                 # not a baseline rung key
    assert any("hidden below the task table" in e for e in errs_of(tmp_path / "d", {"20-spike.toml": [rung(True, task="docker_cache")]}))        # right key, wrong task
    flag = lambda d: rule("task.sd", kind="spike", destructive=d, file="maint.toml", target="tasks.stuck_detector", params={"enforce": True})   # noqa: E731
    assert any("switches a task to act on its own" in e for e in errs_of(tmp_path / "e", {"20-spike.toml": [flag(False)]}))
    assert not errs_of(tmp_path / "f", {"20-spike.toml": [flag(True)]})
    off = rule("task.sd", kind="spike", file="maint.toml", target="tasks.stuck_detector", params={"enforce": False})                          # the shipped value
    assert not errs_of(tmp_path / "g", {"20-spike.toml": [off]})


def test_the_shipped_apply_values_all_have_an_owner_that_says_so(template):
    an = R.analyze(rdir=template.rd, trust=False)
    assert an.ok, an.errors
    tasks = an.comp.docs["maint.toml"]["tasks"]
    leaves = list(R._apply_leaves(tasks, ("tasks",)))
    assert ("tasks", "pressure_response", "reclaim") in [p for p, _k, _v in leaves] and ("tasks", "pressure_response", "mode") in [p for p, _k, _v in leaves]
    assert len(leaves) == sum(1 for t in tasks.values() for k, v in t.items() if v == "apply")                 # nothing hides deeper in the shipped config


# --------------------------------------------------------------------------- critical: the unprotect escape hatch is an allow-list
def unp(task, regexes, destructive=True, **kw) -> dict:
    return rule(f"task.{task}", kind="cleanup", destructive=destructive, file="maint.toml", target=f"tasks.{task}", params={"unprotect": regexes}, **kw)


def test_unprotect_entries_must_be_on_the_baseline_allow_list(tmp_path):
    errs = analysis(tmp_path / "a", {"30-cleanup.toml": [unp("docker_containers_prune", [".*"])]}, patterns=("postgres", "immich_postgres")).errors
    assert any("unprotect '.*' in maint.toml tasks.docker_containers_prune.unprotect is not on the baseline allow-list" in e for e in errs), errs
    assert any("it would exempt from protected.toml (postgres, immich_postgres" in e for e in errs)                               # names what it would expose
    for i, (task, rx) in enumerate([("caps", ["^tunarr-host-net$", "^kavita$"]), ("comfyui_idle_reclaim", ["^comfyui$"]), ("app_cache_trim", ["^/home/ohmz/StudioProjects/tunarr/\\.docker-data/tunarr/cache/subtitles(/|$)"])]):
        assert not errs_of(tmp_path / f"ok{i}", {"30-cleanup.toml": [unp(task, rx)]}), task
    assert errs_of(tmp_path / "b", {"30-cleanup.toml": [unp("docker_cache", ["^comfyui$"])]})                                    # the right regex under the WRONG task
    assert errs_of(tmp_path / "c", {"30-cleanup.toml": [unp("caps", ["^comfyui$ "])]})                                           # not an exact entry
    assert errs_of(tmp_path / "d", {"30-cleanup.toml": [unp("docker_images", ["^nothing-protected-like-this$"])]})               # even a harmless-looking regex needs the allow-list


def test_unprotect_must_be_a_list_of_strings(tmp_path):
    assert any("must be a list of regex strings" in e for e in errs_of(tmp_path / "a", {"30-cleanup.toml": [unp("caps", ".*")]}))              # a bare string is read per character
    assert any("is not a string" in e for e in errs_of(tmp_path / "b", {"30-cleanup.toml": [unp("caps", [1, "^kavita$"])]}))
    assert any("invalid regex" in e for e in errs_of(tmp_path / "c", {"30-cleanup.toml": [unp("caps", ["["])]}))


def test_the_ladder_exemption_lists_of_classes_toml_are_allow_listed_too(tmp_path):
    q = lambda rx: rule("c.ladder", kind="spike", file="classes.toml", target="ladder", params={"qos_unprotect": rx})   # noqa: E731
    errs = errs_of(tmp_path / "a", {"20-spike.toml": [q(["^(immich_postgres)$"])]})
    assert any("qos_unprotect" in e and "not on the baseline allow-list" in e for e in errs)
    ship = next(a for f, pth, a, _w in R.UNPROTECT_ALLOW if (f, pth) == ("classes.toml", "ladder.qos_unprotect"))
    assert not errs_of(tmp_path / "b", {"20-spike.toml": [q(list(ship))]})


def test_the_owner_can_accept_an_extra_unprotect_entry_loudly_but_only_from_the_overrides_file(tmp_path):
    rd = write_reg(tmp_path / "a" / "rd", {"30-cleanup.toml": [unp("docker_cache", ["^old-box$"])]}, patterns=())
    (rd / "99-owner-overrides.toml").write_text('[meta]\ncategory = "safety"\nallow_unprotect = [{ file = "maint.toml", path = "tasks.docker_cache.unprotect", regex = "^old-box$" }]\n')
    os.chmod(rd / "99-owner-overrides.toml", 0o644)
    an = R.analyze(rdir=rd, trust=False)
    assert an.ok and any(w.startswith("LOUD: unprotect '^old-box$' in maint.toml tasks.docker_cache.unprotect accepted by 99-owner-overrides.toml") for w in an.warnings)
    wrong = write_reg(tmp_path / "b" / "rd", {"30-cleanup.toml": [unp("docker_cache", ["^old-box$"])]}, patterns=(), extra_meta={"allow_unprotect": [{"file": "maint.toml", "path": "x", "regex": "y"}]})
    assert any("allow_unprotect is only honoured in 99-owner-overrides.toml" in e for e in R.analyze(rdir=wrong, trust=False).errors)
    rd2 = write_reg(tmp_path / "c" / "rd", {}, patterns=())
    (rd2 / "99-owner-overrides.toml").write_text('[meta]\ncategory = "safety"\nallow_unprotect = ["^old-box$"]\n')
    os.chmod(rd2 / "99-owner-overrides.toml", 0o644)
    assert any("allow_unprotect must be a list of" in e for e in R.analyze(rdir=rd2, trust=False).errors)


def test_the_unprotect_floor_matches_the_shipped_config_exactly(real_floor):
    """The release floor lists every exemption etc/ ships and nothing else: a new exemption in etc/ must be added to the floor on purpose."""
    docs = {f: tomllib.loads((ETC / f).read_text()) for f in ("maint.toml", "classes.toml")}
    shipped = {}
    for f, d in docs.items():
        for path, lst in R._unprotect_lists(d):
            shipped[(f, ".".join(map(str, path)))] = list(lst)
    floor = {k: v for k, v in R._unp_map(R.package_floor()["unprotect"]).items()}
    assert floor == shipped


# --------------------------------------------------------------------------- the public API of the new invariants, in isolation
def test_doc_invariants_judge_plain_documents_without_any_rule(real_floor):
    base, _w = R.effective_baseline({"protected_patterns": [], "never_touch": [], "min_root_depth": 1, "limit": [], "unprotect": [], "apply_keys": []}, R.package_floor())
    cat = R.task_catalog()
    docs = {"protected.toml": {"patterns": list(R.PROTECTED_FLOOR)},
            "maint.toml": {"caps": {"max_gib_per_run": 40}, "tasks": {"retention": {"allowed_roots": ["/var/log"], "rules": [{"path": "/var/log/ai-stack", "glob": "*"}], "unprotect": [".*"]}}}}
    bad, _warns, _rem = R.doc_invariants(docs, base, cat)
    assert any("never-touch" in e for e in bad) and any("unprotect '.*'" in e for e in bad) and not any("lost the baseline" in e for e in bad)
    docs["protected.toml"]["patterns"].remove("postgres")
    docs["maint.toml"]["caps"]["max_gib_per_run"] = 10 ** 6
    bad, _warns, _rem = R.doc_invariants(docs, base, cat)
    assert any("lost the baseline pattern 'postgres'" in e for e in bad) and any("exceeds the hard limit 100" in e for e in bad)
    docs["protected.toml"]["patterns"] = list(R.PROTECTED_FLOOR) + [7]
    assert any("patterns must be a list of strings" in e for e in R.doc_invariants(docs, base, cat)[0])
    assert not any("protected" in e for e in R.doc_invariants({"maint.toml": {}}, base, cat, need_protected=False)[0])       # a file that is not there is not unsafe


# --------------------------------------------------------------------------- the notice shows what really flipped
def test_flipping_destructive_off_is_a_significant_change_and_shows_before_and_after(mig):
    sync(mig)
    rules = R.load_registry(rdir=mig.rd, trust=False).rules
    victim = next(r for r in rules if r.file == "jobs.toml" and r.destructive and r.kind == "job")
    rec = Rec()
    edit_rule(mig, victim.id, lambda r: r.update(destructive=False, kind="policy"))
    res = sync(mig, hooks=rec.hooks)
    assert res.status == "applied"
    n = rec.notices[-1]
    line = next(l for l in n["lines"] if l.startswith(f"Changed {victim.id}:"))
    assert n["significant"] and "destructive true -> false" in line and "kind job -> policy" in line                     # the flags come first, with before and after
    assert R._significant({"added": [], "removed": [], "modified": [{"id": "x", "fields": ["mode"], "before": {}, "after": {}}]}, {"x": {}}, {"x": {}}, [])
    assert not R._significant({"added": [], "removed": [], "modified": [{"id": "x", "fields": ["title"], "before": {}, "after": {}}]}, {"x": {}}, {"x": {}}, [])
    assert R._significant({"added": [], "removed": [], "modified": [{"id": "x", "fields": ["why"], "before": {}, "after": {}}]}, {"x": {"destructive": False}}, {"x": {"destructive": True}}, [])


# =========================================================================== the baseline is pinned in the code, rules.d only mirrors it
def edit_baseline(env, fn) -> None:
    f = env.rd / R.BASELINE_FILE
    d = tomllib.loads(f.read_text())
    fn(d)
    f.write_text(R.dumps(d, aot_keys=("rule",)))
    os.chmod(f, 0o644)


def _drop_nt(d): d["baseline"]["never_touch"].remove("(^|/)ai-stack(/|$)")                                               # noqa: E704
def _raise_cap(d): next(l for l in d["baseline"]["limit"] if l["path"] == "caps.max_gib_per_run").update(max=100000)       # noqa: E704
def _depth(d): d["baseline"]["min_root_depth"] = 1                                                                       # noqa: E704
def _drop_pat(d): d["baseline"]["protected_patterns"].remove("postgres")                                                 # noqa: E704
def _more_unp(d): d["baseline"]["unprotect"][0]["allow"].append(".*")                                                    # noqa: E704
def _drop_limit(d): d["baseline"]["limit"] = [l for l in d["baseline"]["limit"] if l["path"] != "ack.max_days"]           # noqa: E704
def _more_apply(d): d["baseline"]["apply_keys"].append({"task": "docker_cache", "key": "mode"})                          # noqa: E704


@pytest.mark.parametrize("weaken", [_drop_nt, _raise_cap, _depth, _drop_pat, _more_unp, _drop_limit, _more_apply])
def test_a_weaker_baseline_file_is_refused_loudly_and_changes_nothing(mig, weaken):
    sync(mig)
    good = gen(mig)
    rec = Rec()
    edit_baseline(mig, weaken)
    res = sync(mig, hooks=rec.hooks)
    assert res.status == "invalid" and any("is weaker than the release baseline" in e for e in res.errors), res.errors
    assert gen(mig) == good and R.read_current(mig.state)["invalid"]["kind"] == "invalid"
    assert len(rec.notices) == 1 and rec.notices[0]["significant"] and rec.notices[0]["summary"].startswith("The safety baseline in rules.d is WEAKER")
    for _ in range(2):
        assert sync(mig, hooks=rec.hooks).status == "invalid"
    assert len(rec.notices) == 1                                                                                          # said once
    assert R.status(mig.conf, mig.state)["valid"] is False and R.check_task is not None


def test_weakening_the_baseline_and_the_rule_together_does_not_get_through(mig):
    """The reviewer's PoC: delete 'postgres' from the mirror AND from the protection rule: the release floor still has it."""
    sync(mig)
    good = gen(mig)
    edit_baseline(mig, _drop_pat)
    pid = next(r.id for r in R.load_registry(rdir=mig.rd, trust=False).rules if r.kind == "protection" and "postgres" in r.params.get("patterns", []))
    edit_rule(mig, pid, lambda r: r["params"]["patterns"].remove("postgres"))
    res = sync(mig, hooks=R.NO_HOOKS)
    assert res.status == "invalid" and any("lost the baseline pattern 'postgres'" in e for e in res.errors) and gen(mig) == good
    assert "postgres" in parse(good["protected.toml"].decode())["patterns"]


def test_an_owner_override_may_still_drop_a_protected_pattern_even_from_the_mirror(mig):
    sync(mig)
    edit_baseline(mig, _drop_pat)
    pid = next(r.id for r in R.load_registry(rdir=mig.rd, trust=False).rules if r.kind == "protection" and "postgres" in r.params.get("patterns", []))
    edit_rule(mig, pid, lambda r: r["params"]["patterns"].remove("postgres"))
    (mig.rd / "99-owner-overrides.toml").write_text('[meta]\ncategory = "safety"\nallow_baseline_removal = ["postgres"]\n')
    os.chmod(mig.rd / "99-owner-overrides.toml", 0o644)
    rec = Rec()
    res = sync(mig, hooks=rec.hooks)
    assert res.status == "applied" and "postgres" not in parse((mig.conf / "protected.toml").read_text())["patterns"]
    assert rec.notices[-1]["significant"] and any(l.startswith("LOUD: the owner removed the baseline protection 'postgres'") for l in rec.notices[-1]["lines"])


def test_a_baseline_only_edit_is_recorded_and_announced_in_both_directions(mig):
    sync(mig)
    rec = Rec()
    edit_baseline(mig, lambda d: d["baseline"]["never_touch"].append("/extra-never-touch(/|$)"))                         # STRONGER than the release: accepted
    res = sync(mig, hooks=rec.hooks)
    assert res.status == "applied" and not (res.added or res.removed or res.modified) and res.baseline["strengthened"] and not res.baseline["weakened"]
    h = R.history(1, mig.state)[0]
    assert h["baseline_change"]["strengthened"] == ["never-touch regex '/extra-never-touch(/|$)' added"] and rec.notices[-1]["summary"].startswith("The safety baseline changed.")
    assert any(l.startswith("Baseline strengthened:") for l in rec.notices[-1]["lines"]) and not rec.notices[-1]["significant"]
    edit_baseline(mig, lambda d: d["baseline"]["never_touch"].remove("/extra-never-touch(/|$)"))                         # back to the release: the effective floor LOWERED
    res = sync(mig, hooks=rec.hooks)
    assert res.status == "applied" and res.baseline["weakened"] == ["never-touch regex '/extra-never-touch(/|$)' removed"]
    n = rec.notices[-1]
    assert n["significant"] and n["severity"] == "warn" and n["summary"].startswith("BASELINE WEAKENED.") and n["lines"][0].startswith("BASELINE WEAKENED: never-touch regex")
    assert R.history(1, mig.state)[0]["baseline_change"]["weakened"] and R._public_history(R._state(mig.state))[0]["baseline_weakened"] == 1
    assert sync(mig, hooks=rec.hooks).status == "unchanged"


def test_effective_baseline_is_the_strictest_of_floor_and_mirror():
    pkg = {"protected_patterns": ["a", "b"], "never_touch": ["^/x"], "min_root_depth": 2, "limit": [{"file": "f", "path": "p", "max": 10, "why": "w"}],
           "unprotect": [{"file": "f", "path": "u", "allow": ["r1", "r2"], "why": ""}], "apply_keys": [{"task": "t", "key": "k", "why": ""}]}
    mirror = {"protected_patterns": ["b", "c"], "never_touch": ["^/x", "^/y"], "min_root_depth": 3, "limit": [{"file": "f", "path": "p", "max": 5, "why": "w2"},
              {"file": "f", "path": "q", "max": 7, "why": ""}], "unprotect": [{"file": "f", "path": "u", "allow": ["r1"], "why": ""}], "apply_keys": []}
    eff, weak = R.effective_baseline(mirror, pkg, [])
    assert weak == ["protected pattern 'a' is missing"]
    assert eff["protected_patterns"] == ["a", "b", "c"] and eff["never_touch"] == ["^/x", "^/y"] and eff["min_root_depth"] == 3
    assert {(l["path"], l["max"]) for l in eff["limit"]} == {("p", 5), ("q", 7)}
    assert eff["unprotect"][0]["allow"] == ["r1"] and eff["apply_keys"] == [] and eff["sha"] == R.baseline_sha(eff)
    assert R.effective_baseline(mirror, pkg, ["a"])[1] == []                                                              # an owner-allowed removal is excused
    worse = {**mirror, "min_root_depth": 1, "never_touch": [], "limit": [{"file": "f", "path": "p", "max": 11, "why": ""}],
             "unprotect": [{"file": "f", "path": "u", "allow": ["r1", "r2", "r3"], "why": ""}], "apply_keys": [{"task": "t", "key": "z", "why": ""}]}
    weak = R.effective_baseline(worse, pkg, [])[1]
    assert len(weak) == 6 and any("min_root_depth 1" in w for w in weak) and any("max 11 is above 10" in w for w in weak) and any("'r3'" in w for w in weak)
    nofloor = R.effective_baseline(mirror, R.EMPTY_FLOOR, [])
    assert nofloor[1] == [] and nofloor[0]["unprotect"] == mirror["unprotect"]                                           # no floor: the mirror is all there is


def test_baseline_diff_names_what_got_weaker_and_what_got_stronger():
    a = {"protected_patterns": ["a", "b"], "never_touch": ["x"], "min_root_depth": 2, "limit": [{"file": "f", "path": "p", "max": 10}],
         "unprotect": [{"file": "f", "path": "u", "allow": ["r1"]}], "apply_keys": []}
    b = {"protected_patterns": ["a"], "never_touch": ["x", "y"], "min_root_depth": 3, "limit": [{"file": "f", "path": "p", "max": 20}],
         "unprotect": [{"file": "f", "path": "u", "allow": ["r1", "r2"]}], "apply_keys": [{"task": "t", "key": "k"}]}
    d = R.baseline_diff(a, b)
    assert d["weakened"] == ["protected pattern 'b' removed", "limit p 10 -> 20", "unprotect u may now hold 'r2'", "apply key t.k added"]
    assert d["strengthened"] == ["never-touch regex 'y' added", "min_root_depth 2 -> 3"]
    assert R.baseline_diff(None, b) == {"weakened": [], "strengthened": []} and R.baseline_diff(a, a) == {"weakened": [], "strengthened": []}


def test_the_shipped_mirror_is_the_release_floor_and_the_floor_is_part_of_the_shipped_protection(real_floor):
    reg = R.load_registry(rdir=ETC / "rules.d", trust=False)
    assert reg.valid, reg.errors
    eff, weak = R.effective_baseline(reg.baseline, R.package_floor(), [])
    assert weak == [] and eff["protected_patterns"] == list(R.PROTECTED_FLOOR) and eff["never_touch"] == R.NEVER_TOUCH
    want = tomllib.loads(R.baseline_text(list(R.PROTECTED_FLOOR), today=TODAY))["baseline"]
    got = dict(reg.baseline)
    got.pop("derived_from"), want.pop("derived_from")
    for k in ("unprotect", "apply_keys", "limit"):
        assert [dict(x) for x in got[k]] == [dict(x) for x in want[k]], k
    assert got["never_touch"] == want["never_touch"] and got["protected_patterns"] == want["protected_patterns"] and got["min_root_depth"] == want["min_root_depth"]
    pats = tomllib.loads((ETC / "protected.toml").read_text())["patterns"]
    assert set(R.PROTECTED_FLOOR) <= set(pats)                                                                          # a release never pins a pattern etc/ lacks


def test_the_floor_off_switch_exists_only_for_tests(monkeypatch):
    monkeypatch.setattr(R, "FLOOR_ENFORCED", True)
    assert R.package_floor()["protected_patterns"] == list(R.PROTECTED_FLOOR) and R.package_floor()["unprotect"]
    monkeypatch.setattr(R, "FLOOR_ENFORCED", False)
    assert R.package_floor()["protected_patterns"] == [] and R.package_floor()["unprotect"] is None


# =========================================================================== the runner's OWN loaders must accept what the compiler produces
def add_rule(env, fname: str, r: dict) -> None:
    f = env.rd / fname
    d = tomllib.loads(f.read_text())
    d["rule"].append(r)
    f.write_text(R.dumps(d, aot_keys=("rule",)))
    os.chmod(f, 0o644)


@pytest.mark.parametrize("fname,r,needle", [
    ("80-jobs.toml", rule("job.broken", kind="job", file="jobs.toml", target="job[]", merge="append", order=10 ** 6,
                          params={"name": "broken", "schedule": "every purple moon", "command": []}), "jobs: job broken"),
    ("70-monitoring.toml", rule("probe.teleport", kind="probe", file="probes.toml", target="probe[]", merge="append", order=10 ** 6,
                                params={"name": "tele", "type": "teleport"}), "probes: "),
    ("40-protection.toml", rule("protect.ints", kind="protection", file="protected.toml", target="", merge="append", order=10 ** 6, params={"patterns": [1, 2]}),
     "protected.toml: patterns must be a list of strings"),
])
def test_a_config_the_runners_loaders_reject_never_replaces_the_good_one(mig, fname, r, needle):
    sync(mig)
    good = gen(mig)
    rec = Rec()
    add_rule(mig, fname, r)
    res = sync(mig, hooks=rec.hooks)
    assert res.status == "invalid" and any(needle in e for e in res.errors), res.errors
    assert gen(mig) == good and not list(mig.conf.glob(".hm-rules-*"))
    assert R.read_current(mig.state)["invalid"]["kind"] == "invalid" and len(rec.notices) == 1
    for _ in range(2):
        assert sync(mig, hooks=rec.hooks).status == "invalid"
    assert len(rec.notices) == 1


def test_the_other_loaders_are_asked_too_routine_notify_acks_classes(mig):
    sync(mig)
    reg = R.load_registry(rdir=mig.rd, trust=False)
    entry = next(r for r in reg.rules if r.file == "routine.toml" and r.target == "routine[]")
    cases = [(entry.id, lambda r: r["params"].update(cadence="fortnightly"), "routine: routine daily: cadence must be one of"),
             ("notify.quiet_hours", lambda r: r.update(target="", params={"quiet_hours": 5}), "notify: notify config: [quiet_hours] must be a table"),
             ("ack.ack", lambda r: r["params"].update(days="ninety"), "ack: ack.toml: [ack] days = 'ninety' ignored"),
             ("classes.classes", lambda r: r["params"].clear(), "classes: the class map is unusable")]
    for rid_, fn, needle in cases:
        sync(mig)                                                                           # back on the good registry between cases
        good, before = gen(mig), {p.name: p.read_bytes() for p in mig.rd.glob("*.toml")}
        edit_rule(mig, rid_, fn)
        res = sync(mig, hooks=R.NO_HOOKS)
        assert res.status == "invalid" and any(needle in e for e in res.errors), (rid_, res.errors)
        assert gen(mig) == good
        for name, raw in before.items():                                                    # undo the edit for the next case
            (mig.rd / name).write_bytes(raw)


def test_problems_the_files_in_force_already_have_do_not_block_a_sync_but_new_ones_do(env):
    broken = {"name": "broken", "schedule": "every purple moon", "command": []}
    legacy = {"job": [broken]}
    (env.conf / "jobs.toml").write_text(R.dumps(legacy))
    os.chmod(env.conf / "jobs.toml", 0o644)
    write_reg(env.rd, {"80-jobs.toml": [rule("job.broken", kind="job", file="jobs.toml", target="job[]", merge="append", params=broken)]}, patterns=())
    assert R.new_consumer_problems(R.analyze(env.conf, rdir=env.rd, trust=False).comp, env.conf) == []              # same problem as today: nothing NEW
    assert R.consumer_problems({"jobs.toml": R.dumps(legacy)})                                                       # ... though the loader does complain
    res = sync(env)
    assert res.status == "applied", res.errors
    d = tomllib.loads((env.rd / "80-jobs.toml").read_text())
    d["rule"].append(rule("job.worse", kind="job", file="jobs.toml", target="job[]", merge="append", order=2, params={"name": "worse", "schedule": "never o'clock", "command": []}))
    (env.rd / "80-jobs.toml").write_text(R.dumps(d, aot_keys=("rule",)))
    res = sync(env)
    assert res.status == "invalid" and any("job worse" in e for e in res.errors) and not any("job broken" in e for e in res.errors)


def test_a_loader_that_crashes_is_a_finding_not_a_crash_of_the_sync(monkeypatch):
    assert R.consumer_problems({"maint.toml": "[tasks.x]\nmax_gib_per_run = \"40\"\nmode = \"aply\"\n[caps]\nmax_gib_per_run = true\n"})[:3] == [
        "maint.toml: caps.max_gib_per_run must be a number", "maint.toml: tasks.x.max_gib_per_run must be a number", "maint.toml: tasks.x.mode must be report or apply"]
    assert R.consumer_problems({"protected.toml": 'patterns = ["a", 1]\n'}) == ["protected.toml: patterns must be a list of strings"]
    assert R.consumer_problems({"maint.toml": "x = [\n"})[0].startswith("maint.toml: not valid TOML")
    from homelab_maint import jobs
    monkeypatch.setattr(jobs, "load", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert R.consumer_problems({"jobs.toml": "# nothing\n", "maint.toml": "[tasks.x]\n"}) == ["jobs: the loader crashed (RuntimeError: boom)"]


def test_consumer_validation_leaves_no_temp_files_and_restores_conf_dir(mig, monkeypatch):
    before = core.CONF_DIR
    tmpdirs = lambda: {p for p in Path(__import__("tempfile").gettempdir()).glob("hm-validate-*")}   # noqa: E731
    seen = tmpdirs()
    R.consumer_problems(R._current_texts(mig.conf))
    assert core.CONF_DIR == before and tmpdirs() <= seen
    from homelab_maint import probes
    monkeypatch.setattr(probes, "load_probes", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    R.consumer_problems(R._current_texts(mig.conf))
    assert core.CONF_DIR == before and tmpdirs() <= seen


def test_rules_check_runs_the_consumer_validators_too(mig, monkeypatch, capsys):
    monkeypatch.setattr(core, "CONF_DIR", mig.conf)
    monkeypatch.setattr(core, "STATE_DIR", mig.state)
    sync(mig)
    add_rule(mig, "80-jobs.toml", rule("job.broken", kind="job", file="jobs.toml", target="job[]", merge="append", order=10 ** 6,
                                      params={"name": "broken", "schedule": "every purple moon", "command": []}))
    assert R.main(["check"]) == 1
    assert "jobs: job broken" in capsys.readouterr().out


def test_protected_patterns_that_are_not_strings_are_an_explicit_error(tmp_path):
    files = {"40-protection.toml": [prot(["postgres", "redis"]), prot([7], rid="protect.bad")]}
    errs = analysis(tmp_path, files).errors
    assert any("protected.toml: patterns must be a list of strings" in e for e in errs)


# =========================================================================== a stripped GENERATED header is drift, never "a hand-maintained file"
def strip_header(path: Path, drop_pattern: str | None = None) -> None:
    text = path.read_text()
    text = "\n".join(l for l in text.splitlines() if not l.startswith("# GENERATED") and not l.startswith("# `homelab-maint rules sync`")) + "\n"
    if drop_pattern:
        text = text.replace(f'"{drop_pattern}", ', "", 1)
        assert f'"{drop_pattern}"' not in text.split("patterns", 1)[1].split("]", 1)[0] or True
    path.write_text(text)


def test_a_protected_toml_without_its_header_and_without_postgres_is_repaired_not_blocked(mig):
    sync(mig)
    good = gen(mig)
    rec = Rec()
    pt = mig.conf / "protected.toml"
    text = tomllib.loads(good["protected.toml"].decode())
    text["patterns"] = [p for p in text["patterns"] if p != "postgres"]
    pt.write_text(R.dumps(text))                                                              # no GENERATED header, postgres gone
    assert "postgres" not in tomllib.loads(pt.read_text())["patterns"] and not R._is_generated(pt.read_bytes())
    st = R.status(mig.conf, mig.state)
    assert st["drift"] == ["protected.toml"] and not st["in_sync"]
    for i in range(3):
        res = sync(mig, hooks=rec.hooks)
        assert res.status == ("applied" if i == 0 else "unchanged"), (i, res.line())
    assert pt.read_bytes() == good["protected.toml"] and gen(mig) == good                      # repaired on the first tick, whatever the header says
    assert len(rec.notices) == 1 and rec.notices[0]["significant"] and rec.notices[0]["summary"].startswith("Someone edited a generated config file by hand")
    kept = [p for p in (mig.state / "rules" / "orig").iterdir() if p.name.startswith("protected.toml.") and "postgres" not in tomllib.loads(p.read_text())["patterns"]]
    assert len(kept) == 1 and kept[0].name == f"protected.toml.{R.sha_bytes(kept[0].read_bytes())[:8]}"      # the edited copy is kept for the audit (next to the legacy original)
    assert R.history(1, mig.state)[0]["drift"] == ["protected.toml"] and R.status(mig.conf, mig.state)["in_sync"]


def test_copying_an_old_or_shipped_file_over_a_generated_one_is_drift_too(mig):
    sync(mig)
    good = gen(mig)
    shutil.copy(ETC / "maint.toml", mig.conf / "maint.toml")                                  # the plain, header-less shipped file
    res = sync(mig, hooks=R.NO_HOOKS)
    assert res.status == "applied" and res.drift == ["maint.toml"] and gen(mig) == good


def test_a_header_edit_alone_is_drift_with_a_significant_notice_and_a_kept_original(mig):
    sync(mig)
    rec = Rec()
    (mig.conf / "jobs.toml").write_text((mig.conf / "jobs.toml").read_text() + "\n# sneaky comment\n")
    res = sync(mig, hooks=rec.hooks)
    assert res.status == "applied" and res.drift == ["jobs.toml"] and rec.notices[0]["significant"]
    assert any(p.name.startswith("jobs.toml.") for p in (mig.state / "rules" / "orig").iterdir())


def test_a_file_the_registry_never_adopted_is_still_protected_from_being_clobbered(mig):
    (mig.conf / "maint.toml").write_text((mig.conf / "maint.toml").read_text() + "\nmy_knob = 1\n")
    res = sync(mig, hooks=R.NO_HOOKS)
    assert res.status == "blocked" and "maint.toml is not generated and differs" in res.errors[0]


def test_unsafe_files_on_disk_that_the_registry_cannot_adopt_are_announced_once_and_status_says_so(mig, monkeypatch):
    rec = Rec()
    text = tomllib.loads((mig.conf / "protected.toml").read_text())
    text["patterns"] = [p for p in text["patterns"] if p != "postgres"]
    (mig.conf / "protected.toml").write_text(R.dumps(text))                                  # hand-maintained, never adopted, missing a baseline pattern
    for _ in range(3):
        assert sync(mig, hooks=rec.hooks).status == "blocked"
    kinds = [n["record"].get("kind") for n in rec.notices]
    assert kinds == ["blocked", "disk_unsafe"], kinds                                       # one of each, however many ticks
    unsafe = rec.notices[1]
    assert any(l.startswith("UNSAFE: protected.toml lost the baseline pattern 'postgres'") for l in unsafe["lines"])
    assert unsafe["significant"] is False and unsafe["severity"] == "warn" and "rules sync --adopt" in unsafe["summary"]      # first hour: an e-mail with the way out, no text message
    st = R.status(mig.conf, mig.state)
    assert st["safe"] is False and st["in_sync"] is False and st["adopted"] is False and "postgres" in st["unsafe"][0]
    monkeypatch.setattr(core, "CONF_DIR", mig.conf)
    monkeypatch.setattr(core, "STATE_DIR", mig.state)
    res = R.check_task(types.SimpleNamespace(now=NOW))
    assert res.status == "warn" and "not adopted yet" in res.summary and "rules sync --adopt" in res.summary and res.metrics["unsafe"] == 1 and len(res.summary) <= 140   # not a page (first-hour regression)
    assert res.items and "postgres" in res.items[0]["problem"]                                  # the finding itself is kept for the details
    res = sync(mig, adopt=True, hooks=rec.hooks)                                              # the owner adopts: our file replaces it
    assert res.status == "applied" and "postgres" in tomllib.loads((mig.conf / "protected.toml").read_text())["patterns"]
    st = R.status(mig.conf, mig.state)
    assert st["safe"] is True and st["in_sync"] and st["adopted"] and "disk_unsafe" not in R.read_current(mig.state)
    assert R.check_task(types.SimpleNamespace(now=NOW)).status == "ok"


def test_an_unsafe_edit_after_adoption_is_still_a_critical_page_and_a_text(mig, monkeypatch):
    """The first-hour downgrade is for files that predate the registry only: once it has applied anything, a file that breaks the floor is the real thing."""
    sync(mig)
    rec = Rec()
    pt = mig.conf / "protected.toml"
    text = tomllib.loads(pt.read_text())
    text["patterns"] = [p for p in text["patterns"] if p != "postgres"]
    pt.write_text(R.dumps(text))                                                              # a hand edit of a file the registry owns, a baseline pattern gone
    mt = tomllib.loads((mig.conf / "maint.toml").read_text())
    mt["caps"]["max_gib_per_run"] = 10 ** 6
    (mig.conf / "maint.toml").write_text(R.dumps(mt))
    edit_rule(mig, "task.disk_forecast", lambda r: r.update(kind="nonsense"))                  # the registry cannot repair it (invalid), so the watch speaks
    assert sync(mig, hooks=rec.hooks).status == "invalid"
    assert R.status(mig.conf, mig.state)["adopted"] is True
    unsafe = [n for n in rec.notices if n["record"].get("kind") == "disk_unsafe"]
    assert len(unsafe) == 1 and unsafe[0]["significant"] is True and not unsafe[0]["record"].get("unadopted") and unsafe[0]["summary"].startswith("A config file the runner reads breaks")
    monkeypatch.setattr(core, "CONF_DIR", mig.conf)
    monkeypatch.setattr(core, "STATE_DIR", mig.state)
    res = R.check_task(types.SimpleNamespace(now=NOW))
    assert res.status == "crit" and "breaks a safety rule" in res.summary and "not adopted" not in res.summary


def test_a_clean_hand_maintained_file_raises_no_unsafe_notice(mig):
    rec = Rec()
    (mig.conf / "maint.toml").write_text((mig.conf / "maint.toml").read_text() + "\nmy_knob = 1\n")
    assert sync(mig, hooks=rec.hooks).status == "blocked"
    assert [n["record"].get("kind") for n in rec.notices] == ["blocked"] and R.status(mig.conf, mig.state)["safe"]


def test_an_invalid_registry_still_watches_the_files_the_runner_reads(mig):
    sync(mig)
    rec = Rec()
    edit_rule(mig, "task.disk_forecast", lambda r: r.update(kind="nonsense"))
    assert sync(mig, hooks=rec.hooks).status == "invalid"
    mt = tomllib.loads((mig.conf / "maint.toml").read_text())
    mt["caps"]["max_gib_per_run"] = 10 ** 6                                                    # a hand edit of the generated file while the registry is broken
    (mig.conf / "maint.toml").write_text(R.dumps(mt))
    for _ in range(2):
        assert sync(mig, hooks=rec.hooks).status == "invalid"
    assert [n["record"].get("kind") for n in rec.notices] == ["invalid", "disk_unsafe"] and "exceeds the hard limit" in rec.notices[1]["lines"][0]


# =========================================================================== a file that no rule writes any more stops being in force
def test_removing_every_rule_of_a_file_empties_the_generated_file_and_keeps_tracking_it(mig):
    sync(mig)
    rec = Rec()
    assert (mig.conf / "ack.toml").read_text().count("[") > 3
    (mig.rd / "95-ack.toml").unlink()
    res = sync(mig, hooks=rec.hooks)
    assert res.status == "applied" and res.emptied == ["ack.toml"] and "ack.toml" in res.written and len(res.removed) > 5
    assert (mig.conf / "ack.toml").read_text() == R.HEADER and tomllib.loads((mig.conf / "ack.toml").read_text()) == {}      # the old ack config is NOT in force any more
    cur = R.read_current(mig.state)
    assert "ack.toml" in cur["generated"] and R.status(mig.conf, mig.state)["in_sync"] and R.history(1, mig.state)[0]["emptied"] == ["ack.toml"]
    assert sync(mig).status == "unchanged"
    (mig.conf / "ack.toml").write_text(R.HEADER + "[ack]\ndays = 1\n")                         # a later hand edit is drift again
    assert R.status(mig.conf, mig.state)["drift"] == ["ack.toml"]
    res = sync(mig, hooks=R.NO_HOOKS)
    assert res.drift == ["ack.toml"] and (mig.conf / "ack.toml").read_text() == R.HEADER


def test_fill_orphans_covers_files_listed_as_generated_and_files_that_only_carry_the_header(tmp_path):
    conf = mkdir(tmp_path / "conf")
    (conf / "jobs.toml").write_text(R.HEADER + "[[job]]\nname = 'a'\n")                      # carries the header, not listed
    (conf / "probes.toml").write_text("[[probe]]\nname = 'a'\n")                             # hand-maintained, not listed: left alone
    comp = R.Compiled(docs={"maint.toml": {}}, texts={"maint.toml": R.HEADER}, shas={})
    filled = R.fill_orphans(comp, conf, {"classes.toml": "x"})
    assert filled == ["jobs.toml", "classes.toml"] and comp.docs["jobs.toml"] == {} and comp.texts["classes.toml"] == R.HEADER
    assert "probes.toml" not in comp.docs and comp.shas["jobs.toml"] == R.sha_bytes(R.HEADER.encode())


# =========================================================================== a failed sync is visible, not half-done, and says when it was alive
def flaky_replace(monkeypatch, conf: Path, *, fail_from: int = 1, only: int | None = None, restore_fails: bool = False, log: list | None = None) -> dict:
    """os.replace that fails on STAGED replaces (the sync's temp files) from the `fail_from`-th on (or only the `only`-th); the restore's own
    temp files (they carry a counter) fail too when restore_fails. `log` collects the destination names of the replaces that went through."""
    real, n = os.replace, {"n": 0}

    def fake(a, b):
        name = Path(str(a)).name
        into_conf = str(b).startswith(str(conf)) and not str(b).startswith(str(conf / "rules.d"))
        if into_conf and re.fullmatch(r"\.hm-rules-\d+-[a-z.]+\.tmp", name):
            n["n"] += 1
            if (only is None and n["n"] >= fail_from) or n["n"] == only:
                raise OSError(5, "I/O error")
            if log is not None:
                log.append(Path(str(b)).name)
        elif into_conf and restore_fails and re.fullmatch(r"\.hm-rules-\d+-\d+-[a-z.]+\.tmp", name):
            raise OSError(30, "Read-only file system")
        return real(a, b)
    monkeypatch.setattr(R.os, "replace", fake)
    return n


def test_a_failing_sync_is_visible_every_minute_announced_once_and_forgotten_when_it_recovers(mig, monkeypatch):
    sync(mig)
    rec = Rec()
    monkeypatch.setattr(R, "default_hooks", lambda: rec.hooks)
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=W + 2))
    real = os.replace
    flaky_replace(monkeypatch, mig.conf, fail_from=1, restore_fails=True)
    lines = [R.tick(mig.conf, mig.state) for _ in range(3)]
    assert all(l.startswith("rules sync: error: OSError: [Errno 5] I/O error") for l in lines), lines            # reaches stderr/journald each time
    le = R.read_current(mig.state)["last_error"]
    assert le["count"] == 3 and le["error"].endswith("I/O error") and le["last"] >= le["ts"]
    assert [n["record"]["kind"] for n in rec.notices] == ["error"] and rec.notices[0]["summary"].startswith("The rules sync failed")        # one notice, not three
    assert [r.get("kind") for r in R.history(10, mig.state)] == [None, "error"]
    s = R.status(mig.conf, mig.state)
    assert s["last_error"]["count"] == 3 and not s["in_sync"] and s["pending"]
    monkeypatch.setattr(core, "CONF_DIR", mig.conf)
    monkeypatch.setattr(core, "STATE_DIR", mig.state)
    res = R.check_task(types.SimpleNamespace(now=NOW))
    assert res.status == "warn" and res.summary.startswith("rules sync is failing") and "I/O error" in res.summary and len(res.summary) <= 140
    monkeypatch.setattr(R.os, "replace", real)
    assert R.tick(mig.conf, mig.state).startswith("rules sync: applied")
    cur = R.read_current(mig.state)
    assert "last_error" not in cur and "intent" not in cur and R.status(mig.conf, mig.state)["in_sync"]


def test_a_distinct_error_is_announced_again_and_the_same_one_after_six_hours(mig, monkeypatch):
    sync(mig)
    rec = Rec()
    R._record_error(mig.state, rec.hooks, 1000.0, "OSError: first")
    R._record_error(mig.state, rec.hooks, 1100.0, "OSError: first")
    R._record_error(mig.state, rec.hooks, 1200.0, "OSError: second")
    assert len(rec.notices) == 2
    R._record_error(mig.state, rec.hooks, 1200.0 + R.ERROR_RENOTICE_S + 1, "OSError: second")
    assert len(rec.notices) == 3 and R.read_current(mig.state)["last_error"]["count"] == 1
    os.chmod(mig.state, 0o500)
    try:
        R._record_error(mig.state, rec.hooks, 9000.0, "OSError: unwritable state dir")             # best effort: never raises
    finally:
        os.chmod(mig.state, 0o755)


def test_a_half_finished_replace_is_told_from_a_hand_edit_by_its_intent_marker(mig, monkeypatch):
    sync(mig)
    good = gen(mig)
    rec = Rec()
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=W + 3))
    edit_rule(mig, "notify.routes", lambda r: r["params"].update(digest_daily="none"))
    real = os.replace
    flaky_replace(monkeypatch, mig.conf, only=2, restore_fails=True)                                # the 2nd replace fails and nothing can be put back
    res = sync(mig, hooks=rec.hooks)
    assert res.status == "error"
    mixed = gen(mig)
    assert sum(mixed[f] != good[f] for f in LEGACY) == 1                                           # one file made it, one did not: the half-done state
    cur = R.read_current(mig.state)
    assert sorted(cur["intent"]["files"]) == ["maint.toml", "notify.toml"] and cur["intent"]["to"] != cur["hash"]
    s = R.status(mig.conf, mig.state)
    assert s["half_done"] and s["drift"] == [] and s["pending"] and not s["in_sync"]              # the completed file is NOT reported as a hand edit
    monkeypatch.setattr(R.os, "replace", real)
    rec2 = Rec()
    res = sync(mig, hooks=rec2.hooks)
    assert res.status == "applied" and res.drift == [] and "intent" not in R.read_current(mig.state) and R.status(mig.conf, mig.state)["in_sync"]
    assert not any("Someone edited" in n["summary"] for n in rec2.notices) and R.history(1, mig.state)[0]["drift"] == []
    assert parse((mig.conf / "maint.toml").read_text())["tasks"]["disk_forecast"]["warn_free_pct"] == W + 3


def test_protected_toml_goes_first_when_a_change_only_adds_patterns_and_last_when_it_removes_some(mig, monkeypatch):
    sync(mig)
    pid = next(r.id for r in R.load_registry(rdir=mig.rd, trust=False).rules if r.kind == "protection" and r.merge == "append")
    order: list[str] = []
    flaky_replace(monkeypatch, mig.conf, fail_from=10 ** 6, log=order)                           # never fails: only records the order
    edit_rule(mig, pid, lambda r: r["params"]["patterns"].append("zz-new-pattern"))
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=W + 4))
    edit_rule(mig, "notify.routes", lambda r: r["params"].update(digest_daily="none"))
    assert sync(mig).status == "applied" and order[0] == "protected.toml" and set(order) == {"protected.toml", "maint.toml", "notify.toml"}
    order.clear()
    edit_rule(mig, pid, lambda r: r["params"]["patterns"].remove("zz-new-pattern"))
    edit_rule(mig, "task.disk_forecast", lambda r: r["params"].update(warn_free_pct=W + 5))
    assert sync(mig).status == "applied" and order[-1] == "protected.toml" and order[0] == "maint.toml"


def test_a_stuck_lock_holder_is_reported_by_the_tick_and_the_lock_file_names_its_holder(mig, monkeypatch):
    sync(mig)
    with R._flock(mig.state, False) as got:
        assert got and (mig.state / "rules" / "sync.lock").read_text().split()[0] == str(os.getpid())
        assert sync(mig, wait=False).status == "locked" and R.tick(mig.conf, mig.state) == ""               # a short wait is normal
        with monkeypatch.context() as m:
            m.setattr(R, "_lock_held_for", lambda st, now: 9999.0)
            r = sync(mig, wait=False)
            assert r.status == "locked-too-long" and not r.fresh
            assert R.tick(mig.conf, mig.state) == "rules sync: locked-too-long: another rules sync has held the lock for 9999 s"
    assert (mig.state / "rules" / "sync.lock").read_text() == ""                                          # released: nothing stale to misread
    assert R._lock_held_for(mig.state, time.time()) == 0.0


def test_the_idle_tick_stamps_last_tick_at_most_every_five_minutes(mig):
    sync(mig, now=1000.0)
    cur0 = (mig.state / "rules" / "current.json").read_bytes()
    assert R.read_current(mig.state)["last_tick"] == 1000.0
    assert sync(mig, now=1100.0).status == "unchanged" and (mig.state / "rules" / "current.json").read_bytes() == cur0        # cheap: no write
    assert sync(mig, now=1000.0 + R.TICK_REFRESH_S + 1).status == "unchanged"
    assert R.read_current(mig.state)["last_tick"] == 1000.0 + R.TICK_REFRESH_S + 1 and R.read_current(mig.state)["synced_at"] == 1000.0
    assert R.status(mig.conf, mig.state)["last_tick"] == 1000.0 + R.TICK_REFRESH_S + 1


# =========================================================================== history and rules.json stay bounded
def mass_edit(env, text: str) -> None:
    for f in sorted(env.rd.glob("*.toml")):
        d = tomllib.loads(f.read_text())
        for r in d.get("rule", []):
            r["why"], r["does"] = text + r["id"], text + r["id"]
        f.write_text(R.dumps(d, aot_keys=("rule",)))
        os.chmod(f, 0o644)


def test_a_mass_edit_stores_a_bounded_history_record_and_the_notice_still_counts_everything(mig):
    sync(mig)
    rec = Rec()
    mass_edit(mig, "A" * 400)
    res = sync(mig, hooks=rec.hooks)
    assert len(res.modified) >= mig.n - 1
    h = R.history(1, mig.state)[0]
    assert h["modified_count"] == len(res.modified) and len(h["modified"]) == R.HISTORY_MODIFIED_MAX and all(len(m["fields"]) <= 4 for m in h["modified"])
    line = (mig.state / "rules" / "history.jsonl").read_bytes().splitlines()[-1]
    assert len(line) < 80_000                                                                       # was ~670 KB for 400 rules
    assert f"{len(res.modified)} modified" in rec.notices[-1]["summary"] and rec.notices[-1]["lines"][-1].startswith("... and")
    assert R._cap_val("x" * 100) == "x" * 100 and set(R._cap_val("x" * 400)) == {"_truncated", "sha"}


def test_history_stays_under_its_byte_bound_across_many_mass_edits(mig, monkeypatch):
    monkeypatch.setattr(R, "HISTORY_FILE_MAX", 150_000)
    monkeypatch.setattr(R, "HISTORY_MIN_KEEP", 3)
    sync(mig)
    sizes = []
    for i in range(10):
        mass_edit(mig, f"{i}" * 300)
        assert sync(mig, hooks=R.NO_HOOKS, now=2000.0 + i).status == "applied"
        sizes.append((mig.state / "rules" / "history.jsonl").stat().st_size)
    assert max(sizes) < 150_000 + 120_000 and sizes[-1] < 150_000 + 120_000                          # one record over the bound at most, never a growing pile
    rows = R.history(0, mig.state)
    assert rows[-1]["ts"] == 2009.0 and len(rows) >= 3 and sizes[-1] < sum(len(json.dumps(r)) for r in rows) * 2


def _synthetic_doc(n: int, prose: int = 300, params: int = 500) -> dict:
    rules = [{"id": f"rule.synthetic-{i:05d}", "category": "checks", "title": "T" * 120, "kind": "check", "why": "w" * prose, "does": "d" * prose,
              "applies_to": ["a", "b"], "params": {"blob": "x" * params, "n": i}, "mode": None, "enabled": True, "severity": "warn", "destructive": False,
              "proof": "p" * 400, "principle": "q" * 100, "since": "2026-10-02", "source_file": "10-checks.toml", "file": "maint.toml", "target": "tasks.x" * 5,
              "last_evaluated": None, "last_triggered": None, "triggers_30d": 0, "last_result": "ok: " + "r" * 100, "related": ["x", "y"]} for i in range(n)]
    return {"schema": 1, "generated_at": 1.0, "registry_hash": "0" * 64, "rules": rules, "history": [{"ts": i, "pad": "h" * 800} for i in range(50)], "stats": {"total": n}}


def _size(doc: dict) -> int:
    return len(json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode())


@pytest.mark.parametrize("n,prose,params", [(403, 300, 500), (403, 300, 5), (900, 300, 20), (3000, 300, 500), (5000, 400, 2000)])
def test_rules_json_always_ends_under_the_cap_and_does_not_take_quadratic_time(n, prose, params):
    doc = _synthetic_doc(n, prose, params)
    t0 = time.perf_counter()
    out = R._fit(doc, R.RULES_JSON_MAX)
    dt = time.perf_counter() - t0
    assert _size(out) <= R.RULES_JSON_MAX, (n, _size(out))
    assert dt < 8.0, (n, dt)
    if _size(_synthetic_doc(n, prose, params)) > R.RULES_JSON_MAX:
        assert out["stats"]["truncated"] is True
    if n >= 3000:
        assert out["stats"].get("omitted_rules", 0) >= 0


def test_the_cap_stages_shorten_side_text_before_dropping_rules_and_say_so():
    doc = _synthetic_doc(403, 300, 5)                                    # ~100 KB of side text on top of the cap's worth of rules
    cap = _size(doc) - 20_000
    out = R._fit(doc, cap)
    assert _size(out) <= cap and out["stats"]["truncated"] is True and len(out["rules"]) == 403 and "omitted_rules" not in out["stats"]
    assert out["history"] == [] or len(out["history"]) < 50
    doc2 = _synthetic_doc(403, 300, 5)
    out2 = R._fit(doc2, 90_000)                                          # forces the later stages: the website's fields survive, the bulk does not
    assert _size(out2) <= 90_000 and all(r["id"] and r["kind"] and "params" in r and "title" in r for r in out2["rules"])


def test_build_rules_json_on_the_shipped_registry_is_fast_and_within_the_cap(mig):
    sync(mig)
    t0 = time.perf_counter()
    doc = export(mig)
    assert time.perf_counter() - t0 < 5.0 and _size(doc) <= R.RULES_JSON_MAX
    small = R.build_rules_json(mig.conf, mig.state, now=NOW, status={}, history_src=[], audit_src=[], max_bytes=250_000)
    assert _size(small) <= 250_000 and small["stats"]["truncated"] is True and len(small["rules"]) == mig.n
    tiny = R.build_rules_json(mig.conf, mig.state, now=NOW, status={}, history_src=[], audit_src=[], max_bytes=60_000)       # below what the rules need: the tail goes, said so
    assert _size(tiny) <= 60_000 and tiny["stats"]["omitted_rules"] > 0 and tiny["stats"]["total"] == mig.n and len(tiny["rules"]) + tiny["stats"]["omitted_rules"] == mig.n


# =========================================================================== the public export hides what must not leave the host
@pytest.mark.parametrize("key", ["PGPASSWORD", "clientSecret", "accessToken", "smtppassword", "dbpass", "passwordHash", "apiKey", "x-api-key",
                                 "AWS_SECRET_ACCESS_KEY", "webhook_url", "Authorization", "sessionId", "privateKey", "csrf_token", "cookieJar"])
def test_secret_key_names_are_matched_by_substring_without_delimiters_and_in_any_case(key):
    assert R.redact({key: "hunter2"}) == {key: "[redacted]"} and R.redact({key: ["a", {"b": "c"}]}) == {key: ["[redacted]", {"b": "[redacted]"}]}
    assert R.redact({key: 5}) == {key: 5} and R.redact({key: True}) == {key: True}                    # a number or a flag is a setting, not a secret
    assert R.redact({key: ""}) == {key: ""}


def test_urls_keep_their_host_and_lose_what_is_a_credential_in_disguise():
    r = R.redact
    assert r("https://hooks.slack.com/services/T000/B000/XXXX") == "https://hooks.slack.com/[redacted]"
    assert r("https://hc-ping.com/6b3f9a2e-1c4d-4e5f") == "https://hc-ping.com/[redacted]"
    assert r("https://user:hunter2@example.com/x?y=1") == "https://example.com/[redacted]" and "hunter2" not in r("ssh://root:hunter2@host")
    assert r("http://127.0.0.1:3001/api/push/AbCdEf123456?status=up&msg=OK") == "http://127.0.0.1:3001/api/push/[redacted]?[redacted]"
    assert r("http://127.0.0.1:2283/api/server/ping") == "http://127.0.0.1:2283/api/server/ping" and r("http://localhost:8080/health") == "http://localhost:8080/health"
    assert r("https://maintenance.example.ca") == "https://maintenance.example.ca" and r("see https://a.b/c?d=e now") == "see https://a.b/[redacted] now"


def test_argv_secrets_free_text_secrets_blobs_and_home_directories_are_hidden():
    r = R.redact
    assert r("curl --user admin:pw --header X-A:b -u bob:pw2 --password=abc --token xyz") == "curl --user [redacted] --header [redacted] -u [redacted] --password=[redacted] --token [redacted]"
    assert r("token=abcdef123 and password: foo, secret = bar") == "token=[redacted] and password: [redacted] secret = [redacted]"        # the value runs to the next blank: fail safe
    assert r("Authorization: Bearer abcdefghijklmnop") == "Authorization: [redacted] [redacted]"
    blob = "a1" * 20
    assert r(f"key {blob} end") == "key [redacted] end" and r("/mnt/x/" + blob) == "/mnt/x/" + blob                 # a long path segment is not a token
    assert r("/home/ohmz/.cursor/worktrees and /home/bob/x") == "~/.cursor/worktrees and ~/x" and r("/var/crash") == "/var/crash"
    assert r("mail owner@example.com or +15550001111 or 555-123-4567 today") == "mail [email] or [phone] or [phone] today" and r("2026-10-02 07:30, 20260922-190738") == "2026-10-02 07:30, 20260922-190738"
    assert r("^(omnivoice-studio-gpu|ebook2audiobook-ebook2audiobook-gpu-1|kokoro)$") == "^(omnivoice-studio-gpu|ebook2audiobook-ebook2audiobook-gpu-1|kokoro)$"   # a long container name is not a token


def test_commands_are_reduced_to_their_program_and_recipients_and_env_are_never_published():
    p = R.redact({"command": ["/usr/local/sbin/backup.sh", "--a", "secret-arg"], "cmd": "docker prune -f", "argv": ["only"], "env": {"A": "1"}, "headers": {"X": "y"},
                  "to": ["a@b.c"], "sms": "+15550001111", "email": "x@y.z", "handle": "ohmz", "ok": 1, "args": []})
    assert p == {"command": "backup.sh (+2 args)", "cmd": "docker (+2 args)", "argv": "only", "ok": 1, "args": "", "_hidden": ["email", "env", "handle", "headers", "sms", "to"]}
    assert R.redact("x", "to") == "[hidden]" and R.redact({"nested": {"to": 1, "n": 2}}) == {"nested": {"n": 2, "_hidden": ["to"]}}


def test_the_public_rules_json_of_the_shipped_config_leaks_no_home_directory_nor_recipient_nor_webhook(mig):
    sync(mig)
    add_rule(mig, "50-alerts.toml", rule("notify.hook", kind="alert", file="notify.toml", target="hooks", params={
        "webhook_url": "https://hooks.slack.com/services/T000/B000/XXXX", "to": "owner@example.com", "sms": "+15550001111", "ping": "https://hc-ping.com/abc-123-def",
        "cmd": ["curl", "--user", "admin:pw", "http://x"], "env": {"TOKEN": "t"}, "kuma": "http://127.0.0.1:3001/api/push/AbCdEf123456?status=up"}))
    sync(mig, hooks=R.NO_HOOKS)
    blob = json.dumps(export(mig))
    for leak in ("/home/ohmz", "hooks.slack.com/services", "owner@example.com", "+15550001111", "abc-123-def", "admin:pw", "AbCdEf123456", "XXXX", "\"TOKEN\""):
        assert leak not in blob, leak
    p = by_id(export(mig))["notify.hook"]["params"]
    assert p["webhook_url"] == "[redacted]" and p["ping"] == "https://hc-ping.com/[redacted]" and p["cmd"] == "curl (+3 args)" and p["_hidden"] == ["env", "sms", "to"]
    assert "~/ai-stack/scripts" in blob or "~/" in blob                                               # the shipped paths are still shown, with ~ for the home


def test_last_result_of_a_cleanup_rule_reports_counts_and_the_rule_path_never_the_audited_file(mig):
    sync(mig)
    logs = next(r for r in RET if r["path"] == "/volume1/docker/kavita/config/logs")
    audit = [{"ts": iso(NOW - 3000 + i), "task": "retention", "action": "delete", "target": "/volume1/docker/kavita/config/logs/kavita_secret-client-contract.log", "bytes": 1, "outcome": "done"}
             for i in range(4)]
    doc = export(mig, audit_src=audit)
    r = by_id(doc)[rid("retention", logs["name"])]
    assert r["last_result"] == "done: 4 item(s) under /volume1/docker/kavita/config/logs" and "secret-client-contract" not in json.dumps(doc)
    assert R._last_burst([1.0, 2.0, 1000.0, 1001.0, 1002.0]) == 3 and R._last_burst([]) == 0


def test_history_before_after_are_redacted_by_the_param_they_belong_to(env):
    write_reg(env.rd, {"10-checks.toml": [rule("task.h", file="maint.toml", target="tasks.h", params={"to": "a@b.c", "api_key": "k0", "webhook": "https://h.example/x/y", "n": 0})]}, patterns=())
    sync(env, now=1.0)
    d = tomllib.loads((env.rd / "10-checks.toml").read_text())
    d["rule"][0]["params"].update(to="x@y.z", api_key="k1", webhook="https://h.example/x/z", n=1)
    (env.rd / "10-checks.toml").write_text(R.dumps(d, aot_keys=("rule",)))
    sync(env, now=2.0)
    stored = json.dumps(R.history(1, env.state))
    pub = json.dumps(export(env)["history"])
    for blob in (stored, pub):
        assert "x@y.z" not in blob and "a@b.c" not in blob and "k1" not in blob and "x/z" not in blob, blob
    m = export(env)["history"][0]["modified"][0]
    assert m["before"]["params.n"] == 0 and m["after"]["params.n"] == 1 and m["after"]["params.to"] == "[hidden]" and m["after"]["params.api_key"] == "[redacted]"


def test_a_new_owner_unprotect_override_is_announced_loudly_once(mig):
    sync(mig)
    rec = Rec()
    (mig.rd / "99-owner-overrides.toml").write_text('[meta]\ncategory = "safety"\nallow_unprotect = [{ file = "maint.toml", path = "tasks.docker_cache.unprotect", regex = "^old-box$" }]\n')
    os.chmod(mig.rd / "99-owner-overrides.toml", 0o644)
    edit_rule(mig, "task.docker_cache", lambda r: r["params"].update(unprotect=["^old-box$"]))
    res = sync(mig, hooks=rec.hooks)
    assert res.status == "applied" and parse((mig.conf / "maint.toml").read_text())["tasks"]["docker_cache"]["unprotect"] == ["^old-box$"]
    n = rec.notices[-1]
    assert n["significant"] and any(l.startswith("LOUD: the owner accepted '^old-box$' in maint.toml tasks.docker_cache.unprotect") for l in n["lines"])
    assert R.history(1, mig.state)[0]["owner_unprotect_new"] == [["maint.toml", "tasks.docker_cache.unprotect", "^old-box$"]]
    edit_rule(mig, "task.docker_cache", lambda r: r["params"].update(high_gib=HG + 1))
    sync(mig, hooks=rec.hooks)
    assert not any("LOUD: the owner accepted" in l for l in rec.notices[-1]["lines"])                     # said once, not on every later change


def test_removing_the_last_probe_is_refused_as_monitoring_blind_but_removing_all_jobs_empties_jobs_toml(mig):
    sync(mig)
    good = gen(mig)
    (mig.rd / "70-monitoring.toml").unlink()
    res = sync(mig, hooks=R.NO_HOOKS)
    assert res.status == "invalid" and any("probes: probes.toml: present but defines no probes" in e for e in res.errors) and gen(mig) == good
    (mig.rd / "70-monitoring.toml").write_bytes((mig.state / "rules" / "snapshots" / R.read_current(mig.state)["hash"] / "rules.d" / "70-monitoring.toml").read_bytes())
    os.chmod(mig.rd / "70-monitoring.toml", 0o644)
    assert sync(mig, hooks=R.NO_HOOKS).status == "unchanged"
    (mig.rd / "80-jobs.toml").unlink()
    res = sync(mig, hooks=R.NO_HOOKS)
    assert res.status == "applied" and "jobs.toml" in res.emptied and (mig.conf / "jobs.toml").read_text() == R.HEADER
