#!/usr/bin/env node
// Headless-Chrome inspector over the DevTools protocol (node >= 22, no dependencies).
// Loads a URL at a given size/colour scheme/timezone, reports console errors, CSP violations, failed requests and
// horizontal overflow, and writes a screenshot (viewport, full page, or one element).
//
//   node web/tools/inspect.mjs --url 'http://127.0.0.1:8099/?fixture=ok' --w 390 --h 844 --out shot.png
//        [--scheme dark|light] [--tz America/Toronto] [--dsf 2] [--full] [--el '#storage'] [--hover '.chart svg']
//        [--click '#theme'] [--eval 'js expression'] [--wait 1500] [--brief] [--mobile 1]   (--mobile 1: touch/meta-viewport emulation, which widens an overflowing page instead of scrolling it)
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const arg = (k, d) => { const i = process.argv.indexOf(`--${k}`); return i < 0 ? d : process.argv[i + 1] ?? true; };
const has = (k) => process.argv.includes(`--${k}`);
const url = arg('url'); if (!url) { console.error('usage: inspect.mjs --url URL [--w N --h N --out FILE ...]'); process.exit(2); }
const W = +arg('w', 1440), H = +arg('h', 900), DSF = +arg('dsf', W < 600 ? 2 : 1), wait = +arg('wait', 1500);
const dir = mkdtempSync(join(tmpdir(), 'inspect-'));
const chrome = spawn(process.env.CHROME || 'google-chrome', ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${dir}`, '--no-first-run', '--no-default-browser-check',
  '--disable-gpu', '--hide-scrollbars', '--force-color-profile=srgb', '--no-sandbox', 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
const done = (code) => { try { chrome.kill('SIGKILL'); } catch {} try { rmSync(dir, { recursive: true, force: true }); } catch {} process.exit(code); };
setTimeout(() => { console.error('timeout'); done(3); }, 60000).unref();

const port = await new Promise((res, rej) => {
  let buf = '';
  chrome.stderr.on('data', (d) => { buf += d; const m = buf.match(/ws:\/\/127\.0\.0\.1:(\d+)\//); if (m) res(m[1]); });
  chrome.on('exit', () => rej(new Error('chrome exited')));
});
let target;
for (let i = 0; i < 20 && !target; i++) {
  const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  target = list.find((t) => t.type === 'page');
  if (!target) await new Promise((r) => setTimeout(r, 150));
}
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r));
let id = 0; const pend = new Map(), events = [], logs = [];
ws.addEventListener('message', (m) => {
  const d = JSON.parse(m.data);
  if (d.id) { pend.get(d.id)?.(d); pend.delete(d.id); } else events.push(d);
});
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, (d) => (d.error ? rej(new Error(`${method}: ${d.error.message}`)) : res(d.result))); ws.send(JSON.stringify({ id: i, method, params })); });
const ev = async (expr) => { const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true }); if (r.exceptionDetails) throw new Error(r.exceptionDetails.text + ' ' + (r.exceptionDetails.exception?.description || '')); return r.result.value; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

await Promise.all(['Page', 'Runtime', 'Log', 'Network'].map((d) => send(`${d}.enable`)));
await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: DSF, mobile: arg('mobile', '0') === '1' });   // default off: mobile emulation widens the layout viewport and hides horizontal overflow
const feats = [{ name: 'prefers-color-scheme', value: arg('scheme', 'light') }, { name: 'prefers-reduced-motion', value: 'reduce' }];
await send('Emulation.setEmulatedMedia', { features: feats });
await send('Emulation.setTimezoneOverride', { timezoneId: arg('tz', 'America/Toronto') });
await send('Emulation.setLocaleOverride', { locale: arg('locale', 'en-US') }).catch(() => {});
await send('Page.navigate', { url });
await sleep(wait);

const pre = arg('click'); if (pre) { await ev(`document.querySelector(${JSON.stringify(pre)}).click()`); await sleep(400); }
const code = arg('eval'); let evalOut; if (code) evalOut = await ev(code);

for (const e of events) {
  const p = e.params;
  if (e.method === 'Runtime.exceptionThrown') logs.push(`EXCEPTION ${p.exceptionDetails.text} ${p.exceptionDetails.exception?.description?.split('\n')[0] || ''}`);
  else if (e.method === 'Runtime.consoleAPICalled' && ['error', 'warning', 'assert'].includes(p.type)) logs.push(`console.${p.type} ${p.args.map((a) => a.value ?? a.description).join(' ')}`);
  else if (e.method === 'Log.entryAdded' && ['error', 'warning'].includes(p.entry.level)) logs.push(`log.${p.entry.source} ${p.entry.text} ${p.entry.url || ''}`);
  else if (e.method === 'Network.loadingFailed') logs.push(`REQUEST FAILED ${p.errorText}`);
  else if (e.method === 'Network.responseReceived' && p.response.status >= 400) logs.push(`HTTP ${p.response.status} ${p.response.url}`);
}
const stats = await ev(`(() => {
  const iw = innerWidth, bad = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width && (r.right > iw + 1 || r.left < -1) && !el.closest('.nav') && !el.closest('#tip')) bad.push(el.tagName.toLowerCase() + (el.className && el.className.baseVal === undefined ? '.' + String(el.className).split(' ')[0] : '') + ' ' + Math.round(r.left) + '..' + Math.round(r.right));
  }
  return { iw, scrollW: document.documentElement.scrollWidth, scrollH: document.documentElement.scrollHeight, overflow: bad.slice(0, 8), title: document.title,
    externalRefs: [...document.querySelectorAll('[src],[href]')].map((e) => e.getAttribute('src') || e.getAttribute('href')).filter((u) => /^(https?:)?\\/\\//.test(u)) };
})()`);

const hover = arg('hover');
if (hover) {
  const box = await ev(`(() => { const el = document.querySelector(${JSON.stringify(hover)}); el.scrollIntoView({block:'center'}); const r = el.getBoundingClientRect(); return {x: r.left + r.width * ${+arg('hx', 0.6)}, y: r.top + r.height * ${+arg('hy', 0.5)}}; })()`);
  await sleep(200);
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: box.x, y: box.y });
  await sleep(300);
}
const out = arg('out');
if (out) {
  const shot = { format: 'png' };
  const sel = arg('el');
  if (sel) {
    const r = await ev(`(() => { const el = document.querySelector(${JSON.stringify(sel)}); const r = el.getBoundingClientRect(); return { x: r.left + scrollX, y: r.top + scrollY, width: r.width, height: r.height }; })()`);
    const pad = 8; Object.assign(shot, { captureBeyondViewport: true, clip: { x: Math.max(0, r.x - pad), y: Math.max(0, r.y - pad), width: Math.min(W, r.width + 2 * pad), height: r.height + 2 * pad, scale: 1 } });
  } else if (has('full')) {
    Object.assign(shot, { captureBeyondViewport: true, clip: { x: 0, y: 0, width: W, height: stats.scrollH, scale: 1 } });
  }
  const img = await send('Page.captureScreenshot', shot);
  writeFileSync(out, Buffer.from(img.data, 'base64'));
}
if (has('brief')) {                                   // one line, exit code 1 on any problem (used by screenshot.sh)
  const problems = [...logs, ...stats.overflow.map((o) => `overflow: ${o}`), ...stats.externalRefs.map((u) => `external: ${u}`)];
  if (stats.scrollW > stats.iw) problems.push(`scrollWidth ${stats.scrollW} > ${stats.iw}`);
  console.log(`height ${stats.scrollH}px${problems.length ? `; ${problems.join('; ')}` : ''}`);
  done(problems.length ? 1 : 0);
}
console.log(JSON.stringify({ stats, logs, eval: evalOut, out }, null, 1));
done(logs.length ? 1 : 0);
