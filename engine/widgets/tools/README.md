# widgets/tools: real-runtime render harness (optional dev tooling)

`render-check.mjs` validates and renders a Homarr v2 custom-widget definition with the **real sources of a Homarr checkout**: the zod schema (full and the stricter preview gate),
the parser behind Manage -> Custom widgets -> Import, the JSX analyzer, the interpreter with its budgets, Mantine 9 and `CustomJsxRenderer` in jsdom, and, with `--shot`, a real
headless Chromium (esbuild bundle of the same runtime) so charts draw and light/dark/size can be inspected. `preview-entry.tsx` is the browser entry it bundles.

Nothing here is needed to run, build or install the widgets. `python3 widgets/build_v2.py --check` and the pure-Python tests do not use it; the harness-backed tests in
`tests/test_widgets_v2.py` skip cleanly when it cannot run.

## Requirements and environment

| What | Detail |
|---|---|
| Node | >= 22 (it reads `node:sqlite` for `--db`) |
| `HOMARR_REPO` | a Homarr v2 checkout **with its `node_modules`** (default `/home/ohmz/StudioProjects/homarr`). Used read-only: it is never written to and nothing is installed. |
| screenshots | `--shot` additionally needs `playwright-core` + `esbuild` in that checkout's `node_modules` and a Chromium (`~/.cache/ms-playwright`) or `/usr/bin/google-chrome` |
| `RENDER_CHECK_WORK` | work dir (default `<tmp>/homelab-maint-render-check-<uid>-<hash of HOMARR_REPO>`): holds a `node_modules` symlink to the checkout (module resolution walks up from the script) and copies of the two scripts. Safe to delete. |
| network | none, except a fixture given as an `http(s)://` URL. The Homarr database, if used, is opened read-only. |

The script re-executes itself under the checkout's `tsx` with the checkout's tsconfig (`jsx: react-jsx` is required by the repo's `.tsx` runtime files), a hard timeout and
`NODE_NO_WARNINGS=1`, so a plain `node render-check.mjs ...` is all you run.

## Usage

```bash
# one definition, one fixture (= the response BODY of the load request "state")
node widgets/tools/render-check.mjs --definition widgets/ops-disk.json --fixture state=widgets/fixtures/ops-disk/ok.json --text

# failure scenarios the board can produce: ok | http-error | network-error | empty | null
node widgets/tools/render-check.mjs --definition widgets/ops-disk.json --fixture state=widgets/fixtures/ops-disk/ok.json --scenario network-error --text

# real Chromium screenshot at a logical tile size (--tracks 3x3 = 616x616 usable px; --size 616x404 gives exact px), dark or light, with overflow metrics
node widgets/tools/render-check.mjs --definition widgets/ops-thermals.json --fixture state=widgets/fixtures/ops-thermals/hot.json \
     --tracks 3x3 --scheme light --shot /tmp/thermals.png --json

# the row as the app stored it (superjson columns), from a READ-ONLY COPY of the Homarr db, with the live ops service as the fixture
node widgets/tools/render-check.mjs --db /tmp/homarr-copy.sqlite --name "Ops Disk" --fixture state=http://127.0.0.1:9111/disk

# a bare template while writing one (a minimal loopback definition is synthesised)
node widgets/tools/render-check.mjs --template my.jsx --fixture state=sample.json

# many jobs in one process (3 s start-up, ~0.2 s per jsdom job, ~1 s per screenshot)
node widgets/tools/render-check.mjs --batch jobs.json --json
node widgets/tools/render-check.mjs --probe                 # {"ok": true, "shots": true, ...}; exit code 3 when the environment is unusable
python3 widgets/build_v2.py --render /tmp/shots             # the standard matrix for all seven widgets (also what the tests run)
```

Flags: `--definition F | --db F (--name N | --id I) | --template F [--request-id state]`, `--fixture id=file|url` (repeatable; the id may be omitted with one load request),
`--scenario`, `--scheme dark|light`, `--tracks WxH | --size WxH`, `--opacity 0..1`, `--shot out.png`, `--text`, `--html-out F`, `--options-json '{...}'`, `--chart-size WxH` (jsdom only),
`--no-dom`, `--allow-warning SUBSTRING` (repeatable), `--json`, `--timeout SECONDS` (default 300), `--repo DIR`, `--work-dir DIR`, `--batch F`, `--probe`, `--help`.

Batch file: `{"defaults": {...}, "jobs": [{"id": "x", "definition": "widgets/ops-load.json", "fixtures": {"state": "f.json"}, "scenario": "ok", "scheme": "dark",
"tracks": "3x3", "shot": "out.png", "noDom": false, "text": true}]}` (camelCase versions of the flags; `defaults` are merged into every job). With `--json` the output is
`{"reports": [...], "failed": N}`; a single job prints its report object.

## What a job reports and fails on

Report fields: `schema` (full + preview), `import` (the Import button's parser + the review it would show: origin, auth, scope, methods, permissions), `analyzer` (errors, warnings),
`limits`, `interpreter` (error, warnings, minimum budgets used: operations, rendered nodes, AST depth), `dom` (RUNTIME_RENDER_ERROR, yellow "N template warnings" alert, React/Mantine
console, suspect text, optional rendered `text`), `shot` (path, size, `metrics`, Chromium console), `advisories`, `failures`, `failed`.

A job **fails** (`failed: true`, listed in `failures`) on: a schema, preview-schema or import-parser error; an analyzer error or an analyzer warning that is not allowed (the benign
`lineProps on LineChart` one is allowed by default; a typo such as `fsz={10}` is not); an interpreter error or warning; `RUNTIME_RENDER_ERROR`; the yellow template-warnings alert;
React/Mantine console errors (for example duplicate keys); a stray `undefined` / `NaN` / `Infinity` / `null` / `true` / `false` / `[object` in the rendered text; and with `--shot` a missing screenshot,
Chromium console or page errors, or chart labels cut off by their own svg (a too narrow y axis).

The text check scans the rendered **text nodes joined by a separator**, not `textContent` (which glues siblings together: "2/9" + "undefined" is "2/9undefined"), and a token only has to stand alone
between non-alphanumerics, so `12dundefined`, `9NaN`, `p: null` and `paused true` all fail while "nullable" or "annulled" pass. The failure line shows the context with `|` where two text nodes meet. (Data that legitimately contains one of these words, say a container called `null`, trips it too: rename it in the fixture.)

`shot.metrics` are reported, not failed on (the test-suite asserts them): `contentNaturalHeight` (height the content needs, independent of the tile), `contentOverflowsTile`,
`horizontalOverflow`, `clippedChartLabels`, `svgCount`, `tile`, and the text contrast: `lowContrastText` (the 12 worst text elements below WCAG AA, 4.5:1, or 3:1 for large text, with ratio, colours, font
size and opacity), `lowContrastCount`, `textElementsMeasured`. Colours are resolved in Chromium (so `light-dark()`, `color-mix()` and `var()` work), translucent ancestor backgrounds are composited over
white and the ancestors' opacity fades the text (a stale tile is drawn at 0.55 on purpose, so judge stale shots separately). Use `--scheme light` to find the colours that only work on a dark board. Advisories: hard-coded colours outside `light-dark()`, style keys the runtime silently strips, more than 4 load requests.

## Exit codes

| exit code | meaning |
|---|---|
| 0 | every job passed |
| 1 | at least one job failed (or the harness crashed) |
| 2 | usage or input error (unknown flag, bad `--size`, definition not found, ...) |
| 3 | environment unavailable (Node < 22, `HOMARR_REPO` missing or without `node_modules`/`tsx`): the tests skip on this |
| 4 | timeout (`--timeout`; a synchronous render loop cannot be interrupted from inside Node, so the child is killed) |

## What it cannot see

The live board's zoomed canvas and exact card inset (tiles use `212n - 20` px usable content), the real network path from the Homarr container to `127.0.0.1:9111`, live data,
the limiter's Redis, and the host browser's fonts. Check those once on a real board after installing (docs/HOMARR_V2_WIDGETS.md section 7).

## Provenance

Productionised from the research harness of docs/HOMARR_V2_WIDGETS.md section 4 (scratch `rc.sh` + `render-check.mjs` + `preview-entry.tsx`): the checkout path is no longer hard-coded,
the wrapper script is gone (it re-executes itself), jobs can be batched, the Import parser, suspect-text, clipped-label and natural-height checks are new.
