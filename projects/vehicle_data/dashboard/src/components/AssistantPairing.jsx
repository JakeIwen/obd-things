/* The form that connects this browser to the Pi's assistant. Shared by the warning chat and the
   code-research dialog. The PIN comes first (the tablet's path: a connected browser shows a
   one-time PIN under "Pair another device"); pasting the access code stays below it.
   `pinRef` and `codeRef` are the caller's refs to the two inputs (it focuses the PIN field);
   `onSubmit` receives the paste form's submit event; `onCode(code)` receives the access code a
   redeemed PIN returned, which the caller stores exactly as if it had been pasted. */
import { useRef } from "preact/hooks";
import { ACCESS_TEXT, PAIRING } from "../assistantAccess.js";
import { createPinEntry } from "../assistantPairing.js";
import { useController } from "../dialogs/useController.js";

export default function AssistantPairing({ codeRef, pinRef, onSubmit, onCode, id = "chat-code" }) {
  const onCodeRef = useRef(onCode);
  onCodeRef.current = onCode;
  const [entry, snap] = useController(
    (onChange) => createPinEntry({
      fetch: (url, init) => globalThis.fetch(url, init),
      onCode: (code) => { if (onCodeRef.current) onCodeRef.current(code); },
      onChange,
    }),
    (c) => c.snapshot(),
  );
  const submitPin = async (ev) => {
    ev.preventDefault();
    const el = pinRef && pinRef.current;
    if (await entry.submit(el ? el.value : "") && el) el.value = "";
  };
  return (
    <div class="chat-pair">
      <form class="pair-pin-form" onSubmit={submitPin}>
        <label class="field" for={id + "-pin"}>{ACCESS_TEXT.pinLabel}</label>
        <p class="muted chat-hint">{ACCESS_TEXT.pinHint}</p>
        <div class="pair-pin-row">
          <input id={id + "-pin"} class="input pair-pin-input num" type="text" inputmode="numeric" pattern="[0-9 ]*"
            autocomplete="one-time-code" enterkeyhint="go" maxLength={PAIRING.pinDigits * 2} ref={pinRef} />
          <button type="submit" class="btn btn--primary" disabled={snap.busy}>{ACCESS_TEXT.pinButton}</button>
        </div>
        {snap.message ? <p class={"status-line" + (snap.error ? " status-line--error" : "")} role="status">{snap.message}</p> : null}
      </form>
      <form class="pair-paste-form" onSubmit={onSubmit}>
        <p class="pair-or">{ACCESS_TEXT.pasteHeading}</p>
        <label class="field" for={id}>{ACCESS_TEXT.pairingLabel}</label>
        <p class="muted chat-hint">{ACCESS_TEXT.pairingHintBefore}<code>{ACCESS_TEXT.pairingHintPath}</code>{ACCESS_TEXT.pairingHintAfter}</p>
        <input id={id} class="input" type="password" autocomplete="off" maxLength={128} required ref={codeRef} />
        <div class="btn-row"><button type="submit" class="btn btn--primary">Connect</button></div>
      </form>
    </div>
  );
}
