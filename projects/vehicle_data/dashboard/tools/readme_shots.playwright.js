// README screenshot sequence: a Playwright function for the browser_clean MCP's browser_run_code
// (`filename`) on the Mac; tools/readme_shots.mjs runs the same file on the Pi. Prerequisites, in
// order: the dashboard build served by web_v2 at DASHBOARD, the demo bundle from
// `node tools/demo_capture.mjs <dir> <url>` served at INSTALL's directory, and optionally
// `page.__shots = {only: ["03", ...]}` to retake some. The page is the only way to pass this
// function anything, so `page.__shots` also takes `dashboard`, `install` and `out` in place of the
// Mac defaults below (readme_shots.mjs sets all four). Writes <OUT>/<file> for each shot.
async (page) => {
  const {only, dashboard: DASHBOARD = "http://127.0.0.1:8777/", install: INSTALL = "http://127.0.0.1:8791/install-demo.js",
    out: OUT = "/Users/jacobr/dev/obd-things/tmp/readme_screens/"} = page.__shots || {};
  await eval(await (await page.request.get(INSTALL)).text())(page);
  await page.setViewportSize({width: 800, height: 1280});
  if (!page.url().startsWith(DASHBOARD)) await page.goto(DASHBOARD);
  const demo = page.__demo;

  /** Scroll the view so the card titled `title` sits `offset` px below the top bar. */
  const scrollToCard = (title, offset = 12) => page.evaluate(([t, o]) => {
    const view = document.querySelector(".view");
    const heading = [...view.querySelectorAll("h2, h3")].find((e) => e.textContent.trim().startsWith(t));
    if (!heading) throw new Error("no card " + t);
    const card = heading.closest("section, .card") || heading;
    view.scrollTop += card.getBoundingClientRect().top - view.getBoundingClientRect().top - o;
  }, [title, offset]);
  const click = async (selector, text) => {
    await page.locator(selector, {hasText: text}).first().click();
    await page.waitForTimeout(600);
  };
  /** Wait for a dialog to open and finish loading what it reads. */
  const dialogReady = async (text) => {
    await page.waitForSelector("dialog[open]");
    if (text) await page.locator("dialog[open]", {hasText: text}).waitFor();
    await page.waitForFunction(() => !/Loading|Opening the event/.test(document.querySelector("dialog[open]").textContent));
    await page.waitForTimeout(800);
  };

  const shots = {
    "01-drive-gauges.png": () => demo.open("drive", {drivePage: 1}),
    "02-drive-sensors.png": () => demo.open("drive", {drivePage: 2, themeMode: "light"}),
    "03-parked-battery-tires-and-service.png": () => demo.open("parked", {view: "parked"}),
    "04-health-warnings-and-diagnostics.png": () => demo.open("running", {view: "health"}),
    "05-saved-warning-events.png": async () => {
      await demo.open("running", {view: "health"});
      await click(".view button", "Event history");
      await dialogReady("Saved Events");
    },
    "06-warning-event-evidence.png": async () => {
      await demo.open("running", {view: "health"});
      await click(".view button", "Event history");
      await dialogReady("Saved Events");
      await click("dialog[open] button", "View Details");
      await dialogReady("Why This Event Opened");
    },
    "07-history-trips-and-temperature-trends.png": () => demo.open("running", {view: "history"}),
    "08-history-pressure-voltage-and-power.png": async () => {
      await demo.open("running", {view: "history"});
      await scrollToCard("Oil pressure");
    },
    "09-system-bus-health-and-radar.png": () => demo.open("parked", {view: "system"}),
    "10-system-metric-catalog.png": async () => {
      await demo.open("running", {view: "system"});
      await scrollToCard("Metric catalog");
    },
    "11-customise-dashboard-tiles.png": async () => {
      await demo.open("running", {view: "system"});
      await scrollToCard("Customise");
      await click(".view button", "Customise");
      await dialogReady("Customise Drive");
    },
    "12-codex-warning-explanation.png": async () => {
      demo.chat("explain");
      await demo.open("running", {view: "health"});
      await click(".view button", "Ask Codex");
      await dialogReady("Ready for a follow-up");
    },
    "13-create-custom-early-warning.png": async () => {
      demo.chat("setup");
      await demo.open("running", {view: "health"});
      await click(".view button", "Add early warning");
      await dialogReady("Approve");
    },
    // An unsaved example entry: typing stays in the form, and Save is never pressed.
    "14-record-oil-change.png": async () => {
      await demo.open("parked", {view: "parked"});
      await click(".view button", "Record oil change");
      await dialogReady("Record an oil change");
      const form = page.locator("dialog[open]");
      await form.locator("input[type=date]").fill("2026-10-28");
      await form.locator("input[inputmode=numeric], input[type=number]").first().fill("48217");
      await form.locator("select").first().selectOption({label: "Instrument cluster or service receipt"});
      await form.locator("textarea").fill("Full synthetic 5W-20 and OEM filter. DIY service.");
      await page.waitForTimeout(400);
    },
    // Shown as an enabled listener presents it; the confirmation stays unticked and nothing starts.
    "15-guarded-diagnostic-code-scan.png": async () => {
      await demo.open("parkedScan", {view: "health"});
      await page.locator(".view summary", {hasText: "Parked code scan"}).click();
      await page.waitForTimeout(600);
      await page.evaluate(() => {
        const view = document.querySelector(".view");
        const summary = [...view.querySelectorAll("summary")].find((e) => e.textContent.includes("Parked code scan"));
        view.scrollTop += summary.getBoundingClientRect().top - view.getBoundingClientRect().top - 470;
      });
    },
    "16-drive-night-dimming.png": () => demo.open("drive", {drivePage: 1, dim: true}),
    "17-drive-live-fuel-economy.png": () => demo.open("drive", {drivePage: 2}),
    // Ready to start: the stationary box is ticked so the hint reads "Ready to start."; Start is never pressed.
    "18-radar-service-alignment.png": async () => {
      await demo.open("stationaryRadarService", {view: "system"});
      await scrollToCard("Radar alignment");
      await click(".view button", "Drive calibration");
      await dialogReady("Radar drive calibration");
      await page.locator("dialog[open] input[type=checkbox]").check();
      await page.waitForTimeout(400);
    },
  };
  const taken = [];
  for (const [file, prepare] of Object.entries(shots)) {
    if (only && !only.includes(file.slice(0, 2))) continue;
    await prepare();
    await page.screenshot({path: OUT + file});
    taken.push(file);
    await page.keyboard.press("Escape");
  }
  return taken;
}
