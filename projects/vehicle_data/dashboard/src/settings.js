/**
 * Per-device dashboard settings (design section 3 and 5.2): the selected
 * view, automatic view selection, per-view hidden cards and card order, the
 * time format, the night dim toggle and the Drive page. Stored in `localStorage` under `van-telemetry.v2.settings`;
 * never sent to the broker. The former static app's key
 * (`van-telemetry.dashboard.v3`) is migrated once when the v2 key is absent.
 *
 * Storage access is tolerant: a denied, throwing or corrupt store falls back to
 * defaults and the in-memory signal still works for the current page.
 */

import { signal } from "@preact/signals-core";

/** localStorage key for v2 settings. */
export const STORAGE_KEY = "van-telemetry.v2.settings";

/** localStorage key of the former static app's settings (same origin, port 8765), migrated once. */
export const LEGACY_KEY = "van-telemetry.dashboard.v3";

/** Valid view ids in tab order. */
export const VIEWS = ["drive", "parked", "health", "history", "system"];

/** Vehicle states that select Drive when verified and fresh (rule 16). */
const DRIVING_STATES = new Set(["moving", "running", "ignition_on"]);

/** Vehicle states that select Parked when fresh, any confidence (rule 16). */
const PARKED_STATES = new Set(["asleep", "parked"]);

/** Age bound for automatic view selection (rule 16, profiles.js:5). */
export const MAX_AUTOMATIC_STATE_AGE_MS = 5000;

/** Old profile id → v2 view. `auto` is handled separately. */
const LEGACY_VIEWS = {
  driving: "drive",
  parked: "parked",
  overview: "health",
  diagnostics: "system",
  custom: "health",
};

const TIME_FORMATS = new Set(["12h", "24h"]);

/** Drive pages (design 3.1 and 3.1.1): 1 = gauges, 2 = more sensors. */
export const DRIVE_PAGES = [1, 2];

/**
 * Fresh default settings object.
 * @returns {{version:1, view:string, auto:boolean, hidden:object, order:object, timeFormat:string, dim:boolean, drivePage:number}}
 */
export function defaultSettings() {
  return { version: 1, view: "drive", auto: true, hidden: {}, order: {}, timeFormat: "12h", dim: false, drivePage: 1 };
}

/**
 * Coerce any candidate into a valid v2 settings object. Unknown views, card
 * ids that are not strings, duplicate ids and unknown keys are dropped.
 * @param {*} candidate parsed storage value or partial settings
 * @returns {object} normalized settings
 */
export function normalizeSettings(candidate) {
  const out = defaultSettings();
  if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return out;
  if (VIEWS.includes(candidate.view)) out.view = candidate.view;
  if (typeof candidate.auto === "boolean") out.auto = candidate.auto;
  out.hidden = perViewLists(candidate.hidden);
  out.order = perViewLists(candidate.order);
  if (TIME_FORMATS.has(candidate.timeFormat)) out.timeFormat = candidate.timeFormat;
  if (typeof candidate.dim === "boolean") out.dim = candidate.dim;
  if (DRIVE_PAGES.includes(candidate.drivePage)) out.drivePage = candidate.drivePage;
  return out;
}

/**
 * Translate a former static app settings object (`{version:3, selected, ...}`)
 * into v2 settings: `driving`→`drive`, `parked`→`parked`, `overview`→`health`,
 * `diagnostics`→`system`, `auto`→`auto:true` with the default view.
 * @param {*} legacy parsed value of the old key
 * @returns {object} normalized v2 settings
 */
export function migrateLegacy(legacy) {
  const out = defaultSettings();
  if (!legacy || typeof legacy !== "object" || Array.isArray(legacy)) return out;
  const selected = legacy.selected;
  if (selected === "auto") {
    out.auto = true;
  } else if (Object.prototype.hasOwnProperty.call(LEGACY_VIEWS, selected)) {
    out.auto = false;
    out.view = LEGACY_VIEWS[selected];
  }
  return out;
}

/**
 * Load settings from storage: the v2 key first, then a one-time migration of
 * the old key, else defaults. Denied or corrupt storage yields defaults.
 * @param {Storage} [storage] storage to read; defaults to `localStorage`
 * @returns {object} normalized settings
 */
export function loadSettings(storage) {
  const store = storage === undefined ? resolveStorage() : storage;
  if (!store) return defaultSettings();
  const current = readJson(store, STORAGE_KEY);
  if (current.found) return normalizeSettings(current.value);
  const legacy = readJson(store, LEGACY_KEY);
  if (legacy.found && legacy.value !== null) {
    const migrated = migrateLegacy(legacy.value);
    writeJson(store, STORAGE_KEY, migrated);
    return migrated;
  }
  return defaultSettings();
}

/**
 * Normalize, publish to the `settings` signal and persist. A denied write is
 * ignored; the in-memory value still applies.
 * @param {object} next settings (may be partial)
 * @param {Storage} [storage] storage to write; defaults to `localStorage`
 * @returns {object} the normalized settings that were published
 */
export function saveSettings(next, storage) {
  const normalized = normalizeSettings(next);
  settings.value = normalized;
  const store = storage === undefined ? resolveStorage() : storage;
  if (store) writeJson(store, STORAGE_KEY, normalized);
  return normalized;
}

/** The live settings signal; initialized from storage at module load. */
export const settings = signal(loadSettings());

/**
 * Design 5.2 / rule 16 automatic view selection.
 * Drive when `engine.rpm` is live and driver-qualified, or the vehicle state
 * is verified, fresh (≤ 5 s) and moving/running/ignition_on; Parked when the
 * state is asleep/parked and fresh (any confidence); otherwise `current`.
 * @param {{rpmLive:boolean, vehicle:{state:string, confidence:string, ageMs:number|null}, current:string}} input
 * @returns {'drive'|'parked'|string}
 */
export function autoView(input) {
  const rpmLive = Boolean(input && input.rpmLive);
  const v = input && input.vehicle ? input.vehicle : {};
  const current = input && VIEWS.includes(input.current) ? input.current : "drive";
  const fresh =
    typeof v.ageMs === "number" &&
    Number.isFinite(v.ageMs) &&
    v.ageMs >= 0 &&
    v.ageMs <= MAX_AUTOMATIC_STATE_AGE_MS;
  if (rpmLive) return "drive";
  if (v.confidence === "verified" && fresh && DRIVING_STATES.has(v.state)) return "drive";
  if (fresh && PARKED_STATES.has(v.state)) return "parked";
  return current;
}

// ---------------------------------------------------------------------------
// internals

function perViewLists(candidate) {
  const out = {};
  if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return out;
  for (let i = 0; i < VIEWS.length; i += 1) {
    const view = VIEWS[i];
    const list = candidate[view];
    if (!Array.isArray(list)) continue;
    const seen = new Set();
    const ids = [];
    for (let j = 0; j < list.length; j += 1) {
      const id = list[j];
      if (typeof id !== "string" || id === "" || seen.has(id)) continue;
      seen.add(id);
      ids.push(id);
    }
    if (ids.length) out[view] = ids;
  }
  return out;
}

/**
 * `localStorage` when reachable; accessing it can throw when denied. A bare
 * identifier (not `globalThis`, an ES2020 global missing before Chrome 71).
 */
function resolveStorage() {
  try {
    const store = typeof localStorage === "undefined" ? null : localStorage;
    return store && typeof store.getItem === "function" ? store : null;
  } catch (_error) {
    return null;
  }
}

function readJson(store, key) {
  try {
    const raw = store.getItem(key);
    if (raw === null || raw === undefined) return { found: false, value: null };
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { found: true, value: null };
    }
    return { found: true, value: parsed };
  } catch (_error) {
    return { found: false, value: null };
  }
}

function writeJson(store, key, value) {
  try {
    store.setItem(key, JSON.stringify(value));
  } catch (_error) {
    // Private/restricted browsers can deny writes; keep the in-memory value.
  }
}
