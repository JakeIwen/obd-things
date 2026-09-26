/**
 * Pure helpers for the per-device customiser (dashboard v2, design 3.5 "Customise this device").
 *
 * The old layout editor (static/profiles.js + app.js renderLayoutEditor/editLayout) let the owner
 * show or hide tiles, move rows up or down, choose widths, pair half tiles, swap a pair and reset.
 * Widths, pairing and swapping are replaced by the fixed per-view grids; show/hide, move up/down
 * and reset survive here, per view, on top of the settings object from `settings.js`:
 *   settings.hidden = { [view]: [cardId, ...] }   cards the owner hid on this device
 *   settings.order  = { [view]: [cardId, ...] }   the owner's card order for the view
 * Every function is pure: it never mutates its input and returns the same `settings` object when
 * nothing would change, so a caller can compare identity to skip a save.
 */

/** Titles for the view being customised. */
export const VIEW_LABELS = Object.freeze({
  drive: "Drive",
  parked: "Parked",
  health: "Health",
  history: "History",
  system: "System",
});

/** On-screen copy; kept here so the component is a thin renderer. */
export const TEXT = Object.freeze({
  eyebrow: "Customise this device",
  intro: "Show or hide cards and move them up or down. Changes stay in this browser and never change the broker or vehicle.",
  introFixed: "Show or hide tiles; each tile keeps its place on the screen. Changes stay in this browser and never change the broker or vehicle.",
  visibleTitleFixed: "Tiles on this screen",
  hiddenTitleFixed: "Hidden tiles",
  noteDefault: "Default layout · nothing changed on this device.",
  noteCustom: "Saved on this device · rows below match the view.",
  visibleTitle: "Cards in this view",
  hiddenTitle: "Hidden cards",
  allHidden: "Every card is hidden. Tick a card below to show it again.",
  noCards: "This view has no customisable cards.",
  reset: "Reset layout",
  done: "Done",
  moveUp: "Move up",
  moveDown: "Move down",
});

/** `Customise Drive`; unknown views fall back to the raw id. */
export function viewTitle(view) {
  const label = VIEW_LABELS[view] || (typeof view === "string" && view ? view : "view");
  return "Customise " + label;
}

/**
 * Valid card definitions in the order the view renders them by default. Entries without a string
 * id, and duplicate ids, are dropped; a missing label falls back to the id.
 * @param {Array<{id:string,label?:string}>} cards
 * @returns {Array<{id:string,label:string}>}
 */
export function cardList(cards) {
  const out = [];
  const seen = new Set();
  if (!Array.isArray(cards)) return out;
  for (const card of cards) {
    const id = card && typeof card.id === "string" ? card.id : "";
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const label = card.label !== undefined && card.label !== null && String(card.label) !== "" ? String(card.label) : id;
    out.push({ id, label });
  }
  return out;
}

function listFor(settings, key, view) {
  const table = settings && typeof settings === "object" ? settings[key] : null;
  const list = table && typeof table === "object" && !Array.isArray(table) ? table[view] : null;
  return Array.isArray(list) ? list.filter((id) => typeof id === "string" && id !== "") : [];
}

/** Ids the owner hid for `view`, as a Set (unknown ids are kept; they are harmless and dropped on save). */
export function hiddenSet(view, settings) {
  return new Set(listFor(settings, "hidden", view));
}

/**
 * The cards of `view` in the owner's order with their hidden flag: ids named in
 * `settings.order[view]` first (only those the view still has), then the remaining cards in the
 * view's default order. Hidden cards keep their position so re-showing one puts it back.
 * @param {string} view
 * @param {Array<{id:string,label?:string}>} cards default order
 * @param {object|null|undefined} settings settings object from settings.js
 * @returns {Array<{id:string,label:string,hidden:boolean}>}
 */
export function orderedCards(view, cards, settings) {
  const defaults = cardList(cards);
  const byId = new Map(defaults.map((card) => [card.id, card]));
  const hidden = hiddenSet(view, settings);
  const out = [];
  const placed = new Set();
  for (const id of listFor(settings, "order", view)) {
    const card = byId.get(id);
    if (!card || placed.has(id)) continue;
    placed.add(id);
    out.push({ id, label: card.label, hidden: hidden.has(id) });
  }
  for (const card of defaults) {
    if (placed.has(card.id)) continue;
    out.push({ id: card.id, label: card.label, hidden: hidden.has(card.id) });
  }
  return out;
}

/** Ids of the cards the view should render, in order (hidden cards removed). */
export function visibleIds(view, cards, settings) {
  return orderedCards(view, cards, settings).filter((row) => !row.hidden).map((row) => row.id);
}

function withList(settings, key, view, list) {
  const base = settings && typeof settings === "object" && !Array.isArray(settings) ? settings : {};
  const current = base[key] && typeof base[key] === "object" && !Array.isArray(base[key]) ? base[key] : {};
  const table = { ...current };
  if (list.length) table[view] = list.slice();
  else delete table[view];
  return { ...base, [key]: table };
}

function sameList(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) if (a[i] !== b[i]) return false;
  return true;
}

/**
 * Hide or show one card. `hidden` omitted toggles the current state. Returns the same object when
 * the card already has that state.
 * @param {string} view
 * @param {object} settings
 * @param {string} id card id
 * @param {boolean} [hidden] true → hide, false → show
 * @returns {object} next settings
 */
export function toggleHidden(view, settings, id, hidden) {
  if (typeof id !== "string" || !id) return settings;
  const current = listFor(settings, "hidden", view);
  const isHidden = current.includes(id);
  const wantHidden = hidden === undefined ? !isHidden : Boolean(hidden);
  if (wantHidden === isHidden) return settings;
  const next = wantHidden ? [...current, id] : current.filter((other) => other !== id);
  return withList(settings, "hidden", view, next);
}

/** Show every card of the view (clears `hidden[view]`); order is kept. */
export function showAll(view, settings) {
  if (!listFor(settings, "hidden", view).length) return settings;
  return withList(settings, "hidden", view, []);
}

/**
 * Whether the visible card `id` can move one step in `direction` (-1 up, +1 down): it must be
 * visible and have a visible neighbour on that side. Hidden cards never move.
 */
export function canMove(rows, id, direction) {
  return neighbour(rows, id, direction) >= 0;
}

function neighbour(rows, id, direction) {
  const step = direction < 0 ? -1 : 1;
  const index = rows.findIndex((row) => row.id === id);
  if (index < 0 || rows[index].hidden) return -1;
  for (let j = index + step; j >= 0 && j < rows.length; j += step) {
    if (!rows[j].hidden) return j;
  }
  return -1;
}

/**
 * Move a visible card one step up (-1) or down (+1), swapping it with its nearest visible
 * neighbour so hidden cards keep their slots. Stores the full order for the view. Returns the same
 * object when the move is impossible (unknown or hidden card, or already at that end).
 * @param {string} view
 * @param {Array<{id:string,label?:string}>} cards default order
 * @param {object} settings
 * @param {string} id card id
 * @param {number} direction -1 (up) or +1 (down)
 * @returns {object} next settings
 */
export function move(view, cards, settings, id, direction) {
  const rows = orderedCards(view, cards, settings);
  const from = rows.findIndex((row) => row.id === id);
  const to = neighbour(rows, id, direction);
  if (from < 0 || to < 0) return settings;
  const ids = rows.map((row) => row.id);
  [ids[from], ids[to]] = [ids[to], ids[from]];
  return withList(settings, "order", view, ids);
}

/** Convenience wrappers matching the old editor's two buttons. */
export const moveUp = (view, cards, settings, id) => move(view, cards, settings, id, -1);
export const moveDown = (view, cards, settings, id) => move(view, cards, settings, id, 1);

/**
 * Forget the owner's hidden set and order for `view` (the old "Reset layout"). Other views and
 * every other setting are untouched. Returns the same object when the view was already default.
 */
export function reset(view, settings) {
  const hasHidden = listFor(settings, "hidden", view).length > 0;
  const hasOrder = listFor(settings, "order", view).length > 0;
  if (!hasHidden && !hasOrder) return settings;
  let next = settings;
  if (hasHidden) next = withList(next, "hidden", view, []);
  if (hasOrder) next = withList(next, "order", view, []);
  return next;
}

/**
 * True when the view differs from its default on this device: something is hidden, or the
 * effective order of the view's cards differs from the default order.
 */
export function isCustomised(view, cards, settings, fixedOrder = false) {
  const defaults = cardList(cards);
  const rows = orderedCards(view, cards, settings);
  if (rows.some((row) => row.hidden)) return true;
  if (fixedOrder) return false;
  return !sameList(rows.map((row) => row.id), defaults.map((card) => card.id));
}

/**
 * Everything the dialog renders, precomputed: title, note, the visible rows with their move
 * affordances, the hidden rows, and whether anything is customised.
 */
export function editorModel(view, cards, settings, fixedOrder = false) {
  // A fixed-position view (Drive) lists its tiles in their screen order and offers no moves;
  // any order saved earlier is ignored there, as the view itself ignores it.
  const rows = fixedOrder ? orderedCards(view, cards, withList(settings, "order", view, [])) : orderedCards(view, cards, settings);
  const visible = [];
  const hidden = [];
  for (const row of rows) {
    if (row.hidden) hidden.push({ id: row.id, label: row.label });
    else visible.push({
      id: row.id,
      label: row.label,
      canUp: fixedOrder ? false : canMove(rows, row.id, -1),
      canDown: fixedOrder ? false : canMove(rows, row.id, 1),
    });
  }
  const customised = isCustomised(view, cards, settings, fixedOrder);
  return {
    view,
    title: viewTitle(view),
    note: customised ? TEXT.noteCustom : TEXT.noteDefault,
    customised,
    fixedOrder: Boolean(fixedOrder),
    intro: fixedOrder ? TEXT.introFixed : TEXT.intro,
    visibleTitle: fixedOrder ? TEXT.visibleTitleFixed : TEXT.visibleTitle,
    hiddenTitle: fixedOrder ? TEXT.hiddenTitleFixed : TEXT.hiddenTitle,
    visible,
    hidden,
    empty: rows.length === 0,
  };
}

/**
 * Stable key for one control of a row: `<id>:check`, `<id>:up` or `<id>:down`. The dialog tags
 * each control with it so focus can follow the card after an edit re-orders or re-files the row
 * (the old editor restored focus by element id after every change).
 */
export function focusKey(id, control) {
  return id + ":" + control;
}

/**
 * The control that should take focus after an edit, given the freshly built `model` and the key
 * that had focus: the same control when it still exists and is enabled; the other arrow when the
 * card reached an end; its checkbox when neither arrow is enabled; `null` when the card is gone.
 */
export function focusFallback(model, key) {
  if (typeof key !== "string") return null;
  const cut = key.lastIndexOf(":");
  if (cut <= 0) return null;
  const id = key.slice(0, cut);
  const control = key.slice(cut + 1);
  const visible = model && Array.isArray(model.visible) ? model.visible.find((row) => row.id === id) : null;
  if (visible) {
    if (control === "up") return visible.canUp ? key : visible.canDown ? focusKey(id, "down") : focusKey(id, "check");
    if (control === "down") return visible.canDown ? key : visible.canUp ? focusKey(id, "up") : focusKey(id, "check");
    return focusKey(id, "check");
  }
  const hidden = model && Array.isArray(model.hidden) ? model.hidden.find((row) => row.id === id) : null;
  return hidden ? focusKey(id, "check") : null;
}
