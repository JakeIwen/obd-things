/* Saved-event presentation only. API schemas and evaluation remain owned by the event backend. */
(() => {
  const byId = id => document.getElementById(id);
  const dialog = byId("event-history-dialog");
  const content = byId("event-history-content");
  const status = byId("event-history-status");
  let packet = null, cursor = null, generation = 0, trigger = null, listPage = null;
  const node = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text != null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };
  const human = value => String(value ?? "Not recorded").replaceAll("_", " ");
  const time = value => value && Number.isFinite(new Date(value).getTime()) ? new Date(value).toLocaleString() : "Not recorded";
  const num = value => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(undefined, {maximumFractionDigits: 2}) : "Not recorded";
  const reading = a => a?.current?.value == null ? "Not available" : num(a.current.value) + " " + (a.current.unit || "");
  const badge = text => node("span", text, "badge");
  const unevaluated = a => !a || a.persistence?.evaluated === false || ["unavailable", "not_applicable", "recovering", "insufficient_history", "rejected"].includes(a.state);
  const persistence = a => unevaluated(a) ? "Not evaluated" : (a.persistence?.observed ?? "?") + " of " + (a.persistence?.required ?? "?") + " readings";
  const stateLabel = state => ({unavailable: "Fresh data unavailable", not_applicable: "Different operating conditions",
    recovering: "Recovery not yet confirmed", insufficient_history: "Not enough comparable history",
    rejected: "Reading rejected", watch: "Watch", warning: "Warning", normal: "Within monitoring reference"}[state] || human(state));
  const duration = seconds => seconds == null ? "Not recorded" : seconds < 60 ? num(seconds) + " s" : seconds < 3600 ? num(seconds / 60) + " min" : num(seconds / 3600) + " hr";
  const coverageText = value => ({
    retained: "Saved", complete: "Complete", collecting: "Still collecting",
    never_recorded: "Not recorded", never_recorded_legacy: "Not saved for this older event",
    legacy_partial: "Some older evidence was not saved", saved_transition_only: "Saved transition only",
    page_complete: "All saved changes shown", paginated: "More changes available",
    pruned_budget: "Removed under the storage limit",
    collection_incomplete_pending_recorder: "Collection incomplete; waiting for recorder",
    truncated: "Partial — sample limit reached",
  }[value] || human(value));
  function button(label, fn, cls) {
    const b = node("button", label, cls); b.type = "button";
    b.addEventListener("click", async () => {
      b.disabled = true;
      try { await fn(); } catch (e) { status.textContent = e.message; }
      finally { b.disabled = false; }
    });
    return b;
  }
  function section(title, cls = "event-section", heading = "h3") {
    const s = node("section", null, cls); s.append(node(heading, title)); return s;
  }
  function emphasized(text, terms, cls = "") {
    const p = node("p", null, cls);
    const escaped = terms.slice().sort((a, b) => b.length - a.length).map(term => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    const parts = String(text).split(new RegExp("(" + escaped.join("|") + ")", "gi"));
    parts.forEach((part, i) => p.append(i % 2 ? node("strong", part) : document.createTextNode(part)));
    return p;
  }
  function details(label, ...children) {
    const d = node("details", null, "event-disclosure");
    d.append(node("summary", label), ...children); return d;
  }
  function facts(rows) {
    const dl = node("dl", null, "event-facts");
    for (const [label, value] of rows) {
      if (value == null) continue;
      const important = ["Saved reading", "Typical reading", "Persistence", "Deviation / required", "Recovery",
        "Elapsed open time", "Observed abnormal intervals", "Unobserved intervals", "Evaluator gaps"].includes(label);
      const pair = node("div", null, important ? "event-fact-important" : "");
      const valueNode = node("dd");
      valueNode.append(important && !/^(Not|No|Unknown)/i.test(String(value)) ? node("strong", value) : document.createTextNode(String(value)));
      pair.append(node("dt", label), valueNode); dl.append(pair);
    }
    return dl;
  }
  function field(label, input, hint) {
    const wrap = node("label", null, "event-field");
    wrap.append(node("span", label), input);
    if (hint) wrap.append(node("small", hint, "muted"));
    return wrap;
  }
  // Less common evidence stays accessible, as labeled values rather than JSON.
  function record(value, depth = 0) {
    if (value == null) return node("p", "Not recorded", "muted");
    if (typeof value !== "object") return node("p", typeof value === "boolean" ? (value ? "Yes" : "No") : String(value));
    if (Array.isArray(value)) {
      if (!value.length) return node("p", "No saved records", "muted");
      const list = node("div", null, "event-record-list");
      value.slice(0, 30).forEach((item, i) => list.append(typeof item === "object" ? details("Record " + (i + 1), record(item, depth + 1)) : node("p", String(item))));
      if (value.length > 30) list.append(node("p", "Showing 30 records. Export includes the complete saved data.", "muted"));
      return list;
    }
    const out = node("div", null, "event-record");
    const rows = [];
    for (const [key, item] of Object.entries(value)) {
      if (item != null && typeof item === "object") {
        if (depth < 4) out.append(details(human(key), record(item, depth + 1)));
      } else {
        rows.push([human(key), item == null ? "Not recorded" : typeof item === "boolean" ? (item ? "Yes" : "No") :
          typeof item === "number" ? num(item) : /^\d{4}-\d\d-\d\dT/.test(item) ? time(item) : String(item)]);
      }
    }
    out.prepend(facts(rows)); return out;
  }
  async function request(path, payload) {
    for (let attempt = 0; attempt < 35; attempt++) {
      const response = await fetch(path, {cache: "no-store", ...(payload ? {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)} : {}), signal: AbortSignal.timeout(5000)});
      const data = await response.json();
      if (response.status === 202 || response.status === 429) {
        await new Promise(resolve => setTimeout(resolve, 200)); continue;
      }
      if (!response.ok) throw new Error(data.detail || "Saved evidence could not be loaded");
      return data;
    }
    throw new Error("Saved evidence is still loading. Retry shortly.");
  }

  function show(mode) {
    if (!dialog.open) {
      trigger = document.activeElement; dialog.showModal();
      byId("event-history-title").focus({preventScroll: true});
    }
    dialog.dataset.view = mode;
    byId("event-history-filter").hidden = mode !== "list";
    byId("event-history-actions").replaceChildren();
  }
  function assessment(title, a, ref) {
    const s = section(title, "event-assessment");
    if (!a) { s.append(node("p", "No assessment was saved.", "muted")); return s; }
    const c = a.current || {}, b = a.baseline || {}, d = a.deviation || {};
    const state = node("p");
    state.append(node("strong", stateLabel(a.state)), document.createTextNode(a.reason ? " · " + a.reason : ""));
    s.append(state);
    s.append(facts([
      ["Saved reading", reading(a)], ["Measured", time(c.observed_at)],
      ["Evaluated", time(a.evaluated_at || c.captured_at)],
      ["Persistence", persistence(a)],
      ["Typical reading", b.median == null ? "Not evaluated" : num(b.median) + " " + (b.unit || c.unit || "")],
      ["Deviation / required", d.signed_from_median == null ? "Not evaluated" : num(d.signed_from_median) + " / " + num(d.threshold) + " " + (c.unit || "")],
      ["Observed conditions", human(a.regime).replaceAll(":", " · ")],
      ["Baseline matches", human(a.baseline_regime || b.regime || "Not evaluated").replaceAll("|", " · ").replaceAll("=", ": ")],
      ["Recovery", a.recovery ? a.recovery.count + " of " + a.recovery.required + " comparable normal readings" : null],
      ["Baseline support", b.bucket_count == null ? null : b.bucket_count + " minute buckets · " + b.trip_count + " prior trips"],
    ]));
    s.append(details("Source & evidence reference", facts([
      ["Source", c.source || "Not recorded"], ["Quality", human(c.quality)],
      ["Baseline period", b.first_at ? time(b.first_at) + " – " + time(b.last_at) : null],
      ["Evidence reference", ref || (c.sample_id ? "sample:" + c.sample_id : "Not recorded")],
    ])));
    return s;
  }
  function plot(event) {
    const points = (event.sample_window || []).filter(p => typeof p.value === "number" && p.observed_at);
    if (!points.length) return node("p", `No sample chart is available. ${coverageText(event.completeness.sample_window)}.`, "muted");
    const wrap = node("figure");
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 700 220"); svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Retained samples around event opening. Gaps are not joined.");
    const stamp = p => new Date(p.observed_at).getTime();
    const xs = points.map(stamp), ys = points.map(p => p.value);
    const opening = event.first_assessment, baseline = opening.baseline || {}, delta = opening.deviation || {};
    let boundary = null;
    if (baseline.median != null && delta.threshold != null && ["high", "low"].includes(opening.direction)) {
      boundary = baseline.median + (opening.direction === "low" ? -1 : 1) * delta.threshold;
      ys.push(boundary);
    }
    const xmin = Math.min(...xs), xmax = Math.max(...xs), ymin = Math.min(...ys)-1, ymax = Math.max(...ys)+1;
    const x = t => 45 + (t-xmin)/Math.max(1,xmax-xmin)*640, y = v => 190-(v-ymin)/(ymax-ymin)*170;
    const line = (x1,y1,x2,y2,cls) => {
      const n = document.createElementNS(ns,"line");
      for (const [k,v] of Object.entries({x1,y1,x2,y2,class:cls})) n.setAttribute(k,v);
      svg.append(n);
    };
    if (boundary != null) line(45,y(boundary),685,y(boundary),"event-boundary");
    points.forEach((p,i) => {
      if (i && stamp(p)-stamp(points[i-1]) > 0 && stamp(p)-stamp(points[i-1]) <= 15000 && p.source===points[i-1].source) line(x(stamp(points[i-1])),y(points[i-1].value),x(stamp(p)),y(p.value),"event-line");
      const dot = document.createElementNS(ns,"circle");
      dot.setAttribute("cx",x(stamp(p)));dot.setAttribute("cy",y(p.value));dot.setAttribute("r",3);dot.setAttribute("class","event-dot");
      const title = document.createElementNS(ns,"title");title.textContent=`${time(p.observed_at)}: ${p.value} ${p.unit} (${p.evidence_ref})`;dot.append(title);svg.append(dot);
    });
    wrap.append(svg,node("figcaption",`${time(points[0].observed_at)} – ${time(points.at(-1).observed_at)} · ${num(Math.min(...points.map(p=>p.value)))}–${num(Math.max(...points.map(p=>p.value)))} ${points[0].unit}. ${boundary == null ? "" : `Dashed line: opening boundary ${num(boundary)} ${points[0].unit}. `}Gaps over 15 seconds are not joined. ${coverageText(event.completeness.sample_window)}.`));
    return wrap;
  }

  function systemGuide(guide = {}) {
    const d = details("How Early Warning Works");
    d.id = "event-system-guide";
    const topics = [
      ["Watch vs Warning", "A watch can open before enough repeated readings qualify it as a warning. An interrupted watch that never warns is archived as unconfirmed after its quiet window. Neither is a mechanical diagnosis.", ["watch", "warning", "enough repeated readings", "Neither is a mechanical diagnosis"]],
      ["What the Baseline Means", "History-based rules compare similar operating conditions across prior trips. Their boundaries are monitoring references, not manufacturer limits.", ["similar operating conditions", "prior trips", "monitoring references", "not manufacturer limits"]],
      ["Saved vs Live", "Readings keep their original timestamps. Routine freshness expiry is a quiet monitoring note, not a new vehicle-health event. Missing or stale data cannot prove recovery or continuous abnormal operation. Confirmed warnings retain their unresolved history.", ["original timestamps", "Missing or stale data", "cannot prove recovery"]],
      ["Review vs Recovery", "Notes, review and dismissal record your judgment. They do not resolve the event or silence its notifications.", ["Notes, review and dismissal", "do not resolve the event", "silence its notifications"]],
      ["Explore Without Changing Anything", "Threshold comparisons use saved samples only. Viewing, exporting and discussing an event do not request vehicle readings or change a warning rule.", ["saved samples only", "do not request vehicle readings", "change a warning rule"]],
    ];
    const list = node("div", null, "event-guide");
    for (const [heading, text, terms] of topics) {
      const s = section(heading, "event-guide-topic"); s.append(emphasized(text, terms)); list.append(s);
    }
    d.append(list);
    const reference = details("Technical reference · guide " + (guide.version ?? "unknown"));
    reference.classList.add("event-guide-reference");
    for (const [key, value] of Object.entries(guide)) {
      if (key === "version") continue;
      const s = section(human(key), "event-reference-topic", "h4");
      s.append(typeof value === "string" ? emphasized(value, ["first_assessment", "first_warning", "latest_assessment",
        "observed_at", "captured_at", "evaluated_at", "persistence", "baseline", "recovery", "hysteresis",
        "not an OEM limit", "no automatic age deletion", "seven days", "128 MiB", "counterfactual", "legacy"]) : record(value));
      reference.append(s);
    }
    d.append(reference); return d;
  }
  function overview(e) {
    const first = e.first_assessment || {}, b = first.baseline || {}, deviation = first.deviation || {};
    const s = section("Why This Event Opened", "event-overview");
    const tags = node("div", null, "event-tags");
    tags.append(badge(e.outcome === "unconfirmed" ? "Unconfirmed · Monitoring ended" : e.status === "open" ? "Unresolved" : "Closed"),
      badge("Opened as " + human(first.state)));
    s.append(tags, node("p", e.first_warning ? "A warning was recorded on " + time(e.first_warning.at) + "." : "No warning escalation is recorded for this event.", "event-lead"));
    if (e.outcome === "unconfirmed") s.append(node("p", "A brief deviation was observed; monitoring ended before a warning was established. This watch is archived, with its evidence retained. Recovery was not established.", "muted"));
    const metrics = node("div", null, "event-key-readings");
    for (const [label, value] of [
      ["Reading at opening", reading(first)],
      ["Typical reading", b.median == null ? "Not recorded" : num(b.median) + " " + (b.unit || first.current?.unit || "")],
      ["Repeated readings", unevaluated(first) ? "Not evaluated" : (first.persistence?.observed ?? "?") + " of " + (first.persistence?.required ?? "?")],
    ]) {
      const card = node("div"); card.append(node("span", label), node("strong", value)); metrics.append(card);
    }
    s.append(metrics);
    if (deviation.signed_from_median != null) {
      s.append(node("p", num(Math.abs(deviation.signed_from_median)) + " " + (first.current?.unit || "") +
        (deviation.signed_from_median < 0 ? " below" : " above") + " the baseline (" + num(deviation.threshold) + " " +
        (first.current?.unit || "") + " required)."));
    } else if (first.reason) s.append(node("p", first.reason));
    s.append(node("p", "Opened " + time(e.opened_at) + " · Saved, not live", "muted"));
    const latest = e.latest_assessment || {};
    const update = node("div", null, "event-latest");
    update.append(node("h4", "Latest Saved Evaluation"),
      node("p", stateLabel(latest.state) + (unevaluated(latest) ? (e.monitoring_note?.detail ? " · " + e.monitoring_note.detail : latest.reason ? " · " + latest.reason : "") : " · " + reading(latest))),
      node("p", time(e.last_evaluated_at), "muted"));
    if (e.status === "resolved" && e.outcome !== "unconfirmed") update.append(node("p", "Closed " + time(e.resolved_at) + (e.resolution_reason ? " · " + human(e.resolution_reason) : "") + ". Closure alone does not establish mechanical recovery.", "muted"));
    s.append(update); return s;
  }
  function monitoringNote(v) {
    if (v.type === "watch_unconfirmed") return {title: "Watch archived as unconfirmed", detail: "Monitoring ended before a warning was established. Earlier evidence is retained; recovery was not established."};
    if (v.monitoring_note) return v.monitoring_note;
    if (v.type === "evidence_restored") return {title: "Monitoring resumed", detail: "Usable observations became available again."};
    const a = v.assessment || {}, c = a.current || {}, age = c.effective_age_seconds;
    if (a.state === "unavailable") return {title: "Monitoring paused", detail: typeof age === "number"
      ? `The last reading was ${num(c.value)} ${c.unit || ""}, recorded ${num(age)} seconds before this evaluation. It no longer met the freshness check. Earlier evidence is retained; the reason readings stopped is not established.`
      : "No usable fresh reading was available. Earlier evidence is retained; the reason is not established."};
    return {title: "Monitoring coverage changed", detail: a.reason || "The rule could not continue its comparison."};
  }
  function timeline(e) {
    const s = section("What Happened");
    s.append(node("p", "Most recent changes first. Routine monitoring changes are kept in the notes below.", "muted"));
    const rows = node("ol", null, "event-timeline"); s.append(rows);
    const notes = details("Monitoring Notes");
    const noteRows = node("ol", null, "event-timeline"); notes.append(noteRows); notes.hidden = true;
    s.append(notes);
    const names = {opened: "Event opened", resolved: "Event closed",
      escalated: "Warning escalated", rule_replaced: "Rule replaced", rule_retired: "Rule retired"};
    const append = events => (events || []).forEach(v => {
      const quiet = v.presentation === "monitoring_note" || ["evidence_inconclusive", "evidence_restored", "watch_unconfirmed"].includes(v.type);
      const li = node("li");
      const note = quiet ? monitoringNote(v) : null;
      li.append(node("h4", note ? note.title : names[v.type] || human(v.type)), node("time", time(v.at)),
        node("p", note ? note.detail : v.assessment?.reason || human(v.new_state)),
        details("Assessment at this point", assessment("Saved Assessment", v.assessment, v.evidence_ref)));
      if (quiet) { noteRows.append(li); notes.hidden = false; }
      else rows.append(li);
    });
    append(e.timeline);
    let before = e.next_event_before;
    const more = button("Load Earlier Changes", async () => {
      const gen = generation;
      const result = await request("/v1/events/" + e.id + "?before=" + before);
      if (gen !== generation || !dialog.open) return;
      append(result.event.timeline); before = result.event.next_event_before; more.hidden = !before;
    });
    more.hidden = !before; s.append(more); return s;
  }
  function annotationForm(e) {
    const wrap = details("Notes & Review" + (e.annotations?.length ? " · " + e.annotations.length : ""));
    wrap.id = "event-notes";
    wrap.append(node("p", "Record what you checked or observed. Review actions do not mark recovery or silence notifications.", "muted"));
    const form = node("form", null, "event-form");
    const kind = node("select");
    for (const [label, value] of [["Add a note", "note"], ["Mark reviewed", "acknowledged"], ["Record dismissal", "dismissed"], ["Reopen for review", "reopened_for_review"]]) kind.append(new Option(label, value));
    const note = node("textarea"); note.id = "event-note-input"; note.required = true; note.maxLength = 1000; note.rows = 3;
    note.placeholder = "What did you observe, check, or change?";
    const links = node("input"); links.placeholder = "maintenance:123, dtc:456";
    const submit = node("button", "Save Note"); submit.type = "submit";
    const result = node("p", "", "event-feedback"); result.setAttribute("role", "status");
    form.append(field("Review action", kind), field("Your note", note),
      details("Link related records (optional)", field("Record references", links, "Comma-separated trip:, dtc:, maintenance:, quality: or episode: references.")),
      submit, result);
    wrap.append(form);
    for (const a of e.annotations || []) {
      const row = node("article", null, "event-note");
      row.append(node("h4", human(a.kind)), node("time", time(a.at)), node("p", a.note));
      if (a.links?.length) row.append(node("p", a.links.join(" · "), "muted"));
      wrap.append(row);
    }
    form.addEventListener("submit", async ev => {
      ev.preventDefault(); if (submit.disabled) return; submit.disabled = true;
      const gen = generation; result.textContent = "Saving…";
      try {
        await request("/v1/events/" + e.id + "/annotations", {kind: kind.value, note: note.value,
          links: links.value.split(",").map(v => v.trim()).filter(Boolean), request_id: crypto.randomUUID()});
        if (gen !== generation || !dialog.open) return;
        await openEvent(e.id, true); status.textContent = "Note saved.";
      } catch (err) { result.textContent = err.message; } finally { submit.disabled = false; }
    });
    return wrap;
  }
  function replayForm(e) {
    const d = details("Try a Different Threshold");
    d.append(node("p", "An experiment on saved samples only. It does not change the rule, the original event, or the vehicle.", "muted"));
    const form = node("form", null, "event-form event-replay-form");
    const op = node("select"); op.append(new Option("Above", "above"), new Option("Below", "below"));
    const threshold = node("input"); threshold.type = "number"; threshold.step = "any"; threshold.required = true;
    const persistence = node("input"); persistence.type = "number"; persistence.min = 1; persistence.max = 60; persistence.value = 10; persistence.required = true;
    const running = node("input"); running.type = "checkbox"; running.checked = true;
    const check = node("label", null, "event-checkbox"); check.append(running, document.createTextNode("Use engine-running samples only"));
    const submit = node("button", "Compare Saved Samples"); submit.type = "submit";
    const output = node("div", null, "event-replay-result"); output.setAttribute("role", "status");
    form.append(field("Trigger when", op), field("Threshold (" + (e.first_assessment?.current?.unit || "stored units") + ")", threshold),
      field("Consecutive readings required", persistence, "Gaps over 10 seconds restart the count."), check, submit);
    d.append(form, output);
    form.addEventListener("submit", async ev => {
      ev.preventDefault(); if (submit.disabled) return; submit.disabled = true;
      const gen = generation; output.textContent = "Comparing saved samples…";
      try {
        const result = await request("/v1/events/" + e.id + "/replay", {threshold: Number(threshold.value),
          operator: op.value, persistence: Number(persistence.value), max_gap_seconds: 10, running_only: running.checked});
        if (gen !== generation || !dialog.open) return;
        output.replaceChildren(node("p", result.points.length ? result.points.length + " eligible readings; " +
          result.points.filter(p => p.would_warn).length + " would satisfy the persistence requirement." :
          "No eligible readings. This saved window cannot answer the comparison with these settings."),
          node("p", "Coverage: " + coverageText(result.coverage) + ".", "muted"));
        const samples = details("Comparison Readings");
        const table = node("table", null, "event-samples"), head = node("tr");
        ["Recorded", "Value", "Would warn"].forEach(label => head.append(node("th", label))); table.append(head);
        result.points.forEach(p => { const row = node("tr"); row.append(node("td", time(p.at)), node("td", num(p.value)), node("td", p.would_warn ? "Yes" : "No")); table.append(row); });
        samples.append(table); output.append(samples, button("Export Comparison", () => download(result, "telemetry-event-" + e.id + "-comparison.json")));
      } catch (err) { output.textContent = err.message; } finally { submit.disabled = false; }
    });
    return d;
  }
  function supportingEvidence(e, guide) {
    const advanced = details("More Evidence & Tools");
    advanced.id = "event-support";
    const last = e.evidence?.last_evaluable;
    const assessments = details("Saved Assessments",
      assessment("Opening", e.first_assessment, e.evidence_ref + ":opening"));
    if (e.first_warning) assessments.append(assessment("First Warning", e.first_warning.assessment, e.first_warning.evidence_ref));
    assessments.append(assessment("Last Usable Assessment", last?.assessment, last?.evidence_ref),
      assessment("Latest Evaluation", e.latest_assessment, e.evidence_ref + ":latest"));
    if (last?.coverage === "saved_transition_only") assessments.append(node("p", "The last usable assessment comes from a saved transition, not a full evaluation history.", "muted"));
    advanced.append(assessments);
    const d = e.duration || {};
    advanced.append(details("Coverage & Retention", facts([
      ["Elapsed open time", duration(d.elapsed_open_seconds)], ["Observed abnormal intervals", duration(d.observed_abnormal_seconds)],
      ["Unobserved intervals", duration(d.unobserved_seconds)], ["Evaluator gaps", duration(d.evaluator_gap_seconds)],
      ["Coverage starts", time(d.coverage_since)], ["Last evaluation", time(e.last_evaluated_at)],
      ["Opening evidence", coverageText(e.completeness?.opening)], ["Sample window", coverageText(e.completeness?.sample_window)],
      ["Checkpoints", coverageText(e.completeness?.checkpoints)], ["Rule revision", coverageText(e.completeness?.rule_revision)],
    ]), emphasized("Open time includes parked and unobserved time; it is not the duration of a fault. Totals stop at the last evaluation." +
      (d.legacy_prefix_unknown ? " Earlier coverage is unknown." : ""), ["not the duration of a fault", "last evaluation", "Earlier coverage is unknown"], "event-caution"),
      emphasized(guide.retention || "Missing evidence remains explicitly unknown.",
        ["no automatic age deletion", "seven days", "two minutes before/three after opening", "256 primary samples max", "128 MiB", "tombstones"], "event-support-copy")));
    const peaks = details("Recorded Peaks");
    for (const [key, label] of [["peak_value", "Highest Recorded Value"], ["peak_deviation", "Largest Recorded Deviation"]]) {
      const peak = e.evidence?.[key]; peaks.append(assessment(label, peak?.assessment, peak?.evidence_ref));
    }
    advanced.append(peaks, details("Notification History", record(e.notifications)),
      details("Related Records", node("p", "Records near this event provide context, not proof of a cause.", "muted"), record(e.related)));
    const recurrence = e.recurrences || {}, stats = recurrence.last_30_days || {};
    const repeats = details("Other Occurrences", facts([
      ["Openings in 30 days", num(stats.episode_openings)], ["Recorded trips", num(stats.recorded_trips)],
      ["Recorded trip hours", num(stats.recorded_trip_hours)], ["Openings per recorded trip", num(stats.openings_per_recorded_trip)],
    ]), node("p", stats.denominator_caveat || recurrence.interpretation || "These counts include watches.", "muted"));
    const links = node("div", null, "event-related-links");
    (recurrence.events || []).forEach(r => links.append(button("Event " + r.id + " · " + time(r.opened_at) + " · " + (r.status === "open" ? "Unresolved" : "Closed"), () => openEvent(r.id))));
    repeats.append(links, button("See All Events for This Rule", () => {
      byId("event-filter-rule").value = e.rule; byId("event-filter-search").value = ""; byId("event-filter-status").value = "all"; return listEvents();
    }));
    advanced.append(repeats, replayForm(e), details("Technical Identifiers", facts([
      ["Event", e.evidence_ref], ["Rule", e.rule], ["Evidence revision", e.revision],
    ]), record(e.baseline_archives)));
    return advanced;
  }
  async function openEvent(id, revealNotes = false) {
    const gen = ++generation;
    show("detail"); packet = null; content.replaceChildren(node("p", "Loading saved evidence…", "muted"));
    byId("event-history-title").textContent = "Event Details";
    byId("event-history-eyebrow").textContent = "EARLY WARNING";
    status.textContent = "";
    try {
      const result = await request("/v1/events/" + id);
      if (generation !== gen || !dialog.open) return;
      packet = result; cursor = null; renderDetail();
      if (revealNotes) {
        byId("event-notes").open = true; byId("event-notes").scrollIntoView({block: "nearest"});
      }
    } catch (err) {
      if (generation !== gen || !dialog.open) return;
      content.replaceChildren(node("p", err.message, "event-empty"),
        button("Retry", () => openEvent(id)), button("Browse Saved Events", () => listEvents()));
    }
  }
  function renderDetail() {
    const e = packet.event;
    content.replaceChildren();
    byId("event-history-title").textContent = e.title;
    byId("event-history-eyebrow").textContent = "EVENT " + e.id;
    const actions = byId("event-history-actions");
    actions.replaceChildren(button("All Events", () => listEvents()));
    if (window.WarningChat) actions.append(button("Ask Codex", () => {
      dialog.close(); window.WarningChat.open({kind: "episode", id: String(e.id)}, e.title, trigger);
    }));
    actions.append(button("Refresh Evidence", () => openEvent(e.id), "event-refresh"),
      button("Export Event JSON", exportEvent));
    status.textContent = "";
    content.append(overview(e));
    const chart = section("Readings Around the Opening");
    chart.append(plot(e)); content.append(chart, timeline(e), annotationForm(e),
      supportingEvidence(e, packet.system_guide || {}), systemGuide(packet.system_guide));
    content.append(node("p", "Evidence retrieved " + time(packet.generated_at) + ".", "event-footnote muted"));
    dialog.scrollTop = 0;
  }
  function download(value,name) {
    const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:"application/json"}));
    const a=node("a");a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  async function exportEvent() {
    const result=structuredClone(packet);let before=result.event.next_event_before;
    // Export all immutable older transition pages, with an explicit bound.
    for(let page=0;before && page<200;page++) {
      const more=await request(`/v1/events/${result.event.id}?before=${before}`);
      result.event.timeline.push(...more.event.timeline);before=more.event.next_event_before;
    }
    result.baseline_inputs = {};
    for (const archive of result.event.baseline_archives || []) {
      if (archive.status !== "retained") continue;
      let offset = 0, inputs = [];
      do {
        const page = await request(`/v1/events/${result.event.id}/baselines?digest=${archive.digest}&offset=${offset}`);
        inputs.push(...page.inputs);offset = page.next_offset;
      } while (offset != null);
      result.baseline_inputs[archive.digest] = inputs;
    }
    result.event.next_event_before=before;result.event.completeness.timeline=before ? "export_truncated_200_pages" : "all_retained_transitions";
    download(result,`telemetry-event-${result.event.id}.json`);
    status.textContent=before ? "Export saved with explicit timeline limit; older pages remain available." : "Event evidence exported. Keep this file with your backups.";
  }

  async function listEvents(append = false) {
    const gen = ++generation;
    show("list"); packet = null;
    byId("event-history-title").textContent = "Saved Events";
    byId("event-history-eyebrow").textContent = "EARLY WARNING";
    status.textContent = "Loading saved events…";
    if (!append) { cursor = null; listPage = null; content.replaceChildren(); }
    const params = new URLSearchParams({status: byId("event-filter-status").value});
    if (byId("event-filter-search").value) params.set("q", byId("event-filter-search").value);
    if (byId("event-filter-rule").value) params.set("rule", byId("event-filter-rule").value);
    if (byId("event-filter-rule").value) byId("event-rule-filter").open = true;
    if (append && cursor) params.set("before", cursor);
    const result = await request("/v1/events?" + params);
    if (gen !== generation || !dialog.open) return;
    listPage = result;
    content.querySelector(".event-more")?.remove();
    for (const e of result.events) {
      const item = node("article", null, "event-list-row");
      const info = node("div");
      info.append(node("h3", e.title), node("p", "Event " + e.id + " · " + time(e.opened_at), "muted"),
        node("p", e.outcome === "unconfirmed" ? "Unconfirmed · Monitoring ended" : (e.status === "open" ? "Unresolved" : "Closed") + " · " + human(e.evidence_state)));
      item.append(info, button("View Details", () => openEvent(e.id))); content.append(item);
    }
    cursor = result.next_before;
    if (cursor) content.append(button("Load Older Events", () => listEvents(true), "event-more"));
    const total = content.querySelectorAll(".event-list-row").length;
    status.textContent = total ? total + " saved event" + (total === 1 ? "" : "s") + " shown. Closed events remain available." : "No events match these filters.";
    if (!total) content.append(node("p", "Try a different title, event number or status.", "event-empty"));
    byId("event-history-actions").append(button("Clear Filters", () => {
      byId("event-history-filter").reset(); return listEvents();
    }), button("Export This Index Page", () => download(listPage, "telemetry-event-index.json")));
    if (!append) dialog.scrollTop = 0;
  }
  byId("event-history-open").hidden = false;
  byId("event-history-open").addEventListener("click", () => listEvents().catch(e => {status.textContent = e.message;}));
  byId("event-history-close").addEventListener("click", () => dialog.close());
  byId("event-history-filter").addEventListener("submit", e => {e.preventDefault(); listEvents().catch(err => {status.textContent = err.message;});});
  dialog.addEventListener("close", () => {
    generation++;
    (trigger?.isConnected ? trigger : byId("event-history-open"))?.focus();
  });
  window.EventHistory = {open: openEvent, attach(card, id) {
    const b = button("Event Details", () => openEvent(id), "event-details-button");
    b.setAttribute("aria-label", "View details for event " + id);
    card.append(b); card.classList.add("warning-clickable");
    card.addEventListener("click", event => {
      if (event.target.closest("button,a,input,select,textarea,summary") || window.getSelection()?.toString()) return;
      void openEvent(id);
    });
  }};
})();
