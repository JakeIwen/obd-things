import { test } from "node:test";
import assert from "node:assert/strict";
import { MAX_INLINE, parseInline, parseMarkdown, safeHref } from "../src/markdownLite.js";

const types = (nodes) => nodes.map((n) => n.t);

test("headings, paragraphs and both list kinds", () => {
  const blocks = parseMarkdown("## What it means\nOne line\nsecond line\n\n- a\n- b\n\n1. first\n2) second\n\n# Top");
  assert.deepEqual(types(blocks), ["heading", "p", "ul", "ol", "heading"]);
  assert.equal(blocks[0].level, 2);
  assert.equal(blocks[1].children[0].text, "One line second line", "soft line breaks join");
  assert.equal(blocks[2].items.length, 2);
  assert.equal(blocks[3].items.length, 2);
  assert.equal(blocks[4].level, 1);
});

test("inline: bold, italic, code and nesting", () => {
  const nodes = parseInline("a **bold *and italic* text** and `co*de*` and _under_ plain");
  assert.deepEqual(types(nodes), ["text", "strong", "text", "code", "text", "em", "text"]);
  assert.deepEqual(types(nodes[1].children), ["text", "em", "text"]);
  assert.equal(nodes[3].text, "co*de*", "code is literal");
  assert.deepEqual(parseInline("snake_case_name stays"), [{ t: "text", text: "snake_case_name stays" }]);
  assert.deepEqual(parseInline("2 * 3 * 4"), [{ t: "text", text: "2 * 3 * 4" }], "spaced stars are not emphasis");
});

test("links: only http and https become links", () => {
  const [link] = parseInline("[Ram](https://www.ramtrucks.com/)");
  assert.equal(link.t, "a");
  assert.equal(link.href, "https://www.ramtrucks.com/");
  for (const bad of ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html;base64,AAAA", "vbscript:x", "//evil.example", "/relative", "ftp://x.example", "https://a b.example", "https://x.example/\"onmouseover=\"x"]) {
    const nodes = parseInline(`[click](${bad})`);
    assert.deepEqual(types(nodes), ["text"], bad + " stays literal text");
    assert.equal(nodes[0].text, `[click](${bad})`);
  }
  assert.equal(safeHref(" http://example.com/a?b=1 "), "http://example.com/a?b=1");
  assert.equal(safeHref("javascript:alert(1)"), null);
});

test("hostile markup is never interpreted: HTML stays text", () => {
  const blocks = parseMarkdown("<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n**<b>x</b>**");
  const all = JSON.stringify(blocks);
  assert.equal(blocks.filter((b) => b.t === "p").length, 3);
  assert.match(all, /<script>alert\(1\)<\/script>/, "the tag is carried as text for the renderer to escape");
  assert.ok(!blocks.some((b) => "html" in b || "href" in b));
  const link = parseInline("[<script>](https://ok.example/)")[0];
  assert.equal(link.children[0].text, "<script>");
});

test("fenced code is plain preformatted text, and an unterminated fence ends at the input's end", () => {
  const blocks = parseMarkdown("```js\n**not bold** <b>\n```\nafter");
  assert.deepEqual(types(blocks), ["pre", "p"]);
  assert.equal(blocks[0].text, "**not bold** <b>");
  const open = parseMarkdown("```\nnever closed\n- not a list");
  assert.deepEqual(types(open), ["pre"]);
  assert.equal(open[0].text, "never closed\n- not a list");
});

test("deep nesting and unterminated markers cannot run away", () => {
  const nested = "**".repeat(200) + "x" + "**".repeat(200);
  assert.ok(JSON.stringify(parseInline(nested)).length < 20000);
  assert.deepEqual(parseInline("**never closed"), [{ t: "text", text: "**never closed" }]);
  assert.deepEqual(parseInline("`never closed"), [{ t: "text", text: "`never closed" }]);
  assert.deepEqual(parseInline("[x](https://a.example"), [{ t: "text", text: "[x](https://a.example" }]);
});

test("a very long line is literal text and parses fast; very long input is cut", () => {
  const long = "*a ".repeat(MAX_INLINE);
  const started = Date.now();
  const nodes = parseInline(long);
  assert.deepEqual(nodes, [{ t: "text", text: long }]);
  assert.ok(Date.now() - started < 200);
  const big = parseMarkdown("word ".repeat(100000));
  assert.ok(JSON.stringify(big).length < 260000, "input is capped");
  assert.deepEqual(parseMarkdown(null), []);
  assert.deepEqual(parseMarkdown(""), []);
});

test("a heading marker without a space and odd list markers stay text", () => {
  assert.deepEqual(types(parseMarkdown("#nospace")), ["p"]);
  assert.deepEqual(types(parseMarkdown("-nospace")), ["p"]);
  assert.deepEqual(types(parseMarkdown("1.nospace")), ["p"]);
});
