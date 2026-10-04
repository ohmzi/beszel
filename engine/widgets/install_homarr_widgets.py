#!/usr/bin/env python3
"""Install the seven ops-* custom widgets into a Homarr v2 sqlite database (custom_widget_v2_definition + customApi items).

Homarr v2 only. The legacy custom_widget_definition / custom_widget_secret tables are never written (a v1 installer would create
"migration required" tiles); a database without custom_widget_v2_definition, or whose customApi items still point at legacy-only
ids, is refused. Spec: docs/HOMARR_V2_WIDGETS.md section 6, runbook: widgets/INSTALL.md. The widget JSON files are the v2 transfer
format produced by the builders (widgets/<stem>.json); --export-import-files re-emits them for the in-app Import button (option a).

  python3 widgets/install_homarr_widgets.py COPY.sqlite --dry-run                       # full plan + rollback SQL, writes nothing
  python3 widgets/install_homarr_widgets.py COPY.sqlite --boards Local-Big-Screen       # apply to a scratch COPY
  sudo python3 widgets/install_homarr_widgets.py --live /data/compose/5/homarr/appdata/db/db.sqlite \\
       --backup-dir /data/compose/5/homarr/appdata/db/backup-$(date +%Y%m%d-%H%M%S)      # the only way to write the live file
  sudo python3 widgets/install_homarr_widgets.py --live LIVE --backup-dir NEW_DIR --rollback BACKUP_DIR/ops-widgets-manifest.json
  python3 widgets/install_homarr_widgets.py --export-import-files /tmp/ops-widgets       # files + checklist for Manage > Custom widgets > Import

Safety (every write path): the target is a copy, or --live PATH with --backup-dir DIR (a path that looks like the live Homarr
database is refused as a positional argument); an online sqlite backup (integrity_check + non-empty) goes into a NEW dir BEFORE any
write; a non-empty db.sqlite-journal / -wal aborts; busy_timeout 15 s; ONE short BEGIN IMMEDIATE transaction in which the plan is
(re)computed from the rows as they are NOW; foreign_key_check (no new violations) and quick_check run before COMMIT; a manifest of
every inserted/updated row is written next to the backup so --rollback removes exactly those rows (children first).
Plan: one customApi item per widget per board, one item_layout row per layout of the board, appended BELOW the lowest existing
item/container (policy B, never overlapping), left to right, wrapping at the lane's column count.
A same-named definition that DIFFERS from the shipped file, or is disabled, is never silently adopted (tiles on it would show another template while the run
reported success): the plan says so and the run is refused (exit 2, nothing written) unless --update-existing (overwrite it, reversible) or --accept-existing (place
tiles on it as it is) is given.
Exit: 0 done / nothing to do, 2 refused (nothing changed), 1 error (a failed write is rolled back, nothing changed).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import sqlite3
import string
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

HERE = Path(__file__).resolve().parent
SCHEMA_TAG = "homarr-custom-widget-v2"
DEF_TABLE = "custom_widget_v2_definition"
LEGACY_TABLES = ("custom_widget_definition", "custom_widget_secret")          # read-only for us, always
ORDER = ["ops-overview", "ops-disk", "ops-jobs", "ops-guard", "ops-reclaim", "ops-thermals", "ops-load"]
# Content-fit footprints in board tracks (WxH, spec 6.3: one track = 212 logical px) and item refresh seconds (spec 6.5).
SIZES = {"ops-overview": (3, 2), "ops-disk": (3, 3), "ops-jobs": (3, 3), "ops-guard": (3, 3), "ops-reclaim": (3, 3),
         "ops-thermals": (3, 3), "ops-load": (3, 3)}
REFRESH = {s: 30 for s in ORDER} | {"ops-thermals": 60, "ops-load": 60}
LIVE_HINTS = [re.compile(p) for p in (r"^/data/compose/[^/]+/homarr/", r"/appdata/db/db\.sqlite$", r"^/volume1/docker/homarr/")]
MANIFEST_FORMAT = "homelab-maint-ops-widgets-manifest/1"
MANIFEST_NAME = "ops-widgets-manifest.json"
ITEM_ADVANCED = '{"json":{"title":null,"customCssClasses":[],"borderColor":""}}'      # what the app writes for a fresh customApi item
ENFORCE_FK = True                      # PRAGMA foreign_keys=ON on our write connection (Python's default is OFF); checked again before COMMIT
BUSY_MS = 15000
DEF_COLS = ["id", "name", "description", "icon_url", "sources", "requests", "options", "template", "enabled", "created_at",
            "updated_at", "creator_id"]
# Columns we read or write; every other NOT NULL column of a written table must have a default (else the schema moved on).
WRITES = {DEF_TABLE: DEF_COLS, "item": ["id", "board_id", "kind", "options", "advanced_options"],
          "item_layout": ["item_id", "section_id", "layout_id", "x_offset", "y_offset", "width", "height"]}
READS = {"board": ["id", "name"], "section": ["id", "board_id", "kind", "x_offset", "y_offset"],
         "layout": ["id", "board_id", "name", "column_count", "breakpoint", "role", "left_gutter_column_count", "right_gutter_column_count"],
         "section_layout": ["section_id", "layout_id", "parent_section_id", "y_offset", "height"], "user": ["id"]}


class Refuse(Exception):
    """A safety rule or a schema mismatch: nothing was changed."""


@dataclass
class Stmt:
    sql: str
    params: tuple = ()
    note: str = ""


@dataclass
class Plan:
    stmts: list = field(default_factory=list)
    lines: list = field(default_factory=list)          # human-readable plan (SQL comments)
    rec: dict = field(default_factory=lambda: {"definitions": [], "updated": [], "items": [], "item_layouts": []})
    blockers: list = field(default_factory=list)       # reasons the install must not go ahead (nothing is written while any exist)

    @property
    def empty(self) -> bool:
        return not self.stmts


@dataclass
class Widget:
    stem: str
    path: Path
    d: dict            # the v2 transfer definition
    cols: dict         # stored columns derived from it (name, description, icon_url, sources, requests, options, template)


@dataclass
class Opts:
    boards: list | None = None                      # None = every board, [] = definitions only, else names or ids
    sizes: dict = field(default_factory=lambda: dict(SIZES))
    refresh: int | None = None                      # None = per-widget default (REFRESH)
    update: bool = False
    accept: bool = False                            # place tiles on a same-named definition that differs from / is disabled relative to our file
    creator: str | None = None
    now: int = 0


# --------------------------------------------------------------------------- small helpers
def new_id() -> str:
    """cuid2-shaped like the ids the app writes: 24 chars, a leading letter then [a-z0-9]; never the reserved 'seed-' prefix."""
    while True:
        i = secrets.choice(string.ascii_lowercase) + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(23))
        if not i.startswith("seed-"):
            return i


def envelope(obj) -> str:
    """superjson.stringify of plain JSON: {"json": ...}, compact, UTF-8 (what JSON.stringify gives the app)."""
    return json.dumps({"json": obj}, separators=(",", ":"), ensure_ascii=False)


def unwrap(text):
    try:
        v = json.loads(text or "")
    except (TypeError, ValueError):
        return None
    return v.get("json") if isinstance(v, dict) else None


def ro_uri(path: Path) -> str:
    return f"file:{quote(str(path))}?mode=ro"


def ro_connect(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(ro_uri(path), uri=True, timeout=BUSY_MS / 1000)
    c.row_factory = sqlite3.Row
    return c


def has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def lit(v, full: bool) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if not full and len(s) > 90:
        s = s[:60] + f"...<{len(str(v))} chars>"
    return "'" + s.replace("'", "''") + "'"


def render_sql(st: Stmt, full: bool) -> str:
    it = iter(st.params)
    return re.sub(r"\?", lambda _m: lit(next(it), full), st.sql) + ";"


def parse_size(spec: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d+)x(\d+)", spec)
    if not m or not all(1 <= int(g) <= 24 for g in m.groups()):
        raise Refuse(f"bad size {spec!r}, want [STEM=]WxH with 1..24 tracks per axis")
    return int(m.group(1)), int(m.group(2))


def main_cols(lay) -> int:
    """Columns of the main lane (packages/definitions/src/section.ts getBoardLaneColumnCount): mobile layouts have no gutters."""
    total = max(1, int(lay["column_count"]))
    if lay["role"] == "mobile":
        return total
    left = min(max(0, int(lay["left_gutter_column_count"] or 0)), total - 1)
    right = min(max(0, int(lay["right_gutter_column_count"] or 0)), total - left - 1)
    return total - left - right


# --------------------------------------------------------------------------- python-side shape checks of a v2 definition
V1_KEYS = {"url", "authType", "method", "displayType", "displayConfig", "headerName", "requestBody", "stateSchema", "defaultState"}
TOP_KEYS = {"$schema", "name", "description", "iconUrl", "sources", "requests", "options", "template"}
ID_RX = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
REQ_KEYS = {"source", "kind", "method", "path", "trigger", "query", "auth", "permission", "cacheSeconds"}
# Port of definition-security.ts: any of these in ANY string of the definition fails the app's schema on EVERY render.
_HARMLESS = {"anonymous", "authentication", "basic", "bearer", "configured", "default", "disabled", "enabled", "example", "false",
             "inherit", "missing", "none", "optional", "placeholder", "public", "redacted", "required", "separate", "separately",
             "source", "true", "unset"}
_CRED_COMMON = re.compile(
    r"\b(?:sk|pk|rk)-(?:[A-Za-z0-9][A-Za-z0-9._-]{7,})\b|\b(?:sk_(?:live|test)|github_pat|glpat|gh[pousr]|hf_|xox[baprs])-?[A-Za-z0-9._-]{8,}\b"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bAIza[0-9A-Za-z_-]{20,}\b|\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b", re.I | re.A)
_CRED_SCHEME = re.compile(r"(^|[^A-Za-z0-9_-])(bearer|basic)([\s:_-]+)([\"']?)([A-Za-z0-9._~+/%=-]{8,})", re.I | re.A)
_CRED_ASSIGN = re.compile(
    r"\b(authorization|auth(?:entication)?(?:[ _-]?(?:tokens?|keys?|credentials?))?|credentials?|api[ _-]?keys?|passwords?|passwds?|secrets?"
    r"|tokens?|access[ _-]?(?:tokens?|keys?)|refresh[ _-]?tokens?|client[ _-]?secrets?|private[ _-]?keys?|signing[ _-]?keys?)"
    r"([\"']?\s*[:=]\s*[\"']?)([^\s,;\"'}<>]+)", re.I | re.A)
_STRONG = ("authorization", "authentication", "auth", "credential", "credentials", "api key", "api keys", "password", "passwords",
           "passwd", "passwds", "secret", "secrets", "token", "tokens", "access token", "access tokens", "access key", "access keys",
           "refresh token", "refresh tokens", "client secret", "client secrets", "private key", "private keys", "signing key", "signing keys")


def credential_literal(text: str) -> bool:
    return bool(_CRED_COMMON.search(text)
                or any(m.group(5).strip().lower() not in _HARMLESS for m in _CRED_SCHEME.finditer(text))
                or any(m.group(3).strip().lower() not in _HARMLESS for m in _CRED_ASSIGN.finditer(text)))


def key_risk(key: str) -> str | None:
    n = re.sub(r"[^A-Za-z0-9]+", " ", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", key)).strip().lower()
    if not n:
        return None
    if any(n == p or n.endswith(" " + p) for p in _STRONG):
        return "strong"
    return "ambiguous" if n in ("key", "keys") or n.endswith((" key", " keys")) else None


def credential_issues(value, path=()):
    """Paths in the definition the app would reject with 'Credentials must use source authentication'."""
    if isinstance(value, list):
        for i, v in enumerate(value):
            yield from credential_issues(v, (*path, i))
    elif isinstance(value, str):
        if credential_literal(value):
            yield path
    elif isinstance(value, dict):
        for k, child in value.items():
            risk, p = key_risk(k), (*path, k)
            auth_control = len(p) == 3 and p[2] == "auth" and p[0] in ("sources", "requests")
            if risk == "strong" and not auth_control and child not in (None, "") and not (
                    isinstance(child, bool) or (isinstance(child, str) and child.strip().lower() in _HARMLESS)):
                yield p
            elif risk == "ambiguous" and isinstance(child, str) and credential_literal(child):
                yield p
            else:
                yield from credential_issues(child, p)


def _url(v):
    try:
        return urlsplit(v) if isinstance(v, str) else None
    except ValueError:
        return None


def check_definition(d) -> list[str]:
    """Problems with a v2 transfer definition that can be seen without the Homarr checkout (empty list = shape OK)."""
    if not isinstance(d, dict):
        return ["not a JSON object"]
    out: list[str] = []
    v1 = sorted(V1_KEYS & d.keys())
    if v1:
        out.append(f"this is a v1 (customJsx) file: it has {v1}; rebuild it as a v2 definition (sources/requests/template)")
    out += [f"unknown top-level key {k!r}" for k in sorted(d.keys() - TOP_KEYS - V1_KEYS)]
    if d.get("$schema") != SCHEMA_TAG:
        out.append(f"$schema must be {SCHEMA_TAG!r}")
    name = d.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 128 or name != name.strip():
        out.append("name must be a trimmed string of 1..128 chars")
    if "description" in d and not (isinstance(d["description"], str) and len(d["description"]) <= 512):
        out.append("description must be a string <= 512 chars (omit it instead of null)")
    if "iconUrl" in d:
        u = _url(d["iconUrl"])
        if not u or u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or len(d["iconUrl"]) > 2048:
            out.append("iconUrl must be an absolute http(s) URL without credentials")
    srcs, reqs, opts, tpl = d.get("sources"), d.get("requests"), d.get("options", {}), d.get("template")
    if not isinstance(srcs, dict) or "default" not in srcs or not 1 <= len(srcs) <= 8:
        out.append("sources must be an object of 1..8 entries including 'default'")
        srcs = {}
    for sid, s in srcs.items():
        if not ID_RX.match(sid) or not isinstance(s, dict):
            out.append(f"source {sid!r}: bad id or not an object")
            continue
        extra = sorted(set(s) - {"type", "name", "baseUrl", "networkScope", "auth"})
        u = _url(s.get("baseUrl"))
        if extra or s.get("type", "http") != "http":
            out.append(f"source {sid!r}: unsupported keys/type {extra or s.get('type')} (http sources only)")
        if not u or u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment or "?" in s["baseUrl"] or "#" in s["baseUrl"]:
            out.append(f"source {sid!r}: baseUrl must be an http(s) URL with no userinfo, query or fragment")
        if s.get("networkScope") not in ("public", "private", "loopback"):
            out.append(f"source {sid!r}: networkScope (public|private|loopback) is required")
        if s.get("auth", "none") != "none":
            out.append(f"source {sid!r}: only auth 'none' is supported here (this installer writes no secrets)")
    if not isinstance(reqs, dict) or not 1 <= len(reqs) <= 64:
        out.append("requests must be an object of 1..64 entries")
        reqs = {}
    loads = 0
    for rid, r in reqs.items():
        if not ID_RX.match(rid) or not isinstance(r, dict):
            out.append(f"request {rid!r}: bad id or not an object")
            continue
        if set(r) - REQ_KEYS:
            out.append(f"request {rid!r}: unsupported keys {sorted(set(r) - REQ_KEYS)} (read-only queries only)")
        p, cs = r.get("path"), r.get("cacheSeconds")
        if r.get("kind", "query") != "query" or r.get("method", "GET") != "GET":
            out.append(f"request {rid!r}: only kind 'query' with method GET is supported (read-only widgets)")
        if not isinstance(p, str) or not p.startswith("/") or p.startswith("//") or "#" in p or "?" in p or "\\" in p or len(p) > 2048:
            out.append(f"request {rid!r}: path must start with '/', and contain no '?', '#', '\\\\' (use 'query')")
        if r.get("source", "default") not in srcs:
            out.append(f"request {rid!r}: unknown source {r.get('source')!r}")
        if r.get("trigger", "load") not in ("load", "manual"):
            out.append(f"request {rid!r}: trigger must be load or manual")
        if cs is not None and (isinstance(cs, bool) or not isinstance(cs, int) or not 0 <= cs <= 3600):
            out.append(f"request {rid!r}: cacheSeconds must be an integer 0..3600")
        loads += r.get("trigger", "load") == "load"
    if loads > 4:
        out.append(f"{loads} load requests: more than 4 run in parallel and hit the per-widget concurrency cap")
    if not isinstance(opts, dict) or len(opts) > 64 or not all(ID_RX.match(k) for k in opts):
        out.append("options must be an object of <= 64 entries with valid ids")
    if not isinstance(tpl, str) or not tpl.strip():
        out.append("template must be a non-empty string")
    else:
        if len(tpl) > 50000:
            out.append(f"template is {len(tpl)} chars (limit 50000)")
        if unicodedata.normalize("NFC", tpl) != tpl or "​" in tpl:
            out.append("template must already be NFC with no U+200B (the app normalises it, the stored text would differ)")
        for kind in ("data", "status"):
            for ref in sorted(set(re.findall(rf"(?<![\w.$]){kind}\.([A-Za-z][A-Za-z0-9_]*)", tpl))):
                if ref not in reqs:
                    out.append(f"template reads {kind}.{ref} but there is no request named {ref!r} (v1 port: data.x must become data.<requestId>.x)")
    out += [f"credential-looking text at {'.'.join(map(str, p)) or 'definition'} (the app rejects it on every render)" for p in credential_issues(
        {k: v for k, v in d.items() if k in TOP_KEYS})]
    return out


def stored_columns(d: dict) -> dict:
    """The columns the app stores for this definition (parsed form with the schema defaults filled, like customWidget.import)."""
    srcs = {}
    for sid, s in d["sources"].items():
        o = {k: s[k] for k in ("type", "name") if k in s}
        srcs[sid] = {**o, "baseUrl": s["baseUrl"], "networkScope": s["networkScope"], "auth": s.get("auth", "none")}
    reqs = {}
    for rid, r in d["requests"].items():
        o = {"source": r.get("source", "default"), "kind": r.get("kind", "query"), "method": r.get("method", "GET"), "path": r["path"]}
        if "query" in r:
            o["query"] = r["query"]
        o.update(trigger=r.get("trigger", "load"), auth=r.get("auth", "inherit"))
        if "cacheSeconds" in r:
            o["cacheSeconds"] = r["cacheSeconds"]
        reqs[rid] = {**o, "permission": r.get("permission", "view")}
    return {"name": d["name"], "description": d.get("description"), "icon_url": d.get("iconUrl"), "sources": srcs,
            "requests": reqs, "options": d.get("options", {}), "template": d["template"]}


def load_widgets(wdir: Path, stems: list[str]) -> list[Widget]:
    out, names = [], {}
    for stem in stems:
        p = wdir / f"{stem}.json"
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise Refuse(f"cannot read {p}: {exc}") from None
        problems = check_definition(d)
        if problems:
            raise Refuse(f"{p.name} is not an installable v2 definition:\n  - " + "\n  - ".join(problems))
        if d["name"] in names:
            raise Refuse(f"{p.name} and {names[d['name']]} have the same name {d['name']!r}")
        names[d["name"]] = p.name
        out.append(Widget(stem, p, d, stored_columns(d)))
    return out


def harness_check(paths: list[Path], fork: Path | None, widgets_dir: Path = HERE) -> tuple[str, str]:
    """Optional: ask the repo's real-runtime harness (widgets/tools/render-check.mjs --batch --no-dom; needs node and the Homarr checkout) to
    validate the definitions with the app's own schema, import parser and JSX analyzer. ("pass"|"fail"|"skipped", detail); skipped when
    node, the checkout or the harness is missing or cannot start (the python-side shape checks have already run either way)."""
    tool = Path(os.environ.get("HOMARR_WIDGET_VALIDATOR") or widgets_dir / "tools" / "render-check.mjs")
    node = shutil.which("node")
    fork = fork or Path(os.environ.get("HOMARR_REPO") or os.environ.get("HOMARR_FORK") or Path.home() / "StudioProjects" / "homarr")
    if not (node and tool.is_file() and (fork / "node_modules").is_dir()):
        return "skipped", "node, the Homarr checkout with node_modules, or widgets/tools/render-check.mjs is not available"
    env = {**os.environ, "HOMARR_REPO": str(fork), "NODE_NO_WARNINGS": "1"}
    with tempfile.TemporaryDirectory(prefix="hm-harness-") as td:
        jobs = Path(td) / "jobs.json"
        jobs.write_text(json.dumps({"defaults": {"noDom": True}, "jobs": [{"id": p.stem, "definition": str(p)} for p in paths]}))
        try:
            r = subprocess.run([node, str(tool), "--batch", str(jobs), "--json"], capture_output=True, text=True, timeout=180, env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            return "skipped", f"harness could not run: {exc}"
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-12:])
    if r.returncode not in (0, 1):                                    # 2 usage, 3 environment unavailable, 4 timeout
        return "skipped", f"harness exit {r.returncode}: {tail}"
    try:
        reports = json.loads(r.stdout)["reports"]
    except (ValueError, KeyError, TypeError):
        return "skipped", f"harness output not understood: {tail}"
    bad = [f"{x.get('id')}: " + "; ".join(map(str, x.get("failures") or ["failed"])) for x in reports if x.get("failed")]
    return ("fail", "\n".join(bad)) if bad or r.returncode == 1 else ("pass", "")


# --------------------------------------------------------------------------- target safety
def docker_live_paths(container: str = "homarr") -> list[str]:
    """Best effort, read-only: where the running Homarr container keeps its database (bind mount of /appdata)."""
    try:
        r = subprocess.run(["docker", "inspect", "--format", "{{json .Mounts}}", container], capture_output=True, text=True, timeout=5)
        return [f"{m['Source']}/db/db.sqlite" for m in (json.loads(r.stdout) if r.returncode == 0 else []) if m.get("Destination") == "/appdata"]
    except (OSError, ValueError, subprocess.SubprocessError, TypeError, KeyError):
        return []


def looks_live(path: Path, extra: list[str], docker: bool) -> str | None:
    """Why this path must be treated as the live database (None = looks like a copy)."""
    real = str(path.resolve())
    for rx in LIVE_HINTS:
        if rx.search(real):
            return f"path matches the live Homarr data directory ({rx.pattern})"
    for e in [*extra, *filter(None, os.environ.get("HOMARR_LIVE_DB", "").split(":")), *(docker_live_paths() if docker else [])]:
        try:
            if os.path.samefile(path, e):
                return f"same file as the declared live database {e}"
        except OSError:
            continue
    return None


def inflight(path: Path) -> str | None:
    """A non-empty rollback journal or WAL next to the file means another process is mid-write (or crashed mid-write)."""
    for suffix in ("-journal", "-wal"):
        p = Path(str(path) + suffix)
        try:
            if p.exists() and p.stat().st_size > 0:
                return f"{p.name} exists and is non-empty: a write is in flight (or a crashed one is unrecovered)"
        except OSError as exc:
            return f"cannot stat {p.name}: {exc}"
    return None


def resolve_target(a) -> tuple[Path, bool]:
    if a.live and a.db:
        raise Refuse("give either a DB copy as the positional argument, or --live PATH, not both")
    path = a.live or a.db
    if not path:
        raise Refuse("no database given: pass a COPY of the Homarr database, or --live PATH --backup-dir DIR")
    if not path.is_file():
        raise Refuse(f"{path} is not a file")
    why = looks_live(path, a.live_path, not a.no_docker_check)
    if a.live:
        if not a.dry_run and not a.backup_dir:
            raise Refuse("--live needs --backup-dir DIR (a NEW directory, e.g. <appdata>/db/backup-$(date +%Y%m%d-%H%M%S))")
        if not why:
            print(f"note: --live given for {path} (it does not look like the Homarr data dir)", file=sys.stderr)
        return path, True
    if why:
        raise Refuse(f"{path} looks like the LIVE database ({why}); use --live PATH --backup-dir DIR (--dry-run is fine with --live alone)")
    return path, False


def check_schema(conn) -> None:
    if not has_table(conn, DEF_TABLE):
        raise Refuse(f"{DEF_TABLE} is missing: this is a v1 (legacy custom_widget_definition only) database; upgrade Homarr to v2 first. "
                     "This installer never writes the legacy tables")
    for table, cols in {**WRITES, **READS}.items():
        info = {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})")}        # name -> (cid,name,type,notnull,dflt,pk)
        if not info:
            raise Refuse(f"table {table} is missing: not a Homarr v2 database this script understands")
        missing = [c for c in cols if c not in info]
        if missing:
            raise Refuse(f"{table} lacks columns {missing}: the schema changed, update this script")
        if table in WRITES:
            extra = [n for n, r in info.items() if n not in cols and r[3] and r[4] is None and not r[5]]
            if extra:
                raise Refuse(f"{table} has new NOT NULL columns without defaults {extra}: update this script")


def check_not_legacy_only(conn) -> None:
    v2 = {r[0] for r in conn.execute(f"SELECT id FROM {DEF_TABLE}")}
    legacy = {r[0] for r in conn.execute("SELECT id FROM custom_widget_definition")} if has_table(conn, "custom_widget_definition") else set()
    if legacy and not v2:
        raise Refuse("custom_widget_definition has rows but custom_widget_v2_definition is empty: the v1 -> v2 migration has not run")
    pending = set()
    for r in conn.execute("SELECT options FROM item WHERE kind='customApi'"):
        did = (unwrap(r[0]) or {}).get("definitionId")
        if did and did not in v2 and did in legacy:
            pending.add(did)
    if pending:
        raise Refuse(f"{len(pending)} customApi item(s) still point at legacy-only definition ids (v1 -> v2 migration pending): {sorted(pending)}")


# --------------------------------------------------------------------------- backup and checks
def _run_backup(src: Path, dest: Path) -> None:
    s, d = sqlite3.connect(ro_uri(src), uri=True, timeout=BUSY_MS / 1000), sqlite3.connect(dest)
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()


def verify_backup(src: Path, dest: Path) -> None:
    if not dest.is_file() or dest.stat().st_size == 0:
        raise Refuse(f"backup {dest} is missing or empty")
    names = lambda c: {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}      # noqa: E731
    s, d = sqlite3.connect(ro_uri(src), uri=True), sqlite3.connect(ro_uri(dest), uri=True)
    try:
        if d.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise Refuse(f"backup {dest} fails PRAGMA integrity_check")
        if names(s) - names(d) or DEF_TABLE not in names(d):
            raise Refuse(f"backup {dest} lacks tables of the source database")
    finally:
        s.close()
        d.close()


def backup(src: Path, dest_dir: Path) -> Path:
    """Online backup (sqlite backup API: consistent while Homarr has the file open) into a NEW dir, laid out like deploy-homarr.sh's
    db/backup-<stamp>/ (the file keeps its name, so the owner's restore recipe is the same). Verified before it is trusted."""
    if dest_dir.exists() and (not dest_dir.is_dir() or any(dest_dir.iterdir())):
        raise Refuse(f"--backup-dir {dest_dir} exists and is not an empty directory; give a NEW directory (never overwrite a backup)")
    probe = dest_dir.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    need = 2 * src.stat().st_size + (16 << 20)
    if shutil.disk_usage(probe).free < need:
        raise Refuse(f"not enough free space under {probe} for a backup ({need >> 20} MiB needed)")
    created = not dest_dir.exists()
    dest_dir.mkdir(parents=True, exist_ok=True)
    if created:
        os.chmod(dest_dir, 0o700)                    # the DB holds encrypted secrets; only chmod a directory we created
    dest = dest_dir / src.name
    try:
        _run_backup(src, dest)
        verify_backup(src, dest)
    except (Refuse, sqlite3.Error, OSError) as exc:
        dest.unlink(missing_ok=True)
        if created:
            try:
                dest_dir.rmdir()
            except OSError:
                pass
        raise Refuse(f"backup verification failed, nothing was changed: {exc}") from None
    os.chmod(dest, 0o600)
    return dest


def fk_violations(conn) -> set:
    try:
        return {tuple(r) for r in conn.execute("PRAGMA foreign_key_check")}
    except sqlite3.Error as exc:
        raise Refuse(f"PRAGMA foreign_key_check could not run: {exc}") from None


def quick_check(conn) -> str:
    return "; ".join(str(r[0]) for r in conn.execute("PRAGMA quick_check"))


def rw_connect(path: Path, busy_ms: int) -> sqlite3.Connection:
    c = sqlite3.connect(path, isolation_level=None, timeout=busy_ms / 1000)          # autocommit: we issue BEGIN/COMMIT ourselves
    c.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
    if ENFORCE_FK:
        c.execute("PRAGMA foreign_keys=ON")
    return c


def write_json_atomic(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- planning (pure function of a connection)
def build_plan(conn, widgets: list[Widget], o: Opts) -> Plan:
    """Everything is re-queried here: positions, ids, existing rows. Called on a read-only connection for --dry-run / the early refusal
    check, and AGAIN inside BEGIN IMMEDIATE, so the plan that runs is computed from the rows as they are while we hold the write lock."""
    conn.row_factory = sqlite3.Row
    plan = Plan()
    creator, how = o.creator, "--creator-id"
    if creator is None:
        r = conn.execute(f"SELECT creator_id FROM {DEF_TABLE} WHERE name='Thermals' AND creator_id IS NOT NULL").fetchone()
        how = "copied from the existing 'Thermals' definition"
        if not r:
            r = conn.execute(f"SELECT creator_id FROM {DEF_TABLE} WHERE creator_id IS NOT NULL AND id NOT LIKE 'seed-%' ORDER BY created_at, id").fetchone()
            how = "no 'Thermals' definition: copied from the oldest other definition with a creator"
        creator = r[0] if r else None
    elif not conn.execute('SELECT 1 FROM "user" WHERE id=?', (creator,)).fetchone():
        raise Refuse(f"--creator-id {creator!r} is not a user")
    plan.lines.append(f"creator_id for new definitions: {creator} ({how})")
    if creator is None:
        plan.lines.append("WARNING: no creator found, new definitions get creator_id NULL (valid, like the seeded widgets)")
    used = {r[0] for t in (DEF_TABLE, "item") for r in conn.execute(f"SELECT id FROM {t}")}

    def fresh() -> str:
        while (i := new_id()) in used:
            pass
        used.add(i)
        return i

    # -- definitions: idempotent by name
    def_ids: dict[str, str] = {}
    blocked: set[str] = set()                           # stems whose existing definition must not get tiles (see Plan.blockers)
    for w in widgets:
        rows = conn.execute(f"SELECT * FROM {DEF_TABLE} WHERE name=? ORDER BY created_at, id", (w.d["name"],)).fetchall()
        if len(rows) > 1:
            raise Refuse(f"{len(rows)} definitions are named {w.d['name']!r} ({', '.join(r['id'] for r in rows)}): ambiguous; "
                         "delete or rename the extras in Manage > Custom widgets, then retry")
        c = w.cols
        if not rows:
            did = def_ids[w.stem] = fresh()
            plan.stmts.append(Stmt(f"INSERT INTO {DEF_TABLE} ({', '.join(DEF_COLS)}) VALUES ({', '.join('?' * len(DEF_COLS))})",
                                   (did, c["name"], c["description"], c["icon_url"], envelope(c["sources"]), envelope(c["requests"]),
                                    envelope(c["options"]), c["template"], 1, o.now, o.now, creator), f"definition {c['name']}: insert {did}"))
            plan.rec["definitions"].append({"id": did, "name": c["name"], "stem": w.stem})
            plan.lines.append(f"definition {c['name']}: INSERT <new id> (template {len(c['template'])} chars)")
            continue
        row = rows[0]
        def_ids[w.stem] = row["id"]
        cur = {"name": row["name"], "description": row["description"], "icon_url": row["icon_url"], "template": row["template"]}
        try:
            cur |= {k: stored_columns({**w.d, "sources": unwrap(row["sources"]), "requests": unwrap(row["requests"]),
                                       "options": unwrap(row["options"]) or {}, "template": row["template"]})[k] for k in ("sources", "requests", "options")}
        except (KeyError, TypeError, AttributeError):
            cur |= {"sources": None, "requests": None, "options": None}
        if cur == {k: c[k] for k in cur}:
            plan.lines.append(f"definition {c['name']}: unchanged ({row['id']})")
        elif o.update:
            now = max(o.now, int(row["updated_at"] or 0) + 1)                      # an update must bump updated_at
            before = {k: row[k] for k in ("name", "description", "icon_url", "sources", "requests", "options", "template", "updated_at")}
            plan.stmts.append(Stmt(f"UPDATE {DEF_TABLE} SET name=?, description=?, icon_url=?, sources=?, requests=?, options=?, template=?, "
                                   "updated_at=? WHERE id=?", (c["name"], c["description"], c["icon_url"], envelope(c["sources"]), envelope(c["requests"]),
                                                              envelope(c["options"]), c["template"], now, row["id"]), f"definition {c['name']}: update {row['id']}"))
            plan.rec["updated"].append({"id": row["id"], "name": c["name"], "before": before, "after_updated_at": now})
            plan.lines.append(f"definition {c['name']}: UPDATE {row['id']} (differs from {w.path.name}; updated_at {before['updated_at']} -> {now})")
        elif o.accept:
            plan.lines.append(f"definition {c['name']}: exists ({row['id']}) but DIFFERS from {w.path.name}; kept as it is (--accept-existing), its tiles show ITS template")
        else:                                           # tiles bound to a foreign / stale template would look like a successful install: refuse instead
            plan.lines.append(f"definition {c['name']}: exists ({row['id']}) but DIFFERS from {w.path.name}")
            blocked.add(w.stem)
            plan.blockers.append(f"definition {c['name']!r} ({row['id']}) differs from {w.path.name}, so tiles placed on it would show another template: "
                                 "re-run with --update-existing to overwrite it (backed up, reversible with --rollback) or with --accept-existing to place tiles on it unchanged")
        if not row["enabled"]:
            plan.lines.append(f"{'WARNING' if o.accept else 'BLOCKED'}: definition {c['name']} ({row['id']}) is disabled; its tiles would say 'unavailable'")
            if not o.accept:
                blocked.add(w.stem)
                plan.blockers.append(f"definition {c['name']!r} ({row['id']}) is disabled: tiles on it say 'unavailable'; enable it in Manage > Custom widgets, or pass --accept-existing")

    # -- board items
    boards = conn.execute("SELECT id, name FROM board ORDER BY name").fetchall()
    sel = boards if o.boards is None else []
    if o.boards:
        by = {k: b for b in boards for k in (b["name"], b["id"])}
        unknown = [n for n in o.boards if n not in by]
        if unknown:
            raise Refuse(f"no board named {unknown}; boards: {', '.join(b['name'] for b in boards)}")
        sel = list({b["id"]: b for b in (by[n] for n in o.boards)}.values())
    if o.boards is not None and not o.boards:
        plan.lines.append("--boards none: definitions only, nothing is placed on a board")
    for b in sel:
        secs = [s for s in conn.execute("SELECT id, x_offset FROM section WHERE board_id=? AND kind='empty'", (b["id"],)) if s["x_offset"] not in (-1, 1)]
        if len(secs) != 1:
            raise Refuse(f"board {b['name']!r} has {len(secs)} main-lane 'empty' sections (expected exactly 1): not touching it")
        sec = secs[0]["id"]
        layouts = conn.execute("SELECT * FROM layout WHERE board_id=? ORDER BY breakpoint DESC, name, id", (b["id"],)).fetchall()
        roles = [lay["role"] for lay in layouts]
        if roles.count("base") != 1 or roles.count("mobile") != 1:
            raise Refuse(f"board {b['name']!r} needs exactly one Base and one Mobile layout (has roles {roles}): not touching it")
        have = {}
        for r in conn.execute("SELECT id, options FROM item WHERE board_id=? AND kind='customApi'", (b["id"],)):
            have.setdefault((unwrap(r["options"]) or {}).get("definitionId"), r["id"])
        cursor = {}                                    # layout id -> [x, y, row height, columns]
        plan.lines.append(f"board {b['name']} ({b['id']}), main section {sec}")
        for lay in layouts:
            bottom = max(conn.execute("SELECT COALESCE(MAX(y_offset + height), 0) FROM item_layout WHERE layout_id=? AND section_id=?", (lay["id"], sec)).fetchone()[0],
                         conn.execute("SELECT COALESCE(MAX(y_offset + height), 0) FROM section_layout WHERE layout_id=? AND parent_section_id=?", (lay["id"], sec)).fetchone()[0])
            cursor[lay["id"]] = [0, bottom, 0, main_cols(lay)]
            plan.lines.append(f"  layout {lay['name']} [{lay['role']}] {main_cols(lay)} columns: lowest existing bottom y={bottom}, new rows start there")
        for w in widgets:
            did = def_ids[w.stem]
            if w.stem in blocked:
                plan.lines.append(f"  {w.d['name']}: NOT placed (its definition is blocked, see above)")
                continue
            if did in have:
                plan.lines.append(f"  {w.d['name']}: already placed on this board (item {have[did]}), skipped")
                continue
            iid = fresh()
            wd0, ht = o.sizes.get(w.stem, SIZES[w.stem])
            refresh = o.refresh if o.refresh is not None else REFRESH[w.stem]
            plan.stmts.append(Stmt("INSERT INTO item (id, board_id, kind, options, advanced_options) VALUES (?, ?, ?, ?, ?)",
                                   (iid, b["id"], "customApi", envelope({"definitionId": did, "refreshInterval": refresh}), ITEM_ADVANCED),
                                   f"board {b['name']}: item {iid} for {w.d['name']}"))
            plan.rec["items"].append({"id": iid, "board_id": b["id"], "board": b["name"], "definition_id": did, "widget": w.d["name"]})
            for lay in layouts:
                cur = cursor[lay["id"]]
                wd = min(wd0, cur[3])
                if cur[0] + wd > cur[3]:               # wrap to a new row below the tallest item of the previous one
                    cur[0], cur[1], cur[2] = 0, cur[1] + cur[2], 0
                plan.stmts.append(Stmt("INSERT INTO item_layout (item_id, section_id, layout_id, x_offset, y_offset, width, height) "
                                       "VALUES (?, ?, ?, ?, ?, ?, ?)", (iid, sec, lay["id"], cur[0], cur[1], wd, ht),
                                       f"  layout {lay['name']}: x={cur[0]} y={cur[1]} {wd}x{ht}"))
                plan.rec["item_layouts"].append({"item_id": iid, "section_id": sec, "layout_id": lay["id"], "x": cur[0], "y": cur[1], "w": wd, "h": ht})
                plan.lines.append(f"  {w.d['name']} -> new item, {lay['name']}: ({cur[0]},{cur[1]}) {wd}x{ht} refresh {refresh}s")
                cur[0] += wd
                cur[2] = max(cur[2], ht)
    return plan


# --------------------------------------------------------------------------- rollback
def rollback_stmts(eff: dict) -> list[Stmt]:
    """Children first (item_layout, item, definition), then the restores of --update-existing rows. `eff` = ids to remove / rows to restore."""
    out = []
    marks = lambda ids: ",".join("?" * len(ids))                        # noqa: E731
    if eff["item_ids"]:
        out.append(Stmt(f"DELETE FROM item_layout WHERE item_id IN ({marks(eff['item_ids'])})", tuple(eff["item_ids"])))
        out.append(Stmt(f"DELETE FROM item WHERE id IN ({marks(eff['item_ids'])})", tuple(eff["item_ids"])))
    if eff["definition_ids"]:
        out.append(Stmt(f"DELETE FROM {DEF_TABLE} WHERE id IN ({marks(eff['definition_ids'])})", tuple(eff["definition_ids"])))
    for u in eff["restore"]:
        b = u["before"]
        out.append(Stmt(f"UPDATE {DEF_TABLE} SET name=?, description=?, icon_url=?, sources=?, requests=?, options=?, template=?, updated_at=? "
                        "WHERE id=? AND updated_at=?", (b["name"], b["description"], b["icon_url"], b["sources"], b["requests"], b["options"],
                                                        b["template"], b["updated_at"], u["id"], u["after_updated_at"])))
    return out


def rollback_plan(conn, rec: dict) -> tuple[list[Stmt], list[str]]:
    """What a rollback of this manifest does NOW: only rows that still are what we inserted; rows someone else has come to depend on stay."""
    conn.row_factory = sqlite3.Row
    lines, eff = [], {"item_ids": [], "definition_ids": [], "restore": []}
    for it in rec["items"]:
        row = conn.execute("SELECT board_id, kind, options FROM item WHERE id=?", (it["id"],)).fetchone()
        if not row:
            lines.append(f"item {it['id']} ({it['widget']}): already gone")
        elif (row["board_id"], row["kind"], (unwrap(row["options"]) or {}).get("definitionId")) != (it["board_id"], "customApi", it["definition_id"]):
            lines.append(f"WARNING: item {it['id']} ({it['widget']}) is no longer what was installed; left alone")
        else:
            eff["item_ids"].append(it["id"])
    others = {}
    for r in conn.execute("SELECT id, options FROM item WHERE kind='customApi'"):
        if r["id"] not in eff["item_ids"]:
            others.setdefault((unwrap(r["options"]) or {}).get("definitionId"), []).append(r["id"])
    for d in rec["definitions"]:
        row = conn.execute(f"SELECT name FROM {DEF_TABLE} WHERE id=?", (d["id"],)).fetchone()
        nsec = conn.execute("SELECT count(*) FROM custom_widget_v2_secret WHERE definition_id=?", (d["id"],)).fetchone()[0] if has_table(conn, "custom_widget_v2_secret") else 0
        if not row:
            lines.append(f"definition {d['name']} ({d['id']}): already gone")
        elif row["name"] != d["name"]:
            lines.append(f"WARNING: definition {d['id']} was renamed to {row['name']!r}; left alone")
        elif others.get(d["id"]) or nsec:
            lines.append(f"WARNING: definition {d['name']} ({d['id']}) is KEPT: {len(others.get(d['id'], []))} other item(s) / {nsec} secret(s) depend on it")
        else:
            eff["definition_ids"].append(d["id"])
    for u in rec["updated"]:
        row = conn.execute(f"SELECT updated_at FROM {DEF_TABLE} WHERE id=?", (u["id"],)).fetchone()
        if row and row["updated_at"] == u["after_updated_at"]:
            eff["restore"].append(u)
        else:
            lines.append(f"WARNING: definition {u['name']} ({u['id']}) was edited after the install (or is gone); its update is not reverted")
    return rollback_stmts(eff), lines


def load_manifest(path: Path) -> dict:
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        assert rec["format"] == MANIFEST_FORMAT
        for k, shape in (("definitions", ("id", "name")), ("updated", ("id", "name", "before", "after_updated_at")),
                         ("items", ("id", "board_id", "definition_id", "widget")), ("item_layouts", ("item_id", "section_id", "layout_id"))):
            assert isinstance(rec[k], list) and all(isinstance(e, dict) and all(f in e for f in shape) for e in rec[k])
        assert len(rec["items"]) <= 500 and all(isinstance(e["id"], str) for e in rec["items"] + rec["definitions"])
    except (OSError, ValueError, KeyError, AssertionError, TypeError) as exc:
        raise Refuse(f"{path} is not a manifest written by this installer ({exc.__class__.__name__})") from None
    return rec


# --------------------------------------------------------------------------- transactions
def locked_msg(exc: sqlite3.Error, busy_ms: int) -> str | None:
    return (f"the database stayed locked for {busy_ms} ms (another writer, probably Homarr, holds it); nothing was changed, retry in a moment"
            if "locked" in str(exc).lower() or "busy" in str(exc).lower() else None)


def transact(path: Path, busy_ms: int, build, manifest: dict | None, manifest_path: Path | None):
    """BEGIN IMMEDIATE -> build(conn) -> (stmts, info) -> run -> foreign_key_check + quick_check -> COMMIT. One short transaction."""
    if (why := inflight(path)):
        raise Refuse(why)
    conn = rw_connect(path, busy_ms)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            stmts, info = build(conn)
            if not stmts:
                conn.execute("ROLLBACK")
                return None
            before = fk_violations(conn)
            if manifest is not None:                      # record the rows BEFORE touching them; a crash leaves a harmless 'pending' file
                manifest.update(status="pending", **info)
                write_json_atomic(manifest_path, manifest)
            for st in stmts:
                conn.execute(st.sql, st.params)
            if (new := fk_violations(conn) - before):
                raise Refuse(f"foreign_key_check reports {len(new)} new violation(s) after the changes; rolled back, nothing was changed: {sorted(new)[:3]}")
            if (q := quick_check(conn)) != "ok":
                raise Refuse(f"quick_check failed before COMMIT ({q}); rolled back, nothing was changed")
            conn.execute("COMMIT")
            return stmts, info
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if manifest is not None and manifest_path and manifest_path.exists():
                try:
                    manifest["status"] = "aborted"
                    write_json_atomic(manifest_path, manifest)
                except OSError:
                    pass
            raise
    finally:
        conn.close()


def post_verify(path: Path, rec: dict) -> list[str]:
    bad = []
    c = ro_connect(path)
    try:
        for d in rec["definitions"]:
            if not c.execute(f"SELECT 1 FROM {DEF_TABLE} WHERE id=?", (d["id"],)).fetchone():
                bad.append(f"definition {d['id']} missing")
        for it in rec["items"]:
            if not c.execute("SELECT 1 FROM item WHERE id=? AND kind='customApi'", (it["id"],)).fetchone():
                bad.append(f"item {it['id']} missing")
        for l in rec["item_layouts"]:
            if not c.execute("SELECT 1 FROM item_layout WHERE item_id=? AND section_id=? AND layout_id=?", (l["item_id"], l["section_id"], l["layout_id"])).fetchone():
                bad.append(f"item_layout {l['item_id']}/{l['layout_id']} missing")
        if quick_check(c) != "ok":
            bad.append("quick_check failed")
    finally:
        c.close()
    return bad


def preview(path: Path, build):
    """Run build(read-only connection) after the schema checks. A database with a hot journal cannot even be read read-only."""
    try:
        ro = ro_connect(path)
        try:
            check_schema(ro)
            check_not_legacy_only(ro)
            return build(ro)
        finally:
            ro.close()
    except sqlite3.OperationalError as exc:
        if (why := inflight(path)):
            raise Refuse(f"{why}; it cannot be read until that write finishes or is recovered ({exc})") from None
        raise


def manifest_location(a, path: Path, bk: Path | None) -> Path:
    return (bk.parent / MANIFEST_NAME) if bk else path.with_name(f"{path.name}.ops-widgets-manifest-{time.strftime('%Y%m%d-%H%M%S')}.json")


def print_sql(stmts: list[Stmt], full: bool) -> None:
    print("BEGIN IMMEDIATE;")
    for st in stmts:
        print(f"-- {st.note}" if st.note else "--")
        print(render_sql(st, full))
    print("COMMIT;")


def cmd_install(a, path: Path, live: bool, widgets: list[Widget], o: Opts) -> int:
    busy = a.busy_timeout_ms
    if not a.dry_run and (why := inflight(path)):
        raise Refuse(why)
    plan = preview(path, lambda c: build_plan(c, widgets, o))
    mode = "DRY RUN" if a.dry_run else "APPLY"
    print(f"-- homelab-maint Homarr v2 widget installer [{mode}] db={path} ({'LIVE' if live else 'copy'})")
    if not a.dry_run:
        print("-- preview: new ids and positions are decided again under the write lock; the rows really written are listed under 'applied' below")
    for ln in plan.lines:
        print(f"-- {ln}")
    if plan.blockers:
        raise Refuse("nothing was changed; the install cannot go ahead as it is:\n  - " + "\n  - ".join(plan.blockers))
    if plan.empty:
        print("-- nothing to do (already installed): no backup, no write")
        return 0
    if a.dry_run:
        if (why := inflight(path)):
            print(f"-- WARNING: {why}; an install would abort", file=sys.stderr)
        print(f"-- would back up to {a.backup_dir / path.name} (online backup + integrity_check, then a manifest next to it)" if a.backup_dir else
              "-- no --backup-dir: " + ("a real --live run REFUSES without one" if live else "no backup for this copy; the manifest goes next to the database"))
        print("-- ids and timestamps are regenerated at install time, and positions are re-queried under the write lock")
        print_sql(plan.stmts, a.full_sql)
        print("-- rollback (ids of THIS plan; the real one is the manifest written next to the backup at install time):")
        print_sql(rollback_stmts({"item_ids": [i["id"] for i in plan.rec["items"]], "definition_ids": [d["id"] for d in plan.rec["definitions"]],
                                  "restore": plan.rec["updated"]}), a.full_sql)
        return 0
    if (why := inflight(path)):
        raise Refuse(why)
    bk = backup(path, a.backup_dir) if a.backup_dir else None
    if bk:
        print(f"-- backup written and verified: {bk}")
    mpath = manifest_location(a, path, bk)
    manifest = {"format": MANIFEST_FORMAT, "created_at": o.now, "db": str(path.resolve()), "live": live, "backup": str(bk) if bk else None}
    def build(conn):                                     # re-planned under the write lock from the rows as they are now
        p = build_plan(conn, widgets, o)
        if p.blockers:                                   # a definition changed between the preview and the lock
            raise Refuse("the rows changed since the preview and the install can no longer go ahead:\n  - " + "\n  - ".join(p.blockers))
        return p.stmts, p.rec

    done = transact(path, busy, build, manifest, mpath)
    if done is None:
        print("-- nothing to do: the rows changed since the preview and everything is already installed")
        return 0
    stmts, info = done
    manifest["status"] = "committed"
    try:
        write_json_atomic(mpath, manifest)
    except OSError as exc:                                  # rows are in; the 'pending' manifest already lists them and works for --rollback
        print(f"-- note: could not mark the manifest committed ({exc}); {mpath} still lists every row", file=sys.stderr)
    for st in stmts:
        if st.note:
            print(f"-- applied: {st.note}")
    bad = post_verify(path, info)
    if bad:
        print(f"-- POST-CHECK FAILED {bad}; undo with --rollback {mpath}", file=sys.stderr)
        return 1
    print(f"-- done: {len(stmts)} statements committed ({len(info['definitions'])} definitions, {len(info['updated'])} updated, {len(info['items'])} items, "
          f"{len(info['item_layouts'])} layout rows). Reload the board page (no restart needed).")
    print(f"-- manifest: {mpath}\n-- undo exactly these rows: --rollback {mpath}" + (f" (full restore: stop the container, copy {bk} over the db)" if bk else ""))
    return 0


def cmd_rollback(a, path: Path, live: bool) -> int:
    rec = load_manifest(a.rollback)
    if rec.get("db") and Path(rec["db"]) != path.resolve():
        print(f"note: the manifest was written for {rec['db']}, you are rolling back {path.resolve()}", file=sys.stderr)
    if not a.dry_run and (why := inflight(path)):
        raise Refuse(why)
    stmts, lines = preview(path, lambda c: rollback_plan(c, rec))
    print(f"-- homelab-maint Homarr v2 widget ROLLBACK [{'DRY RUN' if a.dry_run else 'APPLY'}] db={path} manifest={a.rollback}")
    for ln in lines:
        print(f"-- {ln}")
    if not stmts:
        print("-- nothing to roll back")
        return 0
    if a.dry_run:
        print_sql(stmts, a.full_sql)
        return 0
    if (why := inflight(path)):
        raise Refuse(why)
    bk = backup(path, a.backup_dir) if a.backup_dir else None
    if bk:
        print(f"-- backup written and verified: {bk}")
    done = transact(path, a.busy_timeout_ms, lambda c: (rollback_plan(c, rec)[0], {}), None, None)
    if done is None:
        print("-- nothing to roll back")
        return 0
    rec["status"] = "rolled_back"
    rec["rolled_back_at"] = int(time.time())
    try:
        write_json_atomic(a.rollback, rec)
    except OSError as exc:
        print(f"-- note: could not update {a.rollback}: {exc}", file=sys.stderr)
    if bk:
        write_json_atomic(bk.parent / "ops-widgets-rollback.json", {"manifest": str(a.rollback), "removed": [render_sql(s, True) for s in done[0]]})
    print(f"-- rolled back: {len(done[0])} statements committed")
    return 0


def cmd_export(a, widgets: list[Widget]) -> int:
    """Option (a): the transfer files for Manage > Custom widgets > Import, plus the checklist. Touches no database."""
    out = a.export_import_files.resolve()
    if out == HERE.resolve() or out == a.widgets_dir.resolve():
        raise Refuse("--export-import-files DIR must not be the widgets source directory")
    out.mkdir(parents=True, exist_ok=True)
    for w in widgets:
        (out / f"{w.stem}.json").write_text(json.dumps(w.d, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(widgets)} transfer file(s) to {out}:")
    for w in widgets:
        wd, ht = a.size_map.get(w.stem, SIZES[w.stem])
        print(f"  {w.stem}.json  {w.d['name']!r:22} size {wd}x{ht} tracks  refresh {REFRESH[w.stem]} s")
    print("""
In-app import checklist (option a, the safest: app-side validation, admin guard and audit trail apply):
  [ ] 1. Copy the files to the machine whose browser you use for Homarr (sign in as the admin, e.g. ohmz_h).
  [ ] 2. Homarr > Manage > Custom widgets > Import: pick one file (or paste its JSON anywhere on that page).
  [ ] 3. In the review dialog check: origin http://127.0.0.1:9111, authentication none, scope loopback, method GET, no actions.
         TICK the URL confirmation (required for any non-public source), then click Import ONCE (each click creates a new definition).
  [ ] 4. Repeat for every file; the list must then show each name exactly once.
  [ ] 5. Per board: edit mode > add widget > Custom widget > pick the definition; set the refresh and size above; save the board.
         (Homarr puts it in the first free spot of EVERY layout, which can be a hole above your tiles: drag it where you want it.)
  [ ] 6. Reload the board: no red triangle, no yellow 'template warnings', no 'migration required' / 'definition not found' tile.
  [ ] 7. Thermals / Load need the ops service routes /thermal and /load (curl http://127.0.0.1:9111/thermal must be 200).
Rollback: delete the definitions in Manage > Custom widgets (their tiles show 'definition not found' with a remove button) and remove the tiles in edit mode.""")
    return 0


# --------------------------------------------------------------------------- main
def make_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", nargs="?", type=Path, help="a COPY of the Homarr sqlite file (the live file needs --live)")
    ap.add_argument("--live", type=Path, metavar="PATH", help="the LIVE database; also needs --backup-dir (unless --dry-run)")
    ap.add_argument("--backup-dir", type=Path, help="NEW directory for the pre-change online backup + manifest (required with --live)")
    ap.add_argument("--dry-run", action="store_true", help="print the full plan, SQL and rollback SQL; write nothing (no backup, no manifest)")
    ap.add_argument("--rollback", type=Path, metavar="MANIFEST", help="remove exactly the rows recorded in this manifest (live: --live + --backup-dir)")
    ap.add_argument("--export-import-files", type=Path, metavar="DIR", help="write the transfer JSON files + print the in-app import checklist (no DB)")
    ap.add_argument("--boards", default="all", help="comma-separated board names or ids, 'all' (default) or 'none' (definitions only)")
    ap.add_argument("--widgets", default=",".join(ORDER), help="comma-separated stems (default: all seven)")
    ap.add_argument("--widgets-dir", type=Path, default=HERE, help="directory holding <stem>.json v2 definitions (default: widgets/)")
    ap.add_argument("--refresh", type=int, help="item refreshInterval seconds for every widget (default 30; 60 for thermals/load; 5..3600)")
    ap.add_argument("--size", action="append", default=[], metavar="[STEM=]WxH", help="footprint in tracks, repeatable (defaults: overview 3x2, others 3x3)")
    ap.add_argument("--update-existing", action="store_true", help="rewrite a same-named definition that differs from the shipped file (keeps its id and items; bumps updated_at)")
    ap.add_argument("--accept-existing", action="store_true",
                    help="place tiles on a same-named definition that DIFFERS from the shipped file or is disabled, unchanged (default: refuse, exit 2)")
    ap.add_argument("--creator-id", help="user id for new definitions (default: copied from the existing Thermals definition)")
    ap.add_argument("--full-sql", action="store_true", help="print long literals (templates) in full")
    ap.add_argument("--live-path", action="append", default=[], help="extra path that is the live DB (or set HOMARR_LIVE_DB)")
    ap.add_argument("--no-docker-check", action="store_true", help="do not ask docker where the homarr container keeps its database")
    ap.add_argument("--busy-timeout-ms", type=int, default=BUSY_MS, help=argparse.SUPPRESS)
    ap.add_argument("--no-harness", action="store_true", help="skip the optional real-runtime schema check (node + Homarr checkout)")
    ap.add_argument("--require-harness", action="store_true", help="fail when the real-runtime schema check cannot run")
    ap.add_argument("--homarr-fork", type=Path, help="Homarr checkout for the harness (default $HOMARR_REPO or ~/StudioProjects/homarr)")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = make_parser().parse_args(argv)
    try:
        stems = [s.strip() for s in a.widgets.split(",") if s.strip()]
        unknown = [s for s in stems if s not in ORDER]
        if unknown or not stems:
            raise Refuse(f"unknown widgets {unknown}; known: {ORDER}")
        a.size_map = {}
        for spec in a.size:
            stem, _, wh = spec.rpartition("=")
            if stem and stem not in ORDER:
                raise Refuse(f"--size: unknown widget {stem!r}")
            for s in ([stem] if stem else ORDER):
                a.size_map[s] = parse_size(wh)
        if a.refresh is not None and not 5 <= a.refresh <= 3600:
            raise Refuse("--refresh must be 5..3600 seconds (240 queries/min are shared by every viewer of a definition)")
        if not 0 < a.busy_timeout_ms <= 600000:
            raise Refuse("bad busy timeout")
        if a.rollback:                                     # needs no widget files: the manifest says what to remove
            return cmd_rollback(a, *resolve_target(a))
        widgets = load_widgets(a.widgets_dir, stems)
        if not a.no_harness and os.geteuid() == 0 and not a.require_harness:
            print("note: real-runtime check skipped as root (node would leave root-owned work files in /tmp); run --dry-run as your own user first", file=sys.stderr)
        elif not a.no_harness:
            verdict, detail = harness_check([w.path for w in widgets], a.homarr_fork, a.widgets_dir)
            if verdict == "fail":
                raise Refuse(f"the real-runtime schema check (app schema + analyzer) rejects:\n{detail}")
            if verdict == "skipped" and a.require_harness:
                raise Refuse(f"--require-harness: {detail}")
            if verdict == "skipped":
                print(f"note: real-runtime check skipped ({detail}); python-side shape checks passed", file=sys.stderr)
        if a.export_import_files:
            return cmd_export(a, widgets)
        path, live = resolve_target(a)
        o = Opts(boards=None if a.boards == "all" else ([] if a.boards == "none" else [b.strip() for b in a.boards.split(",") if b.strip()]),
                 sizes={**SIZES, **a.size_map}, refresh=a.refresh, update=a.update_existing, accept=a.accept_existing, creator=a.creator_id, now=int(time.time()))
        return cmd_install(a, path, live, widgets, o)
    except Refuse as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc} (if this happened before COMMIT nothing was changed)", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        msg = locked_msg(exc, a.busy_timeout_ms)
        print(f"{'refused' if msg else 'sqlite error'}: {msg or exc}" + ("" if msg else " (a failed write was rolled back; nothing was changed)"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
