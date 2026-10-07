/**
 * Application shell: top bar, alert strip, the active view, the tab bar and
 * the lazily loaded dialog host (design section 3).
 */

import { useEffect, useState } from "preact/hooks";
import { effect, useComputed } from "@preact/signals";
import * as store from "../store.js";
import { settings, saveSettings } from "../settings.js";
import {
  activeView,
  evaluateAutoView,
  vehicleHead,
  vehicleTail,
  topBarClass,
  alertStrip,
  healthBadge,
  startEngineTracking,
} from "./derive.js";
import { dialog, closeDialog, openEvent, openChat, DIALOG_LOADERS } from "./dialogs.js";
import { manualRefresh } from "./runtime.js";
import Drive from "../views/Drive.jsx";

const VIEW_LOADERS = {
  parked: () => import("../views/Parked.jsx"),
  health: () => import("../views/Health.jsx"),
  history: () => import("../views/History.jsx"),
  system: () => import("../views/System.jsx"),
};
const TABS = [
  ["drive", "Drive"],
  ["parked", "Parked"],
  ["health", "Health"],
  ["history", "History"],
  ["system", "System"],
];

const loaded = new Map([["drive", Drive]]);

/**
 * Render a lazily loaded module's default export; `null` until the module for
 * the CURRENT key arrives (never the previous key's component with new props).
 */
function useLazy(key, loader) {
  const [entry, setEntry] = useState(() => (loaded.has(key) ? { key, component: loaded.get(key) } : null));
  useEffect(() => {
    let alive = true;
    if (loaded.has(key)) {
      setEntry({ key, component: loaded.get(key) });
      return undefined;
    }
    loader()
      .then((mod) => {
        loaded.set(key, mod.default);
        if (alive) setEntry({ key, component: mod.default });
      })
      .catch((error) => {
        console.error("failed to load", key, error);
      });
    return () => {
      alive = false;
    };
  }, [key]);
  if (entry && entry.key === key) return entry.component;
  return loaded.get(key) || null;
}

export function TopBar() {
  const conn = useComputed(() => "conn conn--" + store.connection.value.state);
  const connLabel = useComputed(() => {
    const c = store.connection.value;
    const state = c.state === "live" ? "Live" : c.state === "unavailable" ? "Broker unavailable" : "Reconnecting";
    return state + " · tap to refresh";
  });
  const autoCls = useComputed(() => "topbar__btn topbar__auto" + (settings.value.auto ? " topbar__btn--on" : ""));
  const autoPressed = useComputed(() => (settings.value.auto ? "true" : "false"));
  const dimCls = useComputed(() => "topbar__btn" + (settings.value.dim ? " topbar__btn--on" : ""));
  const dimPressed = useComputed(() => (settings.value.dim ? "true" : "false"));
  const barCls = useComputed(() => topBarClass(alertStrip.value, store.vehicle.value));
  const headText = useComputed(() => (alertStrip.value ? alertStrip.value.text : vehicleHead.value));
  const tailText = useComputed(() => {
    const a = alertStrip.value;
    if (a) return a.count > 1 ? " +" + (a.count - 1) : "";
    return vehicleTail.value ? " · " + vehicleTail.value : "";
  });
  const toggleAuto = () => {
    const s = settings.peek();
    saveSettings({ ...s, auto: !s.auto, view: activeView.peek() });
    evaluateAutoView();
  };
  const toggleDim = () => {
    const s = settings.peek();
    saveSettings({ ...s, dim: !s.dim });
  };
  const onState = () => {
    const a = alertStrip.peek();
    if (!a) return;
    if (a.first && a.first.episodeId) openEvent(a.first.episodeId);
    else selectView("health");
  };
  return (
    <header class={barCls}>
      <button type="button" class="topbar__conn" aria-label={connLabel} title={connLabel} onClick={() => manualRefresh()}>
        <span class={conn} aria-hidden="true" />
      </button>
      <button type="button" class="topbar__state" onClick={onState}>
        {headText}
        <span class="topbar__state-sub">{tailText}</span>
      </button>
      <button type="button" class={dimCls} aria-pressed={dimPressed} onClick={toggleDim} title="Dim the screen for night driving">
        Dim
      </button>
      <button type="button" class={autoCls} aria-pressed={autoPressed} onClick={toggleAuto} title="Switch between Drive and Parked automatically">
        Auto
      </button>
    </header>
  );
}

/** Manual view selection: turns automatic mode off (design 3). */
export function selectView(view) {
  activeView.value = view;
  const s = settings.peek();
  if (s.view !== view || s.auto) saveSettings({ ...s, view, auto: false });
}

function TabBar() {
  const current = activeView.value;
  const badge = healthBadge.value;
  return (
    <nav class="tabbar" aria-label="Views">
      {TABS.map(([id, label]) => (
        <button
          key={id}
          type="button"
          class={"tab" + (current === id ? " tab--active" : "")}
          aria-current={current === id ? "page" : undefined}
          onClick={() => selectView(id)}
        >
          {label}
          {id === "health" && badge.count > 0 ? (
            <span class={"tab__badge" + (badge.red ? " tab__badge--red" : "")}>{badge.count}</span>
          ) : null}
        </button>
      ))}
    </nav>
  );
}

function ActiveView() {
  const view = activeView.value;
  const Component = useLazy(view, VIEW_LOADERS[view] || (() => Promise.resolve({ default: Drive })));
  if (!Component) return <div class="view view--scroll"><p class="empty">Loading…</p></div>;
  return <Component />;
}

function DialogHost() {
  const current = dialog.value;
  const name = current ? current.name : null;
  const Component = useLazy(name ? "dialog:" + name : "dialog:none", name ? DIALOG_LOADERS[name] : () => Promise.resolve({ default: () => null }));
  if (!current || !Component) return null;
  const props = current.props || {};
  const onClose = () => {
    if (props.onClose) props.onClose();
    closeDialog();
  };
  const extra = {};
  // Ask Codex exists only when this listener has the advisor enabled (as in the former static app).
  if (name === "events" && store.web.value && store.web.value.warning_chat_enabled === true) {
    extra.onAskCodex = (eventId, summary) => openChat({ eventId, summary }, "explain");
  }
  return <Component {...extra} {...props} open onClose={onClose} />;
}

startEngineTracking();

export function App() {
  useEffect(() => effect(() => evaluateAutoView()), []);
  useEffect(
    () =>
      effect(() => {
        document.documentElement.classList.toggle("dim", Boolean(settings.value.dim));
      }),
    [],
  );
  return (
    <>
      <TopBar />
      <ActiveView />
      <TabBar />
      <DialogHost />
    </>
  );
}
