/**
 * Parked view (design 3.2 as amended by critique A13): a scrolling list of
 * cards whose first screen carries what the owner checks when stopped.
 *
 *   BATTERY   big reading (2 decimals), dimmed when held, sub-line from the
 *             sample itself, band badge (derive gates settled/neutral), 24 h
 *             trend (inline SVG, attributes only) and the passive
 *             `Read voltage now` button.
 *   TIRES     TireGrid (colour = absolute floors + warnings only; derive).
 *   LAST TRIP `65 min · ended 4:25 pm`, plus the open trip while one runs.
 *   SERVICE   the shared ServiceCard.
 *   WARNINGS  open count or `Nothing open · last drive 4:25 pm`; tap → Health.
 *   CODES     current DTC count with the first two titles; tap → Health.
 *
 * Card order and visibility follow the device customisation (derive
 * orderedIds / isHidden). Tablet budget: every card model is a module-level
 * computed with a structural-equality gate; the battery value and sub-line
 * are bound straight to text nodes; nothing reads the 1 Hz clock except
 * through minuteClock; no timers; no inline styles.
 */

import { computed } from "@preact/signals";
import { useEffect, useRef, useState } from "preact/hooks";
import * as store from "../store.js";
import {
  obs,
  band,
  tileModel,
  engineRunning,
  minuteClock,
  orderedIds,
  isHidden,
} from "../app/derive.js";
import { manualRefresh } from "../app/runtime.js";
import { warningCards } from "../app/warningCards.js";
import { selectView } from "../app/App.jsx";
import { Card } from "../components/Card.jsx";
import { TireGrid } from "../components/TireGrid.jsx";
import { ServiceCard } from "../components/ServiceCard.jsx";
import { isUnavailableCanState } from "../canAvailability.js";
import * as H from "./parked.helpers.js";

export const PARKED_CARDS = H.PARKED_CARDS;

// ---------------------------------------------------------------------------
// battery

const volt = tileModel("battery.voltage"); // default decimals: 2 (format.js)
const voltBand = band("battery.voltage");
const canUnavailable = computed(() => isUnavailableCanState(store.vehicle.value));

const readingCls = computed(() => H.readingClass(volt.kind.value, voltBand.value.state));

const badgeGate = H.createStableGate();
const batteryBadge = computed(() => badgeGate(H.batteryBadge(voltBand.value.state)));

/** `observed_at` of the displayed battery sample (live record or held sample). */
const shownAt = computed(() => {
  const o = obs("battery.voltage").value;
  if (o.kind === "held") return o.retained ? o.retained.observed_at : null;
  if (o.kind === "live") {
    const rec = store.metricSignal("battery.voltage").value;
    return rec ? rec.observed_at : null;
  }
  return null;
});

const batterySub = computed(() => {
  void minuteClock.value;
  const history = store.summary.history.value;
  const status = store.summary.statusFull.value;
  return H.batterySubline(
    {
      kind: volt.kind.value,
      observedAt: shownAt.value,
      running: engineRunning.value,
      unavailable: canUnavailable.value,
      engineOff: status ? status.engine_off_voltage : null,
      recentTrip: history && Array.isArray(history.recent_trips) ? history.recent_trips[0] : null,
      currentTrip: history ? history.current_trip : null,
    },
    Date.now(),
  );
});

const batteryExtra = computed(() => {
  void minuteClock.value;
  const status = store.summary.statusFull.value;
  return H.engineOffLine(status ? status.engine_off_voltage : null, shownAt.value, Date.now(), canUnavailable.value);
});

const trendGate = H.createStableGate();
const trend = computed(() => trendGate(H.trendModel(store.summary.history.value)));

const acquireGate = H.createStableGate();
const acquire = computed(() => acquireGate(H.acquireAvailability(store.web.value, store.vehicle.value)));

function BatteryTrend() {
  const t = trend.value;
  if (t.empty) return <p class="parked-trend__caption">{t.caption}</p>;
  return (
    <div class="parked-trend">
      <svg
        class="parked-trend__svg"
        viewBox={"0 0 " + t.width + " " + t.height}
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        {t.refY !== null ? (
          <line class="parked-trend__ref" x1="0" x2={t.width} y1={t.refY} y2={t.refY} vector-effect="non-scaling-stroke" />
        ) : null}
        {t.segments.map((s) => (
          <polyline key={s.key} class="parked-trend__line" points={s.points} vector-effect="non-scaling-stroke" />
        ))}
      </svg>
      <p class="parked-trend__caption">{t.caption}</p>
    </div>
  );
}

function ReadVoltage() {
  const [result, setResult] = useState({ kind: "idle", text: "" });
  const alive = useRef(true);
  useEffect(() => () => {
    alive.current = false;
  }, []);
  const avail = acquire.value;
  const busy = result.kind === "reading";
  const onClick = () => {
    if (busy || !acquire.peek().enabled) return;
    setResult({ kind: "reading", text: "" });
    H.readVoltage().then((outcome) => {
      if (!alive.current) return;
      setResult(outcome);
      if (outcome.kind === "done") manualRefresh();
    });
  };
  return (
    <>
      <div class="btn-row">
        <button type="button" class="btn" disabled={!avail.enabled || busy} onClick={onClick}>
          {busy ? H.TEXT.reading : H.TEXT.readNow}
        </button>
      </div>
      {result.text ? (
        <p class={"status-line" + (result.kind === "refused" ? " status-line--error" : "")} role="status">
          {result.text}
        </p>
      ) : null}
      {avail.note ? <p class="status-line">{avail.note}</p> : null}
      <p class="parked-note">{H.TEXT.wakeNote}</p>
    </>
  );
}

function BatteryCard() {
  const b = batteryBadge.value;
  return (
    <Card title="Battery" badge={b.text} badgeTone={b.tone}>
      <div class={readingCls}>
        <span>{volt.valueText}</span>
        <span class="reading__unit">{volt.unitText}</span>
      </div>
      <p class="reading__sub">{batterySub}</p>
      <p class="parked-battery__extra">{batteryExtra}</p>
      <BatteryTrend />
      <ReadVoltage />
    </Card>
  );
}

// ---------------------------------------------------------------------------
// tires

function TiresCard() {
  return (
    <Card title="Tires">
      <div class="parked-tires">
        <TireGrid label="Tires" withFooter />
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// last trip

const tripGate = H.createStableGate();
const trip = computed(() => {
  void minuteClock.value;
  return tripGate(
    H.lastTripModel(store.summary.history.value, {
      running: engineRunning.value,
      unavailable: canUnavailable.value,
      now: Date.now(),
    }),
  );
});

function TripCard() {
  const t = trip.value;
  return (
    <Card title="Last trip">
      {t.last ? (
        <div class="parked-trip">
          <span class="parked-trip__value num">{t.last.duration}</span>
          {t.last.ended ? <span class="parked-trip__sub">{t.last.ended}</span> : null}
        </div>
      ) : null}
      {t.current ? (
        <p class="parked-trip__now">
          This trip: <b class="num">{t.current.duration}</b>
          {t.current.text ? " · " + t.current.text : ""}
        </p>
      ) : null}
      {t.text ? <p class="empty">{t.text}</p> : null}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// warnings and codes summaries (tap → Health)

const toHealth = () => selectView("health");

function LinkCard({ title, badge, children, go }) {
  return (
    <div class="parked-link" onClick={toHealth}>
      <Card
        title={title}
        badge={badge ? badge.text : ""}
        badgeTone={badge ? badge.tone : ""}
        actions={
          <button type="button" class="btn btn--small parked-link__go">
            {go}
          </button>
        }
      >
        {children}
      </Card>
    </div>
  );
}

const warnGate = H.createStableGate();
const warnings = computed(() => {
  void minuteClock.value;
  return warnGate(H.warningsSummary(warningCards.value, store.summary.history.value, Date.now()));
});

function WarningsCard() {
  const w = warnings.value;
  return (
    <LinkCard title="Warnings" badge={w.badge} go="Open Health">
      <p class="parked-summary__line">{w.line}</p>
      {w.items.length ? (
        <ul class="list parked-summary__list">
          {w.items.map((item) => (
            <li class="list__item" key={item.id}>
              <span class={"list__mark" + (item.tone ? " list__mark--" + item.tone : "")} aria-hidden="true" />
              <div class="list__main">
                {item.title}
                {item.meta ? <div class="list__meta">{item.meta}</div> : null}
              </div>
            </li>
          ))}
        </ul>
      ) : null}
      {w.more ? <p class="parked-summary__sub">{w.more}</p> : null}
    </LinkCard>
  );
}

const codesGate = H.createStableGate();
const codes = computed(() => {
  void minuteClock.value;
  return codesGate(H.codesSummary(store.summary.dtcs.value, Date.now()));
});

function CodesCard() {
  const c = codes.value;
  return (
    <LinkCard title="Codes" badge={c.badge} go="Open Health">
      <p class="parked-summary__line">{c.line}</p>
      {c.sub ? <p class="parked-summary__sub">{c.sub}</p> : null}
      {c.items.length ? (
        <ul class="list parked-summary__list">
          {c.items.map((item) => (
            <li class="list__item" key={item.key}>
              <div class="list__main">
                {item.code ? <span class="parked-code num">{item.code}</span> : null}
                {item.title}
              </div>
            </li>
          ))}
        </ul>
      ) : null}
      {c.more ? <p class="parked-summary__sub">{c.more}</p> : null}
    </LinkCard>
  );
}

// ---------------------------------------------------------------------------
// view

const CARD_COMPONENTS = {
  battery: BatteryCard,
  tires: TiresCard,
  trip: TripCard,
  service: ServiceCard, // its `*` is explained by the view's one docs link below
  warnings: WarningsCard,
  codes: CodesCard,
};

// Card order/visibility from the device settings, gated so unrelated settings
// changes (dim, auto view) do not re-render the view.
const layoutGate = H.createStableGate();
const layout = computed(() => {
  const ids = orderedIds("parked", H.PARKED_IDS).filter((id) => CARD_COMPONENTS[id] && !isHidden("parked", id));
  return layoutGate({ ids, spans: H.layoutSpans(ids) });
});

export default function Parked() {
  const { ids, spans } = layout.value;
  return (
    <div class="view view--scroll" role="main">
      <div class="parked-grid">
        {ids.map((id) => {
          const Component = CARD_COMPONENTS[id];
          return (
            <div key={id} class={"parked-cell parked-cell--" + spans[id]}>
              <Component />
            </div>
          );
        })}
      </div>
      {!ids.length ? <p class="empty">Every Parked card is hidden. Show them again in System → Customise.</p> : null}
      <p class="parked-docs">
        <a href={H.DOCS_URL}>* How these readings work</a>
      </p>
    </div>
  );
}
