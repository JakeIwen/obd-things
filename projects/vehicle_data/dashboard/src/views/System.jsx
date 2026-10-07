/**
 * System view (design 3.5 as amended by critique A12): vehicle state, buses, broker, radar
 * alignment and the metric catalog (the device can hide or reorder these), then the fixed
 * Customise and Device & app cards.
 *
 *   VEHICLE STATE  one line; a disclosure with the evidence, confidence and the broker's note.
 *   BUSES          C-CAN / B-CAN / CAN-CH rows with a dot, safety inhibits, bus use, and adapter
 *                  details in a disclosure (ERROR-ACTIVE reads "controller normal").
 *   BROKER         collector, history recorder, summary cache, last-readings storage and the drive
 *                  helpers, from `summary.statusFull` (once a minute, never per second).
 *   RADAR          within/approaching/outside ±1° badge; per axis latest, 1-min and 5-min means (n),
 *                  5-min peak, margin to ±1.00°; the last-read time when not live.
 *   METRIC CATALOG every metric with its value (live, dimmed last reading with time, or a dash),
 *                  sources, bus, quality, freshness limit and retention; raw diagnostics and
 *                  unconfirmed metrics sit in a collapsed "Diagnostic-only" disclosure.
 *   CUSTOMISE      per-view show/hide and order (the lazily loaded Customizer dialog).
 *   DEVICE & APP   tablet facts read once on mount, build id, listener, reload and refresh, docs.
 *
 * Performance: each card reads one module-level computed passed through a structural gate; the
 * vehicle line and every catalog value are bound straight to text nodes, so the 1 Hz stream
 * touches only text that changed. No timers, no layout reads, no inline styles.
 */

import { computed } from "@preact/signals";
import { useEffect, useRef, useState } from "preact/hooks";
import * as store from "../store.js";
import { settings, saveSettings } from "../settings.js";
import { fmtClock } from "../format.js";
import { obs, minuteClock, engineRunning, orderedIds, isHidden } from "../app/derive.js";
import { openDialog } from "../app/dialogs.js";
import { manualRefresh } from "../app/runtime.js";
import { Card, Empty } from "../components/Card.jsx";
import { DRIVE_TILES } from "./Drive.jsx";
import { PARKED_CARDS } from "./parked.helpers.js";
import { HEALTH_CARDS } from "./health.helpers.js";
import * as H from "./system.helpers.js";
import { historyCards as buildHistoryCards } from "./history.helpers.js";
import { isUnavailableCanState } from "../canAvailability.js";

export const SYSTEM_CARDS = H.SYSTEM_CARDS;

// ---------------------------------------------------------------------------
// shared bits

/** Disclosure whose body is built on first open (and kept afterwards). */
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

/** Key/value rows with an optional tone per value: rows = [{k, v, tone}]. */
function ToneList({ rows }) {
  return (
    <dl class="kv system-kv">
      {rows.map((row) => [
        <dt key={"k" + row.k}>{row.k}</dt>,
        <dd key={"v" + row.k} class={row.tone ? "system-kv--" + row.tone : undefined}>
          {row.v}
        </dd>,
      ])}
    </dl>
  );
}

// ---------------------------------------------------------------------------
// vehicle state

const lastDriveAt = computed(() => {
  const h = store.summary.history.value;
  const recent = h && Array.isArray(h.recent_trips) ? h.recent_trips[0] : null;
  return recent && recent.ended_at ? recent.ended_at : null;
});
// Primitive strings: the vehicle record republishes every second while running (its detail
// carries the rpm), but these text nodes change only when their words do.
const vehicleText = computed(() => {
  void minuteClock.value;
  const v = store.vehicle.value;
  return H.vehicleLine(
    v,
    { running: engineRunning.value, lastDriveAt: lastDriveAt.value, nowMs: Date.now() },
  );
});
const vehicleBasis = computed(() => {
  const v = store.vehicle.value;
  return isUnavailableCanState(v) ? H.canAvailabilityLine(v) : H.basisText(v.basis);
});
const vehicleConfidence = computed(() => H.confidenceText(store.vehicle.value.confidence));
const vehicleDetail = computed(() => store.vehicle.value.detail || "No note from the broker.");

function VehicleCard() {
  return (
    <Card title="Vehicle state" tone={isUnavailableCanState(store.vehicle.value) ? "amber" : ""}>
      <p class="system-line">{vehicleText}</p>
      <LazyDisclosure
        summary="How the broker decided"
        render={() => (
          <dl class="kv system-kv">
            <dt>Evidence</dt>
            <dd>{vehicleBasis}</dd>
            <dt>Confidence</dt>
            <dd>{vehicleConfidence}</dd>
            <dt>Broker note</dt>
            <dd>{vehicleDetail}</dd>
          </dl>
        )}
      />
    </Card>
  );
}

// ---------------------------------------------------------------------------
// buses

const busesGate = H.createStableGate();
const buses = computed(() => busesGate(H.busesModel(store.statusLite.value, store.summary.statusFull.value)));

function BusesCard() {
  const b = buses.value;
  return (
    <Card title="Buses" tone={b.tone === "red" ? "red" : ""}>
      <div class="system-buses">
        {b.rows.map((row) => (
          <div key={row.id} class="system-bus">
            <div class="row">
              <span class={"row__dot" + (row.tone ? " row__dot--" + row.tone : "")} aria-hidden="true" />
              <span class="row__main num">{row.main}</span>
              {row.side ? <span class="row__side">{row.side}</span> : null}
            </div>
            {row.meta ? <p class="system-bus__meta">{row.meta}</p> : null}
          </div>
        ))}
      </div>
      <p class={"system-note" + (b.inhibits.tone ? " system-note--" + b.inhibits.tone : "")}>{b.inhibits.text}</p>
      {b.owner ? <p class={"system-note" + (b.owner.tone ? " system-note--" + b.owner.tone : "")}>{b.owner.text}</p> : null}
      {b.issues.map((text, i) => (
        <p key={i} class="system-note system-note--amber">
          {text}
        </p>
      ))}
      {b.adapter.length || b.facts.length ? (
        <LazyDisclosure
          summary="Adapter details"
          render={() => (
            <>
              <p class="system-help">Buses are matched by adapter serial and connector, not by Linux names like can0.</p>
              {b.adapter.length ? (
                <ul class="list">
                  {b.adapter.map((line) => (
                    <li key={line.id} class="list__item system-adapter">
                      <div class="list__main">{line.text}</div>
                    </li>
                  ))}
                </ul>
              ) : null}
              {b.facts.length ? (
                <dl class="kv system-kv">
                  {b.facts.map(([k, v]) => [<dt key={"k" + k}>{k}</dt>, <dd key={"v" + k}>{v}</dd>])}
                </dl>
              ) : null}
            </>
          )}
        />
      ) : null}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// broker

const brokerGate = H.createStableGate();
const broker = computed(() => brokerGate(H.brokerModel(store.summary.statusFull.value, Date.now())));
const vanCheckGate = H.createStableGate();
const vanCheck = computed(() => {
  void minuteClock.value; // "yesterday 3:12 pm" follows the local day
  return vanCheckGate({ row: H.vanCheckRow(store.summary.dtcs.value, Date.now()) });
});

function BrokerCard() {
  const b = broker.value;
  const van = vanCheck.value.row;
  return (
    <Card title="Broker">
      {b.empty ? <Empty>Waiting for broker status…</Empty> : <ToneList rows={van ? b.rows.concat([van]) : b.rows} />}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// radar alignment

const radarGate = H.createStableGate();
const radar = computed(() => {
  const axes = {};
  for (let i = 0; i < H.RADAR_AXES.length; i += 1) {
    const axis = H.RADAR_AXES[i];
    const rec = store.metricSignal(axis.name).value;
    const o = obs(axis.name).value;
    axes[axis.id] = {
      fresh: Boolean(rec && rec.available && !rec.stale && typeof rec.value === "number"),
      value: rec && typeof rec.value === "number" ? rec.value : null,
      held: o.kind === "held" && o.retained ? o.retained : null,
    };
  }
  const full = store.summary.statusFull.value;
  return radarGate(
    H.radarModel({
      axes,
      windows: full ? full.radar_alignment : null,
      polling: full ? full.radar_alignment_polling : null,
      nowMs: Date.now(),
    }),
  );
});

function RadarCard() {
  const r = radar.value;
  return (
    <Card title="Radar alignment" badge={r.badge.text} badgeTone={r.badge.tone}>
      {r.note ? <p class="system-help">{r.note}</p> : null}
      <div class="system-radar">
        {r.axes.map((axis) => (
          <div key={axis.id} class="system-radar__axis">
            <div class="system-radar__label">{axis.label}</div>
            <div class={"system-radar__value num" + (axis.live ? "" : " system-radar__value--held")}>{axis.latest}</div>
            <dl class="kv system-kv num">
              {axis.rows.map(([k, v]) => [<dt key={"k" + k}>{k}</dt>, <dd key={"v" + k}>{v}</dd>])}
            </dl>
          </div>
        ))}
      </div>
      {r.problem ? <p class="system-note system-note--red">{r.problem}</p> : null}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// metric catalog

const catalogGate = H.createStableGate();
const catalog = computed(() => catalogGate(H.catalogModel(store.catalog.value, store.RETAIN_LAST_READING)));

const valueCache = new Map();
/** Per-metric value text and class, bound straight into the row (no component re-render). */
function catalogValue(name) {
  let entry = valueCache.get(name);
  if (!entry) {
    const model = computed(() => {
      const rec = store.metricSignal(name).value;
      const o = obs(name).value;
      if (o.kind === "held") void minuteClock.value; // day labels of dated readings
      const def = store.catalogByName.value.get(name);
      return H.catalogValue(name, rec, o, def ? def.unit : null, Date.now());
    });
    // Why there is no current value; changes only when the record's reason or last error does.
    const why = computed(() => H.catalogWhy(store.metricSignal(name).value, obs(name).value));
    entry = {
      text: computed(() => model.value.text),
      cls: computed(() => "system-cat__value num" + (model.value.held ? " system-cat__value--held" : "")),
      why,
      whyHidden: computed(() => !why.value),
    };
    valueCache.set(name, entry);
  }
  return entry;
}

function CatalogRow({ row }) {
  const v = catalogValue(row.name);
  return (
    <li class="list__item system-cat">
      <div class="list__main">
        <div class="system-cat__head">
          <span class="system-cat__label">{row.label}</span>
          <span class={v.cls}>{v.text}</span>
        </div>
        <div class="system-cat__raw">{row.name}</div>
        <div class="list__meta system-cat__why" hidden={v.whyHidden}>
          {v.why}
        </div>
        {row.sources.map((line, i) => (
          <div key={i} class="list__meta">
            {line}
          </div>
        ))}
        <div class="list__meta">{row.policy}</div>
      </div>
    </li>
  );
}

function CatalogList({ rows }) {
  return (
    <ul class="list">
      {rows.map((row) => (
        <CatalogRow key={row.name} row={row} />
      ))}
    </ul>
  );
}

function CatalogCard() {
  const c = catalog.value;
  const total = c.main.length + c.diagnostic.length;
  return (
    <Card title="Metric catalog" wide badge={total ? String(total) : null}>
      {!total ? <Empty>Waiting for the metric list…</Empty> : null}
      {c.main.length ? <CatalogList rows={c.main} /> : null}
      {c.diagnostic.length ? (
        <LazyDisclosure
          summary={"Diagnostic-only · " + c.diagnostic.length}
          render={() => (
            <>
              <p class="system-help">Raw module values and readings not yet confirmed against a scan tool. They never drive the main screens.</p>
              <CatalogList rows={c.diagnostic} />
            </>
          )}
        />
      ) : null}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// customise (fixed card)

const historyCardsGate = H.createStableGate();
const historyCards = computed(() => historyCardsGate(buildHistoryCards(store.summary.history.value)));

/** Card lists per view, in each view's default order (ids are what the customiser stores). */
const viewCards = computed(() => ({
  drive: DRIVE_TILES,
  parked: PARKED_CARDS,
  health: HEALTH_CARDS,
  history: historyCards.value,
  system: H.SYSTEM_CARDS,
}));

const customiseGate = H.createStableGate();
const customise = computed(() => {
  const s = settings.value;
  const lists = viewCards.value;
  return customiseGate(
    H.CUSTOMISE_VIEWS.map((v) => ({
      view: v.view,
      label: v.label,
      status: H.customiseStatus(v.view, lists[v.view], s, v.noun),
    })),
  );
});

function openCustomiser(view) {
  openDialog("customize", {
    view,
    cards: viewCards.peek()[view],
    settings: settings.peek(),
    // Drive tiles sit in fixed grid places (Drive.jsx ignores a saved order): show/hide only.
    fixedOrder: view === "drive",
    onChange: (next) => saveSettings(next),
  });
}

function CustomiseCard() {
  const rows = customise.value;
  return (
    <Card title="Customise this device">
      <ul class="list">
        {rows.map((row) => (
          <li key={row.view} class="list__item system-cust">
            <div class="list__main">
              <div>{row.label}</div>
              <div class="list__meta">{row.status}</div>
            </div>
            <button type="button" class="btn btn--small" onClick={() => openCustomiser(row.view)}>
              Customise
            </button>
          </li>
        ))}
      </ul>
      <p class="system-help">
        Drive tiles can be hidden; their places on the screen stay fixed. Layouts are saved in this browser only and are not
        copied from the older dashboard.
      </p>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// device & app (fixed card)

/** Tablet facts, read once when the card mounts. */
function readEnv() {
  if (typeof window === "undefined") return {};
  let displayMode = "browser";
  try {
    const modes = ["fullscreen", "standalone", "minimal-ui"];
    for (let i = 0; i < modes.length; i += 1) {
      if (window.matchMedia && window.matchMedia("(display-mode: " + modes[i] + ")").matches) {
        displayMode = modes[i];
        break;
      }
    }
  } catch (error) {
    displayMode = "browser";
  }
  return {
    userAgent: typeof navigator !== "undefined" ? navigator.userAgent : "",
    dpr: window.devicePixelRatio,
    width: window.innerWidth,
    height: window.innerHeight,
    displayMode,
  };
}

function DeviceCard() {
  const [env] = useState(readEnv);
  const [buildInfo, setBuildInfo] = useState(null);
  const [refresh, setRefresh] = useState({ busy: false, text: "", error: false });
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    fetch("/build.json", { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .then((body) => {
        if (alive.current && body && typeof body === "object") setBuildInfo(body);
      })
      .catch(() => {});
    return () => {
      alive.current = false;
    };
  }, []);
  const web = store.web.value;
  // The link publishes `connection` only when its state, reason or detail changes (never per second).
  const rows = [["Connection", H.connectionText(store.connection.value)]]
    .concat(H.appRows(web, buildInfo, Date.now()))
    .concat(H.deviceRows(env));
  const onRefresh = () => {
    if (refresh.busy) return;
    setRefresh({ busy: true, text: "Refreshing…", error: false });
    manualRefresh()
      .then((ok) => {
        if (!alive.current) return;
        if (ok === false) setRefresh({ busy: false, text: "Not connected to the broker yet.", error: true });
        else setRefresh({ busy: false, text: "Data refreshed at " + fmtClock(new Date()), error: false });
      })
      .catch(() => {
        if (alive.current) setRefresh({ busy: false, text: "Refresh failed. The dot in the top bar shows the connection.", error: true });
      });
  };
  return (
    <Card title="Device & app">
      <dl class="kv system-kv">
        {rows.map(([k, v]) => [
          <dt key={"k" + k}>{k}</dt>,
          <dd key={"v" + k} class={k === "Browser" ? "system-ua" : undefined}>
            {v}
          </dd>,
        ])}
      </dl>
      <div class="btn-row">
        <button type="button" class="btn" onClick={() => location.reload()}>
          Reload app
        </button>
        <button type="button" class="btn" disabled={refresh.busy} onClick={onRefresh}>
          Refresh data
        </button>
      </div>
      {refresh.text ? (
        <p class={"status-line" + (refresh.error ? " status-line--error" : "")} role="status">
          {refresh.text}
        </p>
      ) : null}
      <div class="system-docs">
        <a href={H.DOCS.caveats}>How these readings work</a>
        <a href={H.DOCS.warnings}>How warnings work</a>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// view

const CARD_COMPONENTS = {
  vehicle: VehicleCard,
  buses: BusesCard,
  broker: BrokerCard,
  radar: RadarCard,
  catalog: CatalogCard,
};

// Card order/visibility from the device settings, gated so unrelated settings changes (dim, auto
// view) do not re-render the view.
const layoutGate = H.createStableGate();
const layout = computed(() =>
  layoutGate(orderedIds("system", H.SYSTEM_CARD_IDS).filter((id) => CARD_COMPONENTS[id] && !isHidden("system", id))),
);

export default function System() {
  const ids = layout.value;
  return (
    <div class="view view--scroll" role="main">
      <div class="cards">
        {ids.map((id) => {
          const Component = CARD_COMPONENTS[id];
          return <Component key={id} />;
        })}
        <CustomiseCard key="customise" />
        <DeviceCard key="device" />
      </div>
    </div>
  );
}
