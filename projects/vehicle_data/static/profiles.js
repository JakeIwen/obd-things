"use strict";

(() => {
  const STORAGE_KEY = "van-telemetry.dashboard.v3";
  const MAX_AUTOMATIC_STATE_AGE_MS = 5000;
  // This registry owns identity, editor labels, default order and legal widths.
  // Dense visual packing lives here too, so DOM, keyboard and editor order agree.
  const widgets = Object.freeze([
    ["maintenance", "Vehicle & Service", "half"],
    ["drive", "Drive Essentials", "full"],
    ["engine", "Engine Health", "full"],
    ["charging", "Charging", "full"],
    ["vehicle", "Vehicle State", "half"],
    ["battery", "12 V System", "half"],
    ["tires", "Tire Pressure", "half"],
    ["metrics", "Registered Telemetry", "half"],
    ["radar", "ACC / FCW", "half"],
    ["interface", "SocketCAN / Vehicle Buses", "half"],
    ["collector", "Collector Health", "half"],
    ["warnings", "Changes Worth Watching", "full"],
    ["history", "Trips And Trends", "full"],
    ["dtcs", "Diagnostic Trouble Codes", "full"],
    ["catalog", "Metric Catalog", "full"],
  ].map(([id, label, width]) => Object.freeze({
    id, label, width,
    widths: Object.freeze(width === "half" ? ["half", "full"] : ["full"]),
  })));
  const byId = new Map(widgets.map((widget) => [widget.id, widget]));

  const profiles = Object.freeze({
    overview: {
      label: "Overview",
      title: "Road systems",
      widgets: [
        "maintenance", "vehicle",
        "engine",
        "charging",
        "battery",
        "tires",
        "metrics",
        "radar",
        "interface",
        "warnings", "history", "dtcs",
      ],
    },
    parked: {
      label: "Parked",
      title: "Electrical watch",
      widgets: [
        "maintenance",
        "vehicle",
        "battery",
        "tires",
        "radar",
        "collector",
        "warnings", "history", "dtcs",
      ],
    },
    driving: {
      label: "Driving",
      title: "Drive telemetry",
      widgets: [
        "drive",
        "engine",
        "charging",
        "battery",
        "tires",
        "metrics",
        "radar",
        "warnings",
      ],
    },
    diagnostics: {
      label: "Diagnostics",
      title: "Broker diagnostics",
      widgets: [
        "engine",
        "charging",
        "vehicle",
        "battery",
        "metrics",
        "radar",
        "interface",
        "collector",
        "warnings", "history", "dtcs",
        "catalog",
      ],
    },
  });

  const defaultSettings = () => ({
    version: 3,
    selected: "overview",
    customLayout: widgets.map(({id, width}) => ({id, width, visible: true})),
  });

  function normalizeSettings(candidate) {
    const defaults = defaultSettings();
    if (!candidate || typeof candidate !== "object") return defaults;
    const validSelections = new Set(["auto", "custom", ...Object.keys(profiles)]);
    const selected = validSelections.has(candidate.selected)
      ? candidate.selected
      : defaults.selected;
    let supplied = candidate.customLayout;
    if (!Array.isArray(supplied) && Array.isArray(candidate.customWidgets)) {
      const visible = new Set(candidate.customWidgets.map(
        (id) => id === "source" || id === "controls" ? "battery" : id,
      ));
      // These two tiles bypassed v1/v2 customization and were always in Custom.
      visible.add("maintenance");
      visible.add("radar");
      supplied = widgets.map(({id, width}) => ({id, width, visible: visible.has(id)}));
    }
    const seen = new Set();
    const customLayout = [];
    (Array.isArray(supplied) ? supplied : defaults.customLayout).forEach((entry) => {
      const widget = byId.get(entry?.id);
      if (!widget || seen.has(widget.id)) return;
      seen.add(widget.id);
      customLayout.push({id: widget.id,
        width: widget.widths.includes(entry.width) ? entry.width : widget.width,
        visible: entry.visible !== false,
        ...(entry.solo === true ? {solo: true} : {})});
    });
    widgets.forEach(({id, width}) => {
      if (!seen.has(id)) customLayout.push({id, width, visible: true});
    });
    // Preserve the position of the one unavoidable solo row when moving rows.
    // A second solo, or any solo when the half-tile count is even, must pair.
    let soloAllowed = customLayout.filter((entry) => entry.visible && entry.width === "half").length % 2 === 1;
    customLayout.forEach((entry) => {
      if (entry.solo && entry.visible && entry.width === "half" && soloAllowed) soloAllowed = false;
      else delete entry.solo;
    });
    return {version: 3, selected, customLayout};
  }

  function packRows(layout) {
    const queue = layout.filter((entry) => entry.visible).map((entry) => ({...entry}));
    const rows = [];
    while (queue.length) {
      const row = [queue.shift()];
      if (row[0].width === "half" && !row[0].solo) {
        const partner = queue.findIndex((entry) => entry.width === "half" && !entry.solo);
        if (partner >= 0) row.push(queue.splice(partner, 1)[0]);
      }
      rows.push(row);
    }
    return rows;
  }

  function loadSettings() {
    try {
      for (const key of [STORAGE_KEY, "van-telemetry.dashboard.v2", "van-telemetry.dashboard.v1"]) {
        let parsed;
        try {
          const stored = window.localStorage.getItem(key);
          if (stored === null) continue;
          parsed = JSON.parse(stored);
          if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) continue;
        } catch (_error) {
          continue;
        }
        const migrated = normalizeSettings(parsed);
        if (key.endsWith(".v1") && migrated.selected === "auto") migrated.selected = "overview";
        // A denied write must not discard successfully loaded preferences.
        try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(migrated)); } catch (_error) {}
        return migrated;
      }
      return defaultSettings();
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
    let effective;
    if (normalized.selected === "auto") {
      const automatic = automaticProfile(vehicleState);
      effective = {
        ...profiles[automatic.id],
        id: automatic.id,
        selection: "auto",
        reason: automatic.reason,
      };
    } else if (normalized.selected === "custom") {
      effective = {
        id: "custom",
        selection: "custom",
        label: "Custom",
        title: "My telemetry",
        reason: "Custom · stored only in this browser",
      };
    } else {
      const profile = profiles[normalized.selected] || profiles.overview;
      effective = {...profile, id: normalized.selected, selection: normalized.selected,
        reason: `Manual · ${profile.label}`};
    }
    const layout = effective.id === "custom" ? normalized.customLayout
      : effective.widgets.map((id) => ({id, width: byId.get(id).width, visible: true}));
    const rows = packRows(layout);
    const visible = new Set(rows.flat().map((entry) => entry.id));
    return {...effective, rows, widgets: [...visible],
      hidden: widgets.filter((widget) => !visible.has(widget.id)).map((widget) => (
        effective.id === "custom"
          ? normalized.customLayout.find((entry) => entry.id === widget.id)
          : {id: widget.id, width: widget.width, visible: false}
      ))};
  }

  function customize(settings, vehicleState) {
    const effective = resolve(settings, vehicleState);
    return settingsFromRows(effective.rows, effective.hidden);
  }

  function settingsFromRows(rows, hidden) {
    return normalizeSettings({selected: "custom", customLayout: [
      ...rows.flatMap((row) => row.map((entry) => ({...entry,
        solo: row.length === 1 && entry.width === "half"}))), ...hidden,
    ]});
  }

  function changeTile(settings, id, changes) {
    const normalized = normalizeSettings(settings);
    return normalizeSettings({...normalized, selected: "custom",
      customLayout: normalized.customLayout.map((entry) => entry.id === id
        ? {...entry, ...changes, id} : entry)});
  }

  function moveRow(settings, index, direction) {
    const current = resolve({...settings, selected: "custom"});
    const rows = current.rows;
    const target = index + direction;
    if (!rows[index] || !rows[target]) return normalizeSettings(settings);
    [rows[index], rows[target]] = [rows[target], rows[index]];
    return settingsFromRows(rows, current.hidden);
  }

  function pairTiles(settings, id, target) {
    const current = resolve({...settings, selected: "custom"});
    const visible = current.rows.flat();
    const first = visible.find((entry) => entry.id === id && entry.width === "half");
    const second = visible.find((entry) => entry.id === target && entry.width === "half");
    if (!first || !second || id === target) return normalizeSettings(settings);
    const position = current.rows.findIndex((row) => row.includes(first) || row.includes(second));
    const rows = current.rows.map((row) => row.filter((entry) => entry !== first && entry !== second));
    rows.splice(position, 0, [first, second]);
    return settingsFromRows(rows.filter((row) => row.length), current.hidden);
  }

  function swapRow(settings, index) {
    const current = resolve({...settings, selected: "custom"});
    if (current.rows[index]?.length === 2) current.rows[index].reverse();
    return settingsFromRows(current.rows, current.hidden);
  }

  window.VanDashboardProfiles = Object.freeze({
    widgets,
    profiles,
    defaultSettings,
    loadSettings,
    saveSettings,
    normalizeSettings,
    packRows,
    customize,
    changeTile,
    moveRow,
    pairTiles,
    swapRow,
    resolve,
  });
})();
