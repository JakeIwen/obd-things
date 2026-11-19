import { test } from "node:test";
import assert from "node:assert/strict";

import { CARD_TEXT, CODE_TEXT, CREDITS, REASON_TEXT, cardVisible, codeText, creditsModel, reasonText } from "../src/apiCredits.helpers.js";
import { createCreditsPoller } from "../src/apiCredits.js";
import { createStorage } from "../src/assistantAccess.js";
import { demoCreditsView } from "../tools/demo_assistant.mjs";
import contract from "../../api_credits.json" with { type: "json" };

const NOW = Date.parse("2026-11-20T18:00:00Z");
const view = (over = {}) => ({
  key_installed: true, enabled: true, until: contract.until, expired: false,
  new_research: { billing: "api_credits", reason: null, latched_models: [] },
  balance: { state: "known", total_usd: 14.5, as_of: "2026-11-20T17:56:00Z", stale: false,
    grants: [{ label: "Nov 25", expires: "2026-11-25T00:00:00Z", remaining_usd: 6.4 }, { label: "Dec 8", expires: "2026-12-08T00:00:00Z", remaining_usd: 8.1 }] },
  ledger_available: true, spent_since_snapshot_usd: 0, uncertain_count: 0, unknown_spend_count: 0, last_fallback: null, ...over,
});
const model = (v, hasAccessCode = true, nowMs = NOW) => creditsModel(v, { hasAccessCode, nowMs });

test("every billing reason and credit code in the contract has text, and nothing extra", () => {
  assert.deepEqual(Object.keys(REASON_TEXT).sort(), [...contract.billingReasons].sort());
  assert.deepEqual(Object.keys(CODE_TEXT).sort(), Object.keys(contract.creditCodes).sort());
  for (const r of contract.billingReasons) assert.ok(reasonText(r).length > 3 && reasonText(r) !== r);
  for (const c of Object.keys(contract.creditCodes)) assert.ok(codeText(c).length > 3 && codeText(c) !== c);
  assert.equal(CREDITS.pollMs, contract.card.pollMs);
});

test("a known balance shows the total, each grant's expiry and the as-of time", () => {
  const m = model(view());
  assert.equal(m.kind, "active");
  assert.equal(m.balance.total, "$14.50");
  assert.deepEqual(m.balance.grants.map((g) => [g.expires, g.amount]), [["expires Nov 25", "$6.40"], ["expires Dec 8", "$8.10"]]);
  assert.match(m.balance.asOf, /^as of /);
  assert.equal(m.balance.stale, false);
  assert.equal(m.billing, CARD_TEXT.billsCredits);
  assert.equal(m.followUps, CARD_TEXT.followUps);
});

test("an unknown balance reads Balance unknown with no amounts", () => {
  const m = model(view({ balance: { state: "unknown", total_usd: null, as_of: null, stale: false, grants: [] } }));
  assert.equal(m.balance.total, "Balance unknown");
  assert.equal(m.balance.asOf, null);
  assert.deepEqual(m.balance.grants, []);
});

test("a stale snapshot is marked", () => {
  const stale = view(); stale.balance.stale = true;
  assert.equal(model(stale).balance.stale, true);
});

test("after expiry, by flag or by the clock, the card is one line", () => {
  const byFlag = model(view({ expired: true }));
  assert.deepEqual(byFlag, { kind: "expired", text: CARD_TEXT.expired });
  assert.equal(CARD_TEXT.expired, "API credits expired · research uses the subscription");
  assert.deepEqual(model(view(), true, Date.parse(contract.until)), byFlag);
  assert.equal(model(view(), true, Date.parse(contract.until) - 1).kind, "active");
});

test("uncertain and unknown spend counts appear only when non-zero, each with an info text", () => {
  assert.deepEqual(model(view()).qualifiers, []);
  const q = model(view({ uncertain_count: 2, unknown_spend_count: 1 })).qualifiers;
  assert.deepEqual(q.map((x) => x.text), ["2 uncertain", "1 unknown spend"]);
  assert.ok(q.every((x) => x.info.length > 10));
});

test("uncertain spend is described as subtracted in full, so the balance may read low", () => {
  const info = model(view({ uncertain_count: 1 })).qualifiers[0].info;
  assert.match(info, /subtracted from the balance in full/);
  assert.match(info, /lower than the real remainder/);
  assert.doesNotMatch(info, /not subtracted/);
});

test("an unavailable spend ledger says so, and an available one adds nothing", () => {
  const unavailable = model(view({ ledger_available: false, balance: { state: "unknown", total_usd: null, as_of: null, stale: false, grants: [] } }));
  assert.equal(unavailable.balance.total, CARD_TEXT.unknown);
  assert.deepEqual(unavailable.qualifiers.map((x) => x.text), [CARD_TEXT.ledgerUnavailable]);
  assert.ok(unavailable.qualifiers[0].info.length > 10);
  assert.deepEqual(model(view()).qualifiers, []);
});

test("subscription billing names its reason in plain words; latched models are listed", () => {
  const m = model(view({ new_research: { billing: "subscription", reason: "no_key", latched_models: ["opus", "sonnet"] } }));
  assert.equal(m.billing, "New research bills: Subscription (" + REASON_TEXT.no_key + ")");
  assert.deepEqual(m.latchedModels, ["opus", "sonnet"]);
  assert.equal(model(view({ new_research: { billing: "subscription", reason: null, latched_models: [] } })).billing, "New research bills: Subscription");
});

test("the last fallback shows its time and the code in plain words", () => {
  const m = model(view({ last_fallback: { at: "2026-11-20T17:00:00Z", code: "exhausted", model: "opus" } }));
  assert.ok(m.lastFallback.includes(CODE_TEXT.exhausted) && m.lastFallback.includes("opus"));
  assert.equal(model(view()).lastFallback, null);
});

test("the card is hidden unless the browser is paired and the key is installed", () => {
  assert.equal(model(view(), false), null);
  assert.equal(model(view({ key_installed: false })), null);
  assert.equal(model(null), null);
  assert.equal(cardVisible(view(), true), true);
});

test("the demo view's total is the sum of its grants and shows credits billing", () => {
  const v = demoCreditsView(NOW);
  assert.equal(v.balance.total_usd, Math.round(v.balance.grants.reduce((s, g) => s + g.remaining_usd, 0) * 100) / 100);
  const m = model(v);
  assert.equal(m.kind, "active");
  assert.equal(m.billing, CARD_TEXT.billsCredits);
});

/* ---------- poller ---------- */

function memory(values) {
  const map = new Map(Object.entries(values));
  return createStorage({ getItem: (k) => map.get(k) ?? null, setItem: (k, v) => map.set(k, v), removeItem: (k) => map.delete(k) });
}
const CODE = "q".repeat(43);
function rig({ status = 200, body = view(), storage = memory({ "van-warning-chat.access": CODE }) } = {}) {
  const queue = []; let seq = 0;
  const calls = []; const states = [];
  const poller = createCreditsPoller({
    storage, onChange: (s) => states.push(s),
    fetch: async (url, init) => { calls.push({ url, init }); if (status === "throw") throw new Error("net"); return { ok: status < 300, status, json: async () => body }; },
    setTimeout: (fn, ms) => { queue.push({ id: ++seq, fn, ms }); return seq; },
    clearTimeout: (id) => { const i = queue.findIndex((t) => t.id === id); if (i >= 0) queue.splice(i, 1); },
  });
  const flush = () => new Promise((r) => setImmediate(r));
  return { poller, calls, states, queue, flush };
}

test("the poller fetches with the access code, then again every pollMs", async () => {
  const r = rig();
  r.poller.start(); await r.flush();
  assert.equal(r.calls[0].url, contract.routes.credits);
  assert.equal(r.calls[0].init.headers["X-Van-Assistant-Key"], CODE);
  assert.equal(r.states.at(-1).view.key_installed, true);
  assert.deepEqual(r.queue.map((t) => t.ms), [contract.card.pollMs]);
  r.queue.shift().fn(); await r.flush();
  assert.equal(r.calls.length, 2);
});

test("the poller stops polling when stopped (hidden or unmounted)", async () => {
  const r = rig();
  r.poller.start(); await r.flush();
  r.poller.stop();
  assert.equal(r.queue.length, 0);
  r.poller.start(); r.poller.stop(); await r.flush();
  assert.equal(r.queue.length, 0, "a fetch in flight when stopped schedules nothing");
});

test("refusals and network errors leave the card hidden, without an error", async () => {
  for (const status of [401, 403, 404, 503, "throw"]) {
    const r = rig({ status });
    r.poller.start(); await r.flush();
    assert.equal(r.states.at(-1).view, null, String(status));
    r.poller.stop();
  }
});

test("without an access code nothing is fetched", async () => {
  const r = rig({ storage: memory({}) });
  r.poller.start(); await r.flush();
  assert.equal(r.calls.length, 0);
  assert.deepEqual(r.states.at(-1), { view: null, hasAccessCode: false });
  r.poller.stop();
});
