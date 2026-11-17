process.env.TZ = "America/Los_Angeles";
const { test } = await import("node:test");
const assert = (await import("node:assert/strict")).default;
const { codeRow } = await import("../src/views/health.helpers.js");

const NOW = Date.parse("2026-09-22T19:00:00Z");
const record = (source) => ({
  module_key: "bcm_ccan", module_name: "BCM", fca_display: "B162A-15", raw_dtc: "B162A15", description: "Lamp",
  first_seen_at: "2026-09-22T18:00:00Z", last_seen_at: "2026-09-22T19:00:00Z", source,
});

for (const source of ["van", "drive"]) {
  test("a " + source + " record's panel shows the instant as its row does, away from Mountain time", () => {
    const row = codeRow(record(source), NOW);
    const details = Object.fromEntries(row.details);
    assert.match(details["Last seen"], /1:00 PM MDT/, "Mountain with an explicit zone, not the device's Pacific clock");
    assert.match(row.meta, /1:00 PM MDT/);
    assert.match(details["First seen"], /12:00 PM MDT/);
  });
}

test("a saved-list record keeps the device clock in both row and panel", () => {
  const row = codeRow(record(undefined), NOW);
  const last = Object.fromEntries(row.details)["Last seen"];
  assert.equal(row.meta.includes("last seen " + last), true);
  assert.doesNotMatch(last, /MDT/);
});
