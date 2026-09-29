/**
 * Number, unit and time formatting for the v2 dashboard (design.md section 5.1).
 *
 * Every function here is pure, allocation-light and uses no locale-aware
 * formatting API (those are slow on the old Android tablet and printed en-GB
 * dates in the Pi audits). Times use the browser's local zone through the
 * plain `Date` getters. Negative numbers carry a typographic minus (`MINUS`,
 * U+2212) so they keep tabular width. The module is ES2018 and Node-friendly
 * so it can be unit-tested and reused by non-UI code.
 */

/** Placeholder shown for a value that cannot be formatted. */
export const DASH = "—";

/** Typographic minus used in front of negative numbers (U+2212). */
export const MINUS = "−";

/**
 * Fixed decimals per metric name (design 5.1): speed, rpm, temperatures,
 * pressures, power and torque 0; voltage 2; generator duty 0; radar angles 2.
 * Names not listed fall back to `DEFAULT_DECIMALS`.
 */
export const DECIMALS = Object.freeze({
  "vehicle.speed": 0,
  "engine.rpm": 0,
  "transmission.output_speed": 0,
  "transmission.turbine_speed": 0,
  "engine.coolant_temperature": 0,
  "engine.vvt_oil_temperature": 0,
  "transmission.oil_temperature": 0,
  "engine.oil_pressure": 0,
  "tire.pressure.fl": 0,
  "tire.pressure.fr": 0,
  "tire.pressure.rl": 0,
  "tire.pressure.rr": 0,
  "engine.crankshaft_power": 0,
  "engine.crankshaft_torque": 0,
  "engine.target_crankshaft_torque": 0,
  "generator.field_duty": 0,
  "vehicle.odometer": 0,
  "battery.voltage": 2,
  "radar.alignment.azimuth": 2,
  "radar.alignment.elevation": 2,
});

/** Decimals for a metric that `DECIMALS` does not list. */
export const DEFAULT_DECIMALS = 0;

/** Metrics whose integer part is written with a thousands separator. */
export const THOUSANDS = new Set([
  "engine.rpm",
  "vehicle.odometer",
  "transmission.output_speed",
  "transmission.turbine_speed",
]);

/** Broker unit string -> display unit. Unlisted units display as-is. */
export const UNIT_DISPLAY = Object.freeze({
  mph: "mph",
  rpm: "rpm",
  "°F": "°F",
  degF: "°F",
  F: "°F",
  psi: "psi",
  V: "V",
  hp: "hp",
  "lb-ft": "lb-ft",
  "%": "%",
  mi: "mi",
  deg: "°",
  degrees: "°",
  boolean: "",
  raw_u8: "",
  raw_u16_be: "",
});

/** Fallback display unit by metric name, used when the observation has none. */
export const UNIT_BY_NAME = Object.freeze({
  "vehicle.speed": "mph",
  "engine.rpm": "rpm",
  "transmission.output_speed": "rpm",
  "transmission.turbine_speed": "rpm",
  "engine.coolant_temperature": "°F",
  "engine.vvt_oil_temperature": "°F",
  "transmission.oil_temperature": "°F",
  "engine.oil_pressure": "psi",
  "tire.pressure.fl": "psi",
  "tire.pressure.fr": "psi",
  "tire.pressure.rl": "psi",
  "tire.pressure.rr": "psi",
  "battery.voltage": "V",
  "engine.crankshaft_power": "hp",
  "engine.crankshaft_torque": "lb-ft",
  "engine.target_crankshaft_torque": "lb-ft",
  "generator.field_duty": "%",
  "vehicle.odometer": "mi",
  "radar.alignment.azimuth": "°",
  "radar.alignment.elevation": "°",
});

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const POW10 = [1, 10, 100, 1000, 10000, 100000, 1000000];

/**
 * Decimals used for a metric's value.
 * @param {string} name metric name
 * @returns {number}
 */
export function decimalsFor(name) {
  const d = DECIMALS[name];
  return typeof d === "number" ? d : DEFAULT_DECIMALS;
}

/**
 * Round half away from zero (Math.round rounds -12.5 to -12; this gives -13).
 * @param {number} value
 * @returns {number}
 */
export function roundHalfAway(value) {
  return value < 0 ? -Math.round(-value) : Math.round(value);
}

/**
 * Insert thousands separators into a string of digits.
 * @param {string} digits integer digits only, no sign
 * @returns {string}
 */
export function groupThousands(digits) {
  const len = digits.length;
  if (len <= 3) return digits;
  let out = "";
  let head = len % 3;
  if (head > 0) out = digits.slice(0, head);
  for (let i = head; i < len; i += 3) {
    out += (out.length > 0 ? "," : "") + digits.slice(i, i + 3);
  }
  return out;
}

/**
 * Format a number with a fixed number of decimals, rounding half away from
 * zero. Negative zero is written as `0`.
 * @param {number} value
 * @param {number} decimals 0..6
 * @param {boolean} [separate] insert thousands separators into the integer part
 * @returns {string} the number, or `DASH` for null/NaN/Infinity
 */
export function fmtFixed(value, decimals, separate) {
  if (typeof value !== "number" || !isFinite(value)) return DASH;
  const d = decimals > 0 ? (decimals > 6 ? 6 : Math.floor(decimals)) : 0;
  const scale = POW10[d];
  const scaled = Math.round(Math.abs(value) * scale);
  if (!isFinite(scaled)) return DASH;
  const intPart = Math.floor(scaled / scale);
  let s = String(intPart);
  if (separate) s = groupThousands(s);
  if (d > 0) {
    let frac = String(scaled - intPart * scale);
    while (frac.length < d) frac = "0" + frac;
    s += "." + frac;
  }
  return value < 0 && scaled !== 0 ? MINUS + s : s;
}

/**
 * Format a metric value for display using the per-metric decimals and
 * separator rules from design 5.1. Booleans become `ON`/`OFF`; strings are
 * passed through (trimmed); anything unformattable returns `DASH`.
 * @param {string} name metric name (selects decimals and separator)
 * @param {*} value observation value
 * @returns {string}
 */
export function fmtValue(name, value) {
  if (value === null || value === undefined) return DASH;
  if (typeof value === "boolean") return value ? "ON" : "OFF";
  if (typeof value === "string") {
    const t = value.trim();
    return t.length > 0 ? t : DASH;
  }
  if (typeof value !== "number") return DASH;
  // An odometer never rounds up (53,892.8 reads 53,892, as on the cluster and the Service card).
  if (name === "vehicle.odometer" && isFinite(value)) return fmtFixed(Math.trunc(value), 0, true);
  return fmtFixed(value, decimalsFor(name), THOUSANDS.has(name));
}

/**
 * Display unit for a metric. The broker's unit string is mapped through
 * `UNIT_DISPLAY` (`deg` -> `°`, `boolean`/raw units -> empty); when the
 * observation carries no unit the metric-name fallback is used.
 * @param {string} name metric name
 * @param {string|null|undefined} unit unit as sent by the broker
 * @returns {string} display unit, possibly empty
 */
export function fmtUnit(name, unit) {
  if (typeof unit === "string" && unit.length > 0) {
    const mapped = UNIT_DISPLAY[unit];
    return typeof mapped === "string" ? mapped : unit;
  }
  const byName = UNIT_BY_NAME[name];
  return typeof byName === "string" ? byName : "";
}

/**
 * Coarse age label at minute resolution (design 5.1): `now` under a minute, then `2 min`,
 * `1.4 h`, `2 d`. Minutes are floored; hours are floored to a tenth (a trailing
 * `.0` is dropped); days start at 24 h and are floored. Negative ages read as
 * `now`; non-finite input returns `DASH`.
 * @param {number} ms age in milliseconds
 * @returns {string}
 */
export function fmtAgeCoarse(ms) {
  if (typeof ms !== "number" || !isFinite(ms)) return DASH;
  if (ms < 60000) return "now";
  if (ms < 3600000) return Math.floor(ms / 60000) + " min";
  if (ms < 86400000) {
    const tenths = Math.floor(ms / 360000);
    const whole = Math.floor(tenths / 10);
    const rest = tenths - whole * 10;
    return (rest === 0 ? String(whole) : whole + "." + rest) + " h";
  }
  return Math.floor(ms / 86400000) + " d";
}

/**
 * Normalise an ISO string so that old WebKit/V8 parsers accept it: fractional
 * seconds are trimmed to three digits, a trailing `+00:00` is kept.
 * @param {string} iso
 * @returns {string}
 */
function normaliseIso(iso) {
  const dot = iso.indexOf(".");
  if (dot < 0) return iso;
  let end = dot + 1;
  while (end < iso.length && iso.charCodeAt(end) >= 48 && iso.charCodeAt(end) <= 57) end += 1;
  if (end - dot - 1 <= 3) return iso;
  return iso.slice(0, dot + 4) + iso.slice(end);
}

/**
 * Parse a timestamp into a valid `Date`, or return `null`.
 * Accepts a `Date`, epoch milliseconds, or an ISO-8601 string (microsecond
 * fractions from the broker are accepted).
 * @param {Date|number|string|null|undefined} input
 * @returns {Date|null}
 */
export function parseDate(input) {
  let d = null;
  if (input instanceof Date) d = input;
  else if (typeof input === "number") d = new Date(input);
  else if (typeof input === "string" && input.length > 0) d = new Date(normaliseIso(input));
  if (d === null || isNaN(d.getTime())) return null;
  return d;
}

/**
 * Local clock time, 12-hour with lowercase am/pm: `4:25 pm`, `12:00 am`.
 * @param {Date} d valid date
 * @returns {string}
 */
export function fmtClock(d) {
  const hours = d.getHours();
  const minutes = d.getMinutes();
  const h12 = hours % 12 === 0 ? 12 : hours % 12;
  return h12 + ":" + (minutes < 10 ? "0" : "") + minutes + (hours < 12 ? " am" : " pm");
}

/**
 * Local calendar date, `Sep 22`, with the year appended when it differs from
 * the reference year: `Sep 22, 2025`.
 * @param {Date} d valid date
 * @param {number} [refYear] year that is implied when equal
 * @returns {string}
 */
export function fmtDate(d, refYear) {
  const base = MONTHS[d.getMonth()] + " " + d.getDate();
  const year = d.getFullYear();
  return typeof refYear === "number" && year === refYear ? base : base + ", " + year;
}

function sameLocalDay(a, b) {
  return a.getDate() === b.getDate() && a.getMonth() === b.getMonth() && a.getFullYear() === b.getFullYear();
}

/**
 * Wall-clock display for an observation time: `4:25 pm` when it falls on
 * today's local date, `yesterday 4:25 pm` for yesterday, otherwise
 * `Sep 22, 4:25 pm` (`Sep 22, 2025, 4:25 pm` in another year). Unparseable
 * input returns `DASH`. Wall-clock values are display-only; never derive
 * freshness from them.
 * @param {Date|number|string|null|undefined} isoOrDate
 * @param {Date} [now] reference time (defaults to the current time)
 * @returns {string}
 */
export function fmtTime(isoOrDate, now) {
  const d = parseDate(isoOrDate);
  if (d === null) return DASH;
  const ref = now instanceof Date && !isNaN(now.getTime()) ? now : new Date();
  const clock = fmtClock(d);
  if (sameLocalDay(d, ref)) return clock;
  const yesterday = new Date(ref.getFullYear(), ref.getMonth(), ref.getDate() - 1);
  if (sameLocalDay(d, yesterday)) return "yesterday " + clock;
  return fmtDate(d, ref.getFullYear()) + ", " + clock;
}

/**
 * Duration label: `45 s` under a minute, `32 min` under an hour, then
 * `1 h 05` (hours unbounded, minutes zero-padded). Seconds are floored;
 * negative or non-finite input returns `DASH`.
 * @param {number} seconds
 * @returns {string}
 */
export function fmtDuration(seconds) {
  if (typeof seconds !== "number" || !isFinite(seconds) || seconds < 0) return DASH;
  const s = Math.floor(seconds);
  if (s < 60) return s + " s";
  const minutes = Math.floor(s / 60);
  if (minutes < 60) return minutes + " min";
  const hours = Math.floor(minutes / 60);
  const rest = minutes - hours * 60;
  return hours + " h " + (rest < 10 ? "0" : "") + rest;
}

/**
 * Duration inside a sentence, with every unit written out: `45 s`, `32 min`,
 * `1 h 05 min`, then days from 48 h (`2 d 8 h`). Tiles and tables keep the
 * compact `fmtDuration` (`1 h 05`).
 * @param {number} seconds
 * @returns {string}
 */
export function fmtDurationLong(seconds) {
  const text = fmtDuration(seconds);
  const hours = Math.floor(seconds / 3600);
  // Under an hour, and for input fmtDuration rejects (hours is then NaN or negative), text stands.
  if (!(hours >= 1)) return text;
  return hours < 48 ? text + " min" : Math.floor(hours / 24) + " d " + (hours % 24) + " h";
}

/**
 * Upper-case the first letter, for broker text that starts a sentence.
 * @param {string} text
 * @returns {string}
 */
export function capFirst(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * Integer with thousands separators (`6,303`); rounds half away from zero.
 * @param {number} n
 * @returns {string} or `DASH` when not finite
 */
export function fmtInt(n) {
  return fmtFixed(n, 0, true);
}

/**
 * Signed difference for comparison lines: `+3.4`, `−3.4`, `0`.
 * @param {number} delta
 * @param {number} [decimals] defaults to 1
 * @returns {string} or `DASH` when not finite
 */
export function fmtDelta(delta, decimals) {
  const s = fmtFixed(delta, typeof decimals === "number" ? decimals : 1, false);
  if (s === DASH || s.charCodeAt(0) === 0x2212) return s;
  // A value that rounds to zero at this precision is unsigned ("0.0", not "+0.0").
  for (let i = 0; i < s.length; i += 1) {
    const c = s.charCodeAt(i);
    if (c >= 49 && c <= 57) return "+" + s;
  }
  return s;
}
