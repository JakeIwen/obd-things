/**
 * Disclosure whose body is built on first open (and kept afterwards). `render` returns the body.
 * Long lists sit in these so the Health view builds nothing for a group nobody opens.
 */

import { useState } from "preact/hooks";

export default function LazyDisclosure({ summary, render, cls }) {
  const [seen, setSeen] = useState(false);
  const onToggle = (e) => {
    if (e.currentTarget.open && !seen) setSeen(true);
  };
  return (
    <details class={"disc" + (cls ? " " + cls : "")} onToggle={onToggle}>
      <summary class="disc__summary">{summary}</summary>
      <div class="disc__body">{seen ? render() : null}</div>
    </details>
  );
}
