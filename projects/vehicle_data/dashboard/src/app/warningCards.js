/** Lazily loaded warning prose for the Parked and Health views. */

import { computed } from "@preact/signals";
import * as store from "../store.js";
import { buildWarningCards } from "../warnings.js";

export const warningCards = computed(() => buildWarningCards(store.summary.health.value, Date.now()));
