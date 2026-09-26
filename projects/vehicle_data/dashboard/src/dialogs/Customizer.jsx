/* Customiser dialog (dashboard v2, design 3.5 "Customise this device"). Per view: show or hide
   cards and move them up or down; reset. Loaded lazily by the System view:
     const Customizer = (await import("./dialogs/Customizer.jsx")).default;
   Props: { open, view: 'drive'|'parked'|'health'|'history'|'system', cards: [{id, label}],
            settings (the settings.js object: {hidden:{[view]:[id]}, order:{[view]:[id]}}),
            fixedOrder (true for Drive, whose tiles keep fixed grid places: show/hide only),
            onChange(nextSettings), onClose() }.
   Every tap builds the next settings object through the pure helpers in Customizer.helpers.js and
   hands it to `onChange`; the parent saves it (settings.js saveSettings). The dialog keeps only a
   copy of the last settings it emitted (replaced by any new `settings` prop), so a host that passes
   a snapshot still sees every edit. Focus follows the edited card like the old editor.
   Widths, pairing and swapping of the old layout editor are replaced by the fixed grids.
   No inline styles (CSP); every label is a text node. */
import { useEffect, useRef, useState } from "preact/hooks";
import * as H from "./Customizer.helpers.js";

function VisibleRow({ row, index, act, fixedOrder }) {
  return (
    <li class="cust-row">
      <span class="cust-row__index num" aria-hidden="true">{index + 1}</span>
      <label class="check cust-row__check">
        <input
          type="checkbox"
          checked
          data-cust-focus={H.focusKey(row.id, "check")}
          onChange={() => act("hide", row.id, "check")}
        />
        <span class="cust-row__label">{row.label}</span>
      </label>
      {fixedOrder ? null : <div class="cust-row__actions">
        <button
          type="button"
          class="btn btn--small cust-move"
          disabled={!row.canUp}
          data-cust-focus={H.focusKey(row.id, "up")}
          aria-label={H.TEXT.moveUp + ": " + row.label}
          onClick={() => act("up", row.id, "up")}
        >{H.TEXT.moveUp}</button>
        <button
          type="button"
          class="btn btn--small cust-move"
          disabled={!row.canDown}
          data-cust-focus={H.focusKey(row.id, "down")}
          aria-label={H.TEXT.moveDown + ": " + row.label}
          onClick={() => act("down", row.id, "down")}
        >{H.TEXT.moveDown}</button>
      </div>}
    </li>
  );
}

function HiddenRow({ row, act }) {
  return (
    <li class="cust-row cust-row--hidden">
      <span class="cust-row__index" aria-hidden="true">·</span>
      <label class="check cust-row__check">
        <input
          type="checkbox"
          checked={false}
          data-cust-focus={H.focusKey(row.id, "check")}
          onChange={() => act("show", row.id, "check")}
        />
        <span class="cust-row__label">{row.label}</span>
      </label>
    </li>
  );
}

export default function Customizer({ open, view, cards, settings: settingsProp, fixedOrder = false, onChange, onClose }) {
  const dialogRef = useRef(null);
  const closeRef = useRef(null);
  const triggerRef = useRef(null);
  const focusRef = useRef(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  // Edits build on the latest settings this dialog emitted, so a host that passes a snapshot (the
  // dialog host's openDialog props) never loses an earlier edit; a new prop from the parent wins.
  const [settings, setSettings] = useState(settingsProp);
  useEffect(() => { setSettings(settingsProp); }, [settingsProp]);

  const model = H.editorModel(view, cards, settings, fixedOrder);

  // Open/close follows the `open` prop. showModal traps focus and handles Escape; the close button
  // takes focus like the other dialogs.
  useEffect(() => {
    const d = dialogRef.current;
    if (!d) return;
    if (open) {
      if (!d.open) {
        triggerRef.current = typeof document !== "undefined" ? document.activeElement : null;
        try { d.showModal(); } catch { d.setAttribute("open", ""); }
      }
      if (closeRef.current) closeRef.current.focus({ preventScroll: true });
    } else if (d.open) {
      d.close();
    }
  }, [open]);

  // Native close (Esc, X, backdrop, Done) restores focus and tells the parent.
  useEffect(() => {
    const d = dialogRef.current;
    if (!d) return undefined;
    const handle = () => {
      const t = triggerRef.current;
      if (t && t.isConnected && typeof t.focus === "function") { try { t.focus(); } catch { /* ignore */ } }
      if (onCloseRef.current) onCloseRef.current();
    };
    d.addEventListener("close", handle);
    return () => d.removeEventListener("close", handle);
  }, []);

  // After the parent re-renders with the new settings, put focus back on the edited card's control
  // (its row may have moved lists or reached an end, which would otherwise drop focus).
  useEffect(() => {
    const wanted = H.focusFallback(model, focusRef.current);
    focusRef.current = null;
    const d = dialogRef.current;
    if (!wanted || !d || !d.open) return;
    const nodes = d.querySelectorAll("[data-cust-focus]");
    for (let i = 0; i < nodes.length; i += 1) {
      const el = nodes[i];
      if (el.getAttribute("data-cust-focus") === wanted) {
        if (el !== document.activeElement && !el.disabled) el.focus({ preventScroll: true });
        return;
      }
    }
  }, [settings]);

  // Only a real change reaches the parent; the helpers return the same object for a no-op.
  const emit = (next, key) => {
    if (next === settings || !onChangeRef.current) return;
    focusRef.current = key || null;
    setSettings(next);
    onChangeRef.current(next);
  };
  const act = (action, id, control) => {
    const key = H.focusKey(id, control);
    if (action === "hide") emit(H.toggleHidden(view, settings, id, true), key);
    else if (action === "show") emit(H.toggleHidden(view, settings, id, false), key);
    else if (action === "up") emit(H.move(view, cards, settings, id, -1), key);
    else if (action === "down") emit(H.move(view, cards, settings, id, 1), key);
  };
  const close = () => { if (dialogRef.current && dialogRef.current.open) dialogRef.current.close(); };
  const onBackdrop = (ev) => { if (ev.target === dialogRef.current) close(); };

  return (
    <dialog ref={dialogRef} class="dialog cust-dialog" aria-labelledby="cust-title" onClick={onBackdrop}>
      <header class="dialog__head">
        <div class="cust-head__text">
          <p class="cust-eyebrow">{H.TEXT.eyebrow}</p>
          <h2 id="cust-title" class="dialog__title">{model.title}</h2>
        </div>
        <button type="button" class="dialog__close" aria-label="Close customiser" ref={closeRef} onClick={close}>×</button>
      </header>
      <div class="dialog__body cust-body">
        <p class="muted cust-intro">{model.intro}</p>
        <p class="status-line cust-note" aria-live="polite">{model.note}</p>
        {model.empty ? <p class="empty">{H.TEXT.noCards}</p> : null}
        {!model.empty ? (
          <section class="cust-section">
            <h3 class="section-title">{model.visibleTitle}</h3>
            {model.visible.length ? (
              <ol class="cust-list" aria-label={model.visibleTitle}>
                {model.visible.map((row, index) => (
                  <VisibleRow key={row.id} row={row} index={index} act={act} fixedOrder={model.fixedOrder} />
                ))}
              </ol>
            ) : <p class="empty">{H.TEXT.allHidden}</p>}
          </section>
        ) : null}
        {model.hidden.length ? (
          <section class="cust-section cust-hidden">
            <h3 class="section-title">{model.hiddenTitle}</h3>
            <ul class="cust-list" aria-label={model.hiddenTitle}>
              {model.hidden.map((row) => (
                <HiddenRow key={row.id} row={row} act={act} />
              ))}
            </ul>
          </section>
        ) : null}
        <div class="btn-row cust-actions">
          <button type="button" class="btn" onClick={() => emit(H.reset(view, settings), null)}>{H.TEXT.reset}</button>
          <button type="button" class="btn btn--primary" onClick={close}>{H.TEXT.done}</button>
        </div>
      </div>
    </dialog>
  );
}
