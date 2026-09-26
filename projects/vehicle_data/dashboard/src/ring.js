/**
 * Per-metric ring buffer feeding sparklines (design.md section 2.2, `ring.js`).
 *
 * One ring holds the recent samples of one metric on the monotonic
 * (`performance.now()`) axis. Storage is a pair of `Float64Array`s plus a
 * parallel array of dedupe keys, so a push never allocates. Columns for a
 * sparkline are computed on demand into a pooled output array, so a redraw
 * does not allocate either. Wall-clock times only enter through `seed()`,
 * which maps history points onto the monotonic axis using one
 * `(nowMono, nowEpochMs)` pair; freshness is never derived from them.
 *
 * Node-friendly and ES2018: no DOM; `performance` is read only as the default
 * mapping of a `seed()` call made without one.
 */

/** Default window kept in a ring: 15 minutes. */
export const DEFAULT_WINDOW_MS = 900000;

/** Default capacity: 1,200 points (15 min at 1 Hz with headroom). */
export const DEFAULT_MAX_POINTS = 1200;

/**
 * Parse a history timestamp into epoch milliseconds.
 * Accepts epoch milliseconds, a `Date`, or an ISO-8601 string (the broker's
 * microsecond fractions are trimmed so old engines can parse them).
 * @param {number|string|Date|null|undefined} at
 * @returns {number} epoch ms, or `NaN` when unparseable
 */
export function parseEpochMs(at) {
  if (typeof at === "number") return isFinite(at) ? at : NaN;
  if (at instanceof Date) return at.getTime();
  if (typeof at !== "string" || at.length === 0) return NaN;
  let iso = at;
  const dot = iso.indexOf(".");
  if (dot >= 0) {
    let end = dot + 1;
    while (end < iso.length && iso.charCodeAt(end) >= 48 && iso.charCodeAt(end) <= 57) end += 1;
    if (end - dot - 1 > 3) iso = iso.slice(0, dot + 4) + iso.slice(end);
  }
  return Date.parse(iso);
}

/**
 * Map an epoch time onto the monotonic axis given one simultaneous pair.
 * @param {number} epochMs wall-clock time of the point
 * @param {number} nowMono `performance.now()` at the moment `nowEpochMs` was read
 * @param {number} nowEpochMs `Date.now()` read at the same moment
 * @returns {number} monotonic milliseconds
 */
export function epochToMono(epochMs, nowMono, nowEpochMs) {
  return nowMono - (nowEpochMs - epochMs);
}

/** `{nowMono, nowEpochMs}` read now, or null where `performance` is missing. */
function defaultMapping() {
  if (typeof performance === "undefined" || typeof performance.now !== "function") return null;
  return { nowMono: performance.now(), nowEpochMs: Date.now() };
}

/**
 * Create a ring buffer.
 *
 * @param {{windowMs?: number, maxPoints?: number}} [options]
 *   `windowMs` is the span kept and summarised by `columns()`; `maxPoints`
 *   is the storage capacity (the oldest point is overwritten when full).
 * @returns {{
 *   push(tMono: number, value: number, observedAt?: string|number|null): boolean,
 *   latest(): {t: number, value: number, observedAt: string|number|null}|null,
 *   columns(n: number, nowMono: number): Array<{min: number, max: number, mean: number, count: number}|null>,
 *   size(): number,
 *   clear(): void,
 *   seed(points: Array<{at?: string|number, t?: number, value: number}>, mapping?: {nowMono: number, nowEpochMs: number}): number,
 *   windowMs: number,
 *   capacity: number,
 * }}
 *   `push` returns `false` for a rejected sample (non-finite, out of order, or
 *   a repeat of the last `observedAt`). `latest()` and `columns()` return
 *   objects that are reused between calls: copy them if they must outlive the
 *   next call. `seed()` returns how many history points were accepted.
 */
export function createRing(options) {
  const opts = options || {};
  const windowMs = isFinite(opts.windowMs) && opts.windowMs > 0 ? opts.windowMs : DEFAULT_WINDOW_MS;
  const capacity = isFinite(opts.maxPoints) && opts.maxPoints >= 1 ? Math.floor(opts.maxPoints) : DEFAULT_MAX_POINTS;

  const times = new Float64Array(capacity);
  const values = new Float64Array(capacity);
  /** 1 for a point that came from `seed()`, 0 for a live push. */
  const origin = new Uint8Array(capacity);
  /** Dedupe key (`observedAt`) per slot; a stored reference, never a copy. */
  const keys = new Array(capacity).fill(null);

  let start = 0;
  let count = 0;

  const latestOut = { t: NaN, value: NaN, observedAt: null };
  let colPool = null;
  let colOut = null;

  function slot(i) {
    const k = start + i;
    return k >= capacity ? k - capacity : k;
  }

  function lastSlot() {
    return slot(count - 1);
  }

  function evictBefore(cutoff) {
    while (count > 0 && times[start] < cutoff) {
      keys[start] = null;
      start = start + 1 >= capacity ? 0 : start + 1;
      count -= 1;
    }
  }

  function append(t, v, key, seeded) {
    let idx;
    if (count === capacity) {
      idx = start;
      start = start + 1 >= capacity ? 0 : start + 1;
    } else {
      idx = slot(count);
      count += 1;
    }
    times[idx] = t;
    values[idx] = v;
    keys[idx] = key;
    origin[idx] = seeded ? 1 : 0;
  }

  /**
   * Append one live sample.
   * @param {number} tMono monotonic ms of acceptance
   * @param {number} value finite numeric value
   * @param {string|number|null} [observedAt] dedupe key; a repeat of the last accepted key is ignored
   * @returns {boolean} true when stored
   */
  function push(tMono, value, observedAt) {
    if (typeof tMono !== "number" || !isFinite(tMono)) return false;
    if (typeof value !== "number" || !isFinite(value)) return false;
    const key = observedAt === undefined ? null : observedAt;
    if (count > 0) {
      const last = lastSlot();
      if (key !== null && keys[last] === key) return false;
      if (tMono < times[last]) return false;
    }
    evictBefore(tMono - windowMs);
    append(tMono, value, key, false);
    return true;
  }

  /**
   * Newest stored point, or `null` when empty. The returned object is reused.
   * @returns {{t: number, value: number, observedAt: string|number|null}|null}
   */
  function latest() {
    if (count === 0) return null;
    const k = lastSlot();
    latestOut.t = times[k];
    latestOut.value = values[k];
    latestOut.observedAt = keys[k];
    return latestOut;
  }

  function ensureColumns(n) {
    if (colOut !== null && colOut.length === n) return;
    colPool = new Array(n);
    colOut = new Array(n);
    for (let i = 0; i < n; i += 1) {
      colPool[i] = { min: 0, max: 0, mean: 0, count: 0 };
      colOut[i] = null;
    }
  }

  /**
   * Downsample the window ending at `nowMono` into `n` equal columns.
   * Column `i` covers `[now - windowMs + i*w, now - windowMs + (i+1)*w)`
   * with `w = windowMs / n`; a point at or after `now` lands in the last
   * column. Empty columns are `null`, so gaps stay visible. The array and its
   * column objects are pooled and rewritten on every call.
   * @param {number} n number of columns (>= 1)
   * @param {number} nowMono monotonic ms of the right edge
   * @returns {Array<{min: number, max: number, mean: number, count: number}|null>}
   */
  function columns(n, nowMono) {
    const cols = typeof n === "number" && n >= 1 ? Math.floor(n) : 0;
    if (cols === 0) return [];
    ensureColumns(cols);
    for (let i = 0; i < cols; i += 1) {
      const c = colPool[i];
      c.count = 0;
      c.mean = 0;
      colOut[i] = null;
    }
    const startT = nowMono - windowMs;
    const colMs = windowMs / cols;
    for (let i = 0; i < count; i += 1) {
      const k = slot(i);
      const t = times[k];
      if (t < startT) continue;
      let idx = Math.floor((t - startT) / colMs);
      if (idx >= cols) idx = cols - 1;
      const c = colPool[idx];
      const v = values[k];
      if (c.count === 0) {
        c.min = v;
        c.max = v;
        c.mean = v;
      } else {
        if (v < c.min) c.min = v;
        if (v > c.max) c.max = v;
        c.mean += v;
      }
      c.count += 1;
    }
    for (let i = 0; i < cols; i += 1) {
      const c = colPool[i];
      if (c.count > 0) {
        c.mean /= c.count;
        colOut[i] = c;
      }
    }
    return colOut;
  }

  /** @returns {number} stored points */
  function size() {
    return count;
  }

  /** Drop every point. */
  function clear() {
    start = 0;
    count = 0;
    keys.fill(null);
  }

  /**
   * Seed the ring from history points such as `summary.history.metric_trends
   * [name].sparkline` (`[{at: ISO, value}]`). Wall-clock `at` values are
   * mapped onto the monotonic axis with `mapping`. Live points already in the
   * ring are kept and win over history: seed points newer than the oldest
   * live point are dropped, and points from an earlier `seed()` are replaced,
   * so re-seeding after every summary fetch is safe. Points are sorted by
   * time and trimmed to the window and capacity. This path allocates; it is
   * not for the 1 Hz path.
   * @param {Array<{at?: string|number|Date, t?: number, value: number}>} points
   *   `at`/`t` are wall-clock (ISO string or epoch ms), never monotonic
   * @param {{nowMono: number, nowEpochMs: number}} [mapping] simultaneous monotonic /
   *   epoch pair; defaults to `performance.now()` / `Date.now()` read now (the
   *   shared interface calls `seed(points)` with one argument)
   * @returns {number} seed points accepted
   */
  function seed(points, mapping) {
    if (!Array.isArray(points)) return 0;
    const map = mapping || defaultMapping();
    if (!map) return 0;
    const nowMono = map.nowMono;
    const nowEpochMs = map.nowEpochMs;
    if (typeof nowMono !== "number" || !isFinite(nowMono)) return 0;
    if (typeof nowEpochMs !== "number" || !isFinite(nowEpochMs)) return 0;

    // Live points survive; earlier seeds are discarded.
    const live = [];
    for (let i = 0; i < count; i += 1) {
      const k = slot(i);
      if (origin[k] === 0) live.push({ t: times[k], v: values[k], key: keys[k], seeded: false });
    }
    const oldestLive = live.length > 0 ? live[0].t : Infinity;

    const seeds = [];
    for (let i = 0; i < points.length; i += 1) {
      const p = points[i];
      if (!p || typeof p !== "object") continue;
      const v = p.value;
      if (typeof v !== "number" || !isFinite(v)) continue;
      const epoch = p.t !== undefined ? parseEpochMs(p.t) : parseEpochMs(p.at);
      if (!isFinite(epoch)) continue;
      const t = epochToMono(epoch, nowMono, nowEpochMs);
      if (t >= oldestLive) continue;
      seeds.push({ t: t, v: v, key: p.at !== undefined ? p.at : p.t, seeded: true });
    }
    seeds.sort(function (a, b) {
      return a.t - b.t;
    });

    // Both lists are sorted and every seed precedes every live point.
    const merged = seeds.concat(live);
    let newest = nowMono;
    if (merged.length > 0 && merged[merged.length - 1].t > newest) newest = merged[merged.length - 1].t;
    const cutoff = newest - windowMs;
    let first = 0;
    while (first < merged.length && merged[first].t < cutoff) first += 1;
    if (merged.length - first > capacity) first = merged.length - capacity;

    clear();
    let accepted = 0;
    for (let i = first; i < merged.length; i += 1) {
      const m = merged[i];
      append(m.t, m.v, m.key === undefined ? null : m.key, m.seeded);
      if (m.seeded) accepted += 1;
    }
    return accepted;
  }

  return {
    push: push,
    latest: latest,
    columns: columns,
    size: size,
    clear: clear,
    seed: seed,
    windowMs: windowMs,
    capacity: capacity,
  };
}
