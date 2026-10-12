/* Radar drive calibration dialog: starts, watches and stops the ACC radar's Service Drive Alignment.
   Opened from the System view's Radar alignment card, and only where the listener reports
   `web.radar_service_enabled`. Loaded lazily:
     const RadarCalibration = (await import("./dialogs/RadarCalibration.jsx")).default;
   Props: { open, onClose() }.
   Requests, polling, gating hints and every string come from RadarCalibration.helpers.js; the Pi
   enforces the real conditions. The dialog reads the live vehicle speed only to explain why Start
   is not offered. No inline styles (CSP); progress is a native <progress> element. */
import { useEffect, useRef } from "preact/hooks";
import * as store from "../store.js";
import { fmtClock } from "../format.js";
import * as H from "./RadarCalibration.helpers.js";
import { useModalDialog } from "./modalDialog.js";
import { useController } from "./useController.js";

const createController = (onChange) =>
  H.createRadarCalibrationController({
    fetch: (url, init) => globalThis.fetch(url, init),
    isVisible: () => typeof document === "undefined" || document.visibilityState !== "hidden",
    onChange,
  });

/** Fresh vehicle speed in mph, or null when the stream has none. */
function liveSpeed() {
  const rec = store.metricSignal("vehicle.speed").value;
  return rec && rec.available && !rec.stale && typeof rec.value === "number" ? rec.value : null;
}

export default function RadarCalibration({ open, onClose }) {
  const closeRef = useRef(null);
  const [ctrl, state] = useController(createController, (c) => c.state());

  useEffect(() => {
    ctrl.refresh();
    return () => ctrl.dispose();
  }, []);

  // Closing never stops a calibration (it runs on the Pi), and unlike the other sheets a tap on
  // the backdrop does not close this one: the progress should stay up while driving.
  const { dialogRef, close } = useModalDialog({ open, onClose, focusRef: closeRef });

  const vm = H.viewModel(state, { speedMph: liveSpeed() });
  const ended = vm.finishedAt ? new Date(vm.finishedAt) : null;
  const finished = ended && !Number.isNaN(ended.getTime()) ? fmtClock(ended) : "";

  return (
    <dialog ref={dialogRef} class="dialog radar-cal" aria-labelledby="radar-cal-title">
      <header class="dialog__head">
        <h2 id="radar-cal-title" class="dialog__title">{H.TEXT.title}</h2>
        <button type="button" class="dialog__close" aria-label="Close radar calibration" ref={closeRef} onClick={close}>×</button>
      </header>
      <div class="dialog__body">
        {vm.showRun ? (
          <section class={"radar-cal__run" + (vm.tone ? " radar-cal__run--" + vm.tone : "")} aria-live="polite">
            <div class="radar-cal__headline">{vm.headline}</div>
            <div class="radar-cal__percent num">{vm.progressText}</div>
            <progress class="radar-cal__bar" max="100" value={vm.progress === null ? 0 : vm.progress} />
            <dl class="kv radar-cal__facts num">
              {vm.elapsedText ? [<dt key="tk">Running for</dt>, <dd key="tv">{vm.elapsedText}</dd>] : null}
              {vm.active && vm.speedText ? [<dt key="sk">Speed</dt>, <dd key="sv">{vm.speedText}</dd>] : null}
              {finished ? [<dt key="fk">Ended</dt>, <dd key="fv">{finished}</dd>] : null}
            </dl>
            {vm.reasonText ? <p class="radar-cal__reason">{vm.reasonText}</p> : null}
            {vm.detail ? <p class="radar-cal__detail">{vm.detail}</p> : null}
          </section>
        ) : null}
        {vm.active ? null : (
          <>
            <p class="radar-cal__text">{H.TEXT.intro}</p>
            <p class="radar-cal__text radar-cal__text--caution">{H.TEXT.caution}</p>
            <label class="check">
              <input
                type="checkbox"
                checked={vm.confirmed}
                disabled={!vm.canConfirm}
                onChange={(e) => ctrl.setConfirmed(e.currentTarget.checked)}
              />
              <span>{H.TEXT.confirm}</span>
            </label>
          </>
        )}
        <div class="btn-row">
          {vm.active ? (
            <button type="button" class="btn btn--danger radar-cal__action" disabled={!vm.canStop} onClick={() => ctrl.stop()}>
              {vm.stopLabel}
            </button>
          ) : (
            <button type="button" class="btn btn--primary radar-cal__action" disabled={!vm.canStart} onClick={() => ctrl.start()}>
              {vm.startLabel}
            </button>
          )}
        </div>
        {vm.hint ? <p class="status-line">{vm.hint}</p> : null}
        {vm.statusText ? (
          <p class={"status-line" + (vm.statusIsError ? " status-line--error" : "")} role="status">{vm.statusText}</p>
        ) : null}
      </div>
    </dialog>
  );
}
