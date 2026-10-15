import { computed, effect } from "@preact/signals";
import { liveValue, monoNow } from "./app/derive.js";
import { clock } from "./store.js";
import { IDLE_CHIME_STATE, isOverspeed, stepOverspeedChime } from "./overspeed.helpers.js";
import { enableChimeAudio, playWarningChime } from "./chime.js";

const speed = liveValue("vehicle.speed");
const limit = liveValue("vehicle.speed_limit");

/** True while the Speed and Speed-limit tiles are red; shared by both Drive pages. */
export const overspeed = computed(() => isOverspeed(speed.value, limit.value));

/**
 * Run the chime rule in the background for the whole app, whichever view is open. The store's
 * one-second clock re-runs it, so the five seconds elapse without any new frame.
 */
export function startOverspeedChime() {
  enableChimeAudio();
  let state = IDLE_CHIME_STATE;
  return effect(() => {
    void clock.value;
    const step = stepOverspeedChime(state, speed.value, limit.value, monoNow());
    state = step.state;
    if (step.chime) playWarningChime();
  });
}
