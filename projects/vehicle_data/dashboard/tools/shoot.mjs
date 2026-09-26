// Screenshot and measure every dashboard v2 view in headless Firefox over WebDriver BiDi.
// usage: node --experimental-websocket tools/shoot.mjs <url> <outdir> [observeSeconds]
// Output per orientation and view: <outdir>/<orientation>-<view>.png, plus report.json with
// DOM size, document height vs viewport, small text, tap targets, overflow/overlap hits,
// console errors and a MutationObserver/rAF sample on the Drive view.
import {spawn} from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const url = process.argv[2] || "http://127.0.0.1:8767/";
const out = process.argv[3] || "tmp-shots";
const observeSeconds = Number(process.argv[4] || 10);
const port = 9300 + Math.floor(Math.random() * 600);
fs.mkdirSync(out, {recursive: true});
const profileDir = path.join(out, "ff-profile");
fs.mkdirSync(profileDir, {recursive: true});
fs.writeFileSync(path.join(profileDir, "user.js"), [
  'user_pref("remote.active-protocols", 1);',
  'user_pref("browser.shell.checkDefaultBrowser", false);',
  'user_pref("datareporting.policy.dataSubmissionEnabled", false);',
  'user_pref("toolkit.telemetry.enabled", false);',
  'user_pref("layout.css.devPixelsPerPx", "1.0");',
].join("\n") + "\n");
const ff = spawn(process.env.FIREFOX || "/usr/bin/firefox", ["--headless", "--no-remote", "-profile", profileDir, `--remote-debugging-port=${port}`, "--remote-allow-hosts=127.0.0.1,localhost", "--window-size=800,1280", "about:blank"], {stdio: ["ignore", "ignore", "pipe"]});
let stderr = ""; ff.stderr.on("data", (d) => { stderr += d; });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let ws = null;
for (let i = 0; i < 240 && !ws; i++) {
  await sleep(500);
  try { const s = new WebSocket(`ws://127.0.0.1:${port}/session`); await new Promise((res, rej) => { s.onopen = res; s.onerror = rej; }); ws = s; } catch { ws = null; }
}
if (!ws) { console.error("firefox BiDi did not come up\n" + stderr.slice(-2000)); ff.kill("SIGKILL"); process.exit(2); }
let nextId = 0; const pending = new Map(); const consoleErrors = [];
ws.onmessage = (m) => { const msg = JSON.parse(m.data);
  if (msg.id != null && pending.has(msg.id)) { const {res, rej} = pending.get(msg.id); pending.delete(msg.id); msg.type === "error" ? rej(new Error(msg.error + ": " + msg.message)) : res(msg.result); }
  else if (msg.type === "event" && msg.method === "log.entryAdded" && (msg.params.level === "error" || msg.params.level === "warn")) consoleErrors.push(msg.params.level + ": " + msg.params.text);
};
const send = (method, params = {}) => new Promise((res, rej) => { const id = ++nextId; pending.set(id, {res, rej}); ws.send(JSON.stringify({id, method, params})); });
await send("session.new", {capabilities: {alwaysMatch: {}}});
await send("session.subscribe", {events: ["log.entryAdded"]});
const tree = await send("browsingContext.getTree", {});
const context = tree.contexts[0].context;
const viewport = (width, height) => send("browsingContext.setViewport", {context, viewport: {width, height}, devicePixelRatio: 1});
async function evaluate(expression) {
  const r = await send("script.evaluate", {expression: `(async () => { const __v = await (${expression}); return typeof __v === "string" ? __v : JSON.stringify(__v); })()`, target: {context}, awaitPromise: true, resultOwnership: "none"});
  if (r.type !== "success") throw new Error("evaluate failed: " + JSON.stringify(r.exceptionDetails?.text || r));
  const v = r.result.value; try { return JSON.parse(v); } catch { return v; }
}
async function shot(file) {
  const r = await send("browsingContext.captureScreenshot", {context, origin: "viewport"});
  fs.writeFileSync(file, Buffer.from(r.data, "base64"));
  return path.basename(file);
}
const clickTab = async (label) => {
  const ok = await evaluate(`(() => { const b = [...document.querySelectorAll('.tab')].find(e => e.textContent.trim().startsWith(${JSON.stringify(label)})); if (b) b.click(); return !!b; })()`);
  // Lazily loaded views show "Loading…" until their chunk arrives; wait for it (up to 10 s).
  for (let i = 0; i < 40; i++) {
    const loading = await evaluate(`(() => { const e = document.querySelector('.view > .empty'); return !!(e && /Loading/.test(e.textContent)); })()`);
    if (!loading) break;
    await sleep(250);
  }
  return ok;
};
const probe = () => evaluate(`(() => {
  const view = document.querySelector('.view');
  const els = [...document.querySelectorAll('.app *')].filter(e => !e.closest('[hidden]'));
  const small = {}; const tapSmall = []; const overflow = []; const overlaps = [];
  for (const e of els) {
    if ([...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) {
      const px = Math.round(parseFloat(getComputedStyle(e).fontSize)); const k = px < 13 ? '<13px' : px < 16 ? '13-15px' : '>=16px'; small[k] = (small[k] || 0) + 1;
      if (!e.closest('.sr-only') && e.scrollWidth > e.clientWidth + 1 && getComputedStyle(e).textOverflow !== 'ellipsis' && getComputedStyle(e).overflow !== 'visible') overflow.push((e.className || e.tagName) + ': ' + e.textContent.trim().slice(0, 40));
    }
    if (e.matches('button, select, summary, input, a, [role=button]')) { const r = e.getBoundingClientRect(); if (r.width && r.height && (r.height < 44 || r.width < 44)) tapSmall.push((e.className || e.tagName) + ' ' + e.textContent.trim().slice(0, 20) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height)); }
  }
  // Only what is actually on screen: leaves inside the scrolling view are clipped to its visible
  // box (content scrolled under the fixed bars or skipped by content-visibility is not an overlap).
  const vr = view ? view.getBoundingClientRect() : null;
  const clip = (e, r) => {
    if (!vr || !view.contains(e)) return r;
    const left = Math.max(r.left, vr.left), right = Math.min(r.right, vr.right), top = Math.max(r.top, vr.top), bottom = Math.min(r.bottom, vr.bottom);
    return {left, right, top, bottom, width: Math.max(0, right - left), height: Math.max(0, bottom - top)};
  };
  const leaves = els.filter(e => e.children.length === 0 && e.textContent.trim() && !e.closest('.sr-only')).map(e => [e, clip(e, e.getBoundingClientRect())]).filter(([, r]) => r.width > 0 && r.height > 0);
  for (let i = 0; i < leaves.length; i++) for (let j = i + 1; j < leaves.length; j++) { const [a, ra] = leaves[i], [b, rb] = leaves[j]; const x = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left), y = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top); if (x > 2 && y > 2) overlaps.push(a.textContent.trim().slice(0, 20) + ' x ' + b.textContent.trim().slice(0, 20)); }
  const clipped = [...document.querySelectorAll('.tile')].filter(t => t.scrollHeight > t.clientHeight + 1).map(t => (t.getAttribute('aria-label') || t.className) + ' ' + t.scrollHeight + '>' + t.clientHeight);
  return {domNodes: document.getElementsByTagName('*').length, viewScroll: view ? view.scrollHeight : null, viewClient: view ? view.clientHeight : null, docW: document.documentElement.scrollWidth, winW: innerWidth, small, tapSmall: tapSmall.slice(0, 20), overflow: overflow.slice(0, 20), overlaps: overlaps.slice(0, 20), clippedTiles: clipped};
})()`);

const report = {url, startedAt: new Date().toISOString(), views: {}};
await viewport(800, 1280);
await send("browsingContext.navigate", {context, url, wait: "complete"});
await sleep(6000);
report.assets = await evaluate(`performance.getEntriesByType("resource").filter(e => !e.name.includes("/v1/") && !e.name.includes("/v2/")).map(e => ({name: e.name.replace(location.origin, ""), transfer: e.transferSize, decoded: e.decodedBodySize}))`);
// Drive view mutation / frame sample while the stream runs.
await clickTab("Drive"); await sleep(1500);
report.driveIdle = await evaluate(`new Promise((resolve) => {
  const m = {records: 0, attributes: 0, characterData: 0, childList: 0, byTarget: {}};
  const mo = new MutationObserver((l) => { for (const r of l) { m.records++; m[r.type]++; const t = r.target.nodeType === 3 ? r.target.parentElement : r.target; const k = t ? (t.className && String(t.className).split(' ')[0]) || t.tagName : '?'; m.byTarget[k] = (m.byTarget[k] || 0) + 1; } });
  mo.observe(document.body, {subtree: true, childList: true, characterData: true, attributes: true});
  const f = {count: 0, over32: 0, over50: 0, maxGap: 0}; let last = performance.now(); let on = true;
  const tick = (now) => { f.count++; const g = now - last; last = now; if (g > f.maxGap) f.maxGap = Math.round(g); if (g > 32) f.over32++; if (g > 50) f.over50++; if (on) requestAnimationFrame(tick); }; requestAnimationFrame(tick);
  setTimeout(() => { on = false; mo.disconnect(); resolve({seconds: ${observeSeconds}, mutations: m, frames: f, conn: window.__van && window.__van.store.connection.peek()}); }, ${observeSeconds * 1000});
})`);
for (const [w, h, orient] of [[800, 1280, "portrait"], [1280, 800, "landscape"]]) {
  await viewport(w, h); await sleep(800);
  for (const label of ["Drive", "Parked", "Health", "History", "System"]) {
    await clickTab(label); await sleep(1500);
    const file = await shot(path.join(out, `${orient}-${label.toLowerCase()}.png`));
    report.views[`${orient}-${label}`] = Object.assign({file}, await probe());
    // Drive page 2 (design 3.1.1) through its pager, then back to page 1 so the saved choice is restored.
    if (label === "Drive") {
      const pager = (n) => evaluate(`(() => { const b = [...document.querySelectorAll('.drive-pager__btn')].find(e => e.textContent.trim().startsWith(${JSON.stringify(String(n))})); if (b) b.click(); return !!b; })()`);
      if (await pager(2)) {
        for (let i = 0; i < 40; i++) { if (await evaluate(`!!document.querySelector('.drive2 .tile')`)) break; await sleep(250); }
        await sleep(1500);
        const file2 = await shot(path.join(out, `${orient}-drive-2.png`));
        report.views[`${orient}-Drive-2`] = Object.assign({file: file2}, await probe());
        await pager(1); await sleep(800);
      }
    }
    // Portrait only: page through the scrolling card views so every card is reviewed
    // (SHOOT_PAGES=0 disables). Scrolls the .view element, then restores the top.
    const pages = Number(process.env.SHOOT_PAGES ?? 4);
    if (orient === "portrait" && label !== "Drive" && pages > 0) {
      const extra = [];
      for (let p = 1; p <= pages; p++) {
        const moved = await evaluate(`(() => { const v = document.querySelector('.view'); if (!v) return false; const before = v.scrollTop; v.scrollTop = before + v.clientHeight - 80; return v.scrollTop > before; })()`);
        if (!moved) break;
        await sleep(500);
        extra.push(await shot(path.join(out, `${orient}-${label.toLowerCase()}-p${p + 1}.png`)));
      }
      await evaluate(`(() => { const v = document.querySelector('.view'); if (v) v.scrollTop = 0; return true; })()`);
      report.views[`${orient}-${label}`].pages = extra;
    }
  }
}
// Portrait with every disclosure opened, paged, so lazily rendered bodies are reviewed too
// (SHOOT_OPEN=0 disables).
if (Number(process.env.SHOOT_OPEN ?? 1)) {
  await viewport(800, 1280); await sleep(800);
  for (const label of ["Parked", "Health", "History", "System"]) {
    await clickTab(label); await sleep(1200);
    for (let round = 0; round < 3; round++) { await evaluate(`(() => { for (const d of document.querySelectorAll('.view details:not([open])')) d.open = true; return true; })()`); await sleep(400); }
    const key = `open-${label}`;
    report.views[key] = Object.assign({file: await shot(path.join(out, `open-${label.toLowerCase()}.png`))}, await probe());
    const extra = [];
    for (let p = 1; p <= 12; p++) {
      const moved = await evaluate(`(() => { const v = document.querySelector('.view'); if (!v) return false; const before = v.scrollTop; v.scrollTop = before + v.clientHeight - 80; return v.scrollTop > before; })()`);
      if (!moved) break;
      await sleep(400);
      extra.push(await shot(path.join(out, `open-${label.toLowerCase()}-p${p + 1}.png`)));
    }
    report.views[key].pages = extra;
    await evaluate(`(() => { const v = document.querySelector('.view'); if (v) v.scrollTop = 0; for (const d of document.querySelectorAll('.view details[open]')) d.open = false; return true; })()`);
  }
}
// Portrait dialogs that only read (SHOOT_DIALOGS=0 disables): customiser, oil-change form,
// event list and the first event's details. The Codex chat is skipped because opening it asks
// the Pi-side advisor to start a chat; nothing here saves, exports or posts.
if (Number(process.env.SHOOT_DIALOGS ?? 1)) {
  await viewport(800, 1280); await sleep(800);
  const probeDialog = () => evaluate(`(() => {
    const d = document.querySelector('dialog[open]');
    if (!d) return {open: false};
    const els = [...d.querySelectorAll('*')].filter(e => !e.closest('[hidden]'));
    const small = {}; const tapSmall = []; const overflow = [];
    for (const e of els) {
      if ([...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) {
        const px = Math.round(parseFloat(getComputedStyle(e).fontSize)); const k = px < 13 ? '<13px' : px < 16 ? '13-15px' : '>=16px'; small[k] = (small[k] || 0) + 1;
        if (e.scrollWidth > e.clientWidth + 1 && getComputedStyle(e).textOverflow !== 'ellipsis' && getComputedStyle(e).overflow !== 'visible') overflow.push((e.className || e.tagName) + ': ' + e.textContent.trim().slice(0, 40));
      }
      // A checkbox or radio inside a <label> is tapped through the label, so measure that instead.
      const target = e.matches('input[type=checkbox], input[type=radio]') && e.closest('label') ? e.closest('label') : e;
      if (e.matches('button, select, summary, input, a, [role=button]')) { const r = target.getBoundingClientRect(); if (r.width && r.height && (r.height < 44 || r.width < 44)) tapSmall.push((e.className || e.tagName) + ' ' + (e.textContent.trim() || e.getAttribute('aria-label') || '').slice(0, 20) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height)); }
    }
    const r = d.getBoundingClientRect();
    return {open: true, cls: d.className, box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)], docW: document.documentElement.scrollWidth, winW: innerWidth, domNodes: d.getElementsByTagName('*').length, small, tapSmall: tapSmall.slice(0, 20), overflow: overflow.slice(0, 20), text: d.textContent.replace(/\\s+/g, ' ').trim().slice(0, 400)};
  })()`);
  const clickText = (sel, text) => evaluate(`(() => { const b = [...document.querySelectorAll(${JSON.stringify(sel)})].find(e => e.textContent.trim().startsWith(${JSON.stringify(text)}) && !e.disabled); if (b) b.click(); return !!b; })()`);
  const closeDialog = async () => { await evaluate(`(() => { const b = document.querySelector('dialog[open] .dialog__close'); if (b) b.click(); else { const d = document.querySelector('dialog[open]'); if (d) d.close(); } return true; })()`); await sleep(600); };
  const steps = [
    ["oil", "Parked", async () => clickText(".view button", "Record oil change")],
    ["customize", "System", async () => clickText(".view button", "Customise")],
    ["events", "Health", async () => clickText(".view button", "Event history")],
    ["event-detail", "Health", async () => {
      await evaluate(`(() => { for (const d of document.querySelectorAll('.view details:not([open])')) d.open = true; return true; })()`); await sleep(500);
      return clickText(".view button", "Details");
    }],
  ];
  report.dialogs = {};
  for (const [key, tab, open] of steps) {
    await clickTab(tab); await sleep(1000);
    const clicked = await open();
    // The dialog chunk loads on first use; wait for the <dialog> to open (up to 10 s).
    for (let i = 0; i < 40; i++) {
      if (await evaluate(`!!document.querySelector('dialog[open]')`)) break;
      await sleep(250);
    }
    await sleep(800);
    // Saved-evidence routes answer 202 while the broker loads; wait for the dialog to settle.
    for (let i = 0; i < 40; i++) {
      const loading = await evaluate(`(() => { const d = document.querySelector('dialog[open]'); return !!(d && /loading/i.test(d.textContent)); })()`);
      if (!loading) break;
      await sleep(500);
    }
    const entry = {clicked, file: await shot(path.join(out, `dialog-${key}.png`))};
    Object.assign(entry, await probeDialog());
    const scrolled = await evaluate(`(() => { const d = document.querySelector('dialog[open]'); if (!d) return false; const s = [d, ...d.querySelectorAll('*')].find(e => e.scrollHeight > e.clientHeight + 40 && /auto|scroll/.test(getComputedStyle(e).overflowY)); if (!s) return false; s.scrollTop = s.scrollHeight; return true; })()`);
    if (scrolled) { await sleep(500); entry.end = await shot(path.join(out, `dialog-${key}-end.png`)); }
    report.dialogs[key] = entry;
    await closeDialog();
    await evaluate(`(() => { const v = document.querySelector('.view'); if (v) v.scrollTop = 0; for (const d of document.querySelectorAll('.view details[open]')) d.open = false; return true; })()`);
  }
}
// Night dim on the Drive view (toggled back afterwards so the profile keeps its setting).
if (Number(process.env.SHOOT_DIM ?? 1)) {
  await viewport(800, 1280); await clickTab("Drive"); await sleep(800);
  const toggleDim = () => evaluate(`(() => { const b = [...document.querySelectorAll('button')].find(e => /^dim$/i.test(e.textContent.trim())); if (b) b.click(); return !!b; })()`);
  if (await toggleDim()) {
    await sleep(800);
    report.dim = {root: await evaluate(`document.documentElement.className`), file: await shot(path.join(out, "portrait-drive-dim.png"))};
    await toggleDim(); await sleep(400);
  }
}
report.consoleErrors = consoleErrors.slice(0, 30);
fs.writeFileSync(path.join(out, "report.json"), JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));
try { await send("browser.close", {}); } catch {}
ff.kill("SIGKILL"); process.exit(0);
