"use strict";

const byId = (id) => document.getElementById(id);
const profileManager = window.VanDashboardProfiles;
let acquisitionEnabled = false;
let dtcJobsEnabled = false;
let dtcJobPollTimer = null;
let dtcLastJobState = null;
let dtcCancelRequested = false;
let settings = profileManager.loadSettings();
let lastSnapshot = {
  status: {},
  catalog: [],
  metrics: {},
};
let supplemental = {history: {}, earlyWarnings: {}, dtcs: {}};
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
let lastRoleGridSignature = null;
let lastCatalogSignature = null;
let additionalMetricStructureKey = null;
const additionalMetricNodes = new Map();

const DRIVER_QUALITIES = new Set(["verified", "observed_alfa_scale"]);
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
    names: ["engine.oil_temperature", "engine.oil_temp"],
    roles: ["engine_oil_temperature", "oil_temperature"],
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
        "No newer verified vehicle-state snapshot arrived before the " +
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
    vehicle.detail = "Verified vehicle-state evidence lacked a valid age.";
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
  if (quality === "observed_alfa_scale") return "ALFA SCALE";
  return humanize(quality).toUpperCase();
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
  if (name === "vehicle.odometer") return value.toFixed(1);
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
  if (!definition) return "Mapping pending";
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
  return `${displayQuality(state.quality)} · ${formatAge(metric.age_ms)} old`;
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
    customWidgets: settings.customWidgets,
    id: effective.id,
    title: effective.title,
    reason: effective.reason,
    widgets: effective.widgets,
  });
  if (renderKey === lastProfileRenderKey) return;
  lastProfileRenderKey = renderKey;
  const visible = new Set(effective.widgets);
  document.querySelectorAll("[data-widget]").forEach((panel) => {
    panel.hidden = !visible.has(panel.dataset.widget);
  });
  document.body.dataset.profile = effective.id;
  updateMetricPanelSummary(effective.id);
  text("dashboard-title", effective.title);
  text("profile-reason", effective.reason);
  byId("profile").value = settings.selected;
  document.querySelectorAll("#widget-options input").forEach((input) => {
    input.checked = settings.customWidgets.includes(input.value);
  });
}

function setupProfiles() {
  const select = byId("profile");
  const choices = [
    ["auto", "Automatic"],
    ...Object.entries(profileManager.profiles).map(
      ([id, profile]) => [id, profile.label],
    ),
    ["custom", "Custom"],
  ];
  choices.forEach(([value, label]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.append(option);
  });
  profileManager.widgets.forEach((widget) => {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = widget.id;
    input.addEventListener("change", () => {
      const chosen = [...document.querySelectorAll("#widget-options input:checked")]
        .map((element) => element.value);
      settings = profileManager.saveSettings({
        selected: "custom",
        customWidgets: chosen,
      });
      setProfile();
    });
    label.append(input, document.createTextNode(widget.label));
    byId("widget-options").append(label);
  });
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
  text("state-confidence", humanize(vehicle.confidence));
  text("state-basis", humanize(vehicle.basis));
  text("state-age", formatAge(vehicle.age_ms));
  text("state-detail", vehicle.detail, "No passive state detail is available.");
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
    return {value: "ON", detail: `${displayQuality(confidence)} state evidence`};
  }
  if (vehicle.ignition_on === false) {
    return {value: "OFF", detail: `${displayQuality(confidence)} state evidence`};
  }
  if (
    confidence === "verified" &&
    ["moving", "running", "ignition_on"].includes(String(vehicle.state))
  ) {
    return {value: "ON", detail: `VERIFIED · ${humanize(vehicle.state)}`};
  }
  if (confidence === "verified" && vehicle.state === "asleep") {
    return {value: "OFF", detail: "VERIFIED · bus asleep"};
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
        : "Mapping pending",
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
      `${displayQuality(ignitionState.quality)} · ${formatAge(ignitionMetric.age_ms)} old`,
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
    ready === 4
      ? "4/4 LIVE"
      : (
        ready
          ? `${ready} LIVE · ${registered}/4 MAPPED`
          : `${registered}/4 MAPPED`
      ),
  );
  byId("drive-freshness").dataset.state = ready === 4 ? "verified" : "partial";
  text(
    "drive-note",
    ready === 4
      ? "All core drive essentials are fresh. ODOMETER* remains candidate-quality."
      : (
        `${registered}/4 drive sources are mapped. ` +
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
  byId(cardId).hidden = false;
  if (state.heroReady) {
    text(valueId, formatMetricValue(definition.name, metric.value));
    text(unitId, metric.unit || definition.unit, "");
    setCardState(cardId, state.quality);
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
    metricStatus(definition, metric, state),
  );
  return {definition, state};
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
  text(
    "engine-health-state",
    ready
      ? `${ready}/${total} LIVE · ${mapped}/${total} MAPPED`
      : `${mapped}/${total} MAPPED`,
  );
  byId("engine-health-state").dataset.state = ready === total
    ? "verified"
    : "partial";
  text(
    "engine-health-note",
    ready === total
      ? "All powertrain-health values are fresh and driver-qualified."
      : (
        "Priority gauges remain visible while exact C-CAN sources and " +
        "scaling are mapped. Candidate values remain available in Diagnostics."
      ),
  );
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
      ? `${displayChargingQuality(state.quality)} · ${formatAge(metric.age_ms)} old`
      : (
        inactive?.reason === "mapping_pending"
          ? "Mapping pending"
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
          ? `${displayChargingQuality(normalizedQuality(sourceDefinition.quality))} · REGISTERED`
          : null
      ),
  );
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
    ready
      ? `1/1 LIVE · ${displayChargingQuality(state.quality)}`
      : (definition ? "0/1 LIVE · 1/1 MAPPED" : "0/1 MAPPED"),
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
  const card = document.querySelector(".battery-panel");
  if (metric.available) {
    text("voltage", Number(metric.value).toFixed(2));
    text(
      "quality",
      metric.stale ? "STALE" : displayQuality(normalizedQuality(metric.quality)),
    );
    card.dataset.state = metric.stale
      ? "stale"
      : normalizedQuality(metric.quality);
    byId("quality").dataset.state = card.dataset.state;
    text("bus", metric.bus);
    text("source", metric.source);
    text("age", formatAge(metric.age_ms));
    text("acquisition", humanize(metric.acquisition));
    text(
      "battery-detail",
      metric.last_acquisition_error
        ? `Cached value retained · last attempt: ${metric.last_acquisition_error.detail}`
        : "Latest broker-cached observation.",
    );
    text("source-detail", metric.detail, "Verified registered source.");
  } else {
    text("voltage", "—");
    text("quality", String(metric.reason || "unavailable").toUpperCase());
    text("battery-detail", metric.detail, "No cached observation.");
    text("bus", metric.bus);
    text("source", null);
    text("age", null);
    text("acquisition", metric.acquisition ? humanize(metric.acquisition) : null);
    text("source-detail", "No current provenance is available.");
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
  const cardState = state.stale
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
  elementText(nodes.badge, state.stale ? "STALE" : displayQuality(state.quality));
  elementText(
    nodes.value,
    metric?.available
      ? `${formatMetricValue(definition.name, metric.value)} ${metric.unit || definition.unit}`
      : humanize(metric?.reason || "not sampled"),
  );
  elementText(
    nodes.meta,
    metric?.available
      ? `${displayQuality(state.quality)} · ${formatAge(metric.age_ms)} old`
      : metric?.detail || "No cached observation.",
  );
}

function featuredMetricNames(catalog) {
  const names = new Set(["battery.voltage"]);
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
    text(`tire-${position}-unit`, "", "");
    setCardState(cardId, "unavailable");
    text(`tire-${position}-status`, metricStatus(definition, metric, state));
    return {
      registered: false,
      live: false,
      quality: state.quality,
    };
  }
  if (state.heroReady) {
    text(`tire-${position}`, formatMetricValue(definition.name, metric.value));
    text(`tire-${position}-unit`, metric.unit || definition.unit, "");
    setCardState(`tire-${position}-card`, state.quality);
  } else {
    text(`tire-${position}`, "—");
    text(`tire-${position}-unit`, "", "");
    setCardState(
      `tire-${position}-card`,
      state.stale ? "stale" : (state.available ? "unqualified" : "unavailable"),
    );
  }
  text(`tire-${position}-status`, metricStatus(definition, metric, state));
  return {
    registered: Boolean(definition),
    live: state.heroReady,
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
    ready === 4
      ? `4/4 LIVE · ${qualityLabel}`
      : (
        ready
          ? `${ready}/4 LIVE · ${registered}/4 MAPPED`
          : (
            registered
              ? `0/4 LIVE · ${registered}/4 MAPPED`
              : "0/4 MAPPED"
          )
      ),
  );
  byId("tires-state").dataset.state = allVerified
    ? "verified"
    : "partial";
  text(
    "tires-note",
    allVerified
      ? "All four verified wheel-position samples are fresh."
      : (
        ready === 4
          ? (
            "All four wheel-position samples are fresh. ALFA SCALE means " +
            "observed scaling, not independent verification."
          )
          : (
            registered
              ? (
                `${registered}/4 wheel-position sources are mapped; ` +
                "stale, unavailable, or candidate pressures are not shown as live."
              )
              : "Wheel-position pressure mapping is pending."
          )
      ),
  );
}

function renderInterface(status) {
  const iface = status.interface || {};
  const topology = iface.topology || {};
  text("channel", iface.channel);
  text(
    "adapter-state",
    iface.adapter_present === true
      ? (iface.up ? "Present · up" : "Present · down")
      : (iface.adapter_present === false ? "Absent" : "Unknown"),
  );
  text("bitrate", iface.bitrate == null ? null : `${iface.bitrate} bit/s`);
  text("controller-state", iface.controller_state);
  text("listen-only", yesNoUnknown(iface.listen_only));
  text(
    "topology",
    topology.bus
      ? `${topology.bus}${topology.pair ? ` · pins ${topology.pair}` : ""}`
      : null,
  );
  const owner = status.current_owner;
  text(
    "current-owner",
    owner
      ? owner.names?.join(", ") || owner.kind || owner.detail
      : "None reported",
  );
  text(
    "inhibits",
    Array.isArray(iface.active_inhibits) && iface.active_inhibits.length
      ? iface.active_inhibits.join(", ")
      : "None",
  );
  const roleSnapshot = iface.role_interfaces || {};
  const roles = roleSnapshot.roles && typeof roleSnapshot.roles === "object"
    ? roleSnapshot.roles
    : {};
  const roleSignature = JSON.stringify(roles);
  if (roleSignature === lastRoleGridSignature) return;
  lastRoleGridSignature = roleSignature;
  const roleGrid = byId("interface-roles");
  roleGrid.replaceChildren();
  Object.entries(roles).forEach(([role, payload]) => {
    const expected = payload?.expected || {};
    const actual = payload?.actual || {};
    const card = document.createElement("article");
    card.className = "role-card";
    card.dataset.state = payload?.safe ? "ready" : "unavailable";
    const heading = document.createElement("h3");
    heading.textContent = `${role} · ${payload?.channel || "unresolved"}`;
    const link = document.createElement("p");
    link.textContent = expected.passive_required === false
      ? `spare · ${actual.up === false ? "down" : humanize(payload?.reason)}`
      : `${expected.pair ? `pins ${expected.pair} · ` : ""}` +
        `${actual.bitrate ?? expected.bitrate ?? "—"} bit/s · ` +
        `${actual.fd_enabled === false ? "classical CAN" : actual.fd_enabled === true ? "CAN FD" : "CAN mode unknown"} · ` +
        `${actual.listen_only === true ? "listen-only" : humanize(payload?.reason)}`;
    const identity = document.createElement("p");
    identity.textContent = expected.usb_serial
      ? `board ${expected.board} ${expected.connector} · serial …${String(expected.usb_serial).slice(-6)} · dev ${expected.dev_id}`
      : payload?.detail || "Identity unavailable";
    card.append(heading, link, identity);
    roleGrid.append(card);
  });
  roleGrid.hidden = roleGrid.children.length === 0;
}

function renderCollector(status) {
  const collector = status.collector || {};
  text("collector-state", humanize(collector.state));
  text("collector-cycles", collector.cycles);
  text("collector-last", collector.last_cycle_at);
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
      assessment.persistence?.observed == null
        ? null
        : `${assessment.persistence.observed}/${assessment.persistence.required} persistent observations`,
    ].filter(Boolean).join(" · ");
    article.append(heading, reason);
    if (evidence.textContent) article.append(evidence);
    list.append(article);
  });
  const qualityShown = activeQuality.length
    ? activeQuality
    : recentQuality.filter((event) => event?.status === "resolved").slice(0, 3);
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
    list.append(article);
  });
  if (!shown.length && !qualityShown.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = unavailable
      ? summary.detail || "Early-warning history is unavailable."
      : "No persistent regime-matched change is currently active.";
    list.append(empty);
  }
  text(
    "warning-note",
    [
      summary.detail || (
        "Warnings require persistent, like-for-like deviations and show their baseline evidence. They are not a diagnosis or an opaque health score."
      ),
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
      list.append(item);
    });
    section.append(heading, list);
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
            ? "the guarded controls below can queue it after local arming."
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
  const token = byId("dtc-arm-token").value.trim();
  byId("dtc-scan-start").disabled = !(
    dtcJobsEnabled && confirmed && token.length >= 32 &&
    !dtcJobIsActive(dtcLastJobState) && dtcLastJobState !== "restoration_failed"
  );
  byId("dtc-scan-cancel").disabled = !(
    dtcJobsEnabled && dtcJobIsActive(dtcLastJobState) && !dtcCancelRequested
  );
}

function renderDtcJob(payload) {
  const job = payload?.job || {};
  const state = payload?.state || job.state || "idle";
  dtcLastJobState = state;
  dtcCancelRequested = job.cancel_requested === true;
  const progress = job.progress || {};
  const parts = [humanize(state)];
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
      ? "DIAGNOSTICS · LOCALLY ARMED PARKED SCAN"
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
      `stale after ${definition.stale_after_seconds} s`
    );
    item.append(heading, meta);
    (definition.sources || []).forEach((source) => {
      const sourceLine = document.createElement("p");
      sourceLine.textContent = `${source.name} · ${source.bus} · ${source.quality}`;
      item.append(sourceLine);
    });
    list.append(item);
  });
}

function render(snapshot) {
  if (!snapshot || typeof snapshot !== "object") return;
  const status = snapshot.status || {};
  const web = snapshot.web || status.web || {};
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
      ? "A receive-only voltage read can be requested from the broker."
      : "Web acquisition is disabled; this page only reads broker cache.",
  );
  renderVehicleState(status);
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

byId("dtc-arm-token").addEventListener("input", updateDtcJobButtons);
byId("dtc-park-confirm").addEventListener("change", updateDtcJobButtons);

byId("dtc-scan-start").addEventListener("click", async () => {
  if (!dtcJobsEnabled || byId("dtc-scan-start").disabled) return;
  if (!window.confirm(
    "Start the fixed read-only DTC batch now? Confirm Park, ignition ON, engine OFF, and stationary."
  )) return;
  const token = byId("dtc-arm-token").value.trim();
  byId("dtc-scan-start").disabled = true;
  try {
    const response = await fetch("/v1/diagnostics/dtc-jobs", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        token,
        confirm_parked: true,
        confirm_park_gear: true,
        confirm_ignition_on_engine_off: true,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    byId("dtc-arm-token").value = "";
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
