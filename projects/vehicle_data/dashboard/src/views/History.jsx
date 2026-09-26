/**
 * History view (design 3.4): one coverage line, the Trips card (open trip plus the last five),
 * and one compact trend card per recorded metric (7-day and 30-day low/avg/high, the usual trip
 * average, this trip's difference while one is open, and a 24 h time-axis sparkline drawn as an
 * inline SVG polyline that breaks where readings are missing).
 *
 * Performance (old tablet): every card reads one module-level computed over the summary slices
 * (60 s cadence) passed through a structural gate, so a card re-renders only when its text or
 * geometry changed. Time labels follow derive's minuteClock, never the 1 Hz clock. Each trend card
 * subscribes to its own metric's model, so the view itself re-renders only when the card list or
 * the device's order changes. No timers, no layout reads, no inline styles.
 */

import { computed } from "@preact/signals";
import * as store from "../store.js";
import { minuteClock, orderedIds, isHidden, monoNow } from "../app/derive.js";
import { Card, Empty } from "../components/Card.jsx";
import * as H from "./historySystem.helpers.js";

/** Default History cards (Trips + one per trend metric); System builds the same list for Customise. */
const cardsGate = H.createStableGate();
const historyCardList = computed(() => cardsGate(H.historyCards(store.summary.history.value)));

/**
 * Time since the history product was generated: the broker's cache lag (Pi clock: last collector
 * cycle minus `generated_at`) plus the time since this tablet fetched the summary (monotonic).
 */
function sinceGenerated() {
  const history = store.summary.history.value;
  const full = store.summary.statusFull.value;
  const fetched = store.summary.fetchedMono.value;
  const gen = H.wallMs(history && history.generated_at);
  const pi = H.wallMs(full && full.collector && full.collector.last_cycle_at);
  const lag = isFinite(gen) && isFinite(pi) && pi > gen ? pi - gen : 0;
  const since = fetched > 0 ? Math.max(0, monoNow() - fetched) : 0;
  return lag + since;
}

const coverageGate = H.createStableGate();
const coverage = computed(() => {
  void minuteClock.value;
  return coverageGate(
    H.coverageLine(store.summary.history.value, {
      extraMs: sinceGenerated(),
      error: store.summary.error.value,
      nowMs: Date.now(),
    }),
  );
});

const tripsGate = H.createStableGate();
const trips = computed(() => {
  void minuteClock.value;
  return tripsGate(H.tripsModel(store.summary.history.value, { nowMs: Date.now(), extraMs: sinceGenerated() }));
});

const trendCache = new Map();
/** Per-metric trend model (memoised); re-evaluates when the history slice changes. */
function trendVm(metric) {
  let c = trendCache.get(metric);
  if (!c) {
    const gate = H.createStableGate();
    c = computed(() => {
      const h = store.summary.history.value;
      const trends = h && h.metric_trends && typeof h.metric_trends === "object" ? h.metric_trends : null;
      return gate(
        H.trendModel(metric, trends ? trends[metric] : null, {
          nowMs: Date.now(),
          generatedAt: h ? h.generated_at : null,
          tripOpen: Boolean(h && h.current_trip),
        }),
      );
    });
    trendCache.set(metric, c);
  }
  return c;
}

// Card order/visibility from the device settings; trend cards only for metrics the broker reports.
const layoutGate = H.createStableGate();
const layout = computed(() => {
  const defaults = historyCardList.value.map((card) => card.id);
  const present = new Set(H.trendMetrics(store.summary.history.value).map(H.trendId));
  const ids = orderedIds("history", defaults).filter(
    (id) => !isHidden("history", id) && (id === "trips" || present.has(id)),
  );
  return layoutGate(ids);
});

function CoverageLine() {
  const c = coverage.value;
  return (
    <div class="history-coverage" role="status">
      <p class={"history-coverage__text" + (c.tone ? " history-coverage__text--" + c.tone : "")}>{c.text}</p>
      {c.problems.map((text) => (
        <p key={text} class="history-coverage__problem">
          {text}
        </p>
      ))}
    </div>
  );
}

function TripsCard() {
  const t = trips.value;
  return (
    <Card title="Trips" wide>
      {t.current ? (
        <div class="history-trip history-trip--now">
          <span class="history-trip__dur num">{t.current.duration}</span>
          <div class="history-trip__main">
            <div class="history-trip__title">{t.current.title}</div>
            {t.current.meta ? <div class="list__meta">{t.current.meta}</div> : null}
          </div>
        </div>
      ) : null}
      {t.rows.length ? (
        <ul class="list">
          {t.rows.map((row) => (
            <li key={row.id} class="list__item history-trip">
              <span class="history-trip__dur num">{row.duration}</span>
              <div class="history-trip__main">
                <div>{row.when}</div>
                {row.meta ? <div class="list__meta">{row.meta}</div> : null}
              </div>
            </li>
          ))}
        </ul>
      ) : null}
      {t.emptyText ? <Empty>{t.emptyText}</Empty> : null}
    </Card>
  );
}

function TrendSpark({ m }) {
  const s = m.spark;
  return (
    <div class="history-spark">
      {s.empty ? null : (
        <>
          <svg
            class="history-spark__svg"
            viewBox={"0 0 " + s.width + " " + s.height}
            preserveAspectRatio="none"
            aria-hidden="true"
          >
            {s.segments.map((seg) => (
              <polyline key={seg.key} class="history-spark__line" points={seg.points} vector-effect="non-scaling-stroke" />
            ))}
          </svg>
          <div class="history-spark__axis">
            <span>{m.axisStart}</span>
            <span>{m.axisEnd}</span>
          </div>
        </>
      )}
      <p class="history-spark__caption">{m.sparkCaption}</p>
    </div>
  );
}

function TrendCard({ metric }) {
  const m = trendVm(metric).value;
  return (
    <section class="card history-trend" aria-label={m.title + " trend"}>
      <header class="card__head">
        <h2 class="card__title">{m.title}</h2>
        {m.unit ? <span class="history-trend__unit">{m.unit}</span> : null}
      </header>
      {m.stats.length ? (
        <table class="history-stats num">
          <thead>
            <tr>
              <th scope="col">
                <span class="sr-only">Period</span>
              </th>
              <th scope="col">Low</th>
              <th scope="col">Avg</th>
              <th scope="col">High</th>
            </tr>
          </thead>
          <tbody>
            {m.stats.map((row) => (
              <tr key={row.label}>
                <th scope="row">{row.label}</th>
                <td>{row.low}</td>
                <td>{row.avg}</td>
                <td>{row.high}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {m.current ? <p class="history-trend__now">{m.current}</p> : null}
      {m.typical ? <p class="history-trend__typical">{m.typical}</p> : null}
      <TrendSpark m={m} />
      {m.emptyText ? <Empty>{m.emptyText}</Empty> : null}
    </section>
  );
}

export default function History() {
  const ids = layout.value;
  return (
    <div class="view view--scroll" role="main">
      <CoverageLine />
      <div class="history-grid">
        {ids.map((id) =>
          id === "trips" ? <TripsCard key={id} /> : <TrendCard key={id} metric={id.slice("trend:".length)} />,
        )}
      </div>
      {!ids.length ? <p class="empty">Every History card is hidden. Show them again in System → Customise.</p> : null}
      <p class="history-docs">
        <a href={H.DOCS.caveats}>How these readings work</a>
      </p>
    </div>
  );
}
