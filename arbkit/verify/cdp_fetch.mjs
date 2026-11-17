// Same-origin fetches through a headless Chrome tab, for explorer hosts that answer plain HTTP clients with a
// Cloudflare JS challenge (base.blockscout.com, arbitrum.blockscout.com, robinhoodchain.blockscout.com).
//   node cdp_fetch.mjs <origin> [chrome binary]
// Protocol: the first stdout line is {"ready", "challenge", "probe"}; afterwards every stdin line {"id", "url"} is
// answered by one stdout line {"id", "status", "body"}. The tab only ever loads <origin>/api/v2/stats and runs
// fetch() against the same origin; a challenge that does not clear by itself is reported, never clicked.
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";

const [, , origin, chromeArg] = process.argv;
const CHROME = chromeArg || process.env.ARB_CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const port = 9700 + Math.floor(Math.random() * 250);
const profile = mkdtempSync(join(tmpdir(), "arb-chrome-"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = (o) => process.stdout.write(JSON.stringify(o) + "\n");
const chrome = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });
const shutdown = () => { try { chrome.kill(); } catch {} try { rmSync(profile, { recursive: true, force: true }); } catch {} };
process.on("exit", shutdown);

async function targets() {
  for (let i = 0; i < 80; i++) {
    try { const r = await fetch(`http://127.0.0.1:${port}/json`); return await r.json(); } catch { await sleep(250); }
  }
  throw new Error("chrome did not start");
}
const page = (await targets()).find((t) => t.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => (ws.onopen = r));
let id = 0; const pending = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
const send = (method, params = {}) => new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
const evaluate = async (expression, awaitPromise = false) =>
  (await send("Runtime.evaluate", { expression, awaitPromise, returnByValue: true })).result?.result?.value;
await send("Page.enable"); await send("Runtime.enable");

async function settle() {
  await send("Page.navigate", { url: origin + "/api/v2/stats" });
  let text = "";
  for (let i = 0; i < 30; i++) {
    await sleep(1000);
    text = (await evaluate("document.body ? document.body.innerText : ''")) || "";
    if (text.trim().startsWith("{")) return { ready: true, probe: text.slice(0, 60) };
  }
  return { ready: false, challenge: /just a moment|challenge|verify you are human/i.test(text), probe: text.slice(0, 80) };
}
const first = await settle();
out(first);
if (!first.ready) { shutdown(); process.exit(0); }

const rl = createInterface({ input: process.stdin });
for await (const line of rl) {
  if (!line.trim()) continue;
  const { id: rid, url } = JSON.parse(line);
  const expr = `(async () => { try { const r = await fetch(${JSON.stringify(url)}, { headers: { accept: "application/json" } });
    return JSON.stringify({ status: r.status, body: await r.text() }); } catch (e) { return JSON.stringify({ status: 0, body: String(e) }); } })()`;
  let v = await evaluate(expr, true);
  let d = v ? JSON.parse(v) : { status: 0, body: "no result" };
  if (d.status === 403 && /just a moment|challenge/i.test(d.body)) {   // the clearance lapsed: load the origin again, once
    const again = await settle();
    if (again.ready) { v = await evaluate(expr, true); d = v ? JSON.parse(v) : d; }
  }
  out({ id: rid, status: d.status, body: d.body });
}
shutdown();
