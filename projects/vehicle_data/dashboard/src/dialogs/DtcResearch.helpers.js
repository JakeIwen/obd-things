/**
 * The research dialog's whole behaviour without DOM. DtcResearch.jsx renders `snapshot()` and
 * forwards taps; `onChange(snapshot)` fires after every state change. Dependencies (fetch, timers,
 * storage, randomness, visibility, clock) are injected so node can drive it.
 *
 * States of `snapshot().phase`: `loading`, `pairing` (no access code in this browser),
 * `unavailable` (no research on this Pi), `setup` (no run yet, or New research), then the shown
 * run's own state: `running`, `done`, `failed`, `cancelled`. `busy` is separate: a done run is
 * busy while a follow-up runs.
 */

import { buildRequest, createStorage, ensureClientId, isValidAccessCode, randomId, ACCESS_TEXT } from "../assistantAccess.js";
import {
  RESEARCH, TEXT, askBody, cancelBody, codePath, errorText, needsRunFetch, newRequestId, progressLine, progressPath,
  questionProblem, runFooter, runOption, runPath, runPhase, shouldPoll, startBody, turnRows,
} from "../dtcResearch.helpers.js";
import { choiceLabel, chooseEffort, chooseOption, findModel, loadChoice, resolveChoice, saveChoice, validChoice } from "../components/modelPicker.helpers.js";

class ResearchError extends Error {
  constructor(status, reason, message) {
    super(message);
    this.status = status;
    this.reason = reason;
  }
}

const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : null);

export function createResearchController(deps) {
  const {
    fetch: doFetch,
    storage = createStorage(),
    random = randomId,
    choiceBackend,
    setTimeout: setT = globalThis.setTimeout,
    clearTimeout: clearT = globalThis.clearTimeout,
    isVisible = () => true,
    now = () => Date.now(),
    AbortController: Abort = globalThis.AbortController,
    onChange = () => {},
    onFinished = () => {},
  } = deps;
  const client = ensureClientId(storage, random);
  const s = {
    key: storage.get("access") || "",
    opened: false,
    target: null,
    available: null,
    pairing: false,
    loading: false,
    catalog: null,
    modelsTries: 0,
    modelsTimer: null,
    choice: null,
    runs: [],
    run: null,
    progress: null,
    forceNew: false,
    requesting: false,
    error: null,
    pairingError: null,
    pending: null,
    draft: "",
    pollTimer: null,
    generation: 0,
  };

  const busy = () => Boolean(s.run && s.run.busy === true);

  function snapshot() {
    const run = s.run;
    const refreshing = Boolean(s.catalog && s.catalog.refreshing === true);
    let phase = "setup";
    if (!s.opened) phase = "closed";
    else if (s.pairing) phase = "pairing";
    else if (s.available === false) phase = "unavailable";
    else if (s.loading) phase = "loading";
    else if (run && !s.forceNew) phase = runPhase(run);
    const showingRun = phase === "running" || phase === "done" || phase === "failed" || phase === "cancelled";
    const nowMs = now();
    const model = run ? findModel(s.catalog, run.provider, run.model) : null;
    return {
      phase,
      busy: showingRun && busy(),
      target: s.target,
      catalog: s.catalog,
      refreshing,
      choice: s.choice,
      noModels: Boolean(s.catalog) && !refreshing && !s.choice,
      canStart: phase === "setup" && !s.requesting && !refreshing && validChoice(s.choice, s.catalog),
      requesting: s.requesting,
      runs: s.runs.map((r) => runOption(r, nowMs)),
      selectedRunId: run ? run.run_id : null,
      canNew: showingRun && !busy() && !s.requesting,
      canBack: phase === "setup" && Boolean(run),
      showStop: showingRun && busy(),
      canAsk: showingRun && run.can_continue === true && !busy() && !s.requesting,
      progress: showingRun && busy() ? progressLine(s.progress && s.progress.run_id === run.run_id ? s.progress : run) : null,
      turns: showingRun ? turnRows(run, nowMs) : [],
      footer: showingRun ? runFooter(run, model ? model.label : null, nowMs) : { line: "", notes: [] },
      runError: showingRun && !busy() && (phase === "failed" || phase === "cancelled")
        ? errorText(phase === "cancelled" ? "cancelled" : run.reason, run.error, TEXT.working) : null,
      draft: s.draft,
      error: s.error,
      pairingError: s.pairingError,
      choiceText: choiceLabel(s.catalog, s.choice),
    };
  }

  function emit() {
    onChange(snapshot());
  }

  /* ---------- requests ---------- */

  async function call(method, path, payload) {
    const { url, init } = buildRequest(method, path, payload, { key: s.key, client });
    // One deadline covers the headers and the body; racing it means a fetch that ignores the
    // abort signal still ends the wait.
    const abort = Abort ? new Abort() : null;
    if (abort) init.signal = abort.signal;
    let timer = null;
    const deadline = new Promise((_, reject) => {
      timer = setT(() => {
        if (abort) abort.abort();
        reject(new ResearchError(0, "timeout", method === "GET" ? TEXT.timedOut : TEXT.timedOutPost));
      }, RESEARCH.requestTimeoutMs);
    });
    let response;
    let body = null;
    try {
      const exchange = (async () => {
        response = await doFetch(url, init);
        try { body = await response.json(); } catch { body = null; }
      })();
      exchange.catch(() => {});
      await Promise.race([exchange, deadline]);
    } catch (error) {
      if (error instanceof ResearchError) throw error;
      throw new ResearchError(0, "network", TEXT.unreachable);
    } finally {
      if (timer !== null) clearT(timer);
    }
    if (response.status === 401) {
      s.key = "";
      storage.remove("access");
      s.pairing = true;
      throw new ResearchError(401, "auth", ACCESS_TEXT.pairingPrompt);
    }
    const b = obj(body);
    // A route the Pi does not have answers 404 with no reason; an unknown run has one.
    if (response.status === 404 && !(b && b.reason)) throw new ResearchError(404, "no_research", TEXT.unavailable);
    if (!response.ok || !b || b.available !== true) {
      throw new ResearchError(response.status, b && b.reason, errorText(b && b.reason, b && b.detail));
    }
    return b;
  }

  /** Runs `work`, turns a failure into the dialog's state; true when it succeeded. */
  async function guarded(work) {
    try {
      await work();
      return true;
    } catch (error) {
      if (error && error.reason === "no_research") s.available = false;
      else if (!(error && error.status === 401)) s.error = error && error.message ? error.message : TEXT.unreachable;
      return false;
    }
  }

  /* ---------- polling ---------- */

  function stopPolling() {
    if (s.pollTimer !== null) clearT(s.pollTimer);
    s.pollTimer = null;
  }

  function schedulePoll() {
    stopPolling();
    if (!shouldPoll({ busy: busy(), open: s.opened, visible: isVisible() })) return;
    const generation = s.generation;
    s.pollTimer = setT(() => { s.pollTimer = null; void poll(generation); }, RESEARCH.progressPollMs);
  }

  async function poll(generation) {
    if (generation !== s.generation || !s.run) return;
    const runId = s.run.run_id;
    const wasBusy = busy();
    await guarded(async () => {
      const reply = await call("GET", progressPath(runId));
      if (generation !== s.generation || !s.run || s.run.run_id !== runId) return;
      s.progress = reply.progress;
      if (needsRunFetch(s.run, reply.progress)) {
        const full = await call("GET", runPath(runId));
        if (generation !== s.generation) return;
        adoptRun(full.run);
        if (wasBusy && !busy()) onFinished();
      }
      s.error = null;
    });
    if (generation !== s.generation) return;
    emit();
    schedulePoll();
  }

  function adoptRun(run) {
    s.run = run;
    s.runs = s.runs.some((r) => r.run_id === run.run_id)
      ? s.runs.map((r) => (r.run_id === run.run_id ? run : r))
      : [run, ...s.runs];
    s.progress = null;
  }

  /* ---------- the model catalog ---------- */

  function stopModelsTimer() {
    if (s.modelsTimer !== null) clearT(s.modelsTimer);
    s.modelsTimer = null;
  }

  async function loadModels(generation) {
    const reply = await call("GET", RESEARCH.routes.models);
    if (generation !== s.generation) return;
    s.catalog = reply;
    s.choice = resolveChoice(s.choice || loadChoice(choiceBackend), reply);
    stopModelsTimer();
    if (reply.refreshing === true && s.modelsTries < RESEARCH.modelsPollMax) {
      s.modelsTries += 1;
      s.modelsTimer = setT(() => {
        s.modelsTimer = null;
        void guarded(() => loadModels(generation)).then(emit);
      }, RESEARCH.modelsPollMs);
    }
  }

  /* ---------- public actions ---------- */

  async function load() {
    const generation = s.generation;
    s.loading = true;
    s.error = null;
    emit();
    const target = s.target;
    await guarded(async () => {
      const code = await call("GET", codePath(target.module_key, target.fca_display));
      if (generation !== s.generation) return;
      s.runs = Array.isArray(code.runs) ? code.runs : [];
      s.run = obj(code.selected);
      s.forceNew = false;
      s.available = true;
    });
    if (generation !== s.generation) return;
    s.loading = false;
    if (s.available !== false && !s.pairing) {
      s.modelsTries = 0;
      await guarded(() => loadModels(generation));
    }
    if (generation !== s.generation) return;
    emit();
    schedulePoll();
  }

  /** Open on one code: `{module_key, fca_display, code?, meaning?, moduleName?}`. */
  function open(target) {
    s.generation += 1;
    stopPolling();
    stopModelsTimer();
    Object.assign(s, { opened: true, target, run: null, runs: [], progress: null, error: null, forceNew: false, pending: null,
      requesting: false, pairing: false, pairingError: null, available: null, draft: "" });
    if (!s.key) {
      s.pairing = true;
      emit();
      return;
    }
    void load();
  }

  function close() {
    if (!s.opened) return;
    s.generation += 1;
    s.opened = false;
    stopPolling();
    stopModelsTimer();
    emit();
    onFinished();
  }

  function pair(code) {
    const trimmed = String(code || "").trim();
    if (!isValidAccessCode(trimmed)) {
      s.pairingError = ACCESS_TEXT.incompleteCode;
      emit();
      return false;
    }
    s.key = trimmed;
    storage.set("access", trimmed);
    s.pairing = false;
    s.pairingError = null;
    void load();
    return true;
  }

  function setChoice(choice) {
    if (!validChoice(choice, s.catalog)) return;
    s.choice = choice;
    saveChoice(choice, choiceBackend);
    emit();
  }

  const pickModel = (key) => setChoice(chooseOption(s.catalog, s.choice, key));
  const pickEffort = (effort) => setChoice(chooseEffort(s.catalog, s.choice, effort));

  /** Send one POST, reusing the request id while the same body is retried after a lost answer. */
  async function post(body) {
    const signature = JSON.stringify(body);
    if (!s.pending || s.pending.signature !== signature) s.pending = { signature, id: newRequestId(random) };
    return call("POST", RESEARCH.routes.post, { ...body, request_id: s.pending.id });
  }

  async function submit(makeBody) {
    if (s.requesting) return false;
    const generation = s.generation;
    s.requesting = true;
    s.error = null;
    emit();
    const ok = await guarded(async () => {
      const reply = await post(makeBody(""));
      if (generation !== s.generation) return;
      s.pending = null;
      s.forceNew = false;
      adoptRun(reply.run);
    });
    if (generation === s.generation) {
      s.requesting = false;
      emit();
      schedulePoll();
    }
    return ok;
  }

  async function start() {
    const snap = snapshot();
    if (!snap.canStart) return false;
    saveChoice(s.choice, choiceBackend);
    const ok = await submit(() => startBody(s.target, s.choice, ""));
    return ok;
  }

  async function ask(text) {
    const problem = questionProblem(text);
    if (problem) {
      s.error = problem;
      emit();
      return false;
    }
    if (!snapshot().canAsk) return false;
    // The draft stays in place until the server accepted the question, so a refusal can be retried.
    const ok = await submit(() => askBody(s.run.run_id, text, ""));
    if (ok) {
      s.draft = "";
      emit();
    }
    return ok;
  }

  function setDraft(text) {
    s.draft = String(text ?? "");
    emit();
  }

  async function cancel() {
    if (!s.run || !busy()) return false;
    return submit(() => cancelBody(s.run.run_id, ""));
  }

  async function selectRun(runId) {
    if (!s.run || s.run.run_id === runId) {
      s.forceNew = false;
      emit();
      return;
    }
    const generation = s.generation;
    await guarded(async () => {
      const reply = await call("GET", runPath(runId));
      if (generation !== s.generation) return;
      s.forceNew = false;
      adoptRun(reply.run);
    });
    if (generation !== s.generation) return;
    emit();
    schedulePoll();
  }

  function newResearch() {
    if (!s.run || busy()) return;
    s.forceNew = true;
    s.error = null;
    s.pending = null;
    stopPolling();
    emit();
    // A catalog that was refreshing when the dialog opened may have settled since.
    const generation = s.generation;
    s.modelsTries = 0;
    void guarded(() => loadModels(generation)).then(emit);
  }

  function back() {
    s.forceNew = false;
    emit();
    schedulePoll();
  }

  function visibilityChanged() {
    if (!s.opened) return;
    if (!isVisible()) stopPolling();
    else if (busy()) void poll(s.generation);
  }

  return {
    snapshot, open, close, pair, start, ask, setDraft, cancel, selectRun, newResearch, back, pickModel, pickEffort, setChoice, visibilityChanged,
  };
}
