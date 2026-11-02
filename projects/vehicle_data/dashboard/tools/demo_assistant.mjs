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
