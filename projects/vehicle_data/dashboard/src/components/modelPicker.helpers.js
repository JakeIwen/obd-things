/**
 * Pure logic of `ModelPicker`: the catalog from GET /v1/assistant/models as listbox options grouped
 * by provider, effort choices that follow the chosen model, and the remembered choice. The
 * remembered choice is a per-browser convenience (localStorage, guarded), validated against the
 * catalog on every load, so a model that has gone away falls back to the catalog's default.
 *
 * Catalog: {groups: [{provider, label, available, reason, models: [{id, label, efforts,
 * default_effort}]}], default: {provider, model, effort}|null}. Choice: {provider, model, effort}
 * with `effort` null for a model that offers none.
 */

export const CHOICE_STORAGE_KEY = "van-dtc-research.choice";

const SEP = "\u001f";

const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : null);
const arr = (v) => (Array.isArray(v) ? v : []);

export const optionKey = (provider, model) => provider + SEP + model;

/** Groups that can run a model: available, with models. */
export function usableGroups(catalog) {
  return arr(obj(catalog)?.groups).filter((g) => obj(g) && g.available !== false && arr(g.models).length > 0);
}

/** Listbox options (one per model) in group order, each carrying its group's label. */
export function pickerOptions(catalog) {
  return usableGroups(catalog).flatMap((g) =>
    arr(g.models).map((m) => ({ key: optionKey(g.provider, m.id), label: m.label || m.id, group: g.label || g.provider })));
}

/** One note per group that cannot be used: `{label, reason}`; the models of such a group are absent. */
export function unavailableNotes(catalog) {
  return arr(obj(catalog)?.groups)
    .filter((g) => obj(g) && (g.available === false || arr(g.models).length === 0))
    .map((g) => ({ label: g.label || g.provider, reason: typeof g.reason === "string" && g.reason ? g.reason : "Not available right now" }));
}

export function findModel(catalog, provider, model) {
  const group = usableGroups(catalog).find((g) => g.provider === provider);
  return group ? arr(group.models).find((m) => m.id === model) || null : null;
}

/** The efforts a choice may use; empty means the model has none (send `effort: null`). */
export function effortsOf(catalog, choice) {
  const model = choice ? findModel(catalog, choice.provider, choice.model) : null;
  return model ? arr(model.efforts).filter((e) => typeof e === "string") : [];
}

function defaultEffort(model) {
  const efforts = arr(model.efforts);
  if (efforts.length === 0) return null;
  return efforts.includes(model.default_effort) ? model.default_effort : efforts[0];
}

/** Whether `choice` is a model and effort the catalog offers right now. */
export function validChoice(choice, catalog) {
  const c = obj(choice);
  const model = c ? findModel(catalog, c.provider, c.model) : null;
  if (!model) return false;
  const efforts = arr(model.efforts);
  return efforts.length === 0 ? c.effort === null : efforts.includes(c.effort);
}

/** The first usable choice in group order, or null. */
function firstChoice(catalog) {
  const group = usableGroups(catalog)[0];
  const model = group ? arr(group.models)[0] : null;
  return model ? { provider: group.provider, model: model.id, effort: defaultEffort(model) } : null;
}

/** The remembered choice when the catalog still offers it, else the catalog's default, else the first model. */
export function resolveChoice(saved, catalog) {
  if (validChoice(saved, catalog)) return { provider: saved.provider, model: saved.model, effort: saved.effort };
  const fallback = obj(catalog)?.default;
  return validChoice(fallback, catalog)
    ? { provider: fallback.provider, model: fallback.model, effort: fallback.effort }
    : firstChoice(catalog);
}

/** The choice after picking an option key: that model, with the effort kept when it offers it. */
export function chooseOption(catalog, choice, key) {
  for (const g of usableGroups(catalog)) {
    for (const m of arr(g.models)) {
      if (optionKey(g.provider, m.id) !== key) continue;
      const efforts = arr(m.efforts);
      const keep = choice && efforts.includes(choice.effort) ? choice.effort : defaultEffort(m);
      return { provider: g.provider, model: m.id, effort: efforts.length === 0 ? null : keep };
    }
  }
  return choice;
}

/** The choice with another effort, only when the model offers it. */
export function chooseEffort(catalog, choice, effort) {
  return choice && effortsOf(catalog, choice).includes(effort) ? { ...choice, effort } : choice;
}

/** `GPT-6-Astra · high`, or just the model name when it has no effort. */
export function choiceLabel(catalog, choice) {
  const model = choice ? findModel(catalog, choice.provider, choice.model) : null;
  if (!model) return "Choose a model";
  return (model.label || model.id) + (choice.effort ? " · " + choice.effort : "");
}

/* ---- the remembered choice (a per-browser convenience, never owner data) ---- */

export function loadChoice(backend = globalThis.localStorage) {
  try {
    const parsed = JSON.parse(backend?.getItem(CHOICE_STORAGE_KEY) || "null");
    const c = obj(parsed);
    return c && typeof c.provider === "string" && typeof c.model === "string"
      ? { provider: c.provider, model: c.model, effort: typeof c.effort === "string" ? c.effort : null }
      : null;
  } catch {
    return null;
  }
}

export function saveChoice(choice, backend = globalThis.localStorage) {
  try {
    backend?.setItem(CHOICE_STORAGE_KEY, JSON.stringify(choice));
  } catch {
    /* private mode or storage denied: the choice just is not remembered */
  }
}
