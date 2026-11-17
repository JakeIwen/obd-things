/**
 * A small, safe Markdown subset for model replies: headings, paragraphs, ordered and unordered
 * lists, bold, italic, inline code, fenced code (plain preformatted text) and links to http and
 * https URLs only. Everything else is literal text. The result is plain data (`blocks`), which
 * components/MarkdownView.jsx turns into Preact elements: no HTML string is ever built or
 * inserted, so a hostile reply cannot inject markup. Pure and node-testable.
 *
 * Bounds: input over MAX_CHARS is cut, and a line over MAX_INLINE characters is shown as literal
 * text (inline parsing scans forward for closers, so a very long line of unmatched markers would
 * otherwise cost quadratic time), and nesting stops at MAX_DEPTH.
 */

export const MAX_CHARS = 200_000;
export const MAX_INLINE = 4_000;
export const MAX_DEPTH = 4;

const text = (value) => ({ t: "text", text: value });

/** `href` when `raw` is an absolute http or https URL without whitespace or control characters. */
export function safeHref(raw) {
  const value = String(raw || "").trim();
  // eslint-disable-next-line no-control-regex
  if (!value || /[\s\u0000-\u001f"'<>`\\]/.test(value)) return null;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? value : null;
  } catch {
    return null;
  }
}

function closerIndex(source, marker, from) {
  const at = source.indexOf(marker, from);
  return at;
}

/** Inline nodes of one line or paragraph. */
export function parseInline(source, depth = 0) {
  if (source.length > MAX_INLINE || depth > MAX_DEPTH) return [text(source)];
  const nodes = [];
  let buffer = "";
  const flush = () => {
    if (buffer) nodes.push(text(buffer));
    buffer = "";
  };
  let i = 0;
  while (i < source.length) {
    const c = source[i];
    if (c === "`") {
      const end = closerIndex(source, "`", i + 1);
      if (end > i + 1) {
        flush();
        nodes.push({ t: "code", text: source.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
    } else if (c === "*" && source[i + 1] === "*") {
      const end = closerIndex(source, "**", i + 2);
      if (end > i + 2) {
        flush();
        nodes.push({ t: "strong", children: parseInline(source.slice(i + 2, end), depth + 1) });
        i = end + 2;
        continue;
      }
    } else if ((c === "*" || c === "_") && source[i + 1] !== " " && source[i + 1] !== c) {
      // `_` only opens a word-start emphasis, so snake_case_names stay literal.
      const wordStart = c === "*" || i === 0 || /[\s([]/.test(source[i - 1]);
      const end = wordStart ? closerIndex(source, c, i + 1) : -1;
      if (end > i + 1 && source[end - 1] !== " ") {
        flush();
        nodes.push({ t: "em", children: parseInline(source.slice(i + 1, end), depth + 1) });
        i = end + 1;
        continue;
      }
    } else if (c === "[") {
      const mid = source.indexOf("](", i + 1);
      const end = mid > i ? source.indexOf(")", mid + 2) : -1;
      const href = end > mid ? safeHref(source.slice(mid + 2, end)) : null;
      if (href && !source.slice(i + 1, mid).includes("[")) {
        flush();
        nodes.push({ t: "a", href, children: parseInline(source.slice(i + 1, mid), depth + 1) });
        i = end + 1;
        continue;
      }
    }
    buffer += c;
    i += 1;
  }
  flush();
  return nodes;
}

const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^\s{0,3}[-*+]\s+(.*)$/;
const NUMBERED = /^\s{0,3}\d{1,3}[.)]\s+(.*)$/;
const FENCE = /^\s{0,3}(```|~~~)/;

/** Blocks of a reply: heading {level 1-6}, p, ul/ol {items}, pre {text}. */
export function parseMarkdown(input) {
  const lines = String(input == null ? "" : input).slice(0, MAX_CHARS).replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let paragraph = [];
  let list = null;
  const closeParagraph = () => {
    if (paragraph.length) blocks.push({ t: "p", children: parseInline(paragraph.join(" ")) });
    paragraph = [];
  };
  const closeList = () => {
    if (list) blocks.push(list);
    list = null;
  };
  for (let n = 0; n < lines.length; n += 1) {
    const line = lines[n];
    const fence = FENCE.exec(line);
    if (fence) {
      closeParagraph();
      closeList();
      const body = [];
      n += 1;
      while (n < lines.length && !lines[n].trimStart().startsWith(fence[1])) {
        body.push(lines[n]);
        n += 1;
      }
      blocks.push({ t: "pre", text: body.join("\n") });
      continue;
    }
    if (!line.trim()) {
      closeParagraph();
      closeList();
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      closeParagraph();
      closeList();
      blocks.push({ t: "heading", level: heading[1].length, children: parseInline(heading[2].trim()) });
      continue;
    }
    const bullet = BULLET.exec(line);
    const numbered = bullet ? null : NUMBERED.exec(line);
    if (bullet || numbered) {
      closeParagraph();
      const kind = bullet ? "ul" : "ol";
      if (!list || list.t !== kind) {
        closeList();
        list = { t: kind, items: [] };
      }
      list.items.push(parseInline((bullet || numbered)[1].trim()));
      continue;
    }
    closeList();
    paragraph.push(line.trim());
  }
  closeParagraph();
  closeList();
  return blocks;
}
