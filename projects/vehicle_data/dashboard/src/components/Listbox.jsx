/**
 * Keyboard- and touch-usable list of options with an active row: arrow keys, Home and End move,
 * Space or Enter chooses, a tap chooses. `multiple` draws check boxes; a single-select leaves the
 * choice to the caller (`selected` holds one key). Options may carry a `group`, which gets a heading.
 *
 * @param {{id: string, label: string, options: Array<{key: string, label: string, count?: number,
 *   detail?: string, group?: string}>, selected: string[], multiple?: boolean,
 *   onChoose: (key: string) => void}} props
 */

import { useEffect, useRef, useState } from "preact/hooks";
import { initialActive, listboxKeyAction, moveActive, startsGroup } from "./multiSelect.helpers.js";

export default function Listbox({ id, label, options, selected, multiple = false, onChoose }) {
  const [active, setActive] = useState(() => initialActive(selected, options));
  const keyboard = useRef(false);
  const list = useRef(null);
  const at = options.length === 0 ? -1 : Math.min(active, options.length - 1);
  useEffect(() => {
    // Only a key press scrolls: a tap already has its row in view.
    if (!keyboard.current || at < 0 || !list.current) return;
    keyboard.current = false;
    const row = list.current.querySelector('[id="' + id + "-" + at + '"]');
    if (row && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
  }, [at]);
  const onKeyDown = (event) => {
    const action = listboxKeyAction(event.key);
    if (!action) return;
    event.preventDefault();
    if (action === "toggle") {
      if (at >= 0) onChoose(options[at].key);
      return;
    }
    keyboard.current = true;
    setActive(moveActive(at, action, options.length));
  };
  const chosen = new Set(selected);
  const rows = [];
  options.forEach((option, i) => {
    if (startsGroup(options, i)) rows.push(<li key={"g:" + option.group} role="presentation" class="listbox__group">{option.group}</li>);
    const on = chosen.has(option.key);
    rows.push(
      <li key={option.key} id={id + "-" + i} role="option" aria-selected={on ? "true" : "false"}
        class={"listbox__option" + (i === at ? " listbox__option--active" : "") + (on ? " listbox__option--on" : "")}
        onClick={() => { setActive(i); onChoose(option.key); }}>
        <span class={"listbox__mark" + (multiple ? " listbox__mark--box" : "")} aria-hidden="true">{on ? "✓" : ""}</span>
        <span class="listbox__label">{option.label}</span>
        {option.detail ? <span class="listbox__detail">{option.detail}</span> : null}
        {typeof option.count === "number" ? <span class="listbox__count num">{option.count}</span> : null}
      </li>,
    );
  });
  return (
    <ul ref={list} id={id} class="listbox" role="listbox" aria-label={label} aria-multiselectable={multiple ? "true" : undefined}
      tabIndex={0} data-autofocus aria-activedescendant={at >= 0 ? id + "-" + at : undefined} onKeyDown={onKeyDown}>
      {rows}
    </ul>
  );
}
