// Write a Playwright setup script that shows the dashboard with illustrative demo data, for README
// screenshots taken in a Playwright browser: the browser_clean MCP's browser_run_code (`filename`)
// on the Mac, or tools/readme_shots.mjs, which calls writeDemoCapture on the Pi. It blocks every
// request but GET/HEAD, answers each data route from tools/demo_*.mjs, and defines
// page.__demo.apply(variant) to replace the page's live state after load.
// usage: node tools/demo_capture.mjs <outdir> <bundle-url> [nowMs]
// Serve <outdir> on loopback so <bundle-url> reaches <outdir>/demo-bundle.json, run
// <outdir>/install-demo.js once per page, navigate, then `await page.__demo.apply("<variant>")`
// before each shot (variants: drive, running, parked, parkedScan, stationaryRadarService).
import fs from "node:fs";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {buildDemoViews} from "./demo_views.mjs";
import {seedDemoSparklines} from "./demo_sparklines.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");

/**
 * The browser clock is pinned here so every image tells the same afternoon. A mid-week daytime keeps
 * the last 24 hours clear of a DST change and of the night auto-dim.
 */
export const DEFAULT_CAPTURE_TIME = Date.parse("2026-10-28T15:30:00-05:00");
/**
 * The page formats times in the browser's zone. The committed set reads 3:30 pm, as a Central-time
 * browser shows DEFAULT_CAPTURE_TIME; browser_clean takes the Mac's zone, readme_shots.mjs sets this.
 */
export const CAPTURE_TIME_ZONE = "America/Chicago";
/** US English, as the committed set shows it: the oil-change form's native date field reads 10/28/2026. */
export const CAPTURE_LOCALE = "en-US";

/** Saved-event and assistant route tables: [{method, pathname, prefix, status, json, chatScene?}]. */
async function extraRoutes(nowMs) {
  const {demoEventRoutes} = await import("./demo_events.mjs");
  // 12 explains event 614 and 13 drafts a new warning; the scene decides which chat opens.
  const {DEMO_ASSISTANT_SCENES, demoAssistantRoutes, demoAssistantStorage} = await import("./demo_assistant.mjs");
  const chats = DEMO_ASSISTANT_SCENES.flatMap((chatScene) => demoAssistantRoutes(nowMs, chatScene).map((r) => ({...r, chatScene})));
  return {routes: [...demoEventRoutes(nowMs), ...chats], storage: demoAssistantStorage()};
}

/**
 * Runs in the page (serialized): stop the live link, then publish the variant through the store's
 * own snapshot and summary paths, mark the connection live and seed the Drive sparklines.
 */
async function applyDemoInPage(seedDemoSparklines, demo, name) {
  const van = window.__van;
  if (!van || !van.store || !van.link) throw new Error("window.__van is unavailable");
  const variant = demo.variants[name];
  const scene = demo.scene;
  van.link.stop();
  // Let any queued stream callback retire before the demo state becomes authoritative.
  await new Promise((resolve) => setTimeout(resolve, 250));
  const store = van.store;
  const now = performance.now();
  const nowWall = Date.now();
  store.applyBaseline({catalog: store.catalog.peek(), metrics: variant.metrics, status: variant.summary.status_full,
    web: variant.web}, 0, now);
  store.applySummary(variant.summary, now);
  store.connection.value = {state: "live", reason: null, detail: null, sinceMono: now};
  // The sparklines are the highway scene's; a parked or stationary page has no recent trace to show.
  if (variant.metrics["vehicle.speed"].available && variant.metrics["vehicle.speed"].value > 0) {
    seedDemoSparklines(van, scene.sparklines, now, nowWall);
  }
  store.clock.value = Math.round(now / 1000) * 1000;
  window.dispatchEvent(new Event("resize"));
  return {variant: name, metrics: Object.keys(variant.metrics).length, vehicle: store.vehicle.peek().state};
}

/**
 * Runs in the Playwright server for every request (serialized beside installDemo): the variant's
 * and shared demo routes first, then the block on anything that could change state, the
 * snapshot, and the same-origin font proxy; everything else reaches the listener.
 */
async function answerDemoRequest(route, DEMO, state) {
  const json = (body, status = 200) => ({status, contentType: "application/json", body: JSON.stringify(body)});
  // The tablet renders Roboto; the compass gets Roboto Mono, the first face in its stack Chrome
  // supports. The page's CSP allows only same-origin styles and fonts, so the capture browser
  // serves Google's copies under the dashboard's own origin.
  const FONT_CSS = "https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;600;700;800&family=Roboto+Mono:wght@700&display=block";
  const FONTS = "/__demo-fonts/";
  const method = route.request().method();
  // The MCP sandbox has no URL global; strip the origin and query by hand.
  const target = route.request().url().replace(/^[a-z]+:\/\/[^/]+/, "");
  const path = target.split("?")[0];
  const answer = [...DEMO.variants[state.variant].routes, ...DEMO.routes].find((r) => r.method === method
    && (r.prefix ? path.startsWith(r.pathname) : path === r.pathname) && (!r.chatScene || r.chatScene === state.chatScene));
  if (answer) return route.fulfill(json(answer.json, answer.status));
  // Nothing that could change state ever leaves the browser.
  if (method !== "GET" && method !== "HEAD") return route.abort("blockedbyclient");
  if (path === "/v2/stream") return route.abort("blockedbyclient");
  if (path === "/v1/snapshot") {
    // The catalog is code-defined metric metadata; every value and status in the reply is demo data.
    // The page loads parked: the trip integrator must see the engine start after the summary's trip
    // start has arrived, or it begins a new trip at zero miles. __demo.apply then sets the variant.
    const live = await (await route.fetch()).json();
    const v = DEMO.variants.parked;
    return route.fulfill(json({catalog: live.catalog, metrics: v.metrics, status: v.summary.status_full, web: v.web,
      web_delivery: live.web_delivery}));
  }
  if (path === FONTS + "roboto.css") {
    const css = await (await route.fetch({url: FONT_CSS})).text();
    return route.fulfill({contentType: "text/css", body: css.replaceAll("https://fonts.gstatic.com/", FONTS)});
  }
  if (path.startsWith(FONTS)) {
    // With the query: Google sometimes serves faces as /l/font?kit=…, and without it answers 400.
    return route.fulfill({response: await route.fetch({url: "https://fonts.gstatic.com/" + target.slice(FONTS.length)})});
  }
  return route.fallback();
}

/** Runs in the Playwright server (serialized, with the demo bundle passed as DEMO). */
async function installDemo(page, DEMO) {
  const state = {variant: "running", chatScene: "explain"};
  // Each shot settles this long after loading; the fixed waits assume a page that keeps up.
  const SETTLE_MS = 2500;
  page.__demo = {
    use: (name) => { if (!DEMO.variants[name]) throw new Error("unknown variant " + name); state.variant = name; },
    chat: (scene) => { state.chatScene = scene; },
  };
  // Every relative time (trip length, "since", "ago") is measured from the bundle's capture time.
  await page.clock.setFixedTime(DEMO.nowMs);
  await page.unrouteAll({behavior: "ignoreErrors"});
  // One handler, so precedence is answerDemoRequest's order rather than Playwright's registration order.
  // Each reload cancels the page's in-flight requests (it polls the snapshot many times a second),
  // and a handler still answering one fails with one of these errors. Playwright waits on a route
  // until it is answered, so it is aborted then: a no-op for a cancelled request, and if one of these
  // errors ever hits a live request, the page sees it fail (a console error the report counts).
  const cancelled = /Route is already handled|Response has been disposed|Request context disposed|Target page, context or browser has been closed/;
  const ignoreCancelled = (error) => { if (!cancelled.test(String(error && error.message))) throw error; };
  await page.route("**/*", (route) => answerDemoRequest(route, DEMO, state)
    .catch((error) => { ignoreCancelled(error); return route.abort().catch(ignoreCancelled); }));
  await page.addInitScript(({storage, fontsCss}) => {
    try { for (const [key, value] of Object.entries(storage)) localStorage.setItem(key, value); } catch (_error) {}
    document.addEventListener("DOMContentLoaded", () => {
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = fontsCss;
      document.head.appendChild(link);
    });
  }, {storage: DEMO.storage, fontsCss: "/__demo-fonts/roboto.css"});
  page.__demo.apply = async (name) => {
    page.__demo.use(name);
    return page.evaluate(([source, demo, n]) => (0, eval)("(" + source + ")")(demo, n),
      [DEMO.applySource, {variants: {[name]: DEMO.variants[name]}, scene: DEMO.scene}, name]);
  };
  /**
   * Load the dashboard fresh for one shot: display settings (tab, theme, dim, Drive page) and, for
   * a running variant, the trip record the distance integrator resumes from, then the variant.
   */
  page.__demo.open = async (name, settings) => {
    page.__demo.use(name);
    const trip = DEMO.variants[name].summary.history.current_trip;
    await page.evaluate(([s, t, miles]) => {
      localStorage.setItem("van-telemetry.v2.settings", JSON.stringify({version: 1, auto: true, hidden: {}, order: {},
        timeFormat: "12h", dim: false, themeMode: "dark", dimOverrideUntil: null, drivePage: 1, view: "drive", ...s}));
      if (t) localStorage.setItem("van-telemetry.v2.trip", JSON.stringify({start: t.started_at, miles}));
      else localStorage.removeItem("van-telemetry.v2.trip");
    }, [settings || {}, trip, DEMO.scene.trip.distanceMiles]);
    await page.reload({waitUntil: "load"});
    // Without the proxied faces the page falls back to the host's fonts, which would pass unnoticed.
    // fonts.load waits for Roboto itself; fonts.ready alone can resolve before layout asks for it.
    const roboto = await page.evaluate(() => document.fonts.load("16px Roboto").then((faces) => faces.length > 0));
    if (!roboto) throw new Error("Roboto did not load; the font proxy needs fonts.googleapis.com");
    await page.evaluate(() => document.fonts.ready.then(() => true));
    // apply rebuilds the store around the catalog that the page's first snapshot brings. A page that
    // hasn't taken one in within a settle period is starved (a busy host), and the fixed waits would
    // shoot it half-loaded, so the capture stops instead.
    await page.waitForFunction(() => Boolean(window.__van && window.__van.store.catalog.peek().length), null,
      {polling: 100, timeout: SETTLE_MS}).catch((error) => {
      if (error.name !== "TimeoutError") throw error;
      throw new Error(`the page had no snapshot ${SETTLE_MS} ms after loading; the host is too busy for the capture's fixed waits`);
    });
    await page.waitForTimeout(SETTLE_MS);
    const result = await page.__demo.apply(name);
    await page.waitForTimeout(800);
    // Auto view follows the vehicle state, so any other page is reached the way a person would: its tab.
    const view = (settings && settings.view) || "drive";
    await page.locator(".tab", {hasText: new RegExp("^" + view, "i")}).first().click();
    await page.waitForTimeout(1500);
    return result;
  };
  return {variants: Object.keys(DEMO.variants), routes: DEMO.routes.length};
}

/**
 * Write <outDir>/demo-bundle.json and <outDir>/install-demo.js for the build in distDir.
 * @param {{outDir: string, bundleUrl: string, nowMs?: number, distDir?: string}} options
 */
export async function writeDemoCapture({outDir, bundleUrl, nowMs = DEFAULT_CAPTURE_TIME, distDir = path.join(root, "dist")}) {
  const build = JSON.parse(fs.readFileSync(path.join(distDir, "build.json"), "utf8")).build;
  const summaryFixture = JSON.parse(fs.readFileSync(path.join(root, "test", "fixtures", "summary-v2.json"), "utf8"));
  const views = buildDemoViews({nowMs, build, summaryFixture});
  const {routes, storage} = await extraRoutes(nowMs);
  // The page receives the applier with the shared seeding function already bound to it.
  const demo = {...views, routes, storage, applySource: `(${applyDemoInPage}).bind(null, ${seedDemoSparklines})`};
  fs.mkdirSync(outDir, {recursive: true});
  fs.writeFileSync(path.join(outDir, "demo-bundle.json"), JSON.stringify(demo, null, 1));
  // The bundle is fetched by the Playwright server, so the script stays small enough to echo.
  const script = `async (page) => {\n${answerDemoRequest}\n${installDemo}\n` +
    `return installDemo(page, await (await page.request.get(${JSON.stringify(bundleUrl)})).json());\n}\n`;
  fs.writeFileSync(path.join(outDir, "install-demo.js"), script);
  return {outDir, build, nowMs, variants: Object.keys(views.variants), routes: routes.length};
}

async function main() {
  const [outDir, bundleUrl, nowArg] = process.argv.slice(2);
  if (!outDir || !bundleUrl) throw new Error("usage: node tools/demo_capture.mjs <outdir> <bundle-url> [nowMs]");
  console.log(JSON.stringify(await writeDemoCapture({outDir, bundleUrl, nowMs: nowArg ? Number(nowArg) : DEFAULT_CAPTURE_TIME})));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
