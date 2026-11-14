import { DASH, fmtValue } from "../format.js";

/**
 * Drive-page VOLTAGE/FIELD numeral: "13.6v/42%". The field duty is shown only when it can be
 * co-temporal with the voltage half: beside a live voltage it is the live duty; beside a held
 * (old) voltage it is a dash, so the held timestamp does not date a fresh field figure; with no
 * voltage at all a live duty still shows ("—/42%").
 * @param {string} voltageText the tile's formatted battery voltage (DASH when none)
 * @param {"live"|"held"|"off"} kind the battery voltage tile's state
 * @param {number|null} duty live alternator field duty percent, or null when not live
 */
export function voltageFieldText(voltageText, kind, duty) {
  const volts = kind === "off" ? DASH : voltageText;
  const field = kind === "held" || duty === null ? DASH : fmtValue("generator.field_duty", duty) + "%";
  return volts === DASH && field === DASH ? DASH : (volts === DASH ? DASH : volts + "v") + "/" + field;
}
