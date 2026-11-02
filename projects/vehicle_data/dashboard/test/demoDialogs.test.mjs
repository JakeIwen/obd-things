import { test } from "node:test";
import assert from "node:assert/strict";
import * as EH from "../src/dialogs/EventHistory.helpers.js";
import * as WC from "../src/dialogs/WarningChat.helpers.js";
import { buildDemoScene } from "../tools/demo_data.mjs";
import { demoEventRoutes, DEMO_OPEN_EVENT_ID } from "../tools/demo_events.mjs";
import { demoAssistantRoutes, demoAssistantStorage } from "../tools/demo_assistant.mjs";

const NOW = Date.UTC(2026, 9, 21, 19, 30);

/** The capture harness's matching rule: method equal, pathname equal (or prefixed), first match wins. */
function routedFetch(routes) {
  const calls = [];
  const fetch = async (url, init = {}) => {
    const method = init.method || "GET";
    const pathname = new URL(url, "http://demo.invalid").pathname;
    calls.push({ method, url });
    const route = routes.find((r) => r.method === method && (r.prefix ? pathname.startsWith(r.pathname) : pathname === r.pathname));
    assert.ok(route, `no demo route for ${method} ${url}`);
    return { status: route.status, ok: route.status < 300, json: async () => structuredClone(route.json) };
  };
  return { fetch, calls };
}

test("saved events list parses to a newest-first page of about two dozen events", async () => {
  const { fetch } = routedFetch(demoEventRoutes(NOW));
  const page = await EH.requestEvidence(EH.listUrl({}), undefined, { fetch });
  const rows = page.events.map(EH.eventRowModel);
  assert.equal(rows.length, 23);
  assert.equal(EH.listStatusText(rows.length), "23 saved events shown. Closed events remain available.");
  assert.equal(rows[0].id, DEMO_OPEN_EVENT_ID);
  assert.equal(rows[0].title, "Rear-right tire pressure drifting below comparable history");
  assert.match(rows[0].state, /^Unresolved/);
  const ids = page.events.map((e) => e.id);
  assert.deepEqual(ids, [...ids].sort((a, b) => b - a), "newest first");
  const opened = page.events.map((e) => Date.parse(e.opened_at));
  assert.deepEqual(opened, [...opened].sort((a, b) => b - a));
  assert.ok(NOW - opened.at(-1) > 30 * 86400_000 && NOW - opened.at(-1) < 38 * 86400_000, "spans about five weeks");
  assert.equal(page.events.filter((e) => e.status === "open").length, 1);
  assert.ok(page.events.some((e) => e.current_state === "suppressed" && e.status === "resolved"));
  assert.ok(rows.some((r) => r.title === "C-CAN interface configuration mismatch"));
  assert.ok(rows.some((r) => r.state.startsWith("Unconfirmed")));
  assert.equal(page.next_before, null);
});

test("event 614 detail opens with the Drive scene's numbers and a chart that crosses the boundary", async () => {
  const { fetch, calls } = routedFetch(demoEventRoutes(NOW));
  const packet = await EH.requestEvidence(EH.detailUrl(DEMO_OPEN_EVENT_ID), undefined, { fetch });
  const event = packet.event;
  const overview = EH.overviewModel(event);
  assert.deepEqual(overview.keyReadings, [
    ["Reading at opening", "79 psi"], ["Typical reading", "82 psi"], ["Repeated readings", "3 of 3"],
  ]);
  assert.equal(overview.deviationLine, "3 psi below the baseline (2.67 psi required).");
  assert.equal(overview.latest.line, "Warning · 79 psi");
  assert.match(overview.tags.join(), /Unresolved/);

  const scene = buildDemoScene(NOW);
  assert.equal(event.latest_assessment.current.value, scene.metrics["tire.pressure.rr"].value);
  assert.equal(scene.metrics["tire.pressure.rl"].value, 80);

  const chart = EH.chartModel(event);
  assert.equal(chart.empty, false);
  assert.equal(chart.dots.length, 20);
  assert.equal(chart.segments.length, 19, "readings are joined under the 15 s gap rule");
  assert.ok(Math.abs(chart.boundary.value - 79.33) < 0.01);
  assert.match(chart.caption, /79–80 psi/);
  assert.match(chart.caption, /opening boundary 79\.33 psi/);
  const values = event.sample_window.map((p) => p.value);
  assert.ok(values.slice(0, 7).every((v) => v > chart.boundary.value));
  assert.ok(values.slice(7, 10).every((v) => v < chart.boundary.value), "persistence run of three ends at the opening reading");

  const timeline = EH.timelineModel(event.timeline);
  assert.deepEqual(timeline.rows.map((r) => r.title), ["Event opened"]);
  assert.deepEqual(timeline.notes.map((r) => r.title), ["Monitoring resumed", "Monitoring paused"]);
  assert.equal(EH.recurrenceModel(event).events.length, 2);
  const retention = EH.coverageModel(event, packet.system_guide).retention.map((s) => s.text).join("");
  assert.match(retention, /no automatic age deletion/);
  assert.equal(EH.headerModel("detail", packet).title, "Rear-right tire pressure drifting below comparable history");
  assert.deepEqual(calls.map((c) => c.method), ["GET"]);
});

test("every other events path is refused locally and never reaches the broker", async () => {
  const { fetch } = routedFetch(demoEventRoutes(NOW));
  await assert.rejects(EH.requestEvidence(EH.detailUrl(589), undefined, { fetch }), /not retained in this demo/);
  await assert.rejects(EH.requestEvidence(EH.baselinesUrl(614, "a".repeat(64), 0), undefined, { fetch }), /not retained/);
});

function chatController(scene) {
  const { fetch, calls } = routedFetch(demoAssistantRoutes(NOW, scene));
  const store = new Map(Object.entries(demoAssistantStorage()));
  const backend = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) };
  const snapshots = [];
  const ctrl = WC.createChatController({
    fetch, sleep: async () => {}, storage: WC.createStorage(backend), setTimeout: () => 0, clearTimeout: () => {},
    now: () => new Date(NOW), onChange: (s) => snapshots.push(s),
  });
  return { ctrl, calls, last: () => snapshots.at(-1) };
}
const settle = async () => { for (let i = 0; i < 8; i += 1) await new Promise((r) => setImmediate(r)); };

test("Ask Codex about event 614 shows model controls, one question, one finished answer", async () => {
  const { ctrl, calls, last } = chatController("explain");
  ctrl.open("explain", { eventId: DEMO_OPEN_EVENT_ID, summary: "Rear-right tire pressure drifting below comparable history" });
  await settle();
  const snap = last();
  assert.equal(snap.pairing, false, "stored access code skips pairing");
  assert.equal(snap.disabled, false);
  assert.equal(snap.model, "default");
  assert.equal(snap.models[0].label, "Default (gpt-6-astra)");
  assert.equal(snap.effort, "high");
  assert.equal(snap.efforts.find((e) => e.value === snap.effort).label, "High");
  const rows = snap.transcript.rows;
  assert.deepEqual(rows.map((r) => r.heading), ["You", "Codex · gpt-6-astra / high"]);
  assert.equal(rows[0].text, "Why did this warning open, and how does it compare with previous trips?");
  assert.equal(rows[1].kind, "answer");
  assert.equal(rows[1].text.split("\n\n").length, 3);
  assert.equal(snap.status, "Ready for a follow-up");
  assert.equal(snap.canSend, true);
  assert.equal(snap.proposal.visible, false);
  assert.equal(snap.title, "Rear-right tire pressure drifting below comparable history");
  assert.deepEqual(calls.map((c) => c.method + " " + c.url), ["GET /v1/assistant/status", "POST /v1/assistant/chats"]);
});

test("Add Early Warning shows a drafted coolant rule that is not installed", async () => {
  const { ctrl, last } = chatController("setup");
  ctrl.open("add-warning", {});
  await settle();
  const snap = last();
  assert.equal(snap.title, "Add Early Warning");
  assert.equal(snap.evidence, WC.TEXT.setupEvidence);
  assert.equal(snap.transcript.rows[0].text, "Let me know if coolant stays above 220°F while the engine is running.");
  assert.equal(snap.transcript.rows[1].kind, "answer");
  assert.equal(snap.proposal.visible, true);
  assert.equal(snap.proposal.applied, false);
  assert.equal(snap.proposal.appliedText, "Not installed.");
  assert.equal(snap.proposal.open, true);
  assert.equal(snap.proposal.text, [
    "Sustained high coolant temperature",
    "engine.coolant_temperature: above 220 °F",
    "3 distinct observations within 30s; sample age/gap ≤ 5s.",
    "Engine running required (fresh RPM > 400).",
  ].join("\n"));
  assert.equal(snap.saved.visible, false);
  assert.equal(snap.canSend, true);
});

test("assistant routes expose no write beyond opening a chat, and reject an unknown scene", () => {
  const routes = demoAssistantRoutes(NOW);
  assert.deepEqual(routes.filter((r) => r.method !== "GET").map((r) => r.pathname), ["/v1/assistant/chats"]);
  assert.throws(() => demoAssistantRoutes(NOW, "bogus"), /unknown assistant scene/);
  assert.ok(WC.isValidAccessCode(demoAssistantStorage()["van-warning-chat.access"]));
  assert.match(demoAssistantStorage()["van-warning-chat.client"], WC.CLIENT_ID_RE);
});
