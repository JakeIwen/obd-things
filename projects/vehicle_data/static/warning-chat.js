"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const dialog = byId("warning-chat-dialog");
  const storage = {
    get(key) { try { return localStorage.getItem(`van-warning-chat.${key}`); } catch (_error) { return null; } },
    set(key, value) { try { localStorage.setItem(`van-warning-chat.${key}`, value); } catch (_error) {} },
    remove(key) { try { localStorage.removeItem(`van-warning-chat.${key}`); } catch (_error) {} },
  };
  const randomId = (bytes) => Array.from(crypto.getRandomValues(new Uint8Array(bytes)),
    (value) => value.toString(16).padStart(2, "0")).join("");
  let client = storage.get("client");
  if (!/^[a-f0-9]{64}$/.test(client || "")) {
    client = randomId(32);
    storage.set("client", client);
  }
  let key = storage.get("access") || "";
  let enabled = false;
  let selected = null;
  let chat = null;
  let generation = 0;
  let timer = null;
  let opener = null;
  let requesting = false;
  let renderSignature = null;
  let pendingSend = null;
  let options = null;
  let proposalSignature = null;
  const settings = () => ({model: byId("warning-chat-model").value, effort: byId("warning-chat-effort").value});
  function fillEfforts() {
    const previous = byId("warning-chat-effort").value || "high";
    const model = byId("warning-chat-model").value;
    const values = options?.models?.[model === "default" ? options.default_model : model] || ["high"];
    byId("warning-chat-effort").replaceChildren(...values.map((value) => new Option(value[0].toUpperCase() + value.slice(1), value)));
    byId("warning-chat-effort").value = values.includes(previous) ? previous : values.includes("high") ? "high" : values[0];
  }
  async function loadSettings() {
    const info = await request("GET", "/v1/assistant/status");
    if (!info.settings) throw new Error("Restart the updated advisor service to enable model controls and warning setup");
    options = info.settings;
    renderSavedWarnings(info);
    const previous = byId("warning-chat-model").value;
    byId("warning-chat-model").replaceChildren(new Option(`Default (${options.default_model})`, "default"),
      ...Object.keys(options.models).map((model) => new Option(model, model)));
    byId("warning-chat-model").value = previous === "default" || options.models[previous] ? previous : "default";
    fillEfforts();
  }
  function renderSavedWarnings(info) {
    const list = byId("warning-chat-saved-list");
    list.replaceChildren();
    byId("warning-chat-saved").hidden = !info.warnings?.length && !info.warning_config_error;
    if (info.warning_config_error) list.textContent = info.warning_config_error;
    for (const warning of info.warnings || []) {
      const row = document.createElement("p");
      const text = document.createElement("span");
      text.textContent = `${warning.rule.title} · ${warning.loaded ? "Loaded by broker" : "Saved; waiting for upgraded broker"} `;
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "Remove";
      button.addEventListener("click", async () => {
        if (!window.confirm(`Remove “${warning.rule.title}”?`)) return;
        const current = generation;
        button.disabled = true;
        try {
          const result = await request("POST", `/v1/assistant/warnings/${warning.id}/remove`, {confirmed: true});
          if (generation === current) { renderSavedWarnings(result); await poll(); }
        } catch (error) { if (generation === current) failed(error); }
        finally { button.disabled = false; }
      });
      row.append(text, button);
      list.append(row);
    }
  }

  function status(message) { byId("warning-chat-status").textContent = message; }
  function timingText(diagnostics) {
    if (!diagnostics) return "";
    const phases = {preparing: "Preparing", starting: "Starting Codex", thread_ready: "Thread ready",
      waiting_for_model: "Waiting for response", receiving_response: "Response activity received", finishing: "Finishing"};
    const seconds = (value) => `${(value / 1000).toFixed(1)}s`;
    const phase = phases[diagnostics.phase] || "Timing available";
    const outcomes = {timeout: "Timed out", cancelled: "Stopped", error: "Failed"};
    const items = [diagnostics.outcome === "complete" ? "Completed"
      : outcomes[diagnostics.outcome] ? `${outcomes[diagnostics.outcome]}: ${phase}` : phase];
    const milestones = diagnostics.milestones_ms || {};
    for (const [key, label] of [["process_started_ms", "Launched"], ["thread_started_ms", "Thread ready"],
      ["turn_started_ms", "Turn started"], ["first_model_item_ms", "First response"],
      ["turn_completed_ms", "Turn completed"], ["process_exit_ms", "Exited"]]) {
      if (Number.isFinite(milestones[key])) items.push(`${label} +${seconds(milestones[key])}`);
    }
    for (const [key, label] of [["elapsed_ms", "Elapsed"], ["child_cpu_ms", "CPU"], ["cgroup_throttled_ms", "Throttled"]]) {
      if (Number.isFinite(diagnostics[key])) items.push(`${label} ${seconds(diagnostics[key])}`);
    }
    if (diagnostics.signals?.length) items.push(`Signals: ${diagnostics.signals.join(", ")}`);
    return items.join(" · ");
  }
  function busy() { return chat?.turns?.slice(-1)[0]?.state === "running"; }
  function controls() {
    byId("warning-chat-send").disabled = requesting || busy() || !chat;
    byId("warning-chat-stop").hidden = !busy();
    byId("warning-chat-stop").disabled = requesting;
    byId("warning-chat-new").disabled = requesting || busy() || !chat;
    for (const id of ["warning-chat-model", "warning-chat-effort", "warning-chat-apply", "warning-chat-remove"]) {
      byId(id).disabled = requesting || busy();
    }
    const last = chat?.turns?.slice(-1)[0];
    byId("warning-chat-retry").hidden = !last || !["error", "cancelled"].includes(last.state);
  }

  async function request(method, path, payload) {
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 20000);
    try {
      const response = await fetch(path, {
        method, cache: "no-store", signal: abort.signal,
        headers: {"Content-Type": "application/json", "X-Van-Assistant-Key": key, "X-Van-Chat-Client": client},
        ...(payload ? {body: JSON.stringify(payload)} : {}),
      });
      const result = await response.json();
      if (!response.ok) {
        const error = new Error(result.detail || "The local assistant is unavailable");
        error.status = response.status;
        throw error;
      }
      return result;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The assistant connection timed out. Reopen this event to check its answer");
      throw error;
    } finally { clearTimeout(timeout); }
  }

  function failed(error) {
    status(error.message || "The local assistant is unavailable");
    byId("warning-chat-reconnect").hidden = error.status === 401;
    if (error.status === 401) {
      key = "";
      storage.remove("access");
      byId("warning-chat-connect").hidden = false;
      byId("warning-chat-code").focus();
    }
    controls();
  }

  function render(result) {
    chat = result;
    byId("warning-chat-reconnect").hidden = true;
    byId("warning-chat-title").textContent = result.title;
    byId("warning-chat-evidence").textContent =
      result.event.kind === "setup" ? "Propose a trigger, review its settings, then approve it. No vehicle actions."
        : `Evidence captured ${new Date(result.evidence_at).toLocaleString()} · Explanation only; no vehicle actions`;
    // Progress heartbeats update status/timing only, not the whole transcript.
    const signature = JSON.stringify(result.turns.map(({diagnostics, ...turn}) => turn));
    if (signature !== renderSignature) {
      renderSignature = signature;
      const log = byId("warning-chat-messages");
      const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
      log.replaceChildren();
      for (const turn of result.turns) {
        for (const [role, content] of [["You", turn.user], ["Codex", turn.assistant || turn.error || "Reviewing the event…"]]) {
          const message = document.createElement("article");
          message.className = `warning-chat-message ${role === "You" ? "from-user" : "from-codex"}`;
          const heading = document.createElement("h3");
          heading.textContent = role === "Codex" && turn.settings
            ? `Codex · ${turn.settings.resolved_model} / ${turn.settings.effort}` : role;
          const text = document.createElement("p");
          text.textContent = content;
          message.append(heading, text);
          log.append(message);
        }
      }
      if (atBottom || result.turns.length === 1) log.scrollTop = log.scrollHeight;
    }
    const last = result.turns.slice(-1)[0];
    byId("warning-chat-timing").hidden = !last?.diagnostics;
    byId("warning-chat-timing-detail").textContent = timingText(last?.diagnostics);
    if (last?.state === "error" && last.diagnostics) byId("warning-chat-timing").open = true;
    const proposal = last?.state === "complete" ? last.proposal : null;
    byId("warning-chat-proposal").hidden = !proposal;
    if (proposal) {
      const signature = JSON.stringify([last.proposal_id, last.applied === true]);
      if (signature !== proposalSignature) byId("warning-chat-proposal").open = !last.applied;
      proposalSignature = signature;
      byId("warning-chat-rule").textContent = `${proposal.title}\n${proposal.metric}: ${proposal.operator.replaceAll("_", " ")} ${proposal.threshold} ${last.proposal_unit || ""}\n${proposal.persistence_observations} distinct observations within ${proposal.window_seconds}s; sample age/gap ≤ ${proposal.max_age_seconds}s.\n${proposal.engine_running ? "Engine running required (fresh RPM > 400)." : "Any engine state."}`;
      byId("warning-chat-apply").hidden = last.applied === true;
      byId("warning-chat-remove").hidden = last.applied !== true;
      byId("warning-chat-applied").textContent = last.applied ? "Saved on the Pi. The upgraded broker loads approved rules on its next evaluation; this does not restart it." : "Not installed.";
    }
    status(busy() ? (last?.diagnostics ? timingText(last.diagnostics).split(" · ")[0] + "… (up to 5 minutes)"
      : "Codex is working… (up to 5 minutes)") : last?.error || "Ready for a follow-up");
    controls();
    schedulePoll();
  }

  function schedulePoll() {
    clearTimeout(timer);
    if (busy() && !dialog.hidden && document.visibilityState !== "hidden") {
      timer = setTimeout(poll, 1000);
    }
  }

  async function poll() {
    if (!chat || dialog.hidden) return;
    const current = generation;
    try {
      const result = await request("GET", `/v1/assistant/chats/${chat.id}`);
      if (generation === current && !dialog.hidden) render(result);
    } catch (error) { if (generation === current) failed(error); }
  }

  async function connectEvent(newChat = false) {
    const current = generation;
    if (!enabled) {
      status("This dashboard server needs the local warning-chat backend enabled");
      return;
    }
    if (!key) {
      byId("warning-chat-connect").hidden = false;
      status("Connect this browser once using the assistant access code from the Pi");
      byId("warning-chat-code").focus();
      return;
    }
    requesting = true;
    controls();
    status("Opening the event conversation…");
    try {
      await loadSettings();
      if (generation !== current || dialog.hidden) return;
      // The worker returns an existing event conversation for this browser.
      const result = await request("POST", "/v1/assistant/chats", {
        event: selected, request_id: randomId(16), settings: settings(), ...(newChat ? {new_chat: true} : {}),
      });
      if (generation !== current || dialog.hidden) return;
      byId("warning-chat-connect").hidden = true;
      render(result);
    } catch (error) { if (generation === current) failed(error); }
    finally { if (generation === current) { requesting = false; controls(); } }
  }

  function open(event, title, trigger) {
    clearTimeout(timer);
    generation += 1;
    selected = event;
    chat = null;
    requesting = false;
    opener = trigger;
    renderSignature = null;
    proposalSignature = null;
    dialog.hidden = false;
    document.body.classList.add("warning-chat-open");
    byId("warning-chat-title").textContent = title;
    byId("warning-chat-evidence").textContent = "Local Codex · Uses your ChatGPT sign-in and requires internet access";
    byId("warning-chat-messages").replaceChildren();
    byId("warning-chat-proposal").hidden = true;
    byId("warning-chat-saved").hidden = true;
    byId("warning-chat-timing").hidden = true;
    byId("warning-chat-timing").open = false;
    byId("warning-chat-input").value = "";
    byId("warning-chat-connect").hidden = true;
    byId("warning-chat-reconnect").hidden = true;
    byId("warning-chat-close").focus();
    controls();
    void connectEvent();
  }

  function close() {
    clearTimeout(timer);
    generation += 1;
    dialog.hidden = true;
    document.body.classList.remove("warning-chat-open");
    if (opener?.isConnected) opener.focus();
  }

  async function send(retry = false) {
    if (!chat || requesting || busy()) return;
    const input = byId("warning-chat-input");
    const message = input.value.trim();
    if (!retry && !message) return;
    const current = generation;
    const signature = JSON.stringify([chat.id, retry, message, settings()]);
    if (pendingSend?.signature !== signature) {
      pendingSend = {signature, payload: {request_id: randomId(16), settings: settings(), ...(retry ? {retry: true} : {message})}};
    }
    requesting = true;
    controls();
    try {
      const result = await request("POST", `/v1/assistant/chats/${chat.id}/messages`,
        pendingSend.payload);
      if (generation !== current) return;
      pendingSend = null;
      if (!retry) input.value = "";
      render(result);
    } catch (error) { if (generation === current) failed(error); }
    finally { if (generation === current) { requesting = false; controls(); } }
  }

  byId("warning-chat-close").addEventListener("click", close);
  byId("warning-chat-model").addEventListener("change", fillEfforts);
  byId("warning-add").addEventListener("click", () => open({kind: "setup", id: "new-warning"}, "Add Early Warning", byId("warning-add")));
  async function applyWarning(remove) {
    const turn = chat?.turns?.slice(-1)[0];
    if (!turn?.proposal_id || requesting || busy()) return;
    if (!window.confirm(remove ? "Remove this custom warning?" : "Add this exact monitoring rule? It will use recorded telemetry only.")) return;
    const current = generation;
    requesting = true;
    controls();
    try {
      const result = await request("POST", `/v1/assistant/chats/${chat.id}/${remove ? "remove-warning" : "apply-warning"}`,
        {proposal_id: turn.proposal_id, confirmed: true});
      if (current === generation) { render(result); await loadSettings(); }
    } catch (error) { if (current === generation) failed(error); }
    finally { if (current === generation) { requesting = false; controls(); } }
  }
  byId("warning-chat-apply").addEventListener("click", () => void applyWarning(false));
  byId("warning-chat-remove").addEventListener("click", () => void applyWarning(true));
  byId("warning-chat-new").addEventListener("click", () => {
    if (!chat || requesting || busy() || !window.confirm("Refresh the event evidence and start a new explanation? This conversation and its original evidence remain saved on the Pi.")) return;
    clearTimeout(timer);
    generation += 1;
    chat = null;
    renderSignature = null;
    byId("warning-chat-messages").replaceChildren();
    void connectEvent(true);
  });
  dialog.addEventListener("click", (event) => { if (event.target === dialog) close(); });
  document.addEventListener("keydown", (event) => {
    if (dialog.hidden) return;
    if (event.key === "Escape") { event.preventDefault(); close(); }
    if (event.key === "Tab") {
      const focusable = [...dialog.querySelectorAll("button,input,textarea,select")]
        .filter((element) => !element.disabled && element.getClientRects().length);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  byId("warning-chat-connect").addEventListener("submit", (event) => {
    event.preventDefault();
    key = byId("warning-chat-code").value.trim();
    if (!/^[A-Za-z0-9_-]{32,128}$/.test(key)) { status("Enter the complete assistant access code"); return; }
    storage.set("access", key);
    byId("warning-chat-code").value = "";
    void connectEvent();
  });
  byId("warning-chat-form").addEventListener("submit", (event) => { event.preventDefault(); void send(); });
  byId("warning-chat-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); }
  });
  byId("warning-chat-retry").addEventListener("click", () => void send(true));
  byId("warning-chat-reconnect").addEventListener("click", () => {
    if (chat) void poll(); else void connectEvent();
  });
  byId("warning-chat-stop").addEventListener("click", async () => {
    if (!chat || requesting) return;
    const current = generation;
    requesting = true;
    controls();
    try {
      const result = await request("POST", `/v1/assistant/chats/${chat.id}/cancel`, {});
      if (generation === current) render(result);
    } catch (error) { if (generation === current) failed(error); }
    finally { if (generation === current) { requesting = false; controls(); } }
  });
  document.addEventListener("visibilitychange", () => {
    clearTimeout(timer);
    if (!dialog.hidden && document.visibilityState !== "hidden") void poll();
  });

  window.WarningChat = {
    open,
    configure(web) {
      if (Object.hasOwn(web, "warning_chat_enabled")) enabled = web.warning_chat_enabled === true;
      byId("warning-add").hidden = !enabled;
    },
    attach(card, event, title) {
      if (!event?.id) return;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "warning-explain";
      button.textContent = "Ask Codex";
      button.setAttribute("aria-label", `Ask Codex about ${title}`);
      button.addEventListener("click", () => open(event, title, button));
      card.classList.add("warning-clickable");
      card.addEventListener("click", (click) => {
        if (click.target.closest("button,a,input,select,textarea,summary") || window.getSelection()?.toString()) return;
        open(event, title, button);
      });
      card.append(button);
    },
  };
})();
