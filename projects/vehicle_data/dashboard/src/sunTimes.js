import { DAY_MS, MINUTE_MS, SUN_REFERENCE } from "./theme.config.js";

const radians = (degrees) => degrees * Math.PI / 180;
const degrees = (radians) => radians * 180 / Math.PI;
const mountainDate = new Intl.DateTimeFormat("en-US", {
  timeZone: SUN_REFERENCE.timeZone, year: "numeric", month: "numeric", day: "numeric",
});

/** UTC midnight used only as a calendar-date anchor, not as Mountain midnight. */
export function mountainDay(now) {
  const parts = Object.fromEntries(mountainDate.formatToParts(now).map(({ type, value }) => [type, value]));
  return Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day));
}

// NOAA's Meeus equations: https://gml.noaa.gov/grad/solcalc/calcdetails.html
// Keep the epoch-based UTC result unwrapped: Denver sunset is often on the NEXT UTC day.
function solarPosition(julianDay) {
  const t = (julianDay - 2451545) / 36525;
  const longitude = radians((280.46646 + t * (36000.76983 + t * 0.0003032)) % 360);
  const anomaly = radians(357.52911 + t * (35999.05029 - 0.0001537 * t));
  const eccentricity = 0.016708634 - t * (0.000042037 + 0.0000001267 * t);
  const center = Math.sin(anomaly) * (1.914602 - t * (0.004817 + 0.000014 * t))
    + Math.sin(2 * anomaly) * (0.019993 - 0.000101 * t) + Math.sin(3 * anomaly) * 0.000289;
  const omega = radians(125.04 - 1934.136 * t);
  const apparent = longitude + radians(center - 0.00569 - 0.00478 * Math.sin(omega));
  const seconds = 21.448 - t * (46.815 + t * (0.00059 - t * 0.001813));
  const obliquity = radians(23 + (26 + seconds / 60) / 60 + 0.00256 * Math.cos(omega));
  const y = Math.tan(obliquity / 2) ** 2;
  const equation = y * Math.sin(2 * longitude) - 2 * eccentricity * Math.sin(anomaly)
    + 4 * eccentricity * y * Math.sin(anomaly) * Math.cos(2 * longitude)
    - 0.5 * y * y * Math.sin(4 * longitude) - 1.25 * eccentricity ** 2 * Math.sin(2 * anomaly);
  return { declination: Math.asin(Math.sin(obliquity) * Math.sin(apparent)), equation: 4 * degrees(equation) };
}

function eventMinutes(julianDay, rising) {
  const { declination, equation } = solarPosition(julianDay);
  const latitude = radians(SUN_REFERENCE.latitude);
  const hourAngle = degrees(Math.acos(Math.cos(radians(SUN_REFERENCE.zenith))
    / (Math.cos(latitude) * Math.cos(declination)) - Math.tan(latitude) * Math.tan(declination)));
  return 720 - 4 * (SUN_REFERENCE.longitude + (rising ? hourAngle : -hourAngle)) - equation;
}

/** Sunrise and sunset as epoch milliseconds for the calendar-date anchor from mountainDay. */
export function sunTimes(day) {
  const julianDay = day / DAY_MS + 2440587.5;
  const event = (rising) => {
    const first = eventMinutes(julianDay, rising);
    return Math.round(day + eventMinutes(julianDay + first / (DAY_MS / MINUTE_MS), rising) * MINUTE_MS);
  };
  return { sunrise: event(true), sunset: event(false) };
}
