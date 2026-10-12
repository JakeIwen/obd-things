/**
 * A dialog's controller (a plain object from its helpers file), created once per mounted dialog;
 * the dialog re-renders from every state the controller reports.
 */

import { useRef, useState } from "preact/hooks";

/**
 * @param {(onChange: (state: object) => void) => object} create builds the controller
 * @param {(controller: object) => object} read its state before the first report
 * @returns {[object, object]} the controller and its current state
 */
export function useController(create, read) {
  const ref = useRef(null);
  const [state, setState] = useState(null);
  if (ref.current === null) ref.current = create(setState);
  return [ref.current, state || read(ref.current)];
}
