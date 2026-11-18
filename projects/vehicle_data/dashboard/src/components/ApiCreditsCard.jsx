/* "Claude API credits" System card: read-only, shown to a paired browser once the Pi has an API key,
   hidden quietly otherwise. Polling runs only while this card is mounted and the page is visible. */
import { useEffect, useRef, useState } from "preact/hooks";
import { Card } from "./Card.jsx";
import Popover from "./Popover.jsx";
import { createCreditsPoller } from "../apiCredits.js";
import { CARD_TEXT, creditsModel } from "../apiCredits.helpers.js";
import { useController } from "../dialogs/useController.js";

/** A line of text with an info button; the popover opens beneath the line and a tap outside closes it. */
function InfoLine({ cls, info, children }) {
  const [open, setOpen] = useState(false);
  const host = useRef(null);
  return (
    <div class={"api-credits__line " + cls} ref={host}>
      <span class="api-credits__text">{children}</span>
      <button type="button" class="api-credits__i" aria-haspopup="dialog" aria-expanded={open ? "true" : "false"}
        aria-label={CARD_TEXT.infoLabel} title={info} onClick={() => setOpen(!open)}>i</button>
      <Popover open={open} onClose={() => setOpen(false)} hostRef={host} label={CARD_TEXT.infoLabel} class="api-credits__popover">
        <p class="api-credits__tip">{info}</p>
      </Popover>
    </div>
  );
}

function Active({ m }) {
  const b = m.balance;
  return (
    <div class="api-credits">
      <p class="api-credits__total num">{b.total}</p>
      {b.asOf ? (
        <p class="api-credits__asof muted">{b.asOf}{b.stale ? <span class="api-credits__stale"> · {CARD_TEXT.stale}</span> : null}</p>
      ) : null}
      {b.grants.length ? (
        <dl class="kv api-credits__grants">
          {b.grants.map((g) => [<dt key={"k" + g.label}>{g.expires}</dt>, <dd key={"v" + g.label} class="num">{g.amount}</dd>])}
        </dl>
      ) : null}
      {m.qualifiers.map((q) => (
        <InfoLine key={q.text} cls="muted" info={q.info}>{q.text}</InfoLine>
      ))}
      <InfoLine cls="api-credits__bills" info={m.followUps}>{m.billing}</InfoLine>
      {m.latchedModels.length ? <p class="api-credits__latched muted">{CARD_TEXT.latched}: {m.latchedModels.join(", ")}</p> : null}
      {m.lastFallback ? <p class="api-credits__fallback muted">{CARD_TEXT.lastFallback}: {m.lastFallback}</p> : null}
    </div>
  );
}

const create = (onChange) => createCreditsPoller({ onChange });

export default function ApiCreditsCard() {
  const [poller, snap] = useController(create, (c) => c.snapshot());
  useEffect(() => {
    const sync = () => (document.hidden ? poller.stop() : poller.start());
    sync();
    document.addEventListener("visibilitychange", sync);
    return () => { document.removeEventListener("visibilitychange", sync); poller.stop(); };
  }, [poller]);
  const model = creditsModel(snap.view, { hasAccessCode: snap.hasAccessCode, nowMs: Date.now() });
  if (!model) return null;
  return (
    <Card title={CARD_TEXT.title}>
      {model.kind === "expired" ? <p class="muted">{model.text}</p> : <Active m={model} />}
    </Card>
  );
}
