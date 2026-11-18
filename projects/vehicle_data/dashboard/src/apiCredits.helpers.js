/**
 * The "Claude API credits" System card, the pure part: the contract constants, the plain-words text
 * for every billing reason and credit code, and the display model built from the advisor's view
 * (GET /v1/assistant/research/credits). Node-testable: no Preact, no store, no DOM.
 */

import { fmtTime, parseDate } from "./format.js";
import contract from "../../api_credits.json" with { type: "json" };

export const CREDITS = Object.freeze({
  route: contract.routes.credits,
  pollMs: contract.card.pollMs,
  billing: Object.freeze({ ...contract.billing }),
  balanceStates: Object.freeze({ ...contract.balanceStates }),
});

/* Keyed by the contract's names: a name added there without text here fails the helpers test. */
export const REASON_TEXT = Object.freeze({
  legacy: "the research service is an older version",
  forced: "this follow-up must use the subscription",
  ineligible: "this session cannot use API credits",
  disabled: "API credits are switched off",
  no_key: "no API key is installed",
  expired: "the credits have expired",
  unavailable: "the credit service did not answer",
  ledger_unavailable: "the spend record cannot be read",
  ledger_full: "the spend record is full",
});

export const CODE_TEXT = Object.freeze({
  exhausted: "credits ran out",
  invalid_key: "the API key was rejected",
  key_ignored: "the CLI ignored the API key",
  model_unavailable: "the model is not available on credits",
});

export const CARD_TEXT = Object.freeze({
  title: "Claude API credits",
  expired: "API credits expired · research uses the subscription",
  unknown: "Balance unknown",
  stale: "stale",
  billsCredits: "New research bills: API credits",
  billsSubscription: (reason) => "New research bills: Subscription" + (reason ? " (" + reason + ")" : ""),
  followUps: "Follow-ups keep their run's billing.",
  latched: "Switched to subscription",
  lastFallback: "Last fallback",
  uncertain: "uncertain",
  unknownSpend: "unknown spend",
  uncertainInfo: "Research runs whose cost could not be confirmed. Their spend is not subtracted from the balance until the next balance update.",
  unknownSpendInfo: "Runs whose cost the CLI did not report, so the balance may be higher than the account's real remainder.",
  infoLabel: "What does this mean?",
});

export const money = (usd) => "$" + Number(usd).toFixed(2);

const isObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const count = (v) => (Number.isInteger(v) && v > 0 ? v : 0);

/** Plain words for a `billingReasons` item (an unlisted one is shown as sent). */
export const reasonText = (reason) => (Object.hasOwn(REASON_TEXT, reason) ? REASON_TEXT[reason] : String(reason));
export const codeText = (code) => (Object.hasOwn(CODE_TEXT, code) ? CODE_TEXT[code] : String(code));

/** Whether the credits window is over, by the view's flag or by the clock passing `until`. */
export function isExpired(view, nowMs) {
  if (view.expired === true) return true;
  const until = parseDate(view.until);
  return until !== null && nowMs >= until.getTime();
}

/** The card is for a paired browser, and only once the Pi has a key installed. */
export function cardVisible(view, hasAccessCode) {
  return Boolean(hasAccessCode) && isObject(view) && view.key_installed === true;
}

function fallbackLine(fallback, now) {
  if (!isObject(fallback)) return null;
  const at = parseDate(fallback.at);
  const when = at ? fmtTime(at, now) + " · " : "";
  return when + codeText(fallback.code) + (fallback.model ? " (" + fallback.model + ")" : "");
}

function balanceModel(balance, now) {
  const b = isObject(balance) ? balance : {};
  const known = b.state === CREDITS.balanceStates.known && Number.isFinite(b.total_usd);
  const asOf = parseDate(b.as_of);
  return {
    known,
    total: known ? money(b.total_usd) : CARD_TEXT.unknown,
    asOf: known && asOf ? "as of " + fmtTime(asOf, now) : null,
    stale: known && b.stale === true,
    grants: known && Array.isArray(b.grants)
      ? b.grants.filter(isObject).map((g) => ({ label: String(g.label), expires: "expires " + g.label, amount: money(g.remaining_usd) }))
      : [],
  };
}

function qualifiers(view) {
  const out = [];
  const uncertain = count(view.uncertain_count);
  const unknownSpend = count(view.unknown_spend_count);
  if (uncertain) out.push({ text: uncertain + " " + CARD_TEXT.uncertain, info: CARD_TEXT.uncertainInfo });
  if (unknownSpend) out.push({ text: unknownSpend + " " + CARD_TEXT.unknownSpend, info: CARD_TEXT.unknownSpendInfo });
  return out;
}

/**
 * @param {object|null} view the advisor's credits view
 * @param {{hasAccessCode: boolean, nowMs: number}} ctx
 * @returns {null | {kind: "expired", text: string} | {kind: "active", ...}} null hides the card
 */
export function creditsModel(view, { hasAccessCode, nowMs }) {
  if (!cardVisible(view, hasAccessCode)) return null;
  if (isExpired(view, nowMs)) return { kind: "expired", text: CARD_TEXT.expired };
  const now = new Date(nowMs);
  const fresh = isObject(view.new_research) ? view.new_research : {};
  const credits = fresh.billing === CREDITS.billing.apiCredits;
  return {
    kind: "active",
    balance: balanceModel(view.balance, now),
    qualifiers: qualifiers(view),
    billing: credits ? CARD_TEXT.billsCredits : CARD_TEXT.billsSubscription(fresh.reason ? reasonText(fresh.reason) : null),
    followUps: CARD_TEXT.followUps,
    latchedModels: Array.isArray(fresh.latched_models) ? fresh.latched_models.map(String) : [],
    lastFallback: fallbackLine(view.last_fallback, now),
  };
}
