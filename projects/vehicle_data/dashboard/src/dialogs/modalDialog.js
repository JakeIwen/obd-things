/**
 * The open/close behaviour every sheet under dialogs/ shares.
 *
 * `useModalDialog` follows the `open` prop: on the rising edge it remembers what had focus and
 * opens the <dialog> with showModal (which traps focus and handles Escape); every time `open` or
 * a dependency changes while open it runs `onOpen` and focuses `focusRef`; any native close (Esc,
 * the × button, a backdrop tap, close()) runs `onNativeClose`, gives focus back to the opener and
 * tells the parent. The callbacks are read at call time, so they may close over fresh props.
 *
 * `showModal` and `restoreFocus` are exported for a dialog whose open/close flow has extra steps
 * of its own (EventHistory's hand-off to the chat).
 */

import { useEffect, useRef } from "preact/hooks";

/** Open `dialog` modally (attribute fallback where showModal throws); returns what had focus. */
export function showModal(dialog) {
  const trigger = typeof document !== "undefined" ? document.activeElement : null;
  try { dialog.showModal(); } catch { dialog.setAttribute("open", ""); }
  return trigger;
}

/** Give focus back to the element that opened the dialog, if it is still in the page. */
export function restoreFocus(trigger) {
  if (trigger && trigger.isConnected && typeof trigger.focus === "function") { try { trigger.focus(); } catch { /* ignore */ } }
}

/**
 * @param {{open: boolean, onClose?: () => void, focusRef?: {current: any}, onOpen?: () => void,
 *   onNativeClose?: () => void, deps?: any[]}} options `deps` re-run the open step while open.
 * @returns {{dialogRef: {current: any}, close: () => void}}
 */
export function useModalDialog({ open, onClose, focusRef, onOpen, onNativeClose, deps = [] }) {
  const dialogRef = useRef(null);
  const triggerRef = useRef(null);
  const latest = useRef(null);
  latest.current = { onClose, onOpen, onNativeClose };

  useEffect(() => {
    const d = dialogRef.current;
    if (!d) return;
    if (open) {
      if (!d.open) triggerRef.current = showModal(d);
      if (latest.current.onOpen) latest.current.onOpen();
      if (focusRef && focusRef.current) focusRef.current.focus({ preventScroll: true });
    } else if (d.open) {
      d.close();
    }
  }, [open, ...deps]);

  useEffect(() => {
    const d = dialogRef.current;
    if (!d) return undefined;
    const handle = () => {
      if (latest.current.onNativeClose) latest.current.onNativeClose();
      restoreFocus(triggerRef.current);
      if (latest.current.onClose) latest.current.onClose();
    };
    d.addEventListener("close", handle);
    return () => d.removeEventListener("close", handle);
  }, []);

  const close = () => { if (dialogRef.current && dialogRef.current.open) dialogRef.current.close(); };
  return { dialogRef, close };
}
