// Build smoke test (critique A1/A3/A20, minor 10 and 11): run tools/build.mjs into a temporary
// directory (never dist/), then check the served shape, the size budgets from the esbuild
// metafile, the CSP-safe HTML and the published-docs allowlist.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { buildDashboard, DEFAULT_OUT_DIR, PUBLISHED_DOCS, ROOT } from "../tools/build.mjs";

const KIB = 1024;
// A3: the boot bundle (main entry plus every chunk it imports statically) and each chunk file.
const CORE_BUDGET = 90 * KIB;
const CHUNK_BUDGET = 45 * KIB;
// Same pattern web_v2.HASHED_ASSET uses to grant immutable caching.
const HASHED_ASSET = /^\/assets\/[A-Za-z0-9_.-]+\.[0-9A-Za-z]{8,}\.(?:js|mjs|css|map|svg|woff2|png)$/;

let tmp;
let result;
let second;
let distStamp;

function stamp(file) {
  try {
    const stat = fs.statSync(file);
    return `${stat.mtimeMs}:${stat.size}`;
  } catch {
    return null;
  }
}

before(async () => {
  distStamp = stamp(path.join(DEFAULT_OUT_DIR, "build.json"));
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "vt-dash-build-"));
  result = await buildDashboard({ outDir: path.join(tmp, "a"), quiet: true });
  // The second build goes over a previous deployment's leftovers, which it must prune.
  fs.mkdirSync(path.join(tmp, "b", "assets"), { recursive: true });
  fs.mkdirSync(path.join(tmp, "b", "docs"), { recursive: true });
  fs.writeFileSync(path.join(tmp, "b", "assets", "main.OLDHASH1.js"), "old");
  fs.writeFileSync(path.join(tmp, "b", "docs", "design.html"), "<h1>spec</h1>");
  second = await buildDashboard({ outDir: path.join(tmp, "b"), quiet: true });
});

after(() => {
  if (tmp) fs.rmSync(tmp, { recursive: true, force: true });
});

const out = (...parts) => path.join(result.outDir, ...parts);
const outputFile = (key) => path.resolve(ROOT, key);

/** Output keys reachable from `key` through static imports (the file itself included). */
function staticClosure(outputs, key) {
  const seen = new Set();
  const stack = [key];
  while (stack.length) {
    const current = stack.pop();
    if (seen.has(current)) continue;
    seen.add(current);
    for (const edge of outputs[current].imports) {
      if (edge.kind === "import-statement" && outputs[edge.path]) stack.push(edge.path);
    }
  }
  return seen;
}

const bytesOf = (outputs, keys) => [...keys].reduce((sum, key) => sum + outputs[key].bytes, 0);

test("the build writes into the requested directory and leaves dist/ alone", () => {
  assert.equal(stamp(path.join(DEFAULT_OUT_DIR, "build.json")), distStamp);
  assert.ok(fs.existsSync(out("index.html")));
  assert.ok(fs.existsSync(out("build.json")));
  for (const key of Object.keys(result.metafile.outputs)) {
    assert.ok(outputFile(key).startsWith(result.outDir + path.sep), `${key} escaped the out dir`);
  }
});

test("a build over an older deployment prunes files the new build does not reference", () => {
  const dir = second.outDir;
  assert.ok(!fs.existsSync(path.join(dir, "assets", "main.OLDHASH1.js")));
  assert.ok(!fs.existsSync(path.join(dir, "docs", "design.html")));
  const expected = Object.keys(second.metafile.outputs).map((key) => path.basename(outputFile(key))).sort();
  assert.deepEqual(fs.readdirSync(path.join(dir, "assets")).sort(), expected);
  assert.ok(fs.readdirSync(dir).every((name) => !name.includes(".tmp-")), "no temp files left");
});

test("index.html references hashed js and css that exist, with an external sourcemap", () => {
  const html = fs.readFileSync(out("index.html"), "utf8");
  const js = html.match(/<script type="module" src="([^"]+)"><\/script>/);
  const css = html.match(/<link rel="stylesheet" href="([^"]+)">/);
  assert.ok(js, "module script tag");
  assert.ok(css, "stylesheet link");
  for (const ref of [js[1], css[1]]) {
    assert.match(ref, HASHED_ASSET);
    assert.ok(fs.statSync(out(ref)).size > 0, `${ref} exists`);
  }
  assert.match(js[1], /\.js$/);
  assert.match(css[1], /\.css$/);
  assert.equal(js[1], result.js);
  assert.equal(css[1], result.css);
  assert.doesNotMatch(html, /\{\{\w+\}\}/, "no unrendered template variables");

  const source = fs.readFileSync(out(js[1]), "utf8");
  const map = source.match(/\/\/# sourceMappingURL=(\S+)\s*$/);
  assert.ok(map, "linked sourcemap comment");
  assert.ok(fs.existsSync(path.join(path.dirname(out(js[1])), map[1])), "map file beside the bundle");
  assert.doesNotMatch(source, /sourceMappingURL=data:/, "sourcemap is external, not inline");
});

test("build.json carries a stable build id that index.html also names", () => {
  const manifest = JSON.parse(fs.readFileSync(out("build.json"), "utf8"));
  assert.match(manifest.build, /^[0-9a-f]{12}$/);
  assert.equal(manifest.build, result.build);
  assert.equal(manifest.js, result.js);
  const html = fs.readFileSync(out("index.html"), "utf8");
  assert.ok(html.includes(`<meta name="build" content="${manifest.build}">`));
  // Same sources give the same asset names and id, whatever the output directory.
  assert.equal(second.build, result.build);
  assert.equal(second.js, result.js);
  assert.equal(second.css, result.css);
});

test("bundle sizes stay inside the A3 budgets (minified)", (t) => {
  const outputs = result.metafile.outputs;
  const keys = Object.keys(outputs).filter((key) => key.endsWith(".js"));
  const mainKey = keys.find((key) => (outputs[key].entryPoint || "").endsWith("src/main.jsx"));
  assert.ok(mainKey, "main entry output");

  assert.ok(outputs[mainKey].bytes <= CORE_BUDGET, `main entry ${outputs[mainKey].bytes} B`);
  const core = staticClosure(outputs, mainKey);
  const coreBytes = bytesOf(outputs, core);
  t.diagnostic(`core (main + static imports): ${coreBytes} B in ${core.size} files`);
  assert.ok(coreBytes <= CORE_BUDGET, `core ${coreBytes} B > ${CORE_BUDGET} B`);

  for (const key of keys) {
    const bytes = outputs[key].bytes;
    assert.ok(bytes <= CHUNK_BUDGET, `${path.basename(key)} is ${bytes} B > ${CHUNK_BUDGET} B`);
    assert.equal(fs.statSync(outputFile(key)).size, bytes);
  }
  // Opening a lazily loaded dialog costs its own chunk plus whatever it imports that the core
  // has not already loaded; A3 caps that per dialog. Lazy views are reported, not capped.
  for (const key of keys) {
    const entry = outputs[key].entryPoint;
    if (!entry || key === mainKey) continue;
    const extra = [...staticClosure(outputs, key)].filter((dep) => !core.has(dep));
    const bytes = bytesOf(outputs, extra);
    t.diagnostic(`lazy ${entry}: +${bytes} B`);
    if (entry.startsWith("src/dialogs/")) {
      assert.ok(bytes <= CHUNK_BUDGET, `${entry} adds ${bytes} B > ${CHUNK_BUDGET} B`);
    }
  }
});

test("built HTML has no style attributes (CSP style-src 'self')", () => {
  const pages = [out("index.html"), ...fs.readdirSync(out("docs")).map((name) => out("docs", name))];
  for (const page of pages) {
    const html = fs.readFileSync(page, "utf8");
    assert.doesNotMatch(html, /<[^>]*\sstyle\s*=/i, `${path.basename(page)} has a style attribute`);
    assert.doesNotMatch(html, /<style[\s>]/i, `${path.basename(page)} has an inline style element`);
  }
});

test("only the allowlisted docs are published", () => {
  assert.deepEqual([...PUBLISHED_DOCS], ["caveats.md", "warnings.md"]);
  const published = fs.readdirSync(out("docs")).sort();
  assert.deepEqual(published, ["caveats.html", "index.html", "warnings.html"]);
  const index = fs.readFileSync(out("docs", "index.html"), "utf8");
  const listing = index.slice(index.indexOf("<main"), index.indexOf("</main>"));
  const links = [...listing.matchAll(/href="\/docs\/([^"]+)"/g)].map((match) => match[1]).sort();
  assert.deepEqual(links, ["caveats.html", "warnings.html"]);
  assert.deepEqual(result.docs.map((page) => page.slug), ["caveats", "warnings"]);
  // Specs and contracts stay in the repository.
  for (const name of fs.readdirSync(path.join(ROOT, "docs"))) {
    if (!name.endsWith(".md") || PUBLISHED_DOCS.includes(name)) continue;
    assert.ok(!fs.existsSync(out("docs", name.replace(/\.md$/, ".html"))), `${name} leaked`);
  }
  const caveats = fs.readFileSync(out("docs", "caveats.html"), "utf8");
  assert.match(caveats, /<h1[^>]*>/);
  assert.ok(caveats.includes(`href="${result.css}"`), "docs use the hashed stylesheet");
});
