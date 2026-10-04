"""Hermetic tests for widgets/install_homarr_widgets.py (Homarr v2 tables).

The database is built from tests/fixtures/homarr-v2-schema.sql (the REAL DDL of the v2 tables, extracted from a read-only copy of
the live Homarr database) plus synthetic boards shaped like the four real ones (one empty section, Base 10-column + Mobile 3-column
layouts, tiles with holes, Thermals / Fan Control customApi items, seed + legacy definitions). Nothing here touches the live database:
the optional rehearsal at the bottom takes a fresh READ-ONLY online-backup copy through `sudo -n` and skips cleanly when that is not
possible. Regenerate the schema fixture with:  python3 tests/test_homarr_installer_v2.py --regen-schema COPY.sqlite
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "homarr-v2-schema.sql"
_spec = importlib.util.spec_from_file_location("install_homarr_widgets", ROOT / "widgets" / "install_homarr_widgets.py")
inst = importlib.util.module_from_spec(_spec)
sys.modules["install_homarr_widgets"] = inst
_spec.loader.exec_module(inst)

USER = "zfsvodfcjkff593sn2plt7u9"
THERMALS = "nv6wesdh4b9o4krnjb6u1lgz"
FANCTL = "oeoh2bjwtpstoygwa225mxdf"
# name, Base bottom, Mobile bottom, items: the four real boards (docs/HOMARR_V2_WIDGETS.md 6.4)
BOARDS = [("Local-Big-Screen", 44, 76, 40), ("Local-Mobile", 177, 215, 37), ("Remote-Big-Screen", 19, 44, 40), ("Remote-Mobile", 144, 188, 37)]
NAMES = {"ops-overview": ("Ops Overview", "overview"), "ops-disk": ("Ops Disk", "disk"), "ops-jobs": ("Ops Jobs", "jobs"),
         "ops-guard": ("Ops Guard", "guard"), "ops-reclaim": ("Ops Reclaimed", "reclaim"), "ops-thermals": ("Ops Thermals 7d", "thermal"),
         "ops-load": ("Ops Load 7d", "load")}
WRITTEN = ("custom_widget_v2_definition", "item", "item_layout")
LIVE = Path(os.environ.get("HOMARR_LIVE_DB", "/data/compose/5/homarr/appdata/db/db.sqlite").split(":")[0])


# --------------------------------------------------------------------------- schema extraction (regeneration helper)
def extract_schema_sql(db: Path) -> str:
    """CREATE statements of the tables the installer touches plus the transitive closure of the tables they reference."""
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    seen: list[str] = []

    def visit(t):
        if t not in seen:
            seen.append(t)
            for r in c.execute(f"PRAGMA foreign_key_list('{t}')"):
                visit(r[2])

    for t in ("board", "section", "layout", "item", "item_layout", "section_layout", "custom_widget_v2_definition",
              "custom_widget_v2_secret", "user", "custom_widget_definition", "custom_widget_secret"):
        visit(t)
    body = []
    for t in seen:
        body.append(c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()[0] + ";")
        body += [r[0] + ";" for r in c.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (t,))]
    head = (f"-- Generated {time.strftime('%Y-%m-%d')}. Real DDL of the Homarr v2 tables that widgets/install_homarr_widgets.py reads or writes, plus every table they reference.\n"
            f"-- Extracted by tests/test_homarr_installer_v2.py --regen-schema from a read-only (sqlite online backup) copy of the live database,\n"
            f"-- tables: {', '.join(seen)}.\n-- Legacy tables custom_widget_definition / custom_widget_secret are kept: the installer must never write them.\n")
    return head + "\n".join(body) + "\n"


# --------------------------------------------------------------------------- synthetic database
def cid(rng: random.Random) -> str:
    return rng.choice("abcdefghijklmnopqrstuvwxyz") + "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(23))


def place(rng: random.Random, n: int, cols: int, bottom: int) -> list[tuple[int, int, int, int]]:
    """n placements with holes in a `cols` wide layout whose lowest edge is exactly `bottom` (the last one stretches down to it)."""
    cw = 2 if cols >= 10 else cols
    per_row = max(1, cols // cw)
    rows = -(-(n - 1) // per_row)
    ch = max(1, (bottom - 1) // rows)
    out = []
    for i in range(n - 1):
        r, s = divmod(i, per_row)
        out.append((s * cw, r * ch, rng.randint(1, cw), rng.randint(1, ch)))
    y = rows * ch
    assert y < bottom, (n, cols, bottom)
    out.append((0, y, cw, bottom - y))
    return out


def make_db(path: Path, boards=BOARDS, *, legacy=True, v2_defs=True, seed=7) -> Path:
    rng = random.Random(seed)
    c = sqlite3.connect(path)
    c.executescript(FIXTURE.read_text())
    c.execute('INSERT INTO "user" (id, name) VALUES (?, ?)', (USER, "ohmz_h"))
    for i, (did, name, creator) in enumerate([("seed-weather-outlook", "Weather Outlook", None), ("seed-dog-facts", "Random Dog Fact", None),
                                               (THERMALS, "Thermals", USER), (FANCTL, "Fan Control", USER)]):
        if not v2_defs:
            break
        src = '{"json":{"default":{"baseUrl":"http://127.0.0.1:9110","networkScope":"loopback","auth":"none"}}}'
        c.execute("INSERT INTO custom_widget_v2_definition (id,name,description,icon_url,sources,requests,options,template,enabled,created_at,updated_at,creator_id) "
                  "VALUES (?,?,NULL,NULL,?,?,?,?,1,1791004339,1791004339,?)",
                  (did, name, src, '{"json": {"state": {"kind": "query", "method": "GET", "path": "/", "source": "default", "trigger": "load", "cacheSeconds": 5}}}',
                   '{"json": {}}', "<Text>{data.state.head}</Text>", creator))
    if legacy:
        for did, name in [(THERMALS, "Thermals"), (FANCTL, "Fan Control"), ("legacyonly0000000000000001", "Legacy Only")]:
            c.execute("INSERT INTO custom_widget_definition (id,name,url,display_type,display_config,creator_id) VALUES (?,?,?,?,?,?)",
                      (did, name, "http://127.0.0.1:9110/", "customJsx", '{"json": {"type": "customJsx", "template": "<Text>x</Text>"}}', USER))
        c.execute("INSERT INTO custom_widget_secret (kind, value, updated_at, definition_id) VALUES ('header','aa.bb',1,?)", (FANCTL,))
    for name, base_bottom, mob_bottom, n in boards:
        bid, sec, lb, lm = cid(rng), cid(rng), cid(rng), cid(rng)
        c.execute("INSERT INTO board (id, name, is_public, creator_id) VALUES (?,?,0,?)", (bid, name, USER))
        c.execute("INSERT INTO section (id, board_id, kind, x_offset, y_offset) VALUES (?,?,'empty',0,0)", (sec, bid))
        c.execute("INSERT INTO layout (id,name,board_id,column_count,breakpoint,role) VALUES (?,?,?,10,768,'base')", (lb, "Base", bid))
        c.execute("INSERT INTO layout (id,name,board_id,column_count,breakpoint,role) VALUES (?,?,?,3,0,'mobile')", (lm, "Mobile", bid))
        ids = [cid(rng) for _ in range(n)]
        for k, iid in enumerate(ids):
            if k >= n - 2:
                did = (THERMALS, FANCTL)[k - (n - 2)]
                c.execute("INSERT INTO item (id, board_id, kind, options) VALUES (?,?,?,?)", (iid, bid, "customApi", f'{{"json": {{"definitionId": "{did}", "refreshInterval": 10}}}}'))
            else:
                c.execute("INSERT INTO item (id, board_id, kind) VALUES (?,?,?)", (iid, bid, "app"))
        for lay, cols, bottom in ((lb, 10, base_bottom), (lm, 3, mob_bottom)):
            for iid, (x, y, w, h) in zip(ids, place(rng, n, cols, bottom)):
                c.execute("INSERT INTO item_layout VALUES (?,?,?,?,?,?,?)", (iid, sec, lay, x, y, w, h))
    c.commit()
    c.close()
    return path


def snapshot(path: Path, skip=()) -> dict:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {t: sorted(c.execute(f'SELECT * FROM "{t}"').fetchall(), key=repr)
                for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'") if t not in skip}
    finally:
        c.close()


def fhash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(d: Path) -> list:
    return sorted((str(p.relative_to(d)), p.stat().st_size, p.stat().st_mtime_ns) for p in d.rglob("*"))


def write_defs(d: Path, stems=None, template=None) -> Path:
    """Seven synthetic but schema-valid v2 transfer files (names/paths of the real widgets)."""
    d.mkdir(parents=True, exist_ok=True)
    for stem, (name, route) in NAMES.items():
        if stems and stem not in stems:
            continue
        tpl = template or ("<Stack gap={7} p={2}>\n" + '<Text fz={11} fw={800} tt="uppercase">' + name + "</Text>\n"
                           '{status.state?.ok===false&&<Text fz={11} c="red.5">{"ops service: "+(status.state.error||"no response")}</Text>}\n'
                           '{(data.state.tiles||[]).map((x,i)=><Text key={i} fz={10}>{x.l}</Text>)}\n</Stack>')
        (d / f"{stem}.json").write_text(json.dumps({
            "$schema": "homarr-custom-widget-v2", "name": name, "description": f"{name} widget (test fixture)",
            "sources": {"default": {"baseUrl": "http://127.0.0.1:9111", "networkScope": "loopback", "auth": "none"}},
            "requests": {"state": {"kind": "query", "method": "GET", "path": "/" + route, "source": "default", "trigger": "load", "cacheSeconds": 5}},
            "options": {}, "template": tpl}, indent=1, ensure_ascii=False) + "\n")
    return d


class Env:
    def __init__(self, tmp: Path, template: Path):
        self.tmp = tmp
        self.db = tmp / "copy.sqlite"
        shutil.copyfile(template, self.db)
        self.defs = write_defs(tmp / "defs")
        self.n = 0

    def run(self, *args, db=True, harness=False):
        argv = ([str(self.db)] if db is True else [str(db)] if db else []) + list(args)
        argv += ["--widgets-dir", str(self.defs), "--no-docker-check"] + ([] if harness else ["--no-harness"])
        return inst.main(argv)

    def bk(self) -> Path:                                  # a fresh NEW backup dir name each call
        self.n += 1
        return self.tmp / f"backup-{self.n}"


@pytest.fixture(scope="session")
def template_db(tmp_path_factory):
    return make_db(tmp_path_factory.mktemp("template") / "template.sqlite")        # built once, copied per test


@pytest.fixture
def env(tmp_path, template_db):
    return Env(tmp_path, template_db)


def rows(db: Path, sql: str, *p):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, p).fetchall()
    finally:
        c.close()


def installed(db: Path):
    """(definitions by name, new items) relative to the synthetic base: everything whose definition name starts with 'Ops '."""
    defs = {r["name"]: r for r in rows(db, "SELECT * FROM custom_widget_v2_definition WHERE name LIKE 'Ops %'")}
    by_id = {r["id"]: r["name"] for r in defs.values()}
    items = [r for r in rows(db, "SELECT i.*, json_extract(i.options,'$.json.definitionId') did FROM item i WHERE kind='customApi'") if r["did"] in by_id]
    return defs, items


def overlaps(a, b) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


# --------------------------------------------------------------------------- dry run
def test_dry_run_writes_nothing(env, capsys):
    """No bytes, no backup, no manifest, no journal; prints every definition, item and layout row plus the rollback SQL."""
    before, files, snap = fhash(env.db), tree(env.tmp), snapshot(env.db)
    assert env.run("--dry-run") == 0
    out = capsys.readouterr().out
    assert fhash(env.db) == before and tree(env.tmp) == files and snapshot(env.db) == snap       # no bytes, no backup, no manifest, no journal
    assert "DRY RUN" in out and out.count("INSERT INTO custom_widget_v2_definition") == 7
    assert out.count("INSERT INTO item (") == 28 and out.count("INSERT INTO item_layout") == 56
    assert "BEGIN IMMEDIATE;" in out and out.count("COMMIT;") == 2
    # the rollback SQL is printed, children first
    rb = out[out.index("-- rollback"):]
    assert rb.index("DELETE FROM item_layout") < rb.index("DELETE FROM item WHERE") < rb.index("DELETE FROM custom_widget_v2_definition")
    assert "custom_widget_definition " not in out.replace("custom_widget_v2_definition", "")        # legacy table never mentioned in SQL


def test_dry_run_with_live_flag_needs_no_backup_dir_and_writes_nothing(env, capsys):
    before = fhash(env.db)
    assert inst.main(["--live", str(env.db), "--dry-run", "--widgets-dir", str(env.defs), "--no-docker-check", "--no-harness"]) == 0
    assert fhash(env.db) == before and "(LIVE)" in capsys.readouterr().out


def test_dry_run_shortens_templates_unless_full_sql(env, capsys):
    long_tpl = "<Stack>" + "<Text>x</Text>" * 200 + "</Stack>"
    write_defs(env.defs, template=long_tpl)
    env.run("--dry-run")
    assert "chars>" in capsys.readouterr().out
    env.run("--dry-run", "--full-sql")
    assert long_tpl in capsys.readouterr().out


# --------------------------------------------------------------------------- install: exact rows
def test_install_inserts_exactly_the_expected_rows(env, capsys):
    pre = snapshot(env.db)
    t0 = int(time.time())
    assert env.run("--backup-dir", str(env.bk())) == 0
    t1 = int(time.time())
    post = snapshot(env.db)
    # nothing but the three written tables changed; legacy tables and everything else are byte-identical row sets
    assert {t for t in post if post[t] != pre[t]} == set(WRITTEN)
    assert len(post["custom_widget_v2_definition"]) == len(pre["custom_widget_v2_definition"]) + 7
    assert len(post["item"]) == len(pre["item"]) + 28 and len(post["item_layout"]) == len(pre["item_layout"]) + 56
    assert all(r in post["item"] for r in pre["item"]) and all(r in post["item_layout"] for r in pre["item_layout"])    # no old row touched
    defs, items = installed(env.db)
    assert set(defs) == {n for n, _ in NAMES.values()}
    for stem, (name, route) in NAMES.items():
        r = defs[name]
        assert re.fullmatch(r"[a-z][a-z0-9]{23}", r["id"]) and not r["id"].startswith("seed-")
        assert r["creator_id"] == USER and r["enabled"] == 1 and isinstance(r["created_at"], int)
        assert t0 <= r["created_at"] == r["updated_at"] <= t1 and r["created_at"] < 10**10            # Unix SECONDS, not ms
        assert json.loads(r["sources"]) == {"json": {"default": {"baseUrl": "http://127.0.0.1:9111", "networkScope": "loopback", "auth": "none"}}}
        assert json.loads(r["requests"]) == {"json": {"state": {"source": "default", "kind": "query", "method": "GET", "path": "/" + route,
                                                                "trigger": "load", "auth": "inherit", "cacheSeconds": 5, "permission": "view"}}}
        assert json.loads(r["options"]) == {"json": {}} and r["icon_url"] is None
        assert r["template"] == json.loads((env.defs / f"{stem}.json").read_text())["template"]
    assert len(items) == 28
    for it in items:
        assert it["advanced_options"] == '{"json":{"title":null,"customCssClasses":[],"borderColor":""}}' and re.fullmatch(r"[a-z][a-z0-9]{23}", it["id"])
        o = json.loads(it["options"])["json"]
        assert set(o) == {"definitionId", "refreshInterval"} and o["refreshInterval"] == (60 if it["did"] in (defs["Ops Thermals 7d"]["id"], defs["Ops Load 7d"]["id"]) else 30)
        assert len(rows(env.db, "SELECT 1 FROM item_layout WHERE item_id=?", it["id"])) == 2                # one row per layout
    ids = [r["id"] for r in defs.values()] + [i["id"] for i in items]
    assert len(set(ids)) == len(ids)
    assert sqlite3.connect(env.db).execute("PRAGMA foreign_key_check").fetchall() == []
    assert sqlite3.connect(env.db).execute("PRAGMA quick_check").fetchone() == ("ok",)
    assert "reload the board page" in capsys.readouterr().out.lower()


def test_ids_follow_the_app_shape_and_are_never_seed_prefixed():
    ids = {inst.new_id() for _ in range(2000)}
    assert len(ids) == 2000 and all(re.fullmatch(r"[a-z][a-z0-9]{23}", i) and not i.startswith("seed-") for i in ids)


def test_positions_append_below_the_bottom_on_every_layout_like_policy_b(env):
    assert env.run() == 0
    defs, items = installed(env.db)
    by_name = {v["id"]: k for k, v in defs.items()}
    got = {}
    for it in items:
        for lr in rows(env.db, "SELECT l.name lname, il.* FROM item_layout il JOIN layout l ON l.id=il.layout_id WHERE il.item_id=?", it["id"]):
            b = rows(env.db, "SELECT name FROM board WHERE id=?", it["board_id"])[0]["name"]
            got[(b, lr["lname"], by_name[it["did"]])] = (lr["x_offset"], lr["y_offset"], lr["width"], lr["height"])
    order = [NAMES[s][0] for s in inst.ORDER]
    for name, base_b, mob_b, _n in BOARDS:
        # spec 6.4: Base wraps at 3 per row (3x2 / 3x3, row height = tallest), Mobile is one tile per row
        y, exp = base_b, []
        for i, w in enumerate(order):
            h = 2 if i == 0 else 3
            if i % 3 == 0 and i:
                y += 3
            exp.append((i % 3 * 3, y, 3, h))
        assert [got[(name, "Base", w)] for w in order] == exp
        y, exp = mob_b, []
        for i, w in enumerate(order):
            h = 2 if i == 0 else 3
            exp.append((0, y, 3, h))
            y += h
        assert [got[(name, "Mobile", w)] for w in order] == exp
    assert got[("Local-Big-Screen", "Base", "Ops Overview")] == (0, 44, 3, 2)                 # the numbers quoted in the spec
    assert got[("Local-Big-Screen", "Mobile", "Ops Load 7d")] == (0, 93, 3, 3)


def test_new_items_never_overlap_existing_ones_even_with_containers_and_odd_layouts(env):
    c = sqlite3.connect(env.db)
    bid, sec = c.execute("SELECT b.id, s.id FROM board b JOIN section s ON s.board_id=b.id WHERE b.name='Remote-Big-Screen'").fetchone()
    base = c.execute("SELECT id FROM layout WHERE board_id=? AND role='base'", (bid,)).fetchone()[0]
    c.execute("INSERT INTO section (id, board_id, kind, x_offset, y_offset) VALUES ('containersec000000000000001', ?, 'container', 0, 0)", (bid,))
    c.execute("INSERT INTO section_layout VALUES ('containersec000000000000001', ?, ?, 0, 30, 4, 6)", (base, sec))        # container reaching y=36 > items' 19
    c.execute("INSERT INTO layout (id,name,board_id,column_count,breakpoint,role) VALUES ('customlayout0000000000001','Wide',?,12,1400,'custom')", (bid,))
    c.execute("INSERT INTO item_layout VALUES ((SELECT id FROM item WHERE board_id=? LIMIT 1), ?, 'customlayout0000000000001', 0, 0, 12, 7)", (bid, sec))
    c.commit()
    c.close()
    assert env.run() == 0
    c = sqlite3.connect(env.db)
    for lay, cols in c.execute("SELECT id, column_count FROM layout"):
        board = c.execute("SELECT board_id FROM layout WHERE id=?", (lay,)).fetchone()[0]
        sec = c.execute("SELECT id FROM section WHERE board_id=? AND kind='empty'", (board,)).fetchone()[0]
        old = [r for r in c.execute("SELECT x_offset,y_offset,width,height,item_id FROM item_layout WHERE layout_id=? AND section_id=? "
                                    "AND item_id NOT IN (SELECT id FROM item WHERE json_extract(options,'$.json.definitionId') IN (SELECT id FROM custom_widget_v2_definition WHERE name LIKE 'Ops %'))", (lay, sec))]
        new = [r for r in c.execute("SELECT x_offset,y_offset,width,height,item_id FROM item_layout WHERE layout_id=? AND section_id=? "
                                    "AND item_id IN (SELECT id FROM item WHERE json_extract(options,'$.json.definitionId') IN (SELECT id FROM custom_widget_v2_definition WHERE name LIKE 'Ops %'))", (lay, sec))]
        cont = [r for r in c.execute("SELECT x_offset,y_offset,width,height FROM section_layout WHERE layout_id=? AND parent_section_id=?", (lay, sec))]
        if not new:
            continue
        assert len(new) == 7
        bottom = max([r[1] + r[3] for r in old] + [r[1] + r[3] for r in cont] + [0])
        for n in new:
            assert n[1] >= bottom and n[0] + n[2] <= cols                                       # appended below everything, inside the lane
            assert not any(overlaps(n, o) for o in old + cont)
        assert not any(overlaps(a, b) for i, a in enumerate(new) for b in new[i + 1:])
    # the 12-column custom layout got a row for every new item too (one item_layout row per layout)
    assert c.execute("SELECT count(*) FROM item_layout WHERE layout_id='customlayout0000000000001'").fetchone()[0] == 1 + 7


def test_gutters_reduce_the_main_lane_and_mobile_ignores_them(env):
    c = sqlite3.connect(env.db)
    c.execute("UPDATE layout SET left_gutter_column_count=2, right_gutter_column_count=1 WHERE role='base' AND board_id=(SELECT id FROM board WHERE name='Local-Mobile')")
    c.execute("UPDATE layout SET left_gutter_column_count=1 WHERE role='mobile' AND board_id=(SELECT id FROM board WHERE name='Local-Mobile')")
    c.commit()
    c.close()
    assert env.run("--boards", "Local-Mobile", "--size", "4x2") == 0                            # 10-3 = 7 usable: one 4-wide tile per row
    _defs, items = installed(env.db)
    base = rows(env.db, "SELECT il.* FROM item_layout il JOIN layout l ON l.id=il.layout_id WHERE l.role='base' AND il.item_id IN (%s) ORDER BY y_offset, x_offset" % ",".join("?" * 7), *[i["id"] for i in items])
    assert [(r["x_offset"], r["y_offset"] - 177) for r in base] == [(0, 2 * k) for k in range(7)]
    mob = rows(env.db, "SELECT il.* FROM item_layout il JOIN layout l ON l.id=il.layout_id WHERE l.role='mobile' AND il.item_id IN (%s)" % ",".join("?" * 7), *[i["id"] for i in items])
    assert {r["width"] for r in mob} == {3}                                                       # capped to the 3 mobile columns


def test_boards_widgets_refresh_and_size_filters(env, capsys):
    assert env.run("--boards", "Remote-Big-Screen,Local-Mobile", "--widgets", "ops-overview,ops-load", "--refresh", "45", "--size", "ops-load=5x4") == 0
    defs, items = installed(env.db)
    assert set(defs) == {"Ops Overview", "Ops Load 7d"} and len(items) == 4
    assert {r["name"] for r in rows(env.db, "SELECT b.name FROM item i JOIN board b ON b.id=i.board_id WHERE i.id IN (%s)" % ",".join("?" * 4), *[i["id"] for i in items])} == {"Remote-Big-Screen", "Local-Mobile"}
    assert {json.loads(i["options"])["json"]["refreshInterval"] for i in items} == {45}
    load = defs["Ops Load 7d"]["id"]
    sizes = {(r["width"], r["height"]) for r in rows(env.db, "SELECT il.width, il.height FROM item_layout il JOIN item i ON i.id=il.item_id WHERE json_extract(i.options,'$.json.definitionId')=?", load)}
    assert sizes == {(5, 4), (3, 4)}                                                              # Base keeps 5x4, Mobile is capped to 3 columns
    # board ids work too, and --boards none installs definitions only
    assert env.run("--boards", "none", "--widgets", "ops-disk") == 0
    assert "Ops Disk" in installed(env.db)[0] and len(installed(env.db)[1]) == 4


def test_boards_filter_rejects_unknown_board_and_bad_sizes_and_refresh(env, capsys):
    pristine = snapshot(env.db)
    assert env.run("--boards", "Nope") == 2 and "Local-Big-Screen" in capsys.readouterr().err
    assert env.run("--size", "0x3") == 2 and env.run("--size", "25x3") == 2 and env.run("--size", "ops-nope=3x3") == 2
    assert env.run("--refresh", "1") == 2 and env.run("--refresh", "99999") == 2
    assert env.run("--widgets", "ops-nope") == 2
    assert snapshot(env.db) == pristine


# --------------------------------------------------------------------------- idempotence and updates
def test_second_run_is_a_noop_without_backup_or_write(env, capsys):
    assert env.run("--backup-dir", str(env.bk())) == 0
    h, files = fhash(env.db), tree(env.tmp)
    b2 = env.bk()
    assert env.run("--backup-dir", str(b2)) == 0
    out = capsys.readouterr().out
    assert "nothing to do" in out and fhash(env.db) == h and tree(env.tmp) == files and not b2.exists()


def test_idempotent_by_existing_items_for_the_same_definition(env, capsys):
    assert env.run("--boards", "Local-Big-Screen", "--widgets", "ops-overview") == 0
    n = len(rows(env.db, "SELECT 1 FROM item"))
    assert env.run("--boards", "all", "--widgets", "ops-overview,ops-disk") == 0                 # overview exists on one board: only 3 + 4 new items
    out = capsys.readouterr().out
    assert "already placed on this board" in out
    assert len(rows(env.db, "SELECT 1 FROM item")) == n + 3 + 4
    assert len([i for i in installed(env.db)[1] if i["board_id"] == rows(env.db, "SELECT id FROM board WHERE name='Local-Big-Screen'")[0]["id"]]) == 2


def test_definition_present_but_never_placed_gets_items_without_a_second_definition(env):
    assert env.run("--boards", "none") == 0
    defs = installed(env.db)[0]
    assert len(defs) == 7 and not installed(env.db)[1]
    assert env.run() == 0
    assert installed(env.db)[0] == defs and len(installed(env.db)[1]) == 28


def test_update_existing_is_opt_in_bumps_updated_at_and_keeps_id_and_items(env, capsys):
    assert env.run() == 0
    defs, items = installed(env.db)
    old = defs["Ops Disk"]
    write_defs(env.defs, stems=["ops-disk"], template='<Stack p={2}><Text>{data.state.changed}</Text></Stack>')
    snap = snapshot(env.db)
    assert env.run() == 2                                                                          # without the flag: refused (see the DIFFERS tests below), not changed
    cap = capsys.readouterr()
    assert "DIFFERS" in cap.out and "--update-existing" in cap.err and snapshot(env.db) == snap
    time.sleep(0.01)
    assert env.run("--update-existing", "--backup-dir", str(env.bk())) == 0
    new = installed(env.db)[0]["Ops Disk"]
    assert new["id"] == old["id"] and new["created_at"] == old["created_at"] and new["updated_at"] > old["updated_at"]
    assert "changed" in new["template"] and installed(env.db)[1] == items
    assert {k: v for k, v in installed(env.db)[0].items() if k != "Ops Disk"} == {k: v for k, v in defs.items() if k != "Ops Disk"}
    assert env.run("--update-existing") == 0 and "nothing to do" in capsys.readouterr().out      # now identical: no-op


def test_unchanged_comparison_tolerates_the_apps_own_formatting(env, capsys):
    """A row the app wrote (spaces after ':' and the minimal request form) counts as unchanged, not as 'differs'."""
    assert env.run("--boards", "none", "--widgets", "ops-overview") == 0
    c = sqlite3.connect(env.db)
    c.execute("UPDATE custom_widget_v2_definition SET sources='{\"json\": {\"default\": {\"baseUrl\": \"http://127.0.0.1:9111\", \"networkScope\": \"loopback\", \"auth\": \"none\"}}}', "
              "requests='{\"json\": {\"state\": {\"kind\": \"query\", \"method\": \"GET\", \"path\": \"/overview\", \"source\": \"default\", \"trigger\": \"load\", \"cacheSeconds\": 5}}}' WHERE name='Ops Overview'")
    c.commit()
    c.close()
    capsys.readouterr()
    assert env.run("--boards", "none", "--widgets", "ops-overview", "--update-existing") == 0
    assert "unchanged" in capsys.readouterr().out


FOREIGN = ("INSERT INTO custom_widget_v2_definition (id,name,description,sources,requests,options,template,enabled,created_at,updated_at,creator_id) "
           "VALUES ('foreigndisk000000000000a', 'Ops Disk', 'imported by hand from an older build', "
           "'{\"json\":{\"default\":{\"baseUrl\":\"https://example.com\",\"networkScope\":\"public\",\"auth\":\"none\"}}}', "
           "'{\"json\":{\"state\":{\"source\":\"default\",\"kind\":\"query\",\"method\":\"GET\",\"path\":\"/old\",\"trigger\":\"load\",\"auth\":\"inherit\",\"permission\":\"view\"}}}', "
           "'{\"json\":{}}', '<Text>mine</Text>', ?, 1, 1, ?)")


def with_foreign_disk(env, enabled=1):
    c = sqlite3.connect(env.db)
    c.execute(FOREIGN, (enabled, USER))
    c.commit()
    c.close()
    return snapshot(env.db)


@pytest.mark.parametrize("flags", [["--dry-run"], ["--backup-dir", "NEW"], ["--dry-run", "--widgets", "ops-disk"], ["--boards", "none", "--backup-dir", "NEW"]])
def test_a_same_named_definition_that_differs_is_refused_and_nothing_is_placed_on_it(env, capsys, flags):
    """A user-authored / stale 'Ops Disk' must not silently get four tiles and a success exit: refuse, write nothing, say what to do."""
    snap = with_foreign_disk(env)
    files = tree(env.tmp)
    argv = [str(env.bk()) if f == "NEW" else f for f in flags]
    assert env.run(*argv) == 2
    cap = capsys.readouterr()
    assert "foreigndisk000000000000a" in cap.err and "differs" in cap.err and "--update-existing" in cap.err and "--accept-existing" in cap.err
    assert "DIFFERS" in cap.out and "INSERT INTO item" not in cap.out and ("Ops Disk: NOT placed" in cap.out or "--boards" in flags)       # the plan does not pretend to place anything
    assert snapshot(env.db) == snap and not [f for f in tree(env.tmp) if f not in files]                         # no row, no backup, no manifest


def test_accept_existing_places_tiles_on_the_foreign_definition_and_leaves_it_alone(env, capsys):
    snap = with_foreign_disk(env)
    assert env.run("--accept-existing", "--backup-dir", str(env.bk())) == 0
    out = capsys.readouterr().out
    assert "kept as it is (--accept-existing)" in out
    c = sqlite3.connect(env.db)
    assert c.execute("SELECT template, sources FROM custom_widget_v2_definition WHERE id='foreigndisk000000000000a'").fetchone() == (
        "<Text>mine</Text>", snap["custom_widget_v2_definition"][[r[0] for r in snap["custom_widget_v2_definition"]].index("foreigndisk000000000000a")][4])
    assert c.execute("SELECT count(*) FROM item WHERE json_extract(options,'$.json.definitionId')='foreigndisk000000000000a'").fetchone()[0] == 4
    assert c.execute("SELECT count(*) FROM custom_widget_v2_definition WHERE name='Ops Disk'").fetchone()[0] == 1          # no second definition
    c.close()
    assert len(installed(env.db)[0]) == 7


def test_update_existing_replaces_the_foreign_definition_so_the_tiles_show_our_template(env):
    with_foreign_disk(env)
    assert env.run("--update-existing", "--backup-dir", str(env.bk())) == 0
    row = installed(env.db)[0]["Ops Disk"]
    assert row["id"] == "foreigndisk000000000000a" and "mine" not in row["template"] and "https://example.com" not in row["sources"]
    assert len([i for i in installed(env.db)[1] if i["did"] == row["id"]]) == 4


def test_a_disabled_definition_is_refused_unless_accepted(env, capsys):
    """Tiles on a disabled definition say 'unavailable': the install must not report success for them (--update-existing does not enable it)."""
    assert env.run("--boards", "none", "--widgets", "ops-disk") == 0
    c = sqlite3.connect(env.db)
    c.execute("UPDATE custom_widget_v2_definition SET enabled=0 WHERE name='Ops Disk'")
    c.commit()
    c.close()
    snap = snapshot(env.db)
    capsys.readouterr()
    assert env.run("--widgets", "ops-disk") == 2 and "is disabled" in capsys.readouterr().err and snapshot(env.db) == snap
    assert env.run("--widgets", "ops-disk", "--update-existing") == 2 and snapshot(env.db) == snap
    assert env.run("--widgets", "ops-disk", "--accept-existing", "--dry-run") == 0 and "WARNING" in capsys.readouterr().out


def test_a_definition_edited_between_the_preview_and_the_write_lock_is_refused_not_overwritten_or_used(env, monkeypatch, capsys):
    real = inst.backup

    def backup_then_user_edits_the_template(src, dest):
        out = real(src, dest)
        c = sqlite3.connect(src)
        c.execute("UPDATE custom_widget_v2_definition SET template='<Text>edited meanwhile</Text>' WHERE name='Ops Disk'")
        c.commit()
        c.close()
        return out

    assert env.run("--boards", "none", "--widgets", "ops-disk") == 0                                  # identical definition exists, no tiles yet
    monkeypatch.setattr(inst, "backup", backup_then_user_edits_the_template)
    assert env.run("--widgets", "ops-disk", "--backup-dir", str(env.bk())) == 2
    assert "can no longer go ahead" in capsys.readouterr().err
    assert not installed(env.db)[1] and "edited meanwhile" in installed(env.db)[0]["Ops Disk"]["template"]


def test_the_preview_prints_no_ids_that_the_real_run_will_not_use(env, capsys):
    """APPLY re-plans under the write lock with fresh random ids; the preview block must not show ids a reader could copy (they would not exist)."""
    assert env.run("--boards", "Local-Big-Screen", "--backup-dir", str(env.bk())) == 0
    out = capsys.readouterr().out
    preview, applied = out.split("-- backup written and verified", 1)
    assert "preview: new ids and positions are decided again" in preview and "INSERT <new id>" in preview and "-> new item" in preview
    defs, items = installed(env.db)
    for r in [*defs.values(), *items]:                                                                 # every row really written is listed after the commit, none before
        assert r["id"] in applied and r["id"] not in preview


# --------------------------------------------------------------------------- manifest + rollback
def test_install_writes_a_manifest_next_to_the_backup_and_the_backup_is_the_pre_state(env):
    pre = snapshot(env.db)
    bk = env.bk()
    assert env.run("--backup-dir", str(bk)) == 0
    assert sorted(p.name for p in bk.iterdir()) == ["copy.sqlite", "ops-widgets-manifest.json"] and oct(bk.stat().st_mode & 0o777) == "0o700"
    assert snapshot(bk / "copy.sqlite") == pre                                                    # restorable pre-change state
    m = json.loads((bk / "ops-widgets-manifest.json").read_text())
    assert m["format"] == inst.MANIFEST_FORMAT and m["status"] == "committed" and m["backup"] == str(bk / "copy.sqlite")
    assert len(m["definitions"]) == 7 and len(m["items"]) == 28 and len(m["item_layouts"]) == 56
    assert {i["id"] for i in m["items"]} == {i["id"] for i in installed(env.db)[1]}


def test_rollback_removes_exactly_the_recorded_rows_and_restores_every_table(env, capsys):
    pre = snapshot(env.db)
    bk = env.bk()
    assert env.run("--backup-dir", str(bk)) == 0
    manifest = bk / "ops-widgets-manifest.json"
    assert snapshot(env.db) != pre
    assert env.run("--rollback", str(manifest), "--dry-run") == 0
    assert "DELETE FROM item_layout" in capsys.readouterr().out and snapshot(env.db) != pre       # dry-run rollback writes nothing
    assert env.run("--rollback", str(manifest)) == 0
    assert snapshot(env.db) == pre                                                                # every table, every row, identical to before
    assert json.loads(manifest.read_text())["status"] == "rolled_back"
    assert env.run("--rollback", str(manifest)) == 0 and "nothing to roll back" in capsys.readouterr().out
    assert env.run() == 0 and len(installed(env.db)[0]) == 7                                       # and it can be installed again


def test_rollback_keeps_rows_users_added_and_definitions_they_depend_on(env, capsys):
    bk = env.bk()
    assert env.run("--boards", "Local-Big-Screen", "--backup-dir", str(bk)) == 0
    defs, items = installed(env.db)
    c = sqlite3.connect(env.db)
    bid = c.execute("SELECT id FROM board WHERE name='Local-Mobile'").fetchone()[0]
    c.execute("INSERT INTO item (id, board_id, kind, options) VALUES ('useradded0000000000000001', ?, 'customApi', ?)", (bid, '{"json":{"definitionId":"%s","refreshInterval":30}}' % defs["Ops Jobs"]["id"]))
    c.execute("INSERT INTO custom_widget_v2_secret VALUES ('default','bearer','aa.bb',1,?)", (defs["Ops Guard"]["id"],))
    c.execute("UPDATE item SET options=json_set(options,'$.json.refreshInterval',99) WHERE id=?", (items[0]["id"],))          # user edited a tile: still ours, still removed
    c.commit()
    c.close()
    assert env.run("--rollback", str(bk / "ops-widgets-manifest.json")) == 0
    out = capsys.readouterr().out
    left_defs, left_items = installed(env.db)
    assert set(left_defs) == {"Ops Jobs", "Ops Guard"} and [i["id"] for i in left_items] == ["useradded0000000000000001"]
    assert "KEPT" in out and rows(env.db, "SELECT count(*) n FROM custom_widget_v2_secret WHERE definition_id=?", defs["Ops Guard"]["id"])[0]["n"] == 1
    assert sqlite3.connect(env.db).execute("PRAGMA foreign_key_check").fetchall() == [] and rows(env.db, "SELECT count(*) n FROM item_layout WHERE item_id NOT IN (SELECT id FROM item)")[0]["n"] == 0


def test_rollback_restores_updated_definitions_unless_edited_since(env, capsys):
    assert env.run() == 0
    orig = installed(env.db)[0]["Ops Disk"]
    write_defs(env.defs, stems=["ops-disk", "ops-jobs"], template="<Stack p={2}><Text>{data.state.v2}</Text></Stack>")
    bk = env.bk()
    assert env.run("--update-existing", "--backup-dir", str(bk)) == 0
    assert "v2" in installed(env.db)[0]["Ops Disk"]["template"]
    c = sqlite3.connect(env.db)
    c.execute("UPDATE custom_widget_v2_definition SET updated_at=updated_at+5 WHERE name='Ops Jobs'")            # user edited Jobs in the UI afterwards
    c.commit()
    c.close()
    assert env.run("--rollback", str(bk / "ops-widgets-manifest.json")) == 0
    out = capsys.readouterr().out
    restored = installed(env.db)[0]
    assert restored["Ops Disk"]["template"] == orig["template"] and restored["Ops Disk"]["updated_at"] == orig["updated_at"]
    assert "v2" in restored["Ops Jobs"]["template"] and "edited after the install" in out


def test_rollback_refuses_garbage_manifests_and_never_needs_widget_files(env, tmp_path, capsys):
    bad = tmp_path / "m.json"
    for text in ("{", "[]", json.dumps({"format": "other"}), json.dumps({"format": inst.MANIFEST_FORMAT, "definitions": "x"})):
        bad.write_text(text)
        assert env.run("--rollback", str(bad)) == 2
    assert "not a manifest" in capsys.readouterr().err
    bk = env.bk()
    assert env.run("--backup-dir", str(bk)) == 0
    empty = tmp_path / "nowidgets"
    empty.mkdir()
    assert inst.main([str(env.db), "--rollback", str(bk / "ops-widgets-manifest.json"), "--widgets-dir", str(empty), "--no-docker-check"]) == 0
    assert installed(env.db) == ({}, [])


def test_rollback_on_live_target_needs_live_flag_and_a_new_backup_dir(env, tmp_path):
    bk = env.bk()
    assert env.run("--backup-dir", str(bk)) == 0
    m = str(bk / "ops-widgets-manifest.json")
    assert inst.main(["--live", str(env.db), "--rollback", m, "--no-docker-check"]) == 2                       # --live without --backup-dir
    b2 = tmp_path / "backup-rollback"
    assert inst.main(["--live", str(env.db), "--rollback", m, "--backup-dir", str(b2), "--no-docker-check"]) == 0
    assert installed(env.db) == ({}, []) and sqlite3.connect(b2 / "copy.sqlite").execute("SELECT count(*) FROM custom_widget_v2_definition WHERE name LIKE 'Ops %'").fetchone() == (7,)
    assert json.loads((b2 / "ops-widgets-rollback.json").read_text())["manifest"] == m


# --------------------------------------------------------------------------- the legacy tables are never written
def test_legacy_tables_are_never_written_or_planned(env, monkeypatch, capsys):
    legacy_before = {t: snapshot(env.db)[t] for t in inst.LEGACY_TABLES}
    seen = []
    real = inst.sqlite3.connect

    def spy(*a, **k):
        c = real(*a, **k)
        c.set_trace_callback(seen.append)
        return c

    monkeypatch.setattr(inst.sqlite3, "connect", spy)
    bk = env.bk()
    assert env.run("--backup-dir", str(bk)) == 0
    assert env.run("--update-existing") == 0
    assert env.run("--rollback", str(bk / "ops-widgets-manifest.json")) == 0
    writes = [s for s in seen if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER)", s, re.I) and re.search(r"\bcustom_widget_(definition|secret)\b", s)]
    assert writes == []
    assert {t: snapshot(env.db)[t] for t in inst.LEGACY_TABLES} == legacy_before


# --------------------------------------------------------------------------- refusals
def test_refuses_a_v1_database_without_the_v2_table(env, capsys):
    db = make_db(env.tmp / "v1.sqlite", v2_defs=False)
    sqlite3.connect(db).executescript("DROP TABLE custom_widget_v2_secret; DROP TABLE custom_widget_v2_definition;")
    h = fhash(db)
    assert env.run(db=db) == 2 and "v1" in capsys.readouterr().err and fhash(db) == h
    assert env.run("--dry-run", db=db) == 2


def test_refuses_legacy_only_ids_and_unmigrated_v2(env, capsys):
    c = sqlite3.connect(env.db)
    b = c.execute("SELECT id FROM board LIMIT 1").fetchone()[0]
    c.execute("INSERT INTO item (id, board_id, kind, options) VALUES ('pointsatlegacy000000000001', ?, 'customApi', ?)", (b, '{"json":{"definitionId":"legacyonly0000000000000001","refreshInterval":30}}'))
    c.commit()
    c.close()
    h = fhash(env.db)
    assert env.run() == 2 and "legacy-only" in capsys.readouterr().err and fhash(env.db) == h
    db2 = make_db(env.tmp / "empty-v2.sqlite", v2_defs=False)              # legacy rows, empty v2 table: the migration never ran
    assert env.run(db=db2) == 2 and "migration" in capsys.readouterr().err


def test_a_legacy_row_with_the_same_id_as_a_v2_row_is_fine(env):
    assert env.run() == 0                                                  # synthetic base has Thermals / Fan Control in both tables (like the live DB)


@pytest.mark.parametrize("sidecar", ["-journal", "-wal"])
def test_in_flight_journal_aborts_install_and_rollback_but_dry_run_only_warns(env, capsys, sidecar):
    j = Path(str(env.db) + sidecar)
    j.write_bytes(b"\x00" * 512)
    h = fhash(env.db)
    assert env.run() == 2 and "in flight" in capsys.readouterr().err
    assert env.run("--backup-dir", str(env.bk())) == 2 and not (env.tmp / "backup-1").exists() and fhash(env.db) == h
    assert env.run("--dry-run") == 0 and "in flight" in capsys.readouterr().err
    j.write_bytes(b"")                                                     # an EMPTY journal is leftover noise, not a write in flight
    assert env.run() == 0


CRASH = ("import sqlite3, os, sys\n"
         "c = sqlite3.connect(sys.argv[1], isolation_level=None)\nc.execute('PRAGMA cache_size=1')\nc.execute('BEGIN')\n"
         "for i in range(3000): c.execute(\"INSERT INTO item (id, board_id, kind, options) VALUES (?, 'x', 'app', ?)\", ('z%d' % i, 'y' * 300))\n"
         "os._exit(0)\n")                                                  # dies mid-transaction: leaves a REAL hot rollback journal


def test_a_real_hot_journal_from_a_crashed_writer_is_refused_everywhere_and_left_alone(env, capsys):
    subprocess.run([sys.executable, "-c", CRASH, str(env.db)], check=True, timeout=60)
    j = Path(str(env.db) + "-journal")
    assert j.exists() and j.stat().st_size > 512
    h, jh = fhash(env.db), fhash(j)
    assert env.run() == 2 and env.run("--backup-dir", str(env.bk())) == 2 and env.run("--dry-run") == 2
    assert env.run("--rollback", str(env.tmp / "nope.json")) == 2
    err = capsys.readouterr().err
    assert err.count("in flight") >= 3 and fhash(env.db) == h and fhash(j) == jh and not (env.tmp / "backup-1").exists()      # not even recovered by us


def test_in_flight_journal_is_rechecked_after_the_backup(env, monkeypatch):
    real = inst.backup

    def backup_then_crash_appears(src, dest):
        out = real(src, dest)
        Path(str(src) + "-journal").write_bytes(b"x" * 100)               # a writer starts while we back up
        return out

    monkeypatch.setattr(inst, "backup", backup_then_crash_appears)
    h = fhash(env.db)
    assert env.run("--backup-dir", str(env.bk())) == 2 and fhash(env.db) == h


def test_refuses_boards_without_exactly_one_empty_main_section(env, capsys):
    c = sqlite3.connect(env.db)
    bid = c.execute("SELECT id FROM board WHERE name='Local-Mobile'").fetchone()[0]
    c.execute("INSERT INTO section (id, board_id, kind, x_offset, y_offset) VALUES ('secondempty00000000000001', ?, 'empty', 0, 5)", (bid,))
    c.commit()
    c.close()
    snap = snapshot(env.db)
    assert env.run() == 2 and "Local-Mobile" in capsys.readouterr().err and snapshot(env.db) == snap       # 'all' refuses as a whole: nothing half-installed
    assert env.run("--boards", "Local-Big-Screen") == 0                                              # an explicit filter that skips the odd board works
    c = sqlite3.connect(env.db)
    c.execute("DELETE FROM section WHERE id='secondempty00000000000001'")
    c.execute("DELETE FROM section WHERE board_id=? AND kind='empty'", (bid,))
    c.commit()
    c.close()
    assert env.run("--boards", "Local-Mobile") == 2                                                  # zero empty sections


def test_gutter_lane_sections_do_not_count_as_the_main_section(env):
    c = sqlite3.connect(env.db)
    bid = c.execute("SELECT id FROM board WHERE name='Local-Mobile'").fetchone()[0]
    c.execute("INSERT INTO section (id, board_id, kind, x_offset, y_offset) VALUES ('leftlane0000000000000001', ?, 'empty', -1, 0)", (bid,))
    c.commit()
    c.close()
    assert env.run("--boards", "Local-Mobile") == 0


@pytest.mark.parametrize("break_it", ["no_mobile", "two_base", "no_base"])
def test_refuses_boards_without_one_base_and_one_mobile_layout(env, capsys, break_it):
    c = sqlite3.connect(env.db)
    bid = c.execute("SELECT id FROM board WHERE name='Remote-Mobile'").fetchone()[0]
    if break_it == "no_mobile":
        c.execute("UPDATE layout SET role='custom' WHERE board_id=? AND role='mobile'", (bid,))
    elif break_it == "no_base":
        c.execute("UPDATE layout SET role='custom' WHERE board_id=? AND role='base'", (bid,))
    else:
        c.execute("INSERT INTO layout (id,name,board_id,column_count,breakpoint,role) VALUES ('secondbase00000000000001','B2',?,10,900,'base')", (bid,))
    c.commit()
    c.close()
    snap = snapshot(env.db)
    assert env.run("--boards", "Remote-Mobile") == 2 and "Base and one Mobile" in capsys.readouterr().err and snapshot(env.db) == snap


def test_refuses_duplicate_definition_names_and_unknown_creator(env, capsys):
    c = sqlite3.connect(env.db)
    for i in (1, 2):
        c.execute("INSERT INTO custom_widget_v2_definition (id,name,sources,requests,options,template) VALUES (?, 'Ops Disk', '{}', '{}', '{}', 'x')", (f"dupdup{i}0000000000000000000",))
    c.commit()
    c.close()
    snap = snapshot(env.db)
    assert env.run() == 2 and "2 definitions are named 'Ops Disk'" in capsys.readouterr().err and snapshot(env.db) == snap
    assert env.run("--widgets", "ops-overview", "--creator-id", "nobody") == 2 and "not a user" in capsys.readouterr().err
    assert env.run("--widgets", "ops-overview", "--boards", "none", "--creator-id", USER) == 0


def test_creator_comes_from_the_thermals_row_else_any_non_seed_row_else_null(env):
    c = sqlite3.connect(env.db)
    c.execute('INSERT INTO "user" (id, name) VALUES (\'otheruser00000000000000001\', \'other\')')
    c.execute("UPDATE custom_widget_v2_definition SET creator_id='otheruser00000000000000001' WHERE id=?", (FANCTL,))
    c.commit()
    c.close()
    assert env.run("--boards", "none", "--widgets", "ops-overview") == 0
    assert installed(env.db)[0]["Ops Overview"]["creator_id"] == USER                                # Thermals wins over Fan Control
    c = sqlite3.connect(env.db)
    c.execute("DELETE FROM custom_widget_v2_definition WHERE name='Ops Overview'")
    c.execute("UPDATE custom_widget_v2_definition SET name='Thermals (renamed)' WHERE name='Thermals'")                # no 'Thermals' row any more
    c.commit()
    c.close()
    assert env.run("--boards", "none", "--widgets", "ops-overview") == 0
    assert installed(env.db)[0]["Ops Overview"]["creator_id"] == USER                                # fallback: oldest non-seed row with a creator
    c = sqlite3.connect(env.db)
    c.execute("DELETE FROM custom_widget_v2_definition WHERE name='Ops Overview'")
    c.execute("UPDATE custom_widget_v2_definition SET creator_id=NULL")
    c.commit()
    c.close()
    assert env.run("--boards", "none", "--widgets", "ops-overview") == 0
    assert installed(env.db)[0]["Ops Overview"]["creator_id"] is None


def test_refuses_a_schema_it_does_not_know(env, capsys):
    c = sqlite3.connect(env.db)
    c.execute("ALTER TABLE item_layout ADD COLUMN z_index integer NOT NULL DEFAULT 0")                # defaulted: fine
    c.commit()
    assert env.run("--dry-run") == 0
    c.executescript("ALTER TABLE item ADD COLUMN tenant text NOT NULL DEFAULT 'a'; ALTER TABLE custom_widget_v2_definition RENAME COLUMN template TO body;")
    c.close()
    assert env.run("--dry-run") == 2 and "lacks columns" in capsys.readouterr().err


def test_live_gate(env, tmp_path, capsys, monkeypatch):
    # a positional path that looks like the live DB is refused, in every mode
    monkeypatch.setenv("HOMARR_LIVE_DB", str(env.db))
    h = fhash(env.db)
    assert env.run() == 2 and "LIVE database" in capsys.readouterr().err
    assert env.run("--dry-run") == 2
    # --live needs a backup dir (except for --dry-run) and cannot be combined with a positional database
    assert inst.main(["--live", str(env.db), "--widgets-dir", str(env.defs), "--no-docker-check", "--no-harness"]) == 2 and "--backup-dir" in capsys.readouterr().err
    assert inst.main([str(env.db), "--live", str(env.db), "--backup-dir", str(env.bk()), "--no-docker-check", "--no-harness", "--widgets-dir", str(env.defs)]) == 2
    assert inst.main(["--no-docker-check", "--no-harness", "--widgets-dir", str(env.defs)]) == 2
    assert inst.main([str(tmp_path / "missing.sqlite"), "--no-docker-check", "--no-harness", "--widgets-dir", str(env.defs)]) == 2
    assert fhash(env.db) == h
    # hint-by-path: the real data dir pattern, without opening anything
    for p in ("/data/compose/5/homarr/appdata/db/db.sqlite", "/x/appdata/db/db.sqlite"):
        assert any(rx.search(p) for rx in inst.LIVE_HINTS)
    # --live on a real file works (this is how the live run is invoked) and leaves backup + manifest behind
    monkeypatch.delenv("HOMARR_LIVE_DB")
    bk = env.bk()
    assert inst.main(["--live", str(env.db), "--backup-dir", str(bk), "--widgets-dir", str(env.defs), "--no-docker-check", "--no-harness"]) == 0
    assert (bk / "copy.sqlite").stat().st_size > 0 and (bk / "ops-widgets-manifest.json").is_file()


# --------------------------------------------------------------------------- backup verification
@pytest.mark.parametrize("how", ["empty", "garbage", "missing_tables", "raises"])
def test_backup_verification_failure_aborts_before_any_write(env, monkeypatch, capsys, how):
    def fake(src, dest):
        if how == "empty":
            dest.write_bytes(b"")
        elif how == "garbage":
            dest.write_bytes(b"this is not a database" * 400)
        elif how == "missing_tables":
            c = sqlite3.connect(dest)
            c.execute("CREATE TABLE unrelated (x)")
            c.commit()
            c.close()
        else:
            raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(inst, "_run_backup", fake)
    h, snap, bk = fhash(env.db), snapshot(env.db), env.bk()
    assert env.run("--backup-dir", str(bk)) == 2
    assert "backup verification failed" in capsys.readouterr().err
    assert fhash(env.db) == h and snapshot(env.db) == snap and not bk.exists()                      # no write, no stray half-backup, no manifest


def test_backup_dir_must_be_new_or_empty_and_is_never_overwritten(env, capsys):
    bk = env.bk()
    bk.mkdir()
    (bk / "db.sqlite").write_text("precious earlier backup")
    assert env.run("--backup-dir", str(bk)) == 2 and (bk / "db.sqlite").read_text() == "precious earlier backup"
    f = env.tmp / "a-file"
    f.write_text("x")
    assert env.run("--backup-dir", str(f)) == 2
    empty = env.bk()
    empty.mkdir()
    assert env.run("--backup-dir", str(empty)) == 0 and (empty / "copy.sqlite").is_file()


def test_backup_keeps_an_existing_empty_dir_mode_and_creates_new_ones_private(env):
    d = env.bk()
    d.mkdir(mode=0o755)
    d.chmod(0o755)
    assert env.run("--backup-dir", str(d), "--boards", "none") == 0
    assert oct(d.stat().st_mode & 0o777) == "0o755" and oct((d / "copy.sqlite").stat().st_mode & 0o777) == "0o600"
    nested = env.tmp / "a" / "b" / "backup-x"
    assert env.run("--backup-dir", str(nested), "--boards", "none", db=make_db(env.tmp / "second.sqlite")) == 0 and oct(nested.stat().st_mode & 0o777) == "0o700"


def test_backup_is_taken_while_another_connection_has_the_file_open(env):
    reader = sqlite3.connect(env.db)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM item").fetchone()
    bk = env.bk()
    try:
        inst.backup(env.db, bk)
    finally:
        reader.close()
    assert snapshot(bk / "copy.sqlite") == snapshot(env.db)


# --------------------------------------------------------------------------- FK / quick checks before COMMIT
def _inject(monkeypatch, sql, params=()):
    real = inst.build_plan

    def bad(conn, widgets, o):
        p = real(conn, widgets, o)
        p.stmts.append(inst.Stmt(sql, params, "injected"))
        return p

    monkeypatch.setattr(inst, "build_plan", bad)


def test_foreign_key_check_before_commit_rolls_everything_back(env, monkeypatch, capsys):
    monkeypatch.setattr(inst, "ENFORCE_FK", False)                                                   # so the insert itself succeeds: the CHECK must catch it
    _inject(monkeypatch, "INSERT INTO item_layout VALUES ('noitem', 'nosection', 'nolayout', 0, 0, 1, 1)")
    snap, bk = snapshot(env.db), env.bk()
    assert env.run("--backup-dir", str(bk)) == 2
    assert "foreign_key_check" in capsys.readouterr().err and snapshot(env.db) == snap
    assert json.loads((bk / "ops-widgets-manifest.json").read_text())["status"] == "aborted"        # the manifest never claims rows exist


def test_foreign_keys_are_enforced_on_the_write_connection_too(env, monkeypatch, capsys):
    _inject(monkeypatch, "INSERT INTO item_layout VALUES ('noitem', 'nosection', 'nolayout', 0, 0, 1, 1)")
    snap = snapshot(env.db)
    assert env.run() == 1 and "FOREIGN KEY" in capsys.readouterr().err.upper() and snapshot(env.db) == snap


def test_preexisting_fk_violations_do_not_block_but_new_ones_do(env, monkeypatch):
    c = sqlite3.connect(env.db)
    c.execute("INSERT INTO item_layout VALUES ('orphanitem000000000000001', 'nosection', 'nolayout', 0, 0, 1, 1)")        # an old orphan in a user's DB
    c.commit()
    c.close()
    assert env.run("--boards", "none", "--widgets", "ops-disk") == 0


def test_quick_check_failure_before_commit_rolls_back(env, monkeypatch, capsys):
    monkeypatch.setattr(inst, "quick_check", lambda conn: "row 3 missing from index sqlite_autoindex_item_1")
    snap = snapshot(env.db)
    assert env.run() == 2 and "quick_check" in capsys.readouterr().err and snapshot(env.db) == snap


def test_a_crash_inside_the_transaction_leaves_the_database_untouched(env, monkeypatch):
    real = inst.fk_violations
    calls = []

    def boom(conn):
        calls.append(1)
        if len(calls) == 2:                                                                          # after the inserts, before COMMIT
            raise KeyboardInterrupt
        return real(conn)

    monkeypatch.setattr(inst, "fk_violations", boom)
    snap = snapshot(env.db)
    with pytest.raises(KeyboardInterrupt):
        env.run()
    assert snapshot(env.db) == snap and not Path(str(env.db) + "-journal").exists()


# --------------------------------------------------------------------------- concurrent writer
def _hold_lock(path: Path, seconds: float, started: threading.Event):
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("BEGIN IMMEDIATE")
    started.set()
    time.sleep(seconds)
    c.execute("ROLLBACK")
    c.close()


def test_a_writer_that_holds_the_lock_makes_the_installer_give_up_cleanly(env, capsys):
    ev = threading.Event()
    t = threading.Thread(target=_hold_lock, args=(env.db, 2.0, ev))
    t.start()
    ev.wait(5)
    snap = snapshot(env.db)
    t0 = time.time()
    rc = env.run("--busy-timeout-ms", "300", "--backup-dir", str(env.bk()))
    waited = time.time() - t0
    t.join()
    assert rc == 1 and "locked" in capsys.readouterr().err and 0.25 < waited < 1.8
    assert snapshot(env.db) == snap and not Path(str(env.db) + "-journal").exists()
    assert env.run() == 0                                                                            # the lock is gone: a retry installs


def test_a_short_lived_writer_is_waited_for(env):
    ev = threading.Event()
    t = threading.Thread(target=_hold_lock, args=(env.db, 0.6, ev))
    t.start()
    ev.wait(5)
    assert env.run("--busy-timeout-ms", "10000") == 0
    t.join()
    assert len(installed(env.db)[1]) == 28


def test_plan_is_recomputed_under_the_write_lock_from_the_rows_as_they_are_then(env, monkeypatch):
    """Homarr moves/adds tiles between the preview and the write: positions must come from the later state."""
    real = inst.backup

    def backup_then_user_adds_a_tall_tile(src, dest):
        out = real(src, dest)
        c = sqlite3.connect(src)
        bid, sec, lay = c.execute("SELECT b.id, s.id, l.id FROM board b JOIN section s ON s.board_id=b.id JOIN layout l ON l.board_id=b.id WHERE b.name='Local-Big-Screen' AND l.role='base'").fetchone()
        c.execute("INSERT INTO item (id, board_id, kind) VALUES ('latetile0000000000000001', ?, 'app')", (bid,))
        c.execute("INSERT INTO item_layout VALUES ('latetile0000000000000001', ?, ?, 0, 60, 10, 5)", (sec, lay))
        c.commit()
        c.close()
        return out

    monkeypatch.setattr(inst, "backup", backup_then_user_adds_a_tall_tile)
    assert env.run("--boards", "Local-Big-Screen", "--backup-dir", str(env.bk())) == 0
    ys = [r["y_offset"] for r in rows(env.db, "SELECT il.y_offset FROM item_layout il JOIN layout l ON l.id=il.layout_id JOIN item i ON i.id=il.item_id "
                                          "WHERE l.role='base' AND il.item_id IN (SELECT id FROM item WHERE json_extract(options,'$.json.definitionId') IN (SELECT id FROM custom_widget_v2_definition WHERE name LIKE 'Ops %'))")]
    assert min(ys) == 65                                                                              # below the late tile (60+5), not below 44


# --------------------------------------------------------------------------- python-side definition checks
def good():
    return json.loads((write_defs(Path(os.environ.get("TMPDIR", "/tmp")) / "hm-good-defs", ["ops-disk"]) / "ops-disk.json").read_text())


def test_good_definition_passes_and_stored_columns_are_the_apps_parsed_form():
    d = good()
    assert inst.check_definition(d) == []
    c = inst.stored_columns(d)
    assert c["requests"]["state"] == {"source": "default", "kind": "query", "method": "GET", "path": "/disk", "trigger": "load", "auth": "inherit",
                                      "cacheSeconds": 5, "permission": "view"}
    assert c["sources"] == {"default": {"baseUrl": "http://127.0.0.1:9111", "networkScope": "loopback", "auth": "none"}}
    minimal = {**d, "requests": {"state": {"path": "/disk"}}, "sources": {"default": {"baseUrl": "http://127.0.0.1:9111", "networkScope": "loopback"}}}
    assert inst.check_definition(minimal) == [] and inst.stored_columns(minimal)["requests"]["state"]["trigger"] == "load"


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d.update(url="http://127.0.0.1:9111/disk", authType="none", method="GET", displayType="customJsx", displayConfig={}), "v1"),
    (lambda d: d.pop("sources"), "sources"),
    (lambda d: d["sources"].pop("default"), "default"),
    (lambda d: d["sources"]["default"].pop("networkScope"), "networkScope"),
    (lambda d: d["sources"]["default"].update(networkScope="internet"), "networkScope"),
    (lambda d: d["sources"]["default"].update(baseUrl="http://127.0.0.1:9111?x=1"), "baseUrl"),
    (lambda d: d["sources"]["default"].update(baseUrl="http://user:pw@127.0.0.1:9111"), "baseUrl"),
    (lambda d: d["sources"]["default"].update(baseUrl="ftp://127.0.0.1"), "baseUrl"),
    (lambda d: d["sources"]["default"].update(baseUrl="http://[::1"), "baseUrl"),
    (lambda d: d["sources"]["default"].update(auth="bearer"), "only auth 'none'"),
    (lambda d: d["sources"]["default"].update(auth={"type": "apiKeyHeader", "name": "X-Fan"}), "only auth 'none'"),
    (lambda d: d["requests"]["state"].update(kind="action"), "read-only"),
    (lambda d: d["requests"]["state"].update(kind="mutation"), "read-only"),
    (lambda d: d["requests"]["state"].update(method="POST"), "GET"),
    (lambda d: d["requests"]["state"].update(trigger="interval"), "trigger"),
    (lambda d: d["requests"]["state"].update(path="/disk?x=1"), "path"),
    (lambda d: d["requests"]["state"].update(path="//evil"), "path"),
    (lambda d: d["requests"]["state"].update(path="disk"), "path"),
    (lambda d: d["requests"]["state"].update(cacheSeconds=3601), "cacheSeconds"),
    (lambda d: d["requests"]["state"].update(cacheSeconds=1.5), "cacheSeconds"),
    (lambda d: d["requests"]["state"].update(cacheSeconds=True), "cacheSeconds"),
    (lambda d: d["requests"]["state"].update(headers={"X-A": "b"}), "unsupported keys"),
    (lambda d: d["requests"]["state"].update(source="other"), "unknown source"),
    (lambda d: d["requests"].update(**{"a.b": {"path": "/x"}}), "bad id"),
    (lambda d: d["requests"].update(**{f"r{i}": {"path": "/x"} for i in range(4)}), "load requests"),
    (lambda d: d.update(name=" padded "), "name"),
    (lambda d: d.update(name=""), "name"),
    (lambda d: d.update(description=None), "description"),
    (lambda d: d.update(description="x" * 513), "description"),
    (lambda d: d.update(iconUrl="javascript:alert(1)"), "iconUrl"),
    (lambda d: d.update(extra=1), "unknown top-level"),
    (lambda d: d.update(options=[]), "options"),
    (lambda d: d.update({"$schema": "homarr-custom-widget-v1"}), "$schema"),
    (lambda d: d.update(template=""), "template"),
    (lambda d: d.update(template="x" * 50001), "50000"),
    (lambda d: d.update(template="<Text>café</Text>"), "NFC"),
    (lambda d: d.update(template="<Text>a​b</Text>"), "U+200B"),
    (lambda d: d.update(template="<Text>{data.head}</Text>"), "data.head"),
    (lambda d: d.update(template="<Text>{status.nope.ok}</Text>"), "status.nope"),
    (lambda d: d.update(template="<Text>Token: {data.state.t}</Text>"), "credential"),
    (lambda d: d.update(template="<Text>{data.state.token==='x'?1:0}</Text>"), "credential"),
    (lambda d: d.update(template="<Text>password = {data.state.p}</Text>"), "credential"),
    (lambda d: d.update(description="Failed auth: 3"), "credential"),
    (lambda d: d.update(template="<Text>Bearer abcdefghijkl</Text>"), "credential"),
    (lambda d: d["options"].update(token={"label": "x"}), "credential"),
])
def test_python_side_shape_checks_reject_what_the_app_would_reject(mutate, needle):
    d = good()
    mutate(d)
    problems = inst.check_definition(d)
    assert problems and any(needle in p for p in problems), (needle, problems)


@pytest.mark.parametrize("tpl", [
    "<Text>Secret: none</Text>", "<Text>authentication: enabled</Text>", "<Text>{data.state.auth_failures}</Text>", "<Text>{data.state.token}</Text>",
    "<Text>Bearer token</Text>", "<Text>ssh key: ok</Text>", "<Text>fail2ban: 3</Text>", "<Text>keys: 3</Text>", "<Text>{(data.state.x||[]).map((x,i)=><Text key={i}>{x.n}</Text>)}</Text>",
    '<LineChart h={200} data={data.state.rows} series={[{name:"left"}]} />', "<Text>metadata.json and data. Next</Text>",
])
def test_python_side_checks_do_not_flag_what_the_app_accepts(tpl):
    d = good()
    d["template"] = tpl
    assert inst.check_definition(d) == []


def test_installer_refuses_v1_and_missing_widget_files(env, capsys):
    (env.defs / "ops-disk.json").write_text(json.dumps({"$schema": "homarr-custom-widget-v2", "name": "Ops Disk", "url": "http://127.0.0.1:9111/disk", "authType": "none",
                                                        "method": "GET", "displayType": "customJsx", "displayConfig": {"type": "customJsx", "template": "<Text/>"}}))
    snap = snapshot(env.db)
    assert env.run() == 2 and "v1" in capsys.readouterr().err
    (env.defs / "ops-disk.json").unlink()
    assert env.run() == 2 and "cannot read" in capsys.readouterr().err
    assert env.run("--widgets", "ops-overview") == 0 and snapshot(env.db) != snap                    # other widgets still installable when selected


def test_two_files_with_the_same_name_are_refused(env, capsys):
    d = json.loads((env.defs / "ops-disk.json").read_text())
    d["name"] = "Ops Jobs"
    (env.defs / "ops-disk.json").write_text(json.dumps(d))
    assert env.run("--dry-run") == 2 and "same name" in capsys.readouterr().err


# --------------------------------------------------------------------------- the installer mirrors the builder and accepts the repo's real files
def _builder():
    spec = importlib.util.spec_from_file_location("build_v2_for_installer_test", ROOT / "widgets" / "build_v2.py")
    try:
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    except Exception as exc:                                   # the builder is another stream's file: never let it break this suite
        pytest.skip(f"widgets/build_v2.py not importable: {exc!r}")
    return mod


def test_installer_sizes_refresh_and_order_mirror_the_builders_table():
    b = _builder()
    assert list(b.WIDGETS) == inst.ORDER
    for stem, (_name, _route, _desc, tracks, refresh) in b.WIDGETS.items():
        assert inst.SIZES[stem] == tuple(tracks) and inst.REFRESH[stem] == refresh, stem


def test_the_repos_own_widget_files_are_installable(env, capsys):
    b = _builder()
    widgets = inst.load_widgets(ROOT / "widgets", inst.ORDER)           # python-side shape checks over the committed v2 files
    assert [w.d["name"] for w in widgets] == [b.WIDGETS[s][0] for s in inst.ORDER]
    for w, stem in zip(widgets, inst.ORDER):
        assert w.d["requests"]["state"]["path"] == "/" + b.WIDGETS[stem][1] and w.cols["sources"]["default"]["networkScope"] == "loopback"
    snap = snapshot(env.db)
    assert inst.main([str(env.db), "--widgets-dir", str(ROOT / "widgets"), "--no-docker-check", "--no-harness"]) == 0
    defs, items = installed(env.db)
    assert len(defs) == 7 and len(items) == 28 and {d["template"] for d in defs.values()} == {w.d["template"] for w in widgets}
    assert inst.main([str(env.db), "--rollback", str(next(env.tmp.glob("copy.sqlite.ops-widgets-manifest-*.json"))), "--no-docker-check"]) == 0
    assert snapshot(env.db) == snap


def test_a_manifest_that_cannot_be_written_aborts_before_any_row_is_touched(env, monkeypatch, capsys):
    monkeypatch.setattr(inst, "write_json_atomic", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    snap = snapshot(env.db)
    assert env.run("--backup-dir", str(env.bk())) == 1 and "No space left" in capsys.readouterr().err and snapshot(env.db) == snap


def test_harness_is_not_run_as_root(env, monkeypatch, capsys):
    monkeypatch.setattr(inst.os, "geteuid", lambda: 0)
    monkeypatch.setattr(inst, "harness_check", lambda *a, **k: pytest.fail("harness must not run as root"))
    assert env.run("--dry-run", harness=True) == 0 and "skipped as root" in capsys.readouterr().err


# --------------------------------------------------------------------------- optional real-runtime harness hook
class _Done:
    def __init__(self, rc, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def reports(*failed):
    return json.dumps({"reports": [{"id": s, "failed": s in failed, "failures": ["schema: bad thing"] if s in failed else []} for s in inst.ORDER], "failed": len(failed)})


@pytest.fixture
def fake_harness(tmp_path, monkeypatch):
    fork = tmp_path / "fork"
    (fork / "node_modules").mkdir(parents=True)
    tool = tmp_path / "render-check.mjs"
    tool.write_text("// fake")
    monkeypatch.setenv("HOMARR_WIDGET_VALIDATOR", str(tool))
    monkeypatch.setattr(inst.shutil, "which", lambda n: "/usr/bin/node")
    return fork, tool


@pytest.mark.parametrize("rc, out, verdict", [(0, reports(), "pass"), (1, reports("ops-disk"), "fail"), (1, "not json", "skipped"), (0, reports("ops-disk"), "fail"),
                                              (3, "", "skipped"), (2, "", "skipped"), (4, "", "skipped"), (0, "not json", "skipped")])
def test_harness_hook_maps_exit_codes_and_reports(fake_harness, monkeypatch, tmp_path, rc, out, verdict):
    fork, tool = fake_harness
    seen = {}

    def fake_run(cmd, **k):
        seen.update(cmd=cmd, env=k["env"], jobs=json.loads(Path(cmd[3]).read_text()))
        return _Done(rc, out)

    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    got, detail = inst.harness_check([tmp_path / "ops-disk.json", tmp_path / "ops-load.json"], fork)
    assert got == verdict
    assert seen["cmd"][1] == str(tool) and seen["cmd"][2] == "--batch" and seen["cmd"][4] == "--json" and seen["env"]["HOMARR_REPO"] == str(fork)
    assert seen["jobs"] == {"defaults": {"noDom": True}, "jobs": [{"id": "ops-disk", "definition": str(tmp_path / "ops-disk.json")}, {"id": "ops-load", "definition": str(tmp_path / "ops-load.json")}]}
    if verdict == "fail" and rc == 1 and out.startswith("{"):
        assert "ops-disk: schema: bad thing" in detail


def test_harness_hook_skips_without_node_checkout_or_tool(fake_harness, monkeypatch, tmp_path):
    fork, tool = fake_harness
    x = [tmp_path / "x.json"]
    monkeypatch.setattr(inst.shutil, "which", lambda n: None)
    assert inst.harness_check(x, fork)[0] == "skipped"
    monkeypatch.setattr(inst.shutil, "which", lambda n: "/usr/bin/node")
    assert inst.harness_check(x, tmp_path / "nofork")[0] == "skipped"
    tool.unlink()
    assert inst.harness_check(x, fork)[0] == "skipped"
    monkeypatch.setattr(inst.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    tool.write_text("//")
    assert inst.harness_check(x, fork)[0] == "skipped"


def test_harness_failure_blocks_install_and_require_harness_blocks_a_skip(env, fake_harness, monkeypatch, capsys):
    fork, _ = fake_harness
    snap = snapshot(env.db)
    monkeypatch.setattr(inst.subprocess, "run", lambda cmd, **k: _Done(1, reports("ops-disk")))
    assert env.run("--homarr-fork", str(fork), harness=True) == 2 and "ops-disk: schema: bad thing" in capsys.readouterr().err
    monkeypatch.setattr(inst.subprocess, "run", lambda cmd, **k: _Done(3))
    assert env.run("--homarr-fork", str(fork), "--require-harness", harness=True) == 2 and snapshot(env.db) == snap
    assert env.run("--homarr-fork", str(fork), "--dry-run", harness=True) == 0 and "skipped" in capsys.readouterr().err
    monkeypatch.setattr(inst.subprocess, "run", lambda cmd, **k: _Done(0, reports()))
    assert env.run("--homarr-fork", str(fork), "--require-harness", harness=True) == 0 and snapshot(env.db) != snap


def test_the_installer_runs_the_harness_once_for_all_widgets(env, fake_harness, monkeypatch):
    fork, _ = fake_harness
    calls = []
    monkeypatch.setattr(inst.subprocess, "run", lambda cmd, **k: calls.append(cmd) or _Done(0, reports()))
    assert env.run("--homarr-fork", str(fork), "--dry-run", harness=True) == 0 and len(calls) == 1


def test_real_harness_accepts_the_synthetic_and_the_repo_definitions_when_available(env):
    wdir = ROOT / "widgets"
    paths = [env.defs / f"{s}.json" for s in inst.ORDER] + [p for s in inst.ORDER if inst.check_definition(json.loads((p := wdir / f"{s}.json").read_text())) == []]
    verdict, detail = inst.harness_check(paths, None)
    if verdict == "skipped":
        pytest.skip("real-runtime harness not available: " + detail)
    assert verdict == "pass", detail


# --------------------------------------------------------------------------- export for the in-app import path (option a)
def test_export_import_files_writes_seven_files_and_a_checklist_without_a_database(env, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(inst.sqlite3, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("export must not open a database")))
    out = tmp_path / "import-me"
    assert inst.main(["--export-import-files", str(out), "--widgets-dir", str(env.defs), "--no-harness"]) == 0
    text = capsys.readouterr().out
    assert sorted(p.name for p in out.iterdir()) == sorted(f"{s}.json" for s in inst.ORDER)
    for s in inst.ORDER:
        assert json.loads((out / f"{s}.json").read_text()) == json.loads((env.defs / f"{s}.json").read_text())
        assert (out / f"{s}.json").read_text().endswith("}\n")
    for needle in ("Manage > Custom widgets > Import", "TICK the URL confirmation", "ONCE", "definition not found", "/thermal"):
        assert needle in text
    assert "3x2" in text and "refresh 60 s" in text
    assert inst.main(["--export-import-files", str(env.defs), "--widgets-dir", str(env.defs), "--no-harness"]) == 2        # never writes over the sources
    assert inst.main(["--export-import-files", str(tmp_path / "o2"), "--widgets", "ops-load", "--widgets-dir", str(env.defs), "--no-harness"]) == 0
    assert [p.name for p in (tmp_path / "o2").iterdir()] == ["ops-load.json"]


def test_export_refuses_invalid_definitions(env, tmp_path, capsys):
    (env.defs / "ops-jobs.json").write_text(json.dumps({"name": "x"}))
    assert inst.main(["--export-import-files", str(tmp_path / "o"), "--widgets-dir", str(env.defs), "--no-harness"]) == 2


# --------------------------------------------------------------------------- real-database rehearsal (read-only copy of the live DB via sudo)
SNAP_SCRIPT = ("import os,sqlite3,sys; s=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close(); "
               "os.chown(sys.argv[2],int(sys.argv[3]),int(sys.argv[4])); os.chmod(sys.argv[2],0o600)")


def fresh_live_copy(dest: Path) -> bool:
    """Online-backup copy of the live DB (opened mode=ro: the live file is never written). False when sudo/DB are unavailable."""
    if not LIVE.exists():
        return False
    try:
        r = subprocess.run(["sudo", "-n", "python3", "-c", SNAP_SCRIPT, str(LIVE), str(dest), str(os.getuid()), str(os.getgid())], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and dest.is_file() and dest.stat().st_size > 0


def real_defs(tmp: Path) -> Path:
    """The builders' real v2 files when they are already v2, else the synthetic ones (the rehearsal is about the database mechanics)."""
    wdir = ROOT / "widgets"
    try:
        if all(inst.check_definition(json.loads((wdir / f"{s}.json").read_text())) == [] for s in inst.ORDER):
            return wdir
    except (OSError, ValueError):
        pass
    return write_defs(tmp / "defs")


def test_rehearsal_on_a_fresh_copy_of_the_real_live_database(tmp_path, capsys):
    copy = tmp_path / "live-copy.sqlite"
    if not fresh_live_copy(copy):
        pytest.skip("live database or passwordless sudo not available")
    defs = real_defs(tmp_path)
    pre = snapshot(copy)
    c = sqlite3.connect(copy)
    boards = {n: i for i, n in c.execute("SELECT id, name FROM board")}
    names = {r[0] for r in c.execute("SELECT name FROM custom_widget_v2_definition")}
    fk0 = {tuple(r) for r in c.execute("PRAGMA foreign_key_check")}
    legacy_ids_in_use = c.execute("SELECT count(*) FROM item WHERE kind='customApi'").fetchone()[0]
    thermals_creator = c.execute("SELECT creator_id FROM custom_widget_v2_definition WHERE name='Thermals'").fetchone()
    # old bottoms per (layout, section), computed independently of the installer
    bottoms = {(lay, sec): b for lay, sec, b in c.execute("SELECT layout_id, section_id, MAX(y_offset+height) FROM item_layout GROUP BY layout_id, section_id")}
    c.close()
    assert {"Local-Big-Screen", "Local-Mobile", "Remote-Big-Screen", "Remote-Mobile"} <= set(boards)
    expected = [json.loads((defs / f"{s}.json").read_text())["name"] for s in inst.ORDER]
    argv = ["--widgets-dir", str(defs), "--no-docker-check", "--no-harness"]
    if any(n in names for n in expected):
        pytest.skip(f"the ops widgets are already installed on the live database ({sorted(set(expected) & names)}): re-run is covered by the idempotence tests")
    assert inst.main([str(copy), "--dry-run", *argv]) == 0 and snapshot(copy) == pre
    bk = tmp_path / "backup-rehearsal"
    assert inst.main([str(copy), "--backup-dir", str(bk), *argv, "--boards", ",".join(["Local-Big-Screen", "Local-Mobile", "Remote-Big-Screen", "Remote-Mobile"])]) == 0
    post = snapshot(copy)
    assert {t for t in post if post[t] != pre[t]} == set(WRITTEN)
    assert len(post["custom_widget_v2_definition"]) == len(pre["custom_widget_v2_definition"]) + 7
    assert len(post["item"]) == len(pre["item"]) + 28 and len(post["item_layout"]) == len(pre["item_layout"]) + 56
    assert all(r in post["item_layout"] for r in pre["item_layout"]) and all(r in post["item"] for r in pre["item"])
    assert {t: post[t] for t in post if t not in WRITTEN} == {t: pre[t] for t in pre if t not in WRITTEN}          # incl. legacy tables and secrets
    c = sqlite3.connect(copy)
    assert {tuple(r) for r in c.execute("PRAGMA foreign_key_check")} == fk0 and c.execute("PRAGMA quick_check").fetchone() == ("ok",)
    new_defs = {r[0]: r for r in c.execute("SELECT id, name, creator_id, sources, requests, template, enabled FROM custom_widget_v2_definition WHERE name IN (%s)" % ",".join("?" * 7), expected)}
    assert len(new_defs) == 7 and all(r[6] == 1 and not r[0].startswith("seed-") for r in new_defs.values())
    if thermals_creator:
        assert {r[2] for r in new_defs.values()} == {thermals_creator[0]}
    new_items = c.execute("SELECT id, board_id, json_extract(options,'$.json.definitionId') FROM item WHERE json_extract(options,'$.json.definitionId') IN (%s)" % ",".join("?" * 7), list(new_defs)).fetchall()
    assert len(new_items) == 28 and legacy_ids_in_use >= 8
    for iid, bid, did in new_items:
        lays = c.execute("SELECT il.layout_id, il.section_id, il.x_offset, il.y_offset, il.width, il.height, l.column_count FROM item_layout il JOIN layout l ON l.id=il.layout_id WHERE il.item_id=?", (iid,)).fetchall()
        assert len(lays) == 2 == c.execute("SELECT count(*) FROM layout WHERE board_id=?", (bid,)).fetchone()[0]
        for lay, sec, x, y, w, h, cols in lays:
            assert y >= bottoms[(lay, sec)] and x + w <= cols                                                      # below the old bottom, inside the grid
            others = c.execute("SELECT x_offset, y_offset, width, height FROM item_layout WHERE layout_id=? AND section_id=? AND item_id<>?", (lay, sec, iid)).fetchall()
            assert not any(overlaps((x, y, w, h), o) for o in others)
    c.close()
    # idempotent, then an exact rollback back to the pre-install state
    h = fhash(copy)
    assert inst.main([str(copy), "--backup-dir", str(tmp_path / "backup-again"), *argv]) == 0 and fhash(copy) == h and not (tmp_path / "backup-again").exists()
    assert inst.main([str(copy), "--rollback", str(bk / "ops-widgets-manifest.json"), *argv]) == 0
    assert snapshot(copy) == pre
    assert snapshot(bk / "live-copy.sqlite") == pre


def test_the_real_live_path_is_refused_as_a_positional_argument_without_being_opened(capsys):
    if not LIVE.exists():
        pytest.skip("no live database on this host")
    assert inst.main([str(LIVE), "--dry-run", "--no-docker-check", "--no-harness"]) == 2
    assert "LIVE database" in capsys.readouterr().err
    assert inst.main([str(LIVE), "--no-docker-check", "--no-harness"]) == 2


if __name__ == "__main__":                                       # python3 tests/test_homarr_installer_v2.py --regen-schema COPY.sqlite
    if len(sys.argv) == 3 and sys.argv[1] == "--regen-schema":
        FIXTURE.parent.mkdir(exist_ok=True)
        FIXTURE.write_text(extract_schema_sql(Path(sys.argv[2])))
        print(f"wrote {FIXTURE}")
    else:
        sys.exit("usage: test_homarr_installer_v2.py --regen-schema COPY.sqlite  (run pytest to run the tests)")
