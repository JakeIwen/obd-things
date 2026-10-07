/**
 * Health view (design 3.3): early warnings, system notes, saved diagnostic codes with the guarded
 * parked scan, and the service card. A scrolling list of cards in the device's chosen order.
 *
 * Performance: each card reads one module-level `computed` built from a summary slice (60 s
 * cadence at most) plus the rarely changing web flags, so nothing here re-renders per second.
 * Long lists (unconfirmed items, code groups, module coverage) sit in disclosures whose bodies
 * are rendered only once they are first opened.
 */

import { computed } from "@preact/signals";
import { useState } from "preact/hooks";
import * as store from "../store.js";
import { isHidden, orderedIds, dayClock } from "../app/derive.js";
import { warningCards } from "../app/warningCards.js";
import { openEvent, openChat } from "../app/dialogs.js";
import { Card, Empty } from "../components/Card.jsx";
import ServiceCard from "../components/ServiceCard.jsx";
import DtcScan from "../components/DtcScan.jsx";
import * as H from "./health.helpers.js";

export const HEALTH_CARDS = H.HEALTH_CARDS;

const warningsVm = computed(() => H.warningsView(warningCards.value, store.summary.health.value));
// Time labels here are "4:25 pm / yesterday 4:25 pm" plus a one-month cut-off, so the models also
// follow the local day: the DTC slice can stay unchanged for weeks on a tablet left on overnight.
const notesVm = computed(() => {
  void dayClock.value;
  return H.systemNotesView(warningCards.value, store.summary.health.value, Date.now());
});
const scanEnabled = computed(() => store.web.value.dtc_jobs_enabled === true);
const chatEnabled = computed(() => store.web.value.warning_chat_enabled === true);
const codesVm = computed(() => {
  void dayClock.value;
  return H.codesView(store.summary.dtcs.value, Date.now(), scanEnabled.value);
});

/** Disclosure whose body is built on first open (and kept afterwards). `render` returns the body. */
function LazyDisclosure({ summary, render, cls }) {
  const [seen, setSeen] = useState(false);
  const onToggle = (e) => {
    if (e.currentTarget.open && !seen) setSeen(true);
  };
  return (
    <details class={"disc" + (cls ? " " + cls : "")} onToggle={onToggle}>
      <summary class="disc__summary">{summary}</summary>
      <div class="disc__body">{seen ? render() : null}</div>
    </details>
  );
}

function ItemRow({ row, chat }) {
  const showChat = chat && row.chat !== null;
  const details = row.episodeId !== null;
  return (
    <li class="list__item">
      <span class={"list__mark" + (row.tone ? " list__mark--" + row.tone : "")} aria-hidden="true" />
      <div class="list__main">
        <div>{row.line}</div>
        {row.meta ? <div class="list__meta">{row.meta}</div> : null}
        {details || showChat ? (
          <div class="health-row__actions">
            {details ? (
              <button type="button" class="btn btn--small" onClick={() => openEvent(row.episodeId)}>
                Details
              </button>
            ) : null}
            {showChat ? (
              <button type="button" class="btn btn--small" onClick={() => openChat(row.chat, "explain")}>
                Ask Codex
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </li>
  );
}

function ItemList({ rows, chat }) {
  return (
    <ul class="list">
      {rows.map((row) => (
        <ItemRow key={row.id} row={row} chat={chat} />
      ))}
    </ul>
  );
}

function WarningsCard() {
  const vm = warningsVm.value;
  const chat = chatEnabled.value;
  const badge = vm.badge;
  return (
    <section class="card card--wide" aria-label="Early warning">
      <header class="card__head health-head">
        <h2 class="card__title">Early warning</h2>
        {badge ? <span class={"card__badge" + (badge.tone ? " card__badge--" + badge.tone : "")}>{badge.text}</span> : null}
        <div class="health-head__actions">
          <button type="button" class="btn btn--small" onClick={() => openEvent(null)}>
            Event history
          </button>
          {chat ? (
            <button type="button" class="btn btn--small" onClick={() => openChat({}, "add-warning")}>
              Add early warning
            </button>
          ) : null}
        </div>
      </header>
      {vm.open.length ? <ItemList rows={vm.open} chat={chat} /> : <Empty>{vm.emptyText}</Empty>}
      {vm.checks ? <p class="status-line">{vm.checks}</p> : null}
      {vm.unconfirmed.length ? (
        <LazyDisclosure
          summary={"Unconfirmed · " + vm.unconfirmed.length}
          render={() => <ItemList rows={vm.unconfirmed} chat={chat} />}
        />
      ) : null}
      {vm.delivery ? <p class="status-line health-delivery">{vm.delivery}</p> : null}
      {vm.rulesError ? <p class="status-line status-line--error">{vm.rulesError}</p> : null}
      <div class="card__foot">
        <a class="health-link" href={H.DOCS.warnings}>
          How warnings work
        </a>
      </div>
    </section>
  );
}

function NotesCard() {
  const vm = notesVm.value;
  const chat = chatEnabled.value;
  let body;
  if (vm.loading) body = <Empty>Waiting for system status…</Empty>;
  else if (!vm.summary) body = <Empty>{vm.emptyText}</Empty>;
  else {
    body = (
      <LazyDisclosure
        summary={vm.summary}
        render={() => (
          <>
            <p class="health-note">Adapter, bus and data-check items. They never count as warnings and never send alerts.</p>
            {vm.notes.length ? <ItemList rows={vm.notes} chat={chat} /> : null}
            {vm.recovered.length ? (
              <LazyDisclosure
                cls="health-sub"
                summary={"Recovered · " + vm.recovered.length}
                render={() => <ItemList rows={vm.recovered} chat={chat} />}
              />
            ) : null}
          </>
        )}
      />
    );
  }
  return (
    <Card title="System notes" badge={vm.count > 0 ? String(vm.count) : null}>
      {body}
    </Card>
  );
}

function CodeList({ rows }) {
  return (
    <ul class="list">
      {rows.map((row) => (
        <li key={row.key} class="list__item">
          <div class="list__main">
            <div>
              <span class="health-dtc__code num">{row.code}</span>{" "}
              <span class={row.reviewed ? undefined : "health-dtc__unreviewed"}>{row.meaning}</span>
            </div>
            {row.meta ? <div class="list__meta">{row.meta}</div> : null}
          </div>
        </li>
      ))}
    </ul>
  );
}

function CodeGroup({ group }) {
  if (group.total <= 0) return <div class="health-none">{group.title + " · none"}</div>;
  return (
    <LazyDisclosure
      summary={group.summary}
      render={() => (
        <>
          {group.note ? <p class="health-note">{group.note}</p> : null}
          {group.rows.length ? <CodeList rows={group.rows} /> : null}
          {group.older.length ? (
            <LazyDisclosure cls="health-sub" summary={group.olderSummary} render={() => <CodeList rows={group.older} />} />
          ) : null}
        </>
      )}
    />
  );
}

function ModuleList({ rows }) {
  return (
    <ul class="list">
      {rows.map((row) => (
        <li key={row.key} class="list__item">
          <span class={"list__mark" + (row.tone ? " list__mark--" + row.tone : "")} aria-hidden="true" />
          <div class="list__main">
            <div>{row.name}</div>
            <div class="list__meta">{row.meta}</div>
          </div>
        </li>
      ))}
    </ul>
  );
}

function CodesCard() {
  const vm = codesVm.value;
  const web = store.web.value;
  const badge = vm.badge;
  const current = vm.current;
  return (
    <Card
      title="Diagnostic codes"
      wide
      badge={badge ? badge.text : null}
      badgeTone={badge ? badge.tone : ""}
      foot={
        <a class="health-link" href={H.DOCS.caveats}>
          What “current” and “pending” mean
        </a>
      }
    >
      {vm.lastRead ? <p class="health-lead">{vm.lastRead}</p> : null}
      {vm.source ? <p class="status-line">{vm.source}</p> : null}
      {vm.emptyText ? <Empty>{vm.emptyText}</Empty> : null}
      {current ? (
        <>
          <h3 class="section-title">{current.summary}</h3>
          {current.rows.length ? <CodeList rows={current.rows} /> : <Empty>No current codes.</Empty>}
        </>
      ) : null}
      {vm.groups.map((group) => (
        <CodeGroup key={group.key} group={group} />
      ))}
      {vm.modules && vm.modules.rows.length ? (
        <LazyDisclosure key="modules" summary={vm.modules.summary} render={() => <ModuleList rows={vm.modules.rows} />} />
      ) : null}
      {web.dtc_jobs_enabled === true ? <DtcScan key="scan" web={web} /> : null}
    </Card>
  );
}

const CARD_COMPONENTS = {
  warnings: WarningsCard,
  notes: NotesCard,
  codes: CodesCard,
  service: ServiceCard,
};

export default function Health() {
  const ids = orderedIds("health", H.HEALTH_CARD_IDS);
  return (
    <div class="view view--scroll" role="main">
      <div class="cards">
        {ids.map((id) => {
          if (isHidden("health", id)) return null;
          const Component = CARD_COMPONENTS[id];
          return Component ? <Component key={id} /> : null;
        })}
      </div>
    </div>
  );
}
