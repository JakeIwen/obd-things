/* "Pair another device": a collapsed disclosure a connected dialog shows near its footnote. Opening
   it makes a one-time PIN on the Pi and shows it in large digits with a countdown; the new device
   types it in its own pairing form. Closing the disclosure (or the dialog, which unmounts this)
   cancels the PIN. All behaviour is createPairingPanel() in assistantPairing.js. */
import { useEffect } from "preact/hooks";
import { ACCESS_TEXT, createStorage } from "../assistantAccess.js";
import { createPairingPanel } from "../assistantPairing.js";
import { useController } from "../dialogs/useController.js";

const createPanel = (onChange) => createPairingPanel({
  fetch: (url, init) => globalThis.fetch(url, init),
  storage: createStorage(),
  onChange,
});

export default function PairAnotherDevice() {
  const [panel, snap] = useController(createPanel, (c) => c.snapshot());
  useEffect(() => () => panel.close(), [panel]);
  const onToggle = (ev) => { if (ev.currentTarget.open) panel.open(); else panel.close(); };
  return (
    <details class="disc pair-another" onToggle={onToggle}>
      <summary class="disc__summary">{ACCESS_TEXT.anotherTitle}</summary>
      <div class="disc__body">
        {snap.showPin ? (
          <div class="pair-another__pin" role="group" aria-label={ACCESS_TEXT.pinLabel}>
            <p class="pair-another__digits num">{snap.pin}</p>
            <p class="pair-another__countdown num" aria-live="off">{snap.countdown}</p>
            <p class="muted pair-another__hint">{ACCESS_TEXT.anotherHint}</p>
          </div>
        ) : null}
        {snap.message ? <p class={"status-line" + (snap.error ? " status-line--error" : "")} role="status">{snap.message}</p> : null}
        <div class="btn-row">
          <button type="button" class="btn btn--small" disabled={!snap.canNew} onClick={() => panel.newPin()}>{ACCESS_TEXT.anotherNew}</button>
        </div>
      </div>
    </details>
  );
}
