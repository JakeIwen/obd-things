"use strict";

const byId = (id) => document.getElementById(id);
let acquisitionEnabled = false;

function render(snapshot) {
  const metric = snapshot.battery || snapshot;
  const status = snapshot.status || {};
  const web = snapshot.web || status.web;
  if (web && Object.hasOwn(web, "active_acquisition_enabled")) {
    acquisitionEnabled = Boolean(web.active_acquisition_enabled);
  }
  byId("acquire").disabled = !acquisitionEnabled;
  byId("control-note").textContent = acquisitionEnabled
    ? "Wake requests are enabled and remain broker-gated and rate-limited."
    : "Web acquisition is disabled; this page only reads broker cache.";
  if (metric.available) {
    byId("voltage").textContent = Number(metric.value).toFixed(2);
    byId("quality").textContent = metric.stale ? "STALE" : String(metric.quality).toUpperCase();
    byId("bus").textContent = metric.bus || "—";
    byId("source").textContent = metric.source || "—";
    byId("age").textContent = metric.age_ms == null ? "—" : `${(metric.age_ms / 1000).toFixed(1)} s`;
    byId("acquisition").textContent = metric.acquisition || "—";
    byId("detail").textContent = metric.last_acquisition_error
      ? `Last attempt: ${metric.last_acquisition_error.detail}`
      : "Latest broker-cached observation.";
  } else {
    byId("voltage").textContent = "—";
    byId("quality").textContent = String(metric.reason || "unavailable").toUpperCase();
    byId("detail").textContent = metric.detail || "No cached observation.";
  }
  const collector = status.collector || {};
  const retune = status.auto_retune || {};
  const retuneState = String(retune.state || "unknown").replaceAll("_", " ");
  let retuneText = retune.enabled === false
    ? "Disabled"
    : `${retuneState}: ${retune.detail || "No status detail."}`;
  if (
    retune.last_attempt &&
    retune.state === "monitoring" &&
    retune.last_attempt.state === "switched"
  ) {
    retuneText += ` Last switch: ${retune.last_attempt.bus || "unknown bus"} at ` +
      `${retune.last_attempt.to_bitrate || "unknown rate"} bit/s.`;
  }
  byId("auto-retune").textContent = retuneText;
  byId("service-state").textContent =
    `Broker ${status.service ? "online" : "unknown"} · collector ${collector.state || "unknown"}`;
}

async function fetchSnapshot() {
  const [metric, status] = await Promise.all([
    fetch("/v1/metrics/battery.voltage").then((r) => r.json()),
    fetch("/v1/status").then((r) => r.json()),
  ]);
  render({battery: metric, status, web: status.web});
}

byId("refresh").addEventListener("click", () => {
  fetchSnapshot().catch((error) => {
    byId("service-state").textContent = `Broker unavailable: ${error}`;
  });
});

byId("acquire").addEventListener("click", async () => {
  if (!acquisitionEnabled) return;
  if (!window.confirm("Wake the selected ordinary CAN bus if asleep? This may transmit and briefly power modules.")) return;
  const response = await fetch("/v1/acquisitions/battery.voltage", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({mode: "wake_if_asleep"}),
  });
  render(await response.json());
});

const events = new EventSource("/v1/stream");
events.addEventListener("snapshot", (event) => render(JSON.parse(event.data)));
events.addEventListener("error", () => {
  byId("service-state").textContent = "Telemetry stream reconnecting…";
});
fetchSnapshot().catch(() => {});
