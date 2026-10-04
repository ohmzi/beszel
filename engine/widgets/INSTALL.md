# Installing the seven ops widgets on Homarr v2

The widgets (`ops-overview`, `ops-disk`, `ops-jobs`, `ops-guard`, `ops-reclaim`, `ops-thermals`, `ops-load`) are Homarr v2 custom widgets. Their definitions are the
import files `widgets/ops-*.json` (built from `widgets/ops-*.jsx` by `python3 widgets/build_v2.py`). Builder's spec with every source citation:
`docs/HOMARR_V2_WIDGETS.md` (section numbers below refer to it). Evidence labels as there: **[V]** verified here, **[S]** read from Homarr source, **[U]** not verified
(needs the live host or a browser session).

The v1 installer wrote `custom_widget_definition`; **Homarr v2 only reads `custom_widget_v2_definition`**, so a v1 installer would leave "migration required" tiles. This
installer refuses a database without the v2 table and never writes the legacy tables.

## 1. Pick an option

| Rank | Option | Needs | Writes the live DB how | Cost | Use when |
|---|---|---|---|---|---|
| 1 | **A. In-app import + UI placement** | a browser signed in as the admin | the app itself (validation, admin guard, audit trail apply) | 7 imports + up to 28 placements by hand | default; nothing can go wrong that you cannot undo in the UI |
| 2 | **B. Admin API key + `curl`** | an API key (Manage > Tools > API) | the app itself, through tRPC / REST | 7 + 28 calls, scripted; creating and deleting the key are live writes | you want the app's validation but not the clicking |
| 3 | **C. `install_homarr_widgets.py`** | root (the DB dir is root-owned), a new backup dir | this script, directly into SQLite, one `BEGIN IMMEDIATE` transaction | 1 command per step, exact rollback by manifest | you accept direct SQL for speed and have rehearsed on a copy |

Whatever you choose, do the preparation in section 2 first. No option needs a Homarr restart [S: definitions and items are read from the database on every request; open board tabs
need a reload].

## 2. Preparation (all read-only)

```bash
cd /home/ohmz/homelab-maint
# 1. the import files are current and lint clean (pure Python, no node)
python3 widgets/build_v2.py --check
# 2. the ops service answers on every route (must print seven 200s). /thermal and /load were 404 on a service older than payloads_metrics:
#    then re-run install.sh and restart homelab-maint-www.service (an owner action) before expecting data in those two widgets.
for r in overview disk jobs guard reclaim thermal load; do curl -s -o /dev/null -w "$r %{http_code}\n" http://127.0.0.1:9111/$r; done
# 3. the running image is the v2 one [U: docs/INTEGRATION.md of 2026-10-02 says the host still ran the pre-v2 image]
docker inspect homarr --format '{{ index .Config.Labels "org.homarr.dev.revision" }}'
python3 - <<'EOF'      # the live DB must have the v2 table; this opens a read-only URI and writes nothing
import sqlite3
c = sqlite3.connect("file:/data/compose/5/homarr/appdata/db/db.sqlite?mode=ro", uri=True)
print("v2 definitions:", c.execute("SELECT count(*) FROM custom_widget_v2_definition").fetchone()[0])
EOF
```

Decisions that are the owner's (defaults in brackets; section 6.3 / 7 item 14 of the spec): footprint per widget [3x2 for `ops-overview`, 3x3 for the other six, content-fit], refresh
[30 s for the five status widgets, 60 s for thermals/load; never below 10 s because 240 queries/min are shared by every viewer of a definition], which boards [all four:
`Local-Big-Screen`, `Local-Mobile`, `Remote-Big-Screen`, `Remote-Mobile`]. The old 5x4 / 10xN installer sizes came from the v1 "scaling cells" assumption; the v2 board is a fixed
212 x 212 logical-px track grid.

## 3. Option A: in-app import (recommended)

```bash
python3 widgets/install_homarr_widgets.py --export-import-files /tmp/ops-widgets     # writes the seven files, validates them, prints this checklist; touches no database
```

1. Copy `/tmp/ops-widgets/*.json` to the machine whose browser you use for Homarr; sign in as the admin (`ohmz_h`).
2. Manage > Custom widgets > Import: choose a file (or paste its JSON anywhere on that page).
3. The review dialog must show origin `http://127.0.0.1:9111`, authentication `none`, scope `loopback`, method `GET`, no actions. **Tick the URL confirmation** (required for any
   non-public source), then click Import **once** (every click creates another definition).
4. Repeat for all seven; the list must show each name exactly once.
5. Per board: edit mode > add widget > Custom widget > pick the definition, set the refresh interval and size, save the board. Homarr puts a new tile in the first free spot of every
   layout (it can fill a hole above your tiles); drag it where you want it.
6. Reload each board and run the checklist in section 7.

Rollback: delete the definitions in Manage > Custom widgets (their tiles then say "definition not found" with a remove button) and remove the tiles in edit mode.

## 4. Option B: admin API key

Create a key under Manage > Tools > API (shown once as `<id>.<token>`; it does not expire and acts as `ohmz_h`, so **delete it when done**). Do not paste it into a shell history:

```bash
BASE=http://127.0.0.1:7575            # nginx inside the container
read -rs -p 'Homarr API key (id.token): ' KEY; echo
H="ApiKey: $KEY"
# 1. import each definition (tRPC; superjson envelope {"json": input}); the reply is {"result":{"data":{"json":{"id":"<new id>"}}}}
for f in widgets/ops-*.json; do
  body=$(python3 -c 'import json,sys; print(json.dumps({"json":{"widget":json.load(open(sys.argv[1])),"secrets":[]}}))' "$f")
  echo "$f -> $(curl -sS -X POST "$BASE/api/trpc/customWidget.import" -H "$H" -H 'content-type: application/json' --data "$body")"
done
# 2. place one tile per board (REST): creates the item and one item_layout row per layout at the first free position
curl -sS -X POST "$BASE/api/boards/items" -H "$H" -H 'content-type: application/json' \
  --data '{"boardId":"<board id>","kind":"customApi","options":{"definitionId":"<id from step 1>","refreshInterval":30},"size":{"width":3,"height":2}}'
# 3. optional exact position per layout
curl -sS -X PATCH "$BASE/api/boards/<boardId>/items/<itemId>/layouts/<layoutId>" -H "$H" -H 'content-type: application/json' \
  --data '{"boardId":"<boardId>","itemId":"<itemId>","layoutId":"<layoutId>","xOffset":0,"yOffset":44,"width":3,"height":2}'
unset KEY H
```

Board and layout ids: read them from a read-only copy of the DB (`SELECT id, name FROM board;` and `SELECT id, name, role FROM layout WHERE board_id=...;`). The tRPC wire format was checked
against a stand-in router [V]; the `/api/boards/items` and PATCH shapes come from the source and docs [S]; **the calls were not exercised against the live Homarr [U]**.
Rollback is the same as option A (delete in Manage and in edit mode), plus delete the API key.

## 5. Option C: this installer (direct SQLite, last resort)

```
python3 widgets/install_homarr_widgets.py COPY.sqlite [--dry-run] [--boards NAMES|all|none] [--widgets STEMS] [--refresh N] [--size [STEM=]WxH]
                                                      [--update-existing | --accept-existing] [--backup-dir NEW_DIR] [--full-sql]
python3 widgets/install_homarr_widgets.py --live LIVE.sqlite --backup-dir NEW_DIR [same filters]       # the only way to write a non-copy
python3 widgets/install_homarr_widgets.py TARGET --rollback MANIFEST [--live ... --backup-dir NEW_DIR]  # remove exactly the recorded rows
python3 widgets/install_homarr_widgets.py --export-import-files DIR                                     # option A files + checklist
```

### 5.1 Rehearse on a fresh read-only copy (always)

```bash
SP=/tmp/hm-rehearsal; mkdir -p $SP
# online backup of the live file (opened mode=ro: the live file is never written); chown so you can work on it as yourself
sudo python3 - <<EOF
import os, sqlite3
s = sqlite3.connect("file:/data/compose/5/homarr/appdata/db/db.sqlite?mode=ro", uri=True); d = sqlite3.connect("$SP/copy.sqlite")
s.backup(d); d.close(); s.close(); os.chown("$SP/copy.sqlite", int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
EOF
python3 widgets/install_homarr_widgets.py $SP/copy.sqlite --dry-run > $SP/plan.txt        # as yourself: plan, SQL, rollback SQL; also runs the real-runtime schema check
python3 widgets/install_homarr_widgets.py $SP/copy.sqlite --backup-dir $SP/backup-1       # applies to the COPY
python3 widgets/install_homarr_widgets.py $SP/copy.sqlite --backup-dir $SP/backup-2       # second run: "nothing to do", no backup, no write
python3 widgets/install_homarr_widgets.py $SP/copy.sqlite --rollback $SP/backup-1/ops-widgets-manifest.json   # copy is back to its pre-install rows
```

Read `plan.txt`: the positions must match section 6.4 of the spec (for example `Local-Big-Screen` Base overview at (0,44) 3x2, disk (3,44), jobs (6,44), guard (0,47), reclaim (3,47),
thermals (6,47), load (0,50); Mobile one tile per row from y=76). Then apply to the copy and run the read-back of section 6 against it.
A copy that lives under the Homarr data dir, or is the same file as `$HOMARR_LIVE_DB` / the container's `/appdata` mount, is treated as live and refused as a positional argument.

### 5.2 The live run

```bash
APPDATA=/data/compose/5/homarr/appdata
BK=$APPDATA/db/backup-$(date +%Y%m%d-%H%M%S)                      # same naming as deploy-homarr.sh; must NOT exist yet
python3 widgets/install_homarr_widgets.py --live $APPDATA/db/db.sqlite --dry-run         # as yourself: reads the live file read-only, prints the real plan, writes nothing
sudo python3 widgets/install_homarr_widgets.py --live $APPDATA/db/db.sqlite --backup-dir "$BK"
```

Add `--boards Remote-Big-Screen` (your home board) to try one board first; `--widgets ops-overview,ops-disk` to try fewer widgets. Run the schema check as yourself (the dry-run); under `sudo`
the installer skips it on purpose (node would leave root-owned work files in `/tmp`), and `--no-harness` skips it everywhere.

What a run does, in order (any failure before COMMIT leaves the database byte-for-byte as it was):

1. Refuses unless the target is a copy or `--live` + `--backup-dir`; refuses a database without `custom_widget_v2_definition`, or whose `customApi` items still point at legacy-only ids.
2. Python-side shape checks of all seven files (v1 files, unknown keys, secrets/auth, `?` in paths, credential-looking text that the app rejects on **every render**, `data.x` reads with no
   matching request, ...) and, when node and the Homarr checkout exist, the real schema + import parser + analyzer through `widgets/tools/render-check.mjs --batch` (schema-only jobs, one node process).
3. Aborts if a non-empty `db.sqlite-journal` or `-wal` exists (a write is in flight).
4. **Online backup** (sqlite backup API) into the NEW `$BK/` (mode 0700, file 0600, same file name `db.sqlite`), then `integrity_check` and non-empty checks; the dir must not exist or must be empty.
   A failed verification removes the half backup and aborts before any write.
5. `PRAGMA busy_timeout=15000`, **one** `BEGIN IMMEDIATE` transaction. Inside it the plan is computed again from the rows as they are at that moment (ids, existing definitions and items,
   bottoms; the preview printed before the backup therefore shows `<new id>` instead of ids, the rows really written are listed under `applied`), the manifest `$BK/ops-widgets-manifest.json` is written (status `pending`), then the statements run:
   - `INSERT INTO custom_widget_v2_definition` per widget not yet present by name: ids `[a-z][a-z0-9]{23}` (never `seed-`), parsed-form `sources`/`requests`/`options` in the superjson envelope
     `{"json": ...}`, `enabled=1`, `created_at=updated_at=` Unix seconds, `creator_id` copied from the existing `Thermals` row.
   - per board and widget not already placed there (an existing `customApi` item with that `definitionId`): `INSERT INTO item` (`kind='customApi'`, options `{"json":{"definitionId":..,"refreshInterval":30}}`) and one
     `INSERT INTO item_layout` per layout of the board, in the board's single main `empty` section, **below the lowest existing item or container of that layout**, left to right, wrapping at the
     lane's column count (Base 10 columns, Mobile 3). Nothing is moved, nothing existing is overlapped.
   - `PRAGMA foreign_key_check` (no new violations) and `PRAGMA quick_check` must pass **before** `COMMIT`; otherwise `ROLLBACK`, manifest status `aborted`.
6. After COMMIT: manifest status `committed`, every recorded row is read back, and the command prints the manifest path. No restart is needed; reload the board page.

Refusals (exit 2, nothing changed): v1 database or legacy-only ids; live path without `--live`/`--backup-dir`; board without exactly one main-lane `empty` section or without exactly one Base and
one Mobile layout (an odd board makes `--boards all` refuse as a whole; name the boards you want instead); two definitions with the same widget name; unknown board, widget or `--creator-id`;
in-flight journal; backup dir that exists and is not empty; **a same-named definition that differs from the shipped file, or is disabled** (see below). Exit 1: a database error (for example
the database stayed locked for the whole busy timeout: a retry is safe).

A definition named like one of ours (for example an "Ops Disk" you imported by hand from an older build, or edited in Manage) is never silently adopted: tiles placed on it would show ITS
template while the installer reported success. The run prints the plan (`DIFFERS`, `NOT placed`), explains the problem on stderr, **writes nothing (no backup, no manifest)** and exits 2, in
`--dry-run` and in a real run alike (also with `--boards none`). You choose explicitly:

- `--update-existing` rewrites the definition to the shipped file (same id, its tiles stay, `updated_at` bumped, the previous values go into the manifest and are restored by `--rollback`);
- `--accept-existing` keeps your definition as it is and places tiles on it anyway (the plan says "kept as it is"); the same flag accepts a **disabled** definition (its tiles say
  "unavailable" until you enable it in Manage > Custom widgets; `--update-existing` does not enable it);
- or delete / rename the foreign definition in Manage > Custom widgets and re-run.

If the definition changes between the dry run and the write lock the real run refuses as well (the plan is recomputed under the lock).

### 5.3 Rollback

```bash
python3 widgets/install_homarr_widgets.py --live $APPDATA/db/db.sqlite --rollback "$BK/ops-widgets-manifest.json" --dry-run        # prints the DELETEs
sudo python3 widgets/install_homarr_widgets.py --live $APPDATA/db/db.sqlite --rollback "$BK/ops-widgets-manifest.json" \
     --backup-dir $APPDATA/db/backup-rollback-$(date +%Y%m%d-%H%M%S)
```

Removes exactly the rows in the manifest, children first (`item_layout`, then `item`, then `custom_widget_v2_definition`), restores the previous values of rows `--update-existing` changed, inside the same kind of
single transaction with its own fresh backup. It leaves alone, and says so: an item that is no longer what was installed; a definition that other items (for example tiles you added in the UI) or
secrets depend on; a definition you renamed or edited after the install. Tiles you moved or re-sized in the UI are still removed (they are the installer's items).
**Full restore** (loses every change made to Homarr since the backup; prefer the rollback above) is an owner action because it replaces the file under a running app:
`docker stop homarr && sudo cp -a "$BK/db.sqlite" $APPDATA/db/db.sqlite && docker start homarr`.

### 5.4 Notes

- Python's `sqlite3` has foreign keys off by default and the app's driver has them on [S]; the installer turns them on for its connection **and** runs `foreign_key_check` before COMMIT.
- A WAL-mode database is handled by the same protocol (backup API, journal/wal check, one write transaction); the live file is most likely in rollback-journal mode [U].
- Board gutters are honoured as the app does (`packages/definitions/src/section.ts`): only the main lane's columns are used, Mobile layouts have no gutters; gutter lanes are separate `empty`
  sections with `x_offset` -1 / 1 and are not the target.
- The three statement shapes are in `docs/HOMARR_V2_WIDGETS.md` 6.7c; `--dry-run --full-sql` prints them with the real templates.

## 6. Verify (any option)

```bash
SP=/tmp/hm-after; mkdir -p $SP
sudo python3 - <<EOF                                                                   # fresh read-only copy of the live DB, as in 5.1
import os, sqlite3
s = sqlite3.connect("file:/data/compose/5/homarr/appdata/db/db.sqlite?mode=ro", uri=True); d = sqlite3.connect("$SP/after.sqlite")
s.backup(d); d.close(); s.close(); os.chown("$SP/after.sqlite", int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
EOF
sqlite3 -header -column "file:$SP/after.sqlite?mode=ro" "
SELECT d.name, d.enabled, count(i.id) AS tiles FROM custom_widget_v2_definition d
  LEFT JOIN item i ON json_extract(i.options,'\$.json.definitionId')=d.id WHERE d.name LIKE 'Ops %' GROUP BY d.id ORDER BY d.name;   -- 7 rows, enabled 1, 4 tiles each
PRAGMA foreign_key_check;      -- no rows
PRAGMA quick_check;            -- ok"
# each stored row read back through superjson and the real schema, rendered with live ops data (batch = one node process):
python3 - <<EOF
import json
names = {"overview": "Ops Overview", "disk": "Ops Disk", "jobs": "Ops Jobs", "guard": "Ops Guard", "reclaim": "Ops Reclaimed", "thermal": "Ops Thermals 7d", "load": "Ops Load 7d"}
json.dump({"jobs": [{"id": r, "db": "$SP/after.sqlite", "name": n, "fixtures": {"state": f"http://127.0.0.1:9111/{r}"}} for r, n in names.items()]}, open("$SP/jobs.json", "w"))
EOF
node widgets/tools/render-check.mjs --batch $SP/jobs.json --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print("failed:", d["failed"]); [print(r["id"], "FAIL" if r["failed"] else "PASS", r["failures"]) for r in d["reports"]]'
```

## 7. Post-install checklist

- [ ] Section 6: seven definitions, enabled, 4 tiles each (or the number of boards you chose); `foreign_key_check` empty; `quick_check` ok; seven PASS lines from the read-back.
- [ ] Reload each board (hard reload; open tabs keep the old client state): the tiles render, no red triangle, no yellow "N template warnings", `refreshInterval` as chosen (30 / 60).
- [ ] No "definition not found" tile (wrong `definitionId`) and no "migration required" tile (an item points at a legacy-only id).
- [ ] The tiles sit below your existing content on every layout (Base and Mobile) and nothing overlaps; footprints 3x2 / 3x3 show all content without scrolling [U: exact card inset, zoomed canvas, chart heights follow the viewport width].
- [ ] `curl http://127.0.0.1:9111/thermal` and `/load` answer 200 before judging the Thermals / Load tiles; with the service down the tiles show the red "ops service: ..." banner by design.
- [ ] Tab idle: Homarr issues about 2 requests/min per tile (budget 60/min per tile, 240/min per definition); do not set a refresh below 10 s.
- [ ] Option B only: the API key is deleted again (Manage > Tools > API).
- [ ] Option C only: keep `$BK/` (backup + manifest) until you are happy; it is the only thing needed for the rollback in 5.3.

## 8. What the tile says when something is wrong (section 3.9)

| Tile | Meaning |
|---|---|
| "no data" badge, banner "ops service: External request failed" | the ops service is down or unreachable from the container (`127.0.0.1:9111`, scope `loopback`) |
| banner "ops service: HTTP 404: Not Found" | the route does not exist yet (`/thermal`, `/load` on an old service) |
| "definition not found" + remove button | the item's `definitionId` matches no v2 definition (or the definition is disabled / not viewable) |
| "migration required" | the item points at an id that exists only in the legacy table |
| generic red triangle "request failed" | the stored row no longer passes the app's schema (for example a credential-looking label added to a template); re-run the harness on it |
| red `RUNTIME_RENDER_ERROR` alert | a method call on a missing value in the template; run the harness with the failing payload |
