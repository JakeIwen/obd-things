/**
 * Service card (design 3.2 Parked → SERVICE and 3.3 Health → Service; one
 * component for both views).
 *
 * Odometer (live, or dimmed with its time when held; estimates carry a small
 * `*` explained on the docs page), last oil change, miles since service (only
 * when the odometer and the record share a mileage source), an Oil life row
 * only when the broker has a source, `Record oil change` (opens the lazily
 * loaded sheet) and a History disclosure of the saved records.
 *
 * Props: { docsLink?: boolean = false } — also render the card's own
 * "* Estimated mileage · about these numbers" link. Off by default because
 * each view carries its one link to /docs/caveats.html; pass `docsLink` only
 * where the card appears without one.
 *
 * Re-renders only when the card's text changes: the model is a module-level
 * computed with a structural-equality gate, refreshed at most once a minute
 * for time labels (derive's minuteClock), never per second.
 */

import { computed } from "@preact/signals";
import { useState } from "preact/hooks";
import * as store from "../store.js";
import { obs, minuteClock } from "../app/derive.js";
import { openDialog } from "../app/dialogs.js";
import { manualRefresh } from "../app/runtime.js";
import { Card, Disclosure, KV } from "./Card.jsx";
import {
  odometerReading,
  serviceModel,
  createStableGate,
  DOCS_URL,
} from "../views/parked.helpers.js";

const readingGate = createStableGate();
/** The odometer reading handed to the oil-change sheet (reference-stable while unchanged). */
export const odometerNow = computed(() =>
  readingGate(
    odometerReading({
      rec: store.metricSignal("vehicle.odometer").value,
      state: obs("vehicle.odometer").value,
      maintenance: store.summary.maintenance.value,
    }),
  ),
);

const modelGate = createStableGate();
const model = computed(() => {
  void minuteClock.value; // today/yesterday labels
  return modelGate(
    serviceModel(store.summary.maintenance.value, odometerNow.value, obs("engine.oil_life_remaining").value, Date.now()),
  );
});

function recordOilChange() {
  openDialog("oil", {
    maintenance: store.summary.maintenance.peek(),
    odometer: odometerNow.peek(),
    onSaved: () => manualRefresh(),
  });
}

function History({ history }) {
  const [open, setOpen] = useState(false);
  const count = history.rows.length;
  if (!count) return null;
  const onToggle = (event) => setOpen(Boolean(event.currentTarget.open));
  return (
    <Disclosure summary={"History · " + count + (count === 1 ? " record" : " records")} onToggle={onToggle}>
      {open ? (
        <ul class="list">
          {history.rows.map((row) => (
            <li class="list__item" key={row.key}>
              <div class="list__main">
                <div class="parked-svc__date">{row.date}</div>
                <div class="list__meta">{row.meta}</div>
                {row.notes ? <div class="list__meta parked-svc__notes">{row.notes}</div> : null}
              </div>
            </li>
          ))}
        </ul>
      ) : null}
      {open && history.more ? <p class="parked-svc__more">{history.more}</p> : null}
    </Disclosure>
  );
}

export function ServiceCard({ docsLink = false }) {
  const m = model.value;
  const odo = m.odometer;
  const rows = [];
  if (m.last) {
    rows.push(["Last oil change", m.last.text]);
    if (m.last.notes) rows.push(["Notes", <span class="parked-svc__notes">{m.last.notes}</span>]);
  }
  if (m.since) {
    rows.push([
      "Since service",
      <span class={m.since.warn ? "parked-svc__warn" : undefined}>
        {m.since.text}
        {m.since.sub ? <span class="parked-svc__sub"> · {m.since.sub}</span> : null}
      </span>,
    ]);
  }
  if (m.oilLife) {
    rows.push([
      "Oil life",
      <span>
        {m.oilLife.text}
        {m.oilLife.sub ? <span class="parked-svc__sub"> · {m.oilLife.sub}</span> : null}
      </span>,
    ]);
  }
  const foot = docsLink && odo.star ? (
    <a class="parked-svc__docs" href={DOCS_URL}>
      * Estimated mileage · about these numbers
    </a>
  ) : null;
  return (
    <Card title="Service" foot={foot}>
      <div class="parked-svc__odo">
        <span class="parked-svc__label">Odometer</span>
        <span class={"parked-svc__value num" + (odo.held ? " parked-svc__value--held" : "")}>
          {odo.text}
          {odo.star ? <span class="parked-svc__star">*</span> : null}
        </span>
        {odo.sub ? <span class="parked-svc__sub">{odo.sub}</span> : null}
      </div>
      {m.state !== "ok" || m.note ? <p class="status-line">{m.note}</p> : null}
      <KV rows={rows} />
      <div class="btn-row parked-svc__actions">
        <button type="button" class="btn" onClick={recordOilChange}>
          Record oil change
        </button>
      </div>
      <History history={m.history} />
    </Card>
  );
}

export default ServiceCard;
