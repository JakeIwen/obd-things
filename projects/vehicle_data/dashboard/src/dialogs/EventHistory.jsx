/* Event history dialog (dashboard v2). Saved-event presentation only; the event backend owns
   schemas and evaluation. Loaded lazily by the Health view:
     const EventHistory = (await import("./dialogs/EventHistory.jsx")).default;
   Props: { open, eventId, onClose(), onAskCodex(eventId, eventSummary) }.
   All non-DOM logic lives in EventHistory.helpers.js. No inline styles (CSP). */
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import * as H from "./EventHistory.helpers.js";

// ---------- small building blocks ----------

/** Button that disables itself while its async action runs (duplicate-click prevention). */
function ActionButton({ label, onRun, onError, class: cls, ...rest }) {
  const [busy, setBusy] = useState(false);
  const run = async () => {
    if (busy) return;
    setBusy(true);
    try { await onRun(); } catch (err) { if (onError) onError(err.message); } finally { setBusy(false); }
  };
  return <button type="button" class={cls || "btn btn--small"} disabled={busy} onClick={run} {...rest}>{label}</button>;
}

/** Status sits under the action row, like the old #event-history-status, so feedback stays in view. */
function StatusLine({ text }) {
  return text ? <p class="status-line evh-status" role="status">{text}</p> : null;
}

function Segments({ segments }) {
  return segments.map((seg, i) => seg.strong ? <strong key={i}>{seg.text}</strong> : seg.text);
}

function Disclosure({ summary, class: cls, open, onToggle, id, children, summaryClass }) {
  return (
    <details class={"disc " + (cls || "")} open={open} onToggle={onToggle} id={id}>
      <summary class={"disc__summary " + (summaryClass || "")}>{summary}</summary>
      <div class="disc__body">{children}</div>
    </details>
  );
}

function Facts({ rows }) {
  if (!rows.length) return null;
  return (
    <dl class="evh-facts">
      {rows.map(row => (
        <div key={row.label} class={row.important ? "evh-fact evh-fact--key" : "evh-fact"}>
          <dt>{row.label}</dt>
          <dd>{row.strong ? <strong>{row.value}</strong> : row.value}</dd>
        </div>
      ))}
    </dl>
  );
}

function Assessment({ title, assessment, evidenceRef }) {
  const model = H.assessmentModel(assessment, evidenceRef);
  return (
    <section class="evh-assess">
      <h4 class="evh-assess__title">{title}</h4>
      {model ? (
        <>
          <p class="evh-assess__state"><strong>{model.state}</strong>{model.reason}</p>
          <Facts rows={model.facts} />
          <Disclosure summary="Source & evidence reference" class="evh-rail evh-rail--thin">
            <Facts rows={model.reference} />
          </Disclosure>
        </>
      ) : <p class="muted">No assessment was saved.</p>}
    </section>
  );
}

/** Less common evidence as labeled values rather than JSON. */
function Record({ value, depth = 0 }) {
  const model = H.recordModel(value, depth);
  if (model.kind === "empty") return <p class="muted">{model.text}</p>;
  if (model.kind === "text") return <p>{model.text}</p>;
  if (model.kind === "list") {
    return (
      <div class="evh-records">
        {model.items.map(item => item.value
          ? <Disclosure key={item.key} summary={item.label} class="evh-rail evh-rail--thin"><Record value={item.value} depth={depth + 1} /></Disclosure>
          : <p key={item.key}>{item.text}</p>)}
        {model.truncated ? <p class="muted">{model.truncated}</p> : null}
      </div>
    );
  }
  return (
    <div class="evh-record">
      <Facts rows={model.facts} />
      {model.nested.map(item => (
        <Disclosure key={item.key} summary={item.label} class="evh-rail evh-rail--thin"><Record value={item.value} depth={depth + 1} /></Disclosure>
      ))}
    </div>
  );
}

// ---------- detail sections ----------

function Overview({ event }) {
  const m = H.overviewModel(event);
  return (
    <section class="dialog__section evh-overview">
      <h3>Why This Event Opened</h3>
      <div class="evh-tags">{m.tags.map(tag => <span key={tag} class="card__badge">{tag}</span>)}</div>
      <p class="evh-lead">{m.lead}</p>
      {m.unconfirmed ? <p class="muted">{m.unconfirmed}</p> : null}
      <div class="evh-key">
        {m.keyReadings.map(([label, value]) => (
          <div key={label} class="evh-key__item"><span class="evh-key__label">{label}</span><strong class="evh-key__value">{value}</strong></div>
        ))}
      </div>
      {m.deviationLine ? <p>{m.deviationLine}</p> : null}
      <p class="muted">{m.openedLine}</p>
      <div class="evh-latest">
        <h4>Latest Saved Evaluation</h4>
        <p>{m.latest.line}</p>
        <p class="muted">{m.latest.time}</p>
        {m.latest.closed ? <p class="muted">{m.latest.closed}</p> : null}
      </div>
    </section>
  );
}

function SampleChart({ event }) {
  const m = H.chartModel(event);
  return (
    <section class="dialog__section">
      <h3>Readings Around the Opening</h3>
      {m.empty ? <p class="muted">{m.message}</p> : (
        <figure class="evh-figure">
          <svg class="evh-chart" viewBox={m.viewBox} role="img" aria-label={m.label} preserveAspectRatio="xMidYMid meet">
            {m.boundary ? <line class="evh-boundary" x1={m.boundary.x1} y1={m.boundary.y} x2={m.boundary.x2} y2={m.boundary.y} /> : null}
            {m.segments.map((s, i) => <line key={"s" + i} class="evh-line" x1={s.x1} y1={s.y1} x2={s.x2} y2={s.y2} />)}
            {m.dots.map((d, i) => <circle key={"d" + i} class="evh-dot" cx={d.cx} cy={d.cy} r="3"><title>{d.title}</title></circle>)}
          </svg>
          <figcaption class="evh-caption">{m.caption}</figcaption>
        </figure>
      )}
    </section>
  );
}

function TimelineRows({ rows }) {
  return (
    <ol class="evh-timeline">
      {rows.map(row => (
        <li key={row.key} class="evh-timeline__item">
          <h4 class="evh-timeline__title">{row.title}</h4>
          <time class="stamp">{row.at}</time>
          <p>{row.detail}</p>
          <Disclosure summary="Assessment at this point" class="evh-rail evh-rail--thin" summaryClass="evh-summary--quiet">
            <Assessment title="Saved Assessment" assessment={row.assessment} evidenceRef={row.evidenceRef} />
          </Disclosure>
        </li>
      ))}
    </ol>
  );
}

function Timeline({ event, ctx }) {
  const m = H.timelineModel(event.timeline);
  const before = event.next_event_before;
  return (
    <section class="dialog__section">
      <h3>What Happened</h3>
      <p class="muted">Most recent changes first. Routine monitoring changes are kept in the notes below.</p>
      <TimelineRows rows={m.rows} />
      {m.notes.length ? (
        <Disclosure summary="Monitoring Notes" class="evh-rail"><TimelineRows rows={m.notes} /></Disclosure>
      ) : null}
      {before ? <ActionButton label="Load Earlier Changes" onRun={() => ctx.loadEarlier(event.id, before)} onError={ctx.setStatus} /> : null}
    </section>
  );
}

function NotesReview({ event, ctx, open, onToggle, anchorRef }) {
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);
  const rows = H.annotationRows(event);
  const submit = async ev => {
    ev.preventDefault();
    if (busy) return;
    const form = ev.currentTarget;
    const data = new FormData(form);
    const body = H.annotationBody({ kind: data.get("kind"), note: data.get("note"), links: H.parseLinks(data.get("links")), requestId: H.newRequestId() });
    const problem = H.validateAnnotation(body);
    if (problem) { setFeedback(problem); return; }
    setBusy(true);
    const token = ctx.token();
    setFeedback("Saving…");
    try {
      await ctx.request(H.annotationsUrl(event.id), body);
      if (!ctx.isCurrent(token)) return;
      await ctx.openEvent(event.id, true);
      ctx.setStatus("Note saved.");
    } catch (err) { setFeedback(err.message); } finally { setBusy(false); }
  };
  return (
    <div ref={anchorRef}>
      <Disclosure summary={H.notesSummary(event)} class="evh-top" open={open} onToggle={onToggle} id="evh-notes">
        <p class="muted">Record what you checked or observed. Review actions do not mark recovery or silence notifications.</p>
        <form class="evh-form" onSubmit={submit}>
          <label class="field">Review action
            <select class="select" name="kind">
              {H.ANNOTATION_OPTIONS.map(([label, value]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <label class="field">Your note
            <textarea class="textarea" name="note" required maxLength={H.NOTE_MAX} rows={3} placeholder="What did you observe, check, or change?" />
          </label>
          <Disclosure summary="Link related records (optional)" class="evh-rail evh-rail--thin">
            <label class="field">Record references
              <input class="input" name="links" placeholder="maintenance:123, dtc:456" />
              <small class="evh-hint">Comma-separated trip:, dtc:, maintenance:, quality: or episode: references.</small>
            </label>
          </Disclosure>
          <div class="btn-row"><button type="submit" class="btn btn--primary" disabled={busy}>Save Note</button></div>
          {feedback ? <p class="status-line" role="status">{feedback}</p> : null}
        </form>
        {rows.map(row => (
          <article key={row.key} class="evh-note">
            <h4 class="evh-note__kind">{row.kind}</h4>
            <time class="stamp">{row.at}</time>
            <p class="evh-note__text">{row.note}</p>
            {row.links ? <p class="muted">{row.links}</p> : null}
          </article>
        ))}
      </Disclosure>
    </div>
  );
}

function ReplayForm({ event, ctx }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [message, setMessage] = useState("");
  const unit = event.first_assessment?.current?.unit || "stored units";
  const submit = async ev => {
    ev.preventDefault();
    if (busy) return;
    const data = new FormData(ev.currentTarget);
    const body = H.replayBody({ threshold: data.get("threshold"), operator: data.get("operator"), persistence: data.get("persistence"), runningOnly: data.get("running_only") === "on" });
    const problem = H.validateReplay(body);
    if (problem) { setMessage(problem); setResult(null); return; }
    setBusy(true);
    const token = ctx.token();
    setMessage("Comparing saved samples…"); setResult(null);
    try {
      const raw = await ctx.request(H.replayUrl(event.id), body);
      if (!ctx.isCurrent(token)) return;
      setResult({ raw, model: H.replayResultModel(raw) }); setMessage("");
    } catch (err) { setMessage(err.message); } finally { setBusy(false); }
  };
  return (
    <Disclosure summary="Try a Different Threshold" class="evh-rail">
      <p class="muted">An experiment on saved samples only. It does not change the rule, the original event, or the vehicle.</p>
      <form class="evh-form" onSubmit={submit}>
        <label class="field">Trigger when
          <select class="select" name="operator">{H.REPLAY_OPERATORS.map(([label, value]) => <option key={value} value={value}>{label}</option>)}</select>
        </label>
        <label class="field">{"Threshold (" + unit + ")"}
          <input class="input" name="threshold" type="number" step="any" required inputMode="decimal" />
        </label>
        <label class="field">Consecutive readings required
          <input class="input" name="persistence" type="number" min="1" max="60" defaultValue="10" required inputMode="numeric" />
          <small class="evh-hint">Gaps over 10 seconds restart the count.</small>
        </label>
        <label class="check"><input type="checkbox" name="running_only" defaultChecked />Use engine-running samples only</label>
        <div class="btn-row"><button type="submit" class="btn" disabled={busy}>Compare Saved Samples</button></div>
      </form>
      <div class="evh-replay" role="status">
        {message ? <p class="status-line">{message}</p> : null}
        {result ? (
          <>
            <p>{result.model.headline}</p>
            <p class="muted">{result.model.coverage}</p>
            <Disclosure summary="Comparison Readings" class="evh-rail evh-rail--thin">
              <table class="evh-table">
                <thead><tr><th>Recorded</th><th>Value</th><th>Would warn</th></tr></thead>
                <tbody>{result.model.rows.map(r => <tr key={r.key}><td>{r.at}</td><td>{r.value}</td><td>{r.wouldWarn}</td></tr>)}</tbody>
              </table>
            </Disclosure>
            <ActionButton label="Export Comparison" onRun={() => ctx.download(result.raw, H.comparisonFilename(event.id))} onError={ctx.setStatus} />
          </>
        ) : null}
      </div>
    </Disclosure>
  );
}

function Support({ event, guide, ctx }) {
  const assessments = H.assessmentsModel(event);
  const coverage = H.coverageModel(event, guide);
  const recurrence = H.recurrenceModel(event);
  return (
    <Disclosure summary="More Evidence & Tools" class="evh-top" id="evh-support">
      <Disclosure summary="Saved Assessments" class="evh-rail">
        {assessments.items.map(item => <Assessment key={item.title} title={item.title} assessment={item.assessment} evidenceRef={item.ref} />)}
        {assessments.transitionOnly ? <p class="muted">{assessments.transitionOnly}</p> : null}
      </Disclosure>
      <Disclosure summary="Coverage & Retention" class="evh-rail">
        <Facts rows={coverage.facts} />
        <p class="evh-caution"><Segments segments={coverage.caution} /></p>
        <p class="evh-copy"><Segments segments={coverage.retention} /></p>
      </Disclosure>
      <Disclosure summary="Recorded Peaks" class="evh-rail">
        {H.peaksModel(event).map(peak => <Assessment key={peak.title} title={peak.title} assessment={peak.assessment} evidenceRef={peak.ref} />)}
      </Disclosure>
      <Disclosure summary="Notification History" class="evh-rail"><Record value={event.notifications} /></Disclosure>
      <Disclosure summary="Related Records" class="evh-rail">
        <p class="muted">Records near this event provide context, not proof of a cause.</p>
        <Record value={event.related} />
      </Disclosure>
      <Disclosure summary="Other Occurrences" class="evh-rail">
        <Facts rows={recurrence.facts} />
        <p class="muted">{recurrence.caveat}</p>
        <div class="evh-links">
          {recurrence.events.map(r => <ActionButton key={r.id} label={r.label} onRun={() => ctx.openEvent(r.id)} onError={ctx.setStatus} />)}
        </div>
        <ActionButton label="See All Events for This Rule" onRun={() => ctx.listForRule(event.rule)} onError={ctx.setStatus} />
      </Disclosure>
      <ReplayForm event={event} ctx={ctx} />
      <Disclosure summary="Technical Identifiers" class="evh-rail">
        <Facts rows={H.identifierFacts(event)} />
        <Record value={event.baseline_archives} />
      </Disclosure>
    </Disclosure>
  );
}

function Guide({ guide }) {
  const m = H.guideModel(guide);
  return (
    <Disclosure summary="How Early Warning Works" class="evh-top" id="evh-guide">
      <div class="evh-guide">
        {m.topics.map(topic => (
          <section key={topic.heading} class="evh-topic"><h4>{topic.heading}</h4><p><Segments segments={topic.segments} /></p></section>
        ))}
      </div>
      <Disclosure summary={m.referenceTitle} class="evh-rail evh-rail--thin">
        {m.entries.map(entry => (
          <section key={entry.key} class="evh-ref">
            <h5 class="evh-ref__title">{entry.title}</h5>
            {entry.segments ? <p><Segments segments={entry.segments} /></p> : <Record value={entry.record} />}
          </section>
        ))}
      </Disclosure>
    </Disclosure>
  );
}

function Detail({ detail, ctx, status, notesOpen, setNotesOpen, notesRef, onAskCodex }) {
  const e = detail.packet?.event;
  return (
    <>
      <nav class="evh-actions" aria-label="Event actions">
        <ActionButton label="All Events" onRun={() => ctx.listEvents()} onError={ctx.setStatus} />
        {e && onAskCodex ? <ActionButton label="Ask Codex" onRun={() => ctx.askCodex(e)} onError={ctx.setStatus} /> : null}
        {e ? (
          <span class="evh-actions__end">
            <ActionButton label="Refresh Evidence" onRun={() => ctx.openEvent(e.id)} onError={ctx.setStatus} />
            <ActionButton label="Export Event JSON" onRun={() => ctx.exportEvent()} onError={ctx.setStatus} />
          </span>
        ) : null}
      </nav>
      <StatusLine text={status} />
      {detail.loading ? <p class="muted">Loading saved evidence…</p> : null}
      {detail.error ? (
        <div class="evh-empty">
          <p class="status-line status-line--error">{detail.error}</p>
          <div class="btn-row">
            <ActionButton label="Retry" onRun={() => ctx.openEvent(detail.id)} onError={ctx.setStatus} />
            <ActionButton label="Browse Saved Events" onRun={() => ctx.listEvents()} onError={ctx.setStatus} />
          </div>
        </div>
      ) : null}
      {e ? (
        <>
          <Overview event={e} />
          <SampleChart event={e} />
          <Timeline event={e} ctx={ctx} />
          <NotesReview event={e} ctx={ctx} open={notesOpen} onToggle={ev => setNotesOpen(ev.currentTarget.open)} anchorRef={notesRef} />
          <Support event={e} guide={detail.packet.system_guide || {}} ctx={ctx} />
          <Guide guide={detail.packet.system_guide} />
          <p class="evh-footnote muted">{"Evidence retrieved " + H.formatTime(detail.packet.generated_at) + "."}</p>
        </>
      ) : null}
    </>
  );
}

// ---------- list view ----------

function EventList({ list, status, filters, setFilters, ruleOpen, setRuleOpen, ctx }) {
  const update = (key, value) => setFilters(f => ({ ...f, [key]: value }));
  return (
    <>
      <form class="evh-filters" onSubmit={ev => { ev.preventDefault(); ctx.listEvents(); }}>
        <label class="field evh-filters__search">Find an event
          <input class="input" type="search" name="q" maxLength={H.Q_MAX} placeholder="Title or event number" value={filters.q} onInput={ev => update("q", ev.currentTarget.value)} />
        </label>
        <label class="field">Status
          <select class="select" name="status" value={filters.status} onChange={ev => update("status", ev.currentTarget.value)}>
            {H.STATUS_OPTIONS.map(([label, value]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <div class="evh-filters__submit"><button type="submit" class="btn" disabled={list.loading}>Search Events</button></div>
        <Disclosure summary="Filter by rule (optional)" class="evh-filters__rule" open={ruleOpen} onToggle={ev => setRuleOpen(ev.currentTarget.open)}>
          <label class="field">Rule identifier
            <input class="input" name="rule" maxLength={H.RULE_MAX} placeholder="All rules" value={filters.rule} onInput={ev => update("rule", ev.currentTarget.value)} />
          </label>
        </Disclosure>
      </form>
      {list.page ? (
        <nav class="evh-actions" aria-label="Event actions">
          <ActionButton label="Clear Filters" onRun={() => ctx.clearFilters()} onError={ctx.setStatus} />
          <ActionButton label="Export This Index Page" onRun={() => ctx.download(list.page, H.INDEX_FILENAME)} onError={ctx.setStatus} />
        </nav>
      ) : null}
      <StatusLine text={status} />
      <div class="evh-list">
        {list.events.map(e => {
          const row = H.eventRowModel(e);
          return (
            <article key={row.id} class="evh-row">
              <div class="evh-row__main">
                <h3 class="evh-row__title">{row.title}</h3>
                <p class="evh-row__meta">{row.meta}</p>
                <p class="evh-row__state">{row.state}</p>
              </div>
              <ActionButton label="View Details" onRun={() => ctx.openEvent(row.id)} onError={ctx.setStatus} />
            </article>
          );
        })}
        {list.page && !list.events.length ? <p class="evh-empty">{H.LIST_EMPTY_HINT}</p> : null}
        {list.cursor ? <div class="btn-row"><ActionButton label="Load Older Events" onRun={() => ctx.listEvents(true)} onError={ctx.setStatus} /></div> : null}
      </div>
    </>
  );
}

// ---------- dialog ----------

const EMPTY_LIST = { events: [], cursor: null, page: null, loading: false };
const EMPTY_DETAIL = { id: null, packet: null, loading: false, error: null };

export default function EventHistory({ open, eventId, onClose, onAskCodex }) {
  const dialogRef = useRef(null);
  const titleRef = useRef(null);
  const bodyRef = useRef(null);
  const notesRef = useRef(null);
  const eventsRef = useRef([]);
  const triggerRef = useRef(null);
  const generation = useRef(0);
  const cursorRef = useRef(null);
  const filtersRef = useRef({ ...H.DEFAULT_FILTERS });
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const skipCloseRef = useRef(false);
  const handoffRef = useRef(false);

  const [view, setView] = useState("list");
  const [filters, setFiltersState] = useState({ ...H.DEFAULT_FILTERS });
  const [ruleOpen, setRuleOpen] = useState(false);
  const [list, setList] = useState(EMPTY_LIST);
  const [detail, setDetail] = useState(EMPTY_DETAIL);
  const [status, setStatus] = useState("");
  const [notesOpen, setNotesOpen] = useState(false);
  const [reveal, setReveal] = useState(0);

  // filtersRef is updated synchronously so a submit right after a change reads the new values.
  const setFilters = useCallback(updater => {
    const next = typeof updater === "function" ? updater(filtersRef.current) : updater;
    filtersRef.current = next;
    setFiltersState(next);
  }, []);

  const scrollToTop = useCallback(() => {
    if (dialogRef.current) dialogRef.current.scrollTop = 0;
    if (bodyRef.current) bodyRef.current.scrollTop = 0;
  }, []);

  const isCurrent = useCallback(token => token === generation.current && Boolean(dialogRef.current?.open), []);
  const token = useCallback(() => generation.current, []);
  const request = useCallback((path, payload) => H.requestEvidence(path, payload), []);

  const download = useCallback((value, name) => {
    const url = URL.createObjectURL(new Blob([H.toJson(value)], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url; a.download = name;
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }, []);

  const listEvents = useCallback(async (append = false) => {
    const gen = ++generation.current;
    const active = H.normalizeFilters(filtersRef.current);
    setView("list");
    setDetail(EMPTY_DETAIL);
    setStatus("Loading saved events…");
    if (active.rule) setRuleOpen(true);
    if (!append) { cursorRef.current = null; eventsRef.current = []; setList({ ...EMPTY_LIST, loading: true }); }
    else setList(s => ({ ...s, loading: true }));
    try {
      const result = await H.requestEvidence(H.listUrl(active, append ? cursorRef.current : null));
      if (!isCurrent(gen)) return;
      cursorRef.current = result.next_before ?? null;
      const events = append ? [...eventsRef.current, ...(result.events || [])] : (result.events || []);
      eventsRef.current = events;
      setList({ events, cursor: result.next_before ?? null, page: result, loading: false });
      setStatus(H.listStatusText(events.length));
      if (!append) scrollToTop();
    } catch (err) {
      if (!isCurrent(gen)) return;
      setList(s => ({ ...s, loading: false }));
      setStatus(err.message);
    }
  }, [isCurrent, scrollToTop]);

  const openEvent = useCallback(async (id, revealNotes = false) => {
    const gen = ++generation.current;
    setView("detail");
    setDetail({ id, packet: null, loading: true, error: null });
    setStatus("");
    setNotesOpen(false);
    try {
      const result = await H.requestEvidence(H.detailUrl(id));
      if (!isCurrent(gen)) return;
      setDetail({ id, packet: result, loading: false, error: null });
      if (revealNotes) { setNotesOpen(true); setReveal(n => n + 1); }
      else scrollToTop();
    } catch (err) {
      if (!isCurrent(gen)) return;
      setDetail({ id, packet: null, loading: false, error: err.message });
    }
  }, [isCurrent, scrollToTop]);

  const loadEarlier = useCallback(async (id, before) => {
    const gen = generation.current;
    const result = await H.requestEvidence(H.detailUrl(id, before));
    if (!isCurrent(gen)) return;
    setDetail(d => {
      if (!d.packet || d.packet.event?.id !== id) return d;
      const event = { ...d.packet.event, timeline: [...(d.packet.event.timeline || []), ...(result.event?.timeline || [])], next_event_before: result.event?.next_event_before ?? null };
      return { ...d, packet: { ...d.packet, event } };
    });
  }, [isCurrent]);

  const exportEvent = useCallback(async () => {
    const packet = detail.packet;
    if (!packet) return;
    const { result, status: message } = await H.collectExport(packet, path => H.requestEvidence(path));
    download(result, H.exportFilename(result.event.id));
    setStatus(message);
  }, [detail.packet, download]);

  const listForRule = useCallback(rule => {
    setFilters({ q: "", status: "all", rule: rule || "" });
    setRuleOpen(true);
    return listEvents();
  }, [listEvents, setFilters]);

  const clearFilters = useCallback(() => {
    setFilters({ ...H.DEFAULT_FILTERS });
    setRuleOpen(false);
    return listEvents();
  }, [listEvents, setFilters]);

  // Handoff: the native `close` event is queued and would reach onClose only after the host has
  // already switched to the chat (closing it again), and a host that briefly re-renders this
  // component with the chat's props must not re-open the list. Notify onClose first, then Ask Codex.
  const askCodex = useCallback(e => {
    generation.current++;
    if (dialogRef.current?.open) { skipCloseRef.current = true; handoffRef.current = true; dialogRef.current.close(); }
    if (onCloseRef.current) onCloseRef.current();
    if (onAskCodex) onAskCodex(e.id, e.title);
  }, [onAskCodex]);

  const ctx = { request, token, isCurrent, setStatus, download, listEvents, openEvent, loadEarlier, exportEvent, listForRule, clearFilters, askCodex };

  // Open/close follows the `open` prop; showModal on the rising edge, focus the title.
  useEffect(() => {
    const d = dialogRef.current;
    if (!d) return;
    if (handoffRef.current) { handoffRef.current = false; return; }
    if (open) {
      if (!d.open) {
        triggerRef.current = typeof document !== "undefined" ? document.activeElement : null;
        try { d.showModal(); } catch { d.setAttribute("open", ""); }
        if (titleRef.current) titleRef.current.focus({ preventScroll: true });
      }
      if (eventId != null) openEvent(eventId); else listEvents();
    } else if (d.open) {
      generation.current++;
      d.close();
    }
  }, [open, eventId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Native close (Esc, X, close()) invalidates in-flight work, restores focus, tells the parent.
  useEffect(() => {
    const d = dialogRef.current;
    if (!d) return;
    const handle = () => {
      generation.current++;
      if (skipCloseRef.current) { skipCloseRef.current = false; return; }
      const t = triggerRef.current;
      if (t && t.isConnected && typeof t.focus === "function") { try { t.focus(); } catch { /* ignore */ } }
      if (onCloseRef.current) onCloseRef.current();
    };
    d.addEventListener("close", handle);
    return () => d.removeEventListener("close", handle);
  }, []);

  useEffect(() => {
    if (reveal && notesRef.current) notesRef.current.scrollIntoView({ block: "nearest" });
  }, [reveal]);

  const header = H.headerModel(view, detail.packet);

  return (
    <dialog ref={dialogRef} class="dialog evh-dialog" aria-labelledby="evh-title">
      <header class="dialog__head">
        <div class="evh-head__text">
          <p class="evh-eyebrow">{header.eyebrow}</p>
          <h2 id="evh-title" class="dialog__title evh-title" tabIndex={-1} ref={titleRef}>{header.title}</h2>
        </div>
        <button type="button" class="dialog__close" aria-label="Close event history" onClick={() => dialogRef.current?.close()}>×</button>
      </header>
      <div class="dialog__body evh-body" ref={bodyRef}>
        {view === "list"
          ? <EventList list={list} status={status} filters={filters} setFilters={setFilters} ruleOpen={ruleOpen} setRuleOpen={setRuleOpen} ctx={ctx} />
          : <Detail detail={detail} ctx={ctx} status={status} notesOpen={notesOpen} setNotesOpen={setNotesOpen} notesRef={notesRef} onAskCodex={onAskCodex} />}
      </div>
    </dialog>
  );
}
