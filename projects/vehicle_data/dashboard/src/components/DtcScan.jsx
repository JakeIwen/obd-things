/**
 * Guarded parked DTC scan panel (design 3.3 / critique A11). Rendered by the Health view inside
 * the Diagnostic codes card only when the listener reports `web.dtc_jobs_enabled`. All gating,
 * requests and polling live in dtcScan.helpers.js (createDtcScanController); this file renders its
 * view model and forwards taps. The only timer is the controller's documented job poll (2 s while
 * active and visible, 5 s after an error); it stops when the panel unmounts.
 *
 * Props: { web } — the store's web flags (`dtc_jobs_enabled`, `dtc_jobs_require_local_one_use_arm`).
 */

import { useEffect, useRef, useState } from "preact/hooks";
import { manualRefresh } from "../app/runtime.js";
import * as H from "./dtcScan.helpers.js";

function useScanController() {
  const ref = useRef(null);
  const [vm, setVm] = useState(null);
  if (ref.current === null) {
    ref.current = H.createDtcScanController({
      fetch: (url, init) => globalThis.fetch(url, init),
      isVisible: () => typeof document === "undefined" || document.visibilityState !== "hidden",
      setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
      clearTimeout: (id) => globalThis.clearTimeout(id),
      confirm: (message) => typeof window !== "undefined" && window.confirm(message),
      onChange: setVm,
      onFinished: () => {
        manualRefresh();
      },
    });
  }
  return [ref.current, vm || ref.current.snapshot()];
}

export default function DtcScan({ web }) {
  const [ctrl, vm] = useScanController();
  const enabled = Boolean(web && web.dtc_jobs_enabled === true);
  const tokenRequired = Boolean(web && web.dtc_jobs_require_local_one_use_arm === true);

  useEffect(() => {
    ctrl.configure({ dtc_jobs_enabled: enabled, dtc_jobs_require_local_one_use_arm: tokenRequired });
  }, [enabled, tokenRequired]);

  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState !== "hidden") ctrl.resume();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      ctrl.dispose();
    };
  }, []);

  return (
    <details class="disc health-scan">
      <summary class="disc__summary">{vm.summaryText}</summary>
      <div class="disc__body">
        <p class="health-scan__intro">{H.TEXT.intro}</p>
        <label class="check">
          <input
            type="checkbox"
            checked={vm.confirmed}
            onChange={(e) => ctrl.setConfirmed(e.currentTarget.checked)}
          />
          <span>{H.TEXT.confirmLabel}</span>
        </label>
        {vm.tokenRequired ? (
          <label class="field">
            {H.TEXT.tokenLabel}
            <input
              class="input"
              type="password"
              autocomplete="off"
              maxLength={H.TOKEN_MAX}
              value={vm.token}
              onInput={(e) => ctrl.setToken(e.currentTarget.value)}
            />
            <span class="health-scan__hint">{H.TEXT.tokenHint}</span>
          </label>
        ) : null}
        <div class="btn-row">
          <button type="button" class="btn btn--primary" disabled={!vm.canStart} onClick={() => ctrl.start()}>
            {vm.startLabel}
          </button>
          <button type="button" class="btn btn--danger" disabled={!vm.canCancel} onClick={() => ctrl.cancel()}>
            {vm.cancelLabel}
          </button>
        </div>
        {vm.hint ? <p class="status-line">{vm.hint}</p> : null}
        {vm.lockout ? <p class="status-line health-scan__lock">{H.TEXT.lockout}</p> : null}
        <p class={"status-line" + (vm.error ? " status-line--error" : "")} role="status">
          {vm.statusText}
        </p>
      </div>
    </details>
  );
}
