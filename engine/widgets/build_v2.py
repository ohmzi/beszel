#!/usr/bin/env python3
"""Build the seven Homarr v2 custom-widget import files (widgets/ops-*.json) from the readable templates (widgets/ops-*.jsx),
the sample payload fixtures (widgets/fixtures/<widget>/<case>.json) and check them, in pure Python 3.12 (no node needed).

  python3 widgets/build_v2.py                rewrite widgets/ops-*.json and widgets/fixtures/
  python3 widgets/build_v2.py --check        verify the committed files equal the build output and lint every definition (exit 1 on any problem)
  python3 widgets/build_v2.py --render [DIR] run the real-runtime harness (widgets/tools/render-check.mjs, needs the Homarr checkout) over every
                                             fixture, scenario, size and colour scheme; screenshots go to DIR when given (exit 1 on any failure)

The .jsx files are the source of truth: one element per line, `//` lines are build-time comments. The build strips indentation, blank
lines and comment lines, so a template is one JSX expression the Import button accepts (docs/HOMARR_V2_WIDGETS.md 2.2, 5.1).
This file replaced build_widgets.py + build_metrics_widgets.py (v1 `url/authType/displayConfig` files, which Homarr v2 rejects).
"""
from __future__ import annotations

import calendar
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

BASE_URL = "http://127.0.0.1:9111"          # fetched server-side by the Homarr container (host network): needs networkScope "loopback"
SCHEMA = "homarr-custom-widget-v2"
HARNESS = HERE / "tools" / "render-check.mjs"
FIXTURES = HERE / "fixtures"                # sample payloads: what the ops service sends, one file per widget and situation
RING_FIXTURES = HERE / "fixtures-metrics"   # metrics-ring exports (input of the thermal/load payload builders)
TZ = "America/Toronto"                      # x labels are rendered in the host time zone: fixtures are generated in this one
RING_NOW = float(calendar.timegm((2026, 10, 2, 1, 55, 0)))      # Thu 2026-10-01 21:55 America/Toronto, five minutes before the hour turns

WIDGETS = {
    # stem: name, route, description, footprint in board tracks (w, h; one track = 212 logical px), item refresh interval in seconds
    "ops-overview": ("Ops Overview", "overview",
                     "Homelab maintenance runner: overall health, root disk, memory, space freed, and the problems that need a look.", (3, 2), 30),
    "ops-disk": ("Ops Disk", "disk",
                 "Per-mount usage with days-to-full forecast, Docker footprint, backup age, SMART temperatures, growth watch.", (3, 3), 30),
    "ops-jobs": ("Ops Jobs", "jobs",
                 "Cleanup jobs (daily/weekly): last result, report vs apply mode, bytes freed, failing or late timers.", (3, 3), 30),
    "ops-guard": ("Ops Guard", "guard",
                  "Memory pressure, stuck or leaking container candidates, top memory users, orphans and the alert path.", (3, 3), 30),
    "ops-reclaim": ("Ops Reclaimed", "reclaim",
                    "Space freed by the cleaners (24h/7d/30d/90d), what is waiting to be cleaned, and plans awaiting approval.", (3, 3), 30),
    "ops-thermals": ("Ops Thermals 7d", "thermal",
                     "CPU/GPU/RAM temperature and CPU/case/GPU fan speed: now, 1 h / 24 h / 7 d averages and a 7-day hourly history.", (3, 3), 60),
    "ops-load": ("Ops Load 7d", "load",
                 "CPU, GPU and RAM utilisation: now, 1 h / 24 h / 7 d averages and a 7-day hourly history.", (3, 3), 60),
}
STATUS_STEMS = ["ops-overview", "ops-disk", "ops-jobs", "ops-guard", "ops-reclaim"]       # fed by status.json (homelab_maint.payloads)
METRIC_STEMS = ["ops-thermals", "ops-load"]                                                # fed by the metrics ring (payloads_metrics)
STATUS_CASES = ["ok", "warn", "crit", "stale", "stress", "no_tasks", "checks_only", "paused", "no_status_file"]
METRIC_CASES = ["full_ring", "partial_ring", "stale", "all_none", "no_gpu", "hot", "no_ring_file"]
WORST = {s: "stress" for s in STATUS_STEMS} | {s: "hot" for s in METRIC_STEMS}             # the fixture with the tallest content
DEFAULT_FIXTURE = {s: "ok" for s in STATUS_STEMS} | {s: "full_ring" for s in METRIC_STEMS}
SCENARIOS = ["ok", "http-error", "network-error", "empty", "null"]
PAUSED_STEMS = ["ops-overview", "ops-jobs"]                                                  # the two templates with a "paused" badge


# --------------------------------------------------------------------------- definitions
def squash(src: str) -> str:
    """One element per line, no indentation, no blank lines, no `//` authoring comments (they would render as text)."""
    return "\n".join(s for ln in src.splitlines() if (s := ln.strip()) and not s.startswith("//"))


def definition(stem: str) -> dict:
    """The v2 import object, key order as docs/HOMARR_V2_WIDGETS.md 2.2: one source, one load query `state`, template reads data.state.*."""
    name, route, desc, _tracks, _refresh = WIDGETS[stem]
    return {
        "$schema": SCHEMA,
        "name": name,
        "description": desc,
        "sources": {"default": {"baseUrl": BASE_URL, "networkScope": "loopback", "auth": "none"}},
        "requests": {"state": {"kind": "query", "method": "GET", "path": "/" + route, "source": "default", "trigger": "load", "cacheSeconds": 5}},
        "options": {},
        "template": squash((HERE / f"{stem}.jsx").read_text()),
    }


def render(stem: str) -> str:
    return json.dumps(definition(stem), indent=1, ensure_ascii=False) + "\n"    # ensure_ascii=False: ops-disk holds a literal U+00B7


# --------------------------------------------------------------------------- fixtures
@contextlib.contextmanager
def pinned_tz(tz: str = TZ):
    old = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def payload_cases() -> dict[str, dict[str, dict]]:
    """{widget stem: {case name: payload}}: exactly what the ops service would answer in each situation."""
    import make_sample_status as ms
    from homelab_maint import payloads, payloads_metrics as pm, server
    cases: dict[str, dict[str, dict]] = {stem: {} for stem in WIDGETS}
    with pinned_tz():
        fixtures = {s: ms.build(s) for s in ms.STATES}
        fixtures["stress"] = ms.stress()
        fixtures["no_tasks"] = {"generated_at": ms.NOW - 60, "overall": "ok", "tasks": {}}
        ok = ms.build("ok")        # healthy checks but no cleaner entries yet: the one state where "all clear" is true
        fixtures["checks_only"] = dict(ok, tasks={k: v for k, v in ok["tasks"].items() if v["klass"] == "C0"})
        fixtures["paused"] = dict(ms.build("warn"), paused=True)       # the kill switch is on while checks still warn: the orange "paused" badge
        for case, st in fixtures.items():
            for stem in STATUS_STEMS:
                cases[stem][case] = payloads.build(WIDGETS[stem][1], st, ms.NOW)
        with tempfile.TemporaryDirectory() as td:    # the body the server sends before the first run / after a state-dir wipe
            missing = server.StatusSource(Path(td) / "status.json")
            for stem in STATUS_STEMS:
                cases[stem]["no_status_file"] = json.loads(server.render(WIDGETS[stem][1], missing))
        for case in METRIC_CASES[:-1]:
            exp = json.loads((RING_FIXTURES / f"{case}.json").read_text())
            for stem in METRIC_STEMS:
                cases[stem][case] = pm.build(WIDGETS[stem][1], RING_NOW, exp)
        for stem in METRIC_STEMS:
            cases[stem]["no_ring_file"] = pm.build(WIDGETS[stem][1], RING_NOW, {})
    return cases


def fixture_text(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"      # what the server sends, plus a newline


def fixture_path(stem: str, case: str) -> Path:
    return FIXTURES / stem / f"{case}.json"


# --------------------------------------------------------------------------- lint (a pure-Python port of the rules the real schema enforces)
IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
KNOWN_COMPONENTS = {"Stack", "Group", "Text", "Badge", "ColorSwatch", "Divider", "SimpleGrid", "Paper", "Progress", "Sparkline", "LineChart"}
# definition-security.ts: a credential WORD followed by `:` or `=` and a non-harmless value fails the whole schema, also inside JSX text
CRED_ASSIGN = re.compile(r"\b(authorization|auth(?:entication)?(?:[ _-]?(?:tokens?|keys?|credentials?))?|credentials?|api[ _-]?keys?|passwords?|passwds?|secrets?|"
                         r"tokens?|access[ _-]?(?:tokens?|keys?)|refresh[ _-]?tokens?|client[ _-]?secrets?|private[ _-]?keys?|signing[ _-]?keys?)"
                         r"((?:[\"']?\s*[:=]\s*[\"']?))([^\s,;\"'}<>]+)", re.I)
CRED_SCHEME = re.compile(r"(^|[^A-Za-z0-9_-])(bearer|basic)([\s:_-]+)([\"']?)([A-Za-z0-9._~+/%=-]{8,})", re.I)
CRED_COMMON = re.compile(r"\b(?:sk|pk|rk)-(?:[A-Za-z0-9][A-Za-z0-9._-]{7,})\b|\b(?:sk_(?:live|test)|github_pat|glpat|gh[pousr]|hf_|xox[baprs])-?[A-Za-z0-9._-]{8,}\b|"
                         r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bAIza[0-9A-Za-z_-]{20,}\b|\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b", re.I)
HARMLESS = {"anonymous", "authentication", "basic", "bearer", "configured", "default", "disabled", "enabled", "example", "false", "inherit", "missing",
            "none", "optional", "placeholder", "public", "redacted", "required", "separate", "separately", "source", "true", "unset"}
CRED_KEY_WORDS = ("authorization", "authentication", "auth", "credential", "credentials", "api key", "api keys", "password", "passwords", "passwd", "passwds",
                  "secret", "secrets", "token", "tokens", "access token", "access tokens", "access key", "access keys", "refresh token", "refresh tokens",
                  "client secret", "client secrets", "private key", "private keys", "signing key", "signing keys")
BANNED_NESTED = re.compile(r"\b(left|right|top|bottom|pos|inset|zIndex|className|children|component|on[A-Z]\w*|\w+Ref|ref[A-Z]\w*)\s*:")
METHOD_CALL = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*(?:\??\.[A-Za-z_$][\w$]*)*)\??\.([A-Za-z_$][\w$]*)\(")   # lookbehind: linear on long runs
STATIC_ROOTS = {"Math", "JSON", "Object", "Array", "Number", "String", "Boolean", "Date"}   # static helpers (receiver is a global, never missing)


def has_credential_literal(s: str) -> bool:
    if CRED_COMMON.search(s):
        return True
    if any(m.group(5).lower() not in HARMLESS for m in CRED_SCHEME.finditer(s)):
        return True
    return any(m.group(3).strip().lower() not in HARMLESS for m in CRED_ASSIGN.finditer(s))


def key_risk(key: str) -> str | None:
    norm = re.sub(r"[^A-Za-z0-9]+", " ", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", key)).strip().lower()
    if any(norm == w or norm.endswith(" " + w) for w in CRED_KEY_WORDS):
        return "strong"
    return "ambiguous" if norm in ("key", "keys") or norm.endswith((" key", " keys")) else None


def _strings(v, path=()):
    if isinstance(v, str):
        yield path, v
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from _strings(x, path + (i,))
    elif isinstance(v, dict):
        for k, x in v.items():
            yield from _strings(x, path + (k,))


def _keys(v, path=()):
    if isinstance(v, dict):
        for k, x in v.items():
            yield path + (k,), x
            yield from _keys(x, path + (k,))
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from _keys(x, path + (i,))


def prop_spans(tpl: str) -> list[tuple[str, int, int]]:
    """(attribute name, start, end) of every `name={...}` JSX attribute value, found by brace matching that skips string literals."""
    out = []
    for m in re.finditer(r"\b([A-Za-z][\w]*)=\{", tpl):
        depth, i, q = 1, m.end(), None
        while i < len(tpl) and depth:
            c = tpl[i]
            if q:
                q = None if c == q and tpl[i - 1] != "\\" else q
            elif c in "\"'`":
                q = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        out.append((m.group(1), m.end(), i - 1))
    return out


def lint_template(tpl: str) -> list[str]:
    """Static rules from docs/HOMARR_V2_WIDGETS.md section 3: the same mistakes the analyzer/runtime would reject, caught without node."""
    p: list[str] = []
    if not (0 < len(tpl) <= 50_000):
        p.append(f"template length {len(tpl)} outside 1..50000")
    if tpl != unicodedata.normalize("NFC", tpl) or "\u200b" in tpl:
        p.append("template is not NFC-normalised / contains U+200B")
    if "\n\n" in tpl or re.search(r"^\s", tpl, re.M) or re.search(r"^//", tpl, re.M) or tpl != tpl.strip():
        p.append("template is not squashed (blank line, indentation, // comment line or outer whitespace)")
    used = set(re.findall(r"<([A-Z][A-Za-z0-9]*(?:\.[A-Za-z0-9]+)?)", tpl))
    if used - KNOWN_COMPONENTS:
        p.append(f"components outside the verified set: {sorted(used - KNOWN_COMPONENTS)}")
    if re.search(r"<[a-z]", tpl):
        p.append("raw HTML tag")
    if re.search(r"\bon[A-Z]\w*=|\b(className|ref|children|dangerouslySetInnerHTML)=", tpl):
        p.append("blocked prop (event handler, className, ref, children)")
    if re.search(r"=>\s*\{|\bnew\s+[A-Z]|\bawait\b|\bimport\b|\bfetch\b\s*\(|\b(window|document|globalThis|eval)\b\s*[.(\[]", tpl):
        p.append("forbidden syntax or identifier (block arrow, new, await, import, fetch, window, document, eval)")
    if re.search(r"\((?:\s*[\[{]|[^()]*=)[^()]*\)\s*=>", tpl):
        p.append("destructured or default-valued callback parameter")
    for m in re.finditer(r"\bdata(?:\.(?!state\b)\w+|\[|\?\.)|\bstatus(?:\.(?!state\b)\w+|\[|\?\.(?!state\b))", tpl):
        p.append(f"binding read outside data.state / status.state: {m.group(0)!r}")
        break
    if re.search(r"\(\s*(?:data|status|options|inputs)\s*[,)]\s*(?:=>|\w)", tpl):
        p.append("callback parameter shadows a reserved binding (data, status, options, inputs)")
    for m in METHOD_CALL.finditer(tpl):                         # `data.state.rows.map(` throws on undefined: only static helpers and guarded `(x||[]).map(` pass
        if m.group(1).split(".")[0].split("?")[0] not in STATIC_ROOTS:
            p.append(f"unguarded method call {m.group(0)!r} (wrap the receiver: (x||[]).{m.group(2)}( ...)")
    for name, a, b in prop_spans(tpl):                           # nested keys named left/right/... are stripped with a yellow alert (data= and series= are exempt)
        if name not in ("data", "series"):
            p += [f"key {k.group(1)!r} inside {name}={{...}} is stripped or blocked by the runtime" for k in BANNED_NESTED.finditer(tpl[a:b])]
    if has_credential_literal(tpl):
        p.append("credential-literal heuristic matches the template (a label/comparison like `<credential word>: <value>`)")
    if re.search(r'(?<![\w-])c=(?:"(?!dark\.)[a-z]+\.\d"|\{[^{}]*\+"\.\d"\})', tpl):         # c="teal.5" / c={x.c+".5"}: 1.6 to 2.8:1 on a light board (CONVENTIONS.md, Visual style); c="dark.9" is the text ON a bright fill, fine
        p.append('text colour with a bare Mantine shade (c="teal.5"): unreadable on light boards, use light-dark(color-mix(...),var(--mantine-color-X-5)) or "dimmed"')
    if any("," not in cb for cb in re.findall(r"\.map\((\([^)]*\)|\w+)=>", tpl)):
        p.append("every .map callback needs the index parameter: .map((x,i)=> ... key={i})")
    p += [f"key={{{k}}}: use the map index (server-truncated names collide)" for k in re.findall(r"key=\{([^}]*)\}", tpl) if not re.fullmatch(r"[ij]", k)]
    return p


def lint(d: dict) -> list[str]:
    """Strict-shape validation of one import file plus the template rules; [] means the real schema should accept it."""
    if not isinstance(d, dict):
        return ["definition must be a JSON object"]
    p: list[str] = []
    if set(d) - {"$schema", "name", "description", "iconUrl", "sources", "requests", "options", "template"}:
        p.append(f"unknown top-level keys {sorted(set(d) - {'$schema', 'name', 'description', 'iconUrl', 'sources', 'requests', 'options', 'template'})}")
    for req in ("$schema", "name", "sources", "requests", "options", "template"):
        if req not in d:
            p.append(f"missing {req}")
    if d.get("$schema") != SCHEMA:
        p.append("$schema must be homarr-custom-widget-v2")
    name = d.get("name")
    if not (isinstance(name, str) and 1 <= len(name.strip()) <= 128):
        p.append("name must be a 1..128 char string")
    if "description" in d and not (isinstance(d["description"], str) and len(d["description"]) <= 512):
        p.append("description must be a string <= 512 (omit it instead of null)")
    src = d.get("sources")
    if not (isinstance(src, dict) and 1 <= len(src) <= 8 and "default" in src):
        p.append("sources: 1..8 entries including 'default'")
    else:
        for sid, s in src.items():
            if not isinstance(s, dict):
                p.append(f"source {sid} must be an object")
                continue
            if not IDENT.match(sid) or len(sid) > 64:
                p.append(f"source id {sid!r} violates ^[A-Za-z][A-Za-z0-9_-]*$")
            if set(s) - {"type", "name", "baseUrl", "networkScope", "auth"}:
                p.append(f"source {sid}: unknown keys {sorted(set(s) - {'type', 'name', 'baseUrl', 'networkScope', 'auth'})}")
            if not re.fullmatch(r"https?://[^\s/?#@]+(?:/[^\s?#]*)?", str(s.get("baseUrl", ""))) or len(str(s.get("baseUrl"))) > 2048:
                p.append(f"source {sid}: baseUrl must be http(s), no userinfo, query or fragment")
            if s.get("networkScope") != "loopback":
                p.append(f"source {sid}: networkScope must be 'loopback' (the ops service listens on 127.0.0.1)")
            if s.get("auth", "none") != "none":
                p.append(f"source {sid}: the ops service needs no authentication")
    reqs = d.get("requests")
    if not (isinstance(reqs, dict) and 1 <= len(reqs) <= 64):
        p.append("requests: 1..64 entries")
        reqs = {}
    loads = 0
    for rid, r in reqs.items():
        if not isinstance(r, dict):
            p.append(f"request {rid} must be an object")
            continue
        if not IDENT.match(rid) or len(rid) > 64:
            p.append(f"request id {rid!r} violates ^[A-Za-z][A-Za-z0-9_-]*$")
        if set(r) - {"source", "kind", "method", "path", "trigger", "query", "body", "headers", "auth", "permission", "cacheSeconds", "confirmation", "invalidates"}:
            p.append(f"request {rid}: unknown keys")
        if r.get("kind", "query") not in ("query", "action"):
            p.append(f"request {rid}: kind must be query|action")
        if r.get("method", "GET") not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
            p.append(f"request {rid}: method must be an uppercase HTTP verb")
        if r.get("trigger", "load") not in ("load", "manual"):
            p.append(f"request {rid}: trigger must be load|manual")
        path = r.get("path")
        if not (isinstance(path, str) and path.startswith("/") and not path.startswith("//") and not re.search(r"[\\#?{}]", path) and len(path) <= 2048):
            p.append(f"request {rid}: path must start with '/', no '//', '\\', '#', '?' or braces (use the query object)")
        if r.get("source", "default") not in (src or {}):
            p.append(f"request {rid}: unknown source")
        cs = r.get("cacheSeconds")
        if cs is not None and not (type(cs) is int and 0 <= cs <= 3600):
            p.append(f"request {rid}: cacheSeconds must be an integer 0..3600")
        if r.get("kind", "query") == "query" and r.get("trigger", "load") == "load":
            loads += 1
    if loads > 4:
        p.append(f"{loads} load requests: keep <= 4 per widget (4 concurrent per user and item)")
    if not isinstance(d.get("options"), dict) or len(d.get("options", {})) > 64:
        p.append("options must be an object with <= 64 entries")
    for path, s in _strings(d):                                   # the same strings the server re-validates on every render
        if has_credential_literal(s):
            p.append(f"credential-literal heuristic matches {'.'.join(map(str, path))}")
    for path, v in _keys(d):
        k = str(path[-1])
        auth_control = len(path) == 3 and path[2] == "auth" and path[0] in ("sources", "requests")
        harmless = v is None or v == "" or isinstance(v, bool) or (isinstance(v, str) and v.strip().lower() in HARMLESS)
        if key_risk(k) == "strong" and not auth_control and not harmless:
            p.append(f"definition key {'.'.join(map(str, path))} looks like a credential field")
        if k in ("__proto__", "prototype", "constructor"):
            p.append(f"unsafe key {k}")
    if isinstance(d.get("template"), str):
        p += lint_template(d["template"])
    else:
        p.append("template must be a string")
    return p


# --------------------------------------------------------------------------- real-runtime harness (optional: needs node + the Homarr checkout)
def harness_probe(env: dict | None = None) -> dict | None:
    """`render-check.mjs --probe` as a dict ({ok, shots, repo, ...}), or None when node or the script is missing / the probe cannot run."""
    if not shutil.which("node") or not HARNESS.exists():
        return None
    r = subprocess.run(["node", str(HARNESS), "--probe"], capture_output=True, text=True, timeout=30, env=dict(os.environ, **(env or {})))
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def harness_available() -> bool:
    """True when node >= 22 and the Homarr checkout with its node_modules ($HOMARR_REPO) are usable."""
    probe = harness_probe()
    return bool(probe and probe.get("ok"))


def harness_jobs(shots: Path | None = None, schemes=("dark", "light"), sizes=("3x2", "3x3", "2x3")) -> list[dict]:
    """The standard matrix (docs/HOMARR_V2_WIDGETS.md 4.3): every fixture in scenario ok; every scenario on the default fixture; with `shots`,
    the tallest fixture at several tile sizes and both colour schemes plus the default fixture at the recommended footprint."""
    jobs = []
    for stem in WIDGETS:
        d = str(HERE / f"{stem}.json")
        cases = STATUS_CASES if stem in STATUS_STEMS else METRIC_CASES
        for case in cases:
            jobs.append({"id": f"{stem}/{case}/ok", "definition": d, "fixtures": {"state": str(fixture_path(stem, case))}, "text": True})
        for sc in SCENARIOS[1:]:
            jobs.append({"id": f"{stem}/{DEFAULT_FIXTURE[stem]}/{sc}", "definition": d, "fixtures": {"state": str(fixture_path(stem, DEFAULT_FIXTURE[stem]))}, "scenario": sc, "text": True})
        if shots:
            for scheme in schemes:
                for tracks in sizes:
                    jobs.append({"id": f"{stem}/{WORST[stem]}/{tracks}/{scheme}", "definition": d, "fixtures": {"state": str(fixture_path(stem, WORST[stem]))},
                                 "tracks": tracks, "scheme": scheme, "noDom": True, "shot": str(shots / f"{stem}-{WORST[stem]}-{tracks}-{scheme}.png")})
                w, h = WIDGETS[stem][3]
                for case in [DEFAULT_FIXTURE[stem]] + (["paused"] if stem in PAUSED_STEMS else []):        # the filled orange "paused" badge sits on the tinted header
                    jobs.append({"id": f"{stem}/{case}/{w}x{h}/{scheme}", "definition": d, "fixtures": {"state": str(fixture_path(stem, case))},
                                 "tracks": f"{w}x{h}", "scheme": scheme, "noDom": True, "shot": str(shots / f"{stem}-{case}-{w}x{h}-{scheme}.png")})
    return jobs


def run_harness(jobs: list[dict], timeout: int = 900, env: dict | None = None) -> tuple[int, dict | None, str]:
    """Run a batch through render-check.mjs; returns (exit code, parsed {"reports": [...], "failed": n} or None, stderr)."""
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "jobs.json"
        f.write_text(json.dumps({"jobs": jobs}))
        r = subprocess.run(["node", str(HARNESS), "--batch", str(f), "--json", "--timeout", str(timeout)], capture_output=True, text=True,
                           timeout=timeout + 30, env=dict(os.environ, **(env or {})))
    try:
        return r.returncode, json.loads(r.stdout), r.stderr
    except ValueError:
        return r.returncode, None, r.stdout[-500:] + r.stderr[-500:]


# --------------------------------------------------------------------------- main
def write_all() -> list[Path]:
    out = []
    for stem in WIDGETS:
        p = HERE / f"{stem}.json"
        p.write_text(render(stem))
        out.append(p)
    for stem, cases in payload_cases().items():
        for case, payload in cases.items():
            p = fixture_path(stem, case)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(fixture_text(payload))
            out.append(p)
    return out


def check() -> list[str]:
    bad = []
    for stem in WIDGETS:
        f = HERE / f"{stem}.json"
        if not f.exists() or f.read_text() != render(stem):
            bad.append(f"{f.name} is stale: run python3 widgets/build_v2.py")
        bad += [f"{stem}: {x}" for x in lint(definition(stem))]
    for stem, cases in payload_cases().items():
        for case, payload in cases.items():
            f = fixture_path(stem, case)
            if not f.exists() or f.read_text() != fixture_text(payload):
                bad.append(f"fixtures/{stem}/{case}.json is stale: run python3 widgets/build_v2.py")
    return bad


def main(argv: list[str]) -> int:
    if "--check" in argv:
        bad = check()
        print("\n".join(bad) or f"ok: {len(WIDGETS)} definitions + fixtures are current and lint clean")
        return 1 if bad else 0
    if "--render" in argv:
        i = argv.index("--render")
        shots = Path(argv[i + 1]) if len(argv) > i + 1 else None
        if not harness_available():
            print("render-check: node >= 22 or the Homarr checkout ($HOMARR_REPO) is missing, see widgets/tools/README.md", file=sys.stderr)
            return 3
        code, res, err = run_harness(harness_jobs(shots))
        if res is None:
            print(err, file=sys.stderr)
            return code or 1
        for r in res["reports"]:
            print(("FAIL " if r["failed"] else "PASS ") + r["id"] + ("  " + "; ".join(r["failures"])[:200] if r["failed"] else ""))
        print(f"{len(res['reports']) - res['failed']}/{len(res['reports'])} passed")
        return 1 if res["failed"] else 0
    for p in write_all():
        print(f"wrote {p.relative_to(HERE)}" + (f": {len(json.loads(p.read_text())['template'])} template chars" if p.parent == HERE else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
