#!/usr/bin/env node
/**
 * render-check.mjs: validate and render a Homarr v2 custom-widget definition with the REAL runtime sources of a Homarr
 * checkout (zod schema + import parser + JSX analyzer + interpreter + Mantine 9 + CustomJsxRenderer in jsdom, and optionally a
 * real headless Chromium screenshot). Optional DEV tooling for homelab-maint: nothing here is needed to run or install the
 * widgets; the pure-Python checks in widgets/build_v2.py and tests/test_widgets_v2.py do not need it.
 *
 *   node widgets/tools/render-check.mjs --definition widgets/ops-load.json --fixture state=widgets/fixtures/ops-load/full_ring.json \
 *        [--scenario ok|http-error|network-error|empty|null] [--scheme dark|light] [--tracks 3x3 | --size 616x616] [--shot out.png]
 *   node widgets/tools/render-check.mjs --batch jobs.json [--json]     many jobs in ONE process (3 s start-up, ~0.2 s per job)
 *   node widgets/tools/render-check.mjs --probe                        is the environment usable? (exit 0, or 3 = no)
 *
 * Environment: HOMARR_REPO = Homarr checkout with node_modules (default /home/ohmz/StudioProjects/homarr; read-only: it is
 * never written to, its node_modules is reached through a symlink in a work dir), RENDER_CHECK_WORK = work dir (default
 * <tmp>/homelab-maint-render-check-<uid>-<hash of the checkout path>; safe to delete). Needs Node >= 22 (node:sqlite); --shot
 * also needs Playwright's Chromium (in the checkout's node_modules + ~/.cache/ms-playwright) or /usr/bin/google-chrome.
 * No install, no network (except an explicit http(s) fixture URL), the Homarr database is only ever opened read-only.
 *
 * Input (one of): --definition <import.json> (also run through the Import button's own parser) | --db <sqlite> (--name N | --id I)
 * (reads custom_widget_v2_definition read-only through superjson, like the server) | --template <file.jsx> [--request-id state].
 * Fixtures: --fixture <requestId>=<file.json|http(s)://url> (repeatable; one load request => the id may be omitted) = the response BODY.
 * --scenario: ok | http-error (body kept, status.ok=false, "HTTP 500") | network-error (data=null, "External request failed") |
 *             empty (data={}) | null (data=null, status ok).
 * Tile: --tracks WxH (board tracks, usable size 212*n-20 px) or --size WxH (logical px); default 616x616. --opacity 0..1.
 * Other: --shot <png>, --scheme, --text (print rendered text), --html-out <file>, --options-json '{..}', --chart-size 400x160
 *        (jsdom only), --no-dom, --allow-warning <substring> (repeatable; analyzer warnings fail unless allowed; the benign
 *        "lineProps on LineChart" is allowed by default), --json (full report), --timeout <s> (default 300), --repo, --work-dir.
 * Batch file: {"defaults": {..job fields..}, "jobs": [{"id","definition","fixtures":{"state":path},"scenario","scheme","tracks"|"size",
 *             "shot","text",...}]} (camelCase versions of the flags). Output with --json: {"reports":[..],"failed":N}.
 *
 * A job FAILS on: full schema or preview schema or import-parser error, analyzer error or un-allowed warning, interpreter
 * error or warning, RUNTIME_RENDER_ERROR, the yellow "N template warnings" alert, React/Mantine console errors, a stray
 * undefined / NaN / Infinity / null / true / false / [object as a word of the rendered text (checked per text node, so "2/9undefined"
 * and "9NaN" fail too), and (with --shot) a missing screenshot, Chromium console errors or page errors.
 * Chart labels cut off by their svg (a too narrow y axis) also fail. Overflow and legibility metrics (shot.metrics: contentNaturalHeight,
 * contentOverflowsTile, horizontalOverflow, lowContrastText / lowContrastCount = text below WCAG AA as drawn, in the shot's colour scheme)
 * are reported, not failed on: the test-suite asserts them against the recommended footprint and, per scheme, a contrast floor.
 * Exit codes: 0 all pass | 1 a job failed | 2 usage or input error | 3 environment unavailable (Node/checkout/tsx) | 4 timeout.
 * It cannot see the live board (zoomed canvas, real network, live data, the host browser's fonts).
 */
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, readlinkSync, rmSync, symlinkSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const EXIT = { PASS: 0, FAIL: 1, USAGE: 2, ENV: 3, TIMEOUT: 4 };
const SCENARIOS = ["ok", "http-error", "network-error", "empty", "null"];
const BOOLEAN_FLAGS = ["text", "json", "no-dom", "probe", "help"];
const VALUE_FLAGS = ["definition", "db", "name", "id", "template", "request-id", "fixture", "scenario", "scheme", "size", "tracks", "shot", "opacity",
  "chart-size", "options-json", "html-out", "batch", "allow-warning", "timeout", "repo", "work-dir"];
const DEFAULT_ALLOWED_WARNINGS = ["UNKNOWN_MANTINE_PROP: 'lineProps' on LineChart"];   // Mantine 9.6 supports it; the analyzer just does not know
// A template printed a missing/invalid value. Matched on the text NODES joined with a separator (textContent would glue siblings together, "2/9" + "undefined" =
// "2/9undefined", and a \b boundary misses that), so the token only has to stand alone between non-alphanumerics: "12dundefined", "9NaN", "p: null" all fail.
const SUSPECT_TEXT = /(?<![A-Za-z0-9])(?:undefined|NaN|Infinity|null|true|false)(?![A-Za-z0-9])|\[object /u;
const NODE_SEP = "\u0001";
const separatedText = (root) => {                                                           // visible text nodes in document order, one separator between neighbours
  const walker = root.ownerDocument.createTreeWalker(root, 4 /* NodeFilter.SHOW_TEXT */);
  const parts = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) if (n.parentElement?.tagName !== "STYLE" && n.nodeValue) parts.push(n.nodeValue);
  return parts.join(NODE_SEP);
};
const suspectSnippet = (text) => {                                                          // ~70 chars around the first hit, separators shown as "|"
  const at = text.search(SUSPECT_TEXT);
  return at < 0 ? null : text.slice(Math.max(0, at - 30), at + 40).replaceAll(NODE_SEP, "|");
};
const CHART_TAGS = /<(LineChart|AreaChart|BarChart|CompositeChart|DonutChart|PieChart|RadarChart|ScatterChart|BubbleChart|RadialBarChart|Sparkline|BarsList)\b/u;
const usableTrack = (n) => 212 * n - 20;                                                  // 212 px logical track minus ~20 px card inset (docs/HOMARR_V2_WIDGETS.md 6.3)

const HERE = dirname(fileURLToPath(import.meta.url));

// ---------------------------------------------------------------- arguments
function parseArgs(argv) {
  const flags = { fixture: [], "allow-warning": [] };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (!a.startsWith("--")) throw new UsageError(`unexpected argument ${a}`);
    const key = a.slice(2);
    if (BOOLEAN_FLAGS.includes(key)) flags[key] = true;
    else if (VALUE_FLAGS.includes(key)) {
      if (i + 1 >= argv.length) throw new UsageError(`--${key} needs a value`);
      if (Array.isArray(flags[key])) flags[key].push(argv[++i]);
      else flags[key] = argv[++i];
    } else throw new UsageError(`unknown flag --${key}`);
  }
  return flags;
}
class UsageError extends Error {}
function readInput(path, what) {                                      // unreadable or malformed input is a usage error (exit 2), not a crash
  try {
    return readFileSync(path, "utf8");
  } catch (error) {
    throw new UsageError(`cannot read ${what} ${path}: ${error.code ?? error.message}`);
  }
}
function parseJson(text, what) {
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new UsageError(`${what} is not valid JSON: ${error.message}`.slice(0, 300));
  }
}
const helpText = () => readFileSync(fileURLToPath(import.meta.url), "utf8").split("*/")[0].replace(/^#!.*\n\/\*\*\n/u, "").replace(/^ \* ?/gmu, "");

// ---------------------------------------------------------------- parent: environment checks, work dir, re-exec under tsx
function environment(flags) {
  const repo = resolve(flags.repo ?? process.env.HOMARR_REPO ?? "/home/ohmz/StudioProjects/homarr");
  const major = Number(process.versions.node.split(".")[0]);
  const tsx = join(repo, "node_modules/.bin/tsx");
  const problems = [];
  if (major < 22) problems.push(`Node >= 22 is required (node:sqlite), found ${process.versions.node}`);
  if (!existsSync(join(repo, "packages/custom-widgets/src/jsx/index.ts"))) problems.push(`${repo} is not a Homarr v2 checkout (packages/custom-widgets missing); set HOMARR_REPO`);
  if (!existsSync(tsx)) problems.push(`${tsx} missing (the checkout needs its node_modules; this tool never installs anything)`);
  const browsers = process.env.PLAYWRIGHT_BROWSERS_PATH ?? join(homedir(), ".cache/ms-playwright");
  const chromium = (existsSync(browsers) ? readdirSync(browsers).filter((d) => /^chromium(_headless_shell)?-/u.test(d)) : []).length > 0;
  const chrome = ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"].find(existsSync) ?? null;
  const shots = existsSync(join(repo, "node_modules/playwright-core")) && existsSync(join(repo, "node_modules/.bin/esbuild")) && (chromium || chrome !== null);
  return { repo, tsx, node: process.versions.node, problems, shots, chromium, chrome };
}

function workDir(repo, flags) {
  const dir = resolve(flags["work-dir"] ?? process.env.RENDER_CHECK_WORK ?? join(tmpdir(), `homelab-maint-render-check-${process.getuid?.() ?? 0}-${createHash("sha1").update(repo).digest("hex").slice(0, 8)}`));
  mkdirSync(dir, { recursive: true });
  // the checkout's node_modules is reached through a symlink NEXT TO the scripts (module resolution walks up from the real file)
  const link = join(dir, "node_modules");
  const target = join(repo, "node_modules");
  const existing = lstatSync(link, { throwIfNoEntry: false });
  if (existing?.isSymbolicLink() && readlinkSync(link) !== target) unlinkSync(link);      // work dir last used for another checkout
  try {
    symlinkSync(target, link);
  } catch (error) {
    if (error.code !== "EEXIST") throw error;                                              // a parallel run created it first
  }
  for (const f of ["render-check.mjs", "preview-entry.tsx"]) copyFileSync(join(HERE, f), join(dir, f));
  return dir;
}

function bootstrap() {
  let flags;
  try {
    flags = parseArgs(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`render-check: ${error.message} (see --help)\n`);
    process.exit(EXIT.USAGE);
  }
  if (flags.help) {
    process.stdout.write(helpText());
    process.exit(EXIT.PASS);
  }
  const env = environment(flags);
  if (flags.probe) {
    process.stdout.write(`${JSON.stringify({ ok: env.problems.length === 0, ...env }, null, 1)}\n`);
    process.exit(env.problems.length ? EXIT.ENV : EXIT.PASS);
  }
  if (env.problems.length) {
    process.stderr.write(`render-check: environment unavailable:\n  - ${env.problems.join("\n  - ")}\n`);
    process.exit(EXIT.ENV);
  }
  const work = workDir(env.repo, flags);
  const timeoutS = Number(flags.timeout ?? 300);
  const result = spawnSync(env.tsx, ["--tsconfig", join(env.repo, "packages/custom-widgets/tsconfig.json"), join(work, "render-check.mjs"), ...process.argv.slice(2)], {
    stdio: "inherit",
    timeout: timeoutS * 1000,
    killSignal: "SIGKILL",           // a synchronous render loop cannot be interrupted from inside Node
    env: { ...process.env, RENDER_CHECK_CHILD: "1", HOMARR_REPO: env.repo, RENDER_CHECK_WORK: work, NODE_NO_WARNINGS: "1" },
  });
  if (result.error?.code === "ETIMEDOUT") {
    process.stderr.write(`render-check: timed out after ${timeoutS} s\n`);
    process.exit(EXIT.TIMEOUT);
  }
  if (result.error) {
    process.stderr.write(`render-check: cannot start tsx: ${result.error.message}\n`);
    process.exit(EXIT.ENV);
  }
  process.exit(result.status ?? EXIT.FAIL);
}

// ---------------------------------------------------------------- child: DOM globals (jsdom) before react-dom/mantine are imported
async function setupDom(state) {
  const { JSDOM } = await import("jsdom");
  const dom = new JSDOM('<!doctype html><html><body><div id="host"></div></body></html>', { pretendToBeVisual: true, url: "http://localhost/" });
  const define = (name, value) => Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  for (const name of Object.getOwnPropertyNames(dom.window)) {       // every jsdom global, the way jest-environment-jsdom exposes them
    if (name in globalThis && !["window", "document", "navigator", "getComputedStyle"].includes(name)) continue;
    try {
      define(name, dom.window[name]);
    } catch {
      /* read-only host property */
    }
  }
  define("window", dom.window);
  define("document", dom.window.document);
  define("navigator", dom.window.navigator);
  define("requestAnimationFrame", (cb) => setTimeout(() => cb(Date.now()), 0));
  define("cancelAnimationFrame", (id) => clearTimeout(id));
  dom.window.matchMedia = (query) => ({ matches: false, media: query, onchange: null, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent: () => false });
  define("matchMedia", dom.window.matchMedia);
  class FakeResizeObserver {                                         // jsdom has no layout: charts get the size of the current job
    constructor(callback) {
      this.callback = callback;
    }
    observe(target) {
      const { w, h } = state.chart;
      const size = { inlineSize: w, blockSize: h };
      const entry = { target, contentRect: { x: 0, y: 0, top: 0, left: 0, width: w, height: h, right: w, bottom: h }, borderBoxSize: [size], contentBoxSize: [size], devicePixelContentBoxSize: [size] };
      setTimeout(() => this.callback([entry], this), 0);
    }
    unobserve() {}
    disconnect() {}
  }
  define("ResizeObserver", FakeResizeObserver);
  dom.window.ResizeObserver = FakeResizeObserver;
  define("IS_REACT_ACT_ENVIRONMENT", true);
  const fmt = (x) => (x instanceof Error ? x.message : typeof x === "string" ? x : (() => { try { return JSON.stringify(x); } catch { return String(x); } })());
  for (const level of ["error", "warn"]) {                           // React / Mantine noise becomes part of the job report
    console[level] = (...args) => {
      const text = args.map(fmt).join(" ").replace(/%[osdOjc]/gu, "").replace(/\s+/gu, " ").trim();
      if (/width\(0\) and height\(0\) of chart/u.test(text)) state.chartSizeWarnings += 1;   // jsdom cannot lay out charts: benign
      else state.consoleLines.add(`${level}: ${text.slice(0, 300)}`);
    };
  }
  return dom;
}

async function loadRuntime(repo) {
  const pkg = `${repo}/packages`;
  const [React, ReactDom, mantine, query, sj, jsx, schema, importer, options, requestSchema, runtime, icon] = await Promise.all([
    import("react"), import("react-dom/client"), import("@mantine/core"), import("@tanstack/react-query"), import("superjson"),
    import(`${pkg}/custom-widgets/src/jsx/index.ts`), import(`${pkg}/custom-widgets/src/core/custom-jsx-schema.ts`),
    import(`${pkg}/custom-widgets/src/core/import.ts`), import(`${pkg}/custom-widgets/src/core/options.ts`),
    import(`${pkg}/custom-widgets/src/core/request-schema.ts`), import(`${pkg}/custom-widgets/src/runtime/index.ts`),
    import(`${pkg}/widgets/src/custom-api/jsx-icon-adapter.tsx`),
  ]);
  let theme;
  try {
    theme = (await import(`${pkg}/ui/src/theme.ts`)).theme;
  } catch {
    theme = undefined;                                               // Mantine's default theme
  }
  return { React, createRoot: ReactDom.createRoot, MantineProvider: mantine.MantineProvider, QueryClient: query.QueryClient, QueryClientProvider: query.QueryClientProvider, superjson: sj.default, jsx, schema, importer, options, requestSchema, runtime, SafeTablerIcon: icon.SafeTablerIcon, theme };
}

// ---------------------------------------------------------------- one job
const jobFromFlags = (f) => ({
  id: f.definition ?? f.template ?? f.name ?? f.id ?? "job", definition: f.definition, db: f.db, name: f.name, defId: f.id, template: f.template, requestId: f["request-id"],
  fixtures: f.fixture, scenario: f.scenario, scheme: f.scheme, size: f.size, tracks: f.tracks, shot: f.shot, opacity: f.opacity, chartSize: f["chart-size"],
  optionsJson: f["options-json"], htmlOut: f["html-out"], text: f.text, noDom: f["no-dom"], allowWarnings: f["allow-warning"],
});

function tileSize(job) {
  const pair = (s, what) => {
    const m = /^(\d+)x(\d+)$/u.exec(String(s));
    if (!m) throw new UsageError(`${what} must look like 616x616, got ${s}`);
    return [Number(m[1]), Number(m[2])];
  };
  if (job.size) return pair(job.size, "--size");
  if (job.tracks) return pair(job.tracks, "--tracks").map(usableTrack);
  return [usableTrack(3), usableTrack(3)];
}

function loadDefinition(rt, job) {
  if (job.db) {
    const { DatabaseSync } = rt.sqlite;
    const db = new DatabaseSync(job.db, { readOnly: true });
    const row = job.defId
      ? db.prepare("select * from custom_widget_v2_definition where id = ?").get(job.defId)
      : db.prepare("select * from custom_widget_v2_definition where name = ?").get(job.name);
    db.close();
    if (!row) throw new UsageError(`definition not found (id=${job.defId} name=${job.name})`);
    const parse = (value) => rt.superjson.parse(value);
    return { id: row.id, raw: { $schema: "homarr-custom-widget-v2", name: row.name, description: row.description ?? undefined, iconUrl: row.icon_url ?? undefined, sources: parse(row.sources), requests: parse(row.requests), options: parse(row.options), template: row.template } };
  }
  if (job.definition) {
    const text = readInput(job.definition, "definition");
    return { id: job.definition, raw: parseJson(text, job.definition), text };
  }
  if (job.template) {
    const requestId = job.requestId ?? "state";
    return { id: job.template, raw: { $schema: "homarr-custom-widget-v2", name: "template-under-test", sources: { default: { baseUrl: "http://127.0.0.1:9111", networkScope: "loopback", auth: "none" } }, requests: { [requestId]: { path: "/" } }, options: {}, template: readInput(job.template, "template") } };
  }
  throw new UsageError("provide --definition, --db (--name|--id) or --template");
}

async function loadFixture(spec) {
  if (/^https?:\/\//u.test(spec)) {
    const response = await fetch(spec, { headers: { Accept: "application/json" }, signal: AbortSignal.timeout(8000) });
    const text = await response.text();
    try {
      return { body: JSON.parse(text), source: `${spec} (HTTP ${response.status})`, bytes: text.length };
    } catch {
      return { body: text, source: `${spec} (HTTP ${response.status}, non-JSON text)`, bytes: text.length };
    }
  }
  const text = readInput(spec, "fixture");
  return { body: parseJson(text, spec), source: spec, bytes: text.length };
}

// light-dark(...) spans: a colour literal inside one is fine, outside it breaks one of the two schemes
function advisories(template, loadIds) {
  const out = [];
  if (loadIds.length > 4) out.push(`${loadIds.length} load requests: the limiter allows only 4 concurrent requests per user/item, extras fail`);
  const spans = [];
  for (const match of template.matchAll(/light-dark\(/gu)) {
    let depth = 1;
    let end = match.index + match[0].length;
    while (end < template.length && depth > 0) {
      depth += template[end] === "(" ? 1 : template[end] === ")" ? -1 : 0;
      end += 1;
    }
    spans.push([match.index, end]);
  }
  for (const m of template.matchAll(/#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)/gu)) {
    if (spans.some(([a, b]) => m.index >= a && m.index < b)) continue;
    out.push(`hard-coded colour ${m[0]} outside light-dark() (use theme tokens so dark and light both work)`);
    break;
  }
  const stripped = [...new Set([...template.matchAll(/\b(position|zIndex|backdropFilter|clipPath|pointerEvents|inset|mask|filter|behavior)\s*:/gu)].map((m) => m[1]))];
  if (stripped.length) out.push(`style key(s) ${stripped.join(", ")} are silently stripped by the runtime`);
  return out;
}

async function runJob(rt, state, job, g) {
  const report = { id: job.id, definition: undefined, schema: { ok: false, issues: [] }, import: null, analyzer: { errors: [], warnings: [] }, limits: {}, interpreter: { ok: false, error: null, warnings: [], budgets: {} }, dom: { ran: false }, advisories: [], failures: [] };
  const scenario = job.scenario ?? "ok";
  const scheme = job.scheme ?? "dark";
  if (!SCENARIOS.includes(scenario)) throw new UsageError(`scenario must be one of ${SCENARIOS.join("|")}`);
  if (!["dark", "light"].includes(scheme)) throw new UsageError("scheme must be dark or light");
  const [tileW, tileH] = tileSize(job);
  const [chartW, chartH] = String(job.chartSize ?? "400x160").split("x").map(Number);
  state.chart = { w: chartW, h: chartH };
  state.consoleLines = new Set();
  state.chartSizeWarnings = 0;
  const { schema, jsx } = rt;

  const { id: definitionId, raw, text: rawText } = loadDefinition(rt, job);
  report.definition = { id: definitionId, name: raw.name, templateChars: String(raw.template ?? "").length };
  // 1. full-definition schema: what the server re-runs on EVERY getData and on save/import
  const parsed = schema.customWidgetDefinitionSchema.safeParse(raw);
  if (parsed.success) report.schema.ok = true;
  else report.schema.issues = parsed.error.issues.map((i) => ({ path: i.path.join("."), message: i.message }));
  const preview = schema.customWidgetPreviewDefinitionSchema.safeParse(raw);     // the workbench/MCP gate is stricter
  report.schema.previewOk = preview.success;
  if (!preview.success) report.schema.previewIssues = preview.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`);
  // 1b. the exact parser behind Manage > Custom widgets > Import / paste (only for a definition file)
  if (rawText !== undefined) {
    const imported = rt.importer.parseCustomWidgetClipboardDetailed(rawText);
    report.import = imported.success ? { ok: true, review: rt.importer.getImportReview(raw) } : { ok: false, issues: imported.issues };
  }
  const definition = parsed.success ? parsed.data : raw;
  const template = parsed.success ? parsed.data.template : schema.normalizeCustomJsxAuthoringTemplate(String(raw.template ?? ""));
  // 2. analyzer diagnostics
  for (const d of jsx.validateCustomJsxTemplate(template)) (d.severity === "error" ? report.analyzer.errors : report.analyzer.warnings).push(`${d.message} (line ${d.line}, col ${d.column})`);
  // 3. limits
  const loadIds = Object.entries(definition.requests ?? {}).filter(([, r]) => (r.kind ?? "query") === "query" && (r.trigger ?? "load") === "load").map(([id]) => id);
  report.limits = { templateChars: `${template.length} / ${jsx.CUSTOM_JSX_LIMITS.templateLength}`, requests: `${Object.keys(definition.requests ?? {}).length} / 64`, loadRequests: loadIds };
  // 4. fixtures -> data/status per the scenario
  const fixtures = {};
  for (const spec of Array.isArray(job.fixtures) ? job.fixtures : Object.entries(job.fixtures ?? {}).map(([k, v]) => `${k}=${v}`)) {
    const eq = spec.indexOf("=");
    const named = eq > 0 && !/^https?:/u.test(spec.slice(0, eq)) && !spec.slice(0, eq).includes("/");
    const id = named ? spec.slice(0, eq) : loadIds[0];
    if (!id) throw new UsageError("no load request to attach the fixture to");
    fixtures[id] = await loadFixture(named ? spec.slice(eq + 1) : spec);
  }
  const data = {};
  const status = {};
  const okStatus = { loading: false, ok: true, status: 200, statusText: "OK" };
  for (const id of loadIds) {
    const body = fixtures[id] ? fixtures[id].body : null;
    if (scenario === "network-error") [data[id], status[id]] = [null, { loading: false, ok: false, status: 0, error: "External request failed" }];
    else if (scenario === "http-error") [data[id], status[id]] = [body, { loading: false, ok: false, status: 500, statusText: "Internal Server Error", error: "HTTP 500: Internal Server Error" }];
    else if (scenario === "empty") [data[id], status[id]] = [{}, okStatus];
    else if (scenario === "null") [data[id], status[id]] = [null, okStatus];
    else [data[id], status[id]] = [body, okStatus];
  }
  report.fixtures = Object.fromEntries(Object.entries(fixtures).map(([id, f]) => [id, `${f.source}, ${f.bytes} bytes`]));
  report.scenario = scenario;
  const options = { ...rt.options.getCustomWidgetDefaultOptions(definition.options ?? {}), ...(job.optionsJson ? parseJson(job.optionsJson, "--options-json") : {}) };
  // 5. interpreter-only pass (precise errors, warnings, budget usage by binary search)
  const components = jsx.createCustomJsxComponents({ TablerIcon: rt.SafeTablerIcon, copyLabels: { copy: "Copy", copied: "Copied" } });
  const interpret = (budgets) => jsx.renderSafeJsx({ template, components, bindings: { ...jsx.createCustomJsxBindings(data), status, options, inputs: {} }, budgets });
  try {
    report.interpreter.warnings = interpret().warnings;
    report.interpreter.ok = true;
    const L = jsx.CUSTOM_JSX_LIMITS;
    const minimum = (key, ceiling) => {
      let [lo, hi] = [1, ceiling];
      while (lo < hi) {
        const mid = Math.floor((lo + hi) / 2);
        try {
          interpret({ [key]: mid });
          hi = mid;
        } catch {
          lo = mid + 1;
        }
      }
      return lo;
    };
    report.interpreter.budgets = { operations: `${minimum("maxOperations", L.operations)} / ${L.operations}`, renderedNodes: `${minimum("maxRenderedNodes", L.renderedNodes)} / ${L.renderedNodes}`, astDepth: `${minimum("maxAstDepth", L.astDepth)} / ${L.astDepth}` };
  } catch (error) {
    report.interpreter.error = `${error?.name ?? "Error"}: ${error?.message ?? error}`;
  }
  report.advisories = advisories(template, loadIds);

  // 6. real React render through CustomJsxRenderer in jsdom
  const capabilities = rt.runtime.parseRequestCapabilities(Object.entries(definition.requests ?? {}).map(([id, r]) => ({
    id, kind: r.kind ?? "query", method: r.method ?? "GET", trigger: r.kind === "action" ? "manual" : (r.trigger ?? "load"),
    minimumBoardPermission: r.permission ?? (r.kind === "action" ? "modify" : "view"),
    confirmation: r.confirmation ? rt.requestSchema.getCustomWidgetConfirmation({ confirmation: r.confirmation, method: r.method }) : undefined, invalidates: r.invalidates ?? [],
  })));
  const messages = { requestIdRequired: "id required", unsavedPreview: "unsaved", invalidParams: "invalid params", loadRequest: "Load request", requestFailed: "The request failed.", loading: "Loading request...", retry: "Retry request", widgetItemUnavailable: "unavailable", actionsDisabledEditMode: "edit mode", actionSimulated: "simulated", actionCompleted: "completed", confirmDelete: "confirm", toggle: "Toggle", refresh: "Refresh" };
  const rendererMessages = { noTemplate: "No JSX template is configured.", templateWarnings: (n) => `${n} template warnings:`, bindingTypeConflict: (name, a, b) => `Input ${name} conflicts between ${a} and ${b}` };
  const port = { query: async ({ requestId }) => ({ ok: true, status: 200, data: data[requestId] ?? null }), executeAction: async () => ({ ok: true, status: 200, data: null, simulated: true }), invalidate: async () => undefined, confirm: async () => true, notify: () => undefined };
  if (!job.noDom) {
    const { createElement: h } = rt.React;
    const host = state.dom.window.document.getElementById("host");
    host.innerHTML = "";
    const root = rt.createRoot(host);
    const element = h(rt.MantineProvider, { theme: rt.theme, defaultColorScheme: scheme, forceColorScheme: scheme },
      h(rt.QueryClientProvider, { client: new rt.QueryClient({ defaultOptions: { queries: { retry: false } } }) },
        h(rt.runtime.CustomWidgetRuntimeProvider, { itemId: "harness-item", definitionId, isEditMode: false, requestCapabilities: capabilities, port, messages },
          h(rt.runtime.CustomJsxRenderer, { template, data, status, options, components, createBindings: jsx.createCustomJsxBindings, messages: rendererMessages }))));
    try {
      await rt.React.act(async () => root.render(element));
      await rt.React.act(async () => { await new Promise((r) => setTimeout(r, 120)); });
      const clone = host.cloneNode(true);
      clone.querySelectorAll("style").forEach((el) => el.remove());           // MantineProvider injects <style> tags: not visible text
      const text = (clone.textContent ?? "").trim();
      const warnNode = [...host.querySelectorAll("*")].find((el) => /template warnings:/u.test(el.textContent ?? "") && el.children.length === 0);
      report.dom = {
        ran: true, scheme, htmlChars: host.innerHTML.length, domNodes: host.querySelectorAll("*").length,
        runtimeRenderError: text.includes("RUNTIME_RENDER_ERROR") ? (text.split("RUNTIME_RENDER_ERROR")[1] ?? "").slice(0, 300) : null,
        templateWarningsAlert: warnNode ? ((warnNode.closest(".mantine-Alert-root") ?? warnNode.parentElement)?.textContent ?? "").slice(0, 600) : null,
        suspectText: suspectSnippet(separatedText(clone)),
        reactConsole: [...state.consoleLines], chartSizeWarningsIgnored: state.chartSizeWarnings, text,
      };
      if (job.htmlOut) writeFileSync(job.htmlOut, host.innerHTML);
    } catch (error) {
      report.dom = { ran: true, thrown: `${error?.name}: ${error?.message}`, reactConsole: [...state.consoleLines] };
    }
    await rt.React.act(async () => root.unmount());
  }
  // 7. optional real-Chromium screenshot
  if (job.shot) report.shot = await screenshot(state, g, job, { template, data, status, options, scheme, tileW, tileH, definition });

  // verdict
  const f = report.failures;
  if (!report.schema.ok) f.push(`schema: ${report.schema.issues.map((i) => `${i.path}: ${i.message}`).join("; ").slice(0, 300)}`);
  if (!report.schema.previewOk) f.push("preview schema failed");
  if (report.import && !report.import.ok) f.push(`import parser: ${JSON.stringify(report.import.issues).slice(0, 300)}`);
  f.push(...report.analyzer.errors.map((e) => `analyzer: ${e}`));
  const allowed = [...DEFAULT_ALLOWED_WARNINGS, ...(job.allowWarnings ?? [])];
  f.push(...report.analyzer.warnings.filter((w) => !allowed.some((a) => w.includes(a))).map((w) => `analyzer warning: ${w}`));
  if (!report.interpreter.ok) f.push(`interpreter: ${report.interpreter.error}`);
  f.push(...report.interpreter.warnings.map((w) => `interpreter warning: ${w}`));
  if (report.dom.runtimeRenderError) f.push(`RUNTIME_RENDER_ERROR: ${report.dom.runtimeRenderError.slice(0, 120)}`);
  if (report.dom.thrown) f.push(`dom: ${report.dom.thrown}`);
  if (report.dom.templateWarningsAlert) f.push(`template warnings alert: ${report.dom.templateWarningsAlert.slice(0, 160)}`);
  if (report.dom.suspectText) f.push(`suspect text: ...${report.dom.suspectText}...`);
  f.push(...(report.dom.reactConsole ?? []).filter((l) => l.startsWith("error:")).map((l) => `console ${l}`));
  if (report.shot) {
    if (!report.shot.ok) f.push(`shot: ${report.shot.error ?? "no screenshot"}`);
    if (report.shot.metrics?.runtimeRenderError) f.push("shot: RUNTIME_RENDER_ERROR");
    if (report.shot.metrics?.templateWarnings) f.push(`shot: ${report.shot.metrics.templateWarnings.slice(0, 120)}`);
    if (report.shot.metrics?.clippedChartLabels?.length) f.push(`shot: chart labels clipped by the svg viewport: ${report.shot.metrics.clippedChartLabels.join(", ")}`);
    f.push(...(report.shot.chromeConsole ?? []).filter((l) => /^(error|pageerror)/u.test(l)).map((l) => `chromium ${l}`));
  }
  report.failed = f.length > 0;
  return report;
}

// ---------------------------------------------------------------- screenshot (esbuild bundle of the real runtime + Playwright Chromium)
async function bundle(state, g) {
  if (state.bundle) return state.bundle;
  const esbuild = join(g.repo, "node_modules/.bin/esbuild");
  if (!existsSync(esbuild)) return (state.bundle = { error: "esbuild missing in the checkout's node_modules" });
  const out = join(g.work, `bundle-${process.pid}`);
  mkdirSync(out, { recursive: true });
  const r = spawnSync(esbuild, [join(g.work, "preview-entry.tsx"), "--bundle", `--outdir=${out}`, "--jsx=automatic", "--target=chrome120", `--alias:@homarr-repo=${g.repo}`, '--define:process.env.NODE_ENV="production"', "--log-level=error"], { encoding: "utf8" });
  state.bundle = r.status === 0 ? { dir: out } : { error: `esbuild failed: ${(r.stderr || r.stdout).slice(0, 500)}` };
  return state.bundle;
}

async function browser(state, g) {
  if (state.browser) return state.browser;
  const { chromium } = await import("playwright-core");
  const chrome = ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"].find(existsSync);
  try {
    state.browser = await chromium.launch({ args: ["--no-sandbox"] });                       // Playwright's own Chromium
  } catch {
    state.browser = await chromium.launch({ executablePath: chrome, args: ["--no-sandbox"] }); // system Chrome fallback
  }
  return state.browser;
}

async function screenshot(state, g, job, c) {
  const shot = { out: resolve(job.shot), size: `${c.tileW}x${c.tileH}`, scheme: c.scheme };
  const b = await bundle(state, g);
  if (b.error) return { ...shot, error: b.error };
  const config = {
    template: c.template, data: c.data, status: c.status, options: c.options, scheme: c.scheme, width: c.tileW, height: c.tileH, opacity: Number(job.opacity ?? 1),
    requests: Object.entries(c.definition.requests ?? {}).map(([id, r]) => ({ id, kind: r.kind ?? "query", method: r.method ?? "GET", trigger: r.kind === "action" ? "manual" : (r.trigger ?? "load"), minimumBoardPermission: r.permission ?? (r.kind === "action" ? "modify" : "view"), invalidates: r.invalidates ?? [] })),
  };
  const dark = c.scheme === "dark";
  const page = join(b.dir, `page-${state.jobNo += 1}.html`);
  writeFileSync(page, `<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="preview-entry.css"><style>
html,body{margin:0;padding:0}
body{padding:12px;background:${dark ? "#0d0c0b" : "#ece8e3"};width:${c.tileW + 24}px;height:${c.tileH + 24}px;box-sizing:border-box;overflow:hidden}
/* widget-card-shell.module.css equivalents (the real file uses Mantine postcss mixins, which esbuild cannot compile) */
.harness-card{background-color:${dark ? "rgb(from var(--mantine-color-dark-6) r g b / var(--opacity))" : "rgb(from var(--mantine-color-white) r g b / var(--opacity))"} !important;border-color:${dark ? "rgb(from var(--mantine-color-dark-4) r g b / var(--opacity))" : "rgb(from var(--mantine-color-gray-3) r g b / var(--opacity))"} !important}
</style></head><body><div id="root"></div><script>window.__PREVIEW__=${JSON.stringify(config).replace(/</gu, "\\u003c")};</script><script src="preview-entry.js"></script></body></html>`);
  let context;
  try {
    context = await (await browser(state, g)).newContext({ viewport: { width: c.tileW + 24, height: c.tileH + 24 }, deviceScaleFactor: 2, colorScheme: c.scheme });
    const pageHandle = await context.newPage();
    const chromeConsole = [];
    pageHandle.on("console", (m) => { if (["error", "warning"].includes(m.type())) chromeConsole.push(`${m.type()}: ${m.text().slice(0, 300)}`); });
    pageHandle.on("pageerror", (e) => chromeConsole.push(`pageerror: ${String(e.message).slice(0, 300)}`));
    await pageHandle.goto(pathToFileURL(page).href);
    await pageHandle.waitForSelector(".harness-card", { timeout: 15000 });
    await pageHandle.waitForTimeout(CHART_TAGS.test(c.template) ? 1000 : 150);     // ResizeObserver-driven charts need a moment
    mkdirSync(dirname(shot.out), { recursive: true });
    await pageHandle.locator(".harness-card").screenshot({ path: shot.out });
    shot.metrics = await pageHandle.evaluate(() => {
      const tile = document.querySelector(".harness-card");
      const box = tile?.querySelector(":scope > div > div");             // CustomJsxRenderer's scroll box
      const text = (tile?.textContent ?? "").trim();
      const kids = box ? [...box.children] : [];
      const top = box ? box.getBoundingClientRect().top : 0;
      const clipped = [];                                                  // chart labels cut off sideways by their own svg viewport (a too narrow y axis; the top tick may overlap the svg's top edge vertically without losing glyphs)
      for (const svg of tile?.querySelectorAll("svg") ?? []) {
        const b = svg.getBoundingClientRect();
        for (const label of svg.querySelectorAll("text")) {
          const r = label.getBoundingClientRect();
          if (r.width && (r.left < b.left - 0.5 || r.right > b.right + 0.5)) clipped.push(`${(label.textContent ?? "").trim()} (${(Math.max(b.left - r.left, r.right - b.right)).toFixed(1)} px)`);
        }
      }
      // WCAG contrast of every text element as drawn: colours are resolved through a 1x1 canvas (so light-dark() / color-mix() / var() work), translucent
      // ancestor backgrounds are composited over white, and the product of the ancestors' opacity fades the text (a stale tile is drawn at 0.55 on purpose)
      const cvs = document.createElement("canvas");
      cvs.width = cvs.height = 1;
      const ctx = cvs.getContext("2d", { willReadFrequently: true });
      const rgba = (css) => { ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = "#000"; ctx.fillStyle = css; ctx.fillRect(0, 0, 1, 1); const d = ctx.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
      const over = (t, b) => { const a = t[3] + b[3] * (1 - t[3]); return a ? [0, 1, 2].map((i) => (t[i] * t[3] + b[i] * b[3] * (1 - t[3])) / a).concat(a) : [0, 0, 0, 0]; };
      const lin = (c) => { const v = c / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
      const lum = (c) => 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2]);
      const contrast = (a, b) => { const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };
      const weak = [];
      let measured = 0;
      for (const el of tile?.querySelectorAll("*") ?? []) {
        if (el.closest("svg, style") || ![...el.childNodes].some((n) => n.nodeType === 3 && n.nodeValue.trim())) continue;
        let bg = [255, 255, 255, 1];
        let opacity = 1;
        const chain = [];
        for (let n = el; n; n = n.parentElement) chain.unshift(n);
        for (const n of chain) { const cs = getComputedStyle(n); bg = over(rgba(cs.backgroundColor), bg); opacity *= Number(cs.opacity); }
        const cs = getComputedStyle(el);
        const fg = rgba(cs.color);
        const shown = over([fg[0], fg[1], fg[2], fg[3] * opacity], bg);
        const size = parseFloat(cs.fontSize);
        const large = size >= 24 || (size >= 18.66 && Number(cs.fontWeight) >= 700);       // WCAG "large text": 3:1 instead of 4.5:1
        const ratio = contrast(shown, bg);
        measured += 1;
        if (ratio < (large ? 3 : 4.5)) weak.push({ text: el.textContent.trim().slice(0, 40), ratio: Math.round(ratio * 100) / 100, need: large ? 3 : 4.5, size, fg: `rgb(${fg.slice(0, 3).map(Math.round)})`, bg: `rgb(${bg.slice(0, 3).map(Math.round)})`, opacity });
      }
      weak.sort((a, b) => a.ratio - b.ratio);
      return {
        lowContrastText: weak.slice(0, 12), lowContrastCount: weak.length, textElementsMeasured: measured,
        clippedChartLabels: clipped,
        tile: { w: tile?.clientWidth, h: tile?.clientHeight },
        contentNaturalHeight: kids.length ? Math.ceil(Math.max(...kids.map((k) => k.getBoundingClientRect().bottom)) - top + (box?.scrollTop ?? 0)) : null,
        contentScrollHeight: box?.scrollHeight ?? null, contentClientHeight: box?.clientHeight ?? null,
        contentOverflowsTile: box ? box.scrollHeight > box.clientHeight + 1 : null,
        horizontalOverflow: box ? box.scrollWidth > box.clientWidth + 1 || (tile?.scrollWidth ?? 0) > (tile?.clientWidth ?? 0) + 1 : null,
        svgCount: tile?.querySelectorAll("svg").length ?? 0,
        runtimeRenderError: text.includes("RUNTIME_RENDER_ERROR"),
        templateWarnings: /\d+ template warnings:/u.test(text) ? text.slice(text.search(/\d+ template warnings:/u)).slice(0, 400) : null,
      };
    });
    shot.chromeConsole = chromeConsole;
    shot.ok = existsSync(shot.out);
  } catch (error) {
    shot.error = `${error?.name}: ${error?.message}`.slice(0, 400);
  } finally {
    await context?.close();
    rmSync(page, { force: true });
  }
  return shot;
}

// ---------------------------------------------------------------- output
function printReport(r, withText) {
  const line = (label, value) => process.stdout.write(`${label.padEnd(24)}${value}\n`);
  const list = (items) => (items.length ? `\n    - ${items.join("\n    - ")}` : "none");
  line("definition", `${r.definition.name} (${r.id}) template ${r.definition.templateChars} chars`);
  line("schema (full def)", r.schema.ok ? "OK" : `FAIL${list(r.schema.issues.map((i) => `${i.path}: ${i.message}`))}`);
  if (r.schema.previewIssues?.length) line("schema (preview)", `FAIL${list(r.schema.previewIssues)}`);
  if (r.import) line("import parser", r.import.ok ? `OK ${JSON.stringify(r.import.review)}` : `FAIL ${JSON.stringify(r.import.issues)}`);
  line("analyzer errors", list(r.analyzer.errors));
  line("analyzer warnings", list(r.analyzer.warnings));
  line("limits", JSON.stringify(r.limits));
  line("fixtures", `${JSON.stringify(r.fixtures)}  scenario=${r.scenario}`);
  line("interpreter", r.interpreter.ok ? "OK" : `FAIL ${r.interpreter.error}`);
  if (r.interpreter.ok) line("  budget used (min)", JSON.stringify(r.interpreter.budgets));
  line("  unknown/blocked", list(r.interpreter.warnings));
  if (r.dom.ran && !r.dom.thrown) {
    line("dom render", `${r.dom.domNodes} nodes, ${r.dom.htmlChars} html chars (scheme ${r.dom.scheme})`);
    line("  RUNTIME_RENDER_ERROR", r.dom.runtimeRenderError ?? "none");
    line("  warnings alert", r.dom.templateWarningsAlert ?? "none");
    line("  react/mantine console", list(r.dom.reactConsole));
    if (withText) line("  text", r.dom.text);
  } else if (r.dom.thrown) line("dom render", `THROWN ${r.dom.thrown}`);
  if (r.shot) line("chromium screenshot", r.shot.ok ? `${r.shot.out} (${r.shot.size}, ${r.shot.scheme}) ${JSON.stringify(r.shot.metrics)}${r.shot.chromeConsole?.length ? list(r.shot.chromeConsole) : ""}` : `FAILED ${r.shot.error}`);
  line("advisories", list(r.advisories));
  line("RESULT", r.failed ? `FAIL${list(r.failures)}` : "PASS");
}

async function main() {
  let flags;
  try {
    flags = parseArgs(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`render-check: ${error.message}\n`);
    process.exit(EXIT.USAGE);
  }
  const g = { repo: process.env.HOMARR_REPO, work: process.env.RENDER_CHECK_WORK };
  const state = { chart: { w: 400, h: 160 }, consoleLines: new Set(), chartSizeWarnings: 0, jobNo: 0, bundle: null, browser: null, dom: null };
  const write = (text) => new Promise((done) => process.stdout.write(text, done));
  let code = EXIT.PASS;
  try {
    let jobs;
    if (flags.batch) {
      const file = parseJson(readInput(flags.batch, "batch file"), flags.batch);
      jobs = (Array.isArray(file) ? file : file.jobs).map((j) => ({ ...(file.defaults ?? {}), ...j }));
    } else jobs = [jobFromFlags(flags)];
    state.dom = await setupDom(state);
    const rt = await loadRuntime(g.repo);
    rt.sqlite = jobs.some((j) => j.db) ? await import("node:sqlite") : null;
    const reports = [];
    for (const job of jobs) {
      const report = await runJob(rt, state, job, g);
      if (!flags.json && !flags.batch) printReport(report, Boolean(job.text));
      if (!job.text) delete report.dom.text;                              // keep batch reports small unless asked
      reports.push(report);
      if (flags.batch && !flags.json) await write(`${report.failed ? "FAIL" : "PASS"} ${report.id}${report.failed ? `  ${report.failures[0]}` : ""}\n`);
    }
    const failed = reports.filter((r) => r.failed).length;
    if (flags.json) await write(`${JSON.stringify(flags.batch ? { reports, failed } : reports[0], null, 1)}\n`);
    else if (flags.batch) await write(`${reports.length - failed}/${reports.length} passed\n`);
    code = failed ? EXIT.FAIL : EXIT.PASS;
  } catch (error) {
    process.stderr.write(`render-check: ${error instanceof UsageError ? error.message : error?.stack ?? error}\n`);
    code = error instanceof UsageError ? EXIT.USAGE : EXIT.FAIL;
  } finally {
    await state.browser?.close();
    if (state.bundle?.dir) rmSync(state.bundle.dir, { recursive: true, force: true });
  }
  process.exit(code);
}

// ---------------------------------------------------------------- entry (last, so every const above is initialised)
if (process.env.RENDER_CHECK_CHILD !== "1") bootstrap();
else await main();
