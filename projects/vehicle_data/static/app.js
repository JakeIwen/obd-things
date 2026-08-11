"use strict";

const byId = (id) => document.getElementById(id);
const profileManager = window.VanDashboardProfiles;
let acquisitionEnabled = false;
let settings = profileManager.loadSettings();
let lastSnapshot = {status: {}, catalog: [], metrics: {}};
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

const DRIVER_QUALITIES = new Set(["verified", "observed_alfa_scale"]);
const MAX_STATE_FALLBACK_AGE_MS = 5000;
const MAX_STREAM_DELIVERY_AGE_MS = 10000;
const MAX_HTTP_ROUND_TRIP_MS = 2000;
const STREAM_STALL_RESYNC_MS = 3000;
const FRESHNESS_TICK_MS = 1000;
const RESYNC_RETRY_MS = 2000;
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
  byId(id).textContent = value == null || value === "" ? fallback : String(value);
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
  if (name.includes("rpm")) return Math.round(value).toLocaleString();
  if (name.includes("gear")) return String(value);
  if (name.includes("speed") || name.includes("pressure")) return value.toFixed(1);
  if (name === "battery.voltage") return value.toFixed(2);
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

function setCardState(id, state) {
  byId(id).dataset.state = state;
}

function metricStatus(definition, metric, state) {
  if (!definition) return "Mapping pending";
  if (!state.available) {
    return humanize(metric?.reason || "no cached sample");
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
      ? "All drive essentials are fresh and driver-qualified."
      : (
        `${registered}/4 drive sources are mapped. ` +
        "Only fresh, driver-qualified values are promoted here. " +
        "Candidate and raw diagnostic DIDs are held out."
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

function metricCard(definition, metric) {
  const article = document.createElement("article");
  article.className = "metric-card";
  const state = observationState(definition, metric);
  article.dataset.state = state.stale
    ? "stale"
    : (state.available ? state.quality : "unavailable");
  if (!state.driverQualified) article.classList.add("diagnostic-only");
  const label = document.createElement("p");
  label.className = "eyebrow";
  label.textContent = definition.name;
  const badge = document.createElement("span");
  badge.className = "metric-quality";
  badge.textContent = state.stale
    ? "STALE"
    : displayQuality(state.quality);
  const value = document.createElement("p");
  value.className = "metric-value";
  value.textContent = metric?.available
    ? `${formatMetricValue(definition.name, metric.value)} ${metric.unit || definition.unit}`
    : humanize(metric?.reason || "not sampled");
  const meta = document.createElement("p");
  meta.className = "muted";
  meta.textContent = metric?.available
    ? `${displayQuality(state.quality)} · ${formatAge(metric.age_ms)} old`
    : metric?.detail || "No cached observation.";
  article.append(label, badge, value, meta);
  return article;
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
        DRIVER_QUALITIES.has(definitionQuality(definition))
      ) {
        names.add(definition.name);
      }
    });
  return names;
}

function renderAdditionalMetrics(catalog, metrics) {
  const grid = byId("metric-grid");
  grid.replaceChildren();
  const featured = featuredMetricNames(catalog);
  const additional = catalog.filter(
    (definition) => !featured.has(definition.name),
  );
  additional.forEach((definition) => {
    grid.append(metricCard(definition, metrics[definition.name]));
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
}

function renderRetune(status) {
  const retune = status.auto_retune || {};
  text(
    "auto-retune-state",
    retune.enabled === false ? "Disabled" : humanize(retune.state),
  );
  let detail = retune.detail || "No status detail.";
  if (retune.wrong_rate_streak) {
    detail += ` Evidence ${retune.wrong_rate_streak}/${retune.trigger_after}.`;
  }
  if (retune.cooldown_remaining_seconds > 0) {
    detail += ` Cooldown ${retune.cooldown_remaining_seconds} s.`;
  }
  if (retune.last_attempt) {
    const attempt = retune.last_attempt;
    detail += ` Last attempt: ${humanize(attempt.reason)} — ${attempt.detail}`;
  }
  text("auto-retune", detail);
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

function renderCatalog(catalog) {
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
  lastSnapshot = {status, web, metrics, catalog};
  if (Object.hasOwn(web, "active_acquisition_enabled")) {
    acquisitionEnabled = Boolean(web.active_acquisition_enabled);
  }
  byId("acquire").disabled = !acquisitionEnabled;
  text(
    "control-note",
    acquisitionEnabled
      ? "Wake requests are enabled and remain broker-gated and rate-limited."
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
  renderRetune(status);
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
  advanceDisplayedAges();
  if (
    document.visibilityState !== "hidden" &&
    streamAccepting &&
    eventStream &&
    lastAcceptedMonotonicMs != null &&
    performance.now() - lastAcceptedMonotonicMs > STREAM_STALL_RESYNC_MS
  ) {
    resyncSnapshot("stream_stall");
  }
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
  const confirmed = window.confirm(
    "Wake the selected ordinary CAN bus if asleep? " +
    "This may transmit and briefly power modules.",
  );
  if (!confirmed) return;
  const response = await fetch("/v1/acquisitions/battery.voltage", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({mode: "wake_if_asleep"}),
  });
  const result = await response.json();
  if (!response.ok) {
    text("battery-detail", result.detail || humanize(result.reason));
  }
  await resyncSnapshot("acquisition");
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
