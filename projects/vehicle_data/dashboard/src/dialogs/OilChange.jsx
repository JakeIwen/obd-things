/* Oil-change sheet (dashboard v2, design 3.2 Parked → Service, 3.3 Health → Service). Records an
   owner-entered oil change on the Pi and lists the service history. Loaded lazily by the Service
   card:  const OilChange = (await import("./dialogs/OilChange.jsx")).default;
   Props: { open, maintenance (the /v1/maintenance payload slice from the summary),
            odometer ({value, unit, source, observed_at, live?} or null; pass live:true while the
            reading is fresh so the distance line drops " at last reading"),
            onSaved(record, refreshedMaintenance|null), onClose() }.
   Ported from static/index.html #oil-change-form + the "Oil-change history" disclosure and
   app.js renderMaintenance / renderServiceMileage / saveOilChange. All validation, request ids,
   the 202 poller and every displayed string come from OilChange.helpers.js.
   No inline styles (CSP); every string is a text node. */
import { useEffect, useRef, useState } from "preact/hooks";
import * as H from "./OilChange.helpers.js";

const EMPTY_FORM = Object.freeze({ date: "", mileage: "", mileageBad: false, source: H.DEFAULT_SOURCE, notes: "" });

function FieldError({ id, text }) {
  return text ? <span id={id} class="oil-error">{text}</span> : null;
}

export default function OilChange({ open, maintenance, odometer, onSaved, onClose }) {
  const dialogRef = useRef(null);
  const closeRef = useRef(null);
  const triggerRef = useRef(null);
  const pendingRef = useRef(null);
  const mountedRef = useRef(true);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const onSavedRef = useRef(onSaved);
  onSavedRef.current = onSaved;

  const [form, setForm] = useState(EMPTY_FORM);
  const [touched, setTouched] = useState({});
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState({ kind: "idle", text: "" });
  const [fetched, setFetched] = useState(null);

  useEffect(() => () => { mountedRef.current = false; }, []);

  // Open/close follows the `open` prop; showModal traps focus and handles Escape.
  useEffect(() => {
    const d = dialogRef.current;
    if (!d) return;
    if (open) {
      if (!d.open) {
        triggerRef.current = typeof document !== "undefined" ? document.activeElement : null;
        try { d.showModal(); } catch { d.setAttribute("open", ""); }
      }
      if (closeRef.current) closeRef.current.focus({ preventScroll: true });
    } else if (d.open) {
      d.close();
    }
  }, [open]);

  // Native close (Esc, ×, backdrop) restores focus and tells the parent.
  useEffect(() => {
    const d = dialogRef.current;
    if (!d) return undefined;
    const handle = () => {
      const t = triggerRef.current;
      if (t && t.isConnected && typeof t.focus === "function") { try { t.focus(); } catch { /* ignore */ } }
      if (onCloseRef.current) onCloseRef.current();
    };
    d.addEventListener("close", handle);
    return () => d.removeEventListener("close", handle);
  }, []);

  const data = H.newerMaintenance(maintenance, fetched);
  const today = H.localToday(new Date());
  const validation = H.validateOilChange(form, { today });
  const blocked = H.saveBlockReason(data);
  const storage = H.storageNote(data);
  const enabled = H.canSave(data, saving, validation);
  const last = H.lastOilChangeSummary(data);
  const reading = H.effectiveOdometer(odometer, data);
  const since = H.milesSinceService(reading, data && data.last_oil_change);
  const odo = H.odometerLine(reading, new Date());
  const history = H.historyRows(data);
  const sourceHint = H.sourceHint(form);

  // Show a field's problem once the owner has typed in it or tried to save.
  const shown = (field) => {
    const error = validation.errors[field];
    if (!error) return "";
    if (touched[field] || touched.submit) return error;
    if (field === "date") return form.date ? error : "";
    if (field === "mileage") return form.mileage || form.mileageBad ? error : "";
    return error;
  };
  const dateError = shown("date");
  const mileageError = shown("mileage");
  const sourceError = shown("source");
  const notesError = shown("notes");

  const update = (patch) => {
    setForm((prev) => ({ ...prev, ...patch }));
    if (result.kind !== "idle" && result.kind !== "saving") setResult({ kind: "idle", text: "" });
  };
  const touch = (field) => setTouched((prev) => (prev[field] ? prev : { ...prev, [field]: true }));

  const submit = async (ev) => {
    ev.preventDefault();
    if (saving) return;
    if (!H.canSave(data, false, validation)) { touch("submit"); return; }
    const pending = H.nextPending(pendingRef.current, validation.body);
    pendingRef.current = pending;
    setSaving(true);
    setResult({ kind: "saving", text: H.TEXT.saving });
    try {
      const saved = await H.saveOilChange(pending.payload);
      pendingRef.current = null;
      if (!mountedRef.current) return;
      if (saved.maintenance) setFetched(saved.maintenance);
      // A fresh form prevents a second tap from recording the same oil change twice.
      setForm((prev) => ({ ...EMPTY_FORM, source: prev.source }));
      setTouched({});
      setResult({ kind: saved.maintenance ? "ok" : "warn", text: saved.message });
      if (onSavedRef.current) onSavedRef.current(saved.record, saved.maintenance);
    } catch (error) {
      if (mountedRef.current) setResult({ kind: "error", text: String((error && error.message) || error) });
    } finally {
      if (mountedRef.current) setSaving(false);
    }
  };

  const close = () => { if (dialogRef.current && dialogRef.current.open) dialogRef.current.close(); };
  const onBackdrop = (ev) => { if (ev.target === dialogRef.current) close(); };

  const statusText = result.text || storage;
  const blockedText = blocked && blocked !== storage && !saving ? blocked : "";
  const formHint = !blockedText && !saving && validation.errors.date && !dateError ? H.TEXT.dateNeeded : "";
  const statusClass = "status-line oil-status" + (result.kind === "error" ? " status-line--error" : "");

  return (
    <dialog ref={dialogRef} class="dialog oil-dialog" aria-labelledby="oil-title" onClick={onBackdrop}>
      <header class="dialog__head">
        <div class="oil-head__text">
          <p class="oil-eyebrow">Vehicle &amp; service</p>
          <h2 id="oil-title" class="dialog__title">Record an oil change</h2>
        </div>
        <button type="button" class="dialog__close" aria-label="Close oil change" ref={closeRef} onClick={close}>×</button>
      </header>
      <div class="dialog__body oil-body">
        <dl class="kv oil-summary">
          <dt>Odometer</dt>
          <dd>
            <span class="oil-summary__value num">{odo.value}</span>
            <span class="oil-summary__sub">{odo.detail}</span>
          </dd>
          <dt>Last oil change</dt>
          <dd>
            <span class="oil-summary__value">{last.date}</span>
            {last.recorded ? <span class="oil-summary__sub num">{last.mileage + (last.source ? " · " + last.source : "")}</span> : null}
            {last.notes ? <span class="oil-summary__sub oil-notes">{last.notes}</span> : null}
          </dd>
        </dl>
        {since.text ? <p class={"oil-since" + (since.kind === "distance" ? "" : " muted")}>{since.text}</p> : null}
        <p class="oil-odo-note">{odo.note}</p>

        <form class="oil-form" onSubmit={submit} noValidate>
          <div class="oil-grid">
            <label class="field oil-field">
              <span>Service date</span>
              <input
                class="input"
                type="date"
                required
                min={H.DATE_MIN}
                max={today}
                value={form.date}
                aria-invalid={dateError ? "true" : "false"}
                aria-describedby={dateError ? "oil-date-error" : undefined}
                onInput={(ev) => update({ date: ev.currentTarget.value })}
                onChange={(ev) => update({ date: ev.currentTarget.value })}
                onBlur={() => touch("date")}
              />
              <FieldError id="oil-date-error" text={dateError} />
            </label>
            <label class="field oil-field">
              <span>Mileage (miles, optional)</span>
              <input
                class="input num"
                type="number"
                min="0"
                max={String(H.MILEAGE_MAX)}
                step={String(H.MILEAGE_STEP)}
                inputMode="decimal"
                value={form.mileage}
                aria-invalid={mileageError ? "true" : "false"}
                aria-describedby={mileageError ? "oil-mileage-error" : undefined}
                onInput={(ev) => {
                  const el = ev.currentTarget;
                  update({ mileage: el.value, mileageBad: Boolean(el.validity && el.validity.badInput) });
                }}
                onBlur={() => touch("mileage")}
              />
              <FieldError id="oil-mileage-error" text={mileageError} />
            </label>
          </div>
          <label class="field oil-field">
            <span>Mileage source</span>
            <select
              class="select"
              value={form.source}
              aria-invalid={sourceError ? "true" : "false"}
              aria-describedby={sourceError ? "oil-source-error" : sourceHint ? "oil-source-hint" : undefined}
              onChange={(ev) => { update({ source: ev.currentTarget.value }); touch("source"); }}
            >
              {H.SOURCE_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
            <FieldError id="oil-source-error" text={sourceError} />
            {!sourceError && sourceHint ? <span id="oil-source-hint" class="oil-hint">{sourceHint}</span> : null}
          </label>
          <label class="field oil-field">
            <span>Oil, filter, shop, or other notes</span>
            <textarea
              class="textarea"
              rows={3}
              maxLength={H.NOTES_MAX}
              value={form.notes}
              aria-invalid={notesError ? "true" : "false"}
              aria-describedby={notesError ? "oil-notes-error oil-notes-count" : "oil-notes-count"}
              onInput={(ev) => update({ notes: ev.currentTarget.value })}
              onBlur={() => touch("notes")}
            />
            <FieldError id="oil-notes-error" text={notesError} />
            <span id="oil-notes-count" class="oil-hint num">{H.notesCount(form.notes)}</span>
          </label>
          <p class="muted oil-note">{H.TEXT.note}</p>
          <div class="btn-row oil-actions">
            <button type="submit" class="btn btn--primary" disabled={!enabled}>Save oil change</button>
          </div>
          {blockedText ? <p class="status-line oil-blocked">{blockedText}</p> : null}
          {formHint ? <p class="status-line oil-blocked">{formHint}</p> : null}
          <p class={statusClass} role="status" aria-live="polite">{statusText}</p>
        </form>

        <section class="dialog__section oil-history" aria-labelledby="oil-history-title">
          <h3 id="oil-history-title">Oil-change history</h3>
          {history.rows.length ? (
            <ul class="list oil-list">
              {history.rows.map((row) => (
                <li key={row.key} class="list__item oil-row">
                  <div class="list__main">
                    <div class="oil-row__date">{row.date}</div>
                    <div class="list__meta oil-row__meta">{row.meta}</div>
                    {row.notes ? <div class="oil-row__notes">{row.notes}</div> : null}
                  </div>
                </li>
              ))}
            </ul>
          ) : <p class="empty">{H.TEXT.noService}</p>}
          {history.more ? <p class="status-line">{history.more}</p> : null}
        </section>
      </div>
    </dialog>
  );
}
