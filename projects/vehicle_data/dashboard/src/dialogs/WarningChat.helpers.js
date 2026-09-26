/**
 * Warning-chat dialog helpers (no DOM). Ported from static/warning-chat.js.
 *
 * Everything here is pure or takes injectable dependencies (fetch, sleep, timers,
 * storage, randomness) so `node --test` can exercise it. `WarningChat.jsx` is a thin
 * renderer over `createChatController()`.
 *
 * Backend contract: docs/warning-chat.md and web.py `_assistant_request`:
 *   GET  /v1/assistant/status
 *   POST /v1/assistant/chats                          {event, request_id, settings, new_chat?}
 *   GET  /v1/assistant/chats/<32hex>
 *   POST /v1/assistant/chats/<32hex>/messages         {request_id, settings, message | retry:true}
 *   POST /v1/assistant/chats/<32hex>/cancel           {}
 *   POST /v1/assistant/chats/<32hex>/apply-warning    {proposal_id, confirmed:true}
 *   POST /v1/assistant/chats/<32hex>/remove-warning   {proposal_id, confirmed:true}
 *   POST /v1/assistant/warnings/<64hex>/remove        {confirmed:true}
 * Headers X-Van-Assistant-Key (access code) and X-Van-Chat-Client (random browser id),
 * both /[A-Za-z0-9_-]{32,128}/; 401 means "pair this browser", 503 with "disabled on this
 * listener" means the capability is off for this web listener.
 */
import { fmtTime } from "../format.js";

export const STORAGE_PREFIX = "van-warning-chat.";
export const STATUS_PATH = "/v1/assistant/status";
export const CHATS_PATH = "/v1/assistant/chats";
export const REQUEST_TIMEOUT_MS = 20000;
export const PENDING_RETRY_MS = 200;
export const PENDING_MAX_TRIES = 35;
export const POLL_MS = 1000;
export const MAX_QUESTION_CHARS = 2000;
export const ACCESS_CODE_RE = /^[A-Za-z0-9_-]{32,128}$/;
export const CLIENT_ID_RE = /^[a-f0-9]{64}$/;
export const CHAT_ID_RE = /^[a-f0-9]{32}$/;
export const WARNING_ID_RE = /^[a-f0-9]{64}$/;
export const SETUP_EVENT = Object.freeze({kind: "setup", id: "new-warning"});

/** Every user-visible string, kept identical to the old dialog where it had one. */
export const TEXT = Object.freeze({
  eyebrow: "ASK CODEX",
  defaultTitle: "Warning explanation",
  setupTitle: "Add Early Warning",
  intro: "Local Codex · Uses your ChatGPT sign-in and requires internet access",
  setupEvidence: "Propose a trigger, review its settings, then approve it. No vehicle actions.",
  pairingLabel: "Assistant access code",
  pairingHintBefore: "On the Pi, read ",
  pairingHintPath: "/var/lib/van-telemetry-advisor/access-code",
  pairingHintAfter: " and paste the code here. This is not an OpenAI API key.",
  pairingPrompt: "Connect this browser once using the assistant access code from the Pi",
  incompleteCode: "Enter the complete assistant access code",
  disabled: "This dashboard server needs the local warning-chat backend enabled",
  opening: "Opening the event conversation…",
  ready: "Ready for a follow-up",
  working: "Codex is working… (up to 5 minutes)",
  reviewing: "Reviewing the event…",
  unavailable: "The local assistant is unavailable",
  timedOut: "The assistant connection timed out. Reopen this event to check its answer",
  stillLoading: "The assistant is still preparing that request. Use Reconnect to check again",
  restartAdvisor: "Restart the updated advisor service to enable model controls and warning setup",
  noEvent: "This card has no saved event that Codex can explain",
  savedTitle: "Saved Custom Warnings",
  savedLoaded: "Loaded by broker",
  savedWaiting: "Saved; waiting for upgraded broker",
  applied: "Saved on the Pi. The upgraded broker loads approved rules on its next evaluation; this does not restart it.",
  notInstalled: "Not installed.",
  ownerReference: "Owner monitoring reference—not an OEM limit. Uses recorded telemetry only.",
  timingTitle: "Timing Details",
  timingNote: "Local Codex event timings; turn start is not a remote request acknowledgement.",
  proposalTitle: "Review Early Warning",
  footnote: "Conversations are saved on the Pi for this browser. Closing this dialog does not stop an answer.",
  confirmApply: "Add this exact monitoring rule? It will use recorded telemetry only.",
  confirmRemove: "Remove this custom warning?",
  confirmNewChat: "Refresh the event evidence and start a new explanation? This conversation and its original evidence remain saved on the Pi.",
  askLabel: "Ask a follow-up",
  askPlaceholder: "What does this mean for the van?",
});

export const confirmRemoveSaved = (title) => `Remove “${title}”?`;

/* ---------- storage and identity ---------- */

/** Per-origin localStorage wrapper; every access is guarded (private mode, denied, missing). */
export function createStorage(backend) {
  const store = () => (backend === undefined ? globalThis.localStorage : backend);
  return {
    get(key) {
      try {
        const value = store()?.getItem(STORAGE_PREFIX + key);
        return value === undefined ? null : value;
      } catch (_error) {
        return null;
      }
    },
    set(key, value) {
      try { store()?.setItem(STORAGE_PREFIX + key, value); } catch (_error) { /* ignore */ }
    },
    remove(key) {
      try { store()?.removeItem(STORAGE_PREFIX + key); } catch (_error) { /* ignore */ }
    },
  };
}

/** Lower-case hex string of `bytes` random bytes. */
export function randomId(bytes, cryptoLike = globalThis.crypto) {
  const values = new Uint8Array(bytes);
  if (cryptoLike && typeof cryptoLike.getRandomValues === "function") cryptoLike.getRandomValues(values);
  else for (let index = 0; index < bytes; index += 1) values[index] = Math.floor(Math.random() * 256);
  return Array.from(values, (value) => value.toString(16).padStart(2, "0")).join("");
}

/** The browser's random chat-client id (64 hex), created once and kept per origin. */
export function ensureClientId(storage, random = randomId) {
  let client = storage.get("client");
  if (!CLIENT_ID_RE.test(client || "")) {
    client = random(32);
    storage.set("client", client);
  }
  return client;
}

export const isValidAccessCode = (code) => ACCESS_CODE_RE.test(String(code || ""));

/* ---------- event selection and titles ---------- */

/**
 * Map the dialog props to the advisor's event selector.
 * `context` may carry an explicit `{event:{kind,id}}`, an `eventId` (saved episode), or the
 * assessment/quality object shown on the card.
 */
export function eventSelector(mode, context) {
  if (mode === "add-warning") return {kind: SETUP_EVENT.kind, id: SETUP_EVENT.id};
  const ctx = context || {};
  const explicit = ctx.event;
  if (explicit && typeof explicit.kind === "string" && explicit.id != null && explicit.id !== "") {
    return {kind: explicit.kind, id: String(explicit.id)};
  }
  if (ctx.eventId != null && ctx.eventId !== "") return {kind: "episode", id: String(ctx.eventId)};
  const assessment = ctx.assessment || {};
  if (assessment.episode_id != null) return {kind: "episode", id: String(assessment.episode_id)};
  if (assessment.incident_id != null) return {kind: "quality", id: String(assessment.incident_id)};
  if (typeof assessment.rule === "string" && assessment.rule) return {kind: "assessment", id: assessment.rule};
  return null;
}

export function dialogTitle(mode, context) {
  if (mode === "add-warning") return TEXT.setupTitle;
  // `summary` is what the dialog host passes from EventHistory's onAskCodex(eventId, summary).
  const title = context?.title || context?.summary || context?.assessment?.title;
  return typeof title === "string" && title.trim() ? title : TEXT.defaultTitle;
}

/** Stable string for the component's open effect: changes only when mode, event or title change. */
export const contextKey = (mode, context) => JSON.stringify([mode === "add-warning" ? "add-warning" : "explain", eventSelector(mode, context), dialogTitle(mode, context)]);

/* ---------- requests ---------- */

export function requestHeaders(key, client) {
  return {
    "Content-Type": "application/json",
    "X-Van-Assistant-Key": key || "",
    "X-Van-Chat-Client": client || "",
  };
}

export function buildRequest(method, path, payload, identity) {
  const init = {method, cache: "no-store", headers: requestHeaders(identity?.key, identity?.client)};
  if (payload !== undefined && payload !== null) init.body = JSON.stringify(payload);
  return {url: path, init};
}

export function chatPath(chatId, action) {
  if (!CHAT_ID_RE.test(String(chatId || ""))) throw new Error("Invalid conversation id");
  const base = `${CHATS_PATH}/${chatId}`;
  if (action === undefined) return base;
  if (!["messages", "cancel", "apply-warning", "remove-warning"].includes(action)) throw new Error("Invalid chat action");
  return `${base}/${action}`;
}

export function warningRemovePath(warningId) {
  if (!WARNING_ID_RE.test(String(warningId || ""))) throw new Error("Invalid warning id");
  return `/v1/assistant/warnings/${warningId}/remove`;
}

export function openChatBody(event, settings, requestId, newChat = false) {
  const body = {event, request_id: requestId, settings};
  if (newChat) body.new_chat = true;
  return body;
}

export function messageBody(settings, requestId, input) {
  const body = {request_id: requestId, settings};
  if (input && input.retry) body.retry = true;
  else body.message = input.message;
  return body;
}

export const applyBody = (proposalId) => ({proposal_id: proposalId, confirmed: true});
export const removeSavedBody = () => ({confirmed: true});

/** "ok" (2xx result), "pending" (202 + pending:true → re-request identically) or "error". */
export function classifyResponse(status, body) {
  if (status === 202 && body && body.pending === true) return "pending";
  if (status >= 200 && status < 300) return "ok";
  return "error";
}

export function requestError(status, body, fallback = TEXT.unavailable) {
  const detail = body && typeof body.detail === "string" && body.detail ? body.detail : fallback;
  const error = new Error(detail);
  if (status !== undefined) error.status = status;
  return error;
}

export function isDisabledError(error) {
  return error?.status === 503 && /disabled on this listener/i.test(error.message || "");
}

/**
 * JSON requester with the old 20 s deadline and 202-pending re-requests (200 ms, 35 tries).
 * `fetch`, `sleep` and the timers are injectable for tests.
 */
export function createRequester(deps) {
  const {
    fetch: doFetch,
    sleep,
    getKey,
    getClient,
    timeoutMs = REQUEST_TIMEOUT_MS,
    pollMs = PENDING_RETRY_MS,
    maxTries = PENDING_MAX_TRIES,
    AbortController: Abort = globalThis.AbortController,
    setTimeout: setT = globalThis.setTimeout,
    clearTimeout: clearT = globalThis.clearTimeout,
  } = deps;
  return async function request(method, path, payload) {
    const abort = Abort ? new Abort() : null;
    const timer = setT(() => { if (abort) abort.abort(); }, timeoutMs);
    try {
      for (let attempt = 1; ; attempt += 1) {
        const {url, init} = buildRequest(method, path, payload, {key: getKey(), client: getClient()});
        if (abort) init.signal = abort.signal;
        const response = await doFetch(url, init);
        let body = null;
        try { body = await response.json(); } catch (_error) { body = null; }
        const kind = classifyResponse(response.status, body);
        if (kind === "ok") {
          if (!body || typeof body !== "object") throw requestError(response.status, null);
          return body;
        }
        if (kind === "pending") {
          if (attempt >= maxTries) throw requestError(202, null, TEXT.stillLoading);
          await sleep(pollMs);
          continue;
        }
        throw requestError(response.status, body);
      }
    } catch (error) {
      if (error && error.name === "AbortError") throw new Error(TEXT.timedOut);
      throw error;
    } finally {
      clearT(timer);
    }
  };
}

/* ---------- formatting ---------- */

/** Evidence timestamp in the dashboard's fixed 12-hour local format (design 5.1, format.js). */
export const formatDateTime = (value, nowDate) => fmtTime(value, nowDate instanceof Date ? nowDate : undefined);

export function evidenceLine(chat, nowDate) {
  if (!chat) return TEXT.intro;
  if (chat.event && chat.event.kind === "setup") return TEXT.setupEvidence;
  return `Evidence captured ${formatDateTime(chat.evidence_at, nowDate)} · Explanation only; no vehicle actions`;
}

const PHASES = {
  preparing: "Preparing",
  starting: "Starting Codex",
  thread_ready: "Thread ready",
  waiting_for_model: "Waiting for response",
  receiving_response: "Response activity received",
  finishing: "Finishing",
};
const OUTCOMES = {timeout: "Timed out", cancelled: "Stopped", error: "Failed"};
const MILESTONES = [
  ["process_started_ms", "Launched"], ["thread_started_ms", "Thread ready"], ["turn_started_ms", "Turn started"],
  ["first_model_item_ms", "First response"], ["turn_completed_ms", "Turn completed"], ["process_exit_ms", "Exited"],
];
const TOTALS = [["elapsed_ms", "Elapsed"], ["child_cpu_ms", "CPU"], ["cgroup_throttled_ms", "Throttled"]];

/** One-line timing summary of a turn's diagnostics record (verbatim port). */
export function timingText(diagnostics) {
  if (!diagnostics) return "";
  const seconds = (value) => `${(value / 1000).toFixed(1)}s`;
  const phase = PHASES[diagnostics.phase] || "Timing available";
  const items = [diagnostics.outcome === "complete" ? "Completed"
    : OUTCOMES[diagnostics.outcome] ? `${OUTCOMES[diagnostics.outcome]}: ${phase}` : phase];
  const milestones = diagnostics.milestones_ms || {};
  for (const [key, label] of MILESTONES) {
    if (Number.isFinite(milestones[key])) items.push(`${label} +${seconds(milestones[key])}`);
  }
  for (const [key, label] of TOTALS) {
    if (Number.isFinite(diagnostics[key])) items.push(`${label} ${seconds(diagnostics[key])}`);
  }
  if (Array.isArray(diagnostics.signals) && diagnostics.signals.length) items.push(`Signals: ${diagnostics.signals.join(", ")}`);
  return items.join(" · ");
}

export const lastTurn = (chat) => (Array.isArray(chat?.turns) && chat.turns.length ? chat.turns[chat.turns.length - 1] : null);
export const isBusy = (chat) => lastTurn(chat)?.state === "running";
export const canRetry = (chat) => ["error", "cancelled"].includes(lastTurn(chat)?.state);

/** Signature of the transcript without progress heartbeats (diagnostics), so polls don't rebuild rows. */
export function transcriptSignature(chat) {
  if (!chat) return null;
  return JSON.stringify((chat.turns || []).map(({diagnostics: _diagnostics, ...turn}) => turn));
}

/** Rows to render: one "You" row and one "Codex" row per turn. */
export function transcriptRows(chat) {
  const rows = [];
  (chat?.turns || []).forEach((turn, index) => {
    rows.push({key: `${index}-you`, role: "you", kind: "user", heading: "You", text: String(turn.user || "")});
    const kind = turn.assistant ? "answer" : turn.error ? "error" : "progress";
    const heading = turn.settings ? `Codex · ${turn.settings.resolved_model} / ${turn.settings.effort}` : "Codex";
    rows.push({key: `${index}-codex`, role: "codex", kind, heading, text: turn.assistant || turn.error || TEXT.reviewing});
  });
  return rows;
}

export function proposalText(turn) {
  const proposal = turn?.proposal;
  if (!proposal) return "";
  const unit = turn.proposal_unit ? ` ${turn.proposal_unit}` : "";
  return [
    String(proposal.title || ""),
    `${proposal.metric}: ${String(proposal.operator || "").replace(/_/g, " ")} ${proposal.threshold}${unit}`,
    `${proposal.persistence_observations} distinct observations within ${proposal.window_seconds}s; sample age/gap ≤ ${proposal.max_age_seconds}s.`,
    proposal.engine_running ? "Engine running required (fresh RPM > 400)." : "Any engine state.",
  ].join("\n");
}

export const appliedText = (applied) => (applied ? TEXT.applied : TEXT.notInstalled);

export function statusText(chat) {
  const last = lastTurn(chat);
  if (isBusy(chat)) {
    return last?.diagnostics ? `${timingText(last.diagnostics).split(" · ")[0]}… (up to 5 minutes)` : TEXT.working;
  }
  return last?.error || TEXT.ready;
}

/* ---------- model / effort choices ---------- */

const capitalize = (value) => value.charAt(0).toUpperCase() + value.slice(1);

export function modelChoices(options) {
  if (!options || !options.models) return [{value: "default", label: "Default"}];
  return [
    {value: "default", label: `Default (${options.default_model})`},
    ...Object.keys(options.models).map((model) => ({value: model, label: model})),
  ];
}

export function effortValues(options, model) {
  const resolved = model === "default" ? options?.default_model : model;
  const values = options?.models?.[resolved];
  return Array.isArray(values) && values.length ? values : ["high"];
}

export const effortChoices = (options, model) => effortValues(options, model).map((value) => ({value, label: capitalize(value)}));

/** Keep the previous choice when the catalog still allows it; otherwise the old fallbacks. */
export function normalizeChoice(options, model, effort) {
  const nextModel = model === "default" || (options?.models && options.models[model]) ? model : "default";
  const values = effortValues(options, nextModel);
  const previous = effort || "high";
  const nextEffort = values.includes(previous) ? previous : values.includes("high") ? "high" : values[0];
  return {model: nextModel, effort: nextEffort};
}

/* ---------- saved custom warnings ---------- */

export function savedWarningsView(info) {
  const warnings = Array.isArray(info?.warnings) ? info.warnings : [];
  const raw = info?.warning_config_error;
  const error = raw ? String(raw) : null;
  return {
    visible: warnings.length > 0 || Boolean(error),
    error,
    rows: warnings.map((warning) => {
      const title = warning?.rule?.title || String(warning?.id || "");
      return {id: warning.id, title, text: `${title} · ${warning.loaded ? TEXT.savedLoaded : TEXT.savedWaiting}`};
    }),
  };
}

export const shouldPoll = ({busy, open, visible}) => Boolean(busy && open && visible);

/* ---------- controller ---------- */

const emptyTranscript = () => ({signature: null, rows: [], version: 0, newMessage: false});
const hiddenSaved = () => ({visible: false, error: null, rows: []});

/**
 * The dialog's whole behaviour without DOM. The component renders `snapshot()` and forwards
 * user actions; `onChange(snapshot)` fires after every state change.
 */
export function createChatController(deps) {
  const {
    fetch: doFetch,
    sleep = (ms) => new Promise((resolve) => globalThis.setTimeout(resolve, ms)),
    storage = createStorage(),
    random = randomId,
    setTimeout: setT = globalThis.setTimeout,
    clearTimeout: clearT = globalThis.clearTimeout,
    isVisible = () => true,
    now = () => new Date(),
    onChange = () => {},
    onWarningsChanged = () => {},
  } = deps;
  const client = ensureClientId(storage, random);
  const s = {
    key: storage.get("access") || "",
    opened: false,
    mode: "explain",
    event: null,
    title: TEXT.defaultTitle,
    chat: null,
    options: null,
    model: "default",
    effort: "high",
    requesting: false,
    status: "",
    statusError: false,
    pairing: false,
    reconnect: false,
    disabled: false,
    saved: hiddenSaved(),
    transcript: emptyTranscript(),
    timingOpen: false,
    proposalSignature: null,
    proposalOpen: false,
    generation: 0,
    timer: null,
    pendingSend: null,
    removing: new Set(),
  };
  const request = createRequester({
    fetch: doFetch, sleep, getKey: () => s.key, getClient: () => client,
    setTimeout: setT, clearTimeout: clearT, AbortController: deps.AbortController,
    timeoutMs: deps.timeoutMs, pollMs: deps.pollMs, maxTries: deps.maxTries,
  });
  const settings = () => ({model: s.model, effort: s.effort});

  function snapshot() {
    const busy = isBusy(s.chat);
    const last = lastTurn(s.chat);
    const locked = s.requesting || busy;
    const proposal = last?.state === "complete" ? last.proposal : null;
    return {
      open: s.opened,
      mode: s.mode,
      title: s.title,
      evidence: evidenceLine(s.chat, now()),
      status: s.status,
      statusError: s.statusError,
      pairing: s.pairing,
      reconnect: s.reconnect,
      disabled: s.disabled,
      requesting: s.requesting,
      busy,
      locked,
      hasChat: Boolean(s.chat),
      canSend: Boolean(s.chat) && !locked,
      canNew: Boolean(s.chat) && !locked,
      showStop: busy,
      stopEnabled: !s.requesting,
      showRetry: canRetry(s.chat),
      models: modelChoices(s.options),
      model: s.model,
      efforts: effortChoices(s.options, s.model),
      effort: s.effort,
      saved: {...s.saved, rows: s.saved.rows.map((row) => ({...row, removing: s.removing.has(row.id)}))},
      transcript: s.transcript,
      timing: {visible: Boolean(last?.diagnostics), text: timingText(last?.diagnostics), open: s.timingOpen},
      proposal: proposal
        ? {visible: true, text: proposalText(last), applied: last.applied === true, appliedText: appliedText(last.applied === true), open: s.proposalOpen}
        : {visible: false, text: "", applied: false, appliedText: "", open: false},
    };
  }
  const emit = () => onChange(snapshot());

  function stopTimer() {
    if (s.timer !== null) clearT(s.timer);
    s.timer = null;
  }

  function schedulePoll() {
    stopTimer();
    if (shouldPoll({busy: isBusy(s.chat), open: s.opened, visible: isVisible()})) {
      s.timer = setT(() => { s.timer = null; void poll(); }, POLL_MS);
    }
  }

  function render(result) {
    s.chat = result;
    s.reconnect = false;
    if (typeof result.title === "string" && result.title) s.title = result.title;
    const signature = transcriptSignature(result);
    if (signature !== s.transcript.signature) {
      const rows = transcriptRows(result);
      const grew = rows.length > s.transcript.rows.length || (result.turns || []).length === 1;
      s.transcript = {signature, rows, version: s.transcript.version + 1, newMessage: grew};
    }
    const last = lastTurn(result);
    if (last?.state === "error" && last.diagnostics) s.timingOpen = true;
    const proposal = last?.state === "complete" ? last.proposal : null;
    if (proposal) {
      const proposalSignature = JSON.stringify([last.proposal_id, last.applied === true]);
      if (proposalSignature !== s.proposalSignature) s.proposalOpen = !last.applied;
      s.proposalSignature = proposalSignature;
    }
    s.status = statusText(result);
    s.statusError = false;
    emit();
    schedulePoll();
  }

  function failed(error) {
    const hadKey = Boolean(s.key);
    s.disabled = isDisabledError(error);
    s.status = s.disabled ? TEXT.disabled : (error && error.message) || TEXT.unavailable;
    s.statusError = true;
    s.reconnect = !s.disabled && error?.status !== 401;
    if (error?.status === 401) {
      s.key = "";
      storage.remove("access");
      s.pairing = true;
      if (!hadKey) s.status = TEXT.pairingPrompt;
    }
    emit();
  }

  async function loadSettings() {
    const info = await request("GET", STATUS_PATH);
    if (!info.settings) throw new Error(TEXT.restartAdvisor);
    s.options = info.settings;
    s.saved = savedWarningsView(info);
    const choice = normalizeChoice(s.options, s.model, s.effort);
    s.model = choice.model;
    s.effort = choice.effort;
  }

  async function poll() {
    if (!s.chat || !s.opened) return;
    const current = s.generation;
    try {
      const result = await request("GET", chatPath(s.chat.id));
      if (current === s.generation && s.opened) render(result);
    } catch (error) {
      if (current === s.generation) failed(error);
    }
  }

  async function connect(newChat = false) {
    if (!s.opened) return;
    if (!s.event) {
      s.status = TEXT.noEvent;
      s.statusError = true;
      emit();
      return;
    }
    const current = s.generation;
    s.requesting = true;
    s.status = TEXT.opening;
    s.statusError = false;
    emit();
    try {
      await loadSettings();
      if (current !== s.generation || !s.opened) return;
      // The worker returns this browser's existing conversation for the same event unless new_chat.
      const result = await request("POST", CHATS_PATH, openChatBody(s.event, settings(), random(16), newChat));
      if (current !== s.generation || !s.opened) return;
      s.pairing = false;
      s.disabled = false;
      render(result);
    } catch (error) {
      if (current === s.generation) failed(error);
    } finally {
      if (current === s.generation) {
        s.requesting = false;
        emit();
      }
    }
  }

  function open(mode, context) {
    stopTimer();
    s.generation += 1;
    s.opened = true;
    s.mode = mode === "add-warning" ? "add-warning" : "explain";
    s.event = eventSelector(s.mode, context);
    s.title = dialogTitle(s.mode, context);
    s.chat = null;
    s.requesting = false;
    s.status = "";
    s.statusError = false;
    s.pairing = false;
    s.reconnect = false;
    s.disabled = false;
    s.saved = hiddenSaved();
    s.transcript = emptyTranscript();
    s.timingOpen = false;
    s.proposalSignature = null;
    s.proposalOpen = false;
    s.pendingSend = null;
    emit();
    void connect();
  }

  function close() {
    stopTimer();
    s.generation += 1;
    s.opened = false;
    s.requesting = false;
    emit();
  }

  function pair(code) {
    const trimmed = String(code || "").trim();
    if (!isValidAccessCode(trimmed)) {
      s.status = TEXT.incompleteCode;
      s.statusError = true;
      emit();
      return false;
    }
    s.key = trimmed;
    storage.set("access", trimmed);
    void connect();
    return true;
  }

  async function send(message, retry = false) {
    if (!s.chat || s.requesting || isBusy(s.chat)) return false;
    const text = String(message || "").trim();
    if (!retry && !text) return false;
    const current = s.generation;
    const signature = JSON.stringify([s.chat.id, retry, text, settings()]);
    if (!s.pendingSend || s.pendingSend.signature !== signature) {
      s.pendingSend = {signature, payload: messageBody(settings(), random(16), retry ? {retry: true} : {message: text})};
    }
    s.requesting = true;
    emit();
    try {
      const result = await request("POST", chatPath(s.chat.id, "messages"), s.pendingSend.payload);
      if (current !== s.generation) return false;
      s.pendingSend = null;
      render(result);
      return true;
    } catch (error) {
      if (current === s.generation) failed(error);
      return false;
    } finally {
      if (current === s.generation) {
        s.requesting = false;
        emit();
      }
    }
  }

  async function stop() {
    if (!s.chat || s.requesting) return false;
    const current = s.generation;
    s.requesting = true;
    emit();
    try {
      const result = await request("POST", chatPath(s.chat.id, "cancel"), {});
      if (current === s.generation) render(result);
      return true;
    } catch (error) {
      if (current === s.generation) failed(error);
      return false;
    } finally {
      if (current === s.generation) {
        s.requesting = false;
        emit();
      }
    }
  }

  function newChat() {
    if (!s.chat || s.requesting || isBusy(s.chat)) return false;
    stopTimer();
    s.generation += 1;
    s.chat = null;
    s.transcript = emptyTranscript();
    s.proposalSignature = null;
    s.pendingSend = null;
    emit();
    void connect(true);
    return true;
  }

  function reconnect() {
    if (s.chat) void poll();
    else void connect();
  }

  async function applyWarning(remove) {
    const turn = lastTurn(s.chat);
    if (!turn?.proposal_id || s.requesting || isBusy(s.chat)) return false;
    const current = s.generation;
    s.requesting = true;
    emit();
    try {
      const result = await request("POST", chatPath(s.chat.id, remove ? "remove-warning" : "apply-warning"), applyBody(turn.proposal_id));
      if (current !== s.generation) return false;
      render(result);
      onWarningsChanged();
      await loadSettings();
      if (current === s.generation) emit();
      return true;
    } catch (error) {
      if (current === s.generation) failed(error);
      return false;
    } finally {
      if (current === s.generation) {
        s.requesting = false;
        emit();
      }
    }
  }

  async function removeSaved(warningId) {
    if (!WARNING_ID_RE.test(String(warningId || "")) || s.removing.has(warningId)) return false;
    const current = s.generation;
    s.removing.add(warningId);
    emit();
    try {
      const result = await request("POST", warningRemovePath(warningId), removeSavedBody());
      if (current === s.generation) {
        s.saved = savedWarningsView(result);
        emit();
        onWarningsChanged();
        await poll();
      }
      return true;
    } catch (error) {
      if (current === s.generation) failed(error);
      return false;
    } finally {
      s.removing.delete(warningId);
      emit();
    }
  }

  function setModel(model) {
    const choice = normalizeChoice(s.options, model, s.effort);
    s.model = choice.model;
    s.effort = choice.effort;
    emit();
  }

  function setEffort(effort) {
    s.effort = normalizeChoice(s.options, s.model, effort).effort;
    emit();
  }

  function setTimingOpen(value) {
    s.timingOpen = Boolean(value);
    emit();
  }

  function setProposalOpen(value) {
    s.proposalOpen = Boolean(value);
    emit();
  }

  function visibilityChanged() {
    stopTimer();
    if (s.opened && isVisible()) void poll();
  }

  return {
    client,
    snapshot,
    open,
    close,
    pair,
    send: (message) => send(message, false),
    retry: () => send("", true),
    stop,
    newChat,
    reconnect,
    applyWarning: () => applyWarning(false),
    removeWarning: () => applyWarning(true),
    removeSaved,
    setModel,
    setEffort,
    setTimingOpen,
    setProposalOpen,
    poll,
    visibilityChanged,
  };
}
