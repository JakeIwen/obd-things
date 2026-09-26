/**
 * Process-wide handles created in main.jsx (the broker link) so components can
 * trigger a manual refresh without importing main.
 */
export const runtime = { link: null };

/** Manual refresh: HTTP resync, then a summary fetch that bypasses the 60 s throttle. */
export function manualRefresh() {
  const link = runtime.link;
  if (!link) return Promise.resolve(false);
  return link.resync("manual").then(() => link.fetchSummary({ force: true }));
}
