/**
 * Code research with AI, the pure part shared by the Codes card (row summaries, the panel button)
 * and the research dialog: the contract constants, request bodies, error wording, row text and the
 * polling decisions. Contract: the research backend's API (GET /v1/assistant/models,
 * /v1/assistant/dtc-research...). Node-testable: no Preact, no store, no DOM.
 */

import { fmtDuration, fmtTime, parseDate } from "./format.js";

/*
 * The contract in one block. The backend keeps the same vocabulary in
 * projects/vehicle_data/dtc_research.json; a later pass can replace this block with an import
 * of that file.
 */
export const RESEARCH = Object.freeze({
  routes: Object.freeze({
    models: "/v1/assistant/models",
    list: "/v1/assistant/dtc-research",
    codePrefix: "/v1/assistant/dtc-research/code/",
    runPrefix: "/v1/assistant/dtc-research/run/",
    post: "/v1/assistant/dtc-research",
  }),
  actions: Object.freeze({ start: "start", ask: "ask", cancel: "cancel" }),
  states: Object.freeze({ running: "running", done: "done", failed: "failed", cancelled: "cancelled" }),
  modes: Object.freeze({ initial: "initial", resumed: "resumed", replayed: "replayed" }),
  summaryNotes: Object.freeze({ missing: "missing", truncated: "truncated" }),
  progressPollMs: 1000,
  modelsPollMs: 2000,
  modelsPollMax: 15,
  requestTimeoutMs: 20000,
  questionMax: 2000,
  summaryMax: 140,
  moduleKeyRe: /^[a-z][a-z0-9_]{0,39}$/,
  codeRe: /^[PBCU][0-9A-F]{4}(-[0-9A-F]{2})?$/,
  runIdRe: /^[a-f0-9]{32}$/,
  requestIdRe: /^[a-f0-9]{32}$/,
});

const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : null);
const arr = (v) => (Array.isArray(v) ? v : []);
const str = (v) => (typeof v === "string" && v.length > 0 ? v : null);

/* ---------- identity and routes ---------- */

export const itemKey = (moduleKey, code) => moduleKey + "\u001f" + code;

/** Whether a Codes-card row names a code the research routes accept. */
export function researchable(moduleKey, code) {
  return RESEARCH.moduleKeyRe.test(String(moduleKey || "")) && RESEARCH.codeRe.test(String(code || ""));
}

export function codePath(moduleKey, code) {
  if (!researchable(moduleKey, code)) throw new Error("Not a code the research service accepts");
  return RESEARCH.routes.codePrefix + moduleKey + "/" + code;
}

export function runPath(runId, tail = "") {
  if (!RESEARCH.runIdRe.test(String(runId || ""))) throw new Error("Invalid run id");
  return RESEARCH.routes.runPrefix + runId + tail;
}

export const progressPath = (runId) => runPath(runId, "/progress");

/* ---------- request bodies ---------- */

/** Why a follow-up cannot be sent, or null. */
export function questionProblem(text) {
  const n = String(text || "").trim().length;
  if (n === 0) return "Type a question first.";
  return n > RESEARCH.questionMax ? "Keep the question to " + RESEARCH.questionMax + " characters (now " + n + ")." : null;
}

export function startBody(target, choice, requestId) {
  return {
    action: RESEARCH.actions.start, module_key: target.module_key, fca_display: target.fca_display,
    provider: choice.provider, model: choice.model, effort: choice.effort === undefined ? null : choice.effort,
    request_id: requestId,
  };
}

export const askBody = (runId, question, requestId) =>
  ({ action: RESEARCH.actions.ask, run_id: runId, question: String(question).trim(), request_id: requestId });

export const cancelBody = (runId, requestId) =>
  ({ action: RESEARCH.actions.cancel, run_id: runId, request_id: requestId });

/** 32 lowercase hex characters from `random(16)` (assistantAccess.randomId). */
export const newRequestId = (random) => random(16);

/* ---------- wording ---------- */

/** One owner-facing sentence per error `reason` (API errors and failed-turn reasons). */
export const REASON_TEXT = Object.freeze({
  invalid_request: "That request was not accepted. Reopen this code and try again.",
  not_found: "That research is no longer on the Pi.",
  run_busy: "A turn is already running for this code. Wait for it to finish or stop it.",
  turn_limit: "This conversation has reached its limit of 24 turns. Start new research to continue.",
  record_full: "This conversation is full. Start new research to continue.",
  cannot_continue: "The report did not finish, so there is nothing to follow up on. Start new research.",
  busy: "The assistant is busy with another job. Try again in a few seconds.",
  rate_limited: "Too many requests in the last minute. Wait a moment and try again.",
  daily_limit: "The daily research limit has been reached. Try again tomorrow.",
  provider_unavailable: "That model provider is not available right now.",
  worker_unavailable: "The research service on the Pi is not running.",
  storage_error: "The Pi could not save the research. Nothing was started.",
  timeout: "The model took too long and was stopped.",
  cancelled: "Stopped.",
  interrupted: "Interrupted by a restart of the research service.",
  provider_error: "The model provider reported an error.",
  auth: "The provider sign-in was refused. It needs to be set up again on the Pi.",
  upstream_rate_limit: "The provider is rate limiting requests. Try again later.",
  budget_exceeded: "The turn reached its cost limit and was stopped.",
  policy_violation: "The model tried something research does not allow, so the turn was stopped.",
  protocol_error: "The model's answer could not be read.",
  space_exhausted: "The research storage on the Pi is full.",
});

export const TEXT = Object.freeze({
  title: "Research with AI",
  unavailable: "Code research is not available on this Pi.",
  unreachable: "Could not reach the Pi. Nothing changed; try again.",
  setupLead: "Pick a model to research this code. It searches the web for what the code means on this van.",
  sentLine: "Sent to the model: the code, its module and the van's model facts. Never the VIN or a location. The model searches the web; web pages are untrusted.",
  working: "Working…",
  replayNote: "The original session could not be resumed; this answer came from a replay of the conversation.",
  askLabel: "Ask a follow-up",
  askPlaceholder: "Ask a follow-up...",
  refreshing: "Checking which models are available…",
  noModels: "No model is available. Sign in to a provider on the Pi first.",
  summaryMissing: "The report had no one-line summary.",
  summaryTruncated: "The one-line summary was shortened.",
});

/** `REASON_TEXT[reason]`, else the service's own `detail`, else `fallback`. */
export function errorText(reason, detail, fallback = TEXT.unreachable) {
  return REASON_TEXT[reason] || str(detail) || fallback;
}

/* ---------- the row summaries ---------- */

/** Items of GET /v1/assistant/dtc-research by code, or an empty map for anything else. */
export function researchIndex(body) {
  const index = new Map();
  const b = obj(body);
  if (!b || b.available !== true) return index;
  for (const item of arr(b.items)) {
    const i = obj(item);
    if (i && str(i.module_key) && str(i.fca_display)) index.set(itemKey(i.module_key, i.fca_display), i);
  }
  return index;
}

/** The secondary line under a code's meaning: `AI: <summary>`, `AI: researching…` or nothing. */
export function rowExtraLine(item) {
  const i = obj(item);
  if (!i) return null;
  if (i.busy === true) return "AI: researching…";
  const summary = str(i.summary);
  return summary ? "AI: " + summary : null;
}

/** The panel button: `Open research` when the code has a run, else `Research with AI`. */
export const researchButtonText = (item) => (obj(item) ? "Open research" : "Research with AI");

/** Tooltip of the row line's info icon. */
export const AI_LINE_TITLE = "AI research, not a reviewed meaning. Open the research for the full report and its sources.";

/* ---------- runs ---------- */

/** `running`, `done`, `failed` or `cancelled`; an unknown state reads as failed. */
export function runPhase(run) {
  const state = obj(run)?.state;
  return Object.values(RESEARCH.states).includes(state) ? state : RESEARCH.states.failed;
}

/** Whether the dialog should fetch the whole run after a progress poll. */
export function needsRunFetch(known, progress) {
  const k = obj(known);
  const p = obj(progress);
  if (!p) return false;
  if (!k) return true;
  return k.turn_count !== p.turn_count || k.state !== p.state || k.busy !== p.busy;
}

export const shouldPoll = ({ busy, open, visible }) => Boolean(busy && open && visible);

/** `Searching the web · 3 searches, 1 page · 42 s` while a turn runs. */
export function progressLine(progress) {
  const p = obj(progress);
  const text = (p && str(p.progress)) || TEXT.working;
  const elapsed = p && typeof p.elapsed_s === "number" ? fmtDuration(p.elapsed_s) : null;
  return elapsed && elapsed !== "—" ? text + " · " + elapsed : text;
}

const clock = (iso, nowMs) => (parseDate(iso) ? fmtTime(iso, new Date(nowMs)) : null);

/** The conversation rows: the report first, then questions and answers. A turn still running has no row (the progress line stands for it). */
export function turnRows(run, nowMs) {
  return arr(obj(run)?.turns).filter(obj).map((turn, i) => ({ turn, i })).filter(({ turn }) => !(turn.role !== "user" && turn.state === "running")).map(({ turn, i }) => {
    const at = clock(turn.at, nowMs);
    if (turn.role === "user") return { key: "t" + i, role: "user", heading: "You", text: String(turn.text || ""), markdown: false, at, note: null, error: null };
    const failed = turn.state === "failed" || turn.state === "cancelled";
    return {
      key: "t" + i,
      role: "assistant",
      heading: turn.kind === "report" ? "Report" : "Answer",
      text: failed ? "" : String(turn.text || ""),
      markdown: true,
      at,
      note: turn.mode === RESEARCH.modes.replayed ? TEXT.replayNote : null,
      error: failed ? errorText(turn.reason, turn.error, turn.state === "cancelled" ? REASON_TEXT.cancelled : TEXT.working) : null,
    };
  });
}

/** Footer: `{line, notes}`; the notes are the quiet extras (summary and format warnings). */
export function runFooter(run, modelLabel, nowMs) {
  const r = obj(run);
  if (!r) return { line: "", notes: [] };
  const duration = typeof r.duration_s === "number" ? fmtDuration(r.duration_s) : null;
  const line = [modelLabel || r.model, str(r.effort), clock(r.updated_at || r.created_at, nowMs), duration && duration !== "—" ? duration : null]
    .filter(Boolean).join(" · ");
  const notes = [];
  if (r.summary_note === RESEARCH.summaryNotes.missing) notes.push(TEXT.summaryMissing);
  if (r.summary_note === RESEARCH.summaryNotes.truncated) notes.push(TEXT.summaryTruncated);
  const missing = arr(r.format_warnings).filter((w) => typeof w === "string");
  if (missing.length) notes.push("The report is missing: " + missing.join(", ") + ".");
  return { line, notes };
}

/** One entry of the Runs picker: `Oct 26, 3:29 pm · opus · high · done`. */
export function runOption(summary, nowMs) {
  const s = obj(summary) || {};
  const label = [clock(s.created_at, nowMs), str(s.model), str(s.effort), runPhase(s) === "done" ? null : runPhase(s)]
    .filter(Boolean).join(" · ");
  return { id: s.run_id, label: label || String(s.run_id) };
}
