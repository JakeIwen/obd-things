/**
 * Dialog host state. Views call `openDialog(name, props)`; the App renders the
 * lazily loaded component with `open`, the props, and an `onClose` that clears
 * the state. Each dialog is its own esbuild chunk loaded on first use.
 */

import { signal } from "@preact/signals";

/** Current dialog `{name, props}` or null. */
export const dialog = signal(null);

/** Dialog name → loader of its module (default export is the component). */
export const DIALOG_LOADERS = {
  events: () => import("../dialogs/EventHistory.jsx"),
  chat: () => import("../dialogs/WarningChat.jsx"),
  customize: () => import("../dialogs/Customizer.jsx"),
  oil: () => import("../dialogs/OilChange.jsx"),
};

/**
 * Open a dialog.
 * @param {'events'|'chat'|'customize'|'oil'} name
 * @param {object} [props]
 */
export function openDialog(name, props) {
  dialog.value = { name, props: props || {} };
}

/** Close whatever dialog is open. */
export function closeDialog() {
  dialog.value = null;
}

/** Open the event dialog on one event (or the list when id is null). */
export function openEvent(eventId) {
  openDialog("events", { eventId: eventId === undefined ? null : eventId });
}

/** Open the Codex chat about one warning card. */
export function openChat(context, mode) {
  openDialog("chat", { mode: mode || "explain", context: context || {} });
}
