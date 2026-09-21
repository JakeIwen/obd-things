"use strict";

const byId = (id) => document.getElementById(id);
const profileManager = window.VanDashboardProfiles;
let acquisitionEnabled = false;
let dtcJobsEnabled = false;
let dtcLegacyTokenRequired = false;
let dtcJobPollTimer = null;
let dtcLastJobState = null;
let dtcCancelRequested = false;
let settings = profileManager.loadSettings();
let lastSnapshot = {
  status: {},
  catalog: [],
  metrics: {},
};
let supplemental = {history: {}, earlyWarnings: {}, dtcs: {}, maintenance: {}};
let pendingOilChange = null;
let oilChangeSaving = false;
let supplementalRequestSequence = 0;
let supplementalRequestStartedEpochMs = null;
let supplementalRequestInFlight = null;
let acceptedDelivery = null;
let serverMonotonicOffsetMs = null;
let serverMonotonicUncertaintyMs = null;
let eventStream = null;
let streamGeneration = 0;
let streamAccepting = false;
let resyncGeneration = 0;
let resyncRetryTimer = null;
let httpRequestSequence = 0;
let latestHttpResponseSequence = 0;
let lastAcceptedMonotonicMs = null;
let ageCursorMonotonicMs = null;
const retiredInstances = new Set();
let lastProfileRenderKey = null;
let lastLayoutEditorSignature = null;
let lastAppliedLayoutSignature = null;
let lastRoleGridSignature = null;
let lastCatalogSignature = null;
let additionalMetricStructureKey = null;
let warningChatLoading = false;
const additionalMetricNodes = new Map();

const DRIVER_QUALITIES = new Set(["verified", "observed_alfa_scale"]);
// Historical display policy only: never changes freshness, alarms or CAN gates.
// Unlisted metrics (including operational states and diagnostic raw bytes) are live-only.
const RETAIN_LAST_READING = new Set([
  "battery.voltage", "vehicle.odometer", "engine.oil_life_remaining",
  "engine.coolant_temperature", "engine.vvt_oil_temperature", "transmission.oil_temperature",
  "tire.pressure.fl", "tire.pressure.fr", "tire.pressure.rl", "tire.pressure.rr",
  "radar.alignment.elevation", "radar.alignment.azimuth",
]);
const RETAIN_CANDIDATE_ESTIMATES = new Set([
  "vehicle.odometer", "radar.alignment.elevation", "radar.alignment.azimuth",
]);
const MAX_STATE_FALLBACK_AGE_MS = 5000;
const MAX_STREAM_DELIVERY_AGE_MS = 10000;
const MAX_HTTP_ROUND_TRIP_MS = 2000;
const STREAM_STALL_RESYNC_MS = 3000;
const FRESHNESS_TICK_MS = 1000;
const RESYNC_RETRY_MS = 2000;
const SUPPLEMENTAL_REFRESH_MS = 60000;
const DRIVE_METRICS = Object.freeze({
  speed: {
    names: ["vehicle.speed", "cluster.vehicle_speed"],
    roles: ["drive_speed", "vehicle_speed"],
  },
  rpm: {
    names: ["engine.rpm", "cluster.engine_rpm"],
    roles: ["drive_rpm", "engine_rpm"],
  },
  gear: {
    names: ["transmission.gear", "cluster.actual_gear"],
    roles: ["drive_gear", "transmission_gear"],
  },
  ignition: {
    names: ["vehicle.ignition_on", "vehicle.ignition"],
    roles: ["drive_ignition", "ignition"],
  },
  odometer: {
    names: ["vehicle.odometer"],
    roles: ["vehicle_odometer"],
  },
});
const ENGINE_HEALTH_METRICS = Object.freeze({
  oilPressure: {
    id: "oil-pressure",
    names: ["engine.oil_pressure", "engine.oil_pressure_kpa"],
    roles: ["engine_oil_pressure", "oil_pressure"],
  },
  coolantTemperature: {
    id: "coolant-temperature",
    names: ["engine.coolant_temperature", "engine.coolant_temp"],
    roles: ["engine_coolant_temperature", "coolant_temperature"],
  },
  oilTemperature: {
    id: "oil-temperature",
    names: ["engine.vvt_oil_temperature"],
    roles: ["engine_vvt_oil_temperature"],
  },
  transmissionOilTemperature: {
    id: "transmission-oil-temperature",
    names: [
      "transmission.oil_temperature",
      "transmission.fluid_temperature",
    ],
    roles: [
      "transmission_oil_temperature",
      "transmission_fluid_temperature",
    ],
  },
  torque: {
    id: "torque",
    names: [
      "engine.crankshaft_torque",
      "engine.current_torque",
      "engine.torque",
    ],
    roles: ["engine_crankshaft_torque", "engine_torque"],
  },
  power: {
    id: "power",
    names: ["engine.crankshaft_power", "engine.power"],
    roles: ["engine_crankshaft_power", "engine_power"],
  },
});
const CHARGING_METRICS = Object.freeze({
  generatorFieldDuty: {
    id: "generator-field-duty",
    names: ["generator.field_duty"],
    roles: ["generator_field_duty"],
  },
});
const TIRE_METRICS = Object.freeze({
  fl: {
    names: [
      "tire.pressure.fl",
      "tpms.pressure.fl",
      "tire.pressure.front_left",
    ],
    roles: ["tire_pressure_fl", "tire_pressure_front_left"],
  },
  fr: {
    names: [
      "tire.pressure.fr",
      "tpms.pressure.fr",
      "tire.pressure.front_right",
    ],
    roles: ["tire_pressure_fr", "tire_pressure_front_right"],
  },
  rl: {
    names: [
      "tire.pressure.rl",
      "tpms.pressure.rl",
      "tire.pressure.rear_left",
    ],
    roles: ["tire_pressure_rl", "tire_pressure_rear_left"],
  },
  rr: {
    names: [
      "tire.pressure.rr",
      "tpms.pressure.rr",
      "tire.pressure.rear_right",
    ],
    roles: ["tire_pressure_rr", "tire_pressure_rear_right"],
  },
});

function text(id, value, fallback = "—") {
  const element = byId(id);
  const next = value == null || value === "" ? fallback : String(value);
  if (element.textContent !== next) element.textContent = next;
}

function elementText(element, value, fallback = "—") {
  const next = value == null || value === "" ? fallback : String(value);
  if (element.textContent !== next) element.textContent = next;
}

function humanize(value) {
  return String(value || "unknown").replaceAll("_", " ");
}

function formatAge(ageMs) {
  if (ageMs == null) return "—";
  const seconds = Number(ageMs) / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  if (seconds < 3600) return `${(seconds / 60).toFixed(1)} min`;
  return `${(seconds / 3600).toFixed(1)} hr`;
}

function snapshotDelivery(snapshot) {
  const delivery = snapshot?.web_delivery;
  if (
    !delivery ||
    typeof delivery.instance_id !== "string" ||
    !delivery.instance_id ||
    !Number.isSafeInteger(delivery.sequence) ||
    delivery.sequence < 1 ||
    !Number.isSafeInteger(delivery.generated_at_ms) ||
    delivery.generated_at_ms < 1 ||
    !Number.isSafeInteger(delivery.generated_monotonic_ms) ||
    delivery.generated_monotonic_ms < 0
  ) {
    return null;
  }
  return {
    instanceId: delivery.instance_id,
    sequence: delivery.sequence,
    generatedAtMs: delivery.generated_at_ms,
    generatedMonotonicMs: delivery.generated_monotonic_ms,
  };
}

function retireInstance(instanceId) {
  retiredInstances.add(instanceId);
  if (retiredInstances.size > 8) {
    retiredInstances.delete(retiredInstances.values().next().value);
  }
}

function metricDefinitions(snapshot) {
  return new Map(
    (Array.isArray(snapshot?.catalog) ? snapshot.catalog : [])
      .map((definition) => [definition.name, definition]),
  );
}

function validAgeMs(value) {
  return (
    typeof value === "number" &&
    Number.isFinite(value) &&
    value >= 0
  );
}

function metricWouldExpire(metric, definition, addedAgeMs) {
  if (!metric?.available || metric.stale) {
    return false;
  }
  if (
    !validAgeMs(metric.age_ms) ||
    definition?.stale_after_seconds == null
  ) {
    return true;
  }
  const ageMs = Number(metric.age_ms);
  const staleAfterMs = Number(definition.stale_after_seconds) * 1000;
  return (
    !Number.isFinite(staleAfterMs) ||
    staleAfterMs < 0 ||
    ageMs + addedAgeMs > staleAfterMs
  );
}

function snapshotWouldExpire(snapshot, addedAgeMs) {
  const definitions = metricDefinitions(snapshot);
  if (
    Object.entries(snapshot?.metrics || {}).some(
      ([name, metric]) => (
        metricWouldExpire(metric, definitions.get(name), addedAgeMs)
      ),
    )
  ) {
    return true;
  }
  const vehicle = snapshot?.status?.vehicle_state;
  if (normalizedQuality(vehicle?.confidence) !== "verified") return false;
  if (!validAgeMs(vehicle?.age_ms)) return true;
  return (
    Number(vehicle.age_ms) + addedAgeMs > MAX_STATE_FALLBACK_AGE_MS
  );
}

function ageMetric(metric, definition, addedAgeMs) {
  if (!metric) return;
  if (!validAgeMs(metric.age_ms)) {
    if (metric.available) metric.stale = true;
    return;
  }
  const ageMs = Number(metric.age_ms);
  metric.age_ms = Math.round(Math.max(0, ageMs) + addedAgeMs);
  const staleAfterMs = Number(definition?.stale_after_seconds) * 1000;
  if (
    metric.available &&
    (
      !Number.isFinite(staleAfterMs) ||
      staleAfterMs < 0 ||
      metric.age_ms > staleAfterMs
    )
  ) {
    metric.stale = true;
  }
}

function ageSnapshot(snapshot, addedAgeMs) {
  if (!Number.isFinite(addedAgeMs) || addedAgeMs < 0) return;
  const definitions = metricDefinitions(snapshot);
  Object.entries(snapshot?.metrics || {}).forEach(([name, metric]) => {
    ageMetric(metric, definitions.get(name), addedAgeMs);
  });
  Object.entries(snapshot?.status?.cached_metrics || {}).forEach(
    ([name, metric]) => {
      ageMetric(metric, definitions.get(name), addedAgeMs);
    },
  );
  const vehicle = snapshot?.status?.vehicle_state;
  if (vehicle && validAgeMs(vehicle.age_ms)) {
    vehicle.age_ms = Math.round(
      Math.max(0, Number(vehicle.age_ms)) + addedAgeMs,
    );
    if (
      normalizedQuality(vehicle.confidence) === "verified" &&
      vehicle.age_ms > MAX_STATE_FALLBACK_AGE_MS
    ) {
      vehicle.state = "unknown";
      vehicle.running = null;
      vehicle.confidence = "stale";
      vehicle.basis = "client_freshness_expired";
      vehicle.detail = (
        "No newer vehicle-state snapshot arrived before the " +
        "freshness window expired."
      );
    }
  } else if (
    vehicle &&
    normalizedQuality(vehicle.confidence) === "verified"
  ) {
    vehicle.state = "unknown";
    vehicle.running = null;
    vehicle.confidence = "stale";
    vehicle.basis = "client_freshness_invalid";
    vehicle.detail = "Vehicle-state evidence lacked a valid age.";
  }
}

function acceptSnapshot(snapshot, source, httpTiming = null) {
  const delivery = snapshotDelivery(snapshot);
  if (!delivery) return {accepted: false, reason: "missing_delivery_metadata"};
  if (retiredInstances.has(delivery.instanceId)) {
    return {accepted: false, reason: "retired_instance"};
  }
  let deliveryAgeMs = 0;
  if (source === "stream") {
    if (!streamAccepting || document.visibilityState === "hidden") {
      return {accepted: false, reason: "stream_blocked"};
    }
    if (
      !acceptedDelivery ||
      delivery.instanceId !== acceptedDelivery.instanceId
    ) {
      return {accepted: false, reason: "instance_changed"};
    }
    if (serverMonotonicOffsetMs == null) {
      return {accepted: false, reason: "http_resync_required"};
    }
    if (serverMonotonicUncertaintyMs == null) {
      return {accepted: false, reason: "http_resync_required"};
    }
    deliveryAgeMs = Math.max(
      0,
      performance.now() -
      (
        delivery.generatedMonotonicMs +
        serverMonotonicOffsetMs
      )
    );
    // The HTTP midpoint estimate can differ from the true server/client
    // monotonic offset by at most half the bounded round trip. Carry that
    // uncertainty forward so a stream event is never made younger by
    // calibration. Wall-clock steps cannot affect this calculation.
    deliveryAgeMs += serverMonotonicUncertaintyMs;
    if (
      deliveryAgeMs > MAX_STREAM_DELIVERY_AGE_MS ||
      snapshotWouldExpire(snapshot, deliveryAgeMs)
    ) {
      return {accepted: false, reason: "queued_stream_event"};
    }
  } else if (source === "http") {
    if (
      !httpTiming ||
      !Number.isFinite(httpTiming.roundTripMs) ||
      !Number.isFinite(httpTiming.clientMidpointMonotonicMs) ||
      !Number.isFinite(httpTiming.clientReceiptMonotonicMs)
    ) {
      return {accepted: false, reason: "http_timing_required"};
    }
    if (
      httpTiming.roundTripMs < 0 ||
      httpTiming.roundTripMs > MAX_HTTP_ROUND_TRIP_MS
    ) {
      return {accepted: false, reason: "http_response_delayed"};
    }
    // Generation may occur as early as request arrival. The full bounded
    // round trip is the conservative upper bound on age at receipt.
    deliveryAgeMs = httpTiming.roundTripMs;
  } else {
    return {accepted: false, reason: "unsupported_delivery_source"};
  }
  if (
    acceptedDelivery &&
    delivery.instanceId === acceptedDelivery.instanceId
  ) {
    if (
      delivery.sequence <= acceptedDelivery.sequence ||
      delivery.generatedMonotonicMs <
        acceptedDelivery.generatedMonotonicMs
    ) {
      return {accepted: false, reason: "out_of_order"};
    }
  } else if (acceptedDelivery) {
    if (source !== "http") {
      return {accepted: false, reason: "instance_changed"};
    }
    retireInstance(acceptedDelivery.instanceId);
  }
  if (source === "http") {
    serverMonotonicOffsetMs = (
      httpTiming.clientMidpointMonotonicMs -
      delivery.generatedMonotonicMs
    );
    serverMonotonicUncertaintyMs = httpTiming.roundTripMs / 2;
  }
  ageSnapshot(snapshot, deliveryAgeMs);
  acceptedDelivery = delivery;
  lastAcceptedMonotonicMs = performance.now();
  ageCursorMonotonicMs = lastAcceptedMonotonicMs;
  render(snapshot);
  return {accepted: true, reason: "accepted"};
}

function yesNoUnknown(value) {
  if (value === true) return "Yes";
  if (value === false) return "No";
  return "Unknown";
}

function normalizedQuality(value) {
  return String(value || "unknown").toLowerCase().replaceAll(" ", "_");
}

function definitionQuality(definition) {
  const qualities = (definition?.sources || [])
    .map((source) => normalizedQuality(source.quality))
    .filter((quality) => quality !== "unknown");
  if (!qualities.length) return "unknown";
  return qualities.every((quality) => quality === qualities[0])
    ? qualities[0]
    : "mixed";
}

function observationState(definition, metric) {
  const quality = normalizedQuality(metric?.quality || definitionQuality(definition));
  const available = Boolean(metric?.available);
  const ageValid = validAgeMs(metric?.age_ms);
  const staleByAge = (
    ageValid &&
    definition?.stale_after_seconds != null &&
    Number(metric.age_ms) > Number(definition.stale_after_seconds) * 1000
  );
  const stale = Boolean(
    metric?.stale ||
    staleByAge ||
    (available && !ageValid)
  );
  return {
    available,
    stale,
    quality,
    driverQualified: DRIVER_QUALITIES.has(quality),
    heroReady: available && !stale && DRIVER_QUALITIES.has(quality),
  };
}

function displayQuality(quality) {
  if (quality === "verified") return "";
  if (quality === "observed_alfa_scale") return "ALFA SCALE";
  return humanize(quality).toUpperCase();
}

function lastRecordedObservation(definition, metric) {
  if (!definition || !RETAIN_LAST_READING.has(definition.name)) return null;
  const valid = (sample) => {
    if (!sample || typeof sample.value !== "number" || !Number.isFinite(sample.value) ||
        sample.unit !== definition.unit || !Number.isFinite(Date.parse(sample.observed_at)) ||
        Date.parse(sample.observed_at) > Date.now()) return false;
    if (Number.isFinite(definition.minimum) && sample.value < definition.minimum) return false;
    if (Number.isFinite(definition.maximum) && sample.value > definition.maximum) return false;
    const source = definition.sources?.find((item) => item.name === sample.source);
    const quality = normalizedQuality(sample.quality);
    return Boolean(source && normalizedQuality(source.quality) === quality &&
      (DRIVER_QUALITIES.has(quality) ||
        (quality === "candidate" && RETAIN_CANDIDATE_ESTIMATES.has(definition.name))));
  };
  return [metric?.available ? metric : null, metric?.last_recorded]
    .filter(valid).sort((a, b) => Date.parse(b.observed_at) - Date.parse(a.observed_at))[0] || null;
}

function lastRecordedStatus(sample, current) {
  return formatTimestamp(sample.observed_at);
}

function markRetained(element, retained) {
  const next = retained ? "true" : "false";
  if (element.dataset.retained !== next) element.dataset.retained = next;
}

function sectionReadingStatus(live, mapped, total, retained = 0, quality = "") {
  return [
    live > 0 ? `${live}/${total} LIVE` : "NOT LIVE",
    mapped < total ? `${mapped}/${total} MAPPED` : null,
    retained > 0 ? `${retained}/${total} LAST READINGS` : null,
    quality,
  ].filter(Boolean).join(" · ");
}

function metricRole(definition) {
  return (
    definition?.display_role ||
    definition?.presentation?.role ||
    ""
  );
}

function findDefinition(catalog, descriptor) {
  const names = new Set(descriptor.names);
  const roles = new Set(descriptor.roles);
  return catalog.find(
    (definition) => (
      names.has(definition.name) ||
      roles.has(metricRole(definition))
    ),
  );
}

function formatMetricValue(name, value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return String(value);
  if (name === "generator.field_duty") return value.toFixed(3);
  if (name === "vehicle.odometer") return Math.trunc(value).toLocaleString();
  if (name.includes("rpm")) return Math.round(value).toLocaleString();
  if (name.includes("gear")) return String(value);
  if (name.includes("speed") || name.includes("pressure")) return value.toFixed(1);
  if (name === "battery.voltage") return value.toFixed(2);
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

function setCardState(id, state) {
  const card = byId(id);
  if (card.dataset.state !== state) card.dataset.state = state;
}

function metricStatus(definition, metric, state) {
  if (!definition) return "UNMAPPED";
  if (!state.available) {
    return humanize(metric?.reason || "no cached sample");
  }
  if (metric?.last_acquisition_error?.reason === "implausible_transition") {
    return `SUSPECT SAMPLE REJECTED · showing last good · ${formatAge(metric.age_ms)} old`;
  }
  if (state.stale) return `STALE · ${formatAge(metric.age_ms)} old`;
  if (!state.driverQualified) {
    return `${displayQuality(state.quality)} · diagnostics only`;
  }
  return [displayQuality(state.quality), `${formatAge(metric.age_ms)} old`].filter(Boolean).join(" · ");
}

function updateMetricPanelSummary(profileId) {
  const cards = [...document.querySelectorAll("#metric-grid .metric-card")];
  const showDiagnostics = profileId === "diagnostics";
  const visibleCards = showDiagnostics
    ? cards
    : cards.filter((card) => !card.classList.contains("diagnostic-only"));
  text("metric-count", visibleCards.length);
  byId("metric-count").title = (
    showDiagnostics
      ? "Additional driver-facing and diagnostic metrics"
      : "Additional driver-facing metrics"
  );
  byId("metrics-empty").hidden = visibleCards.length > 0;
  text(
    "metrics-empty",
    showDiagnostics
      ? "No additional diagnostic or driver-qualified metrics are registered yet."
      : "No additional driver-qualified metrics are registered yet.",
  );
}

function setProfile() {
  const effective = profileManager.resolve(
    settings,
    lastSnapshot.status?.vehicle_state,
  );
  const renderKey = JSON.stringify({
    selected: settings.selected,
    customLayout: settings.customLayout,
    id: effective.id,
    title: effective.title,
    reason: effective.reason,
    widgets: effective.widgets,
    rows: effective.rows,
  });
  if (renderKey === lastProfileRenderKey) return;
  lastProfileRenderKey = renderKey;
  const layoutSignature = JSON.stringify([effective.rows, effective.hidden]);
  if (layoutSignature !== lastAppliedLayoutSignature) {
    lastAppliedLayoutSignature = layoutSignature;
    const visible = new Set(effective.widgets);
    const panels = new Map();
    document.querySelectorAll("[data-widget]").forEach((panel) => {
      panel.hidden = !visible.has(panel.dataset.widget);
      panels.set(panel.dataset.widget, panel);
    });
    const customizer = document.querySelector(".customizer");
    [...effective.rows.flat(), ...effective.hidden].forEach((entry) => {
      const panel = panels.get(entry.id);
      panel.dataset.width = entry.width;
      customizer.parentElement.insertBefore(panel, customizer);
    });
  }
  document.body.dataset.profile = effective.id;
  updateMetricPanelSummary(effective.id);
  text("dashboard-title", effective.title);
  text("profile-reason", effective.reason);
  byId("profile").value = settings.selected;
  const customizer = document.querySelector(".customizer");
  customizer.hidden = effective.id !== "custom";
  if (!customizer.hidden) renderLayoutEditor(effective);
}

function editLayout(action) {
  const focusId = document.activeElement?.id;
  const current = profileManager.customize(settings, lastSnapshot.status?.vehicle_state);
  settings = profileManager.saveSettings(action(current));
  setProfile();
  if (focusId) byId(focusId)?.focus({preventScroll: true});
}

function renderLayoutEditor(effective) {
  const signature = JSON.stringify([effective.id, effective.rows, effective.hidden]);
  if (signature === lastLayoutEditorSignature) return;
  lastLayoutEditorSignature = signature;
  const root = byId("widget-options");
  root.replaceChildren();
  const definitions = new Map(profileManager.widgets.map((widget) => [widget.id, widget]));
  const halfTiles = effective.rows.flat().filter((entry) => entry.width === "half");
  text("layout-editor-note", effective.id === "custom"
    ? "Editing Custom · saved on this device. Rows below match the dashboard; phones stack each pair."
    : `Showing ${effective.label}. Any edit copies this view to Custom; the preset stays unchanged.`);
  function tileEditor(entry, partner) {
    const widget = definitions.get(entry.id);
    const card = document.createElement("div");
    card.className = "layout-tile";
    card.dataset.layoutWidget = entry.id;
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.id = `layout-visible-${entry.id}`;
    input.type = "checkbox";
    input.checked = entry.visible;
    input.addEventListener("change", () => {
      editLayout((current) => profileManager.changeTile(current, entry.id, {visible: input.checked}));
    });
    label.append(input, document.createTextNode(widget.label));
    card.append(label);
    if (widget.widths.length > 1) {
      const widthLabel = document.createElement("label");
      widthLabel.textContent = "Width";
      const width = document.createElement("select");
      width.id = `layout-width-${entry.id}`;
      width.setAttribute("aria-label", `${widget.label} width`);
      widget.widths.forEach((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value === "half" ? "Half" : "Full";
        width.append(option);
      });
      width.value = entry.width;
      width.addEventListener("change", () => editLayout(
        (current) => profileManager.changeTile(current, entry.id, {width: width.value}),
      ));
      widthLabel.append(width);
      card.append(widthLabel);
    } else {
      const note = document.createElement("p");
      note.className = "muted";
      note.textContent = "Full width required";
      card.append(note);
    }
    if (entry.visible && entry.width === "half" && halfTiles.length > 1) {
      const pairLabel = document.createElement("label");
      pairLabel.textContent = "Pair with";
      const pair = document.createElement("select");
      pair.id = `layout-pair-${entry.id}`;
      pair.setAttribute("aria-label", `Pair ${widget.label} with`);
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "Choose tile";
      pair.append(placeholder);
      halfTiles.filter((other) => other.id !== entry.id).forEach((other) => {
        const option = document.createElement("option");
        option.value = other.id;
        option.textContent = definitions.get(other.id).label;
        pair.append(option);
      });
      pair.value = partner?.id || "";
      pair.addEventListener("change", () => {
        if (pair.value) editLayout((current) => profileManager.pairTiles(current, entry.id, pair.value));
      });
      pairLabel.append(pair);
      card.append(pairLabel);
    }
    return card;
  }
  effective.rows.forEach((row, index) => {
    const section = document.createElement("section");
    section.className = "layout-row";
    section.dataset.layoutRow = String(index);
    const header = document.createElement("div");
    header.className = "layout-row-heading";
    const title = document.createElement("h3");
    title.textContent = `Row ${index + 1}`;
    header.append(title);
    const actions = document.createElement("div");
    actions.className = "button-row";
    for (const [label, direction] of [["Move Up", -1], ["Move Down", 1]]) {
      const button = document.createElement("button");
      button.id = `layout-${direction < 0 ? "up" : "down"}-${row[0].id}`;
      button.type = "button";
      button.textContent = label;
      button.setAttribute("aria-label", `${label}: row ${index + 1}`);
      button.disabled = index + direction < 0 || index + direction >= effective.rows.length;
      button.addEventListener("click", () => editLayout(
        (current) => profileManager.moveRow(current, index, direction),
      ));
      actions.append(button);
    }
    if (row.length === 2) {
      const swap = document.createElement("button");
      swap.id = `layout-swap-${row.map((entry) => entry.id).sort().join("-")}`;
      swap.type = "button";
      swap.textContent = "Swap Tiles";
      swap.setAttribute("aria-label", `Swap tiles in row ${index + 1}`);
      swap.addEventListener("click", () => editLayout((current) => profileManager.swapRow(current, index)));
      actions.append(swap);
    }
    header.append(actions);
    const tiles = document.createElement("div");
    tiles.className = "layout-row-tiles";
    row.forEach((entry, position) => tiles.append(tileEditor(entry, row[1 - position])));
    section.append(header, tiles);
    root.append(section);
  });
  const hidden = byId("hidden-widget-options");
  hidden.replaceChildren();
  effective.hidden.forEach((entry) => hidden.append(tileEditor(entry)));
  byId("hidden-widgets").hidden = effective.hidden.length === 0;
}

function setupProfiles() {
  const panels = [...document.querySelectorAll("main > section.panel")];
  const known = new Set(profileManager.widgets.map((widget) => widget.id));
  if (panels.length !== known.size || panels.some((panel) => !known.has(panel.dataset.widget)) ||
      new Set(panels.map((panel) => panel.dataset.widget)).size !== known.size) {
    throw new Error("Dashboard panels and widget registry must match exactly");
  }
  const select = byId("profile");
  const choices = [["auto", "Automatic"],
    ...Object.entries(profileManager.profiles).map(([id, profile]) => [id, profile.label]),
    ["custom", "Custom"]];
  choices.forEach(([value, label]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.append(option);
  });
  byId("customize-current").addEventListener("click", () => editLayout((current) => current));
  select.addEventListener("change", () => {
    settings = profileManager.saveSettings({
      ...settings,
      selected: select.value,
    });
    setProfile();
  });
  byId("reset-layout").addEventListener("click", () => {
    settings = profileManager.saveSettings(profileManager.defaultSettings());
    setProfile();
  });
  setProfile();
}

function renderVehicleState(status) {
  const vehicle = status.vehicle_state || {};
  text("vehicle-state", humanize(vehicle.state));
  text("running-state", yesNoUnknown(vehicle.running));
  const confidence = byId("state-confidence");
  const verified = normalizedQuality(vehicle.confidence) === "verified";
  text("state-confidence", verified ? "" : humanize(vehicle.confidence), "");
  if (confidence.parentElement) confidence.parentElement.hidden = verified;
  text("state-basis", humanize(vehicle.basis));
  text("state-age", formatAge(vehicle.age_ms));
  text("state-detail", vehicle.detail, "No passive state detail is available.");
}

function renderRadarAlignment(status, catalog, metrics) {
  const summaries = status.radar_alignment || {};
  const format = (value) => Number.isFinite(value)
    ? `${value >= 0 ? "+" : ""}${value.toFixed(3)}°` : "—";
  let freshCount = 0;
  let retainedCount = 0;
  let maxMagnitude = 0;
  ["elevation", "azimuth"].forEach((axis) => {
    const name = `radar.alignment.${axis}`;
    const metric = metrics[name];
    const definition = catalog.find((item) => item.name === name);
    const state = observationState(definition, metric);
    const fresh = state.available && !state.stale && Number.isFinite(metric.value);
    const windows = summaries[name] || {};
    const retainedSample = lastRecordedObservation(definition, metric);
    const latest = Number.isFinite(windows.latest_value) && windows.observed_at
      ? {value: windows.latest_value, observed_at: windows.observed_at} : retainedSample;
    const retained = Boolean(latest);
    const observedAt = !fresh && retained ? latest.observed_at : null;
    const timestamp = byId(`radar-${axis}-time`);
    text(`radar-${axis}-time`, observedAt ? formatTimestamp(observedAt) : "", "");
    if (timestamp.dateTime !== (observedAt || "")) timestamp.dateTime = observedAt || "";
    timestamp.hidden = !observedAt;
    markRetained(timestamp, false);
    markRetained(byId(`radar-${axis}-margin`), false);
    if (!fresh && retained) retainedCount += 1;
    text(`radar-${axis}-current`, fresh ? format(metric.value)
      : retained ? format(latest.value) : "—");
    for (const seconds of [60, 300]) {
      const window = windows[String(seconds)];
      text(`radar-${axis}-${seconds}`, (fresh || retained) && window?.count >= 2 ? format(window.mean) : "—");
    }
    if (fresh) {
      freshCount += 1;
      const magnitude = Math.abs(metric.value);
      maxMagnitude = Math.max(maxMagnitude, magnitude);
      const remaining = 1 - magnitude;
      text(`radar-${axis}-margin`, remaining >= 0
        ? `${remaining.toFixed(3)}° to ±1.00° reference · ${formatAge(metric.age_ms)} old`
        : `${(-remaining).toFixed(3)}° beyond ±1.00° reference · ${formatAge(metric.age_ms)} old`);
      const peak = windows["300"]?.peak_abs;
      text(`radar-${axis}-coverage`, Number.isFinite(peak)
        ? `5 min peak |angle| ${peak.toFixed(3)}°` : "", "");
    } else if (retained) {
      text(`radar-${axis}-margin`, "", "");
      const peak = windows["300"]?.peak_abs;
      text(`radar-${axis}-coverage`, Number.isFinite(peak)
        ? `Last 5 min peak |angle| ${peak.toFixed(3)}°` : "", "");
    } else {
      text(`radar-${axis}-margin`, metric?.available
        ? `Last estimate ${format(metric.value)} · stale (${formatAge(metric.age_ms)} old)`
        : status.radar_alignment_polling?.commissioned === false
          ? status.radar_alignment_polling.detail
          : metric?.detail || "Awaiting commissioned radar angle readings.");
      text(`radar-${axis}-coverage`, "No current rolling average", "");
    }
    byId(`radar-${axis}-margin`).hidden = !byId(`radar-${axis}-margin`).textContent;
  });
  // Show the shared unavailable explanation once, beneath the second axis.
  // Keep axis-specific margins and peak values when readings are present.
  const duplicateUnavailable = byId("radar-elevation-coverage").textContent === "No current rolling average" &&
    byId("radar-azimuth-coverage").textContent === "No current rolling average" &&
    byId("radar-elevation-margin").textContent === byId("radar-azimuth-margin").textContent;
  byId("radar-elevation-margin").hidden = duplicateUnavailable || !byId("radar-elevation-margin").textContent;
  byId("radar-elevation-coverage").hidden = duplicateUnavailable;
  const badge = freshCount < 2 ? (retainedCount ? "LAST RECORDED · NOT LIVE" : "NO LIVE ANGLE DATA")
    : maxMagnitude >= 1 ? "OUTSIDE ±1° REFERENCE"
    : maxMagnitude >= .8 ? "APPROACHING ±1°" : "WITHIN ±1° REFERENCE";
  text("radar-state", badge);
  markRetained(byId("radar-state"), freshCount < 2 && retainedCount > 0);
  setCardState("radar-state", freshCount < 2 ? "unavailable"
    : maxMagnitude >= 1 ? "outside" : maxMagnitude >= .8 ? "watch" : "normal");
  text("radar-note", "±1.00° monitoring reference");
}

function serviceMileageLabel(source) {
  return source === "ics_estimate" ? "ICS estimate*"
    : source === "cluster_or_receipt" ? "Cluster / service receipt" : "Mileage not recorded";
}

function renderServiceMileage(metrics) {
  const current = metrics["vehicle.odometer"];
  const retained = supplemental.maintenance?.last_known_odometer;
  const definition = lastSnapshot.catalog?.find((item) => item.name === "vehicle.odometer");
  const recorded = lastRecordedObservation(definition, current);
  const chosen = current?.available && Number.isFinite(current.value) ? current : recorded || retained;
  text("service-odometer", chosen && Number.isFinite(chosen.value)
    ? `${Math.trunc(chosen.value).toLocaleString()} mi` : "—");
  const live = current?.available && !current.stale && validAgeMs(current.age_ms) && current.age_ms <= 15000;
  const freshness = chosen
    ? live ? `Latest ICS reading · ${formatAge(current.age_ms)} old`
      : formatTimestamp(chosen.observed_at)
    : "No ICS mileage has been recorded yet.";
  const odometer = byId("service-odometer");
  const tooltip = `${odometer.dataset.sourceNote || "ICS module estimate."} ${freshness}`;
  if (odometer.title !== tooltip) odometer.title = tooltip;
  const last = supplemental.maintenance?.last_oil_change;
  if (last?.mileage_source === "ics_estimate" && Number.isFinite(last.mileage_mi) && chosen) {
    const distance = chosen.value - last.mileage_mi;
    text("oil-distance", distance >= 0
      ? `${distance.toLocaleString(undefined, {maximumFractionDigits: 1})} estimated miles since service${live ? "" : " at last reading"}`
      : "Service mileage exceeds the latest ICS reading; check the entry.");
  } else {
    text("oil-distance", last ? "Distance since service is not calculated across different mileage sources." : "", "");
  }
}

function renderOilLife(catalog, metrics) {
  const definition = catalog.find((item) => item.name === "engine.oil_life_remaining");
  const metric = definition ? metrics[definition.name] : null;
  const state = observationState(definition, metric);
  const validPercent = (sample) => sample?.unit === "%" && typeof sample.value === "number" &&
    Number.isFinite(sample.value) && sample.value >= 0 && sample.value <= 100;
  const live = state.heroReady && validPercent(metric);
  const retained = lastRecordedObservation(definition, metric);
  const sample = live ? metric : validPercent(retained) ? retained : null;
  markRetained(byId("service-oil-life-detail"), false);
  text("service-oil-life", sample ? `${formatMetricValue(definition.name, sample.value)}%` : "—");
  text("service-oil-life-detail", sample
    ? live ? metricStatus(definition, metric, state) : lastRecordedStatus(sample, metric)
    : definition ? metricStatus(definition, metric, state)
      : "UNMAPPED");
}

function renderMaintenance(payload) {
  const data = payload || {};
  const last = data.last_oil_change;
  renderOilLife(lastSnapshot.catalog || [], lastSnapshot.metrics || {});
  text("oil-last-date", last?.date || "No service recorded");
  text("oil-last-mileage", last && Number.isFinite(last.mileage_mi)
    ? `${last.mileage_mi.toLocaleString()} mi` : "Mileage not recorded");
  text("oil-last-notes", last?.notes || "", "");
  const history = byId("oil-change-history");
  history.replaceChildren();
  (data.oil_changes || []).forEach((record) => {
    const item = document.createElement("li");
    item.textContent = `${record.date} · ${Number.isFinite(record.mileage_mi) ? `${record.mileage_mi.toLocaleString()} mi · ` : ""}` +
      serviceMileageLabel(record.mileage_source) + (record.notes ? ` · ${record.notes}` : "");
    history.append(item);
  });
  byId("oil-change-save").disabled = oilChangeSaving || !data.available || !data.persistent;
  if (data.storage_error) text("oil-change-result", data.storage_error);
  renderServiceMileage(lastSnapshot.metrics || {});
}

async function saveOilChange(event) {
  event.preventDefault();
  if (oilChangeSaving || byId("oil-change-save").disabled) return;
  const rawMileage = byId("oil-change-mileage").value.trim();
  const fields = {
    date: byId("oil-change-date").value,
    mileage_mi: rawMileage === "" ? null : Number(rawMileage),
    mileage_source: rawMileage === "" ? "unknown" : byId("oil-change-source").value,
    notes: byId("oil-change-notes").value.trim(),
  };
  const signature = JSON.stringify(fields);
  if (!pendingOilChange || pendingOilChange.signature !== signature) {
    const random = new Uint32Array(4);
    window.crypto.getRandomValues(random);
    pendingOilChange = {signature, payload: {...fields,
      request_id: `oil_${Array.from(random, v => v.toString(16).padStart(8, "0")).join("")}`}};
  }
  oilChangeSaving = true;
  byId("oil-change-save").disabled = true;
  text("oil-change-result", "Saving…");
  try {
    const response = await fetch("/v1/maintenance/oil-changes", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(pendingOilChange.payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
    pendingOilChange = null;
    const refreshed = await fetch("/v1/maintenance", {cache: "no-store"});
    if (!refreshed.ok) throw new Error("Record saved; reload the page to view it.");
    supplemental.maintenance = await refreshed.json();
    renderMaintenance(supplemental.maintenance);
    text("oil-change-result", "Oil change saved on the Pi. The vehicle’s oil-change indicator was not reset.");
  } catch (error) {
    text("oil-change-result", String(error.message || error));
  } finally {
    oilChangeSaving = false;
    byId("oil-change-save").disabled = !supplemental.maintenance?.available || !supplemental.maintenance?.persistent;
  }
}

function renderHeroMetric(role, catalog, metrics) {
  const definition = findDefinition(catalog, DRIVE_METRICS[role]);
  const metric = definition ? metrics[definition.name] : null;
  const state = observationState(definition, metric);
  const cardId = `drive-${role}-card`;
  const unitId = `drive-${role}-unit`;
  const card = byId(cardId);
  card.hidden = false;
  if (!definition) {
    text(`drive-${role}`, "—");
    if (byId(unitId)) text(unitId, "", "");
    setCardState(cardId, "unavailable");
    text(`drive-${role}-status`, metricStatus(definition, metric, state));
    return {definition, state};
  }
  if (state.heroReady) {
    text(`drive-${role}`, formatMetricValue(definition.name, metric.value));
    if (byId(unitId)) text(unitId, metric.unit || definition.unit, "");
    setCardState(cardId, state.quality);
  } else {
    text(`drive-${role}`, "—");
    if (byId(unitId)) text(unitId, "", "");
    setCardState(
      cardId,
      state.stale ? "stale" : (state.available ? "unqualified" : "unavailable"),
    );
  }
  text(`drive-${role}-status`, metricStatus(definition, metric, state));
  return {definition, state};
}

function ignitionFromVehicleState(status) {
  const vehicle = status.vehicle_state || {};
  const confidence = normalizedQuality(vehicle.confidence);
  const ageMs = validAgeMs(vehicle.age_ms)
    ? Number(vehicle.age_ms)
    : null;
  if (
    confidence !== "verified" ||
    ageMs == null ||
    ageMs > MAX_STATE_FALLBACK_AGE_MS
  ) {
    return null;
  }
  if (vehicle.ignition_on === true) {
    return {value: "ON", detail: "State evidence"};
  }
  if (vehicle.ignition_on === false) {
    return {value: "OFF", detail: "State evidence"};
  }
  if (
    confidence === "verified" &&
    ["moving", "running", "ignition_on"].includes(String(vehicle.state))
  ) {
    return {value: "ON", detail: humanize(vehicle.state)};
  }
  if (confidence === "verified" && vehicle.state === "asleep") {
    return {value: "OFF", detail: "Bus asleep"};
  }
  return null;
}

function renderDrive(status, catalog, metrics) {
  const speed = renderHeroMetric("speed", catalog, metrics);
  const rpm = renderHeroMetric("rpm", catalog, metrics);
  const gear = renderHeroMetric("gear", catalog, metrics);
  const odometerDefinition = findDefinition(catalog, DRIVE_METRICS.odometer);
  const odometerMetric = odometerDefinition
    ? metrics[odometerDefinition.name]
    : null;
  const odometerState = observationState(odometerDefinition, odometerMetric);
  const retainedOdometer = lastRecordedObservation(odometerDefinition, odometerMetric);
  markRetained(byId("drive-odometer-status"), false);
  byId("drive-odometer-card").hidden = false;
  if (odometerState.available && !odometerState.stale) {
    text(
      "drive-odometer",
      formatMetricValue(odometerDefinition.name, odometerMetric.value),
    );
    text(
      "drive-odometer-unit",
      odometerMetric.unit || odometerDefinition.unit,
      "",
    );
    setCardState("drive-odometer-card", "candidate");
    text(
      "drive-odometer-status",
      `CANDIDATE · ${formatAge(odometerMetric.age_ms)} old · validation required`,
    );
  } else if (retainedOdometer) {
    text("drive-odometer", formatMetricValue(odometerDefinition.name, retainedOdometer.value));
    text("drive-odometer-unit", retainedOdometer.unit);
    setCardState("drive-odometer-card", "stale");
    text("drive-odometer-status", lastRecordedStatus(retainedOdometer, odometerMetric));
  } else {
    text("drive-odometer", "—");
    text("drive-odometer-unit", "", "");
    setCardState(
      "drive-odometer-card",
      odometerState.stale ? "stale" : "unavailable",
    );
    text(
      "drive-odometer-status",
      odometerDefinition
        ? `${humanize(odometerMetric?.reason || "not sampled")} · candidate`
        : "UNMAPPED",
    );
  }

  const ignitionDefinition = findDefinition(catalog, DRIVE_METRICS.ignition);
  const ignitionCard = byId("drive-ignition-card");
  ignitionCard.hidden = false;
  const ignitionMetric = ignitionDefinition
    ? metrics[ignitionDefinition.name]
    : null;
  const ignitionState = observationState(ignitionDefinition, ignitionMetric);
  // Never let a stale registered ignition observation be replaced by an
  // apparently authoritative status fallback. A fallback is useful only
  // before the dedicated metric has produced its first sample.
  const stateFallback = !ignitionDefinition || ignitionMetric?.available
    ? null
    : ignitionFromVehicleState(status);
  let ignitionReady = false;
  if (ignitionState.heroReady && typeof ignitionMetric.value === "boolean") {
    text("drive-ignition", ignitionMetric.value ? "ON" : "OFF");
    text(
      "drive-ignition-status",
      [displayQuality(ignitionState.quality), `${formatAge(ignitionMetric.age_ms)} old`].filter(Boolean).join(" · "),
    );
    setCardState("drive-ignition-card", ignitionState.quality);
    ignitionReady = true;
  } else if (stateFallback) {
    text("drive-ignition", stateFallback.value);
    text("drive-ignition-status", stateFallback.detail);
    setCardState("drive-ignition-card", "verified");
    ignitionReady = true;
  } else {
    text("drive-ignition", "—");
    text(
      "drive-ignition-status",
      metricStatus(ignitionDefinition, ignitionMetric, ignitionState),
    );
    setCardState(
      "drive-ignition-card",
      ignitionState.stale
        ? "stale"
        : (ignitionState.available ? "unqualified" : "unavailable"),
    );
  }

  const registered = [
    speed.definition,
    rpm.definition,
    gear.definition,
    ignitionDefinition,
  ].filter(Boolean).length;
  const ready = [speed.state.heroReady, rpm.state.heroReady, gear.state.heroReady, ignitionReady]
    .filter(Boolean).length;
  text(
    "drive-freshness",
    sectionReadingStatus(ready, registered, 4),
  );
  byId("drive-freshness").dataset.state = ready === 4 ? "verified" : "partial";
  text(
    "drive-note",
    ready === 4
      ? "All core drive essentials are fresh. ODOMETER* remains candidate-quality."
      : (
        (registered < 4 ? `${registered}/4 drive sources are mapped. ` : "") +
        "Only fresh, driver-qualified values are promoted here. " +
        "ODOMETER* is shown separately as a candidate needing validation."
      ),
  );
}

function renderEngineMetric(role, catalog, metrics) {
  const descriptor = ENGINE_HEALTH_METRICS[role];
  const definition = findDefinition(catalog, descriptor);
  const metric = definition ? metrics[definition.name] : null;
  const state = observationState(definition, metric);
  const cardId = `engine-${descriptor.id}-card`;
  const valueId = `engine-${descriptor.id}`;
  const unitId = `${valueId}-unit`;
  const retained = state.heroReady ? null : lastRecordedObservation(definition, metric);
  markRetained(byId(`${valueId}-status`), false);
  byId(cardId).hidden = false;
  if (state.heroReady) {
    text(valueId, formatMetricValue(definition.name, metric.value));
    text(unitId, metric.unit || definition.unit, "");
    setCardState(cardId, state.quality);
  } else if (retained) {
    text(valueId, formatMetricValue(definition.name, retained.value));
    text(unitId, retained.unit, "");
    setCardState(cardId, "stale");
  } else {
    text(valueId, "—");
    text(unitId, "", "");
    setCardState(
      cardId,
      state.stale ? "stale" : (state.available ? "unqualified" : "unavailable"),
    );
  }
  text(
    `${valueId}-status`,
    retained ? lastRecordedStatus(retained, metric) : metricStatus(definition, metric, state),
  );
  return {definition, state, retained: Boolean(retained)};
}

function renderOilPressureReference(metrics) {
  const oil = metrics["engine.oil_pressure"];
  const rpm = metrics["engine.rpm"];
  const coolant = metrics["engine.coolant_temperature"];
  const freshNumber = (metric) => (
    metric?.available &&
    !metric.stale &&
    typeof metric.value === "number" &&
    Number.isFinite(metric.value)
  );
  const general = (
    "OEM warm reference: ~650 rpm 15–34 psi · " +
    "1,000–3,000 rpm 28–35 psi · >3,500 rpm 65–80 psi."
  );
  if (![oil, rpm, coolant].every(freshNumber)) {
    text("engine-oil-pressure-reference", general);
    return;
  }
  const engineRpm = Number(rpm.value);
  const coolantF = Number(coolant.value);
  if (coolantF < 192.2 || coolantF > 212.0) {
    text(
      "engine-oil-pressure-reference",
      `OEM pressure bands apply only at 192–212°F coolant; ` +
      `current coolant is ${coolantF.toFixed(0)}°F.`,
    );
    return;
  }
  let band;
  if (engineRpm >= 550 && engineRpm <= 850) {
    band = (
      "nearest warm curb-idle reference (~650 rpm): 15–34 psi " +
      "(550–850 rpm is a UI context window, not an OEM test band)"
    );
  } else if (engineRpm >= 1000 && engineRpm <= 3000) {
    band = "warm 1,000–3,000 rpm: 28–35 psi";
  } else if (engineRpm > 3500) {
    band = "warm above 3,500 rpm: 65–80 psi";
  } else {
    band = "no OEM band is published for this transition RPM";
  }
  text(
    "engine-oil-pressure-reference",
    `OEM context · ${band}. Advisory context only; no alert is inferred.`,
  );
}

function renderEngineHealth(catalog, metrics) {
  const states = Object.keys(ENGINE_HEALTH_METRICS)
    .map((role) => renderEngineMetric(role, catalog, metrics));
  renderOilPressureReference(metrics);
  const total = states.length;
  const mapped = states.filter((state) => state.definition).length;
  const ready = states.filter((state) => state.state.heroReady).length;
  const retained = states.filter((state) => state.retained).length;
  markRetained(byId("engine-health-state"), retained > 0);
  text(
    "engine-health-state",
    sectionReadingStatus(ready, mapped, total, retained),
  );
  byId("engine-health-state").dataset.state = ready === total
    ? "verified"
    : "partial";
  return states;
}

function sourceDefinitionFor(definition, sourceName) {
  const sources = Array.isArray(definition?.sources)
    ? definition.sources
    : [];
  return (
    sources.find((source) => source.name === sourceName) ||
    (sources.length === 1 ? sources[0] : null)
  );
}

function displayInterfaceMode(mode) {
  if (mode === "armed_diagnostic") return "Armed diagnostic";
  if (mode === "listen_only") return "Listen-only";
  if (mode === "unknown") return "Unknown";
  return null;
}

function chargingInterfaceMode(status, metric) {
  const resultMode = displayInterfaceMode(metric?.interface_mode);
  if (resultMode) {
    return `${resultMode} · ${
      metric?.available ? "observation" : "acquisition result"
    }`;
  }
  const activeDriveMode = displayInterfaceMode(
    status?.active_drive?.interface_mode,
  );
  if (
    activeDriveMode &&
    !["idle", "disabled"].includes(status?.active_drive?.state)
  ) {
    return `${activeDriveMode} · collector`;
  }
  const iface = status?.interface || {};
  if (iface.adapter_present === false) return "Adapter absent";
  if (iface.up === false) return "Interface down";
  if (iface.listen_only === true) return "Listen-only";
  if (iface.listen_only === false) return "Armed (not listen-only)";
  return "Unknown";
}

function displayChargingQuality(quality) {
  if (quality === "observed_alfa_scale") return "OBSERVED ALFA SCALE";
  return displayQuality(quality);
}

function chargingInactiveState(definition, metric, state) {
  const lastError = metric?.last_acquisition_error;
  if (lastError?.reason) {
    return {
      reason: String(lastError.reason),
      detail: lastError.detail || "The latest diagnostic poll did not succeed.",
    };
  }
  if (!definition) {
    return {
      reason: "mapping_pending",
      detail: "generator.field_duty is not present in the metric catalog.",
    };
  }
  if (!state.available) {
    return {
      reason: String(metric?.reason || "not_sampled"),
      detail: metric?.detail || "No generator field-duty observation is cached.",
    };
  }
  if (state.stale) {
    return {
      reason: "stale",
      detail: "The last generator field-duty observation exceeded its freshness window.",
    };
  }
  return null;
}

function renderCharging(status, catalog, metrics) {
  const descriptor = CHARGING_METRICS.generatorFieldDuty;
  const definition = findDefinition(catalog, descriptor);
  const metric = definition ? metrics[definition.name] : null;
  const state = observationState(definition, metric);
  const inactive = chargingInactiveState(definition, metric, state);
  const ready = state.heroReady && inactive === null;
  const sourceDefinition = sourceDefinitionFor(definition, metric?.source);
  const cardId = "charging-generator-field-duty-card";
  const valueId = "charging-generator-field-duty";

  byId(cardId).hidden = false;
  if (ready) {
    text(valueId, formatMetricValue(definition.name, metric.value));
    text(`${valueId}-unit`, metric.unit || definition.unit, "");
    setCardState(cardId, state.quality);
  } else {
    text(valueId, "—");
    text(`${valueId}-unit`, "", "");
    setCardState(
      cardId,
      state.stale
        ? "stale"
        : (
          inactive
            ? "unavailable"
            : (state.available ? "unqualified" : "unavailable")
        ),
    );
  }

  text(
    `${valueId}-status`,
    ready
      ? [displayChargingQuality(state.quality), `${formatAge(metric.age_ms)} old`].filter(Boolean).join(" · ")
      : (
        inactive?.reason === "mapping_pending"
          ? "UNMAPPED"
          : humanize(inactive?.reason || "unavailable")
      ),
  );
  text(
    `${valueId}-inactive-reason`,
    ready ? "Live" : humanize(inactive?.reason || "unavailable"),
  );
  text(
    `${valueId}-quality`,
    metric?.quality
      ? displayChargingQuality(normalizedQuality(metric.quality))
      : (
        sourceDefinition?.quality
          ? [displayChargingQuality(normalizedQuality(sourceDefinition.quality)), "REGISTERED"].filter(Boolean).join(" · ")
          : null
      ),
  );
  const qualityNode = byId(`${valueId}-quality`);
  if (qualityNode.parentElement) qualityNode.parentElement.hidden =
    normalizedQuality(metric?.quality || sourceDefinition?.quality) === "verified";
  text(
    `${valueId}-source`,
    metric?.source || (
      sourceDefinition?.name
        ? `${sourceDefinition.name} · registered`
        : null
    ),
  );
  text(
    `${valueId}-acquisition`,
    metric?.acquisition
      ? humanize(metric.acquisition)
      : (
        sourceDefinition?.acquisition_class
          ? `${humanize(sourceDefinition.acquisition_class)} · registered`
          : null
      ),
  );
  text(`${valueId}-interface-mode`, chargingInterfaceMode(status, metric));
  text(
    `${valueId}-detail`,
    inactive?.detail,
    "Fresh broker-cached PCM generator field-command observation.",
  );

  text(
    "charging-state",
    sectionReadingStatus(ready ? 1 : 0, definition ? 1 : 0, 1, 0,
      ready ? displayChargingQuality(state.quality) : ""),
  );
  byId("charging-state").dataset.state = (
    ready && state.quality === "verified"
      ? "verified"
      : "partial"
  );
  return {definition, state, inactive, ready};
}

function renderBattery(metrics) {
  const metric = metrics["battery.voltage"] || {
    metric: "battery.voltage",
    available: false,
    reason: "stale",
    detail: "No cached observation.",
  };
  const definition = lastSnapshot.catalog?.find((item) => item.name === "battery.voltage");
  const state = observationState(definition, metric);
  const live = Boolean(definition) && state.heroReady && Number.isFinite(metric.value) && metric.unit === "V";
  const retained = live ? null : lastRecordedObservation(definition, metric);
  const sample = live ? metric : retained;
  markRetained(byId("quality"), retained);
  const lastError = metric.last_acquisition_error || (metric.available === false ? metric : null);
  const card = document.querySelector(".battery-panel");
  if (sample) {
    text("voltage", Number(sample.value).toFixed(2));
    text(
      "quality",
      retained ? "LAST RECORDED · NOT LIVE" : displayQuality(normalizedQuality(sample.quality)),
      "",
    );
    byId("quality").hidden = !byId("quality").textContent;
    card.dataset.state = retained
      ? "stale"
      : normalizedQuality(sample.quality);
    byId("quality").dataset.state = card.dataset.state;
    text("bus", sample.bus);
    text("source", sample.source);
    text("battery-observed-at", formatTimestamp(sample.observed_at));
    text("age", formatAge(retained ? Math.max(0, Date.now() - Date.parse(sample.observed_at)) : sample.age_ms));
    text("acquisition", humanize(sample.acquisition));
    text(
      "battery-detail",
      lastError
        ? `Cached value retained · last attempt: ${lastError.detail || humanize(lastError.reason)}`
        : retained ? "Last recorded voltage; not a current battery measurement." : "Latest broker-cached observation.",
    );
    const sourceDetail = normalizedQuality(sample.quality) === "verified" ? ""
      : sample.detail || `${displayQuality(normalizedQuality(sample.quality))} registered source.`;
    text("source-detail", sourceDetail, "");
    byId("source-detail").hidden = !sourceDetail;
  } else {
    text("voltage", "—");
    text("quality", String(metric.reason || "unavailable").toUpperCase());
    byId("quality").hidden = false;
    text("battery-detail", metric.detail, "No cached observation.");
    text("bus", metric.bus);
    text("source", null);
    text("battery-observed-at", null);
    text("age", null);
    text("acquisition", metric.acquisition ? humanize(metric.acquisition) : null);
    text("source-detail", "No current provenance is available.");
    byId("source-detail").hidden = false;
    card.dataset.state = "unavailable";
    byId("quality").dataset.state = "unavailable";
  }
}

function buildMetricCard(definition) {
  const article = document.createElement("article");
  article.className = "metric-card";
  const label = document.createElement("p");
  label.className = "eyebrow";
  label.textContent = definition.name;
  const badge = document.createElement("span");
  badge.className = "metric-quality";
  const value = document.createElement("p");
  value.className = "metric-value";
  const meta = document.createElement("p");
  meta.className = "muted";
  article.append(label, badge, value, meta);
  return {article, badge, value, meta};
}

function updateMetricCard(nodes, definition, metric) {
  const state = observationState(definition, metric);
  const fresh = state.available && !state.stale;
  const retained = fresh ? null : lastRecordedObservation(definition, metric);
  const sample = fresh ? metric : retained;
  markRetained(nodes.badge, retained);
  markRetained(nodes.meta, false);
  const cardState = state.stale || retained
    ? "stale"
    : (state.available ? state.quality : "unavailable");
  if (nodes.article.dataset.state !== cardState) {
    nodes.article.dataset.state = cardState;
  }
  const diagnosticOnly = !state.driverQualified;
  if (nodes.article.classList.contains("diagnostic-only") !== diagnosticOnly) {
    nodes.article.classList.toggle("diagnostic-only", diagnosticOnly);
    // Profile summaries count driver-facing cards, so recompute only when a
    // card crosses that visibility boundary.
    lastProfileRenderKey = null;
  }
  elementText(nodes.badge, retained ? "LAST RECORDED · NOT LIVE" : state.stale ? "STALE" : displayQuality(state.quality), "");
  nodes.badge.hidden = !nodes.badge.textContent;
  elementText(
    nodes.value,
    sample
      ? `${formatMetricValue(definition.name, sample.value)} ${sample.unit || definition.unit}`
      : "—",
  );
  elementText(
    nodes.meta,
    retained ? lastRecordedStatus(retained, metric)
      : metricStatus(definition, metric, state),
  );
}

function featuredMetricNames(catalog) {
  const names = new Set(["battery.voltage", "radar.alignment.elevation", "radar.alignment.azimuth", "engine.oil_life_remaining"]);
  [
    ...Object.values(DRIVE_METRICS),
    ...Object.values(ENGINE_HEALTH_METRICS),
    ...Object.values(CHARGING_METRICS),
    ...Object.values(TIRE_METRICS),
  ]
    .forEach((descriptor) => {
      const definition = findDefinition(catalog, descriptor);
      if (
        definition &&
        (
          DRIVER_QUALITIES.has(definitionQuality(definition)) ||
          definition.name === "vehicle.odometer"
        )
      ) {
        names.add(definition.name);
      }
    });
  return names;
}

function renderAdditionalMetrics(catalog, metrics) {
  const grid = byId("metric-grid");
  const featured = featuredMetricNames(catalog);
  const additional = catalog.filter(
    (definition) => !featured.has(definition.name),
  );
  const structureKey = additional.map(
    (definition) => definition.name,
  ).join("\n");
  if (structureKey !== additionalMetricStructureKey) {
    additionalMetricStructureKey = structureKey;
    additionalMetricNodes.clear();
    grid.replaceChildren();
    additional.forEach((definition) => {
      const nodes = buildMetricCard(definition);
      additionalMetricNodes.set(definition.name, nodes);
      grid.append(nodes.article);
    });
    // The visible metric count depends on the newly built card set.
    lastProfileRenderKey = null;
  }
  additional.forEach((definition) => {
    const nodes = additionalMetricNodes.get(definition.name);
    if (nodes) updateMetricCard(nodes, definition, metrics[definition.name]);
  });
}

function renderTire(position, catalog, metrics) {
  const definition = findDefinition(catalog, TIRE_METRICS[position]);
  const metric = definition ? metrics[definition.name] : null;
  const state = observationState(definition, metric);
  const cardId = `tire-${position}-card`;
  const card = byId(cardId);
  card.hidden = false;
  if (!definition) {
    text(`tire-${position}`, "—");
    markRetained(byId(`tire-${position}-status`), false);
    text(`tire-${position}-unit`, "", "");
    setCardState(cardId, "unavailable");
    text(`tire-${position}-status`, metricStatus(definition, metric, state));
    return {
      registered: false,
      live: false,
      quality: state.quality,
    };
  }
  const live = state.heroReady && Number.isFinite(metric?.value) &&
    metric.value >= 0 && metric.value <= 150 && (metric.unit || definition.unit) === "psi";
  const recorded = live ? null : lastRecordedObservation(definition, metric);
  const retained = Boolean(recorded && recorded.value >= 0 && recorded.value <= 150 && recorded.unit === "psi");
  const sample = live ? metric : retained ? recorded : null;
  const readable = Boolean(sample);
  // The panel badge carries the caution color; wheel details stay muted.
  markRetained(byId(`tire-${position}-status`), false);
  if (readable) {
    text(`tire-${position}`, formatMetricValue(definition.name, sample.value));
    text(`tire-${position}-unit`, sample.unit || definition.unit, "");
    setCardState(`tire-${position}-card`, retained ? "stale" : state.quality);
  } else {
    text(`tire-${position}`, "—");
    text(`tire-${position}-unit`, "", "");
    setCardState(
      `tire-${position}-card`,
      state.stale ? "stale" : (state.available ? "unqualified" : "unavailable"),
    );
  }
  text(`tire-${position}-status`, retained
    ? lastRecordedStatus(sample, metric)
    : metricStatus(definition, metric, state));
  return {
    registered: Boolean(definition),
    live,
    retained,
    quality: state.quality,
  };
}

function renderTires(catalog, metrics) {
  const states = Object.keys(TIRE_METRICS)
    .map((position) => renderTire(position, catalog, metrics));
  const registered = states.filter((state) => state.registered).length;
  byId("tire-grid").hidden = false;
  const live = states.filter((state) => state.live);
  const ready = live.length;
  const retained = states.filter((state) => state.retained).length;
  markRetained(byId("tires-state"), retained > 0);
  const liveQualities = new Set(live.map((state) => state.quality));
  const allVerified = (
    ready === 4 &&
    liveQualities.size === 1 &&
    liveQualities.has("verified")
  );
  const qualityLabel = liveQualities.size === 1
    ? displayQuality(live.values().next().value.quality)
    : "MIXED QUALITY";
  text(
    "tires-state",
    sectionReadingStatus(ready, registered, 4, retained, ready === 4 ? qualityLabel : ""),
  );
  byId("tires-state").dataset.state = allVerified
    ? "verified"
    : "partial";
  text(
    "tires-note",
    allVerified
      ? "All four wheel-position samples are fresh."
      : (
        ready === 4
          ? (
            "All four wheel-position samples are fresh. ALFA SCALE means " +
            "observed scaling, not independent verification."
          )
          : (
            retained
              ? ""
              : registered
              ? (
                `${registered}/4 wheel-position sources are mapped; ` +
                "stale, unavailable, or candidate pressures are not shown as live."
              )
              : "Wheel-position pressure mapping is pending."
          )
    ),
    "",
  );
  byId("tires-note").hidden = !byId("tires-note").textContent;
}

function renderInterface(status) {
  const iface = status.interface || {};
  text(
    "inhibits",
    Array.isArray(iface.active_inhibits)
      ? (iface.active_inhibits.join(", ") || "None")
      : "Unknown",
  );
  const roleSnapshot = iface.role_interfaces || {};
  const roles = roleSnapshot.roles && typeof roleSnapshot.roles === "object"
    ? roleSnapshot.roles
    : {};
  const issues = Array.isArray(roleSnapshot.issues)
    ? roleSnapshot.issues.map((issue) => issue.detail || issue.reason || String(issue))
    : [];
  if (roles.spare && roles.spare.safe !== true) {
    issues.push(roles.spare.detail || "Unconnected spare needs attention");
  }
  text("interface-issues", issues.join(" · "));
  byId("interface-issues").hidden = issues.length === 0;
  const vehicleRoles = ["c-can", "b-can", "can-ch"];
  const linksUp = vehicleRoles.filter((role) => (
    roles[role]?.channel && roles[role]?.actual?.present === true &&
    roles[role]?.actual?.up === true
  )).length;
  text("interface-links", `${linksUp}/${vehicleRoles.length}`);
  const roleSignature = JSON.stringify(roles);
  if (roleSignature === lastRoleGridSignature) return;
  lastRoleGridSignature = roleSignature;
  const roleGrid = byId("interface-roles");
  roleGrid.replaceChildren();
  vehicleRoles.forEach((role) => {
    const payload = roles[role] || {};
    const expected = payload?.expected || {};
    const actual = payload?.actual || {};
    const card = document.createElement("article");
    card.className = "role-card";
    card.dataset.state = payload.safe === true && actual.present === true &&
      actual.fd_enabled === false && actual.up === true &&
      actual.controller_state === "ERROR-ACTIVE" ? "ready" : "unavailable";
    const heading = document.createElement("h3");
    heading.textContent = role === "can-ch" ? "CAN CH" : role.toUpperCase();
    const link = document.createElement("p");
    link.textContent = `${expected.pair ? `Pins ${expected.pair} · ` : ""}` +
      (actual.bitrate == null ? "Bitrate unknown" : `${actual.bitrate / 1000} kbit/s`);
    const operating = document.createElement("p");
    const linkState = actual.present === false ? "Absent"
      : actual.up === true ? "Up" : actual.up === false ? "Down" : "Link unknown";
    const mode = actual.listen_only === true ? "Listen-only"
      : actual.listen_only === false
        ? (payload.operating_mode === "armed_diagnostic" || actual.mode === "armed_diagnostic"
          ? "Diagnostics enabled" : "Transmit enabled")
        : "Mode unknown";
    operating.textContent = `${linkState} · ${mode} · ${actual.controller_state || "Controller unknown"}`;
    operating.title = "ERROR-ACTIVE is the normal CAN controller state, not an active fault.";
    card.append(heading, link, operating);
    if (payload.safe !== true || actual.fd_enabled !== false) {
      const issue = document.createElement("p");
      issue.textContent = [
        actual.fd_enabled === true ? "Unexpected CAN FD" : null,
        payload.detail || humanize(payload.reason) || "Role status unavailable",
      ].filter(Boolean).join(" · ");
      card.append(issue);
    }
    roleGrid.append(card);
  });
  roleGrid.hidden = roleGrid.children.length === 0;
  const identities = byId("interface-identities");
  identities.replaceChildren();
  Object.entries(roles).forEach(([role, payload]) => {
    const expected = payload?.expected || {};
    const identity = document.createElement("p");
    identity.textContent = `${role} → ${payload?.channel || "unresolved"} · ` +
      (expected.usb_serial
        ? `board ${expected.board} ${expected.connector} · serial …${String(expected.usb_serial).slice(-6)} · dev ${expected.dev_id}`
        : "Identity unavailable");
    if (expected.passive_required === false) {
      identity.textContent += ` · Unconnected spare · ${payload?.actual?.up === false ? "Down" : humanize(payload?.reason) || "State unknown"}`;
    }
    identities.append(identity);
  });
}

function renderCollector(status) {
  const collector = status.collector || {};
  text("collector-state", humanize(collector.state));
  text("collector-cycles", collector.cycles);
  text("collector-last", collector.last_cycle_at);
  text("collector-retention", status.last_readings?.storage_error || (
    status.last_readings ? status.last_readings.persistent ? "Persistent" : "Memory only" : "Not reported"
  ));
  text(
    "collector-interval",
    collector.interval_seconds == null
      ? null
      : `${collector.interval_seconds} s`,
  );
}

function formatTimestamp(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return String(value);
  return parsed.toLocaleString();
}

function numeric(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function renderSparkline(container, points) {
  container.replaceChildren();
  const values = (Array.isArray(points) ? points : [])
    .map((point) => numeric(typeof point === "object" ? point?.value : point))
    .filter((value) => value != null)
    .slice(-48);
  if (values.length < 2) return;
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const span = maximum - minimum;
  values.forEach((value) => {
    const bar = document.createElement("span");
    const normalized = span > 0 ? (value - minimum) / span : .5;
    bar.style.height = `${Math.round(20 + normalized * 80)}%`;
    bar.title = String(value);
    container.append(bar);
  });
}

function renderHistory(history) {
  const summary = history && typeof history === "object" ? history : {};
  const coverage = summary.coverage || {};
  const currentTrip = summary.current_trip || null;
  const recentTrips = Array.isArray(summary.recent_trips)
    ? summary.recent_trips
    : [];
  const trends = summary.metric_trends && typeof summary.metric_trends === "object"
    ? summary.metric_trends
    : {};
  const available = summary.available !== false && (
    Boolean(coverage.last_snapshot_at) ||
    Boolean(currentTrip) ||
    recentTrips.length > 0 ||
    Object.keys(trends).length > 0
  );
  text(
    "history-state",
    available ? humanize(coverage.status || "recording") : "NO HISTORY",
  );
  text(
    "history-current-trip",
    currentTrip
      ? `${humanize(currentTrip.state || "active")} · ${formatAge(
        numeric(currentTrip.duration_seconds) == null
          ? null
          : currentTrip.duration_seconds * 1000,
      )}`
      : "None active",
  );
  const metricGaps = Array.isArray(coverage.active_metric_gaps)
    ? coverage.active_metric_gaps.length
    : 0;
  const interfaceGaps = Array.isArray(coverage.active_interface_gaps)
    ? coverage.active_interface_gaps.length
    : 0;
  const retention = coverage.retention && typeof coverage.retention === "object"
    ? coverage.retention
    : {};
  const maintenanceHook = summary.maintenance_hook &&
    typeof summary.maintenance_hook === "object"
    ? summary.maintenance_hook
    : {};
  const retentionIssue = ["partial", "blocked_rollup_backlog"].includes(
    retention.last_status,
  )
    ? `retention ${humanize(retention.last_status)}`
    : (maintenanceHook.last_error ? "retention check failed" : null);
  text(
    "history-coverage",
    available
      ? (metricGaps || interfaceGaps
        ? `${metricGaps} metric · ${interfaceGaps} interface gap(s)` +
          (retentionIssue ? ` · ${retentionIssue}` : "")
        : (retentionIssue || "No active gaps"))
      : "Not recording",
  );
  text("history-trip-count", recentTrips.length);
  text("history-last-sample", formatTimestamp(coverage.last_snapshot_at));

  const grid = byId("history-trends");
  grid.replaceChildren();
  Object.entries(trends).slice(0, 12).forEach(([metricName, trend]) => {
    const current = trend?.current_trip || {};
    const seven = trend?.days_7 || {};
    const thirty = trend?.days_30 || {};
    const article = document.createElement("article");
    article.className = "trend-card";
    const heading = document.createElement("h3");
    heading.textContent = metricName;
    const regime = document.createElement("p");
    regime.textContent = trend?.prior_trips
      ? `${trend.prior_trips.trip_count} comparable prior trip(s)`
      : "Comparable prior-trip baseline pending";
    const stats = document.createElement("dl");
    stats.className = "trend-stats";
    [
      ["Current", current.mean],
      ["7 day", seven?.mean],
      ["30 day", thirty?.mean],
    ].forEach(([label, value]) => {
      const wrapper = document.createElement("div");
      const term = document.createElement("dt");
      const detail = document.createElement("dd");
      term.textContent = label;
      detail.textContent = numeric(value) == null
        ? "—"
        : `${formatMetricValue(metricName, value)} ${trend?.unit || ""}`.trim();
      wrapper.append(term, detail);
      stats.append(wrapper);
    });
    const sparkline = document.createElement("div");
    sparkline.className = "sparkline";
    sparkline.setAttribute("aria-label", `${metricName} bounded trend`);
    renderSparkline(
      sparkline,
      trend?.sparkline || trend?.points || [],
    );
    article.append(heading, regime, stats, sparkline);
    grid.append(article);
  });
  text(
    "history-note",
    summary.detail || (
      available
        ? "Current-trip comparisons and bounded 7-day/30-day aggregates preserve missing telemetry as explicit coverage gaps."
        : "The historian has not recorded a usable telemetry sample yet."
    ),
  );
}

const WARNING_CARD_STATES = new Set(["watch", "warning"]);

function selectWarningCards(active, assessments) {
  if (active.length) return active;
  return assessments.filter(
    (assessment) => WARNING_CARD_STATES.has(assessment?.state),
  ).slice(0, 8);
}

function renderEarlyWarnings(health) {
  const summary = health && typeof health === "object" ? health : {};
  const qualitySummary = summary.data_quality &&
    typeof summary.data_quality === "object"
    ? summary.data_quality
    : {};
  const activeQuality = Array.isArray(qualitySummary.active)
    ? qualitySummary.active
    : [];
  const recentQuality = Array.isArray(qualitySummary.recent)
    ? qualitySummary.recent
    : [];
  const persistedEpisodes = Array.isArray(summary.episodes?.active)
    ? summary.episodes.active
    : [];
  const active = persistedEpisodes.length
    ? persistedEpisodes.map((episode) => ({
      ...(episode.latest_assessment || {}),
      episode_id: episode.id,
      episode_opened_at: episode.opened_at,
      acknowledged: episode.acknowledged,
      evidence_state: episode.evidence_state,
    }))
    : (Array.isArray(summary.active) ? summary.active : []);
  const assessments = Array.isArray(summary.assessments)
    ? summary.assessments
    : [];
  const unavailable = summary.available === false;
  const training = assessments.some(
    (assessment) => assessment?.state === "insufficient_history",
  );
  const dataUnavailable = assessments.some(
    (assessment) => assessment?.state === "unavailable",
  );
  const allNormal = assessments.length > 0 && assessments.every(
    (assessment) => assessment?.state === "normal",
  );
  const delivery = summary.notification_delivery &&
    typeof summary.notification_delivery === "object"
    ? summary.notification_delivery
    : {};
  const outbox = summary.episodes?.notification_outbox &&
    typeof summary.episodes.notification_outbox === "object"
    ? summary.episodes.notification_outbox
    : {};
  const pendingDelivery = numeric(outbox.pending) || 0;
  const failedDelivery = numeric(outbox.failed) || 0;
  const deliveryError = delivery.enabled === true && delivery.last_error
    ? String(delivery.last_error)
    : null;
  const advisoryBadge = unavailable
    ? "UNAVAILABLE"
    : (active.length
      ? `${active.length} TO REVIEW` +
        (activeQuality.length ? ` · ${activeQuality.length} SAMPLE FILTER` : "")
      : (activeQuality.length
        ? `${activeQuality.length} SAMPLE FILTER ACTIVE`
      : (training
        ? "TRAINING"
        : (allNormal
          ? "NO PERSISTENT CHANGES"
          : (dataUnavailable ? "DATA UNAVAILABLE" : "NO ASSESSMENTS")))));
  text(
    "warning-state",
    failedDelivery || deliveryError
      ? `DELIVERY ERROR · ${advisoryBadge}`
      : (pendingDelivery
        ? `${pendingDelivery} DELIVERY PENDING · ${advisoryBadge}`
        : advisoryBadge),
  );
  const list = byId("warning-list");
  list.replaceChildren();
  const shown = selectWarningCards(active, assessments);
  shown.forEach((assessment) => {
    const article = document.createElement("article");
    article.className = "summary-card";
    article.dataset.state = assessment.state || "unavailable";
    const unresolved = assessment.episode_id != null;
    article.dataset.unresolved = String(unresolved);
    if (unresolved) {
      const label = document.createElement("span");
      label.className = "badge advisory-label";
      label.textContent = "Unresolved advisory";
      article.append(label);
    }
    const heading = document.createElement("h3");
    heading.textContent = assessment.title || assessment.label ||
      assessment.metric || "Telemetry change";
    const reason = document.createElement("p");
    const reasons = Array.isArray(assessment.reasons)
      ? assessment.reasons.join(" ")
      : assessment.reason || assessment.detail;
    reason.textContent = reasons || humanize(assessment.state || "unavailable");
    const evidence = document.createElement("p");
    const baseline = assessment.baseline || {};
    const deviation = numeric(
      assessment.signed_deviation ?? assessment.deviation?.signed_from_median,
    );
    evidence.textContent = [
      assessment.episode_id == null ? null : `Episode ${assessment.episode_id}`,
      assessment.episode_opened_at ? `open since ${formatTimestamp(assessment.episode_opened_at)}` : null,
      assessment.acknowledged ? "acknowledged" : null,
      assessment.regime ? `Regime ${humanize(assessment.regime)}` : null,
      numeric(baseline.median) == null ? null : `baseline median ${baseline.median}`,
      numeric(baseline.mad) == null ? null : `MAD ${baseline.mad}`,
      deviation == null ? null : `deviation ${deviation.toFixed(2)}`,
      assessment.custom_rule ? `Owner reference: ${humanize(assessment.custom_rule.operator)} ${assessment.custom_rule.threshold} ${assessment.current?.unit || ""}` : null,
      assessment.persistence?.observed == null
        ? null
        : `${assessment.persistence.observed}/${assessment.persistence.required} persistent observations`,
    ].filter(Boolean).join(" · ");
    article.append(heading, reason);
    if (evidence.textContent) article.append(evidence);
    window.WarningChat?.attach(article, assessment.episode_id != null
      ? {kind: "episode", id: String(assessment.episode_id)}
      : {kind: "assessment", id: assessment.rule}, heading.textContent);
    list.append(article);
  });
  const recovered = recentQuality.filter((event) => event?.status === "resolved").slice(0, 3);
  const recoveredList = byId("warning-recovered-list");
  recoveredList.replaceChildren();
  byId("warning-recovered").hidden = recovered.length === 0;
  text("warning-recovered-title", `Recovered Events · ${recovered.length}`);
  const qualityShown = [...activeQuality, ...recovered];
  qualityShown.forEach((event) => {
    const article = document.createElement("article");
    article.className = "summary-card";
    article.dataset.state = event?.status === "active" ? "watch" : "normal";
    const heading = document.createElement("h3");
    heading.textContent = event?.status === "active"
      ? "Telemetry sample filter active"
      : "Telemetry sample filter recovered";
    const reason = document.createElement("p");
    reason.textContent = event?.detail || (
      "A raw transmission-temperature sample failed its OEM-context plausibility gate."
    );
    const evidence = document.createElement("p");
    evidence.textContent = [
      event?.metric || null,
      event?.rejection_count == null
        ? null
        : `${event.rejection_count} raw rejection(s)`,
      event?.last_seen_at ? `last seen ${formatTimestamp(event.last_seen_at)}` : null,
      event?.status === "resolved" && event?.resolved_at
        ? `recovered ${formatTimestamp(event.resolved_at)}`
        : null,
      "last good value retained",
      "data quality only — never notified",
    ].filter(Boolean).join(" · ");
    article.append(heading, reason, evidence);
    window.WarningChat?.attach(article, {kind: "quality", id: event?.incident_id}, heading.textContent);
    (event?.status === "resolved" ? recoveredList : list).append(article);
  });
  if (!shown.length && !activeQuality.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = unavailable
      ? summary.detail || "Early-warning history is unavailable."
      : (summary.custom_rules?.count ? "No persistent change is currently active." : "No persistent regime-matched change is currently active.");
    list.append(empty);
  }
  text(
    "warning-note",
    [
      summary.custom_rules?.count ? "Warnings use learned baselines or owner-approved thresholds with persistence; neither is a mechanical diagnosis." : summary.detail || (
        "Warnings require persistent, like-for-like deviations and show their baseline evidence. They are not a diagnosis or an opaque health score."
      ),
      summary.custom_rules?.error || "",
      delivery.enabled
        ? (
          `Durable warning delivery is enabled through the queued ntfy sink. ` +
          `Persisted outbox: ${pendingDelivery} pending · ${failedDelivery} failed.` +
          (deliveryError ? ` Latest delivery error: ${deliveryError}.` : "")
        )
        : "External warning delivery is disabled.",
      qualitySummary.detail || (
        "Raw plausibility rejections are recorded separately as data quality and never generate notifications."
      ),
    ].join(" "),
  );
}

function dtcGroupTitle(key) {
  return {
    current: "Current / test-failed when last observed",
    pending: "Pending",
    confirmed_history: "Confirmed history",
    incomplete_only: "Test not completed only",
    other: "Other status combinations",
  }[key] || humanize(key);
}

function dtcObservationLabel(entry) {
  return {
    observed_in_latest_success: "observed in latest successful module result",
    stale_after_unavailable_attempt: (
      "stale saved state: latest module attempt unavailable"
    ),
    retained_incompatible_status_mask: (
      "retained saved state: latest successful status-mask coverage was incompatible"
    ),
  }[entry?.observation_state] || "saved observation state unavailable";
}

function dtcModuleGapDetail(module) {
  if (module?.availability === "never_scanned") {
    return "Never scanned by the repository reader";
  }
  if (module?.availability === "unavailable") {
    return [
      `Latest module attempt unavailable: ${humanize(module.unavailable_reason)}`,
      module.last_success_at
        ? `last successful evidence ${formatTimestamp(module.last_success_at)}`
        : "no successful saved result",
    ].join(" · ");
  }
  if (
    module?.result_state === "status_coverage_incomplete" ||
    (
      module?.last_success_dtc_count === 0 &&
      module?.absence_authoritative !== true
    )
  ) {
    return (
      "Status coverage incomplete: the saved zero-record response does not " +
      "establish DTC absence"
    );
  }
  return null;
}

function dtcHistoryCutoff(now = Date.now()) {
  const cutoff = new Date(now);
  const day = cutoff.getUTCDate();
  cutoff.setUTCMonth(cutoff.getUTCMonth() - 1, 1);
  const lastDay = new Date(Date.UTC(
    cutoff.getUTCFullYear(), cutoff.getUTCMonth() + 1, 0,
  )).getUTCDate();
  cutoff.setUTCDate(Math.min(day, lastDay));
  return cutoff.getTime();
}

function renderDtcs(dtcs) {
  const summary = dtcs && typeof dtcs === "object" ? dtcs : {};
  const groups = summary.groups && typeof summary.groups === "object"
    ? summary.groups
    : {};
  const modules = summary.modules && typeof summary.modules === "object"
    ? Object.values(summary.modules)
    : (Array.isArray(summary.modules) ? summary.modules : []);
  const coverage = summary.coverage && typeof summary.coverage === "object"
    ? summary.coverage
    : {};
  const groupCounts = summary.group_counts &&
    typeof summary.group_counts === "object"
    ? summary.group_counts
    : {};
  const groupReturnedCounts = summary.group_returned_counts &&
    typeof summary.group_returned_counts === "object"
    ? summary.group_returned_counts
    : {};
  const groupsTruncated = summary.groups_truncated === true;
  const records = Object.keys(groupCounts).length
    ? Object.values(groupCounts).reduce(
      (count, value) => count + (numeric(value) || 0),
      0,
    )
    : Object.values(groups).reduce(
      (count, entries) => count + (Array.isArray(entries) ? entries.length : 0),
      0,
    );
  const returnedRecords = Object.keys(groupReturnedCounts).length
    ? Object.values(groupReturnedCounts).reduce(
      (count, value) => count + (numeric(value) || 0),
      0,
    )
    : Object.values(groups).reduce(
      (count, entries) => count + (Array.isArray(entries) ? entries.length : 0),
      0,
    );
  const newestEvidence = coverage.last_attempt_at || coverage.last_success_at;
  const available = numeric(coverage.available_modules) ?? modules.filter(
    (module) => module?.availability === "available",
  ).length;
  const unavailable = numeric(coverage.unavailable_modules) ?? modules.filter(
    (module) => module?.availability === "unavailable",
  ).length;
  const neverScanned = numeric(coverage.never_scanned_modules) ?? modules.filter(
    (module) => module?.availability === "never_scanned",
  ).length;
  const statusCoverageIncomplete = numeric(
    coverage.modules_status_coverage_incomplete,
  ) ?? modules.filter(
    (module) => module?.result_state === "status_coverage_incomplete",
  ).length;
  const authoritativeZero = modules.filter(
    (module) => (
      module?.result_state === "no_dtcs" &&
      module?.absence_authoritative === true
    ),
  ).length;
  const hasCoverageGaps = (
    unavailable > 0 || neverScanned > 0 || statusCoverageIncomplete > 0
  );
  const allAuthoritativeZero = (
    modules.length > 0 &&
    authoritativeZero === modules.length &&
    records === 0 &&
    !hasCoverageGaps
  );
  text(
    "dtc-state",
    summary.available === false
      ? "UNAVAILABLE"
      : (!newestEvidence
        ? "NEVER SCANNED"
        : (records
          ? `${records} SAVED STATUS RECORD${records === 1 ? "" : "S"}` +
            (hasCoverageGaps ? " · COVERAGE GAPS" : "")
          : (allAuthoritativeZero
            ? "NO DTCs IN AUTHORITATIVE RESULTS"
            : "COVERAGE INCOMPLETE"))),
  );
  text(
    "dtc-last-scan",
    formatTimestamp(newestEvidence),
  );
  text(
    "dtc-module-coverage",
    coverage.total_modules != null
      ? `${available}/${coverage.total_modules} available saved module results` +
        `${unavailable ? ` · ${unavailable} unavailable` : ""}` +
        `${neverScanned ? ` · ${neverScanned} never scanned` : ""}` +
        `${statusCoverageIncomplete ? ` · ${statusCoverageIncomplete} status coverage incomplete` : ""}` +
        `${authoritativeZero ? ` · ${authoritativeZero} authoritative zero-DTC` : ""}`
      : "No validated module coverage",
  );
  const root = byId("dtc-groups");
  const olderHistoryOpen = byId("dtc-older-history")?.open === true;
  const historyCutoff = dtcHistoryCutoff();
  root.replaceChildren();
  const coverageGaps = modules.filter((module) => dtcModuleGapDetail(module));
  if (coverageGaps.length) {
    const section = document.createElement("details");
    section.className = "dtc-group";
    const heading = document.createElement("summary");
    heading.textContent = `Module coverage gaps · ${coverageGaps.length}`;
    const list = document.createElement("ul");
    list.className = "dtc-list";
    coverageGaps.forEach((module) => {
      const item = document.createElement("li");
      const name = document.createElement("span");
      name.className = "dtc-code";
      name.textContent = module.module_name || module.module_key || "Unknown module";
      const detail = document.createElement("span");
      detail.className = "dtc-detail";
      detail.textContent = [
        module.logical_bus,
        dtcModuleGapDetail(module),
      ].filter(Boolean).join(" · ");
      item.append(name, detail);
      list.append(item);
    });
    section.append(heading, list);
    root.append(section);
  }
  const order = [
    "current",
    "pending",
    "confirmed_history",
    "incomplete_only",
    "other",
  ];
  let renderedRecordGroups = 0;
  order.forEach((key) => {
    const entries = Array.isArray(groups[key]) ? groups[key] : [];
    if (!entries.length) return;
    renderedRecordGroups += 1;
    const section = document.createElement(
      key === "incomplete_only" ? "details" : "section",
    );
    section.className = "dtc-group";
    const heading = document.createElement(
      key === "incomplete_only" ? "summary" : "h3",
    );
    const total = numeric(groupCounts[key]) ?? entries.length;
    const returned = numeric(groupReturnedCounts[key]) ?? entries.length;
    heading.textContent = `${dtcGroupTitle(key)} · ${total}` +
      (
        groupsTruncated && returned < total
          ? ` · showing ${returned} of ${total}`
          : ""
      );
    const list = document.createElement("ul");
    list.className = "dtc-list";
    const olderList = document.createElement("ul");
    olderList.className = "dtc-list";
    entries.slice(0, returned).forEach((entry) => {
      const item = document.createElement("li");
      const code = document.createElement("span");
      code.className = "dtc-code";
      code.textContent = (
        entry.fca_display || entry.display_code || entry.code || entry.dtc ||
        entry.raw_dtc || entry.raw_code ||
        entry.module_key || entry.module || "—"
      );
      const body = document.createElement("span");
      body.className = "dtc-body";
      const meaningText = entry.label || entry.description;
      if (meaningText) {
        const meaning = document.createElement("span");
        meaning.className = entry.description_reviewed === false
          ? "dtc-meaning dtc-meaning-unreviewed"
          : "dtc-meaning";
        meaning.textContent = meaningText;
        body.append(meaning);
      }
      const detail = document.createElement("span");
      detail.className = "dtc-detail";
      detail.textContent = [
        entry.module_name || entry.module_key || entry.module,
        entry.logical_bus || entry.bus,
        Array.isArray(entry.status_flags)
          ? entry.status_flags.join(", ")
          : entry.status_text || entry.status,
        entry.current ? "current / test-failed when this DTC was last observed" : null,
        dtcObservationLabel(entry),
        entry.last_seen_at ? `last ${formatTimestamp(entry.last_seen_at)}` : null,
      ].filter(Boolean).join(" · ");
      body.append(detail);
      item.append(code, body);
      const lastSeen = Date.parse(entry.last_seen_at);
      if (key === "confirmed_history" && Number.isFinite(lastSeen) && lastSeen < historyCutoff) {
        olderList.append(item);
      } else {
        list.append(item);
      }
    });
    section.append(heading);
    if (list.children.length) section.append(list);
    if (olderList.children.length) {
      const older = document.createElement("details");
      older.id = "dtc-older-history";
      older.className = "panel-note";
      older.open = olderHistoryOpen;
      const olderHeading = document.createElement("summary");
      olderHeading.textContent = `Older Than 1 Month · ${olderList.children.length}`;
      older.append(olderHeading, olderList);
      section.append(older);
    }
    root.append(section);
  });
  if (!renderedRecordGroups) {
    const empty = document.createElement("p");
    empty.className = "muted";
    if (summary.available === false) {
      empty.textContent = summary.detail || "The saved DTC cache is unavailable.";
    } else if (!newestEvidence) {
      empty.textContent = (
        "No saved DTC inventory is available. Run the parked local scanner; " +
        (
          dtcJobsEnabled
            ? "the guarded controls below can queue it after parked confirmation."
            : "this cache-only listener cannot start it."
        )
      );
    } else if (allAuthoritativeZero) {
      empty.textContent = (
        "No DTCs were returned in the saved authoritative module results."
      );
    } else {
      empty.textContent = (
        "No reportable saved DTC records. DTC absence is not established " +
        "because module or status-mask coverage is incomplete."
      );
    }
    root.append(empty);
  }
  const noteParts = [
    summary.detail || (
      dtcJobsEnabled
        ? "Cached ReadDTCInformation; guarded parked scan queue enabled. DTC clearing is unavailable."
        : "Cached ReadDTCInformation only; this listener cannot scan or clear DTCs."
    ),
    (
      "Current and pending describe each module's dated successful " +
      "observation, not live vehicle state."
    ),
    groupsTruncated
      ? `Compact cache: showing ${returnedRecords} of ${records} saved records.`
      : null,
    summary.description_catalog?.detail,
    summary.description_catalog?.returned_records != null
      ? (
        `${summary.description_catalog.reviewed_records || 0}/` +
        `${summary.description_catalog.returned_records} displayed records have ` +
        "a reviewed module-specific meaning; other rows show only the " +
        "standardized failure subtype."
      )
      : null,
  ];
  text(
    "dtc-note",
    noteParts.filter(Boolean).join(" "),
  );
}

function dtcJobIsActive(state) {
  return ["queued", "starting", "created", "running"].includes(state);
}

function updateDtcJobButtons() {
  const confirmed = byId("dtc-park-confirm").checked;
  byId("dtc-scan-start").disabled = !(
    dtcJobsEnabled && confirmed && !dtcLegacyTokenRequired &&
    !dtcJobIsActive(dtcLastJobState) && dtcLastJobState !== "restoration_failed"
  );
  byId("dtc-scan-cancel").disabled = !(
    dtcJobsEnabled && dtcJobIsActive(dtcLastJobState) && !dtcCancelRequested
  );
  byId("dtc-scan-start").title = dtcLegacyTokenRequired
    ? "Restart the updated Tailscale web server to enable scanning without a local token" : "";
}

function renderDtcJob(payload) {
  const job = payload?.job || {};
  const state = payload?.state || job.state || "idle";
  dtcLastJobState = state;
  dtcCancelRequested = job.cancel_requested === true;
  const progress = job.progress || {};
  const parts = [humanize(state)];
  if (dtcLegacyTokenRequired) parts.push("Web server update needed for token-free scanning");
  if (progress.requestable != null) {
    parts.push(
      `${progress.queried || 0}/${progress.requestable} queried`,
      `${progress.imported || 0} imported`,
    );
  }
  if (job.current_bus || job.current_module) {
    parts.push([job.current_bus, job.current_module].filter(Boolean).join(" / "));
  }
  if (job.cancel_requested) parts.push("cancellation requested");
  if (job.restoration_failure) parts.push("RESTORATION UNVERIFIED — inspect before retry");
  if (job.failure) parts.push(job.failure);
  text("dtc-job-status", parts.join(" · "));
  updateDtcJobButtons();
  if (dtcJobIsActive(state)) {
    if (dtcJobPollTimer == null) {
      dtcJobPollTimer = window.setTimeout(fetchDtcJobStatus, 2000);
    }
  } else {
    if (dtcJobPollTimer != null) window.clearTimeout(dtcJobPollTimer);
    dtcJobPollTimer = null;
    if (state === "completed") {
      supplementalRequestStartedEpochMs = null;
      void fetchSupplemental();
    }
  }
}

async function fetchDtcJobStatus() {
  if (dtcJobPollTimer != null) window.clearTimeout(dtcJobPollTimer);
  dtcJobPollTimer = null;
  if (!dtcJobsEnabled || document.visibilityState === "hidden") return;
  try {
    const response = await fetch(
      `/v1/diagnostics/dtc-jobs/current?fresh=${Date.now()}`,
      {cache: "no-store"},
    );
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    renderDtcJob(payload);
  } catch (error) {
    text("dtc-job-status", `DTC job status unavailable: ${error}`);
    if (dtcJobsEnabled) {
      dtcJobPollTimer = window.setTimeout(fetchDtcJobStatus, 5000);
    }
  }
}

function configureDtcJobs(web) {
  const enabled = web?.dtc_jobs_enabled === true;
  const legacy = web?.dtc_jobs_require_local_one_use_arm === true;
  if (legacy !== dtcLegacyTokenRequired) {
    dtcLegacyTokenRequired = legacy;
    updateDtcJobButtons();
  }
  if (enabled === dtcJobsEnabled) {
    if (enabled && dtcJobIsActive(dtcLastJobState) && dtcJobPollTimer == null) {
      void fetchDtcJobStatus();
    }
    return;
  }
  dtcJobsEnabled = enabled;
  text(
    "dtc-eyebrow",
    enabled
      ? "DIAGNOSTICS · GUARDED PARKED SCAN"
      : "DIAGNOSTICS · CACHED ONLY",
  );
  byId("dtc-scan-controls").hidden = !enabled;
  if (!enabled) {
    dtcLastJobState = null;
    dtcCancelRequested = false;
    if (dtcJobPollTimer != null) window.clearTimeout(dtcJobPollTimer);
    dtcJobPollTimer = null;
  } else {
    void fetchDtcJobStatus();
  }
  updateDtcJobButtons();
}

function renderCatalog(catalog) {
  const catalogSignature = JSON.stringify(catalog);
  if (catalogSignature === lastCatalogSignature) return;
  lastCatalogSignature = catalogSignature;
  const list = byId("catalog-list");
  list.replaceChildren();
  catalog.forEach((definition) => {
    const item = document.createElement("article");
    item.className = "catalog-item";
    const heading = document.createElement("h3");
    heading.textContent = definition.name;
    const meta = document.createElement("p");
    meta.className = "muted";
    meta.textContent = (
      `${definition.value_type} · ${definition.unit || "unitless"} · ` +
      `stale after ${definition.stale_after_seconds} s · ` +
      (RETAIN_LAST_READING.has(definition.name) ? "dated last reading retained" : "live-only display")
    );
    item.append(heading, meta);
    (definition.sources || []).forEach((source) => {
      const sourceLine = document.createElement("p");
      sourceLine.textContent = [source.name, source.bus,
        normalizedQuality(source.quality) === "verified" ? null : source.quality,
      ].filter(Boolean).join(" · ");
      item.append(sourceLine);
    });
    list.append(item);
  });
}

function configureWarningChat(web) {
  window.WarningChat?.configure(web);
  if (web.warning_chat_enabled === true && !window.WarningChat && !warningChatLoading) {
    warningChatLoading = true;
    const script = document.createElement("script");
    script.src = "/warning-chat.js";
    script.addEventListener("load", () => {
      window.WarningChat?.configure(lastSnapshot.web || {});
      renderEarlyWarnings(supplemental.earlyWarnings);
    });
    document.head.append(script);
  }
}

function render(snapshot) {
  if (!snapshot || typeof snapshot !== "object") return;
  const status = snapshot.status || {};
  const web = snapshot.web || status.web || {};
  configureWarningChat(web);
  const metrics = snapshot.metrics || {};
  const catalog = Array.isArray(snapshot.catalog) ? snapshot.catalog : [];
  lastSnapshot = {
    status,
    web,
    metrics,
    catalog,
  };
  if (Object.hasOwn(web, "active_acquisition_enabled")) {
    acquisitionEnabled = Boolean(web.active_acquisition_enabled);
  }
  configureDtcJobs(web);
  byId("acquire").disabled = !acquisitionEnabled;
  text(
    "control-note",
    acquisitionEnabled
      ? ""
      : "Web acquisition is disabled; this page only reads broker cache.",
    "",
  );
  byId("control-note").hidden = acquisitionEnabled;
  renderVehicleState(status);
  renderRadarAlignment(status, catalog, metrics);
  renderServiceMileage(metrics);
  renderOilLife(catalog, metrics);
  renderDrive(status, catalog, metrics);
  renderEngineHealth(catalog, metrics);
  renderCharging(status, catalog, metrics);
  renderBattery(metrics);
  renderTires(catalog, metrics);
  renderAdditionalMetrics(catalog, metrics);
  renderInterface(status);
  renderCollector(status);
  renderCatalog(catalog);
  const collector = status.collector || {};
  text(
    "service-state",
    `Broker ${status.service ? "online" : "unknown"} · collector ` +
      `${collector.state || "unknown"} · ${catalog.length} metric` +
      `${catalog.length === 1 ? "" : "s"} registered`,
  );
  setProfile();
}

function renderTimeSensitiveSnapshot() {
  const status = lastSnapshot.status || {};
  const catalog = Array.isArray(lastSnapshot.catalog)
    ? lastSnapshot.catalog
    : [];
  const metrics = lastSnapshot.metrics || {};
  renderVehicleState(status);
  renderRadarAlignment(status, catalog, metrics);
  renderServiceMileage(metrics);
  renderOilLife(catalog, metrics);
  renderDrive(status, catalog, metrics);
  renderEngineHealth(catalog, metrics);
  renderCharging(status, catalog, metrics);
  renderBattery(metrics);
  renderTires(catalog, metrics);
  renderAdditionalMetrics(catalog, metrics);
  setProfile();
}

function advanceDisplayedAges() {
  if (ageCursorMonotonicMs == null) return;
  const nowMonotonicMs = performance.now();
  const addedAgeMs = nowMonotonicMs - ageCursorMonotonicMs;
  if (!Number.isFinite(addedAgeMs) || addedAgeMs <= 0) return;
  ageSnapshot(lastSnapshot, addedAgeMs);
  ageCursorMonotonicMs = nowMonotonicMs;
  renderTimeSensitiveSnapshot();
}

function invalidateDisplayedFreshness(reason) {
  const invalidateMetrics = (metrics) => {
    Object.values(metrics || {}).forEach((metric) => {
      if (metric?.available) metric.stale = true;
    });
  };
  invalidateMetrics(lastSnapshot.metrics);
  invalidateMetrics(lastSnapshot.status?.cached_metrics);
  const vehicle = lastSnapshot.status?.vehicle_state;
  if (vehicle) {
    vehicle.state = "unknown";
    vehicle.running = null;
    vehicle.confidence = "stale";
    vehicle.basis = reason;
    vehicle.detail = (
      "Cached vehicle state was invalidated across a browser page-lifecycle " +
      "boundary; a new HTTP snapshot is required."
    );
  }
  ageCursorMonotonicMs = performance.now();
  if (document.visibilityState !== "hidden") {
    renderTimeSensitiveSnapshot();
  }
}

function freshnessTick() {
  const nowMonotonicMs = performance.now();
  if (
    lastAcceptedMonotonicMs == null ||
    nowMonotonicMs - lastAcceptedMonotonicMs >= FRESHNESS_TICK_MS
  ) {
    // A healthy stream already ages and renders the accepted snapshot. The
    // watchdog renders only when delivery has not advanced since the last
    // one-second window, preserving stale-state expiry without a duplicate
    // full pass on every normal SSE cycle.
    advanceDisplayedAges();
  }
  if (
    document.visibilityState !== "hidden" &&
    streamAccepting &&
    eventStream &&
    lastAcceptedMonotonicMs != null &&
    nowMonotonicMs - lastAcceptedMonotonicMs > STREAM_STALL_RESYNC_MS
  ) {
    resyncSnapshot("stream_stall");
  }
}

function fetchSupplemental() {
  if (supplementalRequestInFlight) return supplementalRequestInFlight;
  const startedAt = Date.now();
  if (
    supplementalRequestStartedEpochMs != null &&
    startedAt - supplementalRequestStartedEpochMs < SUPPLEMENTAL_REFRESH_MS
  ) {
    return Promise.resolve(false);
  }
  supplementalRequestStartedEpochMs = startedAt;
  const sequence = ++supplementalRequestSequence;
  const requests = [
    ["history", "/v1/history"],
    ["earlyWarnings", "/v1/health"],
    ["dtcs", "/v1/diagnostics/dtcs"],
    ["maintenance", "/v1/maintenance"],
  ];
  const operation = (async () => {
    const results = await Promise.all(requests.map(async ([key, path]) => {
      try {
        const response = await fetch(
          `${path}?fresh=${startedAt}-${sequence}`,
          {cache: "no-store"},
        );
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.detail || `HTTP ${response.status}`);
        }
        return [key, payload];
      } catch (error) {
        return [key, {
          available: false,
          reason: "cache_unavailable",
          detail: String(error),
        }];
      }
    }));
    if (sequence !== supplementalRequestSequence) return false;
    supplemental = Object.fromEntries(results);
    renderHistory(supplemental.history);
    renderEarlyWarnings(supplemental.earlyWarnings);
    renderDtcs(supplemental.dtcs);
    renderMaintenance(supplemental.maintenance);
    return true;
  })();
  const trackedOperation = operation.finally(() => {
    if (supplementalRequestInFlight === trackedOperation) {
      supplementalRequestInFlight = null;
    }
  });
  supplementalRequestInFlight = trackedOperation;
  return trackedOperation;
}

async function fetchSnapshot(expectedResyncGeneration = null) {
  const requestSequence = ++httpRequestSequence;
  const requestStartedMonotonicMs = performance.now();
  const requestStartedEpochMs = Date.now();
  const response = await fetch(
    `/v1/snapshot?fresh=${requestStartedEpochMs}-${requestSequence}`,
    {cache: "no-store"},
  );
  const snapshot = await response.json();
  const responseMonotonicMs = performance.now();
  const roundTripMs = Math.max(
    0,
    responseMonotonicMs - requestStartedMonotonicMs,
  );
  if (
    (
      expectedResyncGeneration != null &&
      expectedResyncGeneration !== resyncGeneration
    ) ||
    requestSequence !== httpRequestSequence ||
    requestSequence <= latestHttpResponseSequence
  ) {
    return false;
  }
  if (!response.ok) {
    throw new Error(snapshot.detail || `HTTP ${response.status}`);
  }
  const result = acceptSnapshot(snapshot, "http", {
    roundTripMs,
    clientMidpointMonotonicMs: (
      requestStartedMonotonicMs + roundTripMs / 2
    ),
    clientReceiptMonotonicMs: responseMonotonicMs,
  });
  if (!result.accepted) {
    const reasons = {
      missing_delivery_metadata: "snapshot is missing web delivery metadata",
      http_timing_required: "snapshot HTTP timing metadata is unavailable",
      http_response_delayed: "snapshot HTTP response exceeded the freshness bound",
    };
    throw new Error(reasons[result.reason] || `snapshot rejected: ${result.reason}`);
  }
  latestHttpResponseSequence = requestSequence;
  return true;
}

function stopEventStream() {
  streamAccepting = false;
  streamGeneration += 1;
  if (eventStream) {
    eventStream.close();
    eventStream = null;
  }
}

function startEventStream() {
  const generation = ++streamGeneration;
  const events = new EventSource("/v1/stream");
  eventStream = events;
  streamAccepting = true;
  events.addEventListener("snapshot", (event) => {
    if (generation !== streamGeneration || events !== eventStream) return;
    let snapshot;
    try {
      snapshot = JSON.parse(event.data);
    } catch (error) {
      text("service-state", `Invalid telemetry event: ${error}`);
      return;
    }
    const result = acceptSnapshot(snapshot, "stream");
    if (
      result.reason === "instance_changed" ||
      result.reason === "queued_stream_event" ||
      result.reason === "http_resync_required"
    ) {
      resyncSnapshot(result.reason);
    }
  });
  events.addEventListener("error", () => {
    if (generation === streamGeneration && events === eventStream) {
      text("service-state", "Telemetry stream reconnecting…");
      resyncSnapshot("stream_error");
    }
  });
}

async function resyncSnapshot(reason) {
  stopEventStream();
  if (resyncRetryTimer != null) {
    window.clearTimeout(resyncRetryTimer);
    resyncRetryTimer = null;
  }
  const generation = ++resyncGeneration;
  try {
    const accepted = await fetchSnapshot(generation);
    if (
      accepted &&
      generation === resyncGeneration &&
      document.visibilityState !== "hidden"
    ) {
      void fetchSupplemental();
      startEventStream();
    }
  } catch (error) {
    if (generation !== resyncGeneration) return;
    text("service-state", `Broker unavailable: ${error}`);
    if (document.visibilityState !== "hidden") {
      resyncRetryTimer = window.setTimeout(
        () => resyncSnapshot(`${reason}_retry`),
        RESYNC_RETRY_MS,
      );
    }
  }
}

byId("refresh").addEventListener("click", () => {
  resyncSnapshot("manual");
});

byId("oil-change-form").addEventListener("submit", saveOilChange);

byId("acquire").addEventListener("click", async () => {
  if (!acquisitionEnabled) return;
  const response = await fetch("/v1/acquisitions/battery.voltage", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({mode: "passive"}),
  });
  const result = await response.json();
  if (!response.ok) {
    text("battery-detail", result.detail || humanize(result.reason));
  }
  await resyncSnapshot("acquisition");
});

byId("dtc-park-confirm").addEventListener("change", updateDtcJobButtons);

byId("dtc-scan-start").addEventListener("click", async () => {
  if (!dtcJobsEnabled || byId("dtc-scan-start").disabled) return;
  if (!window.confirm(
    "Start the fixed read-only DTC batch now? Confirm Park, ignition ON, engine OFF, and stationary."
  )) return;
  byId("dtc-scan-start").disabled = true;
  try {
    const response = await fetch("/v1/diagnostics/dtc-jobs", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        confirm_parked: true,
        confirm_park_gear: true,
        confirm_ignition_on_engine_off: true,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    renderDtcJob(payload);
  } catch (error) {
    text("dtc-job-status", `DTC scan was not queued: ${error}`);
    updateDtcJobButtons();
  }
});

byId("dtc-scan-cancel").addEventListener("click", async () => {
  if (!dtcJobsEnabled || byId("dtc-scan-cancel").disabled) return;
  try {
    const response = await fetch(
      "/v1/diagnostics/dtc-jobs/current/cancel",
      {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({action: "cancel"}),
      },
    );
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    renderDtcJob(payload);
  } catch (error) {
    text("dtc-job-status", `Cancellation was not accepted: ${error}`);
  }
});

document.addEventListener("visibilitychange", () => {
  stopEventStream();
  resyncGeneration += 1;
  invalidateDisplayedFreshness(
    document.visibilityState === "hidden"
      ? "client_page_hidden"
      : "client_page_visible",
  );
  if (document.visibilityState !== "hidden") resyncSnapshot("visibility");
});
window.addEventListener("pageshow", () => {
  stopEventStream();
  invalidateDisplayedFreshness("client_page_restored");
  resyncSnapshot("pageshow");
});

setupProfiles();
resyncSnapshot("initial");
window.setInterval(freshnessTick, FRESHNESS_TICK_MS);
window.setInterval(() => {
  if (document.visibilityState !== "hidden") void fetchSupplemental();
}, SUPPLEMENTAL_REFRESH_MS);
