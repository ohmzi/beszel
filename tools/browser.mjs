// A small headless-Chrome driver over the DevTools protocol (node >= 22, no dependencies), used by web/tests/test_frontend.mjs.
// One Chrome per session; every visit() collects console errors, exceptions, CSP violations and failed requests into `problems`.
//
//   const b = await launch({ w: 1440, h: 900, scheme: 'light' });
//   await b.visit('http://127.0.0.1:8099/?fixture=ok#/health');      // waits until the tab has rendered (.view[data-ready])
//   const n = await b.eval('document.querySelectorAll(".chk").length');
//   b.problems  ->  [] when the page was clean
//   await b.close();
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export const chromeBinary = () => process.env.CHROME || 'google-chrome';
export async function haveChrome() {
  return await new Promise((res) => { const p = spawn(chromeBinary(), ['--version'], { stdio: 'ignore' }); p.on('error', () => res(false)); p.on('exit', (c) => res(c === 0)); });
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const DEADLINE = 60000;      // a DevTools call that gets no answer in this long fails (with its name) instead of freezing the whole test run for ever

// mobile: false by default. Mobile emulation silently WIDENS the layout viewport when the page overflows (innerWidth became 487 at "390 px"), which hides
// exactly the horizontal overflow the 390 px checks are looking for. Pass mobile: true only to test touch/meta-viewport behaviour.
export async function launch({ w = 1440, h = 900, scheme = 'light', tz = 'America/Toronto', dsf = 1, reducedMotion = true, mobile = false } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'hm-chrome-'));
  const chrome = spawn(chromeBinary(), ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${dir}`, '--no-first-run', '--no-default-browser-check', '--disable-gpu',
    '--hide-scrollbars', '--force-color-profile=srgb', '--no-sandbox', 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
  const port = await new Promise((res, rej) => {
    let buf = '';
    chrome.stderr.on('data', (d) => { buf += d; const m = buf.match(/ws:\/\/127\.0\.0\.1:(\d+)\//); if (m) res(m[1]); });
    chrome.on('exit', () => rej(new Error('chrome exited')));
    setTimeout(() => rej(new Error('chrome did not start')), 20000).unref();
  });
  const cleanup = () => { try { chrome.kill('SIGKILL'); } catch { /* gone */ } try { rmSync(dir, { recursive: true, force: true }); } catch { /* best effort */ } };
  let ws;
  try {                                                  // a Chrome that started but never answered must not outlive the failed launch
    let target;
    for (let i = 0; i < 40 && !target; i++) {
      target = (await (await fetch(`http://127.0.0.1:${port}/json/list`, { signal: AbortSignal.timeout(5000) })).json()).find((t) => t.type === 'page');
      if (!target) await sleep(100);
    }
    if (!target) throw new Error('chrome has no page to drive');
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', () => rej(new Error('DevTools socket failed'))); setTimeout(() => rej(new Error('DevTools socket did not open')), 10000).unref(); });
  } catch (e) { cleanup(); throw e; }
  let id = 0, events = [];
  const pend = new Map();
  ws.addEventListener('message', (m) => {
    const d = JSON.parse(m.data);
    if (d.id) { pend.get(d.id)?.(d); pend.delete(d.id); } else events.push(d);
  });
  const send = (method, params = {}) => new Promise((res, rej) => {
    const i = ++id, t = setTimeout(() => { pend.delete(i); rej(new Error(`${method}: Chrome gave no answer in ${DEADLINE / 1000} s`)); }, DEADLINE);
    pend.set(i, (d) => { clearTimeout(t); return d.error ? rej(new Error(`${method}: ${d.error.message}`)) : res(d.result); });
    ws.send(JSON.stringify({ id: i, method, params }));
  });
  try { await Promise.all(['Page', 'Runtime', 'Log', 'Network'].map((d) => send(`${d}.enable`))); } catch (e) { cleanup(); throw e; }
  const media = (s) => send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: s }, { name: 'prefers-reduced-motion', value: reducedMotion ? 'reduce' : 'no-preference' }] });
  const viewport = (W, H, D = dsf) => send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: D, mobile });
  await send('Emulation.setFocusEmulationEnabled', { enabled: true }).catch(() => {});   // headless pages otherwise never fire focus events
  await send('Page.bringToFront').catch(() => {});
  await viewport(w, h);
  await media(scheme);
  await send('Emulation.setTimezoneOverride', { timezoneId: tz });
  await send('Emulation.setLocaleOverride', { locale: 'en-US' }).catch(() => {});

  const b = {
    problems: [], send,
    async eval(expr) {
      const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw new Error(`${r.exceptionDetails.text} ${r.exceptionDetails.exception?.description || ''} in: ${expr.slice(0, 120)}`);
      return r.result.value;
    },
    async waitFor(expr, ms = 8000) {
      const end = Date.now() + ms;
      for (;;) {
        let v; try { v = await b.eval(expr); } catch { v = false; }
        if (v) return v;
        if (Date.now() > end) throw new Error(`timeout waiting for: ${expr}`);
        await sleep(40);
      }
    },
    collect() {                                           // console errors, exceptions, CSP reports, failed requests since the last call
      const out = [];
      for (const e of events) {
        const p = e.params;
        if (e.method === 'Runtime.exceptionThrown') out.push(`EXCEPTION ${p.exceptionDetails.text} ${p.exceptionDetails.exception?.description?.split('\n')[0] || ''}`);
        else if (e.method === 'Runtime.consoleAPICalled' && ['error', 'assert'].includes(p.type)) out.push(`console.${p.type} ${p.args.map((a) => a.value ?? a.description).join(' ').split('\n')[0]}`);
        else if (e.method === 'Log.entryAdded' && ['error', 'warning'].includes(p.entry.level)) out.push(`log.${p.entry.source} ${p.entry.text} ${p.entry.url || ''}`);
        else if (e.method === 'Network.loadingFailed' && !p.canceled) out.push(`REQUEST FAILED ${p.errorText} ${p.requestId}`);
        else if (e.method === 'Network.responseReceived' && p.response.status >= 400 && !/\/api\//.test(p.response.url)) out.push(`HTTP ${p.response.status} ${p.response.url}`);
      }
      events = [];
      b.problems.push(...out);
      return out;
    },
    // navigate and wait for the tab to finish its first render (the shell marks .view[data-ready])
    async visit(url, ready = 'document.querySelector("#view > .view[data-ready]")') {
      events = []; b.problems = [];
      await send('Page.navigate', { url });
      await b.waitFor(ready);
      await sleep(250);                                   // charts are drawn after layout (ResizeObserver)
      b.collect();
    },
    async goto(hash, ready) {                             // same-document navigation to another tab
      await b.eval(`location.hash = ${JSON.stringify(hash)}`);
      await b.waitFor(ready || `document.querySelector("#view > .view[data-ready]") && location.hash === ${JSON.stringify(hash)}`);
      await sleep(250);
      b.collect();
    },
    async resize(W, H) { await viewport(W, H); await sleep(150); },
    scheme: (s) => media(s),
    async shot(file, full = false) {
      let opts = { format: 'png' };
      if (full) { const h = await b.eval('document.documentElement.scrollHeight'), W = await b.eval('innerWidth'); opts = { ...opts, captureBeyondViewport: true, clip: { x: 0, y: 0, width: W, height: h, scale: 1 } }; }
      writeFileSync(file, Buffer.from((await send('Page.captureScreenshot', opts)).data, 'base64'));
    },
    sleep,
    async close() { try { ws.close(); } catch { /* already closed */ } try { chrome.kill('SIGKILL'); } catch { /* gone */ } await sleep(50); try { rmSync(dir, { recursive: true, force: true }); } catch { /* best effort */ } },
  };
  return b;
}
