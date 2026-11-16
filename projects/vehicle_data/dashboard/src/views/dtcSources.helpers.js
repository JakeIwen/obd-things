import { parseDate } from "../format.js";

/** Plain group titles in display order. */
export const DTC_GROUPS = Object.freeze([
  ["current", "Current"],
  ["pending", "Pending"],
  ["confirmed_history", "Confirmed history"],
  ["incomplete_only", "Test not completed"],
  ["other", "Other status combinations"],
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

function vanRead(vm) {
  return {
    count: num(vm.dtc_count) !== null ? vm.dtc_count : arr(vm.dtcs).length,
    observed_at: vm.observed_at,
    scan_started_at: str(vm.scan_started_at) || vm.observed_at,
    status_availability_mask: vm.status_availability_mask ?? null,
  };
}

function statusByte(record) {
  const s = str(record.status);
  const n = s ? parseInt(s, 16) : NaN;
  return isFinite(n) ? n : null;
}

/**
 * Per module, keep whichever read is newer: the Pi's saved scan or the van's own health check
 * (`dtcs.in_vehicle_scan`, harvested passively from drive recordings). When the van's read is
 * newer, the Pi's records for that module inside the van's status mask are replaced by the van's
 * records; Pi records outside the mask (for example "test not completed" only) stay, because the
 * van's check cannot see them. Group totals are adjusted by what was replaced among the returned
 * records. Returns the input unchanged when there is no usable in-vehicle result.
 * @param {object} d `summary.dtcs`
 * @returns {object} a dtcs-shaped object with `van: {modules, lastAt}` added
 */
export function mergeVanScan(d) {
  const van = obj(d.in_vehicle_scan);
  if (!van || van.available !== true || !Array.isArray(van.modules) || !van.modules.length) return d;
  const piRows = moduleList(d);
  const piByKey = new Map();
  for (let i = 0; i < piRows.length; i += 1) piByKey.set(piRows[i].module_key, piRows[i]);
  const use = new Map();
  const reads = new Map();
  let lastAt = null;
  let scanLast = null;
  for (let i = 0; i < van.modules.length; i += 1) {
    const vm = obj(van.modules[i]);
    const key = vm ? str(vm.module_key) : null;
    const at = vm ? parseDate(vm.observed_at) : null;
    if (!key || !at) continue;
    reads.set(key, vm);
    const pi = piByKey.get(key);
    const piAt = pi ? parseDate(pi.last_success_at) : null;
    if (piAt && piAt.getTime() >= at.getTime()) continue;
    use.set(key, vm);
    if (!lastAt || at.getTime() > parseDate(lastAt).getTime()) lastAt = vm.observed_at;
    const scanAt = parseDate(vm.scan_started_at) ? vm.scan_started_at : vm.observed_at;
    if (!scanLast || parseDate(scanAt).getTime() > parseDate(scanLast).getTime()) scanLast = scanAt;
  }
  if (!reads.size) return d;

  const groups = obj(d.groups) || {};
  const counts = obj(d.group_counts) || {};
  const returnedCounts = obj(d.group_returned_counts) || {};
  const outGroups = {};
  const outCounts = {};
  const outReturned = {};
  for (let g = 0; g < DTC_GROUPS.length; g += 1) {
    const name = DTC_GROUPS[g][0];
    const entries = arr(groups[name]).filter(obj);
    const kept = entries.filter((e) => {
      if (!use.has(e.module_key)) return true;
      const status = statusByte(e);
      return status !== null && (status & VAN_STATUS_MASK) === 0;
    });
    const removed = entries.length - kept.length;
    outGroups[name] = kept;
    outCounts[name] = Math.max(0, (num(counts[name]) !== null ? counts[name] : entries.length) - removed);
    outReturned[name] = Math.max(0, (num(returnedCounts[name]) !== null ? returnedCounts[name] : entries.length) - removed);
  }
  use.forEach((vm, key) => {
    const records = arr(vm.dtcs).filter(obj);
    for (let i = 0; i < records.length; i += 1) {
      const r = records[i];
      const group = DTC_GROUPS.some((x) => x[0] === r.display_group) ? r.display_group : "other";
      outGroups[group].push({
        ...r,
        module_key: key,
        module_name: str(vm.module_name) || (piByKey.get(key) || {}).module_name || key,
        logical_bus: vm.logical_bus,
        last_seen_at: vm.observed_at,
        van_scan_at: str(vm.scan_started_at) || vm.observed_at,
        source: VAN_SOURCE,
      });
      outCounts[group] += 1;
      outReturned[group] += 1;
    }
  });
  const modules = piRows.map((m) => reads.has(m.module_key)
    ? { ...m, van_read: vanRead(reads.get(m.module_key)) } : m);
  reads.forEach((vm, key) => {
    if (!piByKey.has(key)) modules.push({
      module_key: key,
      module_name: str(vm.module_name) || key,
      logical_bus: vm.logical_bus,
      availability: "never_scanned",
      parked_module: false,
      van_read: vanRead(vm),
    });
  });
  return {
    ...d,
    groups: outGroups,
    group_counts: outCounts,
    group_returned_counts: outReturned,
    modules,
    van: use.size ? { modules: use.size, lastAt, scanAt: scanLast } : null,
  };
}

