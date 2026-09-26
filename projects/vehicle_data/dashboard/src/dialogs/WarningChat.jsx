/* Warning chat dialog (dashboard v2). "Ask Codex" about an early-warning card, or propose and
   approve a custom early warning through the local advisor. Loaded lazily by the Health view:
     const WarningChat = (await import("./dialogs/WarningChat.jsx")).default;
   Props: { open, mode: "explain" | "add-warning", context: {eventId?, assessment?, title?, event?},
            onClose(), onWarningsChanged() }.
   Every behaviour (pairing, requests, 202 re-requests, 1 s polling while busy, transcript,
   proposal and saved-warning state) lives in WarningChat.helpers.js as createChatController();
   this file renders its snapshot and forwards taps. It never reads the store. No inline styles
   (CSP); model replies render as text nodes, never as HTML. */
import { useEffect, useRef, useState } from "preact/hooks";
import * as H from "./WarningChat.helpers.js";

/** The controller is created once per mounted dialog; every state change re-renders from its snapshot. */
function useController(onWarningsChangedRef) {
  const [snap, setSnap] = useState(null);
  const ref = useRef(null);
  if (ref.current === null) {
    ref.current = H.createChatController({
      fetch: (url, init) => globalThis.fetch(url, init),
      isVisible: () => typeof document === "undefined" || document.visibilityState !== "hidden",
      onChange: setSnap,
      onWarningsChanged: () => { if (onWarningsChangedRef.current) onWarningsChangedRef.current(); },
    });
  }
  return [ref.current, snap || ref.current.snapshot()];
}

function rowClass(row) {
  if (row.kind === "user") return "msg msg--me";
  if (row.kind === "error") return "msg msg--error";
  if (row.kind === "progress") return "msg chat-msg--progress";
  return "msg";
}

function Options({ choices }) {
  return choices.map(choice => <option key={choice.value} value={choice.value}>{choice.label}</option>);
}

function PairingForm({ codeRef, onSubmit }) {
  return (
    <form class="chat-pair" onSubmit={onSubmit}>
      <label class="field" for="chat-code">{H.TEXT.pairingLabel}</label>
      <p class="muted chat-hint">{H.TEXT.pairingHintBefore}<code>{H.TEXT.pairingHintPath}</code>{H.TEXT.pairingHintAfter}</p>
      <input id="chat-code" class="input" type="password" autocomplete="off" maxLength={128} required ref={codeRef} />
      <div class="btn-row"><button type="submit" class="btn btn--primary">Connect</button></div>
    </form>
  );
}

function SavedWarnings({ saved, onRemove }) {
  return (
    <details class="disc chat-saved">
      <summary class="disc__summary">{H.TEXT.savedTitle}</summary>
      <div class="disc__body">
        {saved.error ? <p class="status-line status-line--error">{saved.error}</p> : null}
        {saved.rows.map(row => (
          <div key={row.id} class="chat-saved__row">
            <span class="chat-saved__text">{row.text}</span>
            <button type="button" class="btn btn--small" disabled={row.removing} onClick={() => onRemove(row)}>Remove</button>
          </div>
        ))}
      </div>
    </details>
  );
}

function Proposal({ proposal, locked, onToggle, onApply, onRemove }) {
  return (
    <details class="disc chat-proposal" open={proposal.open} onToggle={onToggle}>
      <summary class="disc__summary">{H.TEXT.proposalTitle}</summary>
      <div class="disc__body">
        <p class="chat-rule">{proposal.text}</p>
        <p class="muted">{H.TEXT.ownerReference}</p>
        <div class="btn-row">
          {proposal.applied
            ? <button type="button" class="btn btn--danger" disabled={locked} onClick={onRemove}>Remove Warning</button>
            : <button type="button" class="btn btn--primary" disabled={locked} onClick={onApply}>Approve &amp; Add Warning</button>}
        </div>
        <p class="muted chat-applied">{proposal.appliedText}</p>
      </div>
    </details>
  );
}

export default function WarningChat({ open, mode, context, onClose, onWarningsChanged }) {
  const dialogRef = useRef(null);
  const closeRef = useRef(null);
  const codeRef = useRef(null);
  const inputRef = useRef(null);
  const logRef = useRef(null);
  const stickRef = useRef(true);
  const triggerRef = useRef(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const onWarningsChangedRef = useRef(onWarningsChanged);
  onWarningsChangedRef.current = onWarningsChanged;
  const [ctrl, snap] = useController(onWarningsChangedRef);
  const selectorKey = H.contextKey(mode, context);

  // Open/close follows the `open` prop (and a changed event while open). showModal traps focus
  // and handles Escape; the close button gets focus like the old dialog.
  useEffect(() => {
    const d = dialogRef.current;
    if (!d) return;
    if (open) {
      if (!d.open) {
        triggerRef.current = typeof document !== "undefined" ? document.activeElement : null;
        try { d.showModal(); } catch { d.setAttribute("open", ""); }
      }
      stickRef.current = true;
      if (inputRef.current) inputRef.current.value = "";
      ctrl.open(mode, context);
      if (closeRef.current) closeRef.current.focus({ preventScroll: true });
    } else if (d.open) {
      d.close();
    }
  }, [open, selectorKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // Native close (Esc, X, backdrop, close()) stops polling, restores focus, tells the parent.
  // An in-progress answer keeps running on the Pi; reopening the event shows it.
  useEffect(() => {
    const d = dialogRef.current;
    if (!d) return;
    const handle = () => {
      ctrl.close();
      const t = triggerRef.current;
      if (t && t.isConnected && typeof t.focus === "function") { try { t.focus(); } catch { /* ignore */ } }
      if (onCloseRef.current) onCloseRef.current();
    };
    d.addEventListener("close", handle);
    return () => d.removeEventListener("close", handle);
  }, [ctrl]);

  // Polling pauses while the page is hidden and resumes with an immediate poll when it returns.
  useEffect(() => {
    if (!open || typeof document === "undefined") return undefined;
    const handle = () => ctrl.visibilityChanged();
    document.addEventListener("visibilitychange", handle);
    return () => document.removeEventListener("visibilitychange", handle);
  }, [open, ctrl]);

  useEffect(() => () => ctrl.close(), [ctrl]);

  useEffect(() => {
    if (snap.pairing && codeRef.current) codeRef.current.focus({ preventScroll: true });
  }, [snap.pairing]);

  // The only layout work on the poll path: one scrollTop write when the transcript gained a row
  // (progress heartbeats do not bump `version`). Stays put when the reader scrolled up.
  useEffect(() => {
    const log = logRef.current;
    const t = snap.transcript;
    if (!log || !t.newMessage || !t.rows.length) return;
    if (stickRef.current || t.rows.length <= 2) {
      log.scrollTop = log.scrollHeight;
      stickRef.current = true;
    }
  }, [snap.transcript.version]); // eslint-disable-line react-hooks/exhaustive-deps

  const onLogScroll = ev => {
    const el = ev.currentTarget;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };
  const submitCode = ev => {
    ev.preventDefault();
    const el = codeRef.current;
    if (ctrl.pair(el ? el.value : "") && el) el.value = "";
  };
  const submitMessage = async ev => {
    if (ev) ev.preventDefault();
    const el = inputRef.current;
    const sent = await ctrl.send(el ? el.value : "");
    if (sent && el) el.value = "";
  };
  const onInputKey = ev => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); void submitMessage(); }
  };
  const newChat = () => {
    if (!snap.canNew || !window.confirm(H.TEXT.confirmNewChat)) return;
    ctrl.newChat();
  };
  const applyWarning = () => { if (window.confirm(H.TEXT.confirmApply)) void ctrl.applyWarning(); };
  const removeWarning = () => { if (window.confirm(H.TEXT.confirmRemove)) void ctrl.removeWarning(); };
  const removeSaved = row => { if (window.confirm(H.confirmRemoveSaved(row.title))) void ctrl.removeSaved(row.id); };
  const onBackdrop = ev => { if (ev.target === dialogRef.current) dialogRef.current.close(); };

  return (
    <dialog ref={dialogRef} class="dialog chat-dialog" aria-labelledby="chat-title" onClick={onBackdrop}>
      <header class="dialog__head">
        <div class="chat-head__text">
          <p class="chat-eyebrow">{H.TEXT.eyebrow}</p>
          <h2 id="chat-title" class="dialog__title chat-title">{snap.title}</h2>
        </div>
        <button type="button" class="dialog__close" aria-label="Close warning chat" ref={closeRef} onClick={() => dialogRef.current?.close()}>×</button>
      </header>
      <div class="dialog__body chat-body">
        <p class="muted chat-evidence">{snap.evidence}</p>
        <div class="chat-settings">
          <label class="field">Model
            <select class="select" value={snap.model} disabled={snap.locked} onChange={ev => ctrl.setModel(ev.currentTarget.value)}>
              <Options choices={snap.models} />
            </select>
          </label>
          <label class="field">Effort
            <select class="select" value={snap.effort} disabled={snap.locked} onChange={ev => ctrl.setEffort(ev.currentTarget.value)}>
              <Options choices={snap.efforts} />
            </select>
          </label>
        </div>
        {snap.pairing ? <PairingForm codeRef={codeRef} onSubmit={submitCode} /> : null}
        <div class="msgs chat-log" role="log" aria-label="Warning conversation" ref={logRef} onScroll={onLogScroll}>
          {snap.transcript.rows.map(row => (
            <article key={row.key} class={rowClass(row)}>
              <h3 class="chat-msg__who">{row.heading}</h3>
              <p class="chat-msg__text">{row.text}</p>
            </article>
          ))}
        </div>
        {snap.saved.visible ? <SavedWarnings saved={snap.saved} onRemove={removeSaved} /> : null}
        <p class={"status-line chat-status" + (snap.statusError ? " status-line--error" : "")} role="status">{snap.status}</p>
        {snap.timing.visible ? (
          <details class="disc chat-timing" open={snap.timing.open} onToggle={ev => ctrl.setTimingOpen(ev.currentTarget.open)}>
            <summary class="disc__summary">{H.TEXT.timingTitle}</summary>
            <div class="disc__body">
              <p class="muted chat-timing__text">{snap.timing.text}</p>
              <p class="muted">{H.TEXT.timingNote}</p>
            </div>
          </details>
        ) : null}
        {snap.proposal.visible ? (
          <Proposal proposal={snap.proposal} locked={snap.locked} onToggle={ev => ctrl.setProposalOpen(ev.currentTarget.open)} onApply={applyWarning} onRemove={removeWarning} />
        ) : null}
        <form class="chat-form" onSubmit={submitMessage}>
          <label class="field">{H.TEXT.askLabel}
            <textarea class="textarea chat-input" rows={2} maxLength={H.MAX_QUESTION_CHARS} placeholder={H.TEXT.askPlaceholder} ref={inputRef} onKeyDown={onInputKey} />
          </label>
          <div class="btn-row">
            <button type="submit" class="btn btn--primary" disabled={!snap.canSend}>Send</button>
            <button type="button" class="btn" disabled={!snap.canNew} onClick={newChat}>Refresh Evidence &amp; New Chat</button>
            {snap.showStop ? <button type="button" class="btn btn--danger" disabled={!snap.stopEnabled} onClick={() => void ctrl.stop()}>Stop</button> : null}
            {snap.showRetry ? <button type="button" class="btn" disabled={snap.requesting} onClick={() => void ctrl.retry()}>Retry Answer</button> : null}
            {snap.reconnect ? <button type="button" class="btn" onClick={() => ctrl.reconnect()}>Reconnect</button> : null}
          </div>
        </form>
        <p class="muted chat-footnote">{H.TEXT.footnote}</p>
      </div>
    </dialog>
  );
}
