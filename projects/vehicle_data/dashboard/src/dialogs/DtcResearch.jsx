/* Code research dialog: ask a model what a diagnostic code means on this van, read its report,
   keep asking follow-ups in the same session, and look back at earlier runs. Opened from a row of
   the Codes card (the lazily loaded chunk):
     openDialog("research", { target: {module_key, fca_display, code, meaning, moduleName} })
   Props: { open, target, onClose() }.
   All behaviour (pairing, requests, 1 s polling while a turn runs, model catalog, the run and its
   turns) lives in DtcResearch.helpers.js as createResearchController(); this file renders its
   snapshot. Model text is rendered through MarkdownView (safe subset, never HTML). No inline styles. */
import { useEffect, useRef, useState } from "preact/hooks";
import * as H from "./DtcResearch.helpers.js";
import { RESEARCH, TEXT } from "../dtcResearch.helpers.js";
import { refreshResearchList } from "../dtcResearch.js";
import { useModalDialog } from "./modalDialog.js";
import { useController } from "./useController.js";
import AssistantPairing from "../components/AssistantPairing.jsx";
import PairAnotherDevice from "../components/PairAnotherDevice.jsx";
import MarkdownView from "../components/MarkdownView.jsx";
import ModelPicker from "../components/ModelPicker.jsx";
import Popover from "../components/Popover.jsx";
import Listbox from "../components/Listbox.jsx";

const createController = (onChange) =>
  H.createResearchController({
    fetch: (url, init) => globalThis.fetch(url, init),
    isVisible: () => typeof document === "undefined" || document.visibilityState !== "hidden",
    onChange,
    onFinished: () => { void refreshResearchList(); },
  });

/** Earlier runs of this code, newest first; shown only when there is more than one. */
function RunsPicker({ runs, selected, onSelect }) {
  const [open, setOpen] = useState(false);
  const host = useRef(null);
  const options = runs.map((r) => ({ key: r.id, label: r.label }));
  return (
    <div class="research-runs" ref={host}>
      <button type="button" class="btn btn--small" aria-haspopup="listbox" aria-expanded={open ? "true" : "false"} onClick={() => setOpen(!open)}>
        {"Runs (" + runs.length + ")"}
      </button>
      <Popover open={open} onClose={() => setOpen(false)} hostRef={host} label="Runs" class="research-runs__popover">
        <Listbox id="research-runs" label="Runs" options={options} selected={selected ? [selected] : []}
          onChoose={(key) => { setOpen(false); onSelect(key); }} />
      </Popover>
    </div>
  );
}

function Turn({ row }) {
  return (
    <article class={"msg research-turn" + (row.role === "user" ? " msg--me" : "") + (row.error ? " msg--error" : "")}>
      <h3 class="chat-msg__who">{row.heading + (row.at ? " · " + row.at : "")}</h3>
      {row.error ? <p class="chat-msg__text">{row.error}</p> : null}
      {row.text ? (row.markdown ? <MarkdownView source={row.text} /> : <p class="chat-msg__text">{row.text}</p>) : null}
      {row.note ? <p class="muted research-turn__note">{row.note}</p> : null}
    </article>
  );
}

export default function DtcResearch({ open, target, onClose }) {
  const closeRef = useRef(null);
  const codeRef = useRef(null);
  const pinRef = useRef(null);
  const [ctrl, snap] = useController(createController, (c) => c.snapshot());

  const { dialogRef, close } = useModalDialog({
    open,
    onClose,
    focusRef: closeRef,
    deps: [target && target.module_key, target && target.fca_display],
    onOpen: () => ctrl.open(target),
    onNativeClose: () => ctrl.close(),
  });

  // Polling pauses while the page is hidden and resumes with an immediate poll when it returns.
  useEffect(() => {
    if (!open || typeof document === "undefined") return undefined;
    const handle = () => ctrl.visibilityChanged();
    document.addEventListener("visibilitychange", handle);
    return () => document.removeEventListener("visibilitychange", handle);
  }, [open, ctrl]);
  useEffect(() => () => ctrl.close(), [ctrl]);
  useEffect(() => {
    if (snap.phase === "pairing" && pinRef.current) pinRef.current.focus({ preventScroll: true });
  }, [snap.phase]);

  const submitCode = (ev) => {
    ev.preventDefault();
    const el = codeRef.current;
    if (ctrl.pair(el ? el.value : "") && el) el.value = "";
  };
  const submitQuestion = async (ev) => {
    if (ev) ev.preventDefault();
    await ctrl.ask(snap.draft);
  };
  const onInputKey = (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); void submitQuestion(); }
  };
  const onBackdrop = (ev) => { if (ev.target === dialogRef.current) close(); };
  const t = snap.target || target || {};
  const showingRun = ["running", "done", "failed", "cancelled"].includes(snap.phase);

  return (
    <dialog ref={dialogRef} class="dialog chat-dialog research-dialog" aria-labelledby="research-title" onClick={onBackdrop}>
      <header class="dialog__head">
        <div class="chat-head__text">
          <p class="chat-eyebrow">{TEXT.title}</p>
          <h2 id="research-title" class="dialog__title chat-title"><span class="num">{t.code || t.fca_display}</span>{t.moduleName ? " · " + t.moduleName : ""}</h2>
        </div>
        <button type="button" class="dialog__close" aria-label="Close research" ref={closeRef} onClick={close}>×</button>
      </header>
      <div class="dialog__body chat-body">
        {t.meaning ? <p class="chat-evidence">{t.meaning}</p> : null}
        {snap.phase === "pairing" ? <AssistantPairing codeRef={codeRef} pinRef={pinRef} onSubmit={submitCode} onCode={(code) => ctrl.pair(code)} id="research-code" /> : null}
        {snap.pairingError ? <p class="status-line status-line--error">{snap.pairingError}</p> : null}
        {snap.phase === "loading" ? <p class="muted">{TEXT.working}</p> : null}
        {snap.phase === "unavailable" ? <p class="status-line status-line--error" role="status">{TEXT.unavailable}</p> : null}

        {snap.phase === "setup" ? (
          <div class="research-setup">
            <p class="muted">{TEXT.setupLead}</p>
            <ModelPicker id="research-model" catalog={snap.catalog} choice={snap.choice} disabled={snap.requesting}
              onChange={(choice) => ctrl.setChoice(choice)} />
            <p class="muted research-sent">{TEXT.sentLine}</p>
            {snap.refreshing ? <p class="status-line" role="status">{TEXT.refreshing}</p> : null}
            {snap.noModels ? <p class="status-line" role="status">{TEXT.noModels}</p> : null}
            <div class="btn-row">
              <button type="button" class="btn btn--primary" disabled={!snap.canStart} onClick={() => void ctrl.start()}>
                {snap.requesting ? "Starting…" : "Start research"}
              </button>
              {snap.canBack ? <button type="button" class="btn" onClick={() => ctrl.back()}>Back to research</button> : null}
            </div>
          </div>
        ) : null}

        {showingRun ? (
          <>
            <div class="research-bar">
              {snap.runs.length > 1 ? <RunsPicker runs={snap.runs} selected={snap.selectedRunId} onSelect={(id) => void ctrl.selectRun(id)} /> : null}
              <button type="button" class="btn btn--small" disabled={!snap.canNew} onClick={() => ctrl.newResearch()}>New research</button>
            </div>
            <div class="research-thread" role="log" aria-label="Research conversation">
              {snap.turns.map((row) => <Turn key={row.key} row={row} />)}
            </div>
            {snap.progress ? (
              <p class="status-line research-progress" role="status">
                {snap.progress}
                {snap.showStop ? <button type="button" class="btn btn--small btn--danger research-stop" onClick={() => void ctrl.cancel()}>Stop</button> : null}
              </p>
            ) : null}
            {snap.runError ? <p class="status-line status-line--error" role="status">{snap.runError}</p> : null}
            {snap.canAsk ? (
              <form class="chat-form" onSubmit={submitQuestion}>
                <label class="field">{TEXT.askLabel}
                  <textarea class="textarea chat-input" rows={2} maxLength={RESEARCH.questionMax + 200} placeholder={TEXT.askPlaceholder}
                    value={snap.draft} onInput={(ev) => ctrl.setDraft(ev.currentTarget.value)} onKeyDown={onInputKey} />
                </label>
                <div class="btn-row"><button type="submit" class="btn btn--primary" disabled={snap.requesting}>Send</button></div>
              </form>
            ) : null}
            <p class="muted chat-footnote">{snap.footer.line}</p>
            {snap.footer.notes.map((note) => <p key={note} class="muted chat-footnote">{note}</p>)}
          </>
        ) : null}
        {snap.error ? <p class="status-line status-line--error" role="alert">{snap.error}</p> : null}
        {snap.paired ? <PairAnotherDevice /> : null}
      </div>
    </dialog>
  );
}
