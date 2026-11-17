/**
 * The Codes card's rows: a compact line per code that opens (one row at a time) into a panel with
 * the full meaning, the dates and status flags, and an actions area. Phase 1 puts `Ignore
 * permanently` (or `Restore`, on an ignored row) there; more buttons come in through
 * `slots.actions(row)`, and a secondary line under the meaning through `slots.extra(row)`.
 *
 * @typedef {{actions?: (row: object) => any, extra?: (row: object) => any}} CodeSlots
 */

import { signal } from "@preact/signals";
import { useEffect, useRef, useState } from "preact/hooks";
import * as store from "../store.js";
import { postIgnore } from "../dtcIgnore.js";
import { ignoreBody, restoreBody, noteProblem, nextPending, DTC_IGNORE } from "../dtcIgnore.helpers.js";

/** Key of the one open row (the card shows at most one); null when all are closed. */
export const openRow = signal(null);

const send = (payload) => postIgnore((url, init) => fetch(url, init), payload);

/** Runs one change request and publishes the Pi's reply; errors stay in the panel. */
function useChange(makeBody) {
  const [state, setState] = useState({ busy: false, error: null });
  const pending = useRef(null);
  const run = async () => {
    if (state.busy) return;
    pending.current = nextPending(pending.current, makeBody());
    setState({ busy: true, error: null });
    const reply = await send(pending.current.payload);
    if (reply.kind === "ok") {
      pending.current = null;
      store.applyDtcIgnores(reply.state);
      openRow.value = null;
      return;
    }
    setState({ busy: false, error: reply.message });
  };
  return [state, run];
}

function IgnoreControl({ row }) {
  const [asking, setAsking] = useState(false);
  const [note, setNote] = useState("");
  const [state, run] = useChange(() => ignoreBody(row.moduleKey, row.fca, note));
  const problem = noteProblem(note);
  const field = useRef(null);
  useEffect(() => {
    if (asking && field.current) field.current.focus({ preventScroll: true });
  }, [asking]);
  if (!asking) {
    return (
      <button type="button" class="btn btn--small" onClick={() => setAsking(true)}>
        Ignore permanently
      </button>
    );
  }
  return (
    <div class="dtc-ignore">
      <label class="field dtc-ignore__field">
        Note (optional)
        <input class="input" type="text" value={note} placeholder="Why? e.g. LED headlights" ref={field}
          maxLength={DTC_IGNORE.note_max_chars + 40} aria-invalid={problem ? "true" : undefined}
          onInput={(e) => setNote(e.currentTarget.value)} />
      </label>
      <p class="health-note dtc-ignore__hint">
        The code leaves the lists and counts here. Nothing is cleared in the van, and Restore brings it back.
      </p>
      {problem ? <p class="status-line status-line--error">{problem}</p> : null}
      {state.error ? <p class="status-line status-line--error" role="alert">{state.error}</p> : null}
      <div class="health-row__actions">
        <button type="button" class="btn btn--small btn--primary" disabled={state.busy || Boolean(problem)} onClick={run}>
          {state.busy ? "Ignoring…" : "Ignore"}
        </button>
        <button type="button" class="btn btn--small btn--ghost" disabled={state.busy} onClick={() => setAsking(false)}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function RestoreControl({ row }) {
  const [state, run] = useChange(() => restoreBody(row.moduleKey, row.fca));
  return (
    <div class="dtc-ignore">
      <button type="button" class="btn btn--small" disabled={state.busy} onClick={run}>
        {state.busy ? "Restoring…" : "Restore"}
      </button>
      {state.error ? <p class="status-line status-line--error" role="alert">{state.error}</p> : null}
    </div>
  );
}

function RowPanel({ row, canIgnore, slots }) {
  return (
    <div class="dtc-panel" id={"dtc-panel-" + row.key}>
      <p class="dtc-panel__meaning">{row.meaning}</p>
      {row.note ? <p class="dtc-panel__note">{"Note: " + row.note}</p> : null}
      {row.details.length ? (
        <dl class="kv dtc-panel__kv">
          {row.details.map(([k, v]) => [<dt key={"k" + k}>{k}</dt>, <dd key={"v" + k}>{v}</dd>])}
        </dl>
      ) : null}
      <div class="dtc-actions">
        {row.ignored ? <RestoreControl row={row} /> : canIgnore && row.id ? <IgnoreControl row={row} /> : null}
        {slots.actions ? slots.actions(row) : null}
      </div>
    </div>
  );
}

function CodeRow({ row, canIgnore, slots }) {
  const open = openRow.value === row.key;
  const extra = row.extra || (slots.extra ? slots.extra(row) : null);
  return (
    <li class={"dtc-row" + (open ? " dtc-row--open" : "")}>
      <button type="button" class="dtc-row__head" aria-expanded={open ? "true" : "false"} aria-controls={"dtc-panel-" + row.key}
        onClick={() => { openRow.value = open ? null : row.key; }}>
        <span class="dtc-row__line">
          <span class="health-dtc__code num">{row.code}</span>{" "}
          <span class={"dtc-row__meaning" + (row.reviewed ? "" : " health-dtc__unreviewed")}>{row.meaning}</span>
        </span>
        {extra ? <span class="dtc-row__extra">{extra}</span> : null}
        {row.meta ? <span class="list__meta dtc-row__meta">{row.meta}</span> : null}
        {row.note && !open ? <span class="list__meta dtc-row__meta">{"Note: " + row.note}</span> : null}
      </button>
      {open ? <RowPanel row={row} canIgnore={canIgnore} slots={slots} /> : null}
    </li>
  );
}

/** @param {{rows: object[], canIgnore: boolean, slots?: CodeSlots}} props */
export default function CodeList({ rows, canIgnore, slots = {} }) {
  return (
    <ul class="list dtc-list">
      {rows.map((row) => (
        <CodeRow key={row.key} row={row} canIgnore={canIgnore} slots={slots} />
      ))}
    </ul>
  );
}
