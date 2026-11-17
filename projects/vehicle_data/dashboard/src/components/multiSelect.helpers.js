/**
 * Pure logic of `MultiSelect` and `Listbox` (selection, trigger text, keyboard movement), so the
 * components stay wiring and the rules are testable in node. An option is
 * `{key, label, count?, detail?, group?}`; options sharing a `group` are listed together.
 */

/**
 * Selection after toggling `key`, in option order (stable for display and equality). A result that
 * names every option is the no-filter state `[]`, never a snapshot of today's keys: a module that
 * gains codes later must not be left out of an "all" the owner never narrowed.
 */
export function toggleKey(selected, key, options) {
  const on = new Set(selected);
  if (on.has(key)) on.delete(key);
  else on.add(key);
  const keys = options.filter((o) => on.has(o.key)).map((o) => o.key);
  return keys.length === options.length ? [] : keys;
}

/** Every option's key. */
export function allKeys(options) {
  return options.map((o) => o.key);
}

/** The selection that means "no filter" (all modules, including ones that appear later). */
export function clearKeys() {
  return [];
}

/** The selected keys that are still options, in option order; the rest are dropped. */
export function validKeys(selected, options) {
  const on = new Set(selected);
  return options.filter((o) => on.has(o.key)).map((o) => o.key);
}

/** `All modules`, the one name, or `3 modules`. Choosing every option reads as all. */
export function triggerText(selected, options, noun = "module", allText = "All " + noun + "s") {
  const keys = validKeys(selected, options);
  if (keys.length === 0 || keys.length === options.length) return allText;
  if (keys.length === 1) return options.find((o) => o.key === keys[0]).label;
  return keys.length + " " + noun + "s";
}

/** Chips for the chosen options (none when the selection means all). */
export function chipsOf(selected, options) {
  const keys = validKeys(selected, options);
  if (keys.length === 0 || keys.length === options.length) return [];
  const on = new Set(keys);
  const chosen = options.filter((o) => on.has(o.key));
  const short = (label) => label.replace(/\s*\([^()]*\)\s*$/, "") || label;
  const counts = new Map();
  for (const o of chosen) counts.set(short(o.label), (counts.get(short(o.label)) || 0) + 1);
  // Chips drop a trailing parenthetical; two that would read alike keep their full names.
  return chosen.map((o) => ({ key: o.key, label: o.label, text: counts.get(short(o.label)) > 1 ? o.label : short(o.label) }));
}

/** What a key press means in a listbox, or null when it is not the listbox's. */
export function listboxKeyAction(key) {
  switch (key) {
    case "ArrowDown": return "next";
    case "ArrowUp": return "prev";
    case "Home": return "first";
    case "End": return "last";
    case " ":
    case "Spacebar":
    case "Enter": return "toggle";
    default: return null;
  }
}

/** The active index after a movement action; it stops at the ends instead of wrapping. */
export function moveActive(index, action, count) {
  if (count <= 0) return -1;
  const at = Math.min(Math.max(index, 0), count - 1);
  if (action === "next") return Math.min(at + 1, count - 1);
  if (action === "prev") return Math.max(at - 1, 0);
  if (action === "first") return 0;
  if (action === "last") return count - 1;
  return at;
}

/** Index to start on when a listbox opens: the first selected option, else the first. */
export function initialActive(selected, options) {
  const on = new Set(selected);
  const i = options.findIndex((o) => on.has(o.key));
  return options.length === 0 ? -1 : i < 0 ? 0 : i;
}

/** True when the option at `index` starts a new group heading (options are listed in group order). */
export function startsGroup(options, index) {
  const group = options[index].group;
  return Boolean(group) && (index === 0 || options[index - 1].group !== group);
}
