/**
 * Card, disclosure, key/value and status-row primitives for the scrolling
 * views. Pure presentational components; no store access.
 */

/**
 * @param {{title: any, badge?: any, badgeTone?: 'amber'|'red'|'green'|'', tone?: 'amber'|'red'|'',
 *          wide?: boolean, actions?: any, foot?: any, children?: any, id?: string}} props
 */
export function Card({ title, badge, badgeTone = "", tone = "", wide = false, actions, foot, children, id }) {
  let cls = "card";
  if (wide) cls += " card--wide";
  if (tone) cls += " card--" + tone;
  return (
    <section class={cls} id={id} aria-label={typeof title === "string" ? title : undefined}>
      <header class="card__head">
        <h2 class="card__title">{title}</h2>
        {badge !== undefined && badge !== null && badge !== "" ? (
          <span class={"card__badge" + (badgeTone ? " card__badge--" + badgeTone : "")}>{badge}</span>
        ) : null}
      </header>
      {children}
      {actions ? <div class="card__actions">{actions}</div> : null}
      {foot ? <div class="card__foot">{foot}</div> : null}
    </section>
  );
}

/** Native <details> disclosure with a 48 px summary row. */
export function Disclosure({ summary, open = false, children, onToggle }) {
  return (
    <details class="disc" open={open} onToggle={onToggle}>
      <summary class="disc__summary">{summary}</summary>
      <div class="disc__body">{children}</div>
    </details>
  );
}

/** Key/value list: rows = [[label, value], ...]; null values are skipped. */
export function KV({ rows }) {
  const items = rows.filter((row) => row && row[1] !== null && row[1] !== undefined && row[1] !== "");
  if (!items.length) return null;
  return (
    <dl class="kv">
      {items.map(([k, v]) => [<dt key={"k" + k}>{k}</dt>, <dd key={"v" + k}>{v}</dd>])}
    </dl>
  );
}

/** Status row with a coloured dot. tone: green | amber | red | '' */
export function Row({ tone = "", main, side, onClick }) {
  return (
    <div class="row" onClick={onClick}>
      <span class={"row__dot" + (tone ? " row__dot--" + tone : "")} aria-hidden="true" />
      <span class="row__main">{main}</span>
      {side !== undefined && side !== null ? <span class="row__side">{side}</span> : null}
    </div>
  );
}

/** Section heading inside a view. */
export function SectionTitle({ children }) {
  return <h3 class="section-title">{children}</h3>;
}

/** Empty-state line. */
export function Empty({ children }) {
  return <p class="empty">{children}</p>;
}
