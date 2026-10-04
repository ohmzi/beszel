"""Tests for the SHIPPED registry: etc/rules.d, the release's one definition of what the script does (SPEC6 v2).

tests/test_registry.py proves the machinery on a registry it migrates itself; this file pins the files that actually ship:
  a. compile(etc/rules.d) written into a tmp dir parses to EXACTLY (types, list order) every shipped etc/*.toml (the 8 managed files),
     passes every safety check against the release floor, and a first `rules sync` into a scratch conf dir adopts without a change;
  b. the content phase is DONE and stays done: no placeholder anywhere, real why/does text on every rule, a proof on every destructive one,
     the destructive flag follows the task class, the shipped config mutates nothing but the reclaim rung, every registered task and
     job is named by a rule, the public rules.json shows the text untouched (no scrubber marker, nothing cut), and the prose of the probe,
     job and live-tile rules says what their own config says (class, interval, timeout, container ...: the template facts are re-derived);
  c. rule ids are unique, well formed and STABLE: the pinned ids below may not vanish or be renamed (a rename orphans the rule's history
     and the website's links); retiring one is a deliberate edit of RETIRED.
Everything runs in tmp dirs with explicit conf/state paths; nothing is sent and nothing under /etc, /var or /usr is touched.
"""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path

import pytest

from homelab_maint import probes as PR
from homelab_maint import registry as R

ROOT = Path(__file__).resolve().parent.parent
ETC = ROOT / "etc"
RD = ETC / "rules.d"
LEGACY = list(R.MANAGED_FILES)                                                  # the 8 files the registry generates
FILE_RX = R.REG_FILE_RX                                                         # NN-name.toml, as the loader reads it


def mkdir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o755)                                                          # the registry refuses a group/world-writable conf dir
    return p


def raw_rules() -> dict[str, dict]:
    """registry file name -> its parsed document, straight from tomllib (no registry code in between)."""
    return {p.name: tomllib.loads(p.read_text()) for p in sorted(RD.glob("*.toml"))}


def strings(v):
    """Every string inside a parsed value (a rule's text fields and its params alike)."""
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from strings(x)


def todo_left(rules: list[dict]) -> dict[str, list[str]]:
    """rule id -> the fields that still hold the TODO-CONTENT marker (the file headers may mention it: only rule fields count)."""
    out = {}
    for r in rules:
        bad = [k for k, v in r.items() if any(R.TODO in s for s in strings(v))]
        if bad:
            out[r["id"]] = bad
    return out


@pytest.fixture(autouse=True)
def nothing_is_sent(monkeypatch):
    """A sync would notify and journal through notify.py / routine.py: fail loudly if a test ever reaches for either."""
    def boom(_payload):
        raise AssertionError("a registry test must not send a notice")
    monkeypatch.setattr(R, "_notice_via_notify", boom)
    monkeypatch.setattr(R, "_journal_via_routine", lambda p: True)
    monkeypatch.setattr(R.os, "fsync", lambda fd: None)


@pytest.fixture(scope="module")
def an() -> R.Analysis:
    return R.analyze(rdir=RD, trust=False)                                        # release floor ON (FLOOR_ENFORCED is True by default)


@pytest.fixture(scope="module")
def compiled(an, tmp_path_factory) -> dict[str, Path]:
    """The generated files, written the way sync() would put them (legacy file name -> path in a tmp dir)."""
    d = mkdir(tmp_path_factory.mktemp("compiled"))
    assert an.comp is not None, an.errors
    for name, text in an.comp.texts.items():
        (d / name).write_text(text)
    return {n: d / n for n in an.comp.texts}


# =========================================================================== registry files
def test_the_shipped_registry_is_valid_and_passes_the_release_floor(an):
    assert an.ok, an.errors
    assert an.errors == [] and an.removals == []
    assert R.FLOOR_ENFORCED, "the shipped registry must be judged against the pinned release floor"
    assert an.reg.baseline is not None and an.baseline is not None and len(an.baseline["protected_patterns"]) >= len(R.PROTECTED_FLOOR)
    assert an.warnings == [], an.warnings                                           # not even a placeholder count: the content phase is done


def test_every_registry_file_has_meta_and_a_name_that_matches_its_category():
    docs = raw_rules()
    assert docs and R.BASELINE_FILE in docs and R.OVERRIDES_FILE not in docs        # the owner's overrides are never shipped
    prefixes = []
    for name, doc in docs.items():
        m = FILE_RX.fullmatch(name)
        assert m, f"{name}: registry files are NN-name.toml"
        meta = doc.get("meta")
        assert isinstance(meta, dict) and set(meta) == {"category", "title", "blurb"}, f"{name}: [meta] needs exactly category, title, blurb"
        assert all(isinstance(meta[k], str) and meta[k].strip() for k in meta), f"{name}: empty [meta] text"
        assert meta["category"] in R.CATEGORIES, f"{name}: unknown category {meta['category']!r}"
        if name != R.BASELINE_FILE:
            assert m.group(2) == meta["category"], f"{name}: the file is named for another category than its [meta] ({meta['category']})"
        assert doc.get("rule") and all(isinstance(r, dict) for r in doc["rule"]), f"{name}: no rules"
        assert set(doc) <= {"meta", "rule", "baseline"} and ("baseline" in doc) == (name == R.BASELINE_FILE)
        prefixes.append(m.group(1))
    assert prefixes == sorted(prefixes) and prefixes[0] == "00"                    # load order is the file order
    assert {d["meta"]["category"] for d in docs.values()} == set(R.CATEGORIES)    # no category is empty: the website lists them all


def test_registry_files_are_plain_regular_files_within_the_size_limits():
    for p in RD.glob("*.toml"):
        assert p.is_file() and not p.is_symlink() and p.stat().st_size < R.MAX_FILE_BYTES, p
    assert sum(len(d["rule"]) for d in raw_rules().values()) == len(R.load_registry(rdir=RD, trust=False).rules) <= R.MAX_RULES


def test_counts_per_file_and_per_category_add_up(an):
    docs = raw_rules()
    per_file = {n: len(d["rule"]) for n, d in docs.items()}
    per_cat = Counter()
    for d in docs.values():
        per_cat[d["meta"]["category"]] += len(d["rule"])
    reg = an.reg
    assert sum(per_file.values()) == sum(per_cat.values()) == len(reg.rules)
    assert dict(Counter(r.category for r in reg.rules)) == dict(per_cat)             # what the website's category list will show
    assert dict(Counter(r.source for r in reg.rules)) == per_file
    print("\nshipped rules per file:     " + ", ".join(f"{n[:-5]}={c}" for n, c in per_file.items()) + f"  (total {len(reg.rules)})")      # pytest -s
    print("shipped rules per category: " + ", ".join(f"{k}={v}" for k, v in sorted(per_cat.items())))


# =========================================================================== a. compile(rules.d) == the shipped config files
def test_compile_of_rules_d_equals_every_shipped_etc_file(compiled):
    assert sorted(compiled) == sorted(LEGACY), "the registry must generate exactly the 8 managed files"
    for name in LEGACY:
        want = tomllib.loads((ETC / name).read_text())
        got = tomllib.loads(compiled[name].read_text())
        diffs = R.diff_docs(want, got)
        assert not diffs, f"{name}: compile(etc/rules.d) differs from etc/{name} at {len(diffs)} place(s); first: {diffs[:3]}"
        assert R.same(want, got), f"{name}: equal values of different TOML types (1 vs 1.0, ...)"          # diff_docs is the readable report
        assert compiled[name].read_text().startswith(R.GENERATED_MARK)


def test_the_equality_proof_of_migrate_holds_for_the_shipped_registry(an):
    docs, _raw = R.load_legacy(ETC)
    proof = R.prove(docs, an)
    assert proof.ok, (proof.errors, {f: d for f, d in proof.files.items() if d})
    assert sorted(proof.files) == sorted(LEGACY) and all(d == [] for d in proof.files.values())


def test_every_rule_that_writes_a_file_is_named_in_the_generated_files(an):
    """Nothing in the registry is dead weight and no table of the generated files lacks an author (`# rule: ID` comments)."""
    named = {i for t in an.comp.texts.values() for line in re.findall(r"^\s*# rule: (.+)$", t, re.M) for i in re.split(r"[ ,]+", line.strip())}
    writers = {r.id for r in an.reg.rules if r.file and r.enabled}
    assert writers - named == set(), f"rules that left no trace in the generated files: {sorted(writers - named)[:5]}"
    assert named <= {r.id for r in an.reg.rules}, "a generated file names a rule that does not exist"


def test_a_first_sync_into_a_scratch_conf_adopts_the_shipped_files_without_changing_their_data(tmp_path):
    conf, state = mkdir(tmp_path / "conf"), mkdir(tmp_path / "state")
    for f in ETC.glob("*.toml"):
        shutil.copy(f, conf / f.name)
        os.chmod(conf / f.name, 0o644)
    mkdir(conf / "rules.d")
    for f in RD.glob("*.toml"):
        shutil.copy(f, conf / "rules.d" / f.name)
        os.chmod(conf / "rules.d" / f.name, 0o644)
    before = {f: tomllib.loads((conf / f).read_text()) for f in LEGACY}
    res = R.sync(conf, state, hooks=R.NO_HOOKS, now=1000.0)
    assert res.status == "applied" and res.errors == [] and res.drift == [], (res.status, res.errors)
    assert sorted(res.written) == sorted(LEGACY)
    for f in LEGACY:
        text = (conf / f).read_text()
        assert text.startswith(R.GENERATED_MARK) and R.same(tomllib.loads(text), before[f]), f
    assert R.sync(conf, state, hooks=R.NO_HOOKS, now=1001.0).status == "unchanged"     # idempotent: the tick's per-minute call finds nothing to do
    assert (state / "rules" / "orig").is_dir()                                         # the hand-maintained originals were kept, once


def test_the_first_hour_on_the_owners_older_config_is_a_warning_with_the_way_out_not_a_page(tmp_path, monkeypatch):
    """`sudo ./install.sh` installs rules.d next to the owner's Oct-1 maint.toml (the tunarr cache exemption sat under `retention`; the floor moved it
    to `app_cache_trim`). Until `rules sync --adopt` the sync is blocked and replaces nothing, and the check must say so WITHOUT a critical page or
    a text message (it used to go CRIT, and page after two check runs, in the first hour of every install)."""
    from homelab_maint import core
    conf, state = mkdir(tmp_path / "conf"), mkdir(tmp_path / "state")
    for f in ETC.glob("*.toml"):
        shutil.copy(f, conf / f.name)
        os.chmod(conf / f.name, 0o644)
    mt = tomllib.loads((conf / "maint.toml").read_text())
    rx = mt["tasks"]["app_cache_trim"].pop("unprotect")                                  # the Oct-1 shape: the exemption under the older retention task
    mt["tasks"]["retention"]["unprotect"] = [r"StudioProjects/tunarr/\.docker-data/tunarr/cache/subtitles"]
    assert rx
    (conf / "maint.toml").write_text(R.dumps(mt))
    mkdir(conf / "rules.d")
    for f in RD.glob("*.toml"):
        shutil.copy(f, conf / "rules.d" / f.name)
        os.chmod(conf / "rules.d" / f.name, 0o644)
    notices: list[dict] = []
    hooks = R.Hooks(notify=lambda p: notices.append(p) or True)
    monkeypatch.setattr(core, "CONF_DIR", conf)
    monkeypatch.setattr(core, "STATE_DIR", state)
    ctx = type("Ctx", (), {"now": 5000.0})()
    for i in range(3):                                                                    # the per-minute tick, three times: one notice each, however many
        assert R.sync(conf, state, hooks=hooks, now=1000.0 + 60 * i).status == "blocked"
    assert (conf / "maint.toml").read_text() == R.dumps(mt)                              # nothing was replaced
    kinds = [n["record"]["kind"] for n in notices]
    assert kinds == ["blocked", "disk_unsafe"] and not any(n["significant"] for n in notices), kinds     # e-mails, no text
    assert "rules sync --adopt" in notices[1]["summary"]
    st = R.status(conf, state)
    assert st["adopted"] is False and st["safe"] is False and "retention" in st["unsafe"][0]
    res = R.check_task(ctx)
    assert res.status == "warn" and "not adopted yet" in res.summary and "rules sync --adopt" in res.summary and len(res.summary) <= 140
    res = R.sync(conf, state, hooks=hooks, now=2000.0, adopt=True)                       # the runbook's step: adopt
    assert res.status == "applied" and (state / "rules" / "orig").is_dir()
    st = R.status(conf, state)
    assert st["adopted"] is True and st["safe"] is True and st["in_sync"] is True
    assert R.check_task(ctx).status == "ok"


def test_compiling_is_deterministic_and_independent_of_the_files_on_disk(an, tmp_path):
    """Same rules -> same bytes and same registry hash, whatever the mtimes, copy order or directory the files sit in."""
    d = mkdir(tmp_path / "rules.d")
    for i, f in enumerate(sorted(RD.glob("*.toml"), reverse=True)):
        shutil.copy(f, d / f.name)
        os.utime(d / f.name, (1_000_000 + i * 7, 1_000_000 + i * 7))
    again = R.analyze(rdir=d, trust=False)
    assert again.ok and again.comp.texts == an.comp.texts and again.comp.shas == an.comp.shas
    assert R.registry_hash(R.scan_registry(rdir=d)[0]) == R.registry_hash(R.scan_registry(rdir=RD)[0])


# =========================================================================== b. the content phase is done: these tests keep it done
PLACEHOLDER_CS = re.compile(r"TODO|TBD|FIXME|XXX|\?\?\?")                         # case-sensitive: the shipped rule notify.todo is a real name
PLACEHOLDER_CI = re.compile(r"lorem ipsum|to be written|fill in later|placeholder|coming soon|not yet written", re.I)
MIN_TITLE, MAX_TITLE, MIN_WHY, MIN_DOES, MIN_PROOF = 8, 120, 60, 40, 40          # characters (the shortest shipped: title 12, why 74, does 54)
TEXT_FIELDS = ("title", "why", "does", "proof", "owner_notes", "principle")
ORPHAN_OK = {"rules_registry": "the registry's own self-check, registered at runtime by registry.register_tasks(): it has no tunable knob, so no "
                               "rule configures it. registry.RUNTIME_TASKS makes known_names / task_catalog know it (a rule MAY name it in "
                               "applies_to: tests/test_registry.py); the rules.d content phase is closed, so naming it is a content change, "
                               "and until then this entry is the explicit, reasoned exemption"}


def all_rules() -> list[dict]:
    return [r for d in raw_rules().values() for r in d["rule"]]


def test_no_rule_still_carries_the_todo_content_marker_after_the_content_phase():
    rules = all_rules()
    left = todo_left(rules)
    by_field = Counter(f for fs in left.values() for f in fs)
    assert not left, (f"{len(left)} of {len(rules)} rules contain {R.TODO} (fields: {dict(by_field)}); first: {sorted(left)[:5]}. "
                      f"Write real why/does/proof text for them (`homelab-maint rules check --todo` lists them).")


def test_no_placeholder_text_remains_anywhere_in_etc_rules_d():
    """Any line counts, not only rule fields: a header comment, a [meta] blurb or a param string saying TODO is unfinished work too."""
    hits = [f"{p.name}:{n}: {ln.strip()[:60]}" for p in sorted(RD.glob("*.toml")) for n, ln in enumerate(p.read_text().splitlines(), 1)
            if R.TODO in ln or PLACEHOLDER_CS.search(ln) or PLACEHOLDER_CI.search(ln)]
    assert not hits, f"{len(hits)} placeholder line(s); first: {hits[:3]}"


def test_every_rule_has_real_text_and_every_destructive_rule_a_proof():
    short = []
    for r in all_rules():
        t = str(r.get("title") or "").strip()
        if not MIN_TITLE <= len(t) <= MAX_TITLE:
            short.append(f"{r['id']}.title ({len(t)})")
        for f, lo in (("why", MIN_WHY), ("does", MIN_DOES)):
            if len(str(r.get(f) or "").strip()) < lo:
                short.append(f"{r['id']}.{f} (< {lo})")
        if r.get("destructive") is True and len(str(r.get("proof") or "").strip()) < MIN_PROOF:
            short.append(f"{r['id']}.proof (< {MIN_PROOF}, destructive)")
        if str(r.get("why") or "").strip() == str(r.get("does") or "").strip():
            short.append(f"{r['id']}: why equals does")
    assert not short, f"{len(short)} thin text field(s): {short[:6]}"


def test_rule_text_is_clean_single_line_ascii():
    """These strings are one paragraph on the website and travel through rules.json and the change notices: a TOML line continuation joined
    them already, so no newline, no double space, no padding and no non-ASCII (smart quotes, dashes) may be left."""
    bad = [f"{r['id']}.{f}" for r in all_rules() for f in TEXT_FIELDS
           if (v := r.get(f)) is not None and (not isinstance(v, str) or v != v.strip() or "\n" in v or "  " in v or re.search(r"[^\x20-\x7e]", v))]
    assert not bad, f"{len(bad)} field(s) with stray whitespace or non-ASCII: {bad[:6]}"


SEV_WORD = {"crit": "critical", "warn": "warning", "info": "info"}
EVERY = re.compile(r"Every (minute|(\d+) minutes?|(\d+) hours?)")


def test_probe_rule_text_states_what_the_effective_probe_config_says(an):
    """133 probe rules say 'every 2 minutes; 2 bad runs to go down; P1, warning; objective 99.5 percent ...' in prose. The numbers are the probe's
    EFFECTIVE values (group default overridden by the member, parsed with probes.parse, not the member's own keys), so an edit of a group or a
    member that leaves its sentence behind fails here. Only facts the template states are compared; a rule may say more."""
    _d, plist, errs = PR.parse(an.comp.docs["probes.toml"])
    assert errs == [], errs
    by = {p.name: p for p in plist}
    rules = [r for r in all_rules() if r.get("file") == "probes.toml" and r.get("kind") == "probe" and (r.get("params") or {}).get("name") in by]
    assert len(rules) == len(by) >= 120, (len(rules), len(by))                      # one rule per probe, none missing
    bad = []
    for r in rules:
        p, d = by[r["params"]["name"]], r["does"]
        m = EVERY.search(d)
        got = None if not m else 60 if m[1] == "minute" else int(m[2]) * 60 if m[2] else int(m[3]) * 3600
        facts = {"interval": got == p.interval_s,
                 "confirm/recover": f"{p.confirm} bad run" in d and f"{p.recover} good run" in d,
                 "class/severity": f"{p.cls}, {SEV_WORD[p.severity]}" in d,
                 "slo": (slo := re.search(r"Availability objective ([\d.]+) percent", d)) is not None and p.slo is not None and float(slo[1]) == p.slo or (slo is None and p.slo is None),
                 "optional": ("optional: never seen up" in d) == p.optional,
                 "kuma push key": (f"Kuma push monitor '{p.kuma_push_key}'" in d) == bool(p.kuma_push_key),
                 "kuma monitor": (f"Uptime Kuma monitor '{p.kuma}'" in d) == bool(p.kuma),
                 "external": ("external: allowed to leave the host" in d) == p.external,
                 "when_running": ("Skipped while" in d) == bool(p.when_running)}
        bad += [f"{r['id']}.{k}" for k, ok in facts.items() if not ok]
    assert not bad, f"{len(bad)} probe fact(s) the text gets wrong: {bad[:6]}"


def test_job_rule_text_states_what_the_job_table_says(an):
    """Same idea for the scheduler jobs: class, who runs it, jitter, timeout, nice, heavy, monitor, the mounts it needs and whether it ships observe."""
    jobs = {j["name"]: j for j in an.comp.docs["jobs.toml"]["job"]}
    bad = []
    for r in all_rules():
        j = jobs.get((r.get("params") or {}).get("name")) if r["id"].startswith("job.") and r.get("file") == "jobs.toml" else None
        if j is None:
            continue
        d, live = r["does"], j.get("mode") != "retired"
        paths = [c for c in j["command"] if "/" in c or c.endswith((".py", ".sh"))]          # '{self}', 'run', '-B' say nothing
        ts, jit = j.get("timeout_s"), j.get("jitter_s")
        facts = {"class": f"class {j['class']}" in d,
                 "mode": ("Ships mode = observe" in d or "Ships observe" in d) == (j.get("mode") == "observe"),
                 "user": not live or f"as {j['user']}" in d,
                 "command": not live or not paths or any(os.path.basename(c) in d for c in paths),
                 "jitter": not jit or any(f"jitter {pre}{x}" in d for pre in ("", "up to ") for x in (f"{jit // 60} min", f"{jit} s")),
                 "timeout": not ts or any(f"timeout {x}" in d for x in (f"{ts} s", f"{ts // 60} min", f"{ts // 3600} h", f"{ts / 3600:g} h")),
                 "no timeout": ts != 0 or re.search(r"no[ -]timeout", d, re.I) is not None,
                 "nice": not j.get("nice") or f"nice {j['nice']}" in d,
                 "heavy": not j.get("heavy") or "heavy" in d,
                 "monitor": not j.get("monitor") or "monitor" in d,
                 "mounts": all(m in d for m in j.get("requires_mounts", []))}
        bad += [f"{r['id']}.{k}" for k, ok in facts.items() if not ok]
    assert not bad, f"{len(bad)} job fact(s) the text gets wrong: {bad[:6]}"


def test_a_live_tile_names_a_container_only_when_it_has_one(an):
    """Plex is a snap: its tile has a unit and no container, so the text may not promise a container check (it once did)."""
    tiles = [r for r in all_rules() if r["id"].startswith("live.service.")]
    assert len(tiles) >= 11
    bad = []
    for r in tiles:
        ct, d = (r["params"] or {}).get("container"), r["does"]
        if bool(ct) != ("container must exist" in d) or (ct and f"container {ct}" not in d):
            bad.append(r["id"])
    assert not bad, bad


def test_the_destructive_flag_follows_the_task_class():
    """A C1 task deletes, restarts, kills or purges; a C2 task is a plan that a person approves; a C0 check changes nothing. `rules check` only
    demands the flag where a rule switches apply on, so this pins the reading the website's filter shows. Every retention, cache-trim and
    candidate rule (they name paths to delete) and every rule carrying mode = apply is destructive too."""
    rules = {r["id"]: r for r in all_rules()}
    wrong = []
    for name, info in sorted(R.task_catalog().items()):
        r = rules.get(f"task.{name}")
        if r is None:
            wrong.append(f"{name}: no task.{name} rule")
        elif bool(r.get("destructive")) != (info.klass in ("C1", "C2")):
            wrong.append(f"{name} ({info.klass}): destructive = {r.get('destructive')}")
    wrong += [i for i, r in rules.items() if r.get("destructive") is not True and (r.get("mode") == "apply" or i.startswith(("retention.", "app_cache_trim.", "c2.")))]
    assert not wrong, wrong


def test_the_shipped_config_applies_nothing_but_the_reclaim_rung(an):
    """INTEGRATION decision 12: every cleaner ships report-only and the owner enables apply per task. The one shipped apply is the pressure
    ladder's reclaim rung (unloading idle models, no kill). Jobs: a job with a legacy driver is observe, only the umbrella's own engines are
    managed from the start, mem-guard is retired. Nothing here may enable docker-prune.timer either: its job stays observe."""
    docs = an.comp.docs
    tasks = docs["maint.toml"]["tasks"]
    assert sorted(t for t, tbl in tasks.items() if isinstance(tbl, dict) and tbl.get("mode") == "apply") == ["pressure_response"]
    pr = tasks["pressure_response"]
    assert (pr["reclaim"], pr["throttle"], pr["restart"], pr["emergency"]) == ("apply", "report", "report", "report")
    modes = {j["name"]: j.get("mode") for j in docs["jobs.toml"]["job"]}
    assert set(modes.values()) <= {"observe", "managed", "retired"}, modes
    # container-audit added 2026-10-04: a native job with no legacy driver, so it ships managed like the engines.
    assert {n for n, m in modes.items() if m == "managed"} == {"probes-run", "routine-run", "container-audit"}
    assert {n for n, m in modes.items() if m == "retired"} == {"mem-guard"}
    assert modes["docker-prune"] == "observe"


def related_names(rules: list[dict]) -> set[str]:
    """What the website links a rule to (registry._related): applies_to, the task a maint.toml rule configures, the name a job or probe rule defines."""
    out: set[str] = set()
    for r in rules:
        out |= set(r.get("applies_to") or [])
        m = re.match(r"tasks\.([A-Za-z0-9_-]+)", r.get("target") or "")
        if r.get("file") == "maint.toml" and m:
            out.add(m.group(1))
        nm = (r.get("params") or {}).get("name")
        if isinstance(nm, str) and r.get("file") in ("jobs.toml", "probes.toml"):
            out.add(nm)
    return out


def registered_tasks(tmp_path: Path) -> set[str]:
    """Every task the runner registers (the real cli.load_tasks in a fresh interpreter that sees only tmp dirs)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HOMELAB_MAINT_")}
    for k in ("state", "log", "run", "conf"):
        env[f"HOMELAB_MAINT_{k.upper()}"] = str(mkdir(tmp_path / k))
    env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1", HOMELAB_MAINT_LIB=str(ROOT), HOMELAB_MAINT_NO_SYSLOG="1")
    p = subprocess.run([sys.executable, "-B", "-c", "import json; from homelab_maint import cli, core; cli.load_tasks(); print(json.dumps(sorted(core.REGISTRY)))"],
                       capture_output=True, text=True, timeout=120, cwd=ROOT, env=env, stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p.stderr[-400:]
    return set(json.loads(p.stdout))


def test_every_registered_task_job_and_probe_is_named_by_at_least_one_rule(an, tmp_path):
    """The reverse of `rules check` (which proves every applies_to name exists): nothing the runner does lacks a rule that explains it.
    Config entries no rule owns cannot exist (test a), so this closes the loop for the code side: a new task, job or probe without a rule fails."""
    docs = an.comp.docs
    named = related_names(all_rules())
    tasks = registered_tasks(tmp_path)
    jobs = {e["name"] for k in ("job", "external") for e in docs["jobs.toml"].get(k, [])}
    probes = {p["name"] for g in docs["probes.toml"].get("group", []) for p in g.get("probes", []) if isinstance(p, dict)}
    probes |= {p["name"] for p in docs["probes.toml"].get("probe", [])}
    assert len(tasks) > 60 and len(jobs) >= 45 and len(probes) >= 120, (len(tasks), len(jobs), len(probes))     # the scan sees the real thing
    missing = sorted((tasks | jobs | probes) - named - set(ORPHAN_OK))
    assert not missing, f"{len(missing)} task/job/probe name(s) no rule mentions (applies_to or its own table): {missing[:8]}"
    stale = sorted(n for n in ORPHAN_OK if n in named or n not in tasks)
    assert not stale, f"ORPHAN_OK entries that are no longer orphans (or no longer registered): {stale}"


def test_the_public_export_shows_every_rule_text_whole_and_unscrubbed(tmp_path):
    """rules.json is what the website shows. The scrubber hides URL paths of foreign hosts, `-u NAME`, `--token X`, emails and phone numbers, and
    build_rules_json cuts prose when the file passes its cap: neither may touch a shipped text (home directories become ~ on purpose)."""
    conf = mkdir(tmp_path / "conf")
    shutil.copytree(RD, conf / "rules.d")
    os.chmod(conf / "rules.d", 0o755)
    doc = R.build_rules_json(conf, mkdir(tmp_path / "state"), now=1_800_000_000.0)
    raw = {r["id"]: r for r in all_rules()}
    assert {r["id"] for r in doc["rules"]} == set(raw)
    assert not {"truncated", "omitted_rules"} & set(doc["stats"]), doc["stats"]
    assert len(json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode()) <= R.RULES_JSON_MAX
    assert doc["stats"]["placeholder"] == 0
    bad = [f"{r['id']}.{f}" for r in doc["rules"] for f in ("title", "why", "does", "proof")
           if r[f] != R._pub_str(str(raw[r["id"]].get(f) or "")) or re.search(r"\[(?:redacted|email|phone)\]", r[f])]
    assert not bad, f"{len(bad)} public text(s) changed by the scrubber or cut: {bad[:6]}"


def test_the_todo_scan_itself_sees_a_marker_in_any_field_and_ignores_headers():
    mk = f"{R.TODO}: x"
    rules = [{"id": "a.b.c", "why": "fine", "does": mk}, {"id": "d.e.f", "why": "ok", "proof": [mk], "params": {"x": {"y": [mk]}}},
             {"id": "g.h.i", "why": "# header mentions TODO but not the marker", "params": {"n": 1}}]
    assert todo_left(rules) == {"a.b.c": ["does"], "d.e.f": ["proof", "params"]}


# =========================================================================== c. ids: unique, well formed, stable
# Every id the first shipped registry carried (grouped by the file that held it, for reading; only the union matters: a rule may move to
# another category file). Add NEW ids freely. To drop or rename one, list it in RETIRED with the reason: a removed id would orphan its
# change history, the website's links and the owner's notes.
PINNED = {
    "00-baseline-invariants.toml": """
        safety.protected-superset safety.hard-caps safety.delete-confinement safety.destructive-explicit
    """,
    "10-checks.toml": """
        task.disk_forecast task.failed_units task.backup_freshness task.docker_df task.memory_health task.smart_trend
        task.alert_path_health task.growth_watch growth.-volume1-docker-kavita-config-logs growth.-var-log
        growth.-var-lib-docker-containers growth.-var-log-journal
        growth.-home-ohmz-studioprojects-tunarr-.docker-data-tunarr-cac-54cece task.plex_media_mount_check task.config_drift
        task.os_jobs task.docker_prune_parity task.legacy_audit task.docker_prune_exposure task.surrealdb_health
        task.self_health
    """,
    "20-spike.toml": """
        task.spike_sampler task.stuck_detector task.orphan_report task.image_ledger task.pressure_state task.bulkhead_check
        task.pressure_response task.qos_classes classes.classes classes.units classes.policy classes.defaults.p1
        classes.defaults.p2 classes.defaults.p3 classes.floors classes.ladder classes.ladder.throttle
        classes.ladder.max_per_day classes.ladder.signals.mem_full60 classes.ladder.signals.mem_some60
        classes.ladder.signals.mem_avail_gib classes.ladder.signals.swap_in_pps classes.ladder.signals.io_full60
        classes.ladder.signals.io_some60 classes.ladder.signals.cpu_some60 classes.ladder.signals.gpu_vram_pct
        classes.bulkheads
    """,
    "30-cleanup.toml": """
        task.docker_cache task.docker_images task.apt_clean task.apt_cache task.stale_driver_packages
        task.apt_autoremove_unused task.snap_revisions task.retention retention.kavita-logs retention.kavita-backups
        task.trash task.gradle_reaper task.app_cache_trim app_cache_trim.tunarr-subtitles task.log_compress
        task.dangling_images task.crash_dumps task.tool_caches task.caps task.openwebui_media_prune
        task.comfyui_idle_reclaim task.immich_recycle task.docker_containers_prune task.c2_candidates c2.cursor-state-backup
        c2.cursor-worktrees c2.orphan-docker-plex-config c2.home-venv c2.android-fold-avd c2.kometa-backup-tarball
        c2.hermes-genesis-gguf task.stale_build_output task.unused_venvs task.large_cold_files task.flatpak_unused
    """,
    "40-protection.toml": """
        protect.databases-stateful-stores protect.ai-media-processing-that protect.build-tooling-while-a-build
        protect.infrastructure protect.data-paths-that-must-never protect.busy
    """,
    "50-alerts.toml": """
        notify.root notify.transport notify.site notify.routes notify.routes.alert notify.routes.incident_open
        notify.significant notify.task_routes notify.todo notify.escalation notify.dedupe notify.dedupe.window_s
        notify.dedupe.covered_by notify.budget notify.budget.per_day notify.quiet_hours notify.retry notify.ack notify.log
    """,
    "60-schedule.toml": """
        task.report_daily task.report_weekly task.routine_spike_review task.routine_verify_daily task.routine_capacity
        task.routine_smart_selftest task.routine_updates task.routine_image_updates task.routine_backup_verify
        task.routine_verify_weekly task.routine_restore_check task.routine_expiry task.routine_trends
        task.routine_verify_monthly task.routine_rotate routine.daily routine.daily.step.spike_review
        routine.daily.step.docker_cache routine.daily.step.dangling_images routine.daily.step.docker_images
        routine.daily.step.apt_clean routine.daily.step.apt_cache routine.daily.step.stale_driver_packages
        routine.daily.step.apt_autoremove_unused routine.daily.step.snap_revisions routine.daily.step.retention
        routine.daily.step.app_cache_trim routine.daily.step.log_compress routine.daily.step.crash_dumps
        routine.daily.step.trash routine.daily.step.gradle_reaper routine.daily.step.tool_caches
        routine.daily.step.openwebui_media_prune routine.daily.step.caps routine.daily.step.qos_classes
        routine.daily.step.verify routine.daily.step.report routine.weekly routine.weekly.step.capacity_review
        routine.weekly.step.smart_selftest routine.weekly.step.c2_candidates routine.weekly.step.stale_build_output
        routine.weekly.step.unused_venvs routine.weekly.step.large_cold_files routine.weekly.step.flatpak_unused
        routine.weekly.step.docker_containers_prune routine.weekly.step.docker_prune_parity
        routine.weekly.step.bulkhead_check routine.weekly.step.legacy_audit routine.weekly.step.config_drift
        routine.weekly.step.image_updates routine.weekly.step.updates_review routine.weekly.step.backup_verify
        routine.weekly.step.verify routine.weekly.step.report routine.monthly routine.monthly.step.restore_check
        routine.monthly.step.expiry_check routine.monthly.step.trend_review routine.monthly.step.config_drift
        routine.monthly.step.rotate_logs routine.monthly.step.verify routine.system.backup-system
        routine.system.backup-immich routine.system.stack-backup routine.system.plex-butler routine.system.docker-prune
        routine.system.prune-openwebui-media routine.system.fstrim routine.system.logrotate routine.system.apt-daily-upgrade
        routine.system.e2scrub routine.system.diun routine.settings routine.canary routine.continuous routine.windows
        routine.freeze routine.stepopts.spike_review routine.stepopts.smart_selftest routine.stepopts.image_updates
        routine.stepopts.backup_verify routine.stepopts.restore_check routine.stepopts.expiry_check
    """,
    "70-monitoring.toml": """
        task.probes live.settings live.service.plex live.service.immich live.service.kavita live.service.seerr
        live.service.open-webui live.service.homarr live.service.uptime-kuma live.service.nextcloud live.service.tunarr
        live.service.radarr live.service.sonarr probe-group.umbrella probe.umbrella-status probe.umbrella-probe-plane
        probe.umbrella-tick probe.umbrella-public-export probe.umbrella-live probe.umbrella-metrics-ring probe.umbrella-www
        probe.maintenance-site probe.ct-maintenance-web probe.svc-hm-www probe.svc-hm-live probe.svc-hm-check
        probe.svc-hm-daily probe.svc-hm-weekly probe.svc-hm-metrics probe.svc-hm-tick probe.svc-hm-selfhealth
        probe-group.platform probe.docker-daemon probe.svc-docker probe.svc-cloudflared probe.cloudflared-tunnel
        probe.svc-ssh probe-group.platform-2 probe.svc-containerd probe.svc-tailscaled probe.tailscale probe.svc-fail2ban
        probe.fail2ban probe.svc-networkmanager probe.svc-resolved probe.svc-timesyncd probe.svc-cron probe.svc-rsyslog
        probe.svc-snapd probe.svc-smartd probe.svc-smart-bridge probe.svc-nvidia-persistenced probe.gpu-driver
        probe.internet probe-group.media-stack probe.plex probe.radarr probe.sonarr probe.prowlarr probe.sabnzbd
        probe.deluge probe.transmission probe.bazarr probe.tautulli probe.seerr probe.immich probe.ct-kometa
        probe-group.apps probe.homarr probe.uptime-kuma probe.immich-public-proxy probe.nextcloud probe.openwebui
        probe.owui-public-gate probe.audiobookshelf probe.searxng probe.tday probe.ohmz-cloud probe.omar-iqbal
        probe.afsaane-prod probe.pa-driving-flashcards probe-group.apps-2 probe.kavita probe.tunarr probe.booklore
        probe.grimmory probe.shelfmark probe.hometube probe.trek probe.anchor probe.saved-vault probe.friendarr
        probe.qbittorrent probe.jackett probe.portainer probe.searxng-hermes probe.cancel-service probe-group.host-services
        probe.svc-plex probe.svc-ollama probe.ollama probe.svc-glances probe.glances probe.svc-sensor-exporter
        probe.sensor-exporter probe.svc-teamviewerd probe.svc-thermal-log probe-group.ai probe.comfyui probe.open-notebook
        probe.surrealdb probe.kokoro probe.infinity-rerank probe.omnivoice probe.speaches probe.qdrant probe.tika
        probe-group.containers probe.ct-immich-postgres probe.ct-immich-redis probe.ct-nextcloud-postgres
        probe.ct-nextcloud-redis probe.ct-tday-db probe.ct-afsaane-prod-db probe.ct-open-webui-public
        probe.ct-owui-public-quota probe-group.containers-2 probe.ct-grimmory-db probe.ct-mariadb probe.ct-surrealdb
        probe.ct-qdrant probe.ct-immich-ml probe.ct-docker-socket-proxy probe-group.hermes probe.hermes-api
        probe.hermes-gateway probe.hermes-ticker probe.hermes-ticker-ok probe.hermes-flightclaw probe.hermes-watchdog-alive
        probe.hermes-canary-alive probe-group.hermes-watchdog probe.hermes-wd-gateway probe.hermes-wd-api
        probe.hermes-wd-delivery probe.hermes-wd-backup probe.hermes-wd-flightclaw probe.hermes-wd-pubgate
        probe.hermes-wd-pubquota probe.hermes-wd-ticker probe.hermes-wd-gwrestarts probe.hermes-wd-backlog
        probe.hermes-wd-version probe-group.hermes-watchdog-2 probe.hermes-sc-chat probe.hermes-sc-agent probe-group.public
        probe.public-seerr probe.public-maintenance probe.ct-fleet probes.defaults probes.kuma
    """,
    "80-jobs.toml": """
        job.backup-system job.backup-immich job.stack-backup job.stack-watchdog job.search-canary job.bazarr-rules
        job.tunarr-sync job.docker-prune job.prune-openwebui-media job.comfyui-idle-vram job.notebook-db-alert
        job.immich-server-recycle job.mem-guard job.tier-check job.tier-daily job.tier-weekly job.metrics-sample
        job.probes-run job.routine-run job.purge-public-guests external.apt-daily external.apt-daily-upgrade external.fstrim
        external.logrotate external.man-db external.e2scrub-all external.sysstat-collect external.sysstat-summary
        external.systemd-tmpfiles-clean external.fwupd-refresh external.dpkg-db-backup external.certbot
        external.snapd-refresh external.anacron external.motd-news external.nvidia-tdp external.hermes-delivery
        external.launchpadlib-cache-clean external.firmware-notifier external.smart-alert external.backup-failure
        external.stack-alert external.sensor-exporter external.smartd external.smart-bridge external.glances
        external.thermal-log external.tunarr-autostart external.nvidia-cdi-refresh external.homelab-maint-www
        external.homelab-maint-live sched.settings sched.pressure_max
    """,
    "90-safety.toml": """
        config.global config.caps
    """,
    "95-ack.toml": """
        ack.ack ack.inbox ack.key.disk_forecast ack.key.failed_units ack.key.backup_freshness ack.key.docker_df
        ack.key.memory_health ack.key.plex_media_mount_check ack.key.smart_trend ack.key.alert_path_health
        ack.key.growth_watch ack.key.stuck_detector ack.key.orphan_report ack.key.pressure_state ack.key.pressure_response
        ack.key.probes ack.key.os_jobs ack.key.docker_prune_exposure ack.key.immich_recycle ack.key.comfyui_idle_reclaim
    """,
}
PINNED_IDS = {i for s in PINNED.values() for i in s.split()}
RETIRED: dict[str, str] = {}                                                    # id -> why it is gone (and what replaced it)


def test_rule_ids_are_unique_across_all_registry_files_and_well_formed():
    ids = [r["id"] for d in raw_rules().values() for r in d["rule"]]
    dup = [i for i, n in Counter(ids).items() if n > 1]
    assert not dup, f"duplicate rule ids: {dup[:5]}"
    bad = [i for i in ids if not R.ID_RX.fullmatch(i)]
    assert not bad, f"ids that the registry would refuse: {bad[:5]}"


def test_pinned_ids_still_exist_so_ids_are_stable():
    have = {r["id"] for d in raw_rules().values() for r in d["rule"]}
    assert len(PINNED_IDS) == sum(len(s.split()) for s in PINNED.values()), "an id is pinned twice"
    gone = sorted(PINNED_IDS - have - set(RETIRED))
    assert not gone, f"{len(gone)} rule id(s) vanished or were renamed: {gone[:5]} (new text belongs in the rule, not in a new id; retire on purpose via RETIRED)"
    assert not (set(RETIRED) & have), "an id listed as RETIRED came back: ids are never reused"
    assert all(RETIRED.values()), "every retired id needs its reason"


def test_ids_are_what_migrate_derives_from_the_shipped_config_so_a_regeneration_keeps_the_text():
    """`rules migrate --force` carries human text over BY ID: every id it would mint from today's etc/*.toml must already be in rules.d."""
    docs, raws = R.load_legacy(ETC)
    minted = {m.rule["id"] for m in R.migrate_build(docs, raws, catalog=R.task_catalog(), today="2026-10-03")}
    have = {r["id"] for d in raw_rules().values() for r in d["rule"]}
    assert minted <= have, f"etc/*.toml has parts rules.d does not name: {sorted(minted - have)[:5]}"
