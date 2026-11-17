/**
 * Ignored diagnostic codes: the owner's list of codes the Codes card and the Parked summary leave
 * out (for example the lamp codes his LED headlights cause). The list is kept on the Pi
 * (projects/vehicle_data/dtc_ignore.py) and arrives as the summary's `dtc_ignores` slice; ignoring
 * is display bookkeeping and never clears a code. This module is the pure part: the slice reader,
 * the request bodies, the wording and the rule that takes ignored records out of a `dtcs` slice.
 * Node-testable: no Preact, no store, no DOM.
 */

import vocabulary from "../../dtc_ignore.json" with { type: "json" };

export const DTC_IGNORE = Object.freeze(vocabulary);

const REQUEST_ID_PATTERN = /^[A-Za-z0-9_-]{12,80}$/;

const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : null);
const str = (v) => (typeof v === "string" && v.length > 0 ? v : null);

/** One code's identity across the three features: the module key and the code the card shows. */
export function identity(moduleKey, code) {
  return (str(moduleKey) || "") + "\u001f" + (str(code) || "");
}

/** Identity of a saved record, or null for one with no module key or no FCA display code. */
export function recordIdentity(record) {
  const r = obj(record);
  return r && str(r.module_key) && str(r.fca_display) ? identity(r.module_key, r.fca_display) : null;
}

const EMPTY_STATE = Object.freeze({
  available: false, persistent: false, storageError: null, limit: DTC_IGNORE.max_ignored, entries: [], index: new Map(),
});

/** Whether a reply body is the list (the web listener's own refusals are not). */
export function isState(payload) {
  const p = obj(payload);
  return Boolean(p && p.available === true && Array.isArray(p.ignored));
}

/**
 * The `dtc_ignores` slice as the card reads it. A missing slice, `available: false` or a malformed
 * body means the controls and the Ignored group stay hidden.
 * @returns {{available: boolean, persistent: boolean, storageError: string|null, limit: number,
 *   entries: Array<{moduleKey: string, code: string, note: string, ignoredAt: string|null}>,
 *   index: Map<string, object>}}
 */
export function ignoreState(slice) {
  if (!isState(slice)) {
    // An unreadable list keeps its reason, so the card can say why ignored codes are showing again.
    const reason = obj(slice) ? str(slice.storage_error) : null;
    return reason ? { ...EMPTY_STATE, storageError: reason } : EMPTY_STATE;
  }
  const entries = [];
  const index = new Map();
  for (const item of slice.ignored) {
    const e = obj(item);
    if (!e || !str(e.module_key) || !str(e.fca_display)) continue;
    const entry = {
      moduleKey: e.module_key,
      code: e.fca_display,
      note: typeof e.note === "string" ? e.note : "",
      ignoredAt: str(e.ignored_at),
    };
    const id = identity(entry.moduleKey, entry.code);
    if (index.has(id)) continue;
    index.set(id, entry);
    entries.push(entry);
  }
  return {
    available: true,
    persistent: slice.persistent === true,
    storageError: str(slice.storage_error),
    limit: typeof slice.limit === "number" ? slice.limit : DTC_IGNORE.max_ignored,
    entries,
    index,
  };
}

/**
 * `dtcs` without the ignored records. Group lists lose them and the group counts drop by the number
 * removed from the returned list; records the Pi did not return stay counted (their identity is not
 * known). `found` holds each removed record by identity so the Ignored group can show its meaning.
 * @param {object|null} dtcs a `summary.dtcs` slice or a merged model of it
 * @param {Map<string, object>} index `ignoreState().index`
 * @returns {{dtcs: object|null, found: Map<string, object>}}
 */
export function excludeIgnored(dtcs, index) {
  const found = new Map();
  const d = obj(dtcs);
  const groups = d ? obj(d.groups) : null;
  if (!d || !groups || !index || index.size === 0) return { dtcs, found };
  const nextGroups = {};
  const counts = { ...(obj(d.group_counts) || {}) };
  const returned = { ...(obj(d.group_returned_counts) || {}) };
  let removed = 0;
  for (const key of Object.keys(groups)) {
    const list = Array.isArray(groups[key]) ? groups[key] : [];
    const kept = [];
    let dropped = 0;
    for (const record of list) {
      const id = recordIdentity(record);
      if (id !== null && index.has(id)) {
        dropped += 1;
        if (!found.has(id)) found.set(id, record);
      } else kept.push(record);
    }
    nextGroups[key] = kept;
    removed += dropped;
    if (dropped > 0) {
      if (typeof counts[key] === "number") counts[key] = Math.max(0, counts[key] - dropped);
      if (typeof returned[key] === "number") returned[key] = Math.max(0, returned[key] - dropped);
    }
  }
  if (removed === 0) return { dtcs, found };
  return { dtcs: { ...d, groups: nextGroups, group_counts: counts, group_returned_counts: returned }, found };
}

/** A request id the Pi accepts (the oil-change record's rule): `dtc_` plus 32 hex digits. */
export function makeRequestId(getRandomValues) {
  const words = new Uint32Array(4);
  const fill = typeof getRandomValues === "function" ? getRandomValues
    : typeof globalThis.crypto?.getRandomValues === "function" ? (a) => globalThis.crypto.getRandomValues(a)
      : (a) => { for (let i = 0; i < a.length; i += 1) a[i] = Math.floor(Math.random() * 0x100000000); return a; };
  fill(words);
  return "dtc_" + Array.from(words, (w) => (w >>> 0).toString(16).padStart(8, "0")).join("");
}

export function isValidRequestId(id) {
  return typeof id === "string" && REQUEST_ID_PATTERN.test(id);
}

/** Request body (without `request_id`) to ignore a code; the note is trimmed. */
export function ignoreBody(moduleKey, code, note) {
  return { action: DTC_IGNORE.actions.ignore, module_key: moduleKey, fca_display: code, note: String(note || "").trim() };
}

/** Request body (without `request_id`) to restore an ignored code. */
export function restoreBody(moduleKey, code) {
  return { action: DTC_IGNORE.actions.restore, module_key: moduleKey, fca_display: code };
}

/** Why a note cannot be sent, or null. The Pi refuses a longer one. */
export function noteProblem(note) {
  const text = String(note || "").trim();
  const n = text.length;
  if (n > DTC_IGNORE.note_max_chars) return "Keep the note to " + DTC_IGNORE.note_max_chars + " characters (now " + n + ").";
  // Emoji and other wide characters count more than one byte each toward the Pi's bound.
  const bytes = new TextEncoder().encode(text).length;
  return bytes > DTC_IGNORE.note_max_bytes ? "That note is too long once saved (" + bytes + " of " + DTC_IGNORE.note_max_bytes + " bytes); shorten it." : null;
}

/**
 * The body to send: the previous pending one when the content is unchanged, so a retry after a
 * lost answer reuses its `request_id` and the Pi applies it once; otherwise a new one.
 * @returns {{signature: string, payload: object}}
 */
export function nextPending(pending, body, makeId = makeRequestId) {
  const signature = JSON.stringify(body);
  if (pending && pending.signature === signature && isValidRequestId(pending.payload?.request_id)) return pending;
  return { signature, payload: { ...body, request_id: makeId() } };
}

/**
 * What a POST reply means: the new list, or the text to show in the panel. A refusal carries the
 * Pi's own `detail`; anything else reads as the status.
 * @returns {{kind: "ok", state: object}|{kind: "error", message: string}}
 */
export function classifyReply(status, ok, payload) {
  if (ok && isState(payload)) return { kind: "ok", state: payload };
  const p = obj(payload);
  const detail = p && typeof p.detail === "string" && p.detail ? p.detail : "";
  return { kind: "error", message: detail || (ok ? "The Pi sent an unreadable answer." : "The Pi refused the change (HTTP " + status + ").") };
}

/** Text for a request that never got an answer. */
export const NETWORK_ERROR_TEXT =
  "No answer from the Pi, so the outcome is unknown: the change may or may not have been saved. The list shows the truth at its next refresh.";

/** The line the Codes card shows when the Pi could not read the ignored-codes list, else null. */
export function ignoreUnavailableText(state) {
  return state && !state.available && state.storageError
    ? "The ignored-codes list could not be read on the Pi, so ignored codes are shown again."
    : null;
}

/** `Ignored Oct 28, 2026, 2:30 PM MDT`, or `Ignored` when the date is missing. */
export function ignoredLabel(entry, formatTime) {
  const at = entry && entry.ignoredAt ? formatTime(entry.ignoredAt) : null;
  return at && at !== "unknown time" ? "Ignored " + at : "Ignored";
}
