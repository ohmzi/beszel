# Homarr v2 widget conventions (the seven ops widgets)

The seven widgets (`ops-overview`, `ops-disk`, `ops-jobs`, `ops-guard`, `ops-reclaim`, `ops-thermals`, `ops-load`) are Homarr **v2** custom
widgets: a JSX template plus a declared request, imported as one JSON file. The readable source is `widgets/ops-*.jsx`;
`python3 widgets/build_v2.py` turns it into the import files `widgets/ops-*.json` (and the sample payloads in `widgets/fixtures/`).
Builder's spec with every source citation: `docs/HOMARR_V2_WIDGETS.md` (section numbers below refer to it). This file replaces the v1 notes,
several of which were wrong for v2 (see "What changed from v1").

## Visual style (unchanged, matches the owner's Thermals / Fan Control widgets)

- Root `<Stack gap={7} p={2}>` (`gap={5}` for the two chart widgets); header row `<Group justify="space-between" align="center" wrap="nowrap" gap={8}>`.
- Status dot `<ColorSwatch size={8} withShadow={false}>`: `var(--mantine-color-teal-5)` ok, `yellow-5` warn, `orange-5` the check itself failed,
  `red-6` crit, `gray-5` no data / stale. The ladder is teal -> yellow -> orange -> red (green is not used).
- Title `<Text fz={11} fw={800} tt="uppercase" lts="0.18em">`; badges `size="xs" radius="sm"`, `variant="light"` for neutral, `"filled"` for emphasis.
- **Fills** (status dots, `Progress` bars, filled badges) use the `.5` shade (`red` 6); **coloured TEXT never does**: the Mantine `.5` shades are 1.5 to 2.8:1 on a light board
  (yellow 1.6, teal 2.1, orange 2.2, red 2.8) and the warn/crit value text IS the message. State text carries a scheme-aware colour, `light-dark(color-mix(in srgb,var(--mantine-color-X-9),#000 30%),var(--mantine-color-X-5))`
  (dark scheme: the `.5` shade as before; light scheme: shade 9 darkened 30 %, >= 4.5:1 on every tile, yellow 4.6 on the tinted panel); a gray state uses `c="dimmed"`. Light-variant
  and outline `Badge`s get the same dark shade through `c=` (dark scheme: Mantine's own `--mantine-color-X-light-color`); a filled orange badge is `color="orange.5" c="dark.9"` and a
  filled teal one `color="teal.9"` (white on `teal.5` was 2.6:1). `build_v2.py --check` rejects a bare-shade text colour (`c="teal.5"`, `c={x.c+".5"}`), and the harness measures the
  contrast of every text element as drawn (`shot.metrics.lowContrastText`); the tests require 0 elements below WCAG AA in light, >= 3:1 in dark (the theme's own `dimmed` grey is 3.99:1 on a tinted panel).
  Text is small (`fz` 9-14, headline numbers 18-20). Panels use `light-dark(...)` backgrounds and borders; the card background is the board's own (never paint a full-bleed opaque background, the board opacity must show).
- A stale payload is drawn at `opacity: 0.55`; the badge says `stale <age>` or `no data`.

## Geometry: the board is a fixed-pixel grid (section 6.3)

Homarr v2 (this fork) lays boards out in **fixed logical pixels**, not in cells that resize with the viewport. One grid track is **212 x 212
logical px** (200 cell + 12 gap); an item spanning `w x h` tracks has a footprint of `212w x 212h` and the card is inset a few px inside it. The whole canvas is then
zoomed once with CSS `zoom` to fit the window, so a widget sees the **same logical size on every device**. Consequences:

- Design for the usable content box `212n - 20` px per axis: 1 track 192, 2 tracks 404, 3 tracks 616. The card scrolls vertically when the content is taller
  (nothing is lost) and clips horizontally, so nothing may depend on being wider than the tile.
- Recommended footprints (`WIDGETS` in `build_v2.py`, mirrored by the installer): **3 x 2** for `ops-overview`, **3 x 3** for the other six. Measured natural content
  height in real Chromium at 616 px width, tallest fixture: overview 359, disk 446, jobs 420, guard 412, reclaim 554, thermals 463, load 437 (3x2 offers 402,
  3x3 offers 614). `tests/test_widgets_v2.py` re-measures these and requires 30 px to spare at the recommended footprint.
- **Charts need an explicit pixel height.** `h="100%"` has no parent height to resolve against inside the scrolling box, and a `vw`/`clamp()` length follows the
  browser window and is zoomed with the canvas, so the chart height would vary per device while the tile does not. The payload therefore carries `h` (a plain number,
  260 thermals / 300 load) and `yw` (the y axis label width in px: 40 / 44, because "100%" is wider than "100 deg" and 32 px clipped it); the template uses
  `h={g.h}` and `width:g.yw||44`.
- Container queries work (`<SimpleGrid type="container" cols={{base:1,"300px":2}}>`); the ops templates use fixed column counts and were measured at 404 and 616 px.

## Request model (sections 2, 3.9)

Every definition has **one source and one load request**, identical apart from the path:

```json
"sources":  {"default": {"baseUrl": "http://127.0.0.1:9111", "networkScope": "loopback", "auth": "none"}},
"requests": {"state": {"kind": "query", "method": "GET", "path": "/<route>", "source": "default", "trigger": "load", "cacheSeconds": 5}},
"options":  {}
```

- The fetch is made server-side by the Homarr container, which runs with `--network host`, so `127.0.0.1:9111` reaches the ops service. v2 insists on an explicit
  `networkScope`: it is checked against the RESOLVED address, so `loopback` is the only value that works (`public`/`private` fail at run time with FORBIDDEN).
- The template reads `data.state.<field>`; the request id (`state`) is the key. `status.state` is `{ok, status, statusText, error}`. Polling is the board item's
  `refreshInterval` (30 s for the five status widgets, 60 s for thermals/load; never below 10 s: 240 queries/min are shared per definition). `cacheSeconds: 5` is the
  server-side response cache, always below the refresh interval. Keep at most 4 load requests per widget (4 run concurrently per user and item).
- When the service is down, `data.state` is `null` and nothing else tells the user: every template starts with the **status banner** right under the header,
  `{status.state?.ok===false&&<Text ...>{"ops service: "+(status.state.error||"no response")}</Text>}`. HTTP 404/500 keep the parsed body in `data.state`
  (`{"error":"not found"}`), which the orange `data.state.error` line shows as before.
- Payloads stay small (< 4 KB for the five status routes, < 14 KB for thermal/load; the hard cap is 1 MiB) and pre-compute strings, ages and colour words.

## Template rules (section 3; each one is enforced by `build_v2.py --check` and the tests)

1. **Method calls are not null-safe.** Reading `data.state.x` when `data.state` is `null` gives `undefined`, but `data.state.rows.map(...)` throws and turns the whole tile into a red
   `RUNTIME_RENDER_ERROR`. Always `(data.state.rows||[]).map(...)`; never call `.toFixed`/`.join`/... on a field without a guard. Callback parameters are plain identifiers
   (`(x,i)=>`, never `([a])=>`), and may not be named `data`, `status`, `options`, `inputs`.
2. **Index keys.** Lists are static, so every mapped element gets an index key, `key={i}` from `.map((x,i)=>`. Names are truncated by the server and the stress fixture produces identical ones, which makes React log
   "two children with the same key" and may duplicate or drop children.
3. **No credential-literal text anywhere.** The schema (re-run on EVERY render, not only at save) rejects `<credential word>: <value>` or `=<value>` for auth, authorization,
   credential(s), api key, password(s), secret(s), token(s), access/refresh token, private/signing key, also inside JSX text, object keys and `x.token==="y"`. Do not name a label or a
   compared field like that (`{data.state.auth_failures}` as a bare read is fine, `Auth: {n}` is not).
4. **No nested keys named `left`, `right`, `top`, `bottom`, `pos`, `inset`, `zIndex`, `on*`, `*Ref`, `className`, `children`, `component`** inside prop objects other than `data=`/`series=`
   (they are dropped with a yellow warning alert); that is why `xAxisProps` has no `padding:{left,right}` any more. `style={{...}}` silently loses `position`, `top`, `left`, `filter`, ...
5. Only data via `data.state.*` / `status.state.*`; no other binding; no `new`, block-bodied arrows, destructuring, assignments, `Date` (payloads pre-compute ages).
6. Only registered components: the ops widgets use `Stack Group Text Badge ColorSwatch Divider SimpleGrid Paper Progress Sparkline LineChart` (`KNOWN_COMPONENTS` in `build_v2.py`; adding one is a
   deliberate edit plus a harness run). Style props and `light-dark(...)` colours only; no raw HTML.
7. Template <= 50,000 characters (ours are under 4,000), one JSX expression, one element per line, squashed by the build (no indentation, blank lines or `//` comment lines).
8. A mapped list needs a payload field that exists: `tests/test_widgets_v2.py` checks every `data.state.<key>` / `g.<key>` / `x.<key>` against the fixtures.
9. **Templates guard against a missing list, not a wrong type.** `(x||[]).map(` survives `null`, `undefined`, `""`, `0`, `[]`, `{}` and a `{"error":..}` body, but a truthy non-array
   (an object or a number where a list is expected) throws "Calling method 'map'" and turns the tile red. The ops service never sends that; the contract is enforced at the producer by
   `test_every_list_a_template_maps_over_is_a_list_in_every_payload_case`, not by 25 `Array.isArray` guards in the templates.
10. **Coloured text** follows the Visual style rule above (`light-dark(...)` or `dimmed`, never `c="teal.5"`); the status dot goes gray, not teal, whenever there is no fresh data
    (`stale`, no `head`, service error), in every widget.

## What changed from v1

| v1 (react-jsx-parser, `customJsx`) | v2 |
|---|---|
| file keys `url`, `authType`, `method`, `displayType`, `displayConfig` | strict v2 object: `$schema`, `name`, `description`, `sources`, `requests`, `options`, `template`; any other key is rejected |
| template reads `data.<field>` | template reads `data.state.<field>` (request id `state`) |
| `url` with the route | `sources.default.baseUrl` + `requests.state.path` + mandatory `networkScope: "loopback"` |
| 10,000-character cap, forbidden-word scan, component whitelist file | 50,000-character cap, a real JSX analyzer, a 332-component registry, and the credential-literal check above |
| property read on undefined = warning | property reads are null-safe, method calls on undefined crash the tile |
| boards: "square cells that resize with the viewport" (wrong for this fork) | fixed 212 px logical tracks, canvas zoom; chart heights in px from the payload |
| `check_templates.mjs` + `build_widgets.py` + `build_metrics_widgets.py` | `tools/render-check.mjs` (real runtime) + `build_v2.py` (pure Python); the three old files are deleted |
| `install_homarr_widgets.py` wrote `custom_widget_definition` | v2 tables only (`custom_widget_v2_definition`); never run a v1 installer against a v2 database |

## Validate

```bash
python3 widgets/build_v2.py --check                  # committed files == build output, lint (shape, limits, trap, null-safety), no node needed
python3 -m pytest tests/test_widgets_v2.py tests/test_homarr_installer_v2.py tests/test_payloads.py tests/test_payloads_metrics.py -q
python3 widgets/build_v2.py --render /tmp/shots      # real Homarr runtime + Chromium over every fixture, scenario, size, scheme (needs HOMARR_REPO)
node widgets/tools/render-check.mjs --definition widgets/ops-load.json --fixture state=widgets/fixtures/ops-load/hot.json --tracks 3x3 --shot /tmp/load.png
```

`widgets/tools/render-check.mjs` runs the REAL schema, the Import button's own parser, the analyzer, the interpreter, Mantine and `CustomJsxRenderer` from a Homarr checkout
(`HOMARR_REPO`, default `/home/ohmz/StudioProjects/homarr`, never modified) and a real headless Chromium for screenshots, overflow and text-contrast metrics. It is optional dev tooling;
the harness-backed tests skip cleanly when node >= 22 or the checkout is missing. Details: `widgets/tools/README.md`. It cannot see the live board (zoomed canvas, real network, live data).
After an install, read the stored row back: `render-check.mjs --db <read-only copy of the live db> --name "Ops Disk" --fixture state=http://127.0.0.1:9111/disk`.

## Import and install (section 6.7)

1. Safest: Homarr -> Manage -> Custom widgets -> **Import** (file picker or paste of `widgets/ops-*.json`); the review dialog must show origin `http://127.0.0.1:9111`,
   authentication none, scope loopback, method GET, permission view, no actions; tick the URL confirmation, then Import **once** (every click creates a definition).
   Then add a "Custom widget" item per board, pick the definition, set `refreshInterval` (30 / 60) and the footprint above.
2. Scripted: `python3 widgets/install_homarr_widgets.py` writes the v2 tables with a backup (runbook: `widgets/INSTALL.md`; always rehearse on a copy, the live database is only written with `--live` and a backup dir).
3. `/thermal` and `/load` exist only in a `homelab-maint` installed after `payloads_metrics` landed: curl them before expecting data in those two widgets.

## Decisions and corrections relative to docs/HOMARR_V2_WIDGETS.md (verified while building)

- **Chart height.** The spec left "payload `h` as a `vw` clamp vs px" to the owner; it is px now (260 thermals, 300 load), so the rows of spec 6.3 that depend on the viewport (thermals up to 503, load up to 437)
  are superseded by fixed measurements: thermals 463, load 437 at 616 px width.
- **Y axis width.** Measured with the harness's `clippedChartLabels` metric: the load chart needs 38 px (its `100%` is cut by 5.5 px at 32 and still overflows by 1.5 px at 36), the thermals chart 34 px (`100°`
  overflows its svg by 1.5 px at 32, 3.5 px at 30). The spec's "thermals' `100°` fits at 32" holds only to within 1.5 px. Shipped: 44 / 40 (6 px of margin for a wider host font), carried in the payload as `yw`.
- **Analyzer warnings now fail a harness job** unless allow-listed (only the benign `lineProps` on `LineChart`); the research harness ignored them, so a typo like `fsz={10}` passed silently.
- **Harness.** `rc.sh` and the hard-coded checkout path are gone: `render-check.mjs` re-executes itself under the checkout's `tsx`, reads `HOMARR_REPO`, runs the Import button's own parser on a definition file,
  and the screenshot bundle resolves the checkout through an esbuild alias. New checks: suspect text (`undefined`/`NaN`/`[object`), clipped chart labels, natural content height, batch mode.
- **Text contrast (light boards).** The spec's stock-palette advice (`c="teal.5"`, section 3.6) leaves state text at 1.5 to 2.8:1 on a light board. Text now uses `light-dark(...)` (Visual style above);
  the harness gained a measured contrast metric (colours resolved in Chromium, translucent backgrounds composited, the stale tile's 0.55 opacity applied) so this cannot regress silently.
- **Suspect-text check (spec 4.1: "fails on undefined / NaN / [object").** It used to run `\bundefined\b` over `textContent`, which glues sibling elements together ("2/9" + "undefined" = "2/9undefined"),
  so most real leaks passed. It now scans the text nodes joined by a separator for `undefined`, `NaN`, `Infinity`, `null`, `true`, `false` (and `[object`) standing alone between non-alphanumerics; words that merely
  contain them ("nullable") pass. `[object` is practically unreachable: the interpreter throws on object-to-string conversion and an object child is a RUNTIME_RENDER_ERROR.
- **Routes (spec 0 item 10, 1, 7 item 2).** `/thermal` and `/load` answer 200 on the running ops service now (all seven routes do); the "404 until install.sh is re-run" notes are obsolete (the check stays in INSTALL.md section 2).
- **Harness location (spec 4.1, 4.5).** The harness is `widgets/tools/render-check.mjs` + `preview-entry.tsx` (no `rc.sh`, no hard-coded checkout, `check_templates.mjs` and `build_widgets.py --check` are gone); nothing is copied into `widgets/`.
- **Installer (spec 6.7, 7 item 10).** A same-named definition that differs from the shipped file, or is disabled, is refused (exit 2, nothing written) unless `--update-existing` or `--accept-existing`: the spec only said "idempotent by name".
- **Credential trap (spec 3.8).** All 21 documented rows were re-verified against the real schema (`test_real_schema_agrees_with_the_credential_table`) and against the pure-Python port in `build_v2.py`.
- **Rollout order.** The ops service that is installed today still sends `h` as `clamp(..vw..)` and no `yw` for `/thermal` and `/load`; the new templates render both shapes (`h` may be a CSS length, `yw` falls back to 44),
  verified by rendering the live endpoints. Re-run `install.sh` to get the px payload.

## Changing or adding a widget

Edit `ops-<name>.jsx` (and the payload builder), run `python3 widgets/build_v2.py` (rewrites the JSON and fixtures), `--check`, the tests, and a `--render` with screenshots in dark and light.
A new widget also needs an entry in `WIDGETS` (name, route, description, footprint, refresh), fixtures cases, and a route in `homelab_maint/server.py`.
