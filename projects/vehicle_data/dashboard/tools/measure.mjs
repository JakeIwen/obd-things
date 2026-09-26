// Dependency-free Firefox headless measurement over WebDriver BiDi.
// usage: node --experimental-websocket measure-firefox-bidi.mjs <url> <outdir> [observeSeconds] [profileId]
import {spawn} from "node:child_process";
import fs from "node:fs";
import path from "node:path";
const url = process.argv[2] || "http://192.168.6.103:8765/";
const out = process.argv[3] || ".";
const observeSeconds = Number(process.argv[4] || 10);
const profileId = process.argv[5] || "";
const tag = profileId || "overview";
const port = 9300 + Math.floor(Math.random() * 600);
fs.mkdirSync(out, {recursive: true});
const profileDir = path.join(out, `ff-profile-${tag}`);
fs.mkdirSync(profileDir, {recursive: true});
fs.writeFileSync(path.join(profileDir, "user.js"), `user_pref("remote.active-protocols", 1);\nuser_pref("dom.disable_open_during_load", false);\nuser_pref("browser.shell.checkDefaultBrowser", false);\nuser_pref("datareporting.policy.dataSubmissionEnabled", false);\nuser_pref("toolkit.telemetry.enabled", false);\nuser_pref("layout.css.devPixelsPerPx", "1.0");\n`);
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
  else if (msg.type === "event" && msg.method === "log.entryAdded" && msg.params.level === "error") consoleErrors.push(msg.params.text);
};
const send = (method, params = {}) => new Promise((res, rej) => { const id = ++nextId; pending.set(id, {res, rej}); ws.send(JSON.stringify({id, method, params})); });
await send("session.new", {capabilities: {alwaysMatch: {}}});
await send("session.subscribe", {events: ["log.entryAdded"]});
const tree = await send("browsingContext.getTree", {});
const context = tree.contexts[0].context;
const viewport = (width, height) => send("browsingContext.setViewport", {context, viewport: {width, height}, devicePixelRatio: 1});
// Evaluate in page; the expression must produce a JSON string (or primitive) so BiDi serialisation stays trivial.
async function evaluate(expression) {
  const r = await send("script.evaluate", {expression: `(async () => { const __v = await (${expression}); return typeof __v === "string" ? __v : JSON.stringify(__v); })()`, target: {context}, awaitPromise: true, resultOwnership: "none"});
  if (r.type !== "success") throw new Error("evaluate failed: " + JSON.stringify(r.exceptionDetails?.text || r));
  const v = r.result.value; try { return JSON.parse(v); } catch { return v; }
}
async function shot(file, origin) {
  const r = await send("browsingContext.captureScreenshot", {context, origin});
  fs.writeFileSync(file, Buffer.from(r.data, "base64"));
  const d = Buffer.from(r.data, "base64"); return {file: path.basename(file), width: d.readUInt32BE(16), height: d.readUInt32BE(20)};
}
await viewport(800, 1280);
const t0 = Date.now();
await send("browsingContext.navigate", {context, url, wait: "complete"});
await sleep(7000);
if (profileId) { await evaluate(`(() => { const s = document.getElementById("profile"); s.value = ${JSON.stringify(profileId)}; s.dispatchEvent(new Event("change")); return s.value; })()`); await sleep(2500); }
const report = {url, profile: tag, browser: "firefox headless " + (stderr.match(/Firefox\s[\d.]+/) || [""])[0], startedAt: new Date(t0).toISOString(), observeSeconds};
report.assets = await evaluate(`performance.getEntriesByType("resource").filter(e => !e.name.includes("/v1/")).map(e => ({name: e.name.replace(location.origin, ""), transfer: e.transferSize, encoded: e.encodedBodySize, decoded: e.decodedBodySize, ms: Math.round(e.duration)}))`);
report.navigation = await evaluate(`(() => { const n = performance.getEntriesByType("navigation")[0]; return n ? {domContentLoaded: Math.round(n.domContentLoadedEventEnd), load: Math.round(n.loadEventEnd), transfer: n.transferSize} : null; })()`);
report.domNodes = await evaluate("document.getElementsByTagName('*').length");
report.visibleDomNodes = await evaluate("[...document.getElementsByTagName('*')].filter(e => !e.closest('[hidden]')).length");
report.hiddenPanels = await evaluate("[...document.querySelectorAll('main > [data-widget][hidden]')].map(e => e.dataset.widget)");
report.visiblePanels = await evaluate("[...document.querySelectorAll('main > [data-widget]:not([hidden])')].map(e => e.dataset.widget + ':' + (e.dataset.width || 'full'))");
report.textNodes = await evaluate("(() => { let n = 0; const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); while (w.nextNode()) n++; return n; })()");
report.docHeightPortrait = await evaluate("document.documentElement.scrollHeight");
// Wrap the page's own render functions to time them, then observe an idle window.
await evaluate(`(() => {
  window.__t = {}; const wrap = (name) => { const fn = window[name]; if (typeof fn !== "function") return; window.__t[name] = {calls: 0, ms: 0, max: 0}; window[name] = function (...a) { const s = performance.now(); try { return fn.apply(this, a); } finally { const d = performance.now() - s; const t = window.__t[name]; t.calls++; t.ms += d; if (d > t.max) t.max = d; } }; };
  ["render", "renderTimeSensitiveSnapshot", "ageSnapshot", "renderHistory", "renderEarlyWarnings", "renderDtcs", "renderMaintenance", "setProfile", "renderCatalog", "renderInterface", "renderAdditionalMetrics"].forEach(wrap);
  const jp = JSON.parse; window.__t.jsonParseBig = {calls: 0, ms: 0, max: 0, bytes: 0}; JSON.parse = function (s, r) { if (typeof s === "string" && s.length > 5000) { const t0 = performance.now(); try { return jp.call(JSON, s, r); } finally { const d = performance.now() - t0; const t = window.__t.jsonParseBig; t.calls++; t.ms += d; t.bytes += s.length; if (d > t.max) t.max = d; } } return jp.call(JSON, s, r); };
  window.__mut = {records: 0, added: 0, removed: 0, characterData: 0, attributes: 0, childList: 0, byTarget: {}};
  new MutationObserver((list) => { for (const r of list) { __mut.records++; __mut.added += r.addedNodes.length; __mut.removed += r.removedNodes.length; __mut[r.type]++; const t = r.target.nodeType === 3 ? r.target.parentElement : r.target; const key = t ? (t.id || (t.className && String(t.className).split(" ")[0]) || t.tagName) : "?"; __mut.byTarget[key] = (__mut.byTarget[key] || 0) + 1; } }).observe(document.documentElement, {subtree: true, childList: true, characterData: true, attributes: true});
  window.__long = []; try { new PerformanceObserver((l) => { for (const e of l.getEntries()) __long.push(Math.round(e.duration)); }).observe({type: "longtask"}); } catch (e) { window.__long = "unsupported"; }
  window.__frames = {count: 0, maxGap: 0, over32: 0, over50: 0}; let last = performance.now();
  const tick = (now) => { __frames.count++; const gap = now - last; last = now; if (gap > __frames.maxGap) __frames.maxGap = gap; if (gap > 32) __frames.over32++; if (gap > 50) __frames.over50++; if (window.__keepFrames !== false) requestAnimationFrame(tick); };
  requestAnimationFrame(tick); return true; })()`);
const w0 = Date.now();
await sleep(observeSeconds * 1000);
await evaluate("window.__keepFrames = false");
report.idle = { windowSeconds: (Date.now() - w0) / 1000, mutations: await evaluate("window.__mut"), timings: await evaluate("window.__t"), longTasksMs: await evaluate("window.__long"), frames: await evaluate("window.__frames") };
// Deterministic cost probes: call the page's functions directly (pure DOM/JS work on cached data; no network, no CAN).
report.probes = await evaluate(`(async () => { const out = {}; const time = (label, fn, n) => { const s = performance.now(); for (let i = 0; i < n; i++) fn(); out[label] = {n, avgMs: Number(((performance.now() - s) / n).toFixed(2))}; };
  const snap = await (await fetch("/v1/snapshot", {cache: "no-store"})).json(); const raw = JSON.stringify(snap); out.snapshotBytes = raw.length;
  time("JSON.parse(snapshot)", () => JSON.parse(raw), 20);
  time("render(snapshot) full pass", () => render(JSON.parse(raw)), 10);
  time("renderTimeSensitiveSnapshot()", () => renderTimeSensitiveSnapshot(), 20);
  if (typeof supplemental === "object") { time("renderHistory(cached)", () => renderHistory(supplemental.history), 5); time("renderEarlyWarnings(cached)", () => renderEarlyWarnings(supplemental.earlyWarnings), 5); time("renderDtcs(cached)", () => renderDtcs(supplemental.dtcs), 5); const h = JSON.stringify(supplemental.earlyWarnings); out.healthBytes = h.length; time("JSON.parse(health)", () => JSON.parse(h), 5); }
  const m0 = window.__mut.records; render(JSON.parse(raw)); out.mutationsPerRenderSameData = window.__mut.records - m0;
  return out; })()`);
// Scripted scroll: frame gaps while scrolling the whole document.
report.scroll = await evaluate(`new Promise((resolve) => { const start = performance.now(); let frames = 0, maxGap = 0, over32 = 0, last = start; const max = document.documentElement.scrollHeight;
  const step = (now) => { frames++; const gap = now - last; last = now; if (gap > maxGap) maxGap = gap; if (gap > 32) over32++; window.scrollBy(0, 24); if (now - start < 3000 && window.scrollY + innerHeight < max) requestAnimationFrame(step); else resolve({frames, maxGapMs: Math.round(maxGap), over32, scrolledPx: window.scrollY, ms: Math.round(now - start)}); };
  requestAnimationFrame(step); })`);
await evaluate("window.scrollTo(0, 0)"); await sleep(400);
report.shots = [await shot(path.join(out, `${tag}-portrait-viewport.png`), "viewport"), await shot(path.join(out, `${tag}-portrait-full.png`), "document")];
report.docWidthPortrait = await evaluate("document.documentElement.scrollWidth");
await viewport(1280, 800); await sleep(1500);
report.shots.push(await shot(path.join(out, `${tag}-landscape-viewport.png`), "viewport"), await shot(path.join(out, `${tag}-landscape-full.png`), "document"));
report.docWidthLandscape = await evaluate("document.documentElement.scrollWidth");
report.docHeightLandscape = await evaluate("document.documentElement.scrollHeight");
await viewport(800, 1280); await sleep(600);
const html = await evaluate("document.documentElement.outerHTML");
fs.writeFileSync(path.join(out, `${tag}-dom.html`), html); report.domBytes = Buffer.byteLength(html);
report.smallText = await evaluate(`(() => { const out = {}; for (const e of document.querySelectorAll("main *")) { if (e.closest("[hidden]")) continue; if (![...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) continue; const px = Math.round(parseFloat(getComputedStyle(e).fontSize)); const k = px < 11 ? "<11px" : px < 12 ? "11px" : px < 14 ? "12-13px" : px < 16 ? "14-15px" : ">=16px"; out[k] = (out[k] || 0) + 1; } return out; })()`);
report.tapTargetsUnder44 = await evaluate(`(() => { const small = []; for (const e of document.querySelectorAll("main button, main select, main summary, main input, main a, main [role=button]")) { if (e.closest("[hidden]")) continue; const r = e.getBoundingClientRect(); if (r.width && r.height && (r.height < 44 || r.width < 44)) small.push((e.id || e.textContent.trim().slice(0, 28)) + " " + Math.round(r.width) + "x" + Math.round(r.height)); } return small; })()`);
report.overflowingText = await evaluate(`(() => { const hits = []; for (const e of document.querySelectorAll("main p, main h1, main h2, main h3, main dd, main dt, main span")) { if (e.closest("[hidden]")) continue; if (e.scrollWidth > e.clientWidth + 1) hits.push((e.id || e.className || e.tagName) + ": " + e.textContent.trim().slice(0, 40)); } return hits.slice(0, 40); })()`);
report.overlaps = await evaluate(`(() => { const hits = []; const els = [...document.querySelectorAll("main p, main h2, main h3, main span.badge, main .metric-quality, main dd")].filter(e => !e.closest("[hidden]") && e.textContent.trim()); const rects = els.map(e => [e, e.getBoundingClientRect()]);
  for (let i = 0; i < rects.length; i++) for (let j = i + 1; j < rects.length; j++) { const [a, ra] = rects[i], [b, rb] = rects[j]; if (a.contains(b) || b.contains(a)) continue; const x = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left), y = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top); if (x > 4 && y > 4) hits.push((a.id || a.className) + " '" + a.textContent.trim().slice(0, 24) + "' x " + (b.id || b.className) + " '" + b.textContent.trim().slice(0, 24) + "'"); } return hits.slice(0, 30); })()`);
report.consoleErrors = consoleErrors.slice(0, 20);
fs.writeFileSync(path.join(out, `${tag}-report.json`), JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));
try { await send("browser.close", {}); } catch {}
ff.kill("SIGKILL"); process.exit(0);
