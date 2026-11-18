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
import * as store from "../store.js";
import { isHidden, orderedIds, dayClock } from "../app/derive.js";
import { warningCards } from "../app/warningCards.js";
import { openEvent, openChat } from "../app/dialogs.js";
import { Card, Empty } from "../components/Card.jsx";
import ServiceCard from "../components/ServiceCard.jsx";
import LazyDisclosure from "../components/LazyDisclosure.jsx";
import CodesCard from "../components/CodesCard.jsx";
import * as H from "./health.helpers.js";

export const HEALTH_CARDS = H.HEALTH_CARDS;

const warningsVm = computed(() => H.warningsView(warningCards.value, store.summary.health.value));
// Time labels here are "4:25 pm / yesterday 4:25 pm" plus a one-month cut-off, so the models also
// follow the local day: the DTC slice can stay unchanged for weeks on a tablet left on overnight.
const notesVm = computed(() => {
  void dayClock.value;
  return H.systemNotesView(warningCards.value, store.summary.health.value, Date.now());
});

const chatEnabled = computed(() => store.web.value.warning_chat_enabled === true);

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
      {vm.customHolds.map((line) => (
        <p key={line.id} class="status-line">{line.text}</p>
      ))}
      {vm.rulesError ? <p class="status-line status-line--error">{vm.rulesError}</p> : null}
      {vm.rulesRejected ? <p class="status-line status-line--error">{vm.rulesRejected}</p> : null}
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
