/* Radar reference dialog: zero the System radar card at the current angles, go back to an earlier
   reference, or show raw angles only. Opened from the System view's Radar alignment card.
   Loaded lazily:
     const RadarReference = (await import("./dialogs/RadarReference.jsx")).default;
   Props: { open, onClose() }.
   Nothing here reaches the radar: the points are display bookkeeping kept on the Pi. Requests and
   every string come from RadarReference.helpers.js; the state is shared with the card through
   `store.radarReference`. No inline styles (CSP). */
import { useEffect, useRef } from "preact/hooks";
import * as store from "../store.js";
import * as H from "./RadarReference.helpers.js";
import { useModalDialog } from "./modalDialog.js";
import { useController } from "./useController.js";

const createController = (onChange) =>
  H.createRadarReferenceController({
    fetch: (url, init) => globalThis.fetch(url, init),
    onState: (state) => {
      store.radarReference.value = state;
    },
    onChange,
  });

export default function RadarReference({ open, onClose }) {
  const closeRef = useRef(null);
  const [ctrl, ui] = useController(createController, (c) => c.state());

  useEffect(() => {
    ctrl.refresh();
    return () => ctrl.dispose();
  }, []);

  const { dialogRef, close } = useModalDialog({ open, onClose, focusRef: closeRef });
  const onBackdrop = (ev) => { if (ev.target === dialogRef.current) close(); };
  const vm = H.viewModel(ui, store.radarReference.value, new Date());

  return (
    <dialog ref={dialogRef} class="dialog radar-ref" aria-labelledby="radar-ref-title" onClick={onBackdrop}>
      <header class="dialog__head">
        <h2 id="radar-ref-title" class="dialog__title">{H.TEXT.title}</h2>
        <button type="button" class="dialog__close" aria-label="Close radar reference" ref={closeRef} onClick={close}>×</button>
      </header>
      <div class="dialog__body">
        <p class="radar-cal__text">{H.TEXT.intro}</p>
        <dl class="kv radar-ref__facts num">
          <dt>{H.TEXT.now}</dt>
          <dd>{vm.currentText}</dd>
          <dt>{H.TEXT.since}</dt>
          <dd>{vm.selectedText}</dd>
        </dl>
        <div class="btn-row">
          <button type="button" class="btn btn--primary" disabled={!vm.canZero} onClick={() => ctrl.zero()}>
            {vm.zeroLabel}
          </button>
          <button type="button" class="btn" disabled={!vm.canClear} onClick={() => ctrl.clear()}>
            {H.TEXT.clear}
          </button>
        </div>
        {vm.statusText ? (
          <p class={"status-line" + (vm.statusIsError ? " status-line--error" : "")} role="status">{vm.statusText}</p>
        ) : null}
        <section class="dialog__section" aria-labelledby="radar-ref-list-title">
          <h3 id="radar-ref-list-title">{H.TEXT.listTitle}</h3>
          {vm.rows.length ? (
            <ul class="list">
              {vm.rows.map((row) => (
                <li key={row.id} class="list__item radar-ref__row">
                  <div class="list__main">
                    <div class="radar-ref__name">{row.title}</div>
                    <div class="list__meta num">{row.meta}</div>
                  </div>
                  {row.selected ? (
                    <span class="list__side">{H.TEXT.inUse}</span>
                  ) : (
                    <button type="button" class="btn btn--small" disabled={!row.canUse} onClick={() => ctrl.select(row.id)}>
                      {H.TEXT.use}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p class="empty">{H.TEXT.empty}</p>
          )}
        </section>
      </div>
    </dialog>
  );
}
