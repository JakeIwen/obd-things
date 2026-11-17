/**
 * Multi-select filter: a button that reads `All modules` or `3 modules`, a popover list of the
 * options (check mark, name, count) with `All` and `Clear`, and the chosen options as removable
 * chips beside the button. No chosen option means no filter. The selection is the caller's state
 * (`selected` and `onChange`), so it can live wherever the view wants it.
 *
 * @param {{id: string, label: string, options: Array<{key: string, label: string, count?: number,
 *   detail?: string}>, selected: string[], onChange: (keys: string[]) => void,
 *   noun?: string}} props `label` names the list for screen readers; `noun` is the singular in
 *   the button text
 */

import { useRef, useState } from "preact/hooks";
import Popover from "./Popover.jsx";
import Listbox from "./Listbox.jsx";
import { allKeys, chipsOf, clearKeys, toggleKey, triggerText, validKeys } from "./multiSelect.helpers.js";

export default function MultiSelect({ id, label, options, selected, onChange, noun = "module" }) {
  const [open, setOpen] = useState(false);
  const host = useRef(null);
  const keys = validKeys(selected, options);
  const chips = chipsOf(keys, options);
  return (
    <div class="multiselect" ref={host}>
      <div class="multiselect__bar">
        <button type="button" class="btn btn--small multiselect__trigger" aria-haspopup="listbox" aria-expanded={open ? "true" : "false"}
          aria-controls={id + "-popover"} aria-label={label + ": " + triggerText(keys, options, noun)} onClick={() => setOpen(!open)}>
          {triggerText(keys, options, noun)}
        </button>
        {chips.map((chip) => (
          <button key={chip.key} type="button" class="chip" title={chip.label} aria-label={"Remove " + chip.label}
            onClick={() => onChange(toggleKey(keys, chip.key, options))}>
            <span class="chip__text">{chip.text}</span>
            <span class="chip__x" aria-hidden="true">×</span>
          </button>
        ))}
      </div>
      <Popover open={open} onClose={() => setOpen(false)} hostRef={host} label={label} class="multiselect__popover">
        <div id={id + "-popover"} class="multiselect__actions">
          <button type="button" class="btn btn--small btn--ghost" onClick={() => onChange(allKeys(options))}>All</button>
          <button type="button" class="btn btn--small btn--ghost" onClick={() => onChange(clearKeys())}>Clear</button>
          <button type="button" class="btn btn--small btn--ghost multiselect__done" onClick={() => setOpen(false)}>Done</button>
        </div>
        <Listbox id={id + "-list"} label={label} options={options} selected={keys} multiple
          onChoose={(key) => onChange(toggleKey(keys, key, options))} />
      </Popover>
    </div>
  );
}
