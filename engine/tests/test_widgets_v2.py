"""Tests for the seven Homarr v2 custom-widget definitions (widgets/ops-*.json, built by widgets/build_v2.py from widgets/ops-*.jsx).

Two layers:
  * pure Python (always runs): the committed import files equal the build output, strict-shape / limits / id / enum / loopback checks,
    the credential-literal trap, the null-safety and nested-key heuristics, the payload fixtures, the docs, and negative tests that
    prove the lint really catches each mistake;
  * harness-backed (skipped cleanly when node >= 22 or the Homarr checkout with its node_modules is missing, see widgets/tools/README.md):
    the REAL Homarr schema, Import parser, analyzer, interpreter, React renderer (jsdom) and a real headless Chromium render every
    definition against every fixture, failure scenario, tile size and colour scheme.
"""
import conftest  # noqa: F401  (points homelab_maint at throw-away dirs before it is imported)

import copy
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WIDGETS = ROOT / "widgets"
sys.path.insert(0, str(WIDGETS))

import build_v2 as bv  # noqa: E402
from homelab_maint import payloads, payloads_metrics as pm  # noqa: E402

STEMS = list(bv.WIDGETS)
def LD(name: str) -> str:
    """The scheme-aware TEXT colour of the templates: the .5 shade on dark boards, shade 9 darkened 30 % on light ones (>= 4.5:1 on every tile)."""
    return f"light-dark(color-mix(in srgb,var(--mantine-color-{name}-9),#000 30%),var(--mantine-color-{name}-5))"


BANNER = '{status.state?.ok===false&&<Text fz={11} c="' + LD("red") + '" lineClamp={2}>{"ops service: "+(status.state.error||"no response")}</Text>}'
ROUTES = set(payloads.ROUTES) | set(pm.ROUTES)


def load(stem: str) -> dict:
    return json.loads((WIDGETS / f"{stem}.json").read_text())


def tpl(stem: str) -> str:
    return load(stem)["template"]


# =========================================================================== build output and import format
@pytest.mark.parametrize("stem", STEMS)
def test_committed_import_file_equals_the_build_output(stem):
    assert (WIDGETS / f"{stem}.json").read_text() == bv.render(stem), "run: python3 widgets/build_v2.py"


def test_build_check_cli_is_green():
    r = subprocess.run([sys.executable, str(WIDGETS / "build_v2.py"), "--check"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.startswith("ok:"), r.stdout + r.stderr


@pytest.mark.parametrize("stem", STEMS)
def test_import_file_has_exactly_the_v2_shape(stem):
    d = load(stem)
    name, route, desc, tracks, refresh = bv.WIDGETS[stem]
    assert list(d) == ["$schema", "name", "description", "sources", "requests", "options", "template"]      # strict objects: nothing else is allowed
    assert d["$schema"] == "homarr-custom-widget-v2" and d["name"] == name and d["description"] == desc and d["options"] == {}
    assert d["sources"] == {"default": {"baseUrl": "http://127.0.0.1:9111", "networkScope": "loopback", "auth": "none"}}
    assert d["requests"] == {"state": {"kind": "query", "method": "GET", "path": "/" + route, "source": "default", "trigger": "load", "cacheSeconds": 5}}
    assert route in ROUTES and len(d["requests"]) <= 4                     # one load request, served by the ops service
    assert 0 < len(name) <= 128 and 0 < len(desc) <= 512 and 0 < len(d["template"]) <= 50_000
    assert not {"url", "authType", "method", "displayType", "displayConfig", "stateSchema", "defaultState", "headerName", "requestBody"} & set(d)


def test_files_are_utf8_json_with_one_trailing_newline_and_a_literal_middle_dot():
    for stem in STEMS:
        raw = (WIDGETS / f"{stem}.json").read_text(encoding="utf-8")
        assert raw.endswith("}\n") and not raw.endswith("\n\n") and "\\u" not in raw, stem        # ensure_ascii=False: real characters, not escapes
    assert "·" in (WIDGETS / "ops-disk.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("stem", STEMS)
def test_lint_is_clean(stem):
    assert bv.lint(load(stem)) == []


def test_squash_strips_indentation_blank_lines_and_comment_lines():
    assert bv.squash("// head\n\n  <Stack>\n    <Text>x</Text>\n  </Stack>\n// tail\n") == "<Stack>\n<Text>x</Text>\n</Stack>"
    for stem in STEMS:
        src = (WIDGETS / f"{stem}.jsx").read_text()
        assert src.startswith(f"// {stem}:") and bv.squash(src) == tpl(stem)


def test_metadata_matches_the_spec_geometry_and_refresh():
    """docs/HOMARR_V2_WIDGETS.md 6.3 / 6.5: 3x2 tracks for the overview, 3x3 for the rest; 30 s status, 60 s metrics."""
    assert {s: m[3] for s, m in bv.WIDGETS.items()} == {"ops-overview": (3, 2), "ops-disk": (3, 3), "ops-jobs": (3, 3), "ops-guard": (3, 3),
                                                       "ops-reclaim": (3, 3), "ops-thermals": (3, 3), "ops-load": (3, 3)}
    assert {s: m[4] for s, m in bv.WIDGETS.items()} == {**{s: 30 for s in bv.STATUS_STEMS}, "ops-thermals": 60, "ops-load": 60}


def test_footprints_agree_with_the_installer_when_it_publishes_them():
    import importlib.util
    f = WIDGETS / "install_homarr_widgets.py"
    if not f.exists():
        pytest.skip("no installer")
    try:
        spec = importlib.util.spec_from_file_location("install_homarr_widgets_probe", f)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001 - another stream's file: never fail this suite because it is mid-edit
        pytest.skip(f"installer not importable: {exc}")
    if not hasattr(mod, "SIZES") or not hasattr(mod, "REFRESH"):
        pytest.skip("installer publishes no SIZES/REFRESH")
    assert dict(mod.SIZES) == {s: m[3] for s, m in bv.WIDGETS.items()} and dict(mod.REFRESH) == {s: m[4] for s, m in bv.WIDGETS.items()}


# =========================================================================== template rules (pure-Python port of docs 3.x)
@pytest.mark.parametrize("stem", STEMS)
def test_template_reads_only_data_state_and_status_state(stem):
    t = tpl(stem)
    assert "data.state." in t
    assert not re.search(r"\bdata\.(?!state\b)", t) and not re.search(r"\bstatus\.(?!state\b)", t)
    assert not re.search(r"\b(data|status)\b\s*[\[]", t)


@pytest.mark.parametrize("stem", STEMS)
def test_every_method_call_has_a_guarded_receiver(stem):
    """Property reads on a missing body are null-safe in v2, method calls are not: `data.state.rows.map(` kills the tile when data.state is null."""
    t = tpl(stem)
    assert not [m.group(0) for m in bv.METHOD_CALL.finditer(t) if m.group(1).split(".")[0] not in bv.STATIC_ROOTS]
    for m in re.finditer(r"\.(map|toFixed|join|filter|slice|find)\(", t):
        before = t[max(0, m.start() - 12):m.start()]
        assert before.endswith(("||[])", "]")), (stem, before)               # `(x||[]).map(` or an array literal `[...].map(`


@pytest.mark.parametrize("stem", STEMS)
def test_mapped_elements_use_index_keys_never_server_truncated_names(stem):
    t = tpl(stem)
    assert not re.findall(r"key=\{(?![ij]\})", t) and "key={" in t
    assert not re.search(r"\.map\((?:\w+)=>", t), "a .map callback without the index parameter"
    assert len(re.findall(r"\.map\(\(\w+,\w+\)=>", t)) >= 1


@pytest.mark.parametrize("stem", STEMS)
def test_status_banner_explains_a_dead_ops_service(stem):
    t = tpl(stem)
    assert t.count(BANNER) == 1
    lines = t.split("\n")
    assert lines.index(BANNER) < next(i for i, ln in enumerate(lines) if "data.state.error&&" in ln)          # above the orange payload error line
    assert not [ln for ln in lines[:lines.index(BANNER)] if ln.startswith("<SimpleGrid") or ln.startswith("{(data.state")]   # right after the header


@pytest.mark.parametrize("stem", STEMS)
def test_no_nested_left_right_keys_no_blocked_props_no_raw_html(stem):
    t = tpl(stem)
    for name, a, b in bv.prop_spans(t):
        if name not in ("data", "series"):
            assert not bv.BANNED_NESTED.search(t[a:b]), (stem, name)
    assert "padding:{" not in t and not re.search(r"<[a-z]", t) and not re.search(r"\bon[A-Z]\w*=|className=|ref=", t)


@pytest.mark.parametrize("stem", ["ops-thermals", "ops-load"])
def test_charts_take_pixel_height_and_axis_width_from_the_payload(stem):
    t = tpl(stem)
    assert "h={g.h}" in t and "width:g.yw||44" in t
    assert "clamp(" not in t and not re.search(r"\d\s*vw\b", t)                  # a vw length follows the browser window and is zoomed with the canvas
    assert "lineProps={{isAnimationActive:false}}" in t                          # the one benign analyzer warning (Mantine supports it)


@pytest.mark.parametrize("stem", STEMS)
def test_template_has_no_credential_literal_and_is_far_below_the_size_cap(stem):
    assert not bv.has_credential_literal(tpl(stem)) and not bv.has_credential_literal(load(stem)["description"])
    assert len(tpl(stem)) < 5_000                                                # the v1 cap was 10,000; v2 allows 50,000


@pytest.fixture(scope="module")
def cases():
    return bv.payload_cases()


@pytest.mark.parametrize("stem", STEMS)
def test_template_reads_only_payload_keys_that_exist(stem, cases):
    """Every data.state.<key> (and group / tile key for the metrics widgets) is a key some payload really carries: a typo would render blank."""
    t = tpl(stem)
    payload_keys = set().union(*(set(p) for p in cases[stem].values())) | {"error"}
    assert set(re.findall(r"\bdata\.state\.([A-Za-z_]\w*)", t)) <= payload_keys
    if stem in bv.METRIC_STEMS:
        groups = [g for p in cases[stem].values() for g in p["groups"]]
        assert set(re.findall(r"\bg\.([a-z_]+)", t)) <= set().union(*(set(g) for g in groups))
        assert set(re.findall(r"\bx\.([a-z_]+)", t)) <= set().union(*(set(x) for g in groups for x in g["tiles"]))


# =========================================================================== fixtures
def test_fixtures_are_what_the_payload_builders_produce(cases):
    for stem, per_case in cases.items():
        assert set(per_case) == set(bv.STATUS_CASES if stem in bv.STATUS_STEMS else bv.METRIC_CASES)
        for case, payload in per_case.items():
            assert bv.fixture_path(stem, case).read_text() == bv.fixture_text(payload), f"fixtures/{stem}/{case}.json is stale: run widgets/build_v2.py"
    assert len(list(bv.FIXTURES.glob("*/*.json"))) == 5 * 9 + 2 * 7                  # no orphan fixture files


def test_fixtures_cover_ok_warn_crit_stale_all_none_and_a_partial_ring(cases):
    assert {"ok", "warn", "crit", "stale"} <= set(cases["ops-overview"])
    assert {"all_none", "partial_ring", "stale", "hot", "no_gpu", "no_ring_file"} <= set(cases["ops-load"])
    assert cases["ops-overview"]["stale"]["stale"] is True and cases["ops-overview"]["ok"]["stale"] is False
    assert cases["ops-overview"]["no_status_file"]["error"] == "no status yet"
    assert cases["ops-thermals"]["partial_ring"]["full"] is False and cases["ops-thermals"]["full_ring"]["full"] is True
    assert cases["ops-thermals"]["all_none"]["head"] == "no data" and cases["ops-thermals"]["no_ring_file"]["error"] == "no metrics yet"


def test_fixture_sizes_respect_the_service_budgets(cases):
    for stem, per_case in cases.items():
        limit = 4096 if stem in bv.STATUS_STEMS else 14_000
        for case in per_case:
            assert 0 < bv.fixture_path(stem, case).stat().st_size < limit, (stem, case)


LIST_FIELD = re.compile(r"\((data\.state|g|x)\.(\w+)\|\|\[\]\)")


@pytest.mark.parametrize("stem", STEMS)
def test_every_list_a_template_maps_over_is_a_list_in_every_payload_case(stem, cases):
    """The templates guard against a MISSING list with `(x||[]).map(` but trust its type (a truthy non-array would throw 'Calling method map' and turn
    the tile red). The contract therefore lives at the producer: every payload case carries each mapped field as a list, or leaves it out / null."""
    fields = {(root, key) for root, key in LIST_FIELD.findall(tpl(stem))}
    assert fields and {k for r, k in fields if r == "data.state"}, stem
    for case, p in cases[stem].items():
        for key in {k for r, k in fields if r == "data.state"}:
            assert p.get(key) is None or isinstance(p[key], list), (stem, case, key)
        for g in p.get("groups") or []:
            for key in {k for r, k in fields if r == "g"}:
                assert g.get(key) is None or isinstance(g[key], list), (stem, case, "group", key)
            for tile in g.get("tiles") or []:
                for key in {k for r, k in fields if r == "x"}:
                    assert tile.get(key) is None or isinstance(tile[key], list), (stem, case, "tile", key)


def test_metrics_fixtures_carry_pixel_geometry(cases):
    for stem in bv.METRIC_STEMS:
        for case, p in cases[stem].items():
            for g in p["groups"][:1]:
                assert type(g["h"]) is int and type(g["yw"]) is int, (stem, case)


def test_harness_job_matrix_shape():
    plain = bv.harness_jobs()
    assert len(plain) == 5 * (9 + 4) + 2 * (7 + 4) and len({j["id"] for j in plain}) == len(plain)
    shots = [j for j in bv.harness_jobs(Path("/x")) if "shot" in j]
    assert len(shots) == 7 * (3 * 2 + 2) + 2 * 2 and {j["scheme"] for j in shots} == {"dark", "light"} and all(j["noDom"] for j in shots)    # + the paused badge, 2 widgets x 2 schemes
    assert {j["id"] for j in shots if "/paused/" in j["id"]} == {f"{s}/paused/{w}x{h}/{sc}" for s in bv.PAUSED_STEMS for (w, h) in [bv.WIDGETS[s][3]] for sc in ("dark", "light")}


# =========================================================================== lint: every rule must catch what it claims to (negative tests)
def mutated(fn):
    d = copy.deepcopy(load("ops-overview"))
    fn(d)
    return d


def set_tpl(old, new):
    return lambda d: d.__setitem__("template", d["template"].replace(old, new, 1))


BAD = {
    "v1 keys": (lambda d: d.update(url="http://x/", authType="none", displayType="customJsx"), "unknown top-level keys"),
    "missing sources": (lambda d: d.pop("sources"), "missing sources"),
    "wrong schema tag": (lambda d: d.update({"$schema": "homarr-custom-widget-v1"}), "$schema"),
    "null description": (lambda d: d.update(description=None), "description"),
    "blank name": (lambda d: d.update(name="   "), "name must be"),
    "name over 128": (lambda d: d.update(name="n" * 129), "name must be"),
    "description over 512": (lambda d: d.update(description="d" * 513), "description must be"),
    "nine sources": (lambda d: d["sources"].update({f"s{i}": dict(d["sources"]["default"]) for i in range(8)}), "sources: 1..8"),
    "dotted source id": (lambda d: d["sources"].update({"ops.api": dict(d["sources"]["default"])}), "violates"),
    "unknown source key": (lambda d: d["sources"]["default"].update(timeout=5), "unknown keys"),
    "options not an object": (lambda d: d.update(options=[]), "options must be"),
    "no default source": (lambda d: d["sources"].update({"other": d["sources"].pop("default")}), "default"),
    "public scope": (lambda d: d["sources"]["default"].update(networkScope="public"), "loopback"),
    "missing scope": (lambda d: d["sources"]["default"].pop("networkScope"), "loopback"),
    "base url with query": (lambda d: d["sources"]["default"].update(baseUrl="http://127.0.0.1:9111?x=1"), "baseUrl"),
    "base url with userinfo": (lambda d: d["sources"]["default"].update(baseUrl="http://u:p@127.0.0.1:9111"), "baseUrl"),
    "ftp base url": (lambda d: d["sources"]["default"].update(baseUrl="ftp://127.0.0.1"), "baseUrl"),
    "bearer auth": (lambda d: d["sources"]["default"].update(auth="bearer"), "authentication"),
    "dotted request id": (lambda d: d["requests"].update({"ops.state": d["requests"].pop("state")}), "violates"),
    "digit request id": (lambda d: d["requests"].update({"1state": d["requests"].pop("state")}), "violates"),
    "mutation kind": (lambda d: d["requests"]["state"].update(kind="mutation"), "kind"),
    "interval trigger": (lambda d: d["requests"]["state"].update(trigger="interval"), "trigger"),
    "lowercase method": (lambda d: d["requests"]["state"].update(method="get"), "method"),
    "query in path": (lambda d: d["requests"]["state"].update(path="/overview?x=1"), "path"),
    "fragment in path": (lambda d: d["requests"]["state"].update(path="/overview#x"), "path"),
    "relative path": (lambda d: d["requests"]["state"].update(path="overview"), "path"),
    "protocol relative path": (lambda d: d["requests"]["state"].update(path="//evil/x"), "path"),
    "cache too long": (lambda d: d["requests"]["state"].update(cacheSeconds=3601), "cacheSeconds"),
    "cache fractional": (lambda d: d["requests"]["state"].update(cacheSeconds=1.5), "cacheSeconds"),
    "cache boolean": (lambda d: d["requests"]["state"].update(cacheSeconds=True), "cacheSeconds"),
    "unknown request key": (lambda d: d["requests"]["state"].update(interval=30), "unknown keys"),
    "unknown source": (lambda d: d["requests"]["state"].update(source="nope"), "unknown source"),
    "five load requests": (lambda d: d["requests"].update({f"r{i}": dict(d["requests"]["state"]) for i in range(4)}), "load requests"),
    "oversize template": (lambda d: d.update(template="<Text>" + "x" * 50_000 + "</Text>"), "outside 1..50000"),
    "nfd template": (lambda d: d.update(template=d["template"] + "<Text>é</Text>"), "NFC"),
    "unguarded map": (set_tpl("(data.state.tiles||[]).map(", "data.state.tiles.map("), "unguarded method call"),
    "unguarded toFixed": (set_tpl("{data.state.sub}", "{data.state.n.toFixed(1)}"), "unguarded method call"),
    "v1 data read": (set_tpl("data.state.stale?0.55", "data.stale?0.55"), "outside data.state"),
    "bare status read": (set_tpl("status.state?.ok", "status.ok"), "outside data.state / status.state"),
    "name key": (set_tpl("key={i}", "key={x.l}"), "map index"),
    "no index param": (set_tpl(".map((x,i)=>", ".map(x=>"), "index parameter"),
    "destructured param": (set_tpl(".map((x,i)=>", ".map(([x],i)=>"), "destructured"),
    "block arrow": (set_tpl("(x,i)=>", "(x,i)=>{return "), "forbidden syntax"),
    "raw html": (set_tpl("<Divider />", "<div>x</div>"), "raw HTML"),
    "unknown component": (set_tpl("<Divider />", "<Banana />"), "components outside"),
    "event handler": (set_tpl("<Divider />", "<Divider onClick={1} />"), "blocked prop"),
    "nested padding": (set_tpl("<Divider />", '<Divider xAxisProps={{padding:{left:6,right:6}}} />'), "stripped or blocked"),
    "credential label": (set_tpl(">Ops<", ">Token: {data.state.head}<"), "credential-literal"),
    "credential comparison": (set_tpl("data.state.stale?0.55", 'data.state.token==="x"?0.55'), "credential-literal"),
    "credential in description": (lambda d: d.update(description="Failed auth: 3"), "credential-literal"),
    "credential key": (lambda d: d["sources"]["default"].update(token="abc123456"), "credential"),
    "comment line left in": (lambda d: d.update(template="// x\n" + d["template"]), "not squashed"),
    "indented line": (lambda d: d.update(template=d["template"].replace("\n<Group", "\n  <Group", 1)), "not squashed"),
    "shadowed binding": (set_tpl(".map((x,i)=>", ".map((data,i)=>"), "shadows"),
    "bare shade text colour": (set_tpl('<Text fz={10} c="dimmed" truncate="end">{x.s}', '<Text fz={10} c="teal.5" truncate="end">{x.s}'), "bare Mantine shade"),
    "dynamic .5 text colour": (set_tpl('<Text fz={10} c="dimmed" truncate="end">{x.s}', '<Text fz={10} c={x.c+".5"} truncate="end">{x.s}'), "bare Mantine shade"),
}


@pytest.mark.parametrize("what", list(BAD))
def test_lint_catches(what):
    mutate, expect = BAD[what]
    problems = bv.lint(mutated(mutate))
    assert any(expect.lower() in p.lower() for p in problems), (what, problems)


@pytest.mark.parametrize("garbage", [None, [], "x", 5, {}, {"sources": {"default": "s"}, "requests": {"state": 5}, "options": 3, "template": 4, "name": 5},
                                     {"sources": [], "requests": [], "options": None, "template": None}])
def test_lint_reports_garbage_instead_of_raising(garbage):
    assert bv.lint(garbage)


def test_lint_on_the_untouched_control_is_clean():
    assert bv.lint(mutated(lambda d: None)) == []


def test_lint_allows_dark_text_on_a_bright_fill_and_the_scheme_aware_colour():
    """c="dark.9" is the text ON an orange.5 badge (same in both schemes); the light-dark() colours and "dimmed" are the sanctioned text colours."""
    assert bv.lint(mutated(set_tpl('<Text fz={10} c="dimmed" truncate="end">{x.s}', '<Text fz={10} c="dark.9" truncate="end">{x.s}'))) == []
    assert bv.lint(mutated(set_tpl('<Text fz={10} c="dimmed" truncate="end">{x.s}', f'<Text fz={{10}} c="{LD("teal")}" truncate="end">{{x.s}}'))) == []


@pytest.mark.parametrize("stem", STEMS)
def test_state_text_uses_scheme_aware_colours_but_fills_keep_the_dot_five_shade(stem):
    """The light-board contrast finding: coloured TEXT is light-dark(); swatches, progress bars and badge fills (not text) still use the .5 shade."""
    t = tpl(stem)
    assert not re.search(r'(?<![\w-])c=(?:"(?!dark\.)[a-z]+\.\d"|\{[^{}]*\+"\.\d"\})', t)
    assert t.count(LD("red")) == 1 and LD("orange") in t                               # the service banner and the payload error line
    assert "var(--mantine-color-" in t and 'size={8} withShadow={false}' in t          # the status dot is a fill: .5 (6 for red) as before
    if stem in ("ops-overview", "ops-disk", "ops-guard", "ops-thermals", "ops-load"):
        assert '"-9),#000 30%),var(--mantine-color-"+' in t                            # the dynamic (payload-driven) state text


# the credential trap: docs/HOMARR_V2_WIDGETS.md 3.8 table, checked against the pure-Python port here and the real schema in the harness tests below
CRED_FAIL = ["<Text>Auth: {data.state.x}</Text>", "<Text>Token: {data.state.x}</Text>", "<Text>Secrets: 3</Text>", "<Text>API key: abc</Text>",
             "<Text>passwords = 0</Text>", "<Text>private key: 1</Text>", "<Text>sshd auth: {data.state.n}</Text>",
             '<Text>{data.state.token==="x"?1:0}</Text>', "<Text>{JSON.stringify({token:1})}</Text>", "<Text>{JSON.stringify({auth:1})}</Text>",
             "<Text>{data.state.a?token:1}</Text>"]
CRED_OK = ["<Text>Secret: none</Text>", "<Text>Password: required</Text>", "<Text>authentication: enabled</Text>", "<Text>Bearer token</Text>",
           "<Text>keys: 3</Text>", "<Text>ssh key: ok</Text>", "<Text>fail2ban: 3</Text>", "<Text>{data.state.auth_failures}</Text>",
           "<Text>{data.state.token}</Text>", "<Stack>{(data.state.r||[]).map((x,i)=><Text key={i}>{x}</Text>)}</Stack>"]


@pytest.mark.parametrize("snippet", CRED_FAIL)
def test_credential_port_flags_the_documented_failures(snippet):
    assert bv.has_credential_literal(snippet)


@pytest.mark.parametrize("snippet", CRED_OK)
def test_credential_port_accepts_the_documented_safe_forms(snippet):
    assert not bv.has_credential_literal(snippet)


def test_old_v1_build_tooling_is_gone_on_purpose():
    for gone in ("build_widgets.py", "build_metrics_widgets.py", "check_templates.mjs"):
        assert not (WIDGETS / gone).exists(), f"{gone} pinned the v1 format (react-jsx-parser, url/authType): replaced by build_v2.py + tools/render-check.mjs"
    assert not (WIDGETS / "fixtures-metrics" / "preview_entry.jsx").exists() and not (WIDGETS / "fixtures-metrics" / "samples").exists()
    for stem in STEMS:
        assert not re.search(r"displayConfig|authType", (WIDGETS / f"{stem}.json").read_text())


def test_conventions_describe_v2_not_v1():
    text = (WIDGETS / "CONVENTIONS.md").read_text()
    for must in ("212", "data.state", "networkScope", "loopback", "render-check.mjs", "build_v2.py", "HOMARR_REPO", "Import", "cacheSeconds", "index key",
                 "light-dark", "lowContrastText", "--accept-existing", "text nodes"):
        assert must in text, must
    for stale in ("cells are square", "scale with the screen", "react-jsx-parser with whitelisted", "10,000 chars", "forbidden-word scan over the WHOLE"):
        assert stale not in text, stale
    readme = (WIDGETS / "tools" / "README.md").read_text()
    assert all(w in readme for w in ("HOMARR_REPO", "--batch", "--probe", "exit code", "network-error", "lowContrastText", "text nodes joined by a separator"))


# =========================================================================== harness-backed (real Homarr runtime): skipped when node / the checkout is missing
@pytest.fixture(scope="session")
def probe():
    p = bv.harness_probe()
    if not p or not p.get("ok"):
        pytest.skip("render-check needs node >= 22 and the Homarr checkout with node_modules ($HOMARR_REPO, see widgets/tools/README.md): " + str(p and p.get("problems")))
    return p


@pytest.fixture(scope="session")
def rc_env(tmp_path_factory):
    return {"RENDER_CHECK_WORK": str(tmp_path_factory.mktemp("rc-work"))}


@pytest.fixture(scope="session")
def matrix(probe, rc_env, tmp_path_factory):
    """One harness process for the whole standard matrix (~1.5 min with screenshots): {job id: report}."""
    shots = tmp_path_factory.mktemp("shots") if probe.get("shots") else None
    code, res, err = bv.run_harness(bv.harness_jobs(shots), env=rc_env)
    assert res is not None, f"harness crashed (exit {code}): {err}"
    return {r["id"]: r for r in res["reports"]}


RENDER_IDS = [j["id"] for j in bv.harness_jobs()]
SHOT_IDS = [j["id"] for j in bv.harness_jobs(Path("/x")) if "shot" in j]


@pytest.mark.parametrize("job_id", RENDER_IDS)
def test_harness_render_is_clean(matrix, job_id):
    """Real schema + Import parser + analyzer + interpreter + React render: no runtime error, no yellow alert, no console error, no undefined/NaN text."""
    r = matrix[job_id]
    assert not r["failed"], r["failures"]
    assert r["schema"]["ok"] and r["schema"]["previewOk"] and r["import"]["ok"] and r["interpreter"]["ok"]
    assert not r["analyzer"]["errors"] and all("lineProps" in w for w in r["analyzer"]["warnings"])        # the only tolerated warning is the benign lineProps one
    assert r["limits"]["loadRequests"] == ["state"] and not r["advisories"], r["advisories"]


@pytest.mark.parametrize("stem", STEMS)
def test_import_review_matches_what_the_admin_must_confirm(matrix, stem):
    """The Import dialog shows origin, auth, scope, methods, permissions: loopback http://127.0.0.1:9111, no auth, GET, view, no actions."""
    assert matrix[f"{stem}/{bv.DEFAULT_FIXTURE[stem]}/ok"]["import"]["review"] == {
        "name": bv.WIDGETS[stem][0], "origins": ["http://127.0.0.1:9111"], "authTypes": ["none"], "networkScopes": ["loopback"],
        "methods": ["GET"], "permissions": ["view"], "hasActions": False}


@pytest.mark.parametrize("job_id", [i for i in RENDER_IDS if i.endswith(("/network-error", "/http-error", "/empty", "/null", "/ok"))])
def test_banner_appears_exactly_when_the_service_failed(matrix, job_id):
    text, scenario = matrix[job_id]["dom"]["text"], job_id.rsplit("/", 1)[1]
    assert ("ops service:" in text) == (scenario in ("network-error", "http-error")), text
    if scenario == "network-error":
        assert "External request failed" in text and "no data" in text.lower()
    if scenario == "http-error":
        assert "HTTP 500: Internal Server Error" in text


def test_no_data_states_never_claim_health(matrix):
    """A missing status file, an empty task list or an empty body must not render as "all checks clear" / "no cleanup jobs have run yet" / "stale never"."""
    for stem in bv.STATUS_STEMS:
        for job in (f"{stem}/no_status_file/ok", f"{stem}/no_tasks/ok", f"{stem}/ok/empty", f"{stem}/ok/null", f"{stem}/ok/network-error"):
            text = matrix[job]["dom"]["text"]
            assert "all checks clear" not in text and "no cleanup jobs have run yet" not in text and "stale never" not in text, job
        assert "no data" in matrix[f"{stem}/no_status_file/ok"]["dom"]["text"]
    # the messages still appear when they are true, so the checks above are not vacuous
    assert "all checks clear" in matrix["ops-overview/ok/ok"]["dom"]["text"] and "all checks clear" in matrix["ops-overview/checks_only/ok"]["dom"]["text"]
    assert "no cleanup jobs have run yet" in matrix["ops-jobs/checks_only/ok"]["dom"]["text"]
    assert "no cleanup jobs have run yet" not in matrix["ops-jobs/ok/ok"]["dom"]["text"]


def test_rendered_text_follows_the_payload_for_the_interesting_cases(matrix):
    assert "stale" in matrix["ops-overview/stale/ok"]["dom"]["text"].lower()
    assert "overwrites next" in matrix["ops-thermals/full_ring/ok"]["dom"]["text"] and "ring 168/168 h" in matrix["ops-load/full_ring/ok"]["dom"]["text"]
    t = matrix["ops-thermals/no_ring_file/ok"]["dom"]["text"]
    assert "no metrics yet" in t and "no ring data" in t and "ring 0/168 h" in t                  # an empty ring says so instead of drawing a zero line
    assert all("no history yet" in matrix[f"{s}/full_ring/{sc}"]["dom"]["text"] for s in bv.METRIC_STEMS for sc in ("network-error", "empty", "null"))
    assert "--" in matrix["ops-load/all_none/ok"]["dom"]["text"]                                  # a sensor that read nothing shows dashes, never 0
    assert "filling: first overwrite" in matrix["ops-load/partial_ring/ok"]["dom"]["text"]


@pytest.mark.parametrize("job_id", SHOT_IDS)
def test_real_chromium_render_has_no_overflow_clipped_labels_or_errors(matrix, job_id):
    if job_id not in matrix:
        pytest.skip("no Chromium / esbuild / Playwright for screenshots")
    r = matrix[job_id]
    stem, case, tracks, scheme = job_id.split("/")
    m = r["shot"]["metrics"]
    assert not r["failed"], r["failures"]
    assert m["horizontalOverflow"] is False and m["clippedChartLabels"] == [] and not m["runtimeRenderError"] and m["templateWarnings"] is None
    assert Path(r["shot"]["out"]).stat().st_size > 2_000                                           # a real PNG
    # legibility of every text element as drawn (WCAG AA: 4.5:1, 3:1 for large text): strict on light boards, where the .5 shades used to be 1.5 to 2.8:1;
    # on dark boards only the theme's own "dimmed" grey (3.99:1 on a tinted panel) is below AA, so there the floor is 3:1
    assert m["textElementsMeasured"] >= 15, m["textElementsMeasured"]
    if scheme == "light":
        assert m["lowContrastCount"] == 0, m["lowContrastText"][:4]
    assert all(w["ratio"] >= 3.0 for w in m["lowContrastText"]), m["lowContrastText"][:4]
    if stem in bv.METRIC_STEMS:
        assert m["svgCount"] >= 1                                                                    # the charts / sparklines really drew
    w, h = bv.WIDGETS[stem][3]
    if tracks == f"{w}x{h}":                                                                          # the recommended footprint: content fits with room to spare
        assert m["contentOverflowsTile"] is False and m["contentNaturalHeight"] <= m["tile"]["h"] - 30, (m["contentNaturalHeight"], m["tile"])


def test_the_paused_badge_renders_on_the_two_widgets_that_have_it(matrix):
    for stem in bv.PAUSED_STEMS:
        assert "paused" in matrix[f"{stem}/paused/ok"]["dom"]["text"].lower() and "paused" not in matrix[f"{stem}/ok/ok"]["dom"]["text"].lower()
    for stem in set(bv.STATUS_STEMS) - set(bv.PAUSED_STEMS):
        assert not matrix[f"{stem}/paused/ok"]["failed"]


def test_the_contrast_metric_sees_the_bare_shade_bug_and_the_fix(probe, rc_env, tmp_path):
    """Regression for the light-board finding: c="teal.5" text is 2.1:1 on white (the harness must flag it), the shipped light-dark() colour is not."""
    if not probe.get("shots"):
        pytest.skip("no Chromium / esbuild / Playwright for screenshots")
    jobs = []
    for name, color in (("bare", '"teal.5"'), ("scheme-aware", '"' + LD("teal") + '"'), ("bare-yellow", '"yellow.5"'), ("scheme-aware-yellow", '"' + LD("yellow") + '"')):
        f = tmp_path / f"{name}.jsx"
        f.write_text(f'<Stack p={{8}}><Text fz={{12}} c={color}>183.2 GiB</Text></Stack>')
        for scheme in ("light", "dark"):
            jobs.append({"id": f"{name}/{scheme}", "template": str(f), "fixtures": {"state": str(tmp_path / "fx.json")}, "noDom": True, "scheme": scheme,
                         "size": "300x120", "shot": str(tmp_path / f"{name}-{scheme}.png")})
    (tmp_path / "fx.json").write_text("{}")
    _code, res, err = bv.run_harness(jobs, env=rc_env)
    assert res is not None, err
    low = {r["id"]: r["shot"]["metrics"]["lowContrastText"] for r in res["reports"]}
    assert low["bare/light"] and low["bare/light"][0]["ratio"] < 2.5 and low["bare-yellow/light"][0]["ratio"] < 2.0
    assert not low["scheme-aware/light"] and not low["scheme-aware-yellow/light"]                  # >= 4.5:1 on white
    assert not low["bare/dark"] and not low["scheme-aware/dark"] and not low["scheme-aware-yellow/dark"]


def test_harness_catches_every_class_of_mistake(probe, rc_env, tmp_path):
    """The v2 replacement of the old check_templates tests: bad templates / files must FAIL in the real runtime, the control must pass."""
    bad = {
        "unguarded-map": ('<Stack>{data.state.nothing.map(x=><Text key="a">{x}</Text>)}</Stack>', "Calling method 'map'"),
        "unguarded-tofixed": ("<Stack><Text>{data.state.n.toFixed(1)}</Text></Stack>", "Calling method 'toFixed'"),
        "destructuring": ('<Stack>{(data.state.r||[]).map(([a])=><Text key="a">{a}</Text>)}</Stack>', "Callback parameters must be identifiers"),
        "credential": ("<Stack><Text>Token: {data.state.r}</Text></Stack>", "Credentials must use source authentication"),
        "nested-padding": ('<Stack><LineChart h={100} data={[]} dataKey="x" series={[]} xAxisProps={{padding:{left:6,right:6}}} /></Stack>', "BLOCKED_CAPABILITY"),
        "raw-html": ("<Stack><div>hi</div></Stack>", "UNKNOWN_COMPONENT"),
        "unknown-component": ("<Stack><Banana /></Stack>", "UNKNOWN_COMPONENT"),
        "duplicate-keys": ('<Stack>{(data.state.r||[]).map(x=><Text key="same">{x}</Text>)}</Stack>', "same key"),
        "typo-prop": ("<Stack><Text fsz={10}>x</Text></Stack>", "UNKNOWN_MANTINE_PROP"),
        "prints-undefined": ('<Stack><Text>{"v: "+data.state.missing}</Text></Stack>', "suspect text"),
        # the token glued to the previous text ("2/9" + "undefined"): textContent has no separator, a \b boundary used to miss every one of these
        "gap-sibling-undefined": ("<Stack><Text>cleanup apply 2/9</Text><Text>{String(data.state.nope)}</Text></Stack>", "suspect text"),
        "gap-concat-after-text": ('<Stack><Text>/media</Text><Text>{data.state.nope+" free"}</Text></Stack>', "suspect text"),
        "gap-nan-after-digit": ("<Stack><Text>9</Text><Text>{Number(data.state.nope).toFixed(1)}</Text></Stack>", "suspect text"),
        "gap-null-text": ('<Stack><Text>{"p: "+JSON.stringify(data.state.nope||null)}</Text></Stack>', "suspect text"),
        "gap-infinity": ('<Stack><Text>{"r "+(1/0)}</Text></Stack>', "suspect text"),
        "gap-true": ('<Stack><Text>{"paused "+(data.state.x===undefined)}</Text></Stack>', "suspect text"),
        "v2-control": ("<Stack>{(data.state.r||[]).map((x,i)=><Text key={i}>{x}</Text>)}</Stack>", None),
        # words that merely CONTAIN a suspect token are fine
        "word-boundary-control": ("<Stack><Text>annulled truest Infinitely falsetto nullable undefinedly 9NaNa</Text></Stack>", None),
    }
    fx = tmp_path / "fx.json"
    fx.write_text(json.dumps({"r": ["a", "b", "c"], "n": None}))
    jobs = []
    for name, (t, _expect) in bad.items():
        f = tmp_path / f"{name}.jsx"
        f.write_text(t)
        jobs.append({"id": name, "template": str(f), "fixtures": {"state": str(fx)}})
    v1 = tmp_path / "v1.json"
    v1.write_text(json.dumps({"name": "x", "url": "http://127.0.0.1:9111/disk", "authType": "none", "method": "GET", "displayType": "customJsx",
                              "displayConfig": {"type": "customJsx", "template": "<Stack/>"}}))
    jobs.append({"id": "v1-file", "definition": str(v1), "noDom": True})
    code, res, err = bv.run_harness(jobs, env=rc_env)
    assert res is not None, err
    got = {r["id"]: r for r in res["reports"]}
    for name, (_t, expect) in bad.items():
        if expect is None:
            assert not got[name]["failed"], got[name]["failures"]
        else:
            assert got[name]["failed"] and expect in " ".join(got[name]["failures"]), (name, got[name]["failures"])
    assert got["v1-file"]["failed"] and not got["v1-file"]["import"]["ok"] and "UNSUPPORTED_FIELD" in json.dumps(got["v1-file"]["import"]["issues"])
    assert code == 1


def test_real_schema_agrees_with_the_credential_table(probe, rc_env, tmp_path):
    """The pure-Python port (build_v2.has_credential_literal) and the real zod schema agree on every row of docs 3.8."""
    jobs = []
    for i, t in enumerate(CRED_FAIL + CRED_OK):
        f = tmp_path / f"c{i}.jsx"
        f.write_text(t)
        jobs.append({"id": t, "template": str(f), "noDom": True})
    _code, res, err = bv.run_harness(jobs, env=rc_env)
    assert res is not None, err
    got = {r["id"]: any("Credentials must use source authentication" in x for x in r["failures"]) for r in res["reports"]}
    assert all(got[t] for t in CRED_FAIL) and not any(got[t] for t in CRED_OK), got


# BAD cases that are PURE schema violations (the others are policy of this repo or only fail at run time: public scope, bearer auth, five loads,
# unguarded method calls, name keys, nested padding, v1 data reads, comment/indent leftovers, NFD text which the schema normalises)
SCHEMA_VIOLATIONS = ["v1 keys", "missing sources", "wrong schema tag", "null description", "blank name", "name over 128", "description over 512",
                     "nine sources", "dotted source id", "unknown source key", "options not an object", "no default source", "missing scope",
                     "base url with query", "base url with userinfo", "ftp base url", "dotted request id", "digit request id", "mutation kind",
                     "interval trigger", "lowercase method", "fragment in path", "relative path", "protocol relative path", "cache too long",
                     "cache fractional", "cache boolean", "unknown request key", "unknown source", "oversize template", "destructured param",
                     "block arrow", "raw html", "unknown component", "event handler", "credential label", "credential comparison",
                     "credential in description", "credential key", "shadowed binding"]


def test_real_schema_rejects_every_pure_schema_violation_the_lint_names(probe, rc_env, tmp_path):
    """The lint's strict-shape / id / enum / size rules are ports of the zod schema: each violation must also be rejected by the REAL schema
    and the Import parser, and the untouched control must pass both."""
    jobs = []
    for i, what in enumerate(["control"] + SCHEMA_VIOLATIONS):
        f = tmp_path / f"{i}.json"
        f.write_text(json.dumps(mutated(BAD[what][0]) if what != "control" else load("ops-overview")))
        jobs.append({"id": what, "definition": str(f), "noDom": True})
    _code, res, err = bv.run_harness(jobs, env=rc_env)
    assert res is not None, err
    got = {r["id"]: r for r in res["reports"]}
    assert got["control"]["schema"]["ok"] and got["control"]["import"]["ok"] and not got["control"]["failed"], got["control"]["failures"]
    missed = [w for w in SCHEMA_VIOLATIONS if got[w]["schema"]["ok"] or got[w]["import"]["ok"]]
    assert not missed, f"the real schema accepts what the lint rejects: {missed}"


def test_lint_flags_the_cases_the_real_schema_accepts_only_by_policy():
    """The complement: these pass the zod schema but are wrong for the ops widgets, which is why the lint (and the harness for the runtime ones) exists."""
    policy = [w for w in BAD if w not in SCHEMA_VIOLATIONS]
    assert policy and all(bv.lint(mutated(BAD[w][0])) for w in policy)


def test_a_row_stored_like_the_app_stores_it_reads_back_and_renders(probe, rc_env, tmp_path):
    """docs 2.6 / 4.3(5): custom_widget_v2_definition columns are superjson strings of the PARSED definition (defaults filled), the template is plain text."""
    db = tmp_path / "copy.sqlite"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE custom_widget_v2_definition (id text PRIMARY KEY NOT NULL, name text NOT NULL, description text, icon_url text, sources text NOT NULL,"
              " requests text NOT NULL, options text NOT NULL, template text NOT NULL, enabled integer DEFAULT 1 NOT NULL, created_at integer NOT NULL,"
              " updated_at integer NOT NULL, creator_id text)")
    sj = lambda v: json.dumps({"json": v}, separators=(",", ":"), ensure_ascii=False)  # noqa: E731
    jobs = []
    for n, stem in enumerate(STEMS):
        d = load(stem)
        req = {"state": {"source": "default", "kind": "query", "method": "GET", "path": d["requests"]["state"]["path"], "trigger": "load", "auth": "inherit",
                         "cacheSeconds": 5, "permission": "view"}}
        c.execute("INSERT INTO custom_widget_v2_definition VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (f"id{n:022d}", d["name"], d["description"], None, sj(d["sources"]), sj(req), sj({}), d["template"], 1, 1790000000, 1790000000, None))
        jobs.append({"id": stem, "db": str(db), "name": d["name"], "fixtures": {"state": str(bv.fixture_path(stem, bv.DEFAULT_FIXTURE[stem]))}, "text": True})
    c.commit()
    c.close()
    code, res, err = bv.run_harness(jobs, env=rc_env)
    assert res is not None, err
    assert code == 0 and all(not r["failed"] for r in res["reports"]), [(r["id"], r["failures"]) for r in res["reports"] if r["failed"]]
    assert all(r["dom"]["text"] for r in res["reports"]) and all(r["import"] is None for r in res["reports"])      # --db has no import file to parse
