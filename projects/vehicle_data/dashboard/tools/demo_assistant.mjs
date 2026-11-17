// Synthetic /v1/assistant payloads for README screenshots of the Ask Codex explanation chat and
// the Add Early Warning setup chat. Contract: src/dialogs/WarningChat.helpers.js and
// docs/warning-chat.md. Nothing here was said by a model; it only fills the dialog.
// Routes use the capture harness contract: {method, pathname, prefix, status, json}, first match wins.
import {DEMO_OPEN_EVENT_ID, DEMO_OPEN_EVENT_TITLE, DEMO_RR_BASELINE} from "./demo_events.mjs";
import {MINUTE_MS, iso} from "./demo_data.mjs";

/** Which conversation POST /v1/assistant/chats returns; the harness cannot read request bodies. */
export const DEMO_ASSISTANT_SCENES = Object.freeze(["explain", "setup"]);

const EXPLAIN_CHAT_ID = "3f9c1e5a7b2d4c68a0e1f2b3c4d5e6f7";
const SETUP_CHAT_ID = "b81d4a6e92c0473f8a5d1e7c3b9f2046";
const PROPOSAL_ID = "c4e7a91d02b8365f7e1a4c9d8b2f60531e7a9c4d0b8f2a6e3d51c97b0a4e8f12";

const SETTINGS = Object.freeze({
  default_model: "gpt-6-astra",
  models: {"gpt-6-astra": ["low", "medium", "high", "xhigh"], "gpt-6.1-sol": ["low", "medium", "high"]},
  default_effort: "high",
  timeout_seconds: 300,
});

// Same shape as the real turn settings: the model that answered and the effort it ran at.
const TURN_SETTINGS = Object.freeze({model: "default", effort: "high", resolved_model: "gpt-6-astra"});

const EXPLAIN_QUESTION = "Why did this warning open, and how does it compare with previous trips?";

const median = DEMO_RR_BASELINE.median;
const EXPLAIN_ANSWER = [
  `Event ${DEMO_OPEN_EVENT_ID} opened because the rear-right tire pressure stayed below its usual range for three readings in a row. `
    + `At opening it read 79.0 psi, against a typical ${median}.0 psi for highway driving over the last 30 days across ${DEMO_RR_BASELINE.tripCount} prior drives. `
    + "That is 3.0 psi low, and the rule needs at least 2.67 psi.",
  "A single low reading would not have opened it. The rule needs three readings within three minutes, all below 79.3 psi, "
    + "and the saved samples show 79.3, 79.2 and 79.0 psi over about 30 seconds. Since then they have stayed between 79.0 and 79.2 psi, "
    + "while the rear-left tire reads 80 psi in the same driving, so the two rear tires have drifted about 1 psi apart.",
  "A trend alone does not establish a cause. A slow leak, a temperature change or a sensor offset would all look like this in the saved "
    + "readings, and the comparison band is a monitoring reference built from your own history, not a manufacturer limit. "
    + "Checking the tire with a gauge at the next stop would tell you whether the sensor is right.",
].join("\n\n");

const SETUP_QUESTION = "Let me know if coolant stays above 220°F while the engine is running.";
const SETUP_ANSWER = [
  "I drafted a rule for that. It watches engine coolant temperature and warns when three distinct readings in a row are above 220 °F "
    + "within a 30 second window, each no more than 5 seconds old.",
  "It only counts readings while the engine is running (fresh RPM above 400), so a hot soak after shutdown will not trigger it. "
    + "220 °F is your owner reference, not a manufacturer limit, and I chose three readings so one noisy sample cannot warn. "
    + "Nothing is installed until you approve it below.",
].join("\n\n");

const SETUP_PROPOSAL = Object.freeze({
  title: "Sustained high coolant temperature",
  metric: "engine.coolant_temperature",
  operator: "above",
  threshold: 220,
  persistence_observations: 3,
  window_seconds: 30,
  max_age_seconds: 5,
  engine_running: true,
});

function completeTurn(nowMs, user, extra) {
  return {
    user, assistant: null, error: null, state: "complete", settings: TURN_SETTINGS,
    started_at: iso(nowMs - 2 * MINUTE_MS), completed_at: iso(nowMs - 90_000), ...extra,
  };
}

/** The explanation chat about the open RR event: one question, one finished answer. */
export function demoExplainChat(nowMs) {
  return {
    id: EXPLAIN_CHAT_ID, title: DEMO_OPEN_EVENT_TITLE, event: {kind: "episode", id: String(DEMO_OPEN_EVENT_ID)},
    created_at: iso(nowMs - 2 * MINUTE_MS), evidence_at: iso(nowMs - 2 * MINUTE_MS),
    turns: [completeTurn(nowMs, EXPLAIN_QUESTION, {assistant: EXPLAIN_ANSWER})],
  };
}

/** The Add Early Warning chat: one request, one drafted proposal that is not installed. */
export function demoSetupChat(nowMs) {
  return {
    id: SETUP_CHAT_ID, title: "Add Early Warning", event: {kind: "setup", id: "new-warning"},
    created_at: iso(nowMs - 2 * MINUTE_MS), evidence_at: iso(nowMs - 2 * MINUTE_MS),
    turns: [completeTurn(nowMs, SETUP_QUESTION, {
      assistant: SETUP_ANSWER, proposal: SETUP_PROPOSAL, proposal_unit: "°F", proposal_id: PROPOSAL_ID, applied: false,
    })],
  };
}

/** localStorage entries (full keys) that make the dialog skip the pairing form. */
export function demoAssistantStorage() {
  return {
    "van-warning-chat.access": "DEMO_ILLUSTRATIVE_ACCESS_CODE_0000000000",
    "van-warning-chat.client": "d3".repeat(32),
  };
}

const unknown = {available: false, detail: "Unknown assistant request"};

/**
 * Harness routes for the chat dialog. `scene` picks which conversation POST /v1/assistant/chats opens;
 * both conversations stay readable by id. No message, cancel, apply or remove route exists on purpose.
 */
export function demoAssistantRoutes(nowMs, scene = "explain") {
  if (!DEMO_ASSISTANT_SCENES.includes(scene)) throw new RangeError("unknown assistant scene: " + scene);
  const explain = demoExplainChat(nowMs);
  const setup = demoSetupChat(nowMs);
  const status = {available: true, settings: SETTINGS, warnings: [], warning_config_error: null, warning_evaluator_enabled: true};
  return [
    {method: "GET", pathname: "/v1/assistant/status", prefix: false, status: 200, json: status},
    {method: "POST", pathname: "/v1/assistant/chats", prefix: false, status: 200, json: scene === "setup" ? setup : explain},
    {method: "GET", pathname: "/v1/assistant/chats/" + EXPLAIN_CHAT_ID, prefix: false, status: 200, json: explain},
    {method: "GET", pathname: "/v1/assistant/chats/" + SETUP_CHAT_ID, prefix: false, status: 200, json: setup},
    {method: "GET", pathname: "/v1/assistant/", prefix: true, status: 404, json: unknown},
  ];
}

/* ---------- code research (dialogs/DtcResearch.jsx) ----------
   Illustrative payloads in the research backend's API shapes. Nothing here was said by a model. */

const hex = (n) => n.repeat(32).slice(0, 32);
const RUN_DONE = hex("a1");
const RUN_BUSY = hex("b2");
const RUN_NEW = hex("c3");
const RUN_OLD = hex("d4");

const REPORT = [
  "## What it means",
  "B162E-15 is the body computer's fault for the **right low-beam lamp circuit**: it saw a short to battery, or an open circuit, on the output that drives that lamp. It does not say which, and with an open circuit the bulb is often fine.",
  "## Likely causes on this van",
  "- **Owner-fitted LED low beams.** An LED draws a fraction of a halogen's current, and the body computer reads that as an open circuit. This is the most likely cause here, and it is why the left low beam and the daytime lights report the same way.",
  "- A loose or corroded lamp connector, or a pin that has backed out of it.",
  "- A chafed wire in the harness near the headlamp that touches the body or a power feed.",
  "## What to check",
  "1. Confirm both low beams work. If the lamp lights and only the code is stored, the circuit is being misread, not broken.",
  "2. Unplug the right lamp connector and look for green corrosion, bent pins or a loose terminal.",
  "3. With the lamp unplugged and the lights off, check the lamp feed wire for continuity to ground with a multimeter; it should read open.",
  "4. If the LED kit has a `CAN-bus` or load-resistor variant, that variant is the usual fix. You can do this yourself at the van.",
  "## Sources",
  "- [Ram Trucks owner resources](https://www.ramtrucks.com/)",
  "- [NHTSA vehicle safety information](https://www.nhtsa.gov/)",
  "- [ProMaster Forum](https://www.promasterforum.com/)",
].join("\n");

const ANSWER = [
  "A load resistor can work, but it is the second choice. **First try the CAN-bus-ready (\"canceller\") version of the same LED bulb**, which has the resistance built in.",
  "- Resistors get hot, so mount them on bare metal, away from wiring.\n- Fit one per lamp, in parallel with the bulb.",
  "Because the code does not turn the lamp off, it is safe to drive with while you sort it out. This is general guidance; check it against your kit's instructions.",
].join("\n\n");

const SUMMARY = "Body computer sees the right low-beam circuit as open, most likely because the LED bulbs draw too little current.";

function runBase(id, code, over) {
  return {
    run_id: id, module_key: "bcm_ccan", fca_display: code, created_at: null, updated_at: null, provider: "claude", model: "opus",
    effort: "high", state: "done", busy: false, summary: SUMMARY, summary_note: null, duration_s: 214, error: null, reason: null,
    turn_count: 1, can_continue: true, continue_mode: "resume", phase: null, progress: null, activity: {searches: 0, pages: 0},
    elapsed_s: null, format_warnings: [], turns: [], ...over,
  };
}

/** The research routes: models, the list, three codes with runs and one with none. */
export function demoResearchRoutes(nowMs) {
  const at = (minutes) => iso(nowMs - minutes * MINUTE_MS);
  const done = runBase(RUN_DONE, "B162E-15", {
    created_at: at(46), updated_at: at(40), turn_count: 3,
    turns: [
      {role: "assistant", kind: "report", text: REPORT, at: at(42), state: "complete", error: null, reason: null, mode: "initial", duration_s: 214},
      {role: "user", kind: "question", text: "Would a load resistor fix it?", at: at(41)},
      {role: "assistant", kind: "answer", text: ANSWER, at: at(40), state: "complete", error: null, reason: null, mode: "resumed", duration_s: 38},
    ],
  });
  const busy = runBase(RUN_BUSY, "B104E-15", {
    created_at: at(2), updated_at: at(0), state: "running", busy: true, summary: null, duration_s: null, can_continue: false,
    continue_mode: "none", phase: "searching", progress: "Searching the web · 3 searches, 1 page", activity: {searches: 3, pages: 1},
    elapsed_s: 47, provider: "codex", model: "gpt-6-astra", effort: "high",
    turns: [{role: "assistant", kind: "report", text: null, at: at(2), state: "running", error: null, reason: null, mode: "initial", duration_s: null}],
  });
  const newer = runBase(RUN_NEW, "B104D-15", {
    created_at: at(25), updated_at: at(22), provider: "codex", model: "gpt-6-astra", effort: "high",
    summary: "Left daytime running light circuit reads open; check the LED lamp and its connector.",
    turns: [{role: "assistant", kind: "report", text: REPORT.replace("right low-beam", "left daytime running light"), at: at(22), state: "complete",
      error: null, reason: null, mode: "initial", duration_s: 171}],
  });
  const older = runBase(RUN_OLD, "B104D-15", {
    created_at: at(1500), updated_at: at(1497), summary: "Left daytime lamp circuit short or open.",
    turns: [{role: "assistant", kind: "report", text: "## What it means\nThe left daytime running light circuit reports a short to battery or an open.",
      at: at(1497), state: "complete", error: null, reason: null, mode: "initial", duration_s: 190}],
  });
  const summary = (run) => {
    const {phase, progress, activity, elapsed_s, format_warnings, turns, ...rest} = run;
    return rest;
  };
  const item = (run, runs) => ({module_key: run.module_key, fca_display: run.fca_display, run_id: run.run_id, state: run.state, busy: run.busy,
    summary: run.summary, summary_note: run.summary_note, provider: run.provider, model: run.model, effort: run.effort, updated_at: run.updated_at, runs});
  const code = (c, runs, selected) => ({method: "GET", pathname: "/v1/assistant/dtc-research/code/bcm_ccan/" + c, prefix: false, status: 200,
    json: {available: true, module_key: "bcm_ccan", fca_display: c, runs: runs.map(summary), selected}});
  const catalog = {available: true, refreshing: false, default: {provider: "claude", model: "opus", effort: "high"}, groups: [
    {provider: "claude", label: "Claude", available: true, reason: null, source: "discovered", checked_at: at(3), models: [
      {id: "opus", label: "Opus", efforts: ["low", "medium", "high"], default_effort: "high"},
      {id: "sonnet", label: "Sonnet", efforts: ["low", "medium", "high"], default_effort: "medium"},
      {id: "haiku", label: "Haiku", efforts: [], default_effort: null}]},
    {provider: "codex", label: "GPT (Codex)", available: true, reason: null, source: "discovered", checked_at: at(3), models: [
      {id: "gpt-6-astra", label: "GPT-6-Astra", efforts: ["low", "medium", "high", "xhigh"], default_effort: "high"},
      {id: "gpt-6.1-terra", label: "GPT-6.1-Terra", efforts: ["low", "medium", "high"], default_effort: "medium"}]},
    {provider: "local", label: "Local models", available: false, reason: "No local model is set up on this Pi", source: "fallback", checked_at: null, models: []}]};
  return [
    {method: "GET", pathname: "/v1/assistant/models", prefix: false, status: 200, json: catalog},
    {method: "GET", pathname: "/v1/assistant/dtc-research", prefix: false, status: 200,
      json: {available: true, items: [item(busy, 1), item(newer, 2), item(done, 1)]}},
    code("B162E-15", [done], done), code("B104E-15", [busy], busy), code("B104D-15", [newer, older], newer),
    {method: "GET", pathname: "/v1/assistant/dtc-research/code/telematics/U1934-87", prefix: false, status: 200,
      json: {available: true, module_key: "telematics", fca_display: "U1934-87", runs: [], selected: null}},
    {method: "GET", pathname: "/v1/assistant/dtc-research/run/" + RUN_OLD, prefix: false, status: 200, json: {available: true, run: older}},
    {method: "GET", pathname: "/v1/assistant/dtc-research/run/" + RUN_NEW, prefix: false, status: 200, json: {available: true, run: newer}},
    {method: "GET", pathname: "/v1/assistant/dtc-research/run/" + RUN_BUSY + "/progress", prefix: false, status: 200,
      json: {available: true, progress: {run_id: RUN_BUSY, state: "running", busy: true, phase: busy.phase, progress: busy.progress,
        activity: busy.activity, elapsed_s: busy.elapsed_s, turn_count: 1, updated_at: busy.updated_at}}},
  ];
}
