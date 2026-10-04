/** Throttled supplemental requests. A page invalidation resets the throttle, not the in-flight request. */
import { SUPPLEMENTAL_REFRESH_MS } from './link.protocol.js';

export function createSummary({ fetchImpl, now, wallClock, summaryPath, store, describeError }) {
  const state = { summarySequence: 0, summaryStartedMono: null, summaryInFlight: null, summaryCount: 0 };

  // -- supplementals (rule 13) ---------------------------------------------------

  /**
   * Fetch `/v2/summary` once, throttled to one start per 60 s; obsolete
   * responses are dropped. Failures become `{available:false, reason:'cache_unavailable'}`.
   * @returns {Promise<boolean>} true when a bundle was handed to the store
   */
  function fetchSummary(options) {
    if (state.summaryInFlight) return state.summaryInFlight;
    const startedMono = now();
    const force = Boolean(options && options.force);
    if (!force && state.summaryStartedMono != null && startedMono - state.summaryStartedMono < SUPPLEMENTAL_REFRESH_MS) {
      return Promise.resolve(false);
    }
    state.summaryStartedMono = startedMono;
    const sequence = ++state.summarySequence;
    const url = `${summaryPath}?fresh=${wallClock()}-${sequence}`;
    const operation = (async () => {
      let bundle;
      try {
        const response = await fetchImpl(url, { cache: 'no-store' });
        const payload = await response.json();
        if (response.ok) {
          bundle = payload;
        } else {
          bundle = {
            available: false,
            reason: (payload && payload.reason) || 'cache_unavailable',
            detail: (payload && payload.detail) || `HTTP ${response.status}`,
            status_code: response.status,
          };
        }
      } catch (error) {
        bundle = { available: false, reason: 'cache_unavailable', detail: describeError(error) };
      }
      if (sequence !== state.summarySequence) return false;
      state.summaryCount += 1;
      store.applySummary(bundle, now());
      return true;
    })();
    const tracked = operation.then(
      (value) => {
        if (state.summaryInFlight === tracked) state.summaryInFlight = null;
        return value;
      },
      (error) => {
        if (state.summaryInFlight === tracked) state.summaryInFlight = null;
        throw error;
      },
    );
    state.summaryInFlight = tracked;
    return tracked;
  }

  return { state, fetchSummary };
}
