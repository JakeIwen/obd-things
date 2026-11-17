/**
 * Model picker: a button showing the chosen model and effort, a popover listing the available
 * models grouped by provider (the same Popover and Listbox as the Modules filter, here as a
 * single select), the unavailable providers with their reasons, and the effort as a row of
 * segmented buttons that follows the chosen model (hidden when the model has no efforts).
 *
 * @param {{id: string, catalog: object|null, choice: object|null, onChange: (choice: object) => void,
 *   disabled?: boolean}} props `choice` is `{provider, model, effort}`; the caller validates it
 *   with `resolveChoice` (modelPicker.helpers.js)
 */

import { useRef, useState } from "preact/hooks";
import Popover from "./Popover.jsx";
import Listbox from "./Listbox.jsx";
import {
  choiceLabel, chooseEffort, chooseOption, effortsOf, optionKey, pickerOptions, unavailableNotes,
} from "./modelPicker.helpers.js";

export default function ModelPicker({ id, catalog, choice, onChange, disabled = false }) {
  const [open, setOpen] = useState(false);
  const host = useRef(null);
  const options = pickerOptions(catalog);
  const notes = unavailableNotes(catalog);
  const efforts = effortsOf(catalog, choice);
  return (
    <div class="model-picker" ref={host}>
      <div class="model-picker__bar">
        <button type="button" class="btn btn--small model-picker__trigger" aria-haspopup="listbox" disabled={disabled || options.length === 0}
          aria-expanded={open ? "true" : "false"} aria-controls={id + "-popover"} onClick={() => setOpen(!open)}>
          {options.length === 0 ? "No model available" : choiceLabel(catalog, choice)}
        </button>
        {efforts.length > 0 ? (
          <div class="segmented" role="radiogroup" aria-label="Effort">
            {efforts.map((effort) => (
              <button key={effort} type="button" role="radio" aria-checked={choice.effort === effort ? "true" : "false"} disabled={disabled}
                class={"segmented__option" + (choice.effort === effort ? " segmented__option--on" : "")}
                onClick={() => onChange(chooseEffort(catalog, choice, effort))}>
                {effort}
              </button>
            ))}
          </div>
        ) : null}
      </div>
      <Popover open={open} onClose={() => setOpen(false)} hostRef={host} label="Model" class="model-picker__popover">
        <div id={id + "-popover"}>
          <Listbox id={id + "-list"} label="Model" options={options} selected={choice ? [optionKey(choice.provider, choice.model)] : []}
            onChoose={(key) => { onChange(chooseOption(catalog, choice, key)); setOpen(false); }} />
        </div>
      </Popover>
      {notes.map((note) => (
        <p key={note.label} class="model-picker__note"><b>{note.label}</b>{" · " + note.reason}</p>
      ))}
    </div>
  );
}
