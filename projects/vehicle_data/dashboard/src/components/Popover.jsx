/**
 * Panel that opens under its trigger and closes on Escape, on a tap outside it, or when focus
 * leaves it. It sits in the page flow (the cards clip overflow, so an overlay would be cut off) and
 * spans its container, so it stays inside the viewport at any width. Focus moves into it on open
 * (the element marked `data-autofocus`, else the panel) and returns to the trigger on Escape.
 * `MultiSelect` is the first user; a grouped single-select model picker builds on the same pieces.
 *
 * @param {{open: boolean, onClose: () => void, hostRef: {current: Element|null},
 *   label: string, class?: string, children?: any}} props `hostRef` is the element that holds the
 *   trigger and this panel; a tap inside it is not "outside" (the trigger toggles itself)
 */

import { useEffect, useRef } from "preact/hooks";

export default function Popover({ open, onClose, hostRef, label, class: cls, children }) {
  const panel = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const target = panel.current && (panel.current.querySelector("[data-autofocus]") || panel.current);
    if (target) target.focus({ preventScroll: true });
    const outside = (event) => {
      const host = hostRef.current;
      if (host && !host.contains(event.target)) onClose();
    };
    document.addEventListener("pointerdown", outside, true);
    return () => document.removeEventListener("pointerdown", outside, true);
  }, [open]);
  if (!open) return null;
  const onKeyDown = (event) => {
    if (event.key !== "Escape") return;
    event.stopPropagation();
    onClose();
    const trigger = hostRef.current && hostRef.current.querySelector("[aria-haspopup]");
    if (trigger) trigger.focus();
  };
  const onFocusOut = (event) => {
    const next = event.relatedTarget;
    if (next && panel.current && !panel.current.contains(next) && !(hostRef.current && hostRef.current.contains(next))) onClose();
  };
  return (
    <div ref={panel} class={"popover" + (cls ? " " + cls : "")} role="dialog" aria-label={label} tabIndex={-1}
      onKeyDown={onKeyDown} onFocusOut={onFocusOut}>
      {children}
    </div>
  );
}
