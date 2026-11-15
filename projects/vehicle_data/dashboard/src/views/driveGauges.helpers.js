import { DASH, fmtValue } from "../format.js";

/**
 * Drive-page VOLTAGE / ALT FIELD numeral, "13.6v/42%", as parts so the units can take the
 * tile's smaller grey unit style. The field duty is shown only when it can be co-temporal
 * with the voltage half: beside a live voltage it is the live duty; beside a held (old)
 * voltage it is a dash, so the held timestamp does not date a fresh field figure; with no
 * voltage at all a live duty still shows ("—/42%"). A dash half carries no unit.
 * @param {string} voltageText the tile's formatted battery voltage (DASH when none)
 * @param {"live"|"held"|"off"} kind the battery voltage tile's state
 * @param {number|null} duty live alternator field duty percent, or null when not live
 * @returns {{volts: string, voltUnit: string, sep: string, field: string, fieldUnit: string}}
 */
export function voltageFieldParts(voltageText, kind, duty) {
  const volts = kind === "off" ? DASH : voltageText;
  const field = kind === "held" || duty === null ? DASH : fmtValue("generator.field_duty", duty);
  if (volts === DASH && field === DASH) return { volts: DASH, voltUnit: "", sep: "", field: "", fieldUnit: "" };
  return {
    volts,
    voltUnit: volts === DASH ? "" : "v",
    sep: "/",
    field,
    fieldUnit: field === DASH ? "" : "%",
  };
}
