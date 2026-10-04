# Headless-browser verification harness

Two dependency-free Node (>= 22) scripts that drive headless Chrome over the DevTools protocol.
They were the OhmzMaintainer verification harness (`web/tools/` in the old engine repo) and are kept
here so the dashboard can still be rendered and checked for console errors without a real browser.

- **`browser.mjs`** — a small CDP driver: `launch()` a throw-away Chrome, `visit()` a URL and wait
  for a selector, `eval()` in the page, `shot()` a PNG, and `collect()` the console errors,
  exceptions, CSP violations and failed requests seen since the last visit.
- **`inspect.mjs`** — capture a whole page (beyond the viewport) and report the same problems, plus
  horizontal overflow; used for review screenshots.

## Usage

The dashboard needs a session, so plant a PocketBase auth token in `localStorage` before the real
visit. Pass the credentials in the environment — never commit them. Run against the hub on
`127.0.0.1:8088`:

```js
import { launch } from "./tools/browser.mjs"

const HUB = "http://127.0.0.1:8088"
const auth = await (await fetch(`${HUB}/api/collections/users/auth-with-password`, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ identity: process.env.HM_USER, password: process.env.HM_PASS }),
})).json()

const b = await launch({ w: 1440, h: 900, scheme: "light" })
await b.visit(`${HUB}/`, "document.body")
await b.eval(`localStorage.setItem("pocketbase_auth", ${JSON.stringify(JSON.stringify({ token: auth.token, record: auth.record }))})`)
await b.visit(`${HUB}/checks`, "document.body && document.body.innerText.includes('Checks')")
await b.sleep(1200)
await b.shot("/tmp/checks.png", true)   // true = full page
console.log(b.collect())                 // [] means the page was clean
await b.close()
```

`google-chrome` (or `CHROME`) must be on `PATH`. The scripts start their own Chrome on a random port
with a temporary profile and clean up on `close()`.
