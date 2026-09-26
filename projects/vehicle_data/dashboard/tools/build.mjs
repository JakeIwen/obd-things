// Build the dashboard v2 bundle into dist/ (gitignored build output; `npm ci && npm run build` is
// the deploy step, and dist/build.json records the build id that the System view shows).
// usage: node tools/build.mjs [--watch] [--out-dir <dir>]
// Tests import buildDashboard({outDir}) and build into a temporary directory instead of dist/.
import {build, context} from "esbuild";
import {marked} from "marked";
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import {fileURLToPath, pathToFileURL} from "node:url";

export const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const SRC = path.join(ROOT, "src");
const DOCS = path.join(ROOT, "docs");
export const DEFAULT_OUT_DIR = path.join(ROOT, "dist");

// Only owner-facing pages are published to the LAN. Specs and contracts (design.md,
// freshness-contract.md, warnings-redesign.md) stay in the repository.
export const PUBLISHED_DOCS = Object.freeze(["caveats.md", "warnings.md"]);

function esbuildOptions(outDir, logLevel) {
  return {
    absWorkingDir: ROOT,
    entryPoints: [path.join(SRC, "main.jsx")],
    bundle: true,
    format: "esm",
    splitting: true,
    outdir: path.join(outDir, "assets"),
    entryNames: "[name].[hash]",
    chunkNames: "chunk.[hash]",
    assetNames: "[name].[hash]",
    minify: true,
    // External .map files beside each output (linked by a sourceMappingURL comment).
    sourcemap: true,
    // Android tablet (Chromium). It already runs the 8765 app's ES2020 syntax, so Chrome 80+ is a safe floor.
    target: ["chrome80", "firefox78"],
    jsx: "automatic",
    jsxImportSource: "preact",
    define: {"process.env.NODE_ENV": '"production"'},
    legalComments: "none",
    metafile: true,
    logLevel,
  };
}

// The build is the deploy step and web_v2.py serves outDir live, so nothing is deleted up front:
// hashed assets are written beside the old ones, the pages are replaced atomically after a
// successful build, and only then are files the new build does not reference pruned. A failed
// build therefore leaves the previous deployment intact.
function prepareOut(outDir) {
  fs.mkdirSync(path.join(outDir, "assets"), {recursive: true});
  fs.mkdirSync(path.join(outDir, "docs"), {recursive: true});
}

function writeAtomic(file, content) {
  const temp = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(temp, content);
  fs.renameSync(temp, file);
}

function prune(dir, keep) {
  for (const name of fs.readdirSync(dir)) {
    const file = path.join(dir, name);
    if (!keep.has(file) && fs.statSync(file).isFile()) fs.rmSync(file, {force: true});
  }
}

/** Absolute path of a metafile output key (keys are relative to absWorkingDir). */
function outputPath(file) {
  return path.resolve(ROOT, file);
}

function webPath(outDir, file) {
  return "/" + path.relative(outDir, outputPath(file)).split(path.sep).join("/");
}

function entryOutputs(metafile, outDir) {
  let js = null;
  let css = null;
  for (const [file, meta] of Object.entries(metafile.outputs)) {
    if (meta.entryPoint && meta.entryPoint.split(path.sep).join("/").endsWith("src/main.jsx")) {
      js = webPath(outDir, file);
      if (meta.cssBundle) css = webPath(outDir, meta.cssBundle);
    }
  }
  if (!js) throw new Error("main bundle not found in metafile");
  return {js, css};
}

function render(template, vars) {
  return template.replace(/\{\{(\w+)\}\}/g, (_, key) => {
    if (!(key in vars)) throw new Error(`template variable ${key} missing`);
    return vars[key];
  });
}

function writeIndex(outDir, outputs, buildId) {
  const template = fs.readFileSync(path.join(SRC, "index.html"), "utf8");
  const html = render(template, {JS: outputs.js, CSS: outputs.css || "", BUILD: buildId});
  writeAtomic(path.join(outDir, "index.html"), html);
}

function writeDocs(outDir, outputs, buildId) {
  const template = fs.readFileSync(path.join(SRC, "docs.html"), "utf8");
  const pages = [];
  for (const name of PUBLISHED_DOCS) {
    const source = path.join(DOCS, name);
    if (!fs.existsSync(source)) throw new Error(`published doc ${name} is missing from docs/`);
    const markdown = fs.readFileSync(source, "utf8");
    const title = (markdown.match(/^#\s+(.+)$/m) || [null, name])[1];
    const body = marked.parse(markdown, {gfm: true});
    const slug = name.replace(/\.md$/, "");
    writeAtomic(
      path.join(outDir, "docs", `${slug}.html`),
      render(template, {TITLE: title, BODY: body, CSS: outputs.css || "", BUILD: buildId, SLUG: slug}),
    );
    pages.push({slug, title});
  }
  const index = pages.map((p) => `<li><a href="/docs/${p.slug}.html">${p.title}</a></li>`).join("\n");
  writeAtomic(
    path.join(outDir, "docs", "index.html"),
    render(template, {TITLE: "Dashboard documentation", BODY: `<ul>${index}</ul>`, CSS: outputs.css || "", BUILD: buildId, SLUG: "index"}),
  );
  return pages;
}

function copyStatic(outDir) {
  for (const name of ["manifest.webmanifest", "icon.svg", "favicon.svg"]) {
    const from = path.join(SRC, name);
    if (fs.existsSync(from)) fs.copyFileSync(from, path.join(outDir, name));
  }
}

function finish(result, outDir, quiet) {
  const outputs = entryOutputs(result.metafile, outDir);
  const buildId = crypto
    .createHash("sha256")
    .update(JSON.stringify(Object.keys(result.metafile.outputs).map((f) => webPath(outDir, f)).sort()))
    .digest("hex")
    .slice(0, 12);
  // Docs first: if one fails to render, index.html still names the previous (unpruned) bundle.
  const pages = writeDocs(outDir, outputs, buildId);
  copyStatic(outDir);
  writeIndex(outDir, outputs, buildId);
  const sizes = Object.entries(result.metafile.outputs)
    .filter(([f]) => !f.endsWith(".map"))
    .map(([f, m]) => `${webPath(outDir, f).slice(1)} ${m.bytes} B`);
  const manifest = {build: buildId, builtAt: new Date().toISOString(), js: outputs.js, css: outputs.css, docs: pages, outputs: sizes};
  writeAtomic(path.join(outDir, "build.json"), JSON.stringify(manifest, null, 2));
  prune(path.join(outDir, "assets"), new Set(Object.keys(result.metafile.outputs).map(outputPath)));
  prune(
    path.join(outDir, "docs"),
    new Set(["index", ...pages.map((page) => page.slug)].map((slug) => path.join(outDir, "docs", `${slug}.html`))),
  );
  if (!quiet) console.log(`built ${buildId}: ${outputs.js} ${outputs.css || "(no css)"}\n  ${sizes.join("\n  ")}`);
  return {...manifest, metafile: result.metafile, outDir};
}

/**
 * Build the dashboard into `outDir` (default dist/).
 * @param {{outDir?: string, quiet?: boolean}} [options]
 * @returns {Promise<{build: string, js: string, css: string|null, docs: object[], outputs: string[], metafile: object, outDir: string}>}
 */
export async function buildDashboard({outDir = DEFAULT_OUT_DIR, quiet = false} = {}) {
  const target = path.resolve(outDir);
  prepareOut(target);
  const result = await build(esbuildOptions(target, quiet ? "warning" : "info"));
  return finish(result, target, quiet);
}

async function watchDashboard(outDir) {
  const target = path.resolve(outDir);
  prepareOut(target);
  const ctx = await context({
    ...esbuildOptions(target, "info"),
    plugins: [{name: "finish", setup(b) { b.onEnd((result) => { if (result.errors.length === 0 && result.metafile) finish(result, target, false); }); }}],
  });
  await ctx.watch();
  console.log("watching src/ and docs/ (docs re-render on the next JS rebuild)");
}

function parseOutDir(argv) {
  const index = argv.indexOf("--out-dir");
  if (index === -1) return DEFAULT_OUT_DIR;
  const value = argv[index + 1];
  if (!value) throw new Error("--out-dir needs a directory");
  return value;
}

const invokedDirectly = process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
if (invokedDirectly) {
  const argv = process.argv.slice(2);
  const outDir = parseOutDir(argv);
  if (argv.includes("--watch")) await watchDashboard(outDir);
  else await buildDashboard({outDir});
}
