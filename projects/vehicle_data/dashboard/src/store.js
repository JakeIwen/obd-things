/**
 * Dashboard v2 store: the reactive truth for the metric catalog, per-metric
 * observations, vehicle state, status-lite, web flags, connection state, the
 * coarse clock and the supplemental summary slices.
 *
 * Publishing discipline (design section 2.2, freshness contract rules 7, 8,
 * 10, 12, 15, 17):
 *
 * - Every observation is a plain object that is replaced wholesale when the
 *   *sample* changes (value, unit, quality, source, observed_at, reason,
 *   detail, retained sample, stale flag, ...). When an accepted delivery
 *   carries the same sample again, only the two timing fields
 *   (`receivedMono`, `ageAtReceiptMs`) are refreshed in place and nothing is
 *   published, so components bound to a metric are not re-rendered by the
 *   1 Hz stream when nothing visible changed.
 * - `tick()` publishes only the observations whose stale flag flips.
 * - `applySummary()` publishes a slice only when its JSON differs.
 *
 * Ages are monotonic (`performance.now()` domain, supplied by the caller);
 * wall-clock fields (`observed_at`) are display-only.
 *
 * This module imports `@preact/signals-core` so it runs unchanged under Node
 * for tests; UI code binds these signals through `@preact/signals`.
 */

import { signal, computed, batch } from "@preact/signals-core";

/** Qualities that may drive a primary (hero) number. Rule 8. */
export const DRIVER_QUALITIES = new Set(["verified", "observed_alfa_scale"]);

/** Metrics allowed to show a dated last reading when not live. Rule 15. */
export const RETAIN_LAST_READING = new Set([
  "battery.voltage",
  "vehicle.odometer",
  "engine.oil_life_remaining",
  "engine.coolant_temperature",
  "engine.vvt_oil_temperature",
  "transmission.oil_temperature",
  "tire.pressure.fl",
  "tire.pressure.fr",
  "tire.pressure.rl",
  "tire.pressure.rr",
  "radar.alignment.elevation",
  "radar.alignment.azimuth",
]);

/** Metrics whose `candidate` quality may still be retained. Rule 15. */
export const RETAIN_CANDIDATE_ESTIMATES = new Set([
  "vehicle.odometer",
  "radar.alignment.azimuth",
  "radar.alignment.elevation",
]);

/** Verified vehicle state older than this is unknown. Rules 7 and 16. */
export const MAX_STATE_AGE_MS = 5000;

/**
 * Tolerance for the "non-future observed_at" check of retained samples, so a
 * tablet clock a little behind the Pi does not hide every last reading.
 */
export const FUTURE_TOLERANCE_MS = 300000;

/** Catalog definitions from the last accepted HTTP baseline. */
export const catalog = signal([]);

/** Catalog definitions keyed by metric name. */
export const catalogByName = computed(() => {
  const map = new Map();
  const defs = catalog.value;
  for (let i = 0; i < defs.length; i += 1) {
    const def = defs[i];
    if (def && typeof def.name === "string") map.set(def.name, def);
  }
  return map;
});

/** Sorted metric names present in the last accepted delivery. */
export const metricNames = signal([]);

/**
 * Vehicle state record derived from `status.vehicle_state` (rule 7).
 * `ageAtReceiptMs`/`receivedMono` and the display-only `observed_at` are
 * refreshed in place like observation timing fields; the record is published
 * only when state, running, confidence, basis or detail change.
 */
export const vehicle = signal(unknownVehicle("no_snapshot", null, 0));

/**
 * Status-lite object from the last accepted delivery (HTTP or stream).
 * Published only when its stable projection changes: counters and clocks that
 * move every second (`collector.cycles`, `last_cycle_at`, `interface_probe`
 * elapsed time, `history_recorder`, `inflight`, `vehicle_state` age fields,
 * role-interface generation stamps) and the per-poll ownership flags
 * (`active_acquisition_permitted`, `current_owner`) are excluded from the
 * comparison, so readers are not re-run at 1 Hz. The published object still
 * carries them.
 */
export const statusLite = signal({});

/** Web listener flags (`active_acquisition_enabled`, `dtc_jobs_enabled`, ...). */
export const web = signal({});

/** Broker link state, owned by `link.js`. */
export const connection = signal({
  state: "connecting",
  reason: null,
  detail: null,
  sinceMono: 0,
});

/**
 * Coarse monotonic clock in ms, rounded to the second; advanced by `tick()`
 * and by every accepted delivery (link.js skips `tick()` while a stream
 * event was accepted in the last second).
 */
export const clock = signal(0);

/** Supplemental `/v2/summary` slices, each published only when its JSON changes. */
export const summary = {
  history: signal(null),
  health: signal(null),
  dtcs: signal(null),
  maintenance: signal(null),
  statusFull: signal(null),
  fetchedMono: signal(0),
  error: signal(null),
};

/** @type {Map<string, import("@preact/signals-core").Signal>} */
const metricSignals = new Map();

/** Last-seen JSON per summary slice / small object, for change detection. */
const lastJson = {
  status: null,
  catalog: null,
  web: null,
  history: null,
  health: null,
  dtcs: null,
  maintenance: null,
  statusFull: null,
  error: null,
};

const SUMMARY_SLICES = [
  ["history", "history"],
  ["health", "health"],
  ["dtcs", "dtcs"],
  ["maintenance", "maintenance"],
  ["status_full", "statusFull"],
];

/**
 * Return the signal holding a metric's observation, creating it (as `null`)
 * when the metric has not been seen yet.
 * @param {string} name metric name
 * @returns {import("@preact/signals-core").Signal<object|null>}
 */
export function metricSignal(name) {
  let sig = metricSignals.get(name);
  if (!sig) {
    sig = signal(null);
    metricSignals.set(name, sig);
  }
  return sig;
}

/**
 * Apply a full HTTP snapshot: catalog, metrics, status, web flags and the
 * vehicle state, with rule 7 ageing. Publishes inside one `batch()`.
 * @param {object} snapshot `/v1/snapshot` body (already validated by link.js)
 * @param {number} deliveryAgeMs age added to every `age_ms` (HTTP: round trip)
 * @param {number} nowMono `performance.now()` at acceptance
 */
export function applyBaseline(snapshot, deliveryAgeMs, nowMono) {
  const body = snapshot && typeof snapshot === "object" ? snapshot : {};
  batch(() => {
    const defs = Array.isArray(body.catalog) ? body.catalog : [];
    const json = JSON.stringify(defs);
    if (json !== lastJson.catalog) {
      lastJson.catalog = json;
      catalog.value = defs;
    }
    applyDelivery(body, deliveryAgeMs, nowMono);
  });
}

/**
 * Apply a lite `/v2/stream` event: metrics, status-lite, web flags and the
 * vehicle state. The catalog is untouched (rule 19: a hash change resyncs).
 * @param {object} event stream event body
 * @param {number} deliveryAgeMs computed stream delivery age (rule 4)
 * @param {number} nowMono `performance.now()` at acceptance
 */
export function applyStream(event, deliveryAgeMs, nowMono) {
  const body = event && typeof event === "object" ? event : {};
  batch(() => {
    applyDelivery(body, deliveryAgeMs, nowMono);
  });
}

/**
 * Apply a `/v2/summary` bundle. Each slice is published only when its JSON
 * differs from the previous one; `fetchedMono` always advances. A bundle with
 * `available === false` records `error` and leaves the slices alone.
 * @param {object} bundle `/v2/summary` body, or an error envelope
 * @param {number} nowMono `performance.now()` when the bundle arrived
 */
export function applySummary(bundle, nowMono) {
  const body = bundle && typeof bundle === "object" ? bundle : {};
  batch(() => {
    summary.fetchedMono.value = nowMono;
    let error = null;
    if (body.available === false) {
      error = {
        reason: typeof body.reason === "string" ? body.reason : "cache_unavailable",
        detail: typeof body.detail === "string" ? body.detail : null,
      };
    } else {
      for (let i = 0; i < SUMMARY_SLICES.length; i += 1) {
        const key = SUMMARY_SLICES[i][0];
        const target = SUMMARY_SLICES[i][1];
        if (body[key] === undefined) continue;
        const json = JSON.stringify(body[key]);
        if (json === lastJson[target]) continue;
        lastJson[target] = json;
        summary[target].value = body[key];
      }
    }
    const errorJson = JSON.stringify(error);
    if (errorJson !== lastJson.error) {
      lastJson.error = errorJson;
      summary.error.value = error;
    }
  });
}

/**
 * Rule 12: mark every live observation stale and the vehicle state unknown.
 * Only records that actually flip are published.
 * @param {string} reason basis recorded on the vehicle state, e.g.
 *   `client_page_hidden`, `client_page_visible`, `client_page_restored`
 * @param {number} nowMono `performance.now()`
 */
export function invalidate(reason, nowMono) {
  batch(() => {
    metricSignals.forEach((sig) => {
      const rec = sig.peek();
      if (rec && !rec.stale) sig.value = withStale(rec);
    });
    const current = vehicle.peek();
    if (
      current.state !== "unknown" ||
      current.confidence !== "stale" ||
      current.basis !== reason
    ) {
      vehicle.value = unknownVehicle(reason, current.observed_at, nowMono);
    }
  });
}

/**
 * Rule 10 watchdog: advance the coarse clock (rounded to the second) and flip
 * the stale flag of observations whose age just crossed `staleAfterMs`, plus a
 * verified vehicle state older than 5 s. Publishes only what flipped.
 * @param {number} nowMono `performance.now()`
 */
export function tick(nowMono) {
  batch(() => {
    advanceClock(nowMono);
    metricSignals.forEach((sig) => {
      const rec = sig.peek();
      if (!rec || rec.stale || !rec.available) return;
      const age = ageMs(rec, nowMono);
      if (age === null || rec.staleAfterMs === null || age > rec.staleAfterMs) {
        sig.value = withStale(rec);
      }
    });
    const current = vehicle.peek();
    if (current.confidence === "verified") {
      const age = ageMs(current, nowMono);
      if (age === null) {
        vehicle.value = unknownVehicle("client_freshness_invalid", current.observed_at, nowMono);
      } else if (age > MAX_STATE_AGE_MS) {
        vehicle.value = unknownVehicle("client_freshness_expired", current.observed_at, nowMono);
      }
    }
  });
}

/**
 * Current age of an observation (or the vehicle record) in ms, extrapolated
 * monotonically from its acceptance; `null` when the age was invalid.
 * @param {object|null} observation record with `ageAtReceiptMs`/`receivedMono`
 * @param {number} nowMono `performance.now()`
 * @returns {number|null}
 */
export function ageMs(observation, nowMono) {
  if (!observation || !Number.isFinite(observation.ageAtReceiptMs)) return null;
  const elapsed = nowMono - observation.receivedMono;
  return observation.ageAtReceiptMs + (elapsed > 0 ? elapsed : 0);
}

/**
 * Display state of one metric (rules 8 and 15).
 *
 * - `live`: available, not stale and driver-qualified; `value` is the live one.
 * - `held`: not live, the metric is in `RETAIN_LAST_READING`, and a valid
 *   retained sample exists (the live-but-stale metric itself or
 *   `last_recorded`, newest `observed_at` wins); `value` is the retained one.
 * - `off`: everything else; `value` is `null`.
 *
 * Reads the metric signal and `catalogByName`, so a `computed()` wrapping it
 * tracks both; `nowMono` is a parameter so the caller decides whether to
 * depend on the clock.
 * @param {string} name metric name
 * @param {number} nowMono `performance.now()`
 * @param {number} [nowWall] `Date.now()`, for the non-future check only
 * @returns {{kind:'live'|'held'|'off', value:*, unit:string|null, quality:string|null,
 *   stale:boolean, ageMs:number|null,
 *   retained:{value:number, unit:string, observed_at:string}|null, definition:object|undefined}}
 */
export function observationState(name, nowMono, nowWall) {
  const definition = catalogByName.value.get(name);
  const rec = metricSignal(name).value;
  const unit = definition && typeof definition.unit === "string" ? definition.unit : null;
  if (!rec) {
    return state("off", null, unit, null, true, null, null, definition);
  }
  const quality = rec.quality || definitionQuality(definition, rec.source);
  const age = ageMs(rec, nowMono);
  const stale =
    rec.stale ||
    (age !== null && rec.staleAfterMs !== null && age > rec.staleAfterMs) ||
    (rec.available && age === null);
  if (rec.available && !stale && DRIVER_QUALITIES.has(quality)) {
    return state("live", rec.value, rec.unit, quality, false, age, null, definition);
  }
  if (RETAIN_LAST_READING.has(name) && definition) {
    const retained = retainedSample(name, rec, definition, nowWall === undefined ? Date.now() : nowWall);
    if (retained) {
      return state(
        "held",
        retained.value,
        retained.unit,
        retained.quality,
        true,
        age,
        { value: retained.value, unit: retained.unit, observed_at: retained.observed_at },
        definition,
      );
    }
  }
  return state("off", null, unit || rec.unit || null, quality, stale, age, null, definition);
}

// ---------------------------------------------------------------------------
// internals

function state(kind, value, unit, quality, stale, age, retained, definition) {
  return { kind, value, unit, quality, stale, ageMs: age, retained, definition };
}

/** Set `clock` to `nowMono` rounded to the second; publishes only when the second changes. */
function advanceClock(nowMono) {
  if (!Number.isFinite(nowMono)) return;
  const coarse = Math.round(nowMono / 1000) * 1000;
  if (clock.peek() !== coarse) clock.value = coarse;
}

/**
 * Shared metric/status/web/vehicle path for HTTP baselines and stream events.
 */
function applyDelivery(body, deliveryAgeMs, nowMono) {
  // link.js skips tick() in any second where a delivery was accepted (rule 10),
  // so the coarse clock must also advance here, in the same batch.
  advanceClock(nowMono);
  const added = Number.isFinite(deliveryAgeMs) && deliveryAgeMs > 0 ? deliveryAgeMs : 0;
  const defs = catalogByName.peek();
  const metrics = body.metrics && typeof body.metrics === "object" ? body.metrics : {};
  const names = Object.keys(metrics).sort();

  for (let i = 0; i < names.length; i += 1) {
    const name = names[i];
    upsertMetric(name, metrics[name], defs.get(name), added, nowMono);
  }
  // Metrics the broker stopped reporting are no longer live.
  metricSignals.forEach((sig, name) => {
    const rec = sig.peek();
    if (rec && !rec.stale && !Object.prototype.hasOwnProperty.call(metrics, name)) {
      sig.value = withStale(rec);
    }
  });
  if (names.join("\n") !== metricNames.peek().join("\n")) metricNames.value = names;

  const status = body.status && typeof body.status === "object" ? body.status : {};
  const stable = JSON.stringify(status, stableStatusReplacer);
  if (stable !== lastJson.status) {
    lastJson.status = stable;
    statusLite.value = status;
  }
  upsertVehicle(status.vehicle_state, added, nowMono);

  const flags = body.web && typeof body.web === "object" ? body.web : status.web;
  const flagsJson = JSON.stringify(flags || {});
  if (flagsJson !== lastJson.web) {
    lastJson.web = flagsJson;
    web.value = flags || {};
  }
}

/** Keys whose values change every delivery without a visible meaning. */
const VOLATILE_STATUS_KEYS = new Set([
  "cycles",
  "last_cycle_at",
  "elapsed_seconds",
  "age_ms",
  "observed_at",
  "generated_at",
  "last_stored_at",
  "snapshots_stored",
  "last_refreshed_at",
  "last_refresh_duration_ms",
  "last_event_at",
  "inflight",
  "history_recorder",
  "vehicle_state",
  "web",
  // Both flip with every broker-owned passive voltage poll (~every 2 s while
  // asleep). No view reads them from status-lite (the Broker card reads
  // `current_owner` from status_full), so they must not republish it.
  "active_acquisition_permitted",
  "current_owner",
]);

function stableStatusReplacer(key, value) {
  return VOLATILE_STATUS_KEYS.has(key) ? undefined : value;
}

function validAge(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function staleAfterOf(definition) {
  const seconds = definition ? definition.stale_after_seconds : undefined;
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return null;
  return seconds * 1000;
}

function orNull(value) {
  return value === undefined ? null : value;
}

/**
 * Rule 7 for one metric. Same sample → refresh timing fields in place and
 * publish nothing; otherwise publish a fresh record.
 */
function upsertMetric(name, metric, definition, added, nowMono) {
  const sig = metricSignal(name);
  const m = metric && typeof metric === "object" ? metric : {};
  const available = m.available === true;
  const staleAfterMs = staleAfterOf(definition);
  const ageOk = validAge(m.age_ms);
  const ageAtReceiptMs = ageOk ? m.age_ms + added : null;
  const stale =
    !available ||
    m.stale === true ||
    !ageOk ||
    staleAfterMs === null ||
    ageAtReceiptMs > staleAfterMs;

  const prev = sig.peek();
  if (prev && sameSample(prev, m, available, stale, staleAfterMs)) {
    prev.receivedMono = nowMono;
    prev.ageAtReceiptMs = ageAtReceiptMs;
    return;
  }
  sig.value = {
    name,
    available,
    value: orNull(m.value),
    unit: orNull(m.unit),
    quality: orNull(m.quality),
    source: orNull(m.source),
    bus: orNull(m.bus),
    acquisition: orNull(m.acquisition),
    interface_mode: orNull(m.interface_mode),
    observed_at: orNull(m.observed_at),
    reason: orNull(m.reason),
    detail: orNull(m.detail),
    last_recorded: m.last_recorded && typeof m.last_recorded === "object" ? m.last_recorded : null,
    last_acquisition_error:
      m.last_acquisition_error && typeof m.last_acquisition_error === "object"
        ? m.last_acquisition_error
        : null,
    receivedMono: nowMono,
    ageAtReceiptMs,
    staleAfterMs,
    stale,
  };
}

function sameSample(prev, m, available, stale, staleAfterMs) {
  if (prev.available !== available || prev.stale !== stale) return false;
  if (prev.staleAfterMs !== staleAfterMs) return false;
  if (prev.value !== orNull(m.value) || prev.unit !== orNull(m.unit)) return false;
  if (prev.quality !== orNull(m.quality) || prev.source !== orNull(m.source)) return false;
  if (prev.observed_at !== orNull(m.observed_at)) return false;
  if (prev.reason !== orNull(m.reason) || prev.detail !== orNull(m.detail)) return false;
  if (prev.bus !== orNull(m.bus) || prev.acquisition !== orNull(m.acquisition)) return false;
  if (prev.interface_mode !== orNull(m.interface_mode)) return false;
  if (!sameRecorded(prev.last_recorded, m.last_recorded)) return false;
  return sameError(prev.last_acquisition_error, m.last_acquisition_error);
}

function sameRecorded(a, b) {
  const x = a && typeof a === "object" ? a : null;
  const y = b && typeof b === "object" ? b : null;
  if (!x || !y) return x === y;
  return (
    x.value === y.value &&
    x.unit === y.unit &&
    x.quality === y.quality &&
    x.source === y.source &&
    x.observed_at === y.observed_at
  );
}

function sameError(a, b) {
  const x = a && typeof a === "object" ? a : null;
  const y = b && typeof b === "object" ? b : null;
  if (!x || !y) return x === y;
  return x.reason === y.reason && x.detail === y.detail;
}

function withStale(rec) {
  const next = Object.assign({}, rec);
  next.stale = true;
  return next;
}

function unknownVehicle(basis, observedAt, nowMono) {
  return {
    state: "unknown",
    running: null,
    confidence: basis === "no_snapshot" ? "unknown" : "stale",
    basis,
    detail: null,
    observed_at: orNull(observedAt),
    ageAtReceiptMs: null,
    receivedMono: nowMono,
  };
}

/** Rule 7 for `status.vehicle_state`. */
function upsertVehicle(raw, added, nowMono) {
  const v = raw && typeof raw === "object" ? raw : null;
  if (!v) {
    const prev = vehicle.peek();
    if (prev.basis !== "no_vehicle_state" || prev.state !== "unknown") {
      vehicle.value = unknownVehicle("no_vehicle_state", null, nowMono);
    }
    return;
  }
  const ageOk = validAge(v.age_ms);
  const ageAtReceiptMs = ageOk ? v.age_ms + added : null;
  let next;
  if (v.confidence === "verified" && !ageOk) {
    next = unknownVehicle("client_freshness_invalid", v.observed_at, nowMono);
  } else if (v.confidence === "verified" && ageAtReceiptMs > MAX_STATE_AGE_MS) {
    next = unknownVehicle("client_freshness_expired", v.observed_at, nowMono);
    next.ageAtReceiptMs = ageAtReceiptMs;
  } else {
    next = {
      state: typeof v.state === "string" ? v.state : "unknown",
      running: typeof v.running === "boolean" ? v.running : null,
      confidence: typeof v.confidence === "string" ? v.confidence : "unknown",
      basis: orNull(v.basis),
      detail: orNull(v.detail),
      observed_at: orNull(v.observed_at),
      ageAtReceiptMs,
      receivedMono: nowMono,
    };
  }
  const prev = vehicle.peek();
  if (
    prev.state === next.state &&
    prev.running === next.running &&
    prev.confidence === next.confidence &&
    prev.basis === next.basis &&
    prev.detail === next.detail
  ) {
    // `observed_at` is display-only (rule 9) and the broker restamps it every
    // collector cycle (~2 s) even while the state is unchanged, so it is a
    // timing field here: refreshed in place, never published on its own.
    prev.observed_at = next.observed_at;
    prev.receivedMono = nowMono;
    prev.ageAtReceiptMs = next.ageAtReceiptMs;
    return;
  }
  vehicle.value = next;
}

/**
 * Quality implied by the catalog when the observation carries none: the
 * matching source's quality, else the single quality shared by all sources.
 */
function definitionQuality(definition, source) {
  const sources = definition && Array.isArray(definition.sources) ? definition.sources : [];
  if (source) {
    for (let i = 0; i < sources.length; i += 1) {
      if (sources[i] && sources[i].name === source) return sources[i].quality || null;
    }
  }
  let quality = null;
  for (let i = 0; i < sources.length; i += 1) {
    const q = sources[i] ? sources[i].quality : null;
    if (!q) continue;
    if (quality === null) quality = q;
    else if (quality !== q) return null;
  }
  return quality;
}

/** Rule 15 validation of one retained sample candidate. */
function validRetained(name, sample, definition, nowWall) {
  if (!sample || typeof sample !== "object") return false;
  if (typeof sample.value !== "number" || !Number.isFinite(sample.value)) return false;
  if (sample.unit !== definition.unit) return false;
  const at = Date.parse(sample.observed_at);
  if (!Number.isFinite(at) || at > nowWall + FUTURE_TOLERANCE_MS) return false;
  if (typeof definition.minimum === "number" && sample.value < definition.minimum) return false;
  if (typeof definition.maximum === "number" && sample.value > definition.maximum) return false;
  const sources = Array.isArray(definition.sources) ? definition.sources : [];
  let source = null;
  for (let i = 0; i < sources.length; i += 1) {
    if (sources[i] && sources[i].name === sample.source) {
      source = sources[i];
      break;
    }
  }
  if (!source || source.quality !== sample.quality) return false;
  return (
    DRIVER_QUALITIES.has(sample.quality) ||
    (sample.quality === "candidate" && RETAIN_CANDIDATE_ESTIMATES.has(name))
  );
}

/** Newest valid retained sample: the live metric (if available) or `last_recorded`. */
function retainedSample(name, rec, definition, nowWall) {
  const live = rec.available && validRetained(name, rec, definition, nowWall) ? rec : null;
  const recorded =
    rec.last_recorded && validRetained(name, rec.last_recorded, definition, nowWall)
      ? rec.last_recorded
      : null;
  if (live && recorded) {
    return Date.parse(recorded.observed_at) > Date.parse(live.observed_at) ? recorded : live;
  }
  return live || recorded;
}
