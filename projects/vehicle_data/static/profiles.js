"use strict";

(() => {
  const STORAGE_KEY = "van-telemetry.dashboard.v1";
  const MAX_AUTOMATIC_STATE_AGE_MS = 3000;
  const widgets = Object.freeze([
    {id: "drive", label: "Drive essentials"},
    {id: "vehicle", label: "Vehicle state"},
    {id: "battery", label: "Battery"},
    {id: "tires", label: "Tire pressure"},
    {id: "metrics", label: "Other telemetry"},
    {id: "source", label: "Metric provenance"},
    {id: "interface", label: "CAN interface"},
    {id: "retune", label: "Automatic bus switch"},
    {id: "collector", label: "Collector health"},
    {id: "catalog", label: "Metric catalog"},
    {id: "controls", label: "Acquisition controls"},
  ]);

  const profiles = Object.freeze({
    overview: {
      label: "Overview",
      title: "Road systems",
      widgets: ["vehicle", "battery", "tires", "metrics", "interface", "retune"],
    },
    parked: {
      label: "Parked",
      title: "Electrical watch",
      widgets: ["vehicle", "battery", "source", "collector", "controls"],
    },
    driving: {
      label: "Driving",
      title: "Drive telemetry",
      widgets: ["drive", "tires", "battery", "metrics"],
    },
    diagnostics: {
      label: "Diagnostics",
      title: "Broker diagnostics",
      widgets: [
        "vehicle",
        "metrics",
        "interface",
        "retune",
        "collector",
        "source",
        "catalog",
      ],
    },
  });

  const defaultSettings = () => ({
    selected: "auto",
    customWidgets: widgets.map((widget) => widget.id),
  });

  function normalizeSettings(candidate) {
    const defaults = defaultSettings();
    if (!candidate || typeof candidate !== "object") return defaults;
    const validSelections = new Set(["auto", "custom", ...Object.keys(profiles)]);
    const selected = validSelections.has(candidate.selected)
      ? candidate.selected
      : defaults.selected;
    const allowedWidgets = new Set(widgets.map((widget) => widget.id));
    const customWidgets = Array.isArray(candidate.customWidgets)
      ? candidate.customWidgets.filter((name) => allowedWidgets.has(name))
      : defaults.customWidgets;
    return {
      selected,
      customWidgets: customWidgets.length ? [...new Set(customWidgets)] : defaults.customWidgets,
    };
  }

  function loadSettings() {
    try {
      return normalizeSettings(JSON.parse(window.localStorage.getItem(STORAGE_KEY)));
    } catch (_error) {
      return defaultSettings();
    }
  }

  function saveSettings(settings) {
    const normalized = normalizeSettings(settings);
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(normalized));
    } catch (_error) {
      // A private/restricted browser can deny localStorage. The in-memory
      // selection still works for the current page.
    }
    return normalized;
  }

  function automaticProfile(vehicleState) {
    const state = String(vehicleState?.state || "unknown");
    const confidence = String(vehicleState?.confidence || "unknown");
    const freshState = (
      typeof vehicleState?.age_ms === "number" &&
      Number.isFinite(vehicleState.age_ms) &&
      vehicleState.age_ms >= 0 &&
      vehicleState.age_ms <= MAX_AUTOMATIC_STATE_AGE_MS
    );
    if (
      confidence === "verified" &&
      freshState &&
      (state === "moving" || state === "running" || state === "ignition_on")
    ) {
      return {
        id: "driving",
        reason: `Automatic · ${state.replaceAll("_", " ")} evidence selects Driving`,
      };
    }
    if (state === "moving" || state === "running" || state === "ignition_on") {
      const evidenceStatus = confidence !== "verified"
        ? confidence
        : (freshState ? "fresh and verified" : "verified but stale");
      return {
        id: "overview",
        reason: (
          `Automatic · ${state.replaceAll("_", " ")} evidence is ` +
          `${evidenceStatus}, so Overview remains selected`
        ),
      };
    }
    if ((state === "asleep" || state === "parked") && freshState) {
      return {
        id: "parked",
        reason: `Automatic · ${state} evidence selects Parked`,
      };
    }
    if (state === "asleep" || state === "parked") {
      return {
        id: "overview",
        reason: (
          `Automatic · ${state} evidence is stale or undated, ` +
          "so Overview remains selected"
        ),
      };
    }
    return {
      id: "overview",
      reason: (
        "Automatic · awake traffic does not yet prove the engine is running, " +
        "so Overview remains selected"
      ),
    };
  }

  function resolve(settings, vehicleState) {
    const normalized = normalizeSettings(settings);
    if (normalized.selected === "auto") {
      const automatic = automaticProfile(vehicleState);
      return {
        ...profiles[automatic.id],
        id: automatic.id,
        selection: "auto",
        reason: automatic.reason,
      };
    }
    if (normalized.selected === "custom") {
      return {
        id: "custom",
        selection: "custom",
        label: "Custom",
        title: "My telemetry",
        widgets: normalized.customWidgets,
        reason: "Custom · stored only in this browser",
      };
    }
    const profile = profiles[normalized.selected] || profiles.overview;
    return {
      ...profile,
      id: normalized.selected,
      selection: normalized.selected,
      reason: `Manual · ${profile.label}`,
    };
  }

  window.VanDashboardProfiles = Object.freeze({
    widgets,
    profiles,
    defaultSettings,
    loadSettings,
    saveSettings,
    resolve,
  });
})();
