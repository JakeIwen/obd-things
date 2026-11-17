import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import {
  DTC_IGNORE, classifyReply, excludeIgnored, identity, ignoreBody, ignoreState, ignoredLabel, isValidRequestId,
  makeRequestId, nextPending, noteProblem, restoreBody,
} from "../src/dtcIgnore.helpers.js";
import { postIgnore } from "../src/dtcIgnore.js";
import { codesSummary } from "../src/views/parked.helpers.js";

const SUMMARY = JSON.parse(fs.readFileSync(new URL("./fixtures/summary-v2.json", import.meta.url), "utf8"));
const NOW = Date.parse("2026-09-22T19:00:00Z");

const slice = (ignored = []) => ({ available: true, persistent: true, storage_error: null, limit: 500, ignored });
const entry = (module_key, fca_display, note = "", ignored_at = "2026-11-16T17:00:00Z") => ({ module_key, fca_display, note, ignored_at });

test("the contract file is what the Pi's route and limits say", () => {
  assert.equal(DTC_IGNORE.path, "/v1/diagnostics/dtc-ignores");
  assert.deepEqual(DTC_IGNORE.actions, { ignore: "ignore", restore: "restore" });
  assert.equal(DTC_IGNORE.note_max_chars, 200);
});

test("ignoreState reads the slice and hides the controls when it is missing or unavailable", () => {
  for (const bad of [null, undefined, {}, { available: false }, { available: true }, [], "x"]) {
    const s = ignoreState(bad);
    assert.equal(s.available, false);
    assert.equal(s.entries.length, 0);
  }
  const s = ignoreState(slice([entry("bcm_ccan", "B162A-15", "LED headlights"), entry("bcm_ccan", "B162A-15", "dup"), { module_key: "x" }, null]));
  assert.equal(s.available, true);
  assert.equal(s.entries.length, 1, "malformed and repeated entries are dropped");
  assert.deepEqual(s.entries[0], { moduleKey: "bcm_ccan", code: "B162A-15", note: "LED headlights", ignoredAt: "2026-11-16T17:00:00Z" });
  assert.ok(s.index.has(identity("bcm_ccan", "B162A-15")));
});

test("excludeIgnored takes records out of their groups and counts, and keeps unreturned counts", () => {
  const d = SUMMARY.dtcs;
  const index = ignoreState(slice([entry("bcm_ccan", "B162A-15"), entry("bcm_ccan", "B1632-15")])).index;
  const { dtcs, found } = excludeIgnored(d, index);
  assert.equal(dtcs.groups.current.length, 3);
  assert.equal(dtcs.group_counts.current, 3);
  assert.equal(dtcs.group_returned_counts.current, 3);
  assert.equal(dtcs.group_counts.confirmed_history, 38, "a group with no ignored record keeps its count, truncated or not");
  assert.deepEqual([...found.keys()].sort(), [identity("bcm_ccan", "B1632-15"), identity("bcm_ccan", "B162A-15")].sort());
  assert.equal(d.groups.current.length, 5, "the input is not modified");
  assert.equal(excludeIgnored(d, new Map()).dtcs, d);
  assert.equal(excludeIgnored(d, ignoreState(slice([entry("pcm", "P0000-00")])).index).dtcs, d, "nothing removed returns the input");
});

test("the Parked codes summary leaves ignored codes out of its count and titles", () => {
  const index = ignoreState(slice([entry("bcm_ccan", "B162A-15"), entry("bcm_ccan", "B1632-15")])).index;
  const before = codesSummary(SUMMARY.dtcs, NOW);
  const after = codesSummary(SUMMARY.dtcs, NOW, index);
  assert.match(before.line, /^5 current/);
  assert.match(after.line, /^3 current/);
  assert.equal(after.badge.text, "3");
  assert.ok(after.items.every((i) => !/B162A|B1632/.test(i.code)));
});

test("request bodies carry exactly the contract's fields", () => {
  assert.deepEqual(ignoreBody("bcm_ccan", "B162A-15", "  LED headlights "), {
    action: "ignore", module_key: "bcm_ccan", fca_display: "B162A-15", note: "LED headlights",
  });
  assert.deepEqual(ignoreBody("bcm_ccan", "B162A-15"), { action: "ignore", module_key: "bcm_ccan", fca_display: "B162A-15", note: "" });
  assert.deepEqual(restoreBody("bcm_ccan", "B162A-15"), { action: "restore", module_key: "bcm_ccan", fca_display: "B162A-15" });
});

test("note limit and request ids", () => {
  assert.equal(noteProblem("a".repeat(200)), null);
  assert.match(noteProblem("a".repeat(201)), /200 characters \(now 201\)/);
  assert.equal(noteProblem("  " + "a".repeat(200) + "  "), null, "the trimmed note is what counts");
  const id = makeRequestId();
  assert.ok(isValidRequestId(id));
  assert.notEqual(makeRequestId(), id);
  assert.ok(isValidRequestId(makeRequestId((a) => a.fill(7))));
});

test("a retry of the same change reuses its request id; a different change gets a new one", () => {
  let n = 0;
  const makeId = () => "dtc_test_request_" + ++n;
  const first = nextPending(null, ignoreBody("m", "B1-1", "a"), makeId);
  const again = nextPending(first, ignoreBody("m", "B1-1", "a"), makeId);
  assert.equal(again, first);
  const other = nextPending(first, ignoreBody("m", "B1-1", "b"), makeId);
  assert.notEqual(other.payload.request_id, first.payload.request_id);
  assert.equal(first.payload.action, "ignore");
});

test("classifyReply: the new list, the Pi's own refusal, or the status", () => {
  const state = slice([entry("m", "B1-1")]);
  assert.deepEqual(classifyReply(200, true, state), { kind: "ok", state });
  assert.deepEqual(classifyReply(409, false, { detail: "ignored list is full (500)" }), { kind: "error", message: "ignored list is full (500)" });
  assert.match(classifyReply(500, false, null).message, /HTTP 500/);
  assert.match(classifyReply(200, true, { ok: true }).message, /unreadable/);
  assert.match(classifyReply(200, true, { available: false }).message, /unreadable/, "a 200 that is not a list changes nothing");
});

test("postIgnore sends one same-origin JSON POST and never rejects", async () => {
  const calls = [];
  const reply = (status, body) => async (url, init) => { calls.push({ url, init }); return { ok: status < 300, status, json: async () => body }; };
  const state = slice([entry("m", "B1-1", "n")]);
  const payload = { ...restoreBody("m", "B1-1"), request_id: "dtc_abcdefghijkl" };
  assert.deepEqual(await postIgnore(reply(200, state), payload), { kind: "ok", state });
  assert.equal(calls[0].url, "/v1/diagnostics/dtc-ignores");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[0].init.body), payload);
  assert.equal((await postIgnore(reply(400, { detail: "bad note" }), payload)).message, "bad note");
  assert.match((await postIgnore(async () => { throw new Error("offline"); }, payload)).message, /Could not reach the Pi/);
  assert.match((await postIgnore(async () => ({ ok: false, status: 502, json: async () => { throw new Error("x"); } }), payload)).message, /HTTP 502/);
});

test("ignoredLabel: the date when there is one", () => {
  assert.equal(ignoredLabel({ ignoredAt: "2026-11-16T17:00:00Z" }, () => "Nov 16, 2026, 10:00 AM MST"), "Ignored Nov 16, 2026, 10:00 AM MST");
  assert.equal(ignoredLabel({ ignoredAt: null }, () => "x"), "Ignored");
  assert.equal(ignoredLabel({ ignoredAt: "junk" }, () => "unknown time"), "Ignored");
});
