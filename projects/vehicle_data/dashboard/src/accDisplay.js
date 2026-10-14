import { computed } from "@preact/signals";
import { connection, metricSignal } from "./store.js";
import { ACC_METRICS, accDisplayModel } from "./accDisplay.helpers.js";

/** Shared by the two lazy Drive pages so their state, speed and gap agree. */
export const currentAcc = computed(() => {
  const records = {};
  for (const name of Object.values(ACC_METRICS)) records[name] = metricSignal(name).value;
  return accDisplayModel(records, connection.value.state);
});
