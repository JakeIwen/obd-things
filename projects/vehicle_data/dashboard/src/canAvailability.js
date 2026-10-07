import vocabulary from "../../can_availability.json" with { type: "json" };

export const CAN_AVAILABILITY = Object.freeze(vocabulary);

const UNAVAILABLE_CAN_BASES = new Set([
  CAN_AVAILABILITY.missing_basis,
  CAN_AVAILABILITY.recovering_basis,
  CAN_AVAILABILITY.initializing_basis,
]);

export const isUnavailableCanBasis = (basis) => UNAVAILABLE_CAN_BASES.has(basis);
export const isUnavailableCanState = (vehicle) => Boolean(
  vehicle && vehicle.state === CAN_AVAILABILITY.state && vehicle.confidence === CAN_AVAILABILITY.confidence &&
  isUnavailableCanBasis(vehicle.basis),
);

export function canAvailabilityLabel(vehicle) {
  if (!isUnavailableCanState(vehicle)) return "";
  const text = typeof vehicle.detail === "string" ? vehicle.detail : "";
  const separator = text.indexOf("; ");
  if (separator >= 0) return text.slice(0, separator);
  if (vehicle.basis === CAN_AVAILABILITY.recovering_basis) return "CAN adapters returning";
  if (vehicle.basis === CAN_AVAILABILITY.initializing_basis) return "Checking CAN adapters";
  return "CAN adapters missing";
}

export function canAvailabilityDetail(vehicle) {
  if (!isUnavailableCanState(vehicle)) return "";
  if (vehicle.basis === CAN_AVAILABILITY.missing_basis) return "vehicle state unknown";
  const text = typeof vehicle.detail === "string" ? vehicle.detail : "";
  const separator = text.indexOf("; ");
  return separator >= 0 ? text.slice(separator + 2) : "";
}

export function canAvailabilityLine(vehicle) {
  const label = canAvailabilityLabel(vehicle);
  if (!label) return "";
  const detail = canAvailabilityDetail(vehicle);
  return detail ? label + " · " + detail : label;
}
