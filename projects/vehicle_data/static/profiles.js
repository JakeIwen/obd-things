"use strict";

(() => {
  const STORAGE_KEY = "van-telemetry.dashboard.v1";
  const widgets = Object.freeze([
    {id: "vehicle", label: "Vehicle state"},
    {id: "battery", label: "Battery"},
    {id: "metrics", label: "Additional metrics"},
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
      widgets: ["vehicle", "battery", "metrics", "interface", "retune"],
    },
    parked: {
      label: "Parked",
      title: "Electrical watch",
      widgets: ["vehicle", "battery", "source", "collector", "controls"],
    },
    driving: {
      label: "Driving",
      title: "Drive telemetry",
      widgets: ["vehicle", "battery", "metrics", "interface"],
    },
    diagnostics: {
      label: "Diagnostics",
      title: "Broker diagnostics",
      widgets: ["vehicle", "interface", "retune", "collector", "source", "catalog"],
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
    if (state === "moving" || state === "running" || state === "ignition_on") {
      return {
        id: "driving",
        reason: `Automatic · ${state.replaceAll("_", " ")} evidence selects Driving`,
      };
    }
    if (state === "asleep" || state === "parked") {
      return {
        id: "parked",
        reason: `Automatic · ${state} evidence selects Parked`,
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
