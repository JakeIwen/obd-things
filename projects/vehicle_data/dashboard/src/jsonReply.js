/**
 * A fetch reply whose body must be one JSON object, as the Pi's `/v1` routes answer (refusals
 * included). Node-testable: no Preact, no store, no DOM.
 */

/**
 * @param {{ok: boolean, status: number, json: () => Promise<any>}} response
 * @returns {Promise<{ok: boolean, payload: object}>} rejects with `unreadable reply` (a 2xx body
 *   that is not an object) or `HTTP <status>`
 */
export async function readReply(response) {
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error(response.ok ? "unreadable reply" : "HTTP " + response.status);
  }
  return { ok: response.ok, payload };
}
