/**
 * Renders the safe Markdown subset (src/markdownLite.js) as Preact elements. Text goes in as text
 * nodes; the only attributes taken from the reply are the href of a link that already passed
 * `safeHref` (http or https), opened with rel="noopener noreferrer" in a new tab.
 */

import { parseMarkdown } from "../markdownLite.js";

function Inline({ nodes }) {
  return nodes.map((node, i) => {
    switch (node.t) {
      case "code": return <code key={i}>{node.text}</code>;
      case "strong": return <strong key={i}><Inline nodes={node.children} /></strong>;
      case "em": return <em key={i}><Inline nodes={node.children} /></em>;
      case "a":
        return (
          <a key={i} href={node.href} target="_blank" rel="noopener noreferrer"><Inline nodes={node.children} /></a>
        );
      default: return node.text;
    }
  });
}

const HEADING_TAGS = ["h3", "h3", "h4", "h5", "h6", "h6"];

export default function MarkdownView({ source }) {
  const blocks = parseMarkdown(source);
  return (
    <div class="md">
      {blocks.map((block, i) => {
        if (block.t === "heading") {
          const Tag = HEADING_TAGS[block.level - 1];
          return <Tag key={i} class="md__heading"><Inline nodes={block.children} /></Tag>;
        }
        if (block.t === "p") return <p key={i}><Inline nodes={block.children} /></p>;
        if (block.t === "pre") return <pre key={i} class="md__pre">{block.text}</pre>;
        const List = block.t === "ol" ? "ol" : "ul";
        return (
          <List key={i}>
            {block.items.map((item, k) => <li key={k}><Inline nodes={item} /></li>)}
          </List>
        );
      })}
    </div>
  );
}
