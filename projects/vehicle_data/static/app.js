"use strict";

const byId = (id) => document.getElementById(id);
const profileManager = window.VanDashboardProfiles;
let acquisitionEnabled = false;
let settings = profileManager.loadSettings();
let lastSnapshot = {status: {}, catalog: [], metrics: {}};

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

function yesNoUnknown(value) {
  if (value === true) return "Yes";
  if (value === false) return "No";
  return "Unknown";
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

function renderBattery(metrics) {
  const metric = metrics["battery.voltage"] || {
    metric: "battery.voltage",
    available: false,
    reason: "stale",
    detail: "No cached observation.",
  };
  if (metric.available) {
    text("voltage", Number(metric.value).toFixed(2));
    text("quality", metric.stale ? "STALE" : String(metric.quality).toUpperCase());
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
    text("source-detail", metric.detail, "Verified allowlisted source.");
  } else {
    text("voltage", "—");
    text("quality", String(metric.reason || "unavailable").toUpperCase());
    text("battery-detail", metric.detail, "No cached observation.");
    text("bus", metric.bus);
    text("source", null);
    text("age", null);
    text("acquisition", metric.acquisition ? humanize(metric.acquisition) : null);
    text("source-detail", "No current provenance is available.");
  }
}

function metricCard(definition, metric) {
  const article = document.createElement("article");
  article.className = "metric-card";
  const label = document.createElement("p");
  label.className = "eyebrow";
  label.textContent = definition.name;
  const value = document.createElement("p");
  value.className = "metric-value";
  value.textContent = metric?.available
    ? `${metric.value} ${metric.unit || definition.unit}`
    : humanize(metric?.reason || "not sampled");
  const meta = document.createElement("p");
  meta.className = "muted";
  meta.textContent = metric?.available
    ? `${metric.quality || "unknown quality"} · ${formatAge(metric.age_ms)} old`
    : metric?.detail || "No cached observation.";
  article.append(label, value, meta);
  return article;
}

function renderAdditionalMetrics(catalog, metrics) {
  const grid = byId("metric-grid");
  grid.replaceChildren();
  const additional = catalog.filter(
    (definition) => definition.name !== "battery.voltage",
  );
  additional.forEach((definition) => {
    grid.append(metricCard(definition, metrics[definition.name]));
  });
  text("metric-count", catalog.length);
  byId("metrics-empty").hidden = additional.length > 0;
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
  renderBattery(metrics);
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
      `${catalog.length === 1 ? "" : "s"} allowlisted`,
  );
  setProfile();
}

async function fetchSnapshot() {
  const response = await fetch("/v1/snapshot");
  const snapshot = await response.json();
  if (!response.ok) {
    throw new Error(snapshot.detail || `HTTP ${response.status}`);
  }
  render(snapshot);
}

byId("refresh").addEventListener("click", () => {
  fetchSnapshot().catch((error) => {
    text("service-state", `Broker unavailable: ${error}`);
  });
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
  await fetchSnapshot();
});

setupProfiles();
const events = new EventSource("/v1/stream");
events.addEventListener("snapshot", (event) => render(JSON.parse(event.data)));
events.addEventListener("error", () => {
  text("service-state", "Telemetry stream reconnecting…");
});
fetchSnapshot().catch((error) => {
  text("service-state", `Broker unavailable: ${error}`);
});
