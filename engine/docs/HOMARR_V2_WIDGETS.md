# HOMARR_V2_WIDGETS: builder's spec for the seven ops widgets on Homarr v2

Status: research complete, nothing installed. Date 2026-10-03. Audience: the builders who port
`widgets/ops-overview, ops-disk, ops-jobs, ops-guard, ops-reclaim, ops-thermals, ops-load` from the v1 `customJsx`
format to Homarr v2 custom widgets, and whoever installs them. Every rule below is a constraint the builders are held to.

Evidence labels used throughout:

* **[V]** verified in this session by running the real Homarr code: the repo's zod schemas, JSX analyzer, interpreter,
  Mantine 9.6.0 in jsdom, and a real headless Chromium (Playwright) screenshot of the real runtime.
* **[S]** read from source; citation is `path:line` relative to `/home/ohmz/StudioProjects/homarr` (fork branch
  `feat/v2-integration`, commit `64cd2cb07`; `git diff v2.0.0 HEAD` is empty for every custom-widget, board-API,
  auth and encryption path, the only DB delta is the fork's `0048_uptime_daily` migration).
* **[U]** NOT verified. Needs the live host, a live Homarr call, or a browser session on a real board.

Nothing in `/home/ohmz/StudioProjects/homarr`, the live Homarr database or the container was touched. All experiments ran
against read-only copies and scratch files (index in Appendix B).

---

## 0. The rules that matter most (read first)

1. **v1 files are rejected by v2.** `url`, `authType`, `method`, `displayType`, `displayConfig`, `headerName`, `requestBody`,
   `stateSchema`, `defaultState` are all unknown keys (strict objects). v2 needs `sources`, `requests`, `options`, `template`
   [S `packages/custom-widgets/src/core/custom-jsx-schema.ts:77`, `import.ts:51-70`] [V].
2. **Do NOT run `widgets/install_homarr_widgets.py` against the v2 database.** Its schema guard passes (the v1 tables are kept),
   and a dry run on a scratch copy of the v2 DB planned inserts into the legacy `custom_widget_definition` table plus
   `customApi` items pointing at them. Those items would show "migration required" because `getData` finds only a legacy row
   (`packages/api/src/router/widgets/custom-api.ts:82-93`) [V dry run]. The installer must be rewritten for the v2 tables (§6.7).
3. **One source, one load query, `data.state.*`.** Source `default` = `http://127.0.0.1:9111`, `networkScope: "loopback"`,
   `auth: "none"`; request `state` = `GET /<route>`, `trigger: "load"`. The template reads `data.state.<field>`. This is the same
   shape as the user's migrated Thermals/Fan Control rows [V].
4. **The template is re-validated on EVERY `getData`,** not only at save: the stored row is parsed through the full
   `customWidgetDefinitionSchema` including the JSX analyzer and the credential-literal check
   (`packages/api/src/router/custom-widget/stored-definition.ts:33-44`, `custom-api.ts:97`). A template that fails the schema
   makes the whole tile fail with a generic "request failed" triangle, and a direct DB insert does not bypass that [S].
5. **Credential-literal trap.** Any text matching `<word>: <value>` or `<word>=<value>` for the words auth, authentication,
   authorization, credential(s), api key, password(s), secret(s), token(s), access/refresh token, access key, client secret,
   private key, signing key fails the schema, including inside JSX text, object keys and `x.token==="y"` comparisons (§3.8) [V].
6. **Property reads on null are safe; method calls on null are not.** `data.state.x` when `data.state` is `null` yields
   `undefined`, but `data.state.list.map(...)` or `data.state.n.toFixed(1)` on `undefined` throws and the whole tile shows a red
   `RUNTIME_RENDER_ERROR` alert (§3.4) [V].
7. **When the service is down, `data.state` is `null`, not an error screen.** Only `status.state` tells you (§3.9). Add the
   status banner (§5.6) or the widget just says "no data" [V].
8. **List keys must be unique.** The server truncates names, so the `stress` fixture produces duplicate `key={x.n}` values and a
   React "two children with the same key" console error. Use index keys (§5.6) [V].
9. **Board geometry in this fork is fixed logical pixels, not "square cells that scale with the screen".** One track is 212 x 212
   logical px; the whole canvas is CSS-zoomed. A tile's logical size never depends on the device (§6.3). `CONVENTIONS.md` is wrong
   about this for v2 [S] [V].
10. **The live ops service does not serve `/thermal` or `/load` yet.** `curl 127.0.0.1:9111/thermal` and `/load` return HTTP 404 `{"error":"not found"}`
    today; the running `homelab-maint-www.service` was started 2026-10-01 21:33 from `/usr/local/lib/homelab-maint`, before
    `payloads_metrics` existed [V]. Re-run `install.sh` / restart the unit before expecting data in those two widgets.
11. **Hard limits.** Template <= 50,000 chars; <= 4 load requests per widget; response <= 1 MiB; request deadline 45 s, 10 s per hop;
    60 queries/min per user+item, 240/min per widget definition (§2.4, §6.5) [S].
12. **Two widgets need one edit beyond `data.` -> `data.state.`:** `ops-thermals` and `ops-load` must drop `padding:{left:6,right:6}`
    from `xAxisProps` (nested keys named left/right are blocked and raise a yellow alert) [V].
13. **Safest install path is the in-app import (§6.7a),** then the admin-API path, then direct SQLite with a backup.
14. **Unverified and important:** which image the live container runs, real-board rendering inside the zoomed canvas, and every live
    API call. See §7.

---

## 1. Environment and provenance

| Fact | Value | Evidence |
|---|---|---|
| Homarr checkout | `/home/ohmz/StudioProjects/homarr`, branch `feat/v2-integration`, HEAD `64cd2cb07`, clean tree, `node_modules` present | [V] `git status` |
| Custom widget runtime | `@homarr/custom-widgets` (catalog 2.0.0), Mantine 9.6.0, acorn 8.16.0 + acorn-jsx 5.3.2; 332 enabled components | [S] `packages/custom-widgets/src/core/component-catalog.generated.json` |
| Renderer | first-party safe interpreter (`packages/custom-widgets/src/jsx/*`), NOT react-jsx-parser (that package is gone from `node_modules`) | [V] `ls node_modules/react-jsx-parser` fails |
| Node / tooling | Node v22.23.2, `tsx` at `homarr/node_modules/.bin/tsx`, Playwright Chromium in `~/.cache/ms-playwright`, `/usr/bin/google-chrome` | [V] |
| Live Homarr | container `homarr`, `--network host`, nginx on `:7575`, app `:3000`, ws `:3001`, Redis `:6379` listening on the host | [S] `deploy-homarr.sh:106-120`, [V] `ss -ltn` |
| Live DB | `/data/compose/5/homarr/appdata/db/db.sqlite`, 7.5 MB, root:root 0644, directory root:root 0755; no `-wal`/`-shm`/`-journal` sidecar present at 14:45 | [V] `ls -la` (directory listing only) |
| DB copy used for research | `/tmp/claude-1000/-home-ohmz-StudioProjects/a892927f-f870-4100-8cb1-f2b3f8ad4df8/scratchpad/homarr-v2.sqlite` (copied 14:01), `journal_mode=delete`, header bytes 18-19 = `01 01` | [V] |
| Ops service | `homelab-maint-www.service` -> `python3 -B -m homelab_maint.server` on `127.0.0.1:9111`, HTTP/1.0, one request per connection, `Content-Type: application/json; charset=utf-8`, every route answers 200 JSON except unknown paths (404 `{"error":"not found"}`) and non-GET (405) | [S] `homelab_maint/server.py:1-140`, [V] curl |
| Current route status | `/overview` 515 B, `/disk` 1419 B, `/jobs` 1495 B, `/guard` 867 B, `/reclaim` 310 B, `/status` 20 KB, each ~1 ms; `/thermal` and `/load` 404 | [V] curl |
| User | `ohmz_h` id `zfsvodfcjkff593sn2plt7u9`, member of group `credentials-admin` which holds permission `admin`; no API keys exist yet | [V] DB copy |
| Existing v2 rows | 4 `seed-*` bundled widgets (disabled), Thermals, Fan Control, 4 `Fan Max:` presets; 5 secret rows (X-Fan-Token, one per widget with an action source); none of the seven ops widgets is installed anywhere | [V] DB copy |

---

## 2. The v2 definition format

### 2.1 Shape (annotated; not valid JSON because of the comments)

```jsonc
{
  "$schema": "homarr-custom-widget-v2",        // literal, required
  "name": "Ops Overview",                      // trimmed, 1..128
  "description": "...",                        // optional, <= 512; OMIT instead of null
  "iconUrl": "https://...",                    // optional absolute http(s) URL, <= 2048; omit if unused
  "sources": {                                 // keyed, <= 8, MUST contain "default"
    "default": {
      "baseUrl": "http://127.0.0.1:9111",      // http(s) only, no userinfo, no query/fragment
      "networkScope": "loopback",              // REQUIRED: public | private | loopback
      "auth": "none"                           // none | bearer | basic | {type:"apiKeyHeader",name} | {type:"apiKeyQuery",name}
    }
  },
  "requests": {                                // keyed, <= 64
    "state": {                                 // request id = the key you read as data.state
      "kind": "query",                         // query | action  (there is NO "mutation")
      "method": "GET",                         // GET POST PUT DELETE PATCH, uppercase
      "path": "/overview",                     // starts with "/", appended to baseUrl; no "?" and no "#"
      "source": "default",                     // default "default"
      "trigger": "load",                       // load | manual (query default load; action is forced manual)
      "cacheSeconds": 5                        // integer 0..3600, server-side, in-process
    }
  },
  "options": {},                               // keyed, <= 64; the ops widgets define none
  "template": "<Stack>...</Stack>"             // one JSX expression, 1..50,000 chars
}
```

Defaults the schema fills in when you omit them: `source: "default"`, `kind: "query"`, `method: "GET"`, `auth: "inherit"` (request),
`trigger: "load"` for queries, `permission: "view"` for queries, source `auth: "none"`, `options: {}`. `networkScope` has
no default and `sources.default` is mandatory [S `request-schema.ts:56-60,155-206`] [V].

### 2.2 The exact transfer/import JSON for `ops-overview`

This is the file the in-app Import button accepts and the `widget` member of the `customWidget.import` call. It was produced by
the mechanical rule in §5.1 from `widgets/ops-overview.jsx` and passes both `customWidgetDefinitionSchema` and the stricter
`customWidgetPreviewDefinitionSchema` with 0 analyzer diagnostics, renders in jsdom and in real Chromium [V]. The recommended
final variant (index keys + status banner, §5.6) differs only in the template.

```json
{
 "$schema": "homarr-custom-widget-v2",
 "name": "Ops Overview",
 "description": "Homelab maintenance runner: overall health, root disk, memory, space freed, and the problems that need a look.",
 "sources": {
  "default": {
   "baseUrl": "http://127.0.0.1:9111",
   "networkScope": "loopback",
   "auth": "none"
  }
 },
 "requests": {
  "state": {
   "kind": "query",
   "method": "GET",
   "path": "/overview",
   "source": "default",
   "trigger": "load",
   "cacheSeconds": 5
  }
 },
 "options": {},
 "template": "<Stack gap={7} p={2} style={{opacity:data.state.stale?0.55:1}}>\n<Group justify=\"space-between\" align=\"center\" wrap=\"nowrap\" gap={8}>\n<Group gap={7} wrap=\"nowrap\" align=\"center\">\n<ColorSwatch color={\"var(--mantine-color-\"+(data.state.c||\"gray\")+\"-\"+(data.state.c===\"red\"?6:5)+\")\"} size={8} withShadow={false} />\n<Text fz={11} fw={800} tt=\"uppercase\" lts=\"0.18em\">Ops</Text>\n</Group>\n<Group gap={5} wrap=\"nowrap\">\n{data.state.paused&&<Badge size=\"xs\" radius=\"sm\" variant=\"filled\" color=\"orange\">paused</Badge>}\n{data.state.stale&&<Badge size=\"xs\" radius=\"sm\" variant=\"filled\" color=\"gray\">{data.state.ago&&data.state.ago!==\"never\"?\"stale \"+data.state.ago:\"no data\"}</Badge>}\n<Badge size=\"xs\" radius=\"sm\" variant=\"light\" color={data.state.c||\"gray\"}>{data.state.head||\"no data\"}</Badge>\n</Group>\n</Group>\n{data.state.error&&<Text fz={11} c=\"orange.5\" lineClamp={2}>{data.state.error}</Text>}\n<SimpleGrid cols={3} spacing={6}>\n{(data.state.tiles||[]).map(x=>\n<Paper key={x.l} radius=\"md\" p={8} bg=\"light-dark(rgba(0,0,0,0.035),rgba(255,255,255,0.045))\" style={{border:\"1px solid light-dark(rgba(0,0,0,0.09),rgba(255,255,255,0.08))\"}}>\n<Text fz={10} fw={700} c=\"dimmed\" tt=\"uppercase\" lts=\"0.12em\" truncate=\"end\">{x.l}</Text>\n<Text fz={18} fw={700} lh={1.15} c={x.c+\".5\"} truncate=\"end\">{x.v}</Text>\n<Text fz={10} c=\"dimmed\" truncate=\"end\">{x.s}</Text>\n</Paper>\n)}\n</SimpleGrid>\n{data.state.level&&data.state.level!==\"none\"&&!data.state.error&&data.state.sub&&(data.state.issues||[]).length===0&&<Text fz={12} c=\"dimmed\" ta=\"center\" py={4}>all checks clear</Text>}\n{(data.state.issues||[]).map(x=>\n<Group key={x.n} gap={7} wrap=\"nowrap\" align=\"flex-start\">\n<ColorSwatch color={\"var(--mantine-color-\"+x.c+\"-\"+(x.c===\"red\"?6:5)+\")\"} size={8} withShadow={false} mt={4} />\n<Stack gap={0} style={{flex:1,minWidth:0}}>\n<Text fz={12} fw={700} lh={1.2} truncate=\"end\">{x.n}</Text>\n<Text fz={11} c=\"dimmed\" lh={1.25} lineClamp={2}>{x.s}</Text>\n</Stack>\n</Group>\n)}\n<Divider />\n<Group justify=\"space-between\" align=\"center\" wrap=\"nowrap\" gap={6}>\n<Group gap={5} wrap=\"nowrap\">\n{(data.state.tiers||[]).map(x=><Badge key={x.n} size=\"xs\" radius=\"sm\" variant=\"light\" color={x.c}>{x.n+\" \"+x.a}</Badge>)}\n</Group>\n<Text fz={10} c=\"dimmed\" truncate=\"end\">{data.state.ap_all>0?\"cleanup apply \"+data.state.ap_on+\"/\"+data.state.ap_all:\"\"}</Text>\n</Group>\n<Text fz={10} c=\"dimmed\" lh={1.2} truncate=\"end\">{data.state.sub}</Text>\n</Stack>"
}
```

The template is a single JSX expression. `build_widgets.py` must keep emitting it with `squash()` (strip indentation and blank lines),
`ensure_ascii=False` (ops-disk contains a literal U+00B7), one trailing newline in the file. NFC and U+200B are normalised by the
schema; the stored text must already be NFC [S `custom-jsx-schema.ts:22-26`].

### 2.3 Field reference and limits

All limits are enforced by zod `strictObject`s; **unknown keys are errors at every level** [S].

| Field | Rule | Source |
|---|---|---|
| `$schema` | literal `"homarr-custom-widget-v2"` | `custom-jsx-schema.ts:78` |
| `name` | trimmed, 1..128 (Postgres column is 256; 128 is the effective cap) | `:79` |
| `description` | optional, <= 512, empty allowed; `null` rejected | `:80` |
| `iconUrl` | optional, valid http(s) URL <= 2048, no credentials, no credential-looking query | `:81-89` |
| `template` | 1..50,000 chars AFTER NFC / zero-width normalisation; 50,000 passes, 50,001 fails [V] | `:20,44` |
| `templateLines` | authoring-only alternative to `template` for `create`/`update` (1..2,000 lines, each <= 10,000); NOT allowed in the import JSON | `:234-246` |
| ids (source, request, option) | `^[A-Za-z][A-Za-z0-9_-]*$`, 1..64, unique case-insensitively for sources; a dot is rejected, so `ops.state` is invalid | `request-schema.ts:11-15` |
| sources | <= 8, `default` required | `:84-85` |
| requests | <= 64 | `:229` |
| options | <= 64 | `options-schema.ts` |
| request `path` | starts with `/`, <= 2048, not `//`, no `\`, no `#`, braces only as `{option:name}` or `{param:name}` | `request-schema.ts:161-173` |
| request `query` | record of bound values; values must resolve to string, number or boolean at run time | `:174`, `request-manifest.ts` |
| request `cacheSeconds` | integer 0..3600; 3601, 1.5, -1 rejected [V] | `:179` |
| request headers | <= 32, value <= 8192; names must not be reserved (authorization, cookie, host, forwarded, via, te, upgrade, content-length, connection...), must not start with `proxy-`, `sec-`, `x-forwarded-`, must not contain api-key, authorization, password, secret, token | `:107-143` |
| `kind` | `query` or `action` only; `mutation` is rejected [V] | `:159` |
| `trigger` | `load` or `manual` only; `interval`, `click`, `auto` rejected [V]. There is no per-request poll interval | `:173` |
| `method` | `GET POST PUT DELETE PATCH`; `DELETE` must be an action | `schema-types.ts:4`, `request-schema.ts:182-190` |
| source `networkScope` | required; `public`, `private`, `loopback` | `request-schema.ts:59` |
| source `baseUrl` | see §2.5 | `url-policy.ts`, `request-schema.ts:29-47` |
| source `auth` | `"none"` (default), `"bearer"`, `"basic"`, `{type:"apiKeyHeader",name}`, `{type:"apiKeyQuery",name}`; the bare strings `"apiKeyHeader"` / `"apiKeyQuery"` fail | `:23-27` |
| secrets (create/import) | <= 24, value 1..8192; not applicable here (auth none) | `custom-jsx-schema.ts:57-74` |

Cross-validation that also runs on every parse [S `custom-jsx-schema.ts:105-173,206-229`]: unknown source or unknown `$option`
name is an error; `$param`/`{param:}` in a `load` request is an error; `invalidates` targets must be queries; every
`<SubFetch>`/`<ActionButton>`/`<ToggleSwitch>` needs a literal quoted `requestId` that exists (SubFetch needs a manual query, the other
two need an action).

### 2.4 Request execution limits the builders must respect

| Limit | Value | Source |
|---|---|---|
| response body | <= 1 MiB (content-length, streaming and after decompression) | `server/response.ts:6` |
| parsed JSON | depth <= 32, <= 50,000 nodes | `response.ts:7-8` |
| request body | <= 10 KiB | `request-executor.ts:34` |
| timeouts | 10 s per hop (connect, headers, body); 45 s total | `request-executor.ts:35-36` |
| redirects | queries without auth follow <= 3 same-origin redirects; cross-origin is FORBIDDEN; actions and authenticated queries follow none | `request-executor.ts:99-160` |
| response cache | in-process `Map`, <= 1000 entries, only 2xx responses, keyed by item id + hash(sources, requests, secrets) + request id + params | `request-executor.ts:37,42` |
| rate (per 60 s) | per user+item: 60 queries, 10 actions, 3 deletes, 60 metadata; per definition (all users and items): 240 / 40 / 12 / 240; cache hits count | `server/request-limits.ts:12-13` |
| concurrency | 4 per user+item, 8 per definition, 60 s lease; **all load queries of one widget run in parallel, so keep <= 4 per widget** | `request-limits.ts:9-11`, `custom-api.ts:195-232` |
| limiter store | Redis in production (error "Request limiter is unavailable" if Redis is unreachable) | `api/src/router/custom-widget/request-limits.ts:45-58` |
| HTTP non-2xx | not thrown: `status.<id> = {ok:false,status,statusText,error:"HTTP <code>: <text>"}` and `data.<id>` = the parsed body | `custom-api.ts:200-215` |
| transport failure | `status.<id> = {ok:false,status:0,error:"External request failed"}` (or "...timed out", "...exceeded the total time limit", "Target address is not allowed by the loopback network scope") and `data.<id> = null` | `custom-api.ts:216-231` |
| body parsing | `JSON.parse` first regardless of Content-Type; a `json` Content-Type with invalid JSON is an error "Upstream returned invalid JSON" | `response.ts:51-85` |

The ops server satisfies all of these: answers in about 1 ms, payloads 0.3 to 12 KB, JSON content type [V].

### 2.5 `baseUrl` and `networkScope` for a host-network container

* Use the literal `http://127.0.0.1:9111`. Accepted [V]: `127.0.0.1`, `localhost`, `[::1]`, `host.docker.internal`, port `0`, a trailing slash, a base path. Rejected [V]: `127.1`,
  `2130706433`, `:09111`, `:65536`, userinfo, any query string or fragment, `ftp:`; rejected by the source rules [S]: `0x7f.0.0.1`, `%` in the authority, backslashes, control or zero-width characters
  [S `url-policy.ts:21-116`]. A base path (`http://127.0.0.1:9111/api`) is allowed and the request path is appended.
* `localhost` is schema-valid but may resolve to `::1` first and the executor pins the first validated address; stay with `127.0.0.1` [U].
* Scope is evaluated on the RESOLVED address: loopback needs `networkScope:"loopback"`; `public` or `private` against `127.0.0.1`
  fails at run time with FORBIDDEN "Target address is not allowed by the public network scope" [V `classifyAddress`, S
  `server/network-policy.ts:33-120`]. Always blocked in every scope: `0.0.0.0/8`, `100.64/10`, `169.254/16`, doc/test ranges,
  multicast, `::`, `fe80::/10`, any `::ffff:` mapped address. There is no port allow-list: 9111 needs no exception.
* With `--network host` the container shares the host loopback [S `deploy-homarr.sh:106-120`]; the existing Thermals row
  (`127.0.0.1:9110`, loopback) is the working precedent [V DB copy]. Whether it works today is [U] (live not called).
* The import dialog forces the admin to tick a URL confirmation for any non-public source before the Import button enables
  [S `packages/custom-widgets/src/workbench/source-setup.tsx:56,63-70`]. The API path has no such step.

### 2.6 What the app stores in the database (`custom_widget_v2_definition`)

| Column | Value written by `customWidget.import` | Notes |
|---|---|---|
| `id` | `createId()` (cuid2, 24 chars, lowercase alnum) | any string <= 64 works; avoid the `seed-` prefix (re-created by the seeder) |
| `name`, `description`, `icon_url` | text; NULL when absent | |
| `sources` | superjson string, e.g. `{"json":{"default":{"baseUrl":"http://127.0.0.1:9111","networkScope":"loopback","auth":"none"}}}` | |
| `requests` | `{"json":{"state":{"source":"default","kind":"query","method":"GET","path":"/overview","trigger":"load","auth":"inherit","cacheSeconds":5,"permission":"view"}}}` | parsed form (defaults filled); the minimal form also parses [V] |
| `options` | `{"json":{}}` | |
| `template` | plain text (not superjson) | |
| `enabled` | 1 | |
| `created_at`, `updated_at` | Unix SECONDS | the app compares `configurationVersion` to `updated_at` in ms; irrelevant when the item omits it |
| `creator_id` | the admin user id (FK to `user`, SET NULL on delete) | `zfsvodfcjkff593sn2plt7u9` |

Secrets live in `custom_widget_v2_secret(definition_id, source_id, kind, encrypted_value, updated_at)`; the ops widgets need none (auth none)
[S `packages/db/migrations/sqlite/0042_custom_widget_v2_tables.sql:1-24`, `packages/api/src/router/custom-widget/definition-insert.ts:10-43`,
`stored-definition.ts:21-31`] [V: `validate-definition.ts --columns` prints the parsed, superjson-serialised column strings as shown above; the rehearsal in §6.7c wrote them to a DB copy and read them back].

### 2.7 Common rejection messages

| Message (abridged) | Cause |
|---|---|
| the current `widgets/ops-overview.json` fed to the importer [V]: `sources: Invalid input: expected record, received undefined`, same for `requests` and `template`, and `UNSUPPORTED_FIELD: Unrecognized keys: "url", "authType", "method", "displayType", "displayConfig"` | a v1 file in the import path |
| `REMOVED_LOCAL_STATE: stateSchema is not supported...` (also `defaultState`) | v1 local-state keys; checked before the schema [S `import.ts:59-70`] [V] |
| `Unrecognized key: "foo"` | any extra key at any level, for example inside a request [V] |
| `[requests.state.kind] Invalid option: expected one of "query"\|"action"` | used `mutation` |
| `[requests.state.trigger] expected one of "load"\|"manual"` | used `interval` or `click` |
| `Invalid input` on `auth` | bare `"apiKeyHeader"` string |
| `API source URLs cannot contain a query string or fragment` | `?` or `#` in `baseUrl` |
| `Request paths cannot contain URL fragments` / `#` | `#` in `path`; a literal `?` in `path` passes but is percent-encoded to `%3F` at run time, so use `query` [V `renderRequestTarget`] |
| `Credentials must use source authentication` | credential-literal heuristic (§3.8) |
| `UNKNOWN_COMPONENT: 'Foo' is not available. Did you mean 'Box'?` | not in the 332-component registry |
| `BLOCKED_CAPABILITY: Prop 'onClick' is not allowed` | `on*`, `className`, `ref`, `pos/top/left/right/bottom/inset/zIndex`, `children`, `component`, `form`... |
| `UNSUPPORTED_BLOCK_STATEMENT: 'NewExpression' is not allowed` | `new Date(...)` |
| `INVALID_LOCAL_DECLARATION: Callback parameters must be identifiers` | destructuring or default parameter in an arrow |
| `RESERVED_LOCAL_BINDING: 'data' cannot be shadowed` | callback parameter named `data`, `status`, `options`, `inputs` |
| `Reflective property access is not allowed` | data field or identifier named `constructor`, `__proto__`, `prototype`, `bind`, `call`, `apply`, `arguments`, `caller`, `callee` |
| `Unknown binding 'window'` | any identifier other than the roots in §3.2 |
| `Calling method 'toFixed' is not allowed` (runtime) | method call on `undefined` or on the wrong receiver type (§3.4) |
| `Objects are not valid as a React child` (runtime) | rendering an object or an array of objects directly |

---

## 3. Template runtime rules (checklist)

### 3.1 Where each rule is enforced

1. **Save, import, update, preview:** `customJsxTemplateSchema` runs `validateCustomJsxTemplate` (acorn + acorn-jsx AST walk, errors fail the
   schema with "(line L, column C)"); warnings do not [S `custom-jsx-schema.ts:38-55`].
2. **Every `getData`:** the stored row is parsed again with the same schema [S `stored-definition.ts:33-44`]; a failure is INTERNAL_SERVER_ERROR,
   the tile shows the generic request-failed message and retries up to 3 times [S `custom-api.ts`, `component.tsx:36-41`].
3. **Render:** the interpreter re-checks everything at run time and is slightly more lenient (blocked props are stripped with a yellow
   alert, unknown components are dropped with a warning) but fatal problems show a red `RUNTIME_RENDER_ERROR` alert inside the tile
   [S `runtime/custom-jsx-renderer.tsx:50-60,221-247`] [V].
4. A change that must keep rendering has to pass **both** the analyzer and the runtime; the harness (§4) checks both.

### 3.2 Bindings (the only identifiers a template can read)

| Root | Meaning |
|---|---|
| `data.<requestId>` | the untouched response body of each LOAD query (`null` when the request failed at transport level; the parsed error body on HTTP non-2xx) |
| `status.<requestId>` | `{loading, ok, status, statusText, error}`; for load queries `loading` is always `false` because `getData` only returns when all queries finished |
| `options.<name>` | the widget's saved options (none for the ops widgets) |
| `inputs.<name>` | temporary `bind` values (unused here) |
| globals | `String Number Boolean Math JSON Array Object Date parseInt parseFloat encodeURIComponent decodeURIComponent isNaN isFinite undefined NaN Infinity` [S `analyzer-language.ts:5-27`] |

`data` is deep-sanitised before binding: depth <= 32, <= 50,000 nodes, string <= 200,000 chars, and the keys `constructor`, `__proto__`,
`prototype` are DROPPED from every object [S `jsx/bindings.ts:4-8,16-95`]. Do not name a payload field with those words.
Callback parameters may not shadow `data`, `status`, `options`, `inputs` [V] and may not be named after a reflective property (`call` [V]; also `bind`, `apply`, `arguments`,
`caller`, `callee`, `constructor`, `prototype`, `__proto__` [S `jsx/policy.ts:10-20`, `isBlockedCustomJsxLexicalBinding`]); `name`, `value`, `key`, `item`, `x`, `i` are fine [V case 71s].

### 3.3 Syntax

Allowed [V]: one JSX expression (several sibling roots, bare text or a bare `{expr}` are legal; the source is parsed as `<>template</>`),
JSX comments `{/* */}`, spread attributes `{...{c:"red"}}`, boolean shorthand attributes, fragments `<>`, member components
(`Table.Tr`), template literals including nesting, ternaries, `&&`/`||`/`??`, optional chaining `a?.b` and `a?.[0]`, array and object
literals including spread and computed keys, arrow callbacks with expression bodies and identifier parameters `(x, i) =>`,
`**`, `%`, unary `! + - typeof`, comparisons incl. loose `==`, regex literals <= 128 chars with a conservative shape.

Forbidden [V by probe; `await`, `void`, `delete` and the exact operator lists follow from `jsx/analyzer.ts` and `jsx/safe-language-policy.ts` (S)]: `import`, hooks, refs, raw HTML tags (`<div>`; only registered components), `new`, assignments, `++`/`--`, `await`,
sequence expressions, tagged templates, `in`, bitwise operators, `void`/`delete`, block-bodied arrows (`=> {`), IIFEs, optional calls
`f?.()`, async or generator callbacks, destructured or default-valued callback parameters, shorthand object properties `{a}`,
getters/methods in object literals, callbacks stored in variables or passed outside the first callback slot, `forEach`,
`Object.fromEntries`, `Math.trunc/log/...` (only `round floor ceil abs min max pow sqrt PI`), `Number.isFinite` (use the global `isFinite`),
`React.Fragment`, namespaced attributes (`xlink:href`), `eval`, `fetch`, `window`, `document` as identifiers.
The old v1 forbidden-WORD scan is gone: `window document fetch eval constructor prototype Function require globalThis` are harmless
as plain text and as string literals, they only fail as identifiers or property names [V case 18].

Inside text: a bare `>`, `<` or `}` is a parse error; write `&gt;`, `&lt;`, `{">"}`, or `{"}"}`. HTML entities (`&amp;`, `&nbsp;`, `&middot;`) work.
JS comments (`// x`) are plain text, not comments [V cases 17, 70].

### 3.4 Methods and null-safety (the one that crashes tiles)

* **Property access is null-safe:** `data.state.x`, `data.state.a.b.c`, `data.nothing?.a` return `undefined` when an ancestor is `null`
  or `undefined`; only reflective names throw [S `safe-properties.ts:31-42`] [V].
* **Method calls are NOT null-safe.** Calling any method on `undefined`/`null` throws "Calling method 'X' is not allowed" and kills the render.
  Always guard: `(data.state.rows||[]).map(...)`, `Number(data.state.t).toFixed(1)`, `(data.state.n||0).toFixed(0)` [V cases 08, 60k].
* **The analyzer only checks method NAMES; the runtime checks the RECEIVER TYPE.** These pass the schema and then fail at run time [V]:
  `"abc".at(0)`, `"a".concat("b")`, `[1,2].toString()`, `[1,2,3].lastIndexOf(2)`, `"x".toLocaleString()`, `(5).padStart(2,"0")`.
* Works [V]: arrays `map filter flatMap find findIndex some every reduce sort((a,b)=>..) reverse slice pop concat join includes indexOf at flat`;
  strings `toUpperCase toLowerCase trim trimStart trimEnd replace replaceAll indexOf lastIndexOf repeat slice substring includes startsWith endsWith
  padStart padEnd charAt localeCompare split match search` (regex args must be literals); numbers `toFixed toString(radix) toLocaleString toPrecision`.
  `toLocaleString("de-DE", {...})` ignores locale and options and prints `1,234.5` [V case 60f].
* `sort` and `reduce` need an inline arrow; `sort()` with no callback fails [V]. `Array.from` caps at 2,000 items [S `bindings.ts`].
* Dates: only the static helpers `Date.now() Date.create(v) Date.toISOString(v) Date.toLocaleString(v, locale, tz) Date.toLocaleDateString(v, locale)
  Date.toLocaleTimeString(v, locale) Date.getTime Date.getYear Date.getMonth Date.getDay`. An invalid input (for example `undefined`) throws
  `RangeError: Invalid time value` and kills the render [V case 09]. The ops payloads pre-compute ages and times, so avoid dates.
* Rendering: objects, and arrays of objects, are not valid React children (red `RUNTIME_RENDER_ERROR`); `false null undefined ""` render nothing; `0` and `NaN` render as text [V cases 32, 60n].
* Missing `key` on a mapped element is a React console warning only [V case 60h]; duplicate keys are an error in the console and may duplicate or omit children (§5.6).

### 3.5 Components and props

* 332 enabled component names (Mantine core 266, charts 25, dates 29, Homarr 12), 143 denied [S `component-catalog.generated.json`].
  Homarr-specific: `ActionButton Collapsible PaginatedList RefreshButton StatBar SubData SubFetch TabPanel TablerIcon TabsContainer ToggleSwitch TypeBadge`;
  `Icon` is an alias of `TablerIcon` and needs a registered kebab-case `name` (`server`, `thermometer`; not `IconServer`).
* Used by the seven widgets and verified: `Stack Group Text Badge ColorSwatch Divider SimpleGrid Paper Progress Sparkline LineChart` (plus `Tooltip`, `Table.*`, `RingProgress`, `AreaChart`, `BarChart`, `Skeleton`, `Alert`, `ScrollArea` verified in probes).
* Denied (error `BLOCKED_CAPABILITY` or `UNKNOWN_COMPONENT`), because they escape the tile or need authored callbacks: `Modal`, `Drawer`, `Dialog`, `Affix`, `AppShell`, `ActionBar`, `Portal`/`OptionalPortal`,
  `Combobox*`, `Input`/`InputBase`/`Input.*`, `PillsInput`, `Menubar`, `FileInput`/`FileButton`, `FocusTrap`, every `*Provider`/`*Context`, `Transition`, `InlineStyles`, `RemoveScroll`, `FloatingWindow`,
  `TableOfContents` [S `packages/custom-widgets/src/core/component-catalog-policy.ts:1-30`]. `Popover`, `HoverCard`, `Tooltip`, `Menu`, `Collapse`, `Tabs`, `Accordion` are enabled in the catalog [S]; `Tooltip`, `Popover`, `Tabs`, `Accordion` rendered in the probes [V cases 30, 61b, 74, 75].
* Props: ordinary Mantine style props (`p mt c fz fw tt lts lh w h miw maw flex ta py px bg radius size gap`) and component props pass through.
  An unknown prop is a WARNING only (`UNKNOWN_MANTINE_PROP`, e.g. `lineProps` on `LineChart`, which Mantine does support) and does not block saving [V].
* **Blocked props (error):** any name starting with `on` (any case), any name containing `Ref` or starting with `ref` + capital letter, `ref`, `__*`, `children`, `className`, `classNames`, `classes`, `component`,
  `form`, `formAction`, `formEncType`, `formMethod`, `formNoValidate`, `formTarget`, `dangerouslySetInnerHTML`, `innerRef`, `autoFocus`, `contentEditable`, `ping`, `popover`, `popoverTarget`, `popoverTargetAction`,
  `portalProps`, `renderRoot`, `srcDoc`, `suppressContentEditableWarning`, `suppressHydrationWarning`, `withinPortal`, the reflective names, and the position props `pos top left right bottom inset zIndex`
  [S `jsx/policy.ts:24-110`] [V]. `Tooltip target` and the date pickers' `dropdownType`/`modalProps` are also blocked.
* **Nested keys in prop objects are checked too** (not in `data=`/`series=`/`style`): a nested key named `left`, `right`, `top`, `bottom`, `pos`, `inset`, `zIndex`, `on*`, `*Ref`,
  `className`, `children`, `component` is dropped and raises a yellow "N template warnings" alert. This is why `xAxisProps={{padding:{left:6,right:6}}}` must go [V case 26f].
  A DATA key called `left` inside `data={[{x:1,left:2}]}` is fine [V case 60z].
* URLs (`href src image backgroundImage poster fallbackSrc`): literal values must be `#...`, `/...` (not `//`), or http(s) without credentials; `javascript:` and `data:` are rejected [V].
* `style={{...}}`: these keys are silently removed: `backdropFilter behavior bottom clipPath content filter inset left mask pointerEvents pos position right top zIndex`,
  and any string value containing `url(`, `expression(`, `javascript:` or `position:` [S `policy.ts:74-88`, `safe-properties.ts:60-72`] [V case 29].
  Everything else (gradients, `border`, `opacity`, `overflow`, `flex`, `whiteSpace`, `transform`) passes.

### 3.6 Container sizing, theme and layout

* The widget root is a Mantine `Card` with padding 0, `h=100%`, `w=100%`, `container-type: size`, `overflow-x: hidden; overflow-y: auto`
  [S `packages/widgets/src/widget-card-shell.tsx:13-18,56`, `apps/nextjs/src/components/board/items/item-content.tsx:327-345`].
  Inside it the renderer adds `Box h=100% overflow:auto; contain: layout paint style; isolation: isolate` [S `custom-jsx-renderer.tsx:221`].
  Content taller than the tile scrolls vertically inside the tile; nothing is clipped silently except horizontal overflow.
* Give the root `p={2}` (existing convention) and let content flow; use `h="100%"` on the root only when you want to fill the tile (Fan Control does).
  Charts need an explicit `h`; `h="100%"` has no parent height to resolve against inside a scrolling box.
* **Container queries work:** `<SimpleGrid type="container" cols={{base:1,"300px":2,"500px":3}}>`; `hiddenFrom`/`visibleFrom` also work; `cols={3}` fixed is what the ops widgets use [V cases 27, 60y].
* Theme tokens: use Mantine CSS variables and colour names so dark and light both work: `c="dimmed"`, `c="teal.5"`, `color="var(--mantine-color-teal-5)"`,
  `bg="light-dark(rgba(0,0,0,0.035),rgba(255,255,255,0.045))"`, borders via `light-dark(...)`. The existing ladder is teal -> yellow -> orange -> red (green is not used);
  a hard-coded hex or `rgb()` outside `light-dark()` is flagged by the harness as an advisory [V].
* The card background uses the board opacity (`--opacity`); do not paint a full-bleed opaque background.
* Fonts: sizes are logical pixels (`fz={10..21}`); the whole board is CSS-zoomed (§6.3), so sizes appear scaled but proportions are fixed.

### 3.7 Budgets (interpreter)

AST depth 64, 25,000 operations, 4,000 collection items, 10,000 rendered nodes, strings 200,000 chars, regex literal <= 128 chars
[S `jsx/policy.ts:1-8`]. Measured use of the seven ported widgets on worst-case fixtures: <= 1,356 operations, <= 111 rendered nodes, AST depth <= 34 [V]. Nothing is near a limit.

### 3.8 The credential-literal trap (highest-impact gotcha)

`validateCredentialFreeExport` runs inside `customWidgetDefinitionSchema.superRefine`, so it covers `template`, `description`, `name`, option labels and every
string in the definition, on create, import, update AND on every render parse [S `definition-security.ts:34-196`, `custom-jsx-schema.ts:99`].

A string fails when it contains one of: `authorization`, `auth`, `authentication`, `credential(s)`, `api key`, `password(s)`, `passwd`, `secret(s)`, `token(s)`,
`access|refresh token`, `access key`, `client secret`, `private key`, `signing key`, followed by optional quote/space, then `:` or `=`, then a value that is not one of
`anonymous authentication basic bearer configured default disabled enabled example false inherit missing none optional placeholder public redacted required separate separately source true unset` or empty.
`Bearer`/`Basic` followed by 8+ token characters, `sk-...`, `ghp_...`, JWTs and `AKIA...` also fail.

Results of probing the real schema [V `cred-probe.ts`]:

| Template text | Result |
|---|---|
| `<Text>Auth: {x}</Text>`, `Token: {x}`, `Secrets: 3`, `API key: abc`, `passwords = 0`, `private key: 1`, `sshd auth: {n}` | FAIL |
| `{data.state.token==="x"?1:0}` (comparison), `{JSON.stringify({token:1})}`, `{JSON.stringify({auth:1})}`, ternary `a?token:b` | FAIL |
| description `Failed auth: 3` | FAIL |
| `Secret: none`, `Password: required`, `authentication: enabled`, `Bearer token` (text), `keys: 3`, `ssh key: ok`, `fail2ban: 3` | OK |
| `{data.state.auth_failures}`, `{data.state.token}` (no colon/equals), `key={i}` | OK |
| description `auth failures and tokens`, name `Token Vault` | OK |

Rule for builders: never write a label of the form `<credential word>: <anything dynamic or non-harmless>` anywhere, never compare a field named like those words with `=`/`==`/`===`,
and never use such words as object-literal keys in a template. Name payload fields without them (for example `auth_failures` is fine only as a bare property read).
The same words are also rejected as definition keys and as option names (`token`, `secret`, `password` options fail "Credentials must use source authentication") [S `definition-security.ts:48-60`].

### 3.9 Loading, empty and error states

| Situation | What the template sees | What the user sees |
|---|---|---|
| first fetch in flight | template not rendered | Homarr `WidgetQueryLoadingState` (not a template state) |
| healthy | `data.state` = body, `status.state.ok===true` | template |
| ops service up but no status yet | HTTP 200 body `{"error":"no status yet","stale":true,...}`; `status.state.ok===true` | the template's `data.state.error` line and the stale badge |
| service down / timeout / blocked | `data.state===null`, `status.state={ok:false,status:0,error:"External request failed"}` | "no data" badge unless the template shows `status.state.error` |
| HTTP 404/500 | `data.state` = parsed body (for example `{"error":"not found"}`), `status.state.ok===false`, `error:"HTTP 404: Not Found"` | the v1 `data.state.error` line shows "not found" |
| widget disabled / board not viewable / definition missing | `getData` throws NOT_FOUND or FORBIDDEN | `Unavailable` tile "definition not found" + remove button; polling stops permanently |
| stored row no longer parses | INTERNAL_SERVER_ERROR | generic red-triangle "request failed"; retries 3 times, keeps polling every interval |
| item still points at a legacy-only id | PRECONDITION_FAILED `LEGACY_CUSTOM_WIDGET_MIGRATION_REQUIRED` | "migration required" tile; polling stops |
| template runtime error | n/a | red `RUNTIME_RENDER_ERROR` alert in the tile |
| template warnings | n/a | yellow "N template warnings:" alert listing <= 5 distinct messages |

[S `packages/widgets/src/custom-api/component.tsx:44-79`, `migration-state.ts`, `custom-api.ts:67-110,214-231`] [V scenarios via the harness]. Load queries never expose `status.<id>.loading===true`,
so skeleton states are pointless for them.

### 3.10 Gotchas collected from 158 behaviour probes [V]

* `(x||[]).map(...)` is mandatory for lists; `x.map` on `undefined` kills the tile.
* Callback parameters: `x`, `(x,i)`; never `({a})`, `([k,v])`, `(x=2)`. `Object.entries(o).map(e => e[0])` instead of destructuring.
* Do not use `key={x.n}` for server-truncated names; use the index.
* `{data.state.fans}` (an object) as a child crashes; use `JSON.stringify` or map it.
* `Math.round(NaN)` etc. are fine and render `NaN`; avoid showing `NaN`/`undefined` (the harness flags them as text).
* `style={{opacity:cond?0.55:1}}` and responsive props (`fz={{base:10,sm:12}}`) work.
* A single `LineChart` series name like `left` is fine; `referenceLines={[{x:2,color:"gray.5",strokeDasharray:"3 3"}]}` is fine.

---

## 4. Real-runtime validation method

### 4.1 The working harness (use this; `check_templates.mjs` is dead)

`widgets/check_templates.mjs` needs `react-jsx-parser`, `jsx-whitelist.ts` and `packages/validation/src/custom-widget.ts`, none of which exist in v2: it exits 3
("cannot load the fork's node_modules") [V]. `build_widgets.py --check` and `tests/test_payloads.py` depend on it (§4.5).

The replacement runs the REAL sources (schema + analyzer + interpreter + Mantine + `CustomJsxRenderer`) and optionally a real Chromium:

```
/tmp/claude-1000/-home-ohmz-StudioProjects/a892927f-f870-4100-8cb1-f2b3f8ad4df8/scratchpad/homarr-research/
  rc.sh              wrapper: tsx + the right tsconfig (jsx: react-jsx), NODE_NO_WARNINGS, hard timeout (RC_TIMEOUT, default 120 s)
  render-check.mjs   656 lines; the harness
  preview-entry.tsx  113 lines; browser entry bundled by esbuild for --shot
  validate-definition.ts   schema-only check (§4.2)
```

Copy `rc.sh`, `render-check.mjs`, `preview-entry.tsx` (and `validate-definition.ts`) into `widgets/` (they are NOT in the repo yet; this document was told to
create no other repo file). `rc.sh` creates a `node_modules` symlink next to itself pointing at the Homarr checkout's `node_modules`; `REPO` is hard-coded to `/home/ohmz/StudioProjects/homarr`
in both `.mjs` and `.tsx` (make it `$HOMARR_FORK` when productising). Requires Node >= 22 (`node:sqlite`) and Playwright's Chromium or `/usr/bin/google-chrome` for `--shot`.

Usage (all read-only; the DB is opened `readOnly`):

```bash
cd widgets   # after copying
# 1. a definition file, one fixture per load request (the body of the response)
./rc.sh --definition v2/ops-overview.json --fixture state=fixtures/overview.json --text
# 2. straight from a database copy (reads custom_widget_v2_definition through superjson, like the server)
./rc.sh --db /path/to/copy.sqlite --name "Ops Overview" --fixture state=http://127.0.0.1:9111/overview
# 3. failure scenarios the board can produce
./rc.sh --definition v2/ops-disk.json --fixture state=fixtures/disk.json --scenario network-error   # data=null, status.ok=false
#    scenarios: ok | http-error | network-error | empty | null
# 4. a real-Chromium screenshot at a logical tile size and colour scheme, with scroll/overflow metrics
RC_TIMEOUT=180 ./rc.sh --definition v2/ops-thermals.json --fixture state=fixtures/thermal.json \
    --shot /tmp/thermals.png --size 626x414 --scheme dark
# 5. machine-readable report; exit 0 = pass, 1 = fail
./rc.sh --definition ... --fixture ... --json --text
```

What it reports and fails on: full-definition schema AND the stricter preview schema; analyzer errors and warnings; interpreter result and unknown/blocked
warnings; budgets actually used (binary-searched minimum); jsdom render through `CustomJsxRenderer` with the real `CustomWidgetRuntimeProvider`; `RUNTIME_RENDER_ERROR`;
the yellow template-warnings alert; React/Mantine console errors; with `--shot`: Chromium console, `contentScrollHeight`, `contentOverflowsTile`, `horizontalOverflow`, SVG count;
advisories (hard-coded colours, silently stripped style keys, > 4 load requests). `failed` = any of schema/analyzer/interpreter/warnings/runtime error/yellow alert/console `error:`/shot problems.
It cannot see: the live board's zoomed canvas, real network, live data, the host browser's fonts.

### 4.2 Minimal schema-only check (no browser, ~1 s)

```bash
/home/ohmz/StudioProjects/homarr/node_modules/.bin/tsx validate-definition.ts widgets/ops-overview.json [--columns]
```

It imports `customWidgetDefinitionSchema` and `customWidgetPreviewDefinitionSchema` from
`packages/custom-widgets/src/core/custom-jsx-schema` and `validateCustomJsxTemplate` from `src/jsx/analyzer`, prints PASS/FAIL, the analyzer diagnostics, and with `--columns`
the three superjson column strings that the app would store. Exit 0 = pass. This is exactly what the server re-runs on every render.

### 4.3 Required test matrix for each of the seven widgets

1. Schema + preview schema + analyzer: zero errors (the two `lineProps` warnings on thermals/load are accepted).
2. Fixture states from `build_widgets.payload_cases()` / `build_metrics_widgets.payload_cases()` (54 payloads: ok, warn, crit, stale, stress, no tasks, checks only, no status file for the five
   status widgets; full_ring, partial_ring, hot, no_gpu, all_none, stale, no ring file for thermals/load) with scenario `ok`: no `RUNTIME_RENDER_ERROR`, no yellow alert, no console error, no `undefined`/`NaN`/`[object` in the text.
3. Scenarios `network-error`, `http-error`, `null`, `empty` on the ok fixture: no crash; the banner (§5.6) must appear for the first two.
4. `--shot` at logical sizes 616x404 (3x2 tracks), 616x616 (3x3) and 404x616 (2x3), dark and light (`--scheme dark|light`): no horizontal overflow, `contentOverflowsTile` false at the recommended footprint (§6.3 gives the measured heights).
5. After a database install (any option): re-run (2) with `--db <copy-of-live-after-install> --name "<widget>"` so the row is read back through superjson exactly as the server will.

Results of this run on the recommended definitions (`v2-defs-final`) are in Appendix A.

### 4.4 Cannot be verified without the live host

Real tile rendering inside the zoomed board canvas, the container's network path to `127.0.0.1:9111`, Redis for the limiter, actual import/placement calls, the image the container runs (§7).

### 4.5 Repo files that pin the v1 format and must be updated by the builders

* `widgets/build_widgets.py`, `widgets/build_metrics_widgets.py`: `definition()` emits `url/authType/method/displayType/displayConfig`; replace with the v2 object (§5.1). Both still `import` the payload builders correctly.
* `widgets/install_homarr_widgets.py`: writes `custom_widget_definition` (§0 rule 2, §6.7c).
* `widgets/check_templates.mjs`: dead (§4.1).
* `tests/test_payloads.py` lines 499-642: assert `authType == "none"`, `method == "GET"`, template <= 9500 chars, the v1 `FORBIDDEN` regex, `check_templates.mjs` PASS counts, the v1 `customWidgetImportSchema` (skipped now), `N = len(inst.ORDER)`, backup file names `.bak-before-ops-widgets-`.
* `widgets/CONVENTIONS.md`: statements about react-jsx-parser, the 10,000-char cap, the forbidden-word scan, "no Date, no icons", "cells are square and scale with the screen" are obsolete (§3, §6.3).
* `docs/INTEGRATION.md` "Open items" and decision 22 describe the v1 situation.

---

## 5. v1 -> v2 mapping for the seven templates

### 5.1 Mechanical rules (verified on all seven)

| # | v1 | v2 |
|---|---|---|
| 1 | `"$schema":"homarr-custom-widget-v2"` with `url`, `authType`, `method`, `displayType`, `displayConfig` | strict v2 object: `$schema`, `name`, `description`, `sources`, `requests`, `options`, `template` (§2.1) |
| 2 | `url: http://127.0.0.1:9111/<route>` | `sources.default.baseUrl = http://127.0.0.1:9111` + `requests.state.path = /<route>` |
| 3 | `authType: "none"` | `sources.default.auth = "none"` (no secret rows) |
| 4 | implicit network access | `sources.default.networkScope = "loopback"` (required) |
| 5 | `method: "GET"` | `requests.state.kind="query"`, `method="GET"`, `trigger="load"` |
| 6 | polling only by item `refreshInterval` | unchanged: item `refreshInterval`; add `cacheSeconds: 5` on the request |
| 7 | template reads `data.<field>` | template reads `data.state.<field>`: `re.sub(r"\bdata\.", "data.state.", tpl)` is safe for these seven (no string literal contains `data.`; checked with an acorn walk [V]); it must not touch `data={...}` props, and the regex does not |
| 8 | template <= 10,000 chars (target 9,500) | <= 50,000 (current 2.2 to 3.8 KB; keep the 9,500 budget only if you want to) |
| 9 | forbidden-word regex, react-jsx-parser whitelist | irrelevant; the analyzer and §3 apply |
| 10 | `build_widgets.py` squash | keep: strip leading whitespace and blank lines per line, join with `\n` |

`definition()` after the change (both build scripts):

```python
def definition(stem):
    name, route, desc = WIDGETS[stem]
    return {
        "$schema": "homarr-custom-widget-v2",
        "name": name,
        "description": desc,
        "sources": {"default": {"baseUrl": BASE_URL, "networkScope": "loopback", "auth": "none"}},
        "requests": {"state": {"kind": "query", "method": "GET", "path": "/" + route, "source": "default",
                               "trigger": "load", "cacheSeconds": 5}},
        "options": {},
        "template": squash((HERE / f"{stem}.jsx").read_text()),   # after the builders edit the .jsx files as in this section (data.state.*, thermals/load padding)
    }
```

Edit the `.jsx` sources to `data.state.` (they stay the source of truth) instead of rewriting at build time.

### 5.2 Per-widget table

| Widget (name) | Route | v2 request path | Template change vs v1 | Chars v1 -> v2 | Notes verified |
|---|---|---|---|---|---|
| `ops-overview` "Ops Overview" | `/overview` | `/overview` | `data.` -> `data.state.` | 2267 -> 2411 | 0 diagnostics; all 8 fixture payloads and 4 failure scenarios render; 3 `.map` use `key={x.l}`/`{x.n}` |
| `ops-disk` "Ops Disk" | `/disk` | `/disk` | same | 2603 -> 2777 | stress fixture has duplicate truncated names; contains U+00B7 and the label `"Plex: "` (not a credential word) |
| `ops-jobs` "Ops Jobs" | `/jobs` | `/jobs` | same | 2089 -> 2209 | same duplicate-key note |
| `ops-guard` "Ops Guard" | `/guard` | `/guard` | same | 2652 -> 2802 | labels such as "Top of ..." and "Stuck candidates" pass the credential check; same duplicate-key note |
| `ops-reclaim` "Ops Reclaimed" | `/reclaim` | `/reclaim` | same | 3335 -> 3473 | list keys `x.w+x.n+x.h` collide in stress |
| `ops-thermals` "Ops Thermals 7d" | `/thermal` | `/thermal` | same, plus delete `padding:{left:6,right:6},` from `xAxisProps` | 3596 -> 3709 | warning `UNKNOWN_MANTINE_PROP lineProps` is benign (Mantine 9.6.0 `LineChart` implements `lineProps`); 404 on the live service today |
| `ops-load` "Ops Load 7d" | `/load` | `/load` | same, plus the padding deletion | 3402 -> 3527 | same |

### 5.3 Calling two endpoints in one widget

Declare each as its own load query on the same source; ids are free-form but unique and must match `^[A-Za-z][A-Za-z0-9_-]*$`:

```json
"requests": {
  "overview": {"path": "/overview", "cacheSeconds": 5},
  "disk":     {"path": "/disk",     "cacheSeconds": 5}
}
```

Template: `data.overview?.head`, `data.disk?.mounts`, `status.overview?.ok===false`. All load queries run in parallel on every poll; stay <= 4 per widget (concurrency cap 4) and
count each toward 60 queries/min per user+item. A failure of one does not affect the other; each has its own `status.<id>` [V two-endpoint definition in the harness, scenarios ok / network-error / http-error].
Avoid naming a request `status` (it creates `data.status` and `status.status`). Different hosts need extra sources (`sources.second`), each with its own scope.

### 5.4 Charts

Everything charts-related that the v1 templates used is available: `LineChart`, `Sparkline`, `Progress`, `RingProgress`, `AreaChart`, `BarChart`, `DonutChart`, `PieChart`, `BarsList` and more
(`@mantine/charts` 9.6.0, 25 components). Real Chromium renders the ported `ops-thermals` and `ops-load` with multi-series lines, dashed reference line, per-tile sparklines [V screenshots].
Constraints: chart `h` must be explicit; `data=`/`series=` are exempt from the nested-key rules; `xAxisProps`/`yAxisProps`/`referenceLines` are fine except nested `left/right/top/bottom`.
The payload's `h` is the CSS string `clamp(110px,16vw,300px)` (`homelab_maint/payloads_metrics.py:311,337`). `vw` follows the VIEWPORT, and a `vw` length inside the zoomed canvas is scaled by the zoom too
[V Chromium: viewport 1000 px, ancestor `zoom:0.5`, `10vw` measured 50 px]. So chart height varies with the device while the tile's logical size does not. It stays inside the 3x3 recommendation
at all viewports (110..300 px); fixed pixel heights from the payload would remove the variance (optional builder decision, [U] visual check on the real board).
Cosmetic nit seen in the real-Chromium render of `ops-load`: the top y tick `100%` is clipped to `00%` because `yAxisProps.width` is 32 px (thermals' `100°` fits); widening it to about 40 fixes it [V screenshot `ops-load-v2-fixed.png`].

### 5.5 Removed or changed with no v2 equivalent

* The v1 forbidden-word scan, the 10,000-char cap, `react-jsx-parser` semantics (for example its warning on property reads of undefined) and the component whitelist file.
* Per-request poll intervals and any `interval`/`click` trigger (none exist). Polling is the board item's `refreshInterval` (slider 1..3600, default 30).
* `displayType` presets other than `customJsx` (not used here).
* v1 icons restrictions: `Icon`/`TablerIcon` now exist (registered names only). The ops widgets do not use icons.
* `url` containing a query string: use the request `query` object.
* Secrets: none needed; if a future ops route is authenticated use source `auth` and `custom_widget_v2_secret`, never headers (header names containing token/secret/api-key are rejected).

### 5.6 Recommended improvements (verified; adopt them in the ported `.jsx`)

1. **Index keys** in the five status widgets: `.map((x,i)=>` and `key={i}`. The stress fixture contains three identical truncated names
   (`TTTTTTTTTTTTTTTTTTTT..`), which makes React log "Encountered two children with the same key" and "may cause children to be duplicated and/or omitted".
   With index keys the console is clean and the rendered text is byte-identical on stress, ok and crit for all five [V]. (The lists are static and never reordered or stateful.)
2. **Status banner** as the first element after the header, for all seven:
   `{status.state?.ok===false&&<Text fz={11} c="red.5" lineClamp={2}>{"ops service: "+(status.state.error||"no response")}</Text>}`.
   Without it a dead service renders "no data" with no explanation. Verified under `network-error` ("ops service: External request failed") and `http-error` ("ops service: HTTP 500: Internal Server Error") [V].
3. Keep every payload < 4 KB for the five status routes (`payloads.py` enforces 3,900 B); the metrics payloads are ~10 to 12 KB and far below the 1 MiB cap.

The final definitions with 1 and 2 applied are generated by `make_v2_final.py` into `v2-defs-final/` (Appendix B).

---

## 6. Placement and database facts

### 6.1 Tables (SQLite, schema `packages/db/schema/sqlite.ts`)

| Table | Columns that matter | Notes |
|---|---|---|
| `custom_widget_v2_definition` | `id` PK, `name`, `description`, `icon_url`, `sources`, `requests`, `options`, `template`, `enabled` (default 1), `created_at`/`updated_at` (Unix seconds), `creator_id` FK `user` ON DELETE SET NULL | `sqlite.ts:624`, migration `0042_custom_widget_v2_tables.sql` |
| `custom_widget_v2_secret` | PK (`definition_id`,`source_id`,`kind`), `encrypted_value`, `updated_at`; FK to definition ON DELETE CASCADE | AES-256-CBC with `SECRET_ENCRYPTION_KEY`; not used here |
| `custom_widget_definition`, `custom_widget_secret` | legacy v1 tables, kept read-only so a v1 binary can still read the DB; 10 and 4 rows | do not write |
| `item` | `id` PK, `board_id` FK board CASCADE, `kind`, `options` (superjson), `advanced_options` (superjson, default `{"json": {}}`) | one row per widget instance |
| `item_layout` | PK (`item_id`,`section_id`,`layout_id`), `x_offset`, `y_offset`, `width`, `height`; FKs to item, section, layout all CASCADE | **one row per layout of the board** (each board has a Base and a Mobile layout) |
| `section` | `id`, `board_id`, `kind`, `x_offset`, `y_offset`; each board has exactly one `kind='empty'` main section | place items there |
| `layout` | `id`, `board_id`, `name`, `column_count`, `breakpoint`, `role`, gutters | Base = 10 columns, breakpoint 768; Mobile = 3 columns, breakpoint 0 |
| `section_layout`, `integration_item` | empty / not used for `customApi` | `customApi` items have no `integration_item` rows |

Item shapes, copied from the live rows [V]:

```
item.kind              customApi
item.options           {"json": {"definitionId": "<custom_widget_v2_definition.id>", "refreshInterval": 30}}
item.advanced_options  {"json":{"title":null,"customCssClasses":[],"borderColor":""}}
```

`configuration` and `configurationVersion` may be omitted (defaults `{}` and 1). `refreshInterval` is validated 1..3600 and read by the client as `max(1000 ms, value*1000)` [S `custom-api/index.ts:14-20`, `component.tsx:30-41`].
`definitionId` must name a v2 row; an id that exists only in the legacy table gives "migration required" (§3.9). No format check on the id string.

### 6.2 What the runtime reads for an item

`getData({itemId})`: item row (`kind` must be `customApi`) -> board view permission -> `definitionId` -> v2 definition row with secrets -> `enabled` must be 1 -> full schema parse -> run all load queries ->
return `{template, data, status, options, requestCapabilities, queryCacheKey}`. The `template` travels to the browser on every poll [S `custom-api.ts:49-110,190-283`].
Placing or editing a `customApi` item through the board API requires the `admin` permission [S `packages/api/src/router/board/custom-widget-placement-access.ts:35-70`]; direct SQL bypasses that guard.

### 6.3 Board geometry and the sizes to use

This fork's board is fixed logical geometry [S `apps/nextjs/src/components/board/layout/constants.ts:1-8`, `README.md`, `scaled-board-canvas.tsx:19-27,87`]:

* one grid track = **212 x 212 logical px** (200 cell + 12 gap); an item spanning `w x h` tracks has a **footprint of `212w x 212h` logical px** (1 track 212, 2 tracks 424, 3 tracks 636) and the card is inset
  inside it. The README says cards are inset by 10 visual px on every side (`CONTAINER_CARD_INSET = 5` is the container constant at `section-grid.tsx:34`), so the usable content box is roughly the footprint minus 10 to 20 px
  per axis: this document uses the conservative `212n - 20` (1 track 192, 2 tracks 404, 3 tracks 616); the harness presets `626x414`, i.e. footprint minus 10 [U exact inset];
* the whole canvas has a fixed logical width (`columns x 212`) and is zoomed once with CSS `zoom` to fit the viewport, so a widget sees the SAME logical size on every device;
* Base layout: 10 columns (2120 logical px wide), used at viewport >= 768 px. Mobile layout: 3 columns (636 px), used below 768 px.

Natural content height of each ported widget measured in real Chromium (px, logical = CSS because the harness has no zoom) [V `measure_sizes.py`]:

| Widget | ok fixture | stress / hot fixture | width dependence | Recommended footprint (tracks, WxH) | Why (usable height: 2 rows 404, 3 rows 616) |
|---|---|---|---|---|---|
| ops-overview | 181 | 359 | none | **3 x 2** | 359 < 404 |
| ops-disk | 425 (467 at a 390 px tile) | 446 (522 at a 390 px tile) | wraps below ~600 px | **3 x 3** | 446 > 404 |
| ops-jobs | 383 | 420 | none | **3 x 3** | 420 > 404 |
| ops-guard | 330 (365 at 390) | 412 (433 at 390) | slight | **3 x 3** | 412 > 404 |
| ops-reclaim | 380 | 553 | none | **3 x 3** | 553 < 616 |
| ops-thermals | 309 | 313 (viewport < 700 px), 360 (viewport 984 px), 503 (viewport >= 1875 px, chart clamps at 300 px) | chart height follows the viewport width, not the tile | **3 x 3** | 503 < 616 |
| ops-load | 287 | 287 (viewport < 700 px), 353 (984 px), 437 (>= 1875 px) | same | **3 x 3** | 437 > 404 |

(The harness viewport is the tile width plus 24 px; a real board viewport is the browser window, so the chart rows above use the `clamp(110px,16vw,300px)` payload value for that window.)
Width: 3 tracks (usable ~616 px) is the smallest comfortable width for the 3-column tile grids; 2 tracks (usable 404 px) works but increases heights (disk +40 to +75 px). The Mobile layout is 3 columns wide, so every widget is full width there.
The owner's existing precedent differs per board: Thermals is 10x9 on `Local-Big-Screen`, 10x9 on `Local-Mobile`, 3x3 on `Remote-Big-Screen`, 8x11 on `Remote-Mobile`; Fan Control 10x7 / 10x8 / 3x2 / 8x9.
Ask the owner whether they want content-fit sizes (above) or the old 5x4 / 10xN installer defaults; the old defaults were derived from the v1 "scaling cells" assumption.
Content taller than the footprint scrolls inside the tile; it is never lost.

### 6.4 The four boards (from the DB copy at 14:01; re-query at install time, ids are not stable contracts)

| Board | id | Layout Base (10 cols) id, current bottom | Layout Mobile (3 cols) id, current bottom | empty section id | items |
|---|---|---|---|---|---|
| `Local-Big-Screen` | `hxrcwrfp9ssfbfctv2wye2pt` | `ij10s7lkjuco8ux94bzkamoc`, y=44 | `dckxw8omym3toa36wf2q0du7`, y=76 | `q7xo6y0s7hbaalg2vnr3a7lq` | 40 |
| `Local-Mobile` | `uo5xoctxprt31tzaqgyvq6fn` | `zj6ifwje2yr8v798yu1hp5ju`, y=177 | `uno55jwzb8e8i2tijyjj08tu`, y=215 | `i543jkyntlt6756s6v1q1pmb` | 37 |
| `Remote-Big-Screen` (user's home board) | `fvi7s231odv8nntna549k43c` | `isnd7hek277aawz2hoz8rvj9`, y=19 | `a0zln4uebkrqu7yo3oinottw`, y=44 | `p13iqghiltm9ij93zfybf5fi` | 40 |
| `Remote-Mobile` (user's mobile home) | `nx5osbinfqopbpw4o0nrmr7i` | `sysvl3mjuu7kw7ou81tt3459`, y=144 | `oclf7z06ggrfvslr0ykmpqr0`, y=188 | `h9spl71t3k0qwb1kru1xeodv` | 37 |

All four boards are private (`is_public=0`), no containers (`section_layout` empty), one main section each. "Bottom" = max(`y_offset+height`) over the layout. Existing customApi items: Thermals and Fan Control on each board.

Two placement policies, computed for the sizes in §6.3 in the order overview, disk, jobs, guard, reclaim, thermals, load [V `placement_plan.py`]:

* **Policy A, `board.addItem` (API/UI "add widget"):** first free spot scanning from the top, so it FILLS HOLES in existing content. Examples: `Local-Big-Screen` Base overview lands at (0,31), disk (3,32), jobs (0,33);
  `Remote-Big-Screen` Base overview (5,12); `Remote-Mobile` Base overview (0,42), the rest from (0,144); the 3-column Mobile layouts had no holes in this data and landed at the bottom.
  [S `packages/api/src/router/board.ts:113-131,2220-2300`]
* **Policy B, append below the current bottom, left to right, wrapping (the old installer's rule):** Base layouts at y = 44 / 177 / 19 / 144 and Mobile at y = 76 / 215 / 44 / 188. Result:
  `Local-Big-Screen` Base: overview (0,44) 3x2, disk (3,44), jobs (6,44), guard (0,47), reclaim (3,47), thermals (6,47), load (0,50); new bottom 53. Mobile: 76, 78, 81, 84, 87, 90, 93, new bottom 96.
  Other boards follow the same pattern from their bottoms (`Local-Mobile` 177 / 215, `Remote-Big-Screen` 19 / 44, `Remote-Mobile` 144 / 188).

Use B for direct SQL, A for the API/UI, then `PATCH /api/boards/{boardId}/items/{itemId}/layouts/{layoutId}` if exact spots matter (rejects overlap and out-of-bounds).

### 6.5 Cache, restart and rate behaviour

* Definitions and items are read from the database on every request; **no restart is needed** after inserting rows. Open board tabs need a reload (client React Query). No board-level server cache was found; the only
  Next route revalidation is triggered by settings saves [S `apps/nextjs/.../_header-actions.tsx:104`] [U verify once on a copy].
* Response cache is an in-process `Map` of <= 1000 entries and is lost on container restart; nothing persistent. `cacheSeconds` (5) <= refresh interval (30) means each poll gets fresh data.
* The widget template change takes effect on the next poll because the template is returned by `getData` (no client bundle).
* Polling pauses in a hidden tab (React Query default) [U]. Polling stops permanently on NOT_FOUND / FORBIDDEN / PRECONDITION_FAILED.
* Budget: one load request per poll. At `refreshInterval` 30 and 4 boards x 1 open tab that is 8 requests/min per definition against 240/min; per user+item 2/min against 60/min. Do not set a refresh below 5 s.
  Recommended item refresh: 30 s for the five status widgets (data changes at most every 15 min), 60 s for thermals/load.

### 6.6 SQLite locking and journal mode

* The app opens the file with `new Database(url)` (better-sqlite3 defaults: rollback-journal mode, 5 s busy timeout, no PRAGMA tuning) [S `packages/core/src/infrastructure/db/drivers/sqlite.ts`].
  The directory listing shows no `-wal`/`-shm`/`-journal` file with the container running, and the copy is `journal_mode=delete` [V], so the live file is most likely in rollback-journal mode [U for the live file header].
* An external writer must run as root (directory and file are root-owned, SQLite creates `db.sqlite-journal` next to the file). Use one short `BEGIN IMMEDIATE` transaction, `PRAGMA busy_timeout=15000`,
  and abort if a non-empty `db.sqlite-journal` or `-wal` already exists (a write is in flight). Do not replace or rename the file under the running app (it holds an open handle); restoring a backup requires stopping the container first.
* Python's `sqlite3` has foreign keys OFF by default, while the app's better-sqlite3 build has them ON (`SQLITE_DEFAULT_FOREIGN_KEYS=1`, `node_modules/better-sqlite3/deps/defines.gypi:14`; busy timeout default 5000 ms,
  `lib/database.js:34`) [S, repo `node_modules`; the image's binary is [U]]. Every inserted id must reference an existing row, and the installer must run `PRAGMA foreign_key_check` before `COMMIT` (the old installer does) [S `install_homarr_widgets.py:apply`].
* The tasks service in the same container also writes to the file; keep the transaction well under a second.

### 6.7 Installation options, safest first

**(a) In-app import + UI placement (recommended).** No SQL, no keys, app-side validation, the item guard and the audit trail apply.

1. Produce the seven import files (§5.1; the exact format of §2.2) and copy them to the machine running the browser.
2. Homarr -> Manage -> Custom widgets -> Import (file picker accepts `.json` / `.md`); or paste the JSON anywhere on that page (the page listens for `paste` when focus is not in an input and the text contains `"$schema":`).
   The review dialog shows origin `http://127.0.0.1:9111`, authentication `none`, scope `loopback`, methods `GET`, permission `view`, no actions. **Tick the URL confirmation** (required for any non-public source), then Import.
   Do not click Import twice (each click creates a new definition) [S `use-custom-widget-import.ts` comment "must only ever be imported once"].
3. For each board: edit mode -> add widget -> Custom widget -> pick the definition, set refresh interval (30 / 60), size and position per §6.3-6.4, save the board.
   Cost: 7 imports + 28 placements. Rollback: delete the definition(s) in Manage (items show "definition not found" with a remove button) and remove the items in edit mode.
4. Verify (§6.8). Risks: only human error.

**(b) Authenticated API calls (admin API key).** Needs a key (Manage -> Tools -> API, shown once as `<id>.<token>`; "does not expire"; delete it afterwards). The key acts as `ohmz_h` (admin) [S `packages/auth/api-key/get-api-key-session.ts:13-67`, `apps/docs/docs/management/api/index.mdx`].

```bash
BASE=http://127.0.0.1:7575            # nginx in the container; or the public URL
H='ApiKey: <id>.<token>'
# 1. import one definition (tRPC, superjson envelope {"json": input}); returns {"result":{"data":{"json":{"id":"<new id>"}}}}
curl -sS -X POST "$BASE/api/trpc/customWidget.import" -H "$H" -H 'content-type: application/json' \
  --data "$(python3 -c 'import json,sys; print(json.dumps({"json":{"widget":json.load(open(sys.argv[1])),"secrets":[]}}))' widgets/ops-overview.json)"
# 2. place it on one board (OpenAPI REST, plain JSON body); creates item + one item_layout per layout, first-free position
curl -sS -X POST "$BASE/api/boards/items" -H "$H" -H 'content-type: application/json' \
  --data '{"boardId":"<board id>","kind":"customApi","options":{"definitionId":"<id from 1>","refreshInterval":30},"size":{"width":3,"height":2}}'
# 3. optional exact position per layout (PATCH /api/boards/{boardId}/items/{itemId}/layouts/{layoutId})
curl -sS -X PATCH "$BASE/api/boards/<boardId>/items/<itemId>/layouts/<layoutId>" -H "$H" -H 'content-type: application/json' \
  --data '{"boardId":"<boardId>","itemId":"<itemId>","layoutId":"<layoutId>","xOffset":0,"yOffset":44,"width":3,"height":2}'
```

Wire format of the tRPC leg was checked against a stand-in router using the same `fetchRequestHandler` + superjson transformer: POST body must be `{"json": <input>}` (a bare input gives 400),
GET queries take `?input=<urlencoded {"json":<input>}>`, responses are `{"result":{"data":{"json":...}}}` [V]. The `/api/boards/items` and PATCH shapes come from `.meta({openapi})` and the docs [S `board.ts:2220-2300`, `packages/validation/src/board.ts:175-198`]; **not exercised live [U]**.
`customWidget.*` is tRPC/MCP only (not in the OpenAPI REST document); every `customWidget.*` procedure needs `admin`. Demo mode would block mutations (not set here).
`size` is capped 1..24 per axis, width capped to each layout's columns. The API key creation and deletion write to the live DB through the app.

**(c) Direct SQLite insert with a backup (last resort).** Mirrors the owner's `deploy-homarr.sh` backup convention (`db/backup-<stamp>/` under the appdata dir).

1. Rehearse on a copy first. The scratch script `install_v2_rehearsal.py` did this end to end on a copy of the DB copy for two boards (`Local-Big-Screen`, `Remote-Big-Screen`): 7 definitions + 14 items + 28 layout rows inserted in one `BEGIN IMMEDIATE`
   (all four boards would be 28 items + 56 layout rows),
   `PRAGMA foreign_key_check` empty, `quick_check` ok, then every new row was read back through superjson and the real schema and rendered by the harness (`--db rehearsal-v2.sqlite --name "<widget>"`): 7/7 PASS [V].
2. Backup (`deploy-homarr.sh:53-70` minus the container replacement; do not run `deploy-homarr.sh` just for this, it recreates the container):

```bash
APPDATA=/data/compose/5/homarr/appdata
BK="$APPDATA/db/backup-$(date +%Y%m%d-%H%M%S)"
sudo mkdir -p "$BK" && sudo sh -c "cp -a '$APPDATA'/db/*.sqlite* '$BK'/"
sudo test -s "$BK/db.sqlite" || { echo "FATAL: backup empty" >&2; exit 1; }
# preferred consistent alternative (online backup API, safe while Homarr has the file open):
sudo python3 -c "import sqlite3,sys; s=sqlite3.connect('file:$APPDATA/db/db.sqlite?mode=ro',uri=True); d=sqlite3.connect('$BK/db.sqlite'); s.backup(d); print(d.execute('pragma integrity_check').fetchone())"
```

3. Run the (rewritten) installer as root against the live path with `--live --backup-dir`, inside one transaction. Statements, per widget (ids from `createId` equivalent: letter + 23 chars of `[a-z0-9]`, not starting `seed-`):

```sql
INSERT INTO custom_widget_v2_definition
  (id,name,description,icon_url,sources,requests,options,template,enabled,created_at,updated_at,creator_id)
VALUES (:id,:name,:desc,NULL,
  '{"json":{"default":{"baseUrl":"http://127.0.0.1:9111","networkScope":"loopback","auth":"none"}}}',
  '{"json":{"state":{"source":"default","kind":"query","method":"GET","path":"/overview","trigger":"load","auth":"inherit","cacheSeconds":5,"permission":"view"}}}',
  '{"json":{}}', :template, 1, :now, :now, :creator_id);       -- :now = int(time.time()) (Unix seconds); creator = the existing 'Thermals' row's creator_id
-- per board (4) and widget (7); :item_options is built in code as json.dumps({"json":{"definitionId":<new definition id>,"refreshInterval":30}}, separators=(",",":"))
INSERT INTO item (id,board_id,kind,options,advanced_options)
VALUES (:item_id,:board_id,'customApi',:item_options,'{"json":{"title":null,"customCssClasses":[],"borderColor":""}}');
-- per layout of that board (2):
INSERT INTO item_layout (item_id,section_id,layout_id,x_offset,y_offset,width,height) VALUES (:item_id,:section_id,:layout_id,:x,:y,:w,:h);
```

   `:section_id` = the board's single `kind='empty'` section; positions by policy B (§6.4), cursors seeded from `max(y_offset+height)` of `item_layout` and `section_layout` in that section and layout.
   Refuse unless the board has exactly one empty section, a Base and a Mobile layout, and no existing item for the same `definitionId`.
4. Idempotency: skip a widget whose `name` already exists; update only with an explicit flag (an update must bump `updated_at`).
5. Rollback of just these rows (no restore needed): `DELETE FROM item_layout WHERE item_id IN (...)`; `DELETE FROM item WHERE id IN (...)`; `DELETE FROM custom_widget_v2_definition WHERE id IN (...)` (child rows first because Python has FKs off).
   Full restore from `$BK/db.sqlite` requires stopping the container first, then `docker start`; that is an owner action.
6. No container restart is needed for the new rows; reload the board page. A restart is only needed if the file was replaced.
7. **Never** reuse the v1 installer (§0 rule 2), and never insert into `custom_widget_definition`.

### 6.8 Post-install verification (any option)

1. Read-only copy of the live DB (online backup), then `render-check --db <copy> --name "Ops Overview" --fixture state=http://127.0.0.1:9111/overview` for each widget: schema OK, interpreter OK, PASS.
2. `PRAGMA foreign_key_check` and `PRAGMA quick_check` on the copy.
3. Open each board (reload): tiles render, no red triangle, no yellow "template warnings" alert, `refreshInterval` as chosen. A tile that says "definition not found" has a wrong `definitionId`; "migration required" means it points at a legacy-only id.
4. For `ops-thermals` / `ops-load`: confirm the ops service serves `/thermal` and `/load` first (§0 rule 10).

---

## 7. Open questions and risks

1. **Deployed image is unverified.** The task states Homarr is v2, but `docs/INTEGRATION.md` (2026-10-02) says the host still ran `homarr:develop` from before the upgrade. I did not run docker or call Homarr. Confirm
   (`docker inspect` label `org.homarr.dev.revision` of the running container; the build label is set by `deploy-homarr.sh --build`) before installing. [U]
2. **`/thermal` and `/load` return 404 on the live ops service** (installed lib older than `payloads_metrics`). Until `install.sh` is re-run and `homelab-maint-www.service` restarted, those two widgets show "no data" plus "not found". [V]
3. **Real-board rendering in the zoomed canvas is unexercised.** The harness reproduces the card (padding 0, `container-type:size`, overflow, opacity variable) but not `zoom`, the inset, or the board CSS. Check sizes and the thermals/load charts (`vw` heights) visually after the first install. [U]
4. **Exact card inset** inside the 212 px footprint was not measured (README: 10 visual px; the harness presets footprint minus 10). §6.3 sizes use the conservative `212n - 20`: the largest measured content (553 px, reclaim stress) leaves 63 px in a 3-row tile
   (616 usable) and the overview's 359 px leaves 45 px in a 2-row tile (404 usable); 3x2 is NOT enough for disk, jobs, guard, reclaim, thermals, load. [U]
5. **API legs not exercised.** `customWidget.import` and `POST /api/boards/items` are source-derived; only the tRPC wire format was confirmed on a stand-in router. No API key exists; creating one is a live write. [U]
6. **Redis for the limiter** must be reachable from the app; port 6379 is listening on the host (inferred to be the container's Redis). If it were down every custom widget request would fail with "Request limiter is unavailable" (the existing Thermals widget would show it too). [U]
7. **Journal mode of the live file** is inferred from the missing sidecars and the copy. The recipe is safe in either mode. [U]
8. **Foreign keys in the app connection:** ON in the repo's better-sqlite3 build (`SQLITE_DEFAULT_FOREIGN_KEYS=1`) [S]; the binary inside the deployed image is [U]. The installer does not rely on it.
9. **Credential-heuristic regressions:** any future label or field comparison with those words fails the whole widget at render time, not only at save. Keep the harness in CI for the seven definitions (§4.3) and never hand-edit the template in the UI without re-running it. [V]
10. **Duplicate-name definitions:** names are not unique in the DB; the picker lists them by name. The installer must be idempotent by name. [S]
11. **Seeded widgets:** the four `seed-*` rows are re-created by the seeder when missing; do not reuse that prefix. [S `packages/db/migrations/seed.ts:1112-1139`]
12. **Per-definition budget:** 240 queries/min is shared by all viewers of one definition; a kiosk tab with `refreshInterval` 1 would exhaust it. Keep >= 10 s (30 / 60 recommended). [S]
13. **Docs vs code differences found:** docs say responses use shared-Redis cache keys and that authentication secrets are redacted from upstream responses; the code uses an in-process `Map` and applies redaction only to integration sources; the docs say HTTP sources support "GET, POST, PUT, and PATCH" for actions while the schema also allows DELETE as a confirmed full-permission action.
    Neither affects these widgets. [S `requests-and-security.mdx` vs `request-executor.ts:37-44`]
14. **Owner decisions needed:** sizes per board (content-fit vs the old 5x4 / 10xN), refresh intervals, whether to adopt the banner and index keys, whether to switch payload chart heights from `vw` to px, and the install route (a / b / c).
15. **INTEGRATION.md/README text** still describes the v1 widgets as deployed. Update when the port lands.

---

## 8. Reconciliation of the research reports

The three input reports (schema/API, runtime, placement/DB) were reconciled by re-reading the sources and re-running the checks. The third report (placement/DB) and the tail of the second were not in the text I received, so §6 and
parts of §3 were derived from the DB copy, the board code and my own probes.

| Topic | Disagreement or gap | Resolution |
|---|---|---|
| Null handling | Report 1: "member access on null returns undefined"; report 2 probes: `.map`/`.toFixed` on `undefined` crash | Both right: property reads are null-safe (`safe-properties.ts:31-42`), method calls are not (`Calling method 'X' is not allowed`); §3.4 [V] |
| Thermals and load | Report 1: all seven pass the schema with `lineProps` warnings only; report 2: they need the padding edit | Both right at different layers: schema and analyzer pass; the runtime strips nested `left`/`right` keys with a yellow alert; §5.2 [V] |
| Method allow-list | Analyzer lists names (`at`, `concat`, `toString`, `lastIndexOf`, `padStart`...) | Runtime also checks the receiver type; the listed combinations fail at run time; §3.4 [V] |
| Request vocabulary in the brief (`mutation`, `interval`, `click`) | Not in the schema | Only `query`/`action` and `load`/`manual`; §2.3 [V] |
| Forbidden-word scan, 10,000-char cap, react-jsx-parser | Still described in `widgets/CONVENTIONS.md` and pinned by `tests/test_payloads.py` | Gone in v2 (50,000 chars, analyzer); the credential-literal heuristic replaces the word scan; §3.8, §4.5 [V] |
| Board cells | `CONVENTIONS.md`: square cells that scale with the screen | This fork uses fixed 212 px logical tracks with a zoomed canvas; §6.3 [S] [V vw-under-zoom] |
| Deployed image | `docs/INTEGRATION.md` (2026-10-02) says the host still ran the v1 image; the task says Homarr is now v2 | Unverified; §7 item 1 |
| Docs vs code | Redis cache keys, secret redaction, HTTP method list | Code differs from docs; none affects these widgets; §7 item 13 |
| Polling in background tabs | Asserted by report 1 from React Query defaults | Not tested; [U] |

---

## Appendix A. Evidence log (this session)

| What | Result |
|---|---|
| `customWidgetDefinitionSchema` and `customWidgetPreviewDefinitionSchema` on the seven mechanical ports (`v2-defs/`) | 14/14 PASS; analyzer: 0 diagnostics for five, 1 benign `lineProps` warning for thermals and load |
| Same on the recommended variants (`v2-defs-final/`) | 14/14 PASS |
| Parse of all 10 live v2 rows (`validate-live.ts`, rows exported from the DB copy) | 10/10 OK |
| Harness (jsdom + real `CustomJsxRenderer`), mechanical ports, the 54 v1 fixture payloads in `ok` | 49 clean; 5 console `error:` = React duplicate key on each status widget's `stress` payload (fixed by index keys, §5.6) |
| Harness, mechanical ports, 5 scenarios (ok, http-error, network-error, empty, null) x 7 widgets | 35/35: no runtime error, no yellow alert, no console error |
| Index keys vs original keys on stress, ok, crit x 5 status widgets | clean console; rendered text identical in 15/15 |
| Recommended definitions (index keys + banner): 54 payloads in `ok` plus `network-error`, `http-error`, `null`, `empty` on the ok fixture of each widget | 82/82 clean (no `undefined`/`NaN`/`[object` in the text; the banner appears exactly in `network-error` and `http-error`) |
| Natural content heights in real Chromium, 7 widgets x {ok, stress} x tile widths {390, 640, 960, 1920} | table in §6.3; no horizontal overflow, no runtime error or warning in any of the 56 runs |
| Real Chromium screenshots | ops-overview (dark, 626x414) by me; ops-thermals and ops-load (light, charts drawn) by the sibling run; no console errors |
| Two-endpoint definition (`overview` + `disk`) | schema OK; ok / network-error / http-error render correctly with per-request banners |
| Rehearsal install on a copy | 7 definitions, 14 items, 28 layout rows; `foreign_key_check` empty; `quick_check` ok; 7/7 rows re-read from the DB and PASS the harness |
| v1 installer dry run on a copy of the v2 DB | plans inserts into legacy `custom_widget_definition` and `customApi` items (confirms §0 rule 2); nothing applied |
| `vw` under CSS `zoom` (Chromium 149) | `10vw` at viewport 1000 px, ancestor `zoom:0.5` => 50 visual px (viewport units are zoomed) |
| tRPC wire format on a stand-in router (same adapter + superjson) | POST `{"json":input}` 200; bare input 400; GET `?input={"json":...}` 200 |
| Live ops service | `/overview /disk /jobs /guard /reclaim /status` 200 JSON; `/thermal /load` 404 |
| Repos | `git status` clean in `/home/ohmz/StudioProjects/homarr`; no tracked-file change in `/home/ohmz/homelab-maint` (only this document is new) |

## Appendix B. Scratch artefacts (session-scoped; copy what you keep)

All under `/tmp/claude-1000/-home-ohmz-StudioProjects/a892927f-f870-4100-8cb1-f2b3f8ad4df8/scratchpad/homarr-research/`:

* Harness: `rc.sh`, `render-check.mjs`, `preview-entry.tsx`; schema check `validate-definition.ts`, `validate-live.ts`.
* Definitions: `make_v2_defs.py` -> `v2-defs/ops-*.v2.json` (mechanical); `make_v2_final.py` -> `v2-defs-final/ops-*.v2.json` (recommended); `v2-defs/two-endpoints.v2.json`.
* Fixtures: `cases/<widget>/<case>.json` (54 payloads from `dump_cases.py`), `ops-fixtures/`.
* Runs: `run_all_cases.py` / `run_all_cases.out`, `run_final.py` / `run_final.out`, `cases.json` + `cases-output.txt` (158 behaviour probes), `measure_sizes.py` / `sizes/measure.out`, `cred-probe.ts`, `zoom-vw.mjs`, `trpc-wire.mts`, `placement_plan.py`, `install_v2_rehearsal.py`, `rehearsal-v2.sqlite`.
* Schema/JSON Schema: `custom-widget-v2.schema.json` (what `customWidget.schema` serves), `edge-cases.ts` / `edge-cases.out` (about 110 accept/reject cases).
