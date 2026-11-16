import { parseDate } from "../format.js";
import contract from "../../../drive_dtc.json" with { type: "json" };
import { SUN_REFERENCE } from "../theme.config.js";

/** Plain group titles in display order. */
export const DTC_GROUPS = Object.freeze([
  ["current", "Current"],
  ["pending", "Pending"],
  ["confirmed_history", "Confirmed history"],
  ["incomplete_only", "Test not completed"],
  ["other", "Other status combinations"],
  ["not_reported", "Not reported by a later filtered read"],
]);

/** `source` marker on records and module rows taken from the van's own health check. */
export const VAN_SOURCE = "van";

/** Status bits the van's check asks for (test failed, pending, confirmed): mask 0D. */
export const VAN_STATUS_MASK = 0x0d;

export function obj(v) {
  return v && typeof v === "object" && !Array.isArray(v) ? v : null;
}

export function arr(v) {
  return Array.isArray(v) ? v : [];
}

export function num(v) {
  return typeof v === "number" && isFinite(v) ? v : null;
}

export function str(v) {
  return typeof v === "string" && v.length > 0 ? v : null;
}

export function moduleList(dtcs) {
  const modules = dtcs.modules;
  if (Array.isArray(modules)) return modules.filter(obj);
  const o = obj(modules);
  return o ? Object.keys(o).map((k) => o[k]).filter(obj) : [];
}

export const DRIVE_SOURCE = "drive";
export const OUTCOME = Object.freeze(Object.fromEntries(Object.keys(contract.outcomes).map((key) => [key, key])));
const DRIVE_STATUS_MASK = parseInt(contract.request.split(" ").pop(), 16);
const COVERAGE = Object.freeze({
  van: "failed, pending or confirmed codes only",
  drive: contract.coverage.replace(/ \(.*\)$/, ""),
});
const mountain = new Intl.DateTimeFormat("en-US", {
  timeZone: SUN_REFERENCE.timeZone, year: "numeric", month: "short", day: "numeric",
  hour: "numeric", minute: "2-digit", timeZoneName: "short",
});

/** Filtered evidence always carries an explicit date and Mountain zone, even today. */
export function filteredTime(iso) {
  const at = parseDate(iso);
  return at ? mountain.format(at) : "unknown time";
}

export function filteredSourceLine(source, iso) {
  return (source === VAN_SOURCE ? "from the van’s own health check" : "from the Pi’s drive read") +
    " · " + COVERAGE[source] + " · " + filteredTime(iso);
}

function stamp(value) {
  const at = parseDate(value);
  return at ? at.getTime() : -Infinity;
}

function statusByte(value) {
  return typeof value === "string" && /^[0-9a-f]{2}$/i.test(value) ? parseInt(value, 16) : 0;
}

function requestMask(read) {
  return read.source === VAN_SOURCE ? VAN_STATUS_MASK : DRIVE_STATUS_MASK;
}

function effectiveMask(read) {
  return requestMask(read) & statusByte(read.status_availability_mask);
}

/**
 * Whether an empty filtered read may say "no <coverage> codes": the module returned availability
 * for every requested status bit. A null, zero or partial availability mask never can.
 */
export function fullCoverage(read) {
  return effectiveMask(read) === requestMask(read);
}

function latest(reads) {
  return reads.reduce((best, read) => !best || stamp(read.observed_at) > stamp(best.observed_at) ? read : best, null);
}

function sourceReads(d) {
  const byKey = new Map();
  function add(module, read) {
    if (!str(module.module_key) || stamp(read.observed_at) === -Infinity) return;
    if (!byKey.has(module.module_key)) byKey.set(module.module_key, { module, reads: [] });
    byKey.get(module.module_key).reads.push(read);
  }
  const van = obj(d.in_vehicle_scan);
  if (van?.available === true) arr(van.modules).filter(obj).forEach((m) => add(m, {
    ...m, count: num(m.dtc_count) ?? arr(m.dtcs).length, source: VAN_SOURCE, outcome: OUTCOME.complete,
  }));
  const drive = obj(d.drive_reads);
  if (drive?.available === true) arr(drive.modules).filter(obj).forEach((m) => {
    arr(m.reads).filter(obj).forEach((r) => add(m, { ...r, source: DRIVE_SOURCE }));
  });
  return byKey;
}

function selectedReads(reads) {
  const van = latest(reads.filter((r) => r.source === VAN_SOURCE));
  const drive = reads.filter((r) => r.source === DRIVE_SOURCE);
  const complete = latest(drive.filter((r) => r.outcome === OUTCOME.complete));
  const truncated = latest(drive.filter((r) => r.outcome === OUTCOME.truncated));
  const success = latest([complete, truncated].filter(Boolean));
  const failure = latest(drive.filter((r) => ![OUTCOME.complete, OUTCOME.truncated].includes(r.outcome)));
  return { van, complete,
    truncated: truncated && (!complete || stamp(truncated.observed_at) > stamp(complete.observed_at)) ? truncated : null,
    failure: failure && (!success || stamp(failure.observed_at) > stamp(success.observed_at)) ? failure : null };
}

function recordGroup(record) {
  if (record.observation_state === "not_reported_by_source") return "not_reported";
  return DTC_GROUPS.some(([key]) => key === record.display_group) ? record.display_group : "other";
}

function applyRead(records, read, module) {
  const listed = new Map(arr(read.dtcs).filter(obj).map((r) => [r.raw_dtc, r]));
  records.forEach((r, key) => {
    if (stamp(r.last_seen_at) >= stamp(read.observed_at)) { listed.delete(key); return; }
    if (listed.has(key)) return;
    if ((statusByte(r.status) & effectiveMask(read)) !== 0) records.set(key, {
      ...r, observation_state: "not_reported_by_source", not_reported_source: read.source,
      not_reported_at: read.observed_at,
    });
  });
  listed.forEach((record, key) => records.set(key, {
    ...record, module_key: module.module_key, module_name: module.module_name || module.module_key,
    logical_bus: module.logical_bus, last_seen_at: read.observed_at,
    observation_state: "observed_in_filtered_read", source: read.source,
  }));
}

/** Three-source merge. Parked module facts and hidden parked group counts never change. */
export function mergeVanScan(d) {
  const sources = sourceReads(d);
  if (!sources.size) return d;
  const piRows = moduleList(d);
  const modules = new Map(piRows.map((m) => [m.module_key, { ...m }]));
  const groups = Object.fromEntries(DTC_GROUPS.map(([name]) => [name, arr(d.groups?.[name]).filter(obj)]));
  const hidden = Object.fromEntries(DTC_GROUPS.map(([name]) => [name,
    Math.max(0, (num(d.group_counts?.[name]) ?? groups[name].length) - groups[name].length)]));
  let vanCount = 0;
  let vanAt = null;
  sources.forEach(({ module, reads }, key) => {
    const parked = modules.get(key);
    const m = parked || { module_key: key, module_name: module.module_name || key,
      logical_bus: module.logical_bus, availability: "never_scanned", parked_module: false };
    const selected = selectedReads(reads);
    if (selected.van) {
      m.van_read = selected.van;
      vanCount += 1;
      if (stamp(selected.van.observed_at) > stamp(vanAt)) vanAt = selected.van.observed_at;
    }
    if (selected.complete) m.drive_read = selected.complete;
    if (selected.truncated) m.drive_truncated = selected.truncated;
    if (selected.failure) m.drive_failure = selected.failure;
    modules.set(key, m);
    const records = new Map();
    DTC_GROUPS.forEach(([group]) => {
      groups[group].filter((r) => r.module_key === key).forEach((r) => records.set(r.raw_dtc, { ...r, display_group: group }));
      groups[group] = groups[group].filter((r) => r.module_key !== key);
    });
    [selected.van, selected.complete].filter(Boolean)
      .sort((a, b) => stamp(a.observed_at) - stamp(b.observed_at))
      .forEach((read) => applyRead(records, read, m));
    if (selected.truncated) {
      const shown = [...records.values()].filter((r) =>
        (statusByte(r.status) & effectiveMask(selected.truncated)) !== 0).length;
      m.drive_truncated = { ...selected.truncated, count_mismatch: shown !== selected.truncated.count };
    }
    records.forEach((r) => groups[recordGroup(r)].push(r));
  });
  return { ...d, groups, modules: [...modules.values()],
    group_counts: Object.fromEntries(DTC_GROUPS.map(([name]) => [name, groups[name].length + hidden[name]])),
    group_returned_counts: Object.fromEntries(DTC_GROUPS.map(([name]) => [name, groups[name].length])),
    van: vanCount ? { modules: vanCount, lastAt: vanAt, scanAt: vanAt } : null };
}

function returnedCoverageNote(read) {
  if (fullCoverage(read)) return "";
  return effectiveMask(read) === 0
    ? " · no returned status coverage; older codes cannot be ruled out"
    : " · limited returned status coverage; only covered older statuses can be ruled out";
}

export function filteredReadText(read) {
  const coverage = read.source === VAN_SOURCE ? "failed, pending or confirmed" : "failed, pending, confirmed or warning-lamp";
  const result = read.count === 0 ? (fullCoverage(read) ? "no " + coverage + " codes" : "zero codes reported") :
    read.count + (read.count === 1 ? " code reported" : " codes reported");
  return result + " · " + filteredSourceLine(read.source, read.observed_at) + returnedCoverageNote(read);
}

export function driveReadLines(module) {
  const lines = [];
  if (module.drive_read) lines.push(filteredReadText(module.drive_read));
  const read = module.drive_truncated;
  if (read) lines.push(read.count + " codes reported, list not retrieved; first " +
    (read.first_code?.fca_display || read.first_code?.raw_dtc || "unknown") + " · " +
    filteredSourceLine(DRIVE_SOURCE, read.observed_at) + returnedCoverageNote(read) +
    (read.count_mismatch ? " · count differs from the codes shown" : ""));
  const failure = module.drive_failure;
  if (failure) lines.push((failure.outcome === OUTCOME.not_sent ? "drive read not sent (" : "drive read failed (") +
    String(failure.reason || failure.outcome).replace(/_/g, " ") +
    ") · " + filteredSourceLine(DRIVE_SOURCE, failure.observed_at));
  return lines;
}

/**
 * The newest successful read behind a merged model (`mergeVanScan` output): the parked cache's last
 * success, a module's van read, or its latest complete or truncated drive read. Failures never count.
 */
export function newestSuccess(d) {
  const stamps = [obj(d.coverage)?.last_success_at, obj(d.van)?.lastAt];
  moduleList(d).forEach((m) => stamps.push(m.drive_read?.observed_at, m.drive_truncated?.observed_at));
  return stamps.filter((s) => stamp(s) > -Infinity).reduce((best, s) => (!best || stamp(s) > stamp(best) ? s : best), null);
}

/**
 * Codes a module reported after its last parked success that no list shows: a truncated drive
 * read gives only a count. Any such report rules out an overall "no codes" conclusion.
 */
export function unlistedReports(d) {
  return moduleList(d)
    .filter((m) => m.drive_truncated && num(m.drive_truncated.count) > 0 &&
      stamp(m.drive_truncated.observed_at) > stamp(m.last_success_at))
    .map((m) => ({ name: str(m.module_name) || m.module_key, count: m.drive_truncated.count,
      at: m.drive_truncated.observed_at }));
}
