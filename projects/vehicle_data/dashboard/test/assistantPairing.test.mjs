import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ACCESS_TEXT, PAIRING, countdownText, createStorage, isValidPin, normalizePin, pairingStatusView, pinDisplay,
  redeemOutcome, redeemRequest, storedIdentity,
} from "../src/assistantAccess.js";
import { createPairingPanel, createPinEntry, redeemPin } from "../src/assistantPairing.js";

// Made-up values only.
const CODE = "q".repeat(43);
const PIN_ID = "0123456789abcdef";

function memory(initial = {}) {
  const map = new Map(Object.entries(initial));
  return { getItem: (k) => (map.has(k) ? map.get(k) : null), setItem: (k, v) => map.set(k, String(v)), removeItem: (k) => map.delete(k), map };
}

/** A scripted Pi: handlers by "METHOD path", each returning [status, body]. */
function pi(handlers) {
  const log = [];
  const fetch = async (url, init) => {
    const body = init.body ? JSON.parse(init.body) : null;
    log.push({ method: init.method, url, body, headers: init.headers });
    const handler = handlers[init.method + " " + url];
    if (!handler) return { status: 404, ok: false, json: async () => ({ available: false }) };
    const [status, reply] = await handler(body, log.length);
    return { status, ok: status < 300, json: async () => reply };
  };
  return { fetch, log };
}

function fakeTimers() {
  const queue = [];
  let seq = 0;
  return {
    setTimeout: (fn, ms) => { const id = ++seq; queue.push({ id, fn, ms }); return id; },
    clearTimeout: (id) => { const at = queue.findIndex((t) => t.id === id); if (at >= 0) queue.splice(at, 1); },
    pending: (ms) => queue.filter((t) => ms === undefined || t.ms === ms),
    async fire(ms) {
      const due = queue.filter((t) => t.ms === ms);
      for (const t of due) { this.clearTimeout(t.id); t.fn(); }
      await settle();
      return due.length;
    },
  };
}
const settle = async () => { for (let i = 0; i < 6; i += 1) await new Promise((r) => setImmediate(r)); };

const OWNER = PAIRING.routes.owner;
const created = (pin = "042917", pinId = PIN_ID) => [200, { available: true, pin, pin_id: pinId, expires_in_s: PAIRING.pinLifetimeSeconds }];
const status = (state, pinId = PIN_ID, extra = {}) => [200, { available: true, pin_id: pinId, state, expires_in_s: 0, locked_for_s: 0, ...extra }];

test("PIN text helpers", () => {
  assert.equal(normalizePin(" 042 917 "), "042917");
  assert.equal(isValidPin("042917"), true);
  for (const bad of ["42917", "0429170", "04291a", "٠١٢٣٤٥", "", null, undefined]) assert.equal(isValidPin(bad), false, String(bad));
  assert.equal(pinDisplay("042917"), "042 917");
  assert.equal(countdownText(300000), "5:00");
  assert.equal(countdownText(299001), "5:00");
  assert.equal(countdownText(299000), "4:59");
  assert.equal(countdownText(61000), "1:01");
  assert.equal(countdownText(-5), "0:00");
  assert.match(ACCESS_TEXT.anotherHint, /within 5 minutes/);
});

test("the redeem request carries the PIN in its body only and no assistant headers", () => {
  const { url, init } = redeemRequest("042917");
  assert.equal(url, "/v1/assistant/pairing/redeem");
  assert.equal(url, PAIRING.routes.redeem);
  assert.deepEqual(JSON.parse(init.body), { pin: "042917" });
  assert.equal(init.method, "POST");
  assert.equal(init.cache, "no-store");
  assert.deepEqual(Object.keys(init.headers), ["Content-Type"]);
  assert.ok(!url.includes("042917"));
});

test("redeem outcomes", async () => {
  assert.deepEqual(redeemOutcome(200, { available: true, access_code: CODE }), { code: CODE });
  assert.deepEqual(redeemOutcome(200, { available: true, access_code: "short" }), { message: ACCESS_TEXT.pinUnavailable });
  assert.deepEqual(redeemOutcome(200, null), { message: ACCESS_TEXT.pinUnavailable });
  assert.deepEqual(redeemOutcome(403, { reason: "pin_rejected" }), { message: ACCESS_TEXT.pinRejected });
  assert.deepEqual(redeemOutcome(429, { retry_after_s: 61 }), { message: ACCESS_TEXT.pinLocked(2) });
  assert.match(redeemOutcome(429, { retry_after_s: 1 }).message, /in 1 min/);
  assert.deepEqual(redeemOutcome(400, {}), { message: ACCESS_TEXT.pinIncomplete });
  assert.deepEqual(redeemOutcome(503, {}), { message: ACCESS_TEXT.pinUnavailable });
  assert.deepEqual(redeemOutcome(0, null), { message: ACCESS_TEXT.pinUnavailable });
  const failing = async () => { throw new TypeError("network"); };
  assert.deepEqual(await redeemPin(failing, "042917"), { message: ACCESS_TEXT.pinUnavailable });
});

test("status views for each state and a replaced PIN", () => {
  assert.deepEqual(pairingStatusView(status("active")[1], PIN_ID), { done: false, text: "", error: false });
  assert.equal(pairingStatusView(status("redeemed")[1], PIN_ID).text, ACCESS_TEXT.anotherPaired);
  assert.equal(pairingStatusView(status("expired")[1], PIN_ID).text, ACCESS_TEXT.anotherExpired);
  assert.deepEqual(pairingStatusView(status("wrong_tries")[1], PIN_ID), { done: true, text: ACCESS_TEXT.anotherWrongTries, error: true });
  assert.equal(pairingStatusView(status("cancelled")[1], PIN_ID).text, ACCESS_TEXT.anotherCancelled);
  assert.equal(pairingStatusView(status("active", "f".repeat(16))[1], PIN_ID).text, ACCESS_TEXT.anotherReplaced);
  assert.equal(pairingStatusView(status("none", null)[1], PIN_ID).text, ACCESS_TEXT.anotherEnded);
  assert.equal(pairingStatusView(status("active", PIN_ID, { locked_for_s: 300 })[1], PIN_ID).text, ACCESS_TEXT.anotherLocked(5));
  // This PIN's own ending wins over a later lock.
  assert.equal(pairingStatusView(status("redeemed", PIN_ID, { locked_for_s: 300 })[1], PIN_ID).text, ACCESS_TEXT.anotherPaired);
  for (const state of Object.values(PAIRING.states)) assert.equal(typeof pairingStatusView(status(state)[1], PIN_ID).done, "boolean");
});

test("storedIdentity reads the stored code and client id", () => {
  const storage = createStorage(memory({ "van-warning-chat.access": CODE, "van-warning-chat.client": "ab".repeat(32) }));
  assert.deepEqual(storedIdentity(storage), { key: CODE, client: "ab".repeat(32) });
});

test("PIN entry hands the received code to the dialog and keeps nothing itself", async () => {
  const server = pi({ ["POST " + PAIRING.routes.redeem]: () => [200, { available: true, access_code: CODE }] });
  const codes = [];
  const entry = createPinEntry({ fetch: server.fetch, onCode: (code) => codes.push(code) });
  assert.equal(await entry.submit("12"), false);
  assert.equal(entry.snapshot().message, ACCESS_TEXT.pinIncomplete);
  assert.equal(server.log.length, 0);
  assert.equal(await entry.submit("042 917"), true);
  assert.deepEqual(codes, [CODE]);
  assert.deepEqual(server.log[0].body, { pin: "042917" });
  assert.equal(server.log[0].headers["X-Van-Assistant-Key"], undefined);
  assert.deepEqual(entry.snapshot(), { busy: false, message: "", error: false });
  const rejecting = pi({ ["POST " + PAIRING.routes.redeem]: () => [403, { available: false, reason: "pin_rejected" }] });
  const other = createPinEntry({ fetch: rejecting.fetch, onCode: (code) => codes.push(code) });
  assert.equal(await other.submit("000000"), false);
  assert.deepEqual(other.snapshot(), { busy: false, message: ACCESS_TEXT.pinRejected, error: true });
  assert.equal(codes.length, 1);
});

function panelSetup(handlers) {
  const server = pi(handlers);
  const timers = fakeTimers();
  const clock = { now: 1000 };
  const storage = createStorage(memory({ "van-warning-chat.access": CODE, "van-warning-chat.client": "ab".repeat(32) }));
  const panel = createPairingPanel({ fetch: server.fetch, storage, now: () => clock.now,
    setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout });
  return { server, timers, clock, panel };
}

test("panel: create, countdown, poll until redeemed", async () => {
  let state = "active";
  const t = panelSetup({ ["POST " + OWNER]: () => created(), ["GET " + OWNER]: () => status(state) });
  t.panel.open();
  assert.equal(t.panel.snapshot().phase, "creating");
  await settle();
  let snap = t.panel.snapshot();
  assert.deepEqual([snap.phase, snap.pin, snap.countdown, snap.showPin], ["active", "042 917", "5:00", true]);
  assert.deepEqual(t.server.log[0].body, { action: "create" });
  assert.equal(t.server.log[0].headers["X-Van-Assistant-Key"], CODE);
  t.clock.now += 1000;
  await t.timers.fire(1000);
  assert.equal(t.panel.snapshot().countdown, "4:59");
  t.clock.now += 1000;
  await t.timers.fire(PAIRING.statusPollSeconds * 1000);
  assert.equal(t.server.log.at(-1).method, "GET");
  assert.equal(t.panel.snapshot().phase, "active");
  state = "redeemed";
  await t.timers.fire(PAIRING.statusPollSeconds * 1000);
  snap = t.panel.snapshot();
  assert.deepEqual([snap.phase, snap.message, snap.showPin, snap.pin], ["ended", ACCESS_TEXT.anotherPaired, false, ""]);
  assert.equal(t.timers.pending().length, 0, "polling and the countdown stop");
  t.panel.close();
  assert.equal(t.server.log.filter((r) => r.body && r.body.action === "cancel").length, 0, "a redeemed PIN is not cancelled");
});

test("panel: closing cancels the active PIN; no poll after expiry", async () => {
  const t = panelSetup({ ["POST " + OWNER]: (body) => (body.action === "create" ? created() : status("cancelled")),
    ["GET " + OWNER]: () => status("active") });
  t.panel.open();
  await settle();
  t.panel.close();
  await settle();
  assert.deepEqual(t.server.log.at(-1).body, { action: "cancel", pin_id: PIN_ID });
  assert.equal(t.timers.pending().length, 0);
  assert.equal(t.panel.snapshot().phase, "idle");

  t.panel.open();
  await settle();
  t.clock.now += PAIRING.pinLifetimeSeconds * 1000;
  await t.timers.fire(1000);
  assert.equal(t.panel.snapshot().message, ACCESS_TEXT.anotherExpired);
  assert.equal(t.timers.pending().length, 0);
  const requests = t.server.log.length;
  await t.timers.fire(PAIRING.statusPollSeconds * 1000);
  assert.equal(t.server.log.length, requests);
  t.panel.close();
  assert.equal(t.server.log.length, requests, "an ended PIN is not cancelled");
});

test("panel: a lockout, a replaced PIN and a lost code are shown and stop polling", async () => {
  const locked = panelSetup({ ["POST " + OWNER]: () => [429, { available: false, reason: "pairing_locked", retry_after_s: 400 }] });
  locked.panel.open();
  await settle();
  assert.deepEqual([locked.panel.snapshot().message, locked.panel.snapshot().error], [ACCESS_TEXT.anotherLocked(7), true]);
  assert.equal(locked.timers.pending().length, 0);

  const replaced = panelSetup({ ["POST " + OWNER]: () => created(), ["GET " + OWNER]: () => status("active", "f".repeat(16)) });
  replaced.panel.open();
  await settle();
  await replaced.timers.fire(PAIRING.statusPollSeconds * 1000);
  assert.equal(replaced.panel.snapshot().message, ACCESS_TEXT.anotherReplaced);

  const lost = panelSetup({ ["POST " + OWNER]: () => created(), ["GET " + OWNER]: () => [401, { available: false }] });
  lost.panel.open();
  await settle();
  await lost.timers.fire(PAIRING.statusPollSeconds * 1000);
  assert.equal(lost.panel.snapshot().message, ACCESS_TEXT.pairingPrompt);
  assert.equal(lost.timers.pending().length, 0);
});

test("panel: closed while the PIN was being made cancels it when it arrives; New PIN replaces", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  let creates = 0;
  const t = panelSetup({ ["POST " + OWNER]: async (body) => {
    if (body.action !== "create") return status("cancelled");
    creates += 1;
    if (creates === 1) await gate;
    return created(creates === 2 ? "123456" : "042917", creates === 2 ? "1".repeat(16) : PIN_ID);
  } });
  t.panel.open();
  t.panel.close();
  release();
  await settle();
  assert.deepEqual(t.server.log.at(-1).body, { action: "cancel", pin_id: PIN_ID });
  assert.equal(t.panel.snapshot().phase, "idle");
  t.panel.open();
  await settle();
  assert.equal(t.panel.snapshot().pin, "123 456");
  t.panel.newPin();
  await settle();
  assert.equal(t.panel.snapshot().pin, "042 917");
  assert.equal(t.timers.pending(1000).length, 1, "one countdown, not two");
});
