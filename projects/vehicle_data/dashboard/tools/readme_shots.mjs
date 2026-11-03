// Take the README screenshot set on vanpi, so no Mac session is needed: build the checkout's
// dashboard into a scratch directory beside dist/, add demo_capture.mjs's setup under __demo/,
// serve it on loopback with web_v2 (cache-only, warning chat off, reading the broker socket), and
// run readme_shots.playwright.js in the system Chromium through Playwright. The capture is refused
// unless every requested shot was taken and the listener saw only reads, with /v1/snapshot (kept
// for its metric catalog) as the one data route. Only then do the shots and capture.json replace
// tmp/readme_screens/. Look at every image, then --promote copies them into screenshots/ and
// refreshes their manifest entries; captions and capture_method stay hand-written. The Roboto
// faces come from Google Fonts, so the capture needs internet.
// usage: node tools/readme_shots.mjs [--only 01,05]
//        node tools/readme_shots.mjs --promote
import {spawn} from "node:child_process";
import fs from "node:fs";
import {createRequire} from "node:module";
import net from "node:net";
import path from "node:path";
import readline from "node:readline";
import {setTimeout as sleep} from "node:timers/promises";
import {fileURLToPath} from "node:url";
import {parseArgs as parseCommandLine} from "node:util";
import {chromium} from "playwright-core";
import {ROOT as DASHBOARD_ROOT, buildDashboard} from "./build.mjs";
import {CAPTURE_LOCALE, CAPTURE_TIME_ZONE, writeDemoCapture} from "./demo_capture.mjs";
import {imageRecord, refreshManifest} from "./screenshot_manifest.mjs";

const REPO = path.resolve(DASHBOARD_ROOT, "..", "..", "..");
const SEQUENCE = path.join(DASHBOARD_ROOT, "tools", "readme_shots.playwright.js");
const SCREENSHOTS = path.join(DASHBOARD_ROOT, "screenshots");
const MANIFEST = path.join(SCREENSHOTS, "manifest.json");
const OUT = path.join(REPO, "tmp", "readme_screens");
const REPORT = "capture.json";
const CHROMIUM = "/usr/bin/chromium";
const PLAYWRIGHT_VERSION = createRequire(import.meta.url)("playwright-core/package.json").version;
const DEMO_DIR = "__demo";
const LISTENER_START_MS = 20_000;
const REQUEST_TIMEOUT_MS = 5_000;
// demo_capture keeps only the snapshot's metric catalog; any other data route would show live data.
const LIVE_DATA_ALLOWED = new Set(["/v1/snapshot"]);
const USAGE = "usage: node tools/readme_shots.mjs [--only 01,05]\n       node tools/readme_shots.mjs --promote";

export function parseArgs(argv) {
  let values;
  try {
    ({values} = parseCommandLine({args: argv, options: {only: {type: "string"}, promote: {type: "boolean", default: false}}}));
  } catch (error) {
    throw new Error(`${error.message}\n${USAGE}`);
  }
  const only = values.only === undefined ? null : values.only.split(",");
  if (only && !only.every((number) => /^\d\d$/.test(number))) {
    throw new Error(`--only takes two-digit shot numbers, such as 01,05\n${USAGE}`);
  }
  if (values.promote && only) throw new Error(`--promote copies what the last capture took; retake with --only first\n${USAGE}`);
  return {promote: values.promote, only};
}

/**
 * What reached the listener, from web.py's access log: counts per method and per path (assets
 * aside), and the problems that refuse the capture. A log with no parsed GET is one of them, so a
 * changed log format can't pass the check unread.
 */
export function auditListenerLog(lines) {
  const methods = {};
  const paths = {};
  for (const line of lines) {
    const match = /"([A-Z]+) (\S+) HTTP\/[\d.]+" \d{3}/.exec(line);
    if (!match) continue;
    const [, method, target] = match;
    methods[method] = (methods[method] || 0) + 1;
    const pathname = target.split("?")[0];
    if (!pathname.startsWith("/assets/")) paths[pathname] = (paths[pathname] || 0) + 1;
  }
  const problems = [];
  if (!methods.GET) problems.push("no GET requests in the listener log");
  for (const method of Object.keys(methods)) {
    if (method !== "GET" && method !== "HEAD") problems.push(`${method} requests reached the listener`);
  }
  for (const pathname of Object.keys(paths)) {
    if (/^\/v\d+\//.test(pathname) && !LIVE_DATA_ALLOWED.has(pathname)) problems.push(`${pathname} reached the listener instead of the demo`);
  }
  return {methods, paths, problems};
}

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const {port} = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

/** Spawn web_v2 for staticRoot; the handle exists before anything awaits, so every path can stop it. */
function spawnListener(staticRoot, port) {
  const child = spawn("python3", ["-m", "projects.vehicle_data.web_v2", "--bind", "127.0.0.1", "--port", String(port),
    "--cache-only", "--no-warning-chat", "--static-root", staticRoot], {cwd: REPO, stdio: ["ignore", "pipe", "pipe"]});
  const exited = () => child.exitCode !== null || child.signalCode !== null;
  const listener = {url: `http://127.0.0.1:${port}/`, log: [], error: null, exited, stop: () => { if (!exited()) child.kill("SIGTERM"); }};
  child.on("error", (error) => { listener.error = error; });
  for (const stream of [child.stdout, child.stderr]) readline.createInterface({input: stream}).on("line", (line) => listener.log.push(line));
  return listener;
}

/** Wait until the listener serves this build (not some other process on the port) and the broker's catalog. */
async function waitForListener(listener, build) {
  const get = (route) => fetch(listener.url + route, {signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS)})
    .then((reply) => (reply.ok ? reply.json() : null)).catch(() => null);
  const deadline = Date.now() + LISTENER_START_MS;
  let served = null;
  while (!served) {
    if (listener.error || listener.exited()) {
      throw new Error(`web_v2 exited: ${listener.error ? listener.error.message : listener.log.slice(-5).join(" | ")}`);
    }
    if (Date.now() > deadline) throw new Error(`web_v2 did not answer within ${LISTENER_START_MS} ms`);
    served = await get("build.json");
    if (!served) await sleep(250);
  }
  if (served.build !== build) throw new Error(`${listener.url} serves build ${served.build}, not the scratch build ${build}`);
  if (!(await get("v1/snapshot"))?.catalog) throw new Error("web_v2 returned no metric catalog from the broker socket");
}

/**
 * Uncaught page errors, console errors and failed responses, counted (the blocked live stream logs
 * one console error per load). The console names no URL, so the failed responses carry it.
 */
function watchPageErrors(page) {
  const found = {pageErrors: {}, consoleErrors: {}, failedResponses: {}};
  const count = (bucket, message) => { bucket[message] = (bucket[message] || 0) + 1; };
  page.on("pageerror", (error) => count(found.pageErrors, String(error.message || error)));
  page.on("console", (message) => { if (message.type() === "error") count(found.consoleErrors, message.text()); });
  page.on("response", (response) => {
    if (response.status() >= 400) count(found.failedResponses, `${response.status()} ${response.url().split("?")[0]}`);
  });
  return found;
}

/**
 * Chromium in the committed set's language. Playwright's locale reaches the page's Intl and
 * navigator; native controls such as the date field follow the browser's own language, which
 * Chromium on Linux reads from LANGUAGE and LANG.
 */
function launchBrowser() {
  const language = CAPTURE_LOCALE.replace("-", "_");
  return chromium.launch({executablePath: CHROMIUM, headless: true, env: {...process.env, LANGUAGE: language, LANG: `${language}.UTF-8`}});
}

function localDate(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** Put staged at target. The previous target moves aside first and comes back if staged can't take its place. */
export function replaceDirectory(target, staged) {
  const previous = `${staged}.previous`;
  const hadPrevious = fs.existsSync(target);
  if (hadPrevious) fs.renameSync(target, previous);
  try {
    fs.renameSync(staged, target);
  } catch (error) {
    if (hadPrevious) fs.renameSync(previous, target);
    throw error;
  }
  fs.rmSync(previous, {recursive: true, force: true});
}

/** Run the README sequence against a scratch build; on success its shots and report replace OUT. */
async function capture(only) {
  // Synchronous disposers, drained by the finally once the browser has closed, or by the process's
  // exit event when a signal or a crash ends the run first (an unhandled rejection in a route
  // handler skips the finally); Playwright's own exit hook kills the browser. Each disposer runs
  // even when an earlier one fails, so a stuck directory can't keep web_v2 alive.
  const cleanup = [];
  const release = () => {
    while (cleanup.length) {
      try { cleanup.pop()(); } catch (error) { console.error(`cleanup failed: ${error.message}`); }
    }
  };
  // A signal's default action ends the process with no exit event, so signals exit explicitly.
  const interrupted = () => process.exit(130);
  process.on("exit", release).once("SIGINT", interrupted).once("SIGTERM", interrupted);
  let browser;
  let errors = null;
  try {
    // Bundle hashes, and so the build id, depend on the output directory's place relative to src/
    // (source-map paths): a build beside dist/ gets the id the deploy build of this source gets.
    const dist = fs.mkdtempSync(path.join(DASHBOARD_ROOT, ".readme-shots-"));
    cleanup.push(() => fs.rmSync(dist, {recursive: true, force: true}));
    const {build} = await buildDashboard({outDir: dist, quiet: true});
    const listener = spawnListener(dist, await freePort());
    cleanup.push(() => listener.stop());
    const demoUrl = `${listener.url}${DEMO_DIR}/`;
    const demo = await writeDemoCapture({outDir: path.join(dist, DEMO_DIR), bundleUrl: demoUrl + "demo-bundle.json", distDir: dist});
    await waitForListener(listener, build);
    fs.mkdirSync(path.dirname(OUT), {recursive: true});
    const staged = fs.mkdtempSync(path.join(path.dirname(OUT), ".readme_screens-"));
    cleanup.push(() => fs.rmSync(staged, {recursive: true, force: true}));
    browser = await launchBrowser();
    const page = await (await browser.newContext({deviceScaleFactor: 1, locale: CAPTURE_LOCALE, timezoneId: CAPTURE_TIME_ZONE})).newPage();
    errors = watchPageErrors(page);
    page.__shots = {only, dashboard: listener.url, install: demoUrl + "install-demo.js", out: staged + path.sep};
    // The same evaluation browser_run_code gives the file on the Mac.
    const taken = await (0, eval)(fs.readFileSync(SEQUENCE, "utf8"))(page);
    const missing = (only || []).filter((number) => !taken.some((file) => file.startsWith(number + "-")));
    if (missing.length || !taken.length) throw new Error(`the sequence has no shot ${missing.join(", ") || "at all"}`);
    const audit = auditListenerLog(listener.log);
    if (audit.problems.length) throw new Error(`capture refused: ${audit.problems.join("; ")}`);
    const report = {capturedOn: localDate(new Date()), build, demoTime: new Date(demo.nowMs).toISOString(),
      timeZone: CAPTURE_TIME_ZONE, locale: CAPTURE_LOCALE, chromium: browser.version(), playwright: PLAYWRIGHT_VERSION,
      files: taken.map((file) => imageRecord(file, fs.readFileSync(path.join(staged, file)))),
      listener: {methods: audit.methods, paths: audit.paths}, ...errors};
    fs.writeFileSync(path.join(staged, REPORT), JSON.stringify(report, null, 2) + "\n");
    // Swapped in only now, so a failed or refused run leaves the previous one whole.
    replaceDirectory(OUT, staged);
    return report;
  } catch (error) {
    // What the page saw so far usually names the request behind the failure.
    if (errors) console.error(JSON.stringify(errors, null, 1));
    throw error;
  } finally {
    // The handlers go last: Playwright's own SIGINT handler exits once the browser has closed.
    await browser?.close().catch(() => {});
    release();
    process.off("exit", release).off("SIGINT", interrupted).off("SIGTERM", interrupted);
  }
}

/** Copy the last capture's shots into screenshots/ and refresh their manifest entries. */
function promote() {
  const report = JSON.parse(fs.readFileSync(path.join(OUT, REPORT), "utf8"));
  const records = report.files.map(({file}) => imageRecord(file, fs.readFileSync(path.join(OUT, file))));
  const {manifest, wholeSet} = refreshManifest(JSON.parse(fs.readFileSync(MANIFEST, "utf8")), records,
    {capturedOn: report.capturedOn, build: report.build});
  for (const {file} of records) fs.copyFileSync(path.join(OUT, file), path.join(SCREENSHOTS, file));
  fs.writeFileSync(MANIFEST, JSON.stringify(manifest, null, 2) + "\n");
  return {promoted: records.map((record) => record.file), build: report.build, wholeSet};
}

async function main() {
  const {promote: promoting, only} = parseArgs(process.argv.slice(2));
  if (promoting) {
    const result = promote();
    console.log(JSON.stringify(result, null, 1));
    console.log(result.wholeSet
      ? "The whole set was promoted, so captured_on and build now name this capture."
      : `A partial set keeps the manifest's captured_on and build; name build ${result.build} in the commit message.`);
    console.log("Captions and capture_method are hand-written: check them against the new images.");
    return;
  }
  const report = await capture(only);
  console.log(JSON.stringify({out: OUT, build: report.build, chromium: report.chromium, shots: report.files.length,
    listener: report.listener, pageErrors: report.pageErrors, failedResponses: report.failedResponses}, null, 1));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
