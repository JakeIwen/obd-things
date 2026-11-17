/**
 * Pure pieces of the Codes card that sit between the saved list and its groups: the module filter
 * (which modules have codes, narrowing a `dtcs` model to the chosen ones) and the Ignored group.
 * Node-testable: no Preact, no store, no DOM. `health.helpers.js` composes them in `codesView`.
 */

import { DTC_GROUPS, obj, arr, num, str, moduleList } from "./dtcSources.helpers.js";
import { fmtTime } from "../format.js";
import { identity, ignoredLabel } from "../dtcIgnore.helpers.js";

/**
 * Modules that have codes in a model, with their counts. An ignored code keeps its module in the
 * list (the filter applies to the Ignored group too) and is counted apart.
 * @param {object} d model with `groups` (ignored records already removed)
 * @param {Array<{moduleKey: string}>} ignored ignore entries
 * @returns {Array<{key: string, label: string, count: number, detail: string|null}>} by name
 */
export function moduleOptions(d, ignored) {
  const names = new Map(moduleList(d).map((m) => [m.module_key, str(m.module_name) || m.module_key]));
  const rows = new Map();
  const row = (key, name) => {
    if (!rows.has(key)) rows.set(key, { key, label: names.get(key) || name || key, count: 0, ignored: 0 });
    return rows.get(key);
  };
  const groups = obj(d.groups) || {};
  for (const [name] of DTC_GROUPS) {
    for (const r of arr(groups[name]).filter(obj)) if (str(r.module_key)) row(r.module_key, str(r.module_name)).count += 1;
  }
  for (const e of ignored) row(e.moduleKey, null).ignored += 1;
  return [...rows.values()]
    .sort((a, b) => a.label.localeCompare(b.label))
    .map((r) => ({ key: r.key, label: r.label, count: r.count, detail: r.ignored > 0 ? r.ignored + " ignored" : null }));
}

/** The chosen keys that still have codes, in option order; none, or every module, means no filter. */
export function activeModules(chosen, options) {
  const want = new Set(chosen);
  const keys = options.filter((o) => want.has(o.key)).map((o) => o.key);
  return keys.length === options.length ? [] : keys;
}

/**
 * `d` narrowed to the chosen modules. Group counts become the filtered count of the returned
 * records; records the Pi did not return (a truncated group, parked records a filtered read did not
 * return) have no known module, so they are neither counted nor filtered and `filter_unreturned`
 * says how many each group leaves out.
 * @param {object} d merged model
 * @param {string[]} keys chosen module keys; none returns `d` itself
 */
export function filterByModules(d, keys) {
  if (!keys.length) return d;
  const chosen = new Set(keys);
  const groups = {};
  const counts = {};
  const unreturned = {};
  const hiddenIn = obj(d.group_hidden_counts);
  for (const [name] of DTC_GROUPS) {
    const list = arr(d.groups?.[name]).filter(obj);
    const kept = list.filter((r) => chosen.has(r.module_key));
    const total = num(d.group_counts?.[name]) ?? list.length;
    groups[name] = kept;
    counts[name] = kept.length;
    unreturned[name] = Math.max(0, total - list.length) + (hiddenIn ? num(hiddenIn[name]) || 0 : 0);
  }
  return {
    ...d,
    groups,
    group_counts: counts,
    group_returned_counts: { ...counts },
    ...(hiddenIn ? { group_hidden_counts: Object.fromEntries(DTC_GROUPS.map(([name]) => [name, 0])) } : {}),
    filter_unreturned: unreturned,
  };
}

/** Note under a filtered group that left unreturned records out of its count. */
export function unreturnedNote(count) {
  return count > 0
    ? "Filtered from the codes the Pi returned; " + count + (count === 1 ? " more was" : " more were") + " not returned and not filtered."
    : null;
}

/**
 * The Ignored group: one row per ignore entry in the chosen modules, newest first. A row shows the
 * saved record's meaning when the code is in the list now, and says so when it is not.
 * @param {Array} entries `ignoreState().entries`
 * @param {Map<string, object>} found removed records by identity (`excludeIgnored`)
 * @param {string[]} keys chosen module keys (none: all)
 * @param {(record: object, nowMs: number) => object} rowFor `codeRow`
 */
export function ignoredGroup(entries, found, keys, nowMs, rowFor) {
  const chosen = keys.length ? new Set(keys) : null;
  const rows = [];
  for (const entry of entries) {
    if (chosen && !chosen.has(entry.moduleKey)) continue;
    const id = identity(entry.moduleKey, entry.code);
    const record = found.get(id) || null;
    const base = record
      ? rowFor(record, nowMs)
      : { key: "", code: entry.code, meaning: "not in the saved list now", reviewed: false, meta: null, moduleKey: entry.moduleKey,
        fca: entry.code, id, details: [], extra: null };
    const moduleName = record ? str(record.module_name) || entry.moduleKey : entry.moduleKey;
    rows.push({
      ...base,
      key: "ignored:" + entry.moduleKey + ":" + entry.code,
      id,
      moduleKey: entry.moduleKey,
      fca: entry.code,
      ignored: true,
      note: entry.note,
      meta: [moduleName, ignoredLabel(entry, (iso) => fmtTime(iso, new Date(nowMs)))].filter(Boolean).join(" · "),
    });
  }
  return {
    key: "ignored",
    title: "Ignored",
    total: rows.length,
    summary: "Ignored (" + rows.length + ")",
    rows,
  };
}
