/* The form that connects this browser to the Pi's assistant with its access code. Shared by the
   warning chat and the code-research dialog. `codeRef` is the caller's ref to the password input
   (it focuses it and reads the code); `onSubmit` receives the submit event. */
import { ACCESS_TEXT } from "../assistantAccess.js";

export default function AssistantPairing({ codeRef, onSubmit, id = "chat-code" }) {
  return (
    <form class="chat-pair" onSubmit={onSubmit}>
      <label class="field" for={id}>{ACCESS_TEXT.pairingLabel}</label>
      <p class="muted chat-hint">{ACCESS_TEXT.pairingHintBefore}<code>{ACCESS_TEXT.pairingHintPath}</code>{ACCESS_TEXT.pairingHintAfter}</p>
      <input id={id} class="input" type="password" autocomplete="off" maxLength={128} required ref={codeRef} />
      <div class="btn-row"><button type="submit" class="btn btn--primary">Connect</button></div>
    </form>
  );
}
