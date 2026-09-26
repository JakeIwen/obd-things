/**
 * Band evaluation for gauges and sparklines (design.md section 5).
 *
 * Bands are display context, not alarms: they colour the band bar and the
 * faint "normal" region of a sparkline. Alarms come from the broker's
 * assessments. The red thresholds here are the same numbers the tier-0 rules
 * use, so a bar never turns red while the rule set calls the value normal.
 * Between "normal" and "amber" there is a neutral zone: outside the typical
 * envelope but no rule applies.
 *
 * Boundary convention: the normal band is inclusive on both ends; anything
 * above its upper limit but below the red threshold is amber; red is `>=`
 * the rule's threshold (or `<` for low-side rules), exactly as the rule
 * compares.
 *
 * `evaluateBand` memoises the last result per metric name, so repeated calls
 * on the 1 Hz path return the same object whenever nothing visible changed
 * (a `computed` bound to it does not notify its subscribers) and allocate
 * nothing in the steady state.
 */

/** Gauge spans (min/max of the band bar) per metric, design section 5. */
export const BAND_SPANS = Object.freeze({
  "engine.coolant_temperature": Object.freeze({ min: 60, max: 240 }),
  "transmission.oil_temperature": Object.freeze({ min: 60, max: 240 }),
  "engine.oil_pressure": Object.freeze({ min: 0, max: 100 }),
  "battery.voltage": Object.freeze({ min: 10, max: 16 }),
  "engine.vvt_oil_temperature": Object.freeze({ min: 60, max: 240 }),
  "generator.field_duty": Object.freeze({ min: 0, max: 100 }),
  "engine.rpm": Object.freeze({ min: 0, max: 4500 }),
  "vehicle.speed": Object.freeze({ min: 0, max: 90 }),
  "engine.crankshaft_power": Object.freeze({ min: -30, max: 260 }),
  "engine.crankshaft_torque": Object.freeze({ min: -60, max: 240 }),
  "tire.pressure.fl": Object.freeze({ min: 40, max: 70 }),
  "tire.pressure.fr": Object.freeze({ min: 40, max: 70 }),
  "tire.pressure.rl": Object.freeze({ min: 60, max: 90 }),
  "tire.pressure.rr": Object.freeze({ min: 60, max: 90 }),
});

/**
 * Threshold numbers from design section 5 (the same numbers the warning
 * tiers in section 7.2 use), exported so tests and the docs page can cite
 * one source.
 */
export const LIMITS = Object.freeze({
  // Normal reaches 220 °F: hot-day idle at 213–220 °F is the thermostat-open plateau (13 of 57 trips).
  coolant: Object.freeze({ normalLo: 160, normalHi: 220, red: 230, critical: 240 }),
  transmission: Object.freeze({ normalLo: 80, normalHi: 183, red: 230 }),
  vvt: Object.freeze({ normalHi: 219, red: 230 }),
  oil: Object.freeze({
    minRpm: 400,
    minCoolant: 160,
    critical: 12,
    normalHi: 85,
    // Minimum psi by rpm band (design 5): idle >= 15; 1,000-3,000 rpm >= 22;
    // > 3,500 rpm >= 55. The gaps (850-1,000 and 3,000-3,500) take the lower
    // adjacent floor so the bar never colours where no rule applies.
    idleFloor: 15,
    midFloor: 22,
    highFloor: 55,
    midFromRpm: 1000,
    highAboveRpm: 3500,
  }),
  voltageRunning: Object.freeze({ normalLo: 13.6, normalHi: 14.6, reducedCharge: 12.8 }),
  voltageParked: Object.freeze({ normalLo: 12.2, amberLo: 11.8, critical: 11.5 }),
  tires: Object.freeze({
    under: -3,
    over: 8,
    amberUnder: -5,
    pairExcess: 3,
    floorFront: 50,
    floorRear: 68,
  }),
});

/** Band states, in escalation order. */
export const BAND_STATES = Object.freeze(["none", "neutral", "normal", "amber", "red"]);

const NONE_UNKNOWN = Object.freeze({ state: "none", lo: null, hi: null, min: null, max: null, label: null });

/** name -> last result object returned by evaluateBand (memo, see header). */
const lastByName = new Map();

/**
 * Return the memoised result for `name` when every field matches, otherwise
 * create, freeze and remember a new one.
 */
function emit(name, state, lo, hi, min, max, label) {
  const prev = lastByName.get(name);
  if (
    prev !== undefined &&
    prev.state === state &&
    prev.lo === lo &&
    prev.hi === hi &&
    prev.min === min &&
    prev.max === max &&
    prev.label === label
  ) {
    return prev;
  }
  const next = Object.freeze({ state: state, lo: lo, hi: hi, min: min, max: max, label: label });
  lastByName.set(name, next);
  return next;
}

function isNum(v) {
  return typeof v === "number" && isFinite(v);
}

/**
 * Whether a metric name is a front tire (`tire.pressure.fl`/`fr`).
 * @param {string} name
 * @returns {boolean}
 */
export function isFrontTire(name) {
  return name === "tire.pressure.fl" || name === "tire.pressure.fr";
}

/**
 * Whether a metric name is any tire pressure metric.
 * @param {string} name
 * @returns {boolean}
 */
export function isTire(name) {
  return typeof name === "string" && name.indexOf("tire.pressure.") === 0;
}

/**
 * Minimum acceptable oil pressure for an engine speed (design 5: idle >= 15,
 * 1,000-3,000 rpm >= 22, > 3,500 rpm >= 55; gaps take the lower floor, so
 * exactly 3,500 rpm still uses 22).
 * @param {number} rpm
 * @returns {number} psi floor
 */
export function oilPressureFloor(rpm) {
  const L = LIMITS.oil;
  if (rpm < L.midFromRpm) return L.idleFloor;
  if (rpm <= L.highAboveRpm) return L.midFloor;
  return L.highFloor;
}

/**
 * Tire band against the wheel's own cold baseline (design 5, tires row).
 * Absolute floors (50 psi front / 68 psi rear) are red regardless of the
 * baseline; with a baseline, `value - coldBaseline` within -3..+8 is normal,
 * -5..-3 amber, below -5 red, above +8 neutral; an axle-pair excess over the
 * usual offset > 3 psi is amber when nothing worse applies. Without a
 * baseline only the floor and the pair rule can colour the wheel.
 * @param {number|null} value current pressure (psi)
 * @param {{coldBaseline?: number|null, isFront?: boolean, pairDeltaExcess?: number|null}} opts
 *   `pairDeltaExcess` is |(L - R) - usual offset| for the wheel's axle, already
 *   corrected for the pair's 30-day median offset.
 * @returns {{state: string, delta: number|null, label: string|null, floor: number, lo: number|null, hi: number|null}}
 */
export function tireBand(value, opts) {
  const o = opts || {};
  const floor = o.isFront ? LIMITS.tires.floorFront : LIMITS.tires.floorRear;
  const cold = isNum(o.coldBaseline) ? o.coldBaseline : null;
  const lo = cold === null ? null : cold + LIMITS.tires.under;
  const hi = cold === null ? null : cold + LIMITS.tires.over;
  if (!isNum(value)) return { state: "neutral", delta: null, label: null, floor: floor, lo: lo, hi: hi };
  const delta = cold === null ? null : value - cold;
  const pairExcess = isNum(o.pairDeltaExcess) ? o.pairDeltaExcess : null;
  let state = "neutral";
  let label = null;
  if (value < floor) {
    state = "red";
    label = "under " + floor + " psi floor";
  } else if (delta !== null && delta < LIMITS.tires.amberUnder) {
    state = "red";
    label = "low";
  } else if (delta !== null && delta < LIMITS.tires.under) {
    state = "amber";
    label = "low";
  } else if (pairExcess !== null && pairExcess > LIMITS.tires.pairExcess) {
    state = "amber";
    label = "pair mismatch";
  } else if (delta === null) {
    state = "neutral";
    label = "no baseline";
  } else if (delta > LIMITS.tires.over) {
    state = "neutral";
    label = "high";
  } else {
    state = "normal";
  }
  return { state: state, delta: delta, label: label, floor: floor, lo: lo, hi: hi };
}

/**
 * Band state string for one wheel; thin wrapper over `tireBand` for the
 * tires card.
 * @param {number|null} value current pressure (psi)
 * @param {{coldBaseline?: number|null, isFront?: boolean, pairDeltaExcess?: number|null}} opts
 * @returns {'normal'|'neutral'|'amber'|'red'}
 */
export function tireBandState(value, opts) {
  return tireBand(value, opts).state;
}

function coolantBand(name, value, span) {
  const L = LIMITS.coolant;
  let state = "neutral";
  let label = null;
  if (!isNum(value)) {
    state = "neutral";
  } else if (value >= L.critical) {
    state = "red";
    label = "critical";
  } else if (value >= L.red) {
    state = "red";
  } else if (value > L.normalHi) {
    state = "amber";
  } else if (value >= L.normalLo) {
    state = "normal";
  } else {
    state = "neutral";
    label = "warm-up";
  }
  return emit(name, state, L.normalLo, L.normalHi, span.min, span.max, label);
}

function transmissionBand(name, value, span) {
  const L = LIMITS.transmission;
  let state = "neutral";
  let label = null;
  if (!isNum(value)) {
    state = "neutral";
  } else if (value >= L.red) {
    state = "red";
  } else if (value > L.normalHi) {
    state = "amber";
  } else if (value >= L.normalLo) {
    state = "normal";
  } else {
    state = "neutral";
    label = "cold";
  }
  return emit(name, state, L.normalLo, L.normalHi, span.min, span.max, label);
}

function vvtBand(name, value, span) {
  const L = LIMITS.vvt;
  let state = "neutral";
  if (!isNum(value)) state = "neutral";
  else if (value >= L.red) state = "red";
  else if (value > L.normalHi) state = "amber";
  else state = "normal";
  return emit(name, state, span.min, L.normalHi, span.min, span.max, null);
}

function oilPressureBand(name, value, ctx, span) {
  const L = LIMITS.oil;
  const rpm = ctx.rpm;
  const coolant = ctx.coolant;
  if (!isNum(rpm)) return emit(name, "neutral", null, null, span.min, span.max, "no rpm");
  if (rpm < L.minRpm) return emit(name, "neutral", null, null, span.min, span.max, "engine off");
  if (!isNum(coolant) || coolant < L.minCoolant) {
    return emit(name, "neutral", null, null, span.min, span.max, "warm-up");
  }
  const floor = oilPressureFloor(rpm);
  let state = "neutral";
  let label = null;
  if (!isNum(value)) {
    state = "neutral";
  } else if (value < L.critical) {
    state = "red";
    label = "critical";
  } else if (value < floor) {
    state = "amber";
    label = "low for rpm";
  } else if (value <= L.normalHi) {
    state = "normal";
  } else {
    state = "neutral";
    label = "high";
  }
  return emit(name, state, floor, L.normalHi, span.min, span.max, label);
}

function voltageBand(name, value, ctx, span) {
  // `voltageMode` (preferred) is 'running' | 'parked' | 'neutral'; without it,
  // `running === true` selects the running table and anything else the parked one.
  const mode = ctx.voltageMode === "running" || ctx.voltageMode === "parked" || ctx.voltageMode === "neutral"
    ? ctx.voltageMode
    : ctx.running === true ? "running" : "parked";
  if (mode === "neutral") {
    return emit(name, "neutral", null, null, span.min, span.max, "settling");
  }
  if (mode === "running") {
    const R = LIMITS.voltageRunning;
    let state = "neutral";
    let label = null;
    if (!isNum(value)) {
      state = "neutral";
    } else if (ctx.chargingFailure === true) {
      state = "red";
      label = "charging failure";
    } else if (value < R.reducedCharge) {
      state = "amber";
      label = "reduced charge";
    } else if (value >= R.normalLo && value <= R.normalHi) {
      state = "normal";
    } else {
      state = "neutral";
    }
    return emit(name, state, R.normalLo, R.normalHi, span.min, span.max, label);
  }
  const P = LIMITS.voltageParked;
  let state = "neutral";
  let label = null;
  if (!isNum(value)) {
    state = "neutral";
  } else if (value < P.critical) {
    state = "red";
    label = "critical";
  } else if (value < P.amberLo) {
    state = "red";
  } else if (value < P.normalLo) {
    state = "amber";
  } else {
    state = "normal";
  }
  return emit(name, state, P.normalLo, span.max, span.min, span.max, label);
}

function tireMetricBand(name, value, ctx, span) {
  const b = ctx.baseline || null;
  const r = tireBand(value, {
    coldBaseline: b ? b.cold : null,
    isFront: b && typeof b.isFront === "boolean" ? b.isFront : isFrontTire(name),
    pairDeltaExcess: b ? b.pairDelta : null,
  });
  return emit(name, r.state, r.lo, r.hi, span.min, span.max, r.label);
}

/**
 * Evaluate the display band for a metric value (design section 5).
 *
 * @param {string} name metric name
 * @param {number|null|undefined} value current value in the metric's unit
 * @param {{rpm?: number|null, coolant?: number|null, running?: boolean|null,
 *          chargingFailure?: boolean,
 *          baseline?: {cold: number|null, pairDelta: number|null, isFront?: boolean}|null}} [ctx]
 *   `rpm`: pass the live rpm only when it is fresh and driver-qualified,
 *   otherwise null (oil pressure is neutral without it). `coolant`: live
 *   coolant °F or null. `running`: `vehicle.state === 'running'` (true selects
 *   the running voltage table; anything else the parked table).
 *   `chargingFailure`: true only while the charging-failure rule is in
 *   watch/warning (the only red for running voltage). `baseline`: per-wheel
 *   tire context; `pairDelta` is the axle pair's excess over its usual offset.
 * @returns {{state: 'normal'|'neutral'|'amber'|'red'|'none', lo: number|null,
 *            hi: number|null, min: number|null, max: number|null, label: string|null}}
 *   `lo`/`hi` bound the green band (null when the band cannot be placed, e.g.
 *   oil pressure without rpm); `min`/`max` are the gauge span; `state` is
 *   `none` for metrics without a band (rpm, speed, power, torque, generator
 *   duty, unknown names) and `neutral` for a banded metric with no usable
 *   value. Results are frozen and reference-stable while unchanged.
 */
export function evaluateBand(name, value, ctx) {
  const c = ctx || {};
  const span = BAND_SPANS[name];
  if (span === undefined) return NONE_UNKNOWN;
  switch (name) {
    case "engine.coolant_temperature":
      return coolantBand(name, value, span);
    case "transmission.oil_temperature":
      return transmissionBand(name, value, span);
    case "engine.vvt_oil_temperature":
      return vvtBand(name, value, span);
    case "engine.oil_pressure":
      return oilPressureBand(name, value, c, span);
    case "battery.voltage":
      return voltageBand(name, value, c, span);
    case "tire.pressure.fl":
    case "tire.pressure.fr":
    case "tire.pressure.rl":
    case "tire.pressure.rr":
      return tireMetricBand(name, value, c, span);
    default:
      return emit(name, "none", null, null, span.min, span.max, null);
  }
}

/**
 * Whether a metric has a coloured band at all (false for rpm, speed, power,
 * torque, generator duty and unknown names).
 * @param {string} name
 * @returns {boolean}
 */
export function hasBand(name) {
  return (
    name === "engine.coolant_temperature" ||
    name === "transmission.oil_temperature" ||
    name === "engine.vvt_oil_temperature" ||
    name === "engine.oil_pressure" ||
    name === "battery.voltage" ||
    isTire(name)
  );
}
