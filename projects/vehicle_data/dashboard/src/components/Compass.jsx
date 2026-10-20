import { useComputed } from "@preact/signals";
import { obs } from "../app/derive.js";
import { connection } from "../store.js";
import { COMPASS, compassDisplay } from "../compass.js";

export function Compass() {
  const reading = obs(COMPASS.metric);
  const state = useComputed(() => compassDisplay(reading.value, connection.value.state));
  const letters = useComputed(() => state.value.letters);
  const className = useComputed(() => "compass" + (state.value.stale ? " compass--stale" : ""));
  const label = useComputed(() => state.value.letters
    ? `Compass: ${state.value.letters}${state.value.stale ? " (last reported)" : ""}`
    : "Compass: waiting for a direction");
  return <span class={className} role="img" aria-label={label} title={label}>{letters}</span>;
}
