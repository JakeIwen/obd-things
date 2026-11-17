/**
 * The one request the Codes card makes to change the ignored list: a same-origin JSON POST whose
 * reply (the new list, or a refusal's `detail`) is all the card goes by. No optimistic guess.
 */

import { DTC_IGNORE, NETWORK_ERROR_TEXT, classifyReply } from "./dtcIgnore.helpers.js";
import { readReply } from "./jsonReply.js";

/**
 * @param {Function} fetchFn
 * @param {object} payload body with its `request_id`
 * @returns {Promise<{kind: "ok", state: object}|{kind: "error", message: string}>} never rejects
 */
export async function postIgnore(fetchFn, payload) {
  let response;
  try {
    response = await fetchFn(DTC_IGNORE.path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      cache: "no-store",
    });
  } catch {
    return { kind: "error", message: NETWORK_ERROR_TEXT };
  }
  try {
    const reply = await readReply(response);
    return classifyReply(response.status, reply.ok, reply.payload);
  } catch (error) {
    return { kind: "error", message: "The Pi sent no readable answer (" + (error && error.message) + "). Nothing is known to have changed." };
  }
}
