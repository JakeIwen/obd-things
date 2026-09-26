/**
 * Four-wheel tire pressure grid (FL FR / RL RR). Each wheel is coloured
 * against its own baseline via bands.js; one shared time stamp is shown when
 * the readings are held (parked).
 */

import { useComputed } from "@preact/signals";
import { tileModel } from "../app/derive.js";

export const WHEELS = [
  ["tire.pressure.fl", "FL"],
  ["tire.pressure.fr", "FR"],
  ["tire.pressure.rl", "RL"],
  ["tire.pressure.rr", "RR"],
];

function Wheel({ name, pos }) {
  const m = tileModel(name);
  const cls = useComputed(() => {
    let c = "tire tire--" + m.kind.value;
    const g = m.geometry.value;
    const state = g ? g.state : "none";
    if (m.kind.value !== "off" && (state === "red" || state === "amber")) c += " tire--" + state;
    const alert = m.colour.value;
    if (alert === "red") c += " tire--red";
    return c;
  });
  return (
    <div class={cls}>
      <span class="tire__pos">{pos}</span>
      <span class="tire__value num">
        {m.valueText}
        <span class="tire__unit">psi</span>
      </span>
    </div>
  );
}

/**
 * @param {{area?: string, label?: string, withFooter?: boolean}} props
 */
export function TireGrid({ area, label = "Tires", withFooter = true }) {
  const models = WHEELS.map(([name]) => tileModel(name));
  const footer = useComputed(() => {
    for (let i = 0; i < models.length; i += 1) {
      const held = models[i].heldText.value;
      if (held) return "last read " + held;
    }
    const anyLive = models.some((m) => m.kind.value === "live");
    return anyLive ? "" : "read while the engine runs";
  });
  const cls = area ? "tile area-" + area : "tile";
  return (
    <section class={cls} aria-label={label}>
      <div class="tile__label">{label}</div>
      <div class="tires">
        {WHEELS.map(([name, pos]) => (
          <Wheel key={name} name={name} pos={pos} />
        ))}
      </div>
      {withFooter ? <div class="tile__sub">{footer}</div> : null}
    </section>
  );
}
