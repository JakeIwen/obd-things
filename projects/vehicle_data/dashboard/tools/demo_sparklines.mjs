// Fill the Drive sparkline rings with the demo scene's trace. Shared by shoot.mjs (Firefox over
// WebDriver BiDi) and demo_capture.mjs (Playwright): both run it in the page by embedding its source,
// so it must stay self-contained, using only its arguments and the page's own globals.

/**
 * Runs in the page. Sparse knots leave empty canvas columns, so each metric's knots are interpolated
 * into a bounded five-second series and seeded through the public ring API, which keeps old samples
 * out of the trip-distance integral.
 * @param {{ringFor: Function}} van window.__van
 * @param {Object<string, number[]>} sparklines scene.sparklines: metric name → knot values
 * @param {number} nowMono performance.now() at injection
 * @param {number} nowWall Date.now() at injection
 */
export function seedDemoSparklines(van, sparklines, nowMono, nowWall) {
  const count = 169;
  for (const [metric, values] of Object.entries(sparklines)) {
    const ring = van.ringFor(metric);
    ring.clear();
    ring.seed(Array.from({length: count}, (_, i) => {
      const position = i * (values.length - 1) / (count - 1);
      const left = Math.floor(position), right = Math.min(left + 1, values.length - 1);
      return {at: nowWall - (count - 1 - i) * 5000, value: values[left] + (values[right] - values[left]) * (position - left)};
    }), {nowMono, nowEpochMs: nowWall});
  }
}
