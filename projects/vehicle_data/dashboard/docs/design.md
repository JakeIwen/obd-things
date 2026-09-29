# Van telemetry dashboard — design specification

Status: build contract, 2026-09-23. It is the 2026-09-22 design baseline with the confirmed design
review applied (amendments A1–A21, with the owner exceptions listed below). It was built as "v2", a
parallel app on port 8766. On 2026-09-27 it replaced the former static app: it now runs from
`projects/vehicle_data/dashboard/` on port 8765 in the existing `van-telemetry-web.service` and
`van-telemetry-web-tailscale.service` units, and the old `projects/vehicle_data/static/` frontend and
port 8766 were removed. "v2" below names this app and its `/v2/*` routes; there is no other
dashboard.

Inputs: the September 22 audits (UI inventory, API contract, early-warning history, historian data,
DID-mapping state, tablet performance), the September 23 design review, and the owner's answers.
Owner decisions:

- The tablet is **portrait-mounted, 800×1280 CSS px**, old and slow, and scrolling lags today. It is
  the Samsung device at `192.168.6.142` (Samsung OUI). Its browser already runs the current bundle's
  ES2020 syntax. It keeps the page open for hours (150 SSE reconnects a week), reloads the bundle
  about twice a week and has never sent a POST. The model, Android version, browser and DPR are
  unconfirmed. `web_v2.py` logs each client's User-Agent once to record them.
- The Drive view shows the core gauges **plus** crank torque/power and small trends, **without
  scrolling** in portrait.
- Early warnings become **two tiers**: absolute limits with hysteresis, and sustained same-regime
  deviation. **ntfy is used only for actionable items**; adapter and data-quality issues never notify.
- **No feature is removed.** Simpler is fine; absent is not. Event history, Codex chat and Add Early
  Warning, the guarded DTC scan, radar alignment, per-device customisation, oil-change records and
  the passive voltage read all stay.
- Caveats move to a docs page. Cards carry no provenance words (REGISTERED, MAPPED, ALFA SCALE,
  `n/m LIVE`, candidate prose). A card says "stale" at most once. Plain English, numbers first,
  nothing under 13 px, 44 px tap targets.
- Owner exceptions to the review: **DTC scans stay available on the Tailscale listener**, with the
  exact trusted origin configured beside the bind (section 2.1). **Alerts render inside the 48 px top bar**, in
  place of the state text (section 3).
- A manual view choice lasts until the engine next starts or stops (section 5.2).

## 1. Goals and non-goals

Goals, in priority order:

1. Glanceable while driving: the numbers that matter fill one portrait screen, large and high
   contrast, and are coloured only when something deserves attention.
2. Cheap on the tablet: flat paint, a small DOM, one batched update per second, nothing re-rendered
   when nothing visible changed, and small assets served compressed and cached.
3. One fact, one place: each metric appears once per view. The vehicle's state (running, parked,
   asleep) is said once, in the top bar, and is never repeated under every tile.
4. Honest without noise: provenance and caveats live on the docs page (`/docs/caveats.html`, linked
   once per view) and in the System view's catalog. A tile shows a value, or a dimmed last value with
   its time, or a dash.
5. Warnings a driver can act on: the number, the comparison and an action. System noise goes on a
   separate card.

Non-goals for v2.0: any new CAN acquisition; changing the
broker's safety contract; a light theme (a high-contrast day mode waits until the screen is checked in
sunlight); a no-scroll landscape layout; standalone (browser-chrome-free) display, which needs HTTPS.
Deferred to v2.1: the per-wheel tire cold baseline, per-trip statistics, seeded Drive sparklines,
consuming the warning backend's `group`/`tier`/`action` fields, and the gear footer.

## 2. Architecture

```
tablet browser ──HTTP/SSE──▶ web_v2.py (:8765, subclass of web.py)
                                │  /            index.html (no-cache, ETag)
                                │  /assets/*    hashed bundle, gzip, immutable cache
                                │  /docs/*      caveats.html and warnings.html only
                                │  /v1/*        unchanged proxies to the broker (cache-only GETs,
                                │               same POST gates and origin checks as web.py)
                                │  /v2/stream   lite SSE: metrics + status-lite + catalog hash
                                │  /v2/summary  trimmed, gzip'd supplemental bundle
                                ▼
                       /run/van-telemetry/api.sock (broker, untouched)
```

### 2.1 Server: `projects/vehicle_data/web_v2.py`

- It imports `projects.vehicle_data.web` and subclasses `TelemetryWebHandler` and
  `TelemetryWebServer`. `web.py` provides the `/v1` JSON API (broker proxy, stream, POST gates, DTC
  jobs, advisor proxy) and, since the 2026-09-27 swap, serves no page of its own; only `web_v2.py`
  serves the frontend.
- Static root: `projects/vehicle_data/dashboard/dist/`, which is **gitignored build output**
  (section 2.2). The server refuses to start when `dist/index.html` is missing, so the build must
  succeed before the units are installed. Path traversal gets the same JSON 404 envelope as `web.py`.
  MIME types come from an extension table (`.js`, `.mjs`, `.css`, `.html`, `.svg`, `.json`, `.map`,
  `.webmanifest`, `.woff2`, `.png`, `.ico`, `.md`, `.txt`).
- Compression: gzip for text, JSON, JavaScript, SVG and manifest bodies over 1 KiB when the client
  sends `Accept-Encoding: gzip`. Static gzip bodies are cached in memory, keyed by path and mtime.
  SSE is never gzipped.
- Caching: `/assets/<name>.<hash>.<ext>` gets `Cache-Control: public, max-age=31536000, immutable`.
  `/`, `/index.html` and `/docs/*` are `no-cache` with ETag revalidation. All `/v1/*` and `/v2/*`
  keep `no-store`. A rebuild is therefore picked up without restarting the service.
- Security headers stay as in `web.py`. The CSP stays `default-src 'self'; script-src 'self';
  style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none';
  frame-ancestors 'none'`. `style-src 'self'` blocks style attributes in HTML, not CSSOM writes, but
  the project rule is stricter: no inline style props at all. Drawn geometry uses SVG/canvas
  attributes and everything else uses classes. The build test asserts that built HTML has no
  `style=` attribute.
- `GET /v2/stream`: the same loop, cadence, `web_delivery` envelope and `id:` line as `/v1/stream`.
  The event data is `{status: <status-lite>, metrics, catalog_hash, catalog_count, web,
  web_delivery, status_code}`.
  - status-lite keeps `service, started_at, vehicle_state, collector, active_drive, auxiliary_drive,
    engine_off_voltage, radar_alignment, radar_alignment_polling, last_readings, supplemental_cache,
    current_owner, interface_probe, history_recorder, inflight, last_acquisition_errors,
    active_acquisition_permitted`.
  - From the interface it keeps `interface.{mode, active_inhibits, adapter_present, up}` and
    `interface.role_interfaces.{ready, passive_ready, resolved, vehicle_buses_ready, mode, issues,
    generation, roles[*].{channel, resolution, reason, safe, passive_ready}}`.
  - It also keeps `data_quality` and `usb_can_monitor` reduced to `{state, active_count, enabled}`.
  - It drops `cached_metrics`, adapter inventories, USB event lists and the reconcile dump. The client
    reads those from `/v2/summary.status_full`, which is at most 60 s old (freshness rule 20).
  - `catalog_hash` is a SHA-256 prefix of the canonical catalog JSON. When it changes, the client
    resyncs over HTTP. `/v1/snapshot` on this listener carries the same hash, so the baseline and the
    stream agree.
  - Measured on the September 22 payloads, a `/v2/stream` event is 14.0 KB against 40.9 KB for
    `/v1/stream`.
- `GET /v2/summary`: one gzip'd JSON built from the broker's cached `/v1/history`, `/v1/health`,
  `/v1/diagnostics/dtcs`, `/v1/maintenance` and `/v1/status`:
  - `health`: `web_v2.health_lite`. `assessments`, `active` and `episodes.active` are **never
    capped**. Other capped lists keep their first 16 items and report `<key>_omitted_count` beside
    the list, never as a sentinel inside it. The client builds warning rows by joining
    `episodes.active[*]` (`id`, `opened_at`, `latest_assessment`) to assessments by `rule`. It reads
    `absolute_threshold`, `deviation{signed_from_median, threshold}`, `baseline{median, mad, regime}`,
    `custom_rule{operator, threshold}`, `persistence`, `notification_eligible` and
    `notification_suppressed_by`, plus `data_quality.active/recent`, `usb_can_incidents`,
    `notification_delivery` and `episodes.notification_outbox`. Assessments have no `id`,
    `opened_at` or `threshold`; those live on episodes or in the fields above.
  - `dtcs`: records keep `raw_dtc`, `fca_display`, `module_key`, `module_name`, `logical_bus`,
    `status`, `display_group`, `description`, `description_reviewed`, `description_source`,
    `first_seen_at`, `last_seen_at`, `observation_state` and the status flags. There is no `code`
    field; the display code is `fca_display`, falling back to `raw_dtc`. `current` and `pending` are
    complete. The broker truncates each group at `per_group_limit` (25), so counts always come from
    `group_counts`.
  - `history`: the broker's `/v1/history` product as is: `recent_trips`, `current_trip`,
    `trip_comparison`, `windows`, `maintenance_hook` and `coverage`, which holds `status`,
    `age_seconds`, `last_snapshot_at`, `active_interface_gaps`, `active_metric_gaps` and
    `retention`. It also holds `metric_trends[name]`, with fields `unit`, `current_trip`,
    `current_minus_prior_median`, `prior_trips`, `days_7`, `days_30`, `series` and `sparkline`. Both
    `series` and `sparkline` use 900 s buckets.
  - `maintenance`: as is (1.4 KB).
  - `status_full`: the complete broker status for the System view, fetched here once a minute rather
    than every second.
  - Measured size: ≈ 190 KB raw, ≈ 27 KB gzip (history 10.0, health 10.0, DTCs 3.8 and status 3.3 KB
    gzip). Re-measure after the uncapping.
  - The client fetches `/v2/summary` after every resync and every 60 s while the page is visible. The
    store publishes a slice only when its JSON changed. A manual refresh bypasses the 60 s throttle.
- Everything the dialogs need stays on `/v1/*`: `/v1/health` (now gzipped), `/v1/events…`,
  `/v1/assistant/*`, `/v1/maintenance/oil-changes`, `/v1/diagnostics/dtc-jobs*` and
  `/v1/acquisitions/battery.voltage`. No route is removed. There is no `/v2/health-full`.
- Every `web` flags object on this listener adds `bind` and the dashboard `build` id, read from
  `<static_root>/build.json` and refreshed when that file's mtime changes.
- CLI: `web_v2.py --socket … --bind <addr> [--port 8765] --allow-remote-bind [--cache-only]
  [--enable-dtc-jobs --dtc-trusted-origin …] [--warning-chat-origin …] --static-root <dist>`.
  `--port` defaults to 8765 and `--static-root` to `dashboard/dist`.
- Two deployments, both on port 8765:
  - **LAN** (`van-telemetry-web.service`). The tracked unit binds loopback; the machine-local drop-in
    `/etc/systemd/system/van-telemetry-web.service.d/10-lan.conf` adds `--allow-remote-bind` and the
    selected LAN address (`http://vanpi.lan:8765/`). The passive voltage read is allowed (the
    listener is not `--cache-only`). There are no DTC jobs: `web.py` refuses DTC jobs on any bind
    other than loopback or Tailscale.
  - **Tailscale** `${VAN_TELEMETRY_TAILSCALE_BIND}:8765` (`van-telemetry-web-tailscale.service`),
    **with guarded DTC jobs**. The trusted origin is `VAN_TELEMETRY_DTC_ORIGIN` from
    `/etc/van-telemetry/tailscale-web.env` (`http://<tailscale-ip>:8765`). The unit
    `Requires=van-dtc-batch.path` and has `ReadWritePaths=/run/van-telemetry`.
  - The MacBook-managed Van Dashboard (`/home/pi/scripts/python-automation/van_dashboard_common.py`)
    reads `http://192.168.6.103:8765/v1/snapshot` from the LAN listener, so every `/v1` route must keep
    working.
  - Concurrency: the DTC request file is exclusive-create, so two scans can never both be queued. The
    job pointer on disk is not locked across processes; only the Tailscale listener has DTC jobs, so
    only one process writes it. Several browsers on that listener share the pointer and each shows a
    scan started from another. Rule: start one scan at a time.

### 2.2 Frontend: Preact + signals, esbuild, no framework re-renders on the hot path

- **Build.** Sources are under `dashboard/src/` and output goes to `dashboard/dist/`.
  - `dist/` is **gitignored build output**, and **`npm ci && npm run build` is the deploy step**. Node
    20 and the esbuild arm64 binary run on the Pi. `node_modules/` is gitignored.
  - `package.json` pins `preact` 10, `@preact/signals` 2, `esbuild` and `marked`.
  - `node tools/build.mjs` (`npm run build`) bundles `src/main.jsx` into
    `dist/assets/main.<hash>.js`, with lazily loaded chunks as `chunk.<hash>.js`, and the CSS into
    `main.<hash>.css`. Output is minified ESM with external sourcemaps.
  - **esbuild target: `['chrome80', 'firefox78']`.** Safari is left out on purpose: esbuild cannot
    downlevel destructuring for old Safari targets, and the only client is the Android Chromium
    tablet, which already runs ES2020.
  - The build writes `dist/index.html` with the hashed names and renders **only** `docs/caveats.md`
    and `docs/warnings.md` to `dist/docs/*.html`. Specs and contracts (`design.md`,
    `freshness-contract.md`, `warnings-redesign.md`) stay in the repository and are never published
    to the LAN.
  - It copies `manifest.webmanifest` and `icon.svg`, and records `{build, builtAt, js, css, docs,
    outputs}` in `dist/build.json`. The System view and the deployment note show the build id.
  - The build is safe to run against a live listener. New hashed assets are written beside the old
    ones, the pages are replaced atomically, and unreferenced files are pruned only afterwards. A
    failed build leaves the previous deployment intact.
  - `--out-dir <dir>` builds elsewhere; the smoke test uses a temporary directory.
- **Manifest.** It supplies the icon and an orientation hint only. Over plain HTTP, Chromium-family
  browsers do not install a standalone app: "Add to Home screen" opens with the toolbar. The layout
  therefore never assumes the full 1280 px. HTTPS (`tailscale cert`) or a kiosk browser is a later
  option. 192 px and 512 px PNG icons are still to be shipped.
- **Why Preact + signals.** Each metric is a signal, and a tile binds signals directly into text
  nodes and attributes. A 1 Hz update touches only the text nodes whose formatted value changed; no
  virtual-DOM diff of the page runs per tick. Views are JSX components, which keeps lists (DTCs,
  events, trips) readable.
- **Budget.** Core (the main entry plus its static imports) is ≤ 90 KB minified and ≤ 30 KB gzip.
  Each dialog chunk, including what it adds beyond the core, is ≤ 45 KB minified and ≤ 15 KB gzip.
  `test/build.test.mjs` asserts the minified numbers from the esbuild metafile. Dialogs (event
  history, chat, customiser, oil-change sheet) and every view except Drive are separate chunks loaded
  on first use.
- **Modules.**
  - `link.js`: the broker link. It implements the freshness contract (section 6) exactly and emits
    into the store inside `batch()`.
  - `store.js`: the signals `catalog`, per-metric observation signals (`metricSignal(name)`),
    `vehicle`, `statusLite`, `web`, `connection`, `clock` (monotonic, whole seconds) and `summary.*`
    (one signal per slice: `history`, `health`, `dtcs`, `maintenance`, `statusFull`).
  - `app/derive.js`: shared computeds. It provides:
    - `obs`, `liveValue`, `band` and `tileModel(name, {decimals})` (all primitives or
      reference-stable);
    - `engineRunning`, `voltageMode` and `warningModel`, `metricAlertColour`;
    - the top bar's `vehicleHead`, `vehicleTail` and `alertStrip`, plus `healthBadge`;
    - `minuteClock`, `activeView` and `evaluateAutoView`;
    - the customisation helpers `isHidden` and `orderedIds`;
    - `startEngineTracking`.
  - `app/rings.js`: 15-minute typed-array rings for the Drive sparklines, plus the trip distance
    integral.
  - `app/dialogs.js` (the lazy dialog host) and `app/runtime.js` (`manualRefresh`).
  - `ring.js`, `format.js`, `bands.js`, `warnings.js` (warning rows and system notes from the
    health slice) and `settings.js` (per-device settings and `autoView`).
  - `components/`: `Tile` + `BandBar`, `Sparkline` (canvas), `TireGrid`, `Card` / `Disclosure` / `KV`
    / `Row`, `ServiceCard` and `DtcScan`.
  - `views/`: `Drive` (with the page-2 chunk `DriveMore` and `drivePager.js`), `Parked`, `Health`,
    `History` and `System`.
  - `dialogs/`: `EventHistory`, `WarningChat`, `Customizer` and `OilChange`.
  - Heavy logic lives in a sibling `*.helpers.js` (pure, node-testable), with `node:test` coverage
    in `test/<name>.test.mjs`.
- **Store publishing rules.**
  - Signals are written only on change.
  - A metric record is replaced only when its *sample* changes (`sameSample`). Ages are kept outside
    signals and refreshed in place.
  - `tick()` publishes only stale-flag flips.
  - **Status-lite is published only when its stable projection changes.** The comparison ignores the
    counters and clocks that move every second: `cycles`, `last_cycle_at`, `elapsed_seconds`,
    `age_ms`, `observed_at`, `generated_at`, `last_stored_at`, `snapshots_stored`,
    `last_refreshed_at`, `last_refresh_duration_ms`, `last_event_at`, `inflight`,
    `history_recorder`, `vehicle_state` and `web`. Vehicle state has its own signal, and `web` has
    its own change check.
  - Summary slices publish only when their JSON changed.
- **Rendering rules (the tablet budget).**
  - The scrolling views are lists of `.card` elements, and only the active view is mounted.
  - Components read `store.summary.*` slices and stable signals through module-level `computed`s
    that return primitives or structurally gated objects.
  - Nothing reads `store.clock` or per-second data in a render, except through `minuteClock`.
    Minute-resolution text (trip duration, "last drive 4:25 pm", ages) changes at most once a minute.
  - Lists are keyed by stable ids. There are no inline style props and no layout reads in render.
    `Sparkline` reads its size once on mount and on window resize.
  - There are two timers:
    - the DTC job poll (2 s while a job is active and the page is visible, 5 s after an error),
      which stops when the panel unmounts;
    - one shared sparkline redraw timer (at most every 10 s, only while Drive sparklines are
      mounted, and only when a ring changed or the window slid by a column).
  - Long disclosure bodies (DTC groups, module coverage, unconfirmed items) render on first open.
- **CSS class contract.** `src/styles/app.css` holds the shared classes (frame, top bar, tab bar,
  tiles, band bar, cards, disclosures, key/value lists, rows, buttons, dialogs, docs pages, and the
  `.dim` night tokens). Each view or dialog appends its own classes at the end, under a comment
  naming its source file, with a prefix: `.chat-*`, `.evh-*`, `.cust-*`, `.oil-*`, `.health-*`,
  `.parked-*`, and `.history-*` / `.system-*` for the views in progress. No file restyles another
  file's prefix.

## 3. Information architecture

Five views sit on a bottom tab bar (64 px, within thumb reach in portrait): **Drive · Parked ·
Health · History · System**. The Health tab shows a badge with the number of open items (amber, or
red if any is critical).

**Top bar (48 px, fixed height).** From left to right:

- **Connection dot**: a 48 px button. Green means live, amber means connecting or resyncing, red
  means the broker is unavailable. Tapping it refreshes: an HTTP resync, then a `/v2/summary` fetch
  that bypasses the 60 s throttle.
- **State text**: said once, for example `Running · 32 min`, `Asleep · last drive 4:25 pm` or
  `Ignition on · engine off`.
- **Dim**: a night toggle, stored per device. Primary text drops to the secondary tone, and band
  bars and values are subdued; red and amber are unchanged.
- **Auto**: turns automatic view selection back on (section 5.2).

**Alert in the top bar.** When an item is in `warning` state or has critical severity, the state text
is replaced by the most severe item in a short `metric value` form, plus `+n` for the rest. Examples:
`Coolant 232 °F +1`, or `Oil pressure 9 psi · stop the engine when safe` when a live value is in a
red band before the broker has confirmed a rule. The bar background turns amber, or red for
critical. The layout never changes height. Tapping the alert opens that item's event, or the Health
view when it has none. `watch` items appear only on the Health card.

Each scrolling view carries one link to `/docs/caveats.html`. Settings live in `localStorage` under
`van-telemetry.v2.settings`, per device, and are never sent to the broker. The app now serves the
former app's origin (port 8765), so the old `van-telemetry.dashboard.v3` view selection migrates once
when the v2 key is absent, and the Codex chat access code (`van-warning-chat.*`) carries over.

### 3.1 Drive (one screen in portrait; no scrolling)

Portrait grid: 3 columns (`repeat(3, minmax(0,1fr))`) and 4 rows (`1.15fr 1fr 1fr 1.15fr`) filling
the view height. The frame is `100dvh` (falling back to `100vh`) minus the 48 px top bar and 64 px
tab bar, with 16 px gutters and 12 px gaps.

| row | content |
|---|---|
| A | **SPEED** (2 cols, hero numerals, `mph`; sub-line `limit 55 mph` while `vehicle.speed_limit` is live, else empty) · **RPM** (1 col; the unit is in the label; the footer reads `gear 6` once a live gear metric exists and is empty until then; 15-min sparkline) |
| B | **COOLANT** · **TRANS OIL** · **OIL PRESSURE**: value, unit, band bar, 15-min sparkline |
| C | **VOLTAGE** (1 decimal; sub-line `alternator 42 %`) · **POWER** (`hp`; sub-line `165 lb-ft`) · **OIL TEMP (VVT)** |
| D | **TIRES** (2 cols: 2×2 grid FL FR / RL RR) · **TRIP** (Time, Distance `≈ 12.3 mi`, Coolant max, Avg power) |

The portrait gate is no scrolling within ≤ 1,096 visible px (the browser toolbar stays; see the
manifest note). In landscape (`min-aspect-ratio: 1/1` and width ≥ 900 px), there are 4 columns:
SPEED (2) · RPM · TIRES spanning the rows, then COOLANT · TRANS · OIL PRESSURE, then VOLTAGE · POWER ·
OIL TEMP · TRIP. Landscape may scroll in v2.0. At ≤ 640 px (phone) Drive becomes two columns and
scrolls. Type sizes are `clamp()`ed against the viewport height (`--hero: clamp(64px, 11vh, 128px)`,
with `--large`, `--value` and `--tire` in proportion). Because the rows are fractions of the same
height, type and rows scale together.

Rules for Drive tiles:

- A live value is white on the flat card. A held value (only metrics in the retention policy) is
  dimmed to 55 % opacity, with its time (`4:25 pm`) in the sub-line in place of the live sub-line. A
  metric that cannot be read in the current state shows `—` and nothing else. There are no ages,
  quality words or fractions.
- **Numerals change colour only for a confirmed warning or a red band.** They turn amber or red when
  a rule for that metric is in `warning` state or critical, and red when a live value is inside a red
  band. Otherwise they stay white.
- Band position is shown only by the bar. The **normal band is drawn in a neutral colour**
  (`--band-ok`), and the marker is white, amber or red. Green is never used on a value; it marks
  only "live/on" UI (the connection dot, active toggles and the current tab).
- Ignition is not a tile; its value is in the System catalog. Odometer is not a page-1 tile; it is in
  Service and on page 2 (3.1.1), and trip distance comes from the speed integral.
- Sparklines show the last 15 minutes from the client ring, with no axis and no labels. They start
  empty and fill live; they are not seeded from 900 s buckets. Each scales to its own range with a
  per-metric minimum span (10 psi, 20 °F, 0.5 V) and draws only the nearest band edge.
- Trip distance integrates live speed samples (gaps longer than 30 s are skipped). It is persisted
  in `localStorage` (`van-telemetry.v2.trip`) keyed by the trip's start, so a reload mid-trip keeps
  it. Coolant max and average power come from `history.trip_comparison` (60 s cadence).
- Customisation can hide a Drive tile. Positions are fixed, so a hidden tile leaves its cell empty
  and nothing else moves.

### 3.1.1 Drive page 2 (Sensors)

Owner request, 2026-09-24. Drive has two pages. Page 1 ("Gauges") is the grid above, with the same
content. Page 2 ("Sensors") uses the same frame and the same no-scrolling gate.

- **Paging.**
  - A two-segment pager (`1 Gauges | 2 Sensors`, 44 px high, each segment ≥ 168 px wide) sits
    under the page grid, above the tab bar. The view is a grid with rows `minmax(0,1fr) 44px`, so
    page 1's rows are 56 px shorter than before and nothing else changes.
  - A deliberate horizontal swipe on the view also pages: ≥ 80 px, at least twice as wide as tall,
    under 800 ms. Left goes to page 2 and right to page 1. The two listeners are passive
    `touchstart`/`touchend`, and `views/drivePager.js` `swipeTarget` decides.
  - The page is remembered per device as `settings.drivePage` (1 or 2; anything else loads page 1).
    Paging never touches automatic view selection.
  - Only the page on screen is mounted, so page-1 tiles and sparklines unmount on page 2.
    `views/DriveMore.jsx` is a lazy chunk, fetched the first time page 2 is shown. The core bundle
    grows only by the pager and swipe code (about 2 KB).
- **Portrait grid** (3 columns; rows `1fr 1fr 1.2fr 1fr 1fr`; the speed-limit/ACC row was added
  on 2026-09-27):

  | row | content |
  |---|---|
  | 0 | **SPEED LIMIT** (`vehicle.speed_limit` in a sign outline with `MPH` under it; `—` in a dim outline when the cluster shows none) · **ACC** (2 cols: `acc.set_speed` `66 mph`; sub-line from the `acc.state` record, then what the cluster shows beside it: `engaged · vehicle ahead · gap 4 of 4`, `standby · gap 4 of 4`, `engaged · fixed cruise`; without those records it reads `set · engaged` as before; standby dims the number; `Off` when the state is off; `—` with `ready · gap 1 of 4`, or `ready · no set speed`, in ready) |
  | A | **ALTERNATOR FIELD** (2 cols: `generator.field_duty` %, 15-min sparkline, sub-line `battery 14.0 V`) · **GEAR** (`~7` from `transmission.gear_estimate` while its record is current, else `—`; sub-line `ratio 0.70` = turbine ÷ output when the output shaft turns ≥ 100 rpm) |
  | B | **RADAR AIM** (3 cols): Horizontal and Vertical side by side. Each shows its latest angle (`+0.15°`) and then `1-min avg` / `5-min avg` from `status.radar_alignment` windows `"60"`/`"300"`. |
  | C | **TURBINE** (rpm) · **OUTPUT SHAFT** (rpm) · **POWER** (hp, sparkline) |
  | D | **TORQUE** (`engine.crankshaft_torque`, lb-ft) · **TARGET TORQUE** (`engine.target_crankshaft_torque`, lb-ft) · **ODOMETER** (held value with its time; smaller `--tire` numerals so six digits fit a third of the width) |

- **Landscape** (4 columns × 4 rows): `limit acc acc gear` / `field field power odo` /
  `radar radar turbine output` / `radar radar torque target`. On a phone (≤ 640 px) it has 2
  columns (`limit acc` first) and scrolls, like page 1.
- **Radar aim footer** (said once):
  - live: `within ±1°`, `near the ±1° limit` (≥ 0.8°) or `outside ±1°`;
  - otherwise: `Not reading · last 3:10 pm` with the values dimmed, or `Not reading` when there is
    nothing to show or the reads are not commissioned.
  - The numerals never change colour; the System card keeps the badge, peak and margin.
  - The windows come from status-lite (already in `STATUS_LITE_KEYS`), with `status_full` as the
    fallback. No server change was needed.
- **Data rules.** The gear estimate and the radar angles are candidate quality, so they are read
  from the metric record: current means `available && !stale`. The ACC state, mode, distance bars
  and lead-vehicle flag are `verified` but are strings, a small integer and a boolean, so they are
  read from their records the same way. The speed limit and the set speed are `verified`
  (owner-referenced) and use `liveValue`. The broker keeps a value until its 10 s staleness, so the
  state gates everything else: `off` or `ready` hides a set speed, `vehicle ahead` shows only
  while engaged or in override, and nothing beside the state shows without a current state.
  `vehicle ahead` comes first in the sub-line, which ends in an ellipsis on a narrow tile. The
  other page-2 tiles use the
  normal `Tile` rules (live, held with its time, or `—`). `generator.field_duty` joins
  `SPARK_METRICS` (minimum span 20 %). No new timers were added.
- **Customisation.** The page-2 tiles are in `DRIVE_TILES` with ids `limit`, `acc`, `field`,
  `gear`, `radar`, `turbine`, `output`, `power2`, `torque`, `target` and `odometer`, each labelled
  `… (page 2)`. They
  hide like page-1 tiles, and a hidden tile leaves its cell empty.
- Caveats for these tiles are in `docs/caveats.md` (Drive and engine, Electrical, Radar alignment).
- The core bundle grew by about 130 B for the two registry entries and the page-1 speed-limit
  sub-line (91,749 B of 92,160 B on 2026-09-27); the tiles themselves live in the lazy chunk.
  The warning-sentence fixes of the same day (section 7.1) took the core to 92,107 B and the
  shared chunk holding `warnings.js` to 46,069 B of 46,080 B, so both budgets are nearly spent.

### 3.2 Parked

A scrolling list of cards, in the device's order. The first screen carries everything the owner
checks when stopped:

- **BATTERY**
  - The reading has 2 decimals, at the big-reading size, and is dimmed when held.
  - The sub-line comes from the sample itself, for example `read 4:25 pm · 2 h after engine off`. It
    says "resting" only when the sample qualifies.
  - The band follows the gating in section 5 (settled samples only; otherwise neutral).
  - A 24 h trend (inline SVG, attributes only) comes from the summary's 15-minute buckets. Gaps
    longer than three buckets break the line.
  - **`Read voltage now`** has these states: idle, `Reading…`, `Refused: <plain reason>`, and done,
    which triggers a resync. The request is receive-only (`{"mode": "passive"}`): it never transmits
    and cannot wake the van. When the van is asleep it is refused with "there is no voltage traffic
    to read". The button is disabled with a one-line explanation when the listener does not permit
    acquisition (`web.active_acquisition_enabled` false).
- **TIRES**: the last reading per wheel, with one time for the set. In v2.0 wheels are coloured only
  by the absolute floors (front < 50 psi, rear < 68 psi) and by an active `warning` rule. The
  cold-baseline comparison arrives with the server baseline in v2.1 (section 8).
- **LAST TRIP**: `65 min · ended 4:25 pm`, plus the open trip while one runs. Per-trip statistics
  (distance, maxima) arrive in v2.1.
- **SERVICE** (the shared `ServiceCard`):
  - Odometer: live, or dimmed with its time. An estimate (the ICS source) carries a small `*`
    explained on the docs page.
  - Last oil change, and miles since service. Miles since is shown only when the odometer and the
    record share a mileage source. When the recorded mileage is above the odometer, the card reads
    "Check the entry" instead.
  - An Oil life row, only when `maintenance.oil_life.available`.
  - `Record oil change` opens the sheet. It has four fields, `mileage_source` is `unknown` when the
    mileage is blank, and `request_id` makes saves idempotent. Save is disabled unless storage is
    persistent, and `storage_error` is shown.
  - A history disclosure of saved records.
- **WARNINGS** summary: the open count, or `Nothing open · last drive 4:25 pm`. **CODES** summary: the
  number of current codes with the first two titles. Both open Health.

### 3.3 Health

- **Early warning** card:
  - Open items use the row template (section 7.1). Each row has `Details` (the event dialog) and
    `Ask Codex`.
  - Header buttons: `Event history` and `Add early warning` (the Codex proposal flow; backend
    unchanged).
  - Unconfirmed items (`Unconfirmed · engine off since 4:25 pm`) sit in a collapsed disclosure.
  - The footer shows one delivery line (`Phone alerts: 2 pending`) only when something is pending or
    failed, plus a `How warnings work` link to `/docs/warnings.html`.
  - `Ask Codex` appears only when `web.warning_chat_enabled`.
- **System notes** (collapsed): infrastructure and telemetry-quality items in `watch`/`warning`,
  with the backend's cause-specific titles. They are never counted in the badge. Data-quality
  incidents ("sample filter", "implausible reading") appear here with a `Recovered` sub-disclosure
  and `Ask Codex` (kind `quality`).
- **Diagnostic codes**:
  - The badge is the number of current codes.
  - The `Current` group is expanded: code (`fca_display`), module, reviewed title, last seen.
  - `Pending`, `Confirmed history`, `Test not completed`, `Other status combinations` (only when
    `group_counts.other > 0`) and `Modules · n of m answered` are collapsed.
  - A group longer than the cache says how many of how many are shown.
- **Guarded scan**, inside the codes card, only when `web.dtc_jobs_enabled` (the Tailscale
  listener):
  - A parked-confirmation checkbox gates `Scan`.
  - Scan also requires: no active job; `state !== "restoration_failed"` (otherwise it shows
    "restoration unverified — inspect before retry"); a legacy one-use arm token when
    `dtc_jobs_require_local_one_use_arm` is set; and a confirm dialog.
  - The POST body is exactly `{confirm_parked, confirm_park_gear, confirm_ignition_on_engine_off}`.
  - `Cancel` is available while the job is queued, starting, created or running.
  - The status line shows the state, bus/module, imported count, cancellation and failure.
  - `/v1/diagnostics/dtc-jobs/current` is polled every 2 s while a job is active and the page is
    visible, and every 5 s after an error. On completion it refreshes, including `/v2/summary`.
  - A scan is never offered on the LAN listener, and codes are never cleared.
- **Service**: the same `ServiceCard` as Parked.

### 3.4 History

- **Trips**: the current trip (if open) and the last five, with duration and end time. Distance and
  max speed appear when per-trip statistics exist (v2.1).
- **Trends**: one compact card per history metric, with a plain-English label rather than the raw
  metric name. Each shows the 7-day and 30-day low/average/high, the typical range of prior trips
  (median and spread), and the current-trip difference while driving, plus a 24 h sparkline with a
  time-based x-axis (gaps visible).
- **Coverage**: one line, `Recording · last sample 4:25 pm`, or the degraded reason, once. A second
  line appears only when it has news. It draws on `coverage.age_seconds`, `last_snapshot_at`,
  `active_interface_gaps` while running, `coverage.retention`, and `maintenance_hook.last_error`.
  Interface and metric gaps while parked are expected, so they are not shown as a problem.

### 3.5 System

- **Vehicle state**: one line (`Asleep · bus silent since 4:25 pm`), with a disclosure holding the
  broker's basis, confidence and detail text.
- **Buses**:
  - Three rows, such as `C-CAN · listening · 500 kbit/s`, each with a status dot. `ERROR-ACTIVE` is
    rendered as `controller normal`.
  - A `Safety inhibits` line and a current-owner line.
  - Adapter identity, controller state and reconcile details sit behind a disclosure.
- **Broker**: collector state, history recorder, supplemental cache age, last-readings storage, and
  active/auxiliary drive helper state. These come from `status_full` (60 s cadence); counters are
  never rendered live.
- **Radar alignment**:
  - The badge is one of three reference states: within ±1°, approaching, outside.
  - Per axis: the latest estimate, then in a disclosure the 1-min mean (n), 5-min mean (n), 5-min
    peak |angle|, the margin to ±1.00°, and the not-commissioned detail.
  - A held value carries its time.
- **Metric catalog**:
  - Every metric shows its value and unit, a dimmed held value with its time, or `—`, plus source,
    bus, quality, freshness limit and retention policy. This is where the provenance vocabulary
    lives.
  - `vehicle.ignition_on` renders `on` or `—`, never `off`.
  - `diagnostics.*.raw` metrics and candidate-quality metrics (for example target torque and the
    shaft speeds) sit in a collapsed `Diagnostic-only` disclosure.
- **Customise this device**:
  - Per view, show or hide cards, and on the scrolling views move them up or down; reset.
  - Drive tiles can be hidden but not reordered.
  - Width, pairing and swapping from the old editor are retired, because the grids are fixed.
- **About**: links to `/docs/caveats.html` and `/docs/warnings.html`, a `Reload app` button
  (`location.reload()`, needed when there is no browser chrome), the build id and the listener bind.

## 4. Visual system

- Flat, opaque surfaces: page `#0e1412`, card `#172120`, card border `#26332f`, raised row `#1d2a27`.
  There are no gradients, `backdrop-filter`, shadows, fixed overlays or translucent fills.
- Text: primary `#f3f6f4`; secondary `#aab6b1` (≥ 7:1 on a card); dimmed/held `#7f8b87`.
- Semantic colours: amber `#f5b642`, red `#ff5d4d`, green `#5fd39a`. The neutral normal-band colour
  is `#52665f`.
- `.dim` (night) redefines the tokens: primary text becomes `#aab6b1`, and the band and lines are
  darker. Red and amber are unchanged.
- Type: `Roboto, system-ui, "Segoe UI", sans-serif`, with `font-variant-numeric: tabular-nums`.
  Roboto comes first because on One UI `system-ui` resolves to SamsungOne, which may lack tabular
  digits.
- Scale: hero, large, tile and tire values are clamped against viewport height (up to 128, 72, 56 and
  40 px). Body text is 16 px, labels 14 px (uppercase allowed, letter-spacing ≤ 0.04 em), and
  tertiary text 13 px. **Nothing is under 13 px.** Values never wrap, and units are 40 % of the value
  size.
- Containment:
  - `.tile` has `contain: layout paint style`.
  - `.card` has `contain: content; content-visibility: auto; contain-intrinsic-size: auto 220px`, so
    off-screen cards cost nothing while scrolling.
  - Grids use `minmax(0, 1fr)` tracks, so a digit change can never resize a track.
- Spacing: 16 px page gutter, 12 px grid gap, 16 px card padding.
- Tap targets are ≥ 44 px (tabs 64 px, top-bar buttons 48/44 px, buttons 48 px, disclosure summaries
  48 px).
- Breakpoints:
  - ≤ 640 px: phone; Drive has 2 columns and scrolls.
  - 641 px and wider in portrait: the portrait tablet, which is the design target.
  - Aspect ≥ 1 and width ≥ 900 px: Drive has 4 columns.
  - ≥ 1024 px: the scrolling views use 2 card columns.

## 5. Bands and colour rules (from 57 trips of rollups, September 22)

Bands colour the band bar's marker and, only when red and live, the value. They are display context,
not alarms; alarms come from the broker's assessments (section 7). The red thresholds are the same
numbers the warning tiers use (section 7.2), so a tile never turns red while the rule set calls the
value normal. Outside the normal band but short of amber there is a neutral zone: the value is
outside the typical envelope, but no rule applies. Intervals are contiguous, and every value falls
in exactly one band.

| metric | span | normal | amber | red | neutral |
|---|---|---|---|---|---|
| coolant | 60–240 °F | 160–220 (213–220 is the thermostat-open hot-day idle plateau, reached in 13 of 57 trips) | 221–229 | ≥ 230; ≥ 240 critical | < 160 (warm-up) |
| transmission oil | 60–240 °F | 80–183 (183 is the observed highway maximum) | 184–229 | ≥ 230 | < 80 (cold) |
| oil pressure | 0–100 psi | from the rpm floor up to 85: < 1,000 rpm ≥ 15; 1,000–3,500 rpm ≥ 22; > 3,500 rpm ≥ 55 (the two-stage pump's 65–85 psi mode is normal) | below the rpm floor but ≥ 12 | < 12 (critical) | > 85; and always when rpm is not live, rpm < 400, or coolant < 160 °F |
| voltage, running | 10–16 V | 13.6–14.6 | < 12.8 (`reduced charge`; the PCM lowers charge voltage routinely, so this is not red) | only while a charging-failure warning is open (< 12.0 V for 60 s with alternator ≥ 90 %) | 12.8–13.6 and > 14.6 |
| voltage, parked (settled) | 10–16 V | ≥ 12.2 | 11.8–12.2 | < 11.8; < 11.5 critical | — |
| tires (v2.0) | per wheel | — | — | below the absolute floor: front < 50, rear < 68 psi; or an active warning rule | everything else |
| VVT oil temperature | 60–240 °F | ≤ 219 (observed max) | 220–229 | ≥ 230 | — |
| generator duty | 0–100 % | text only, no band | — | — | — |
| rpm / speed / power / torque | 0–4,500 · 0–90 · −30–260 · −60–240 | no band | — | — | — |

**Voltage band gating** (`app/derive.js` `voltageMode`):

- **Running table**: only after live RPM ≥ 400 has been continuous for 10 s.
- **Parked table**: only for samples taken ≥ 30 s after the last running evidence. That evidence is
  the page's own engine tracking or the historian's `last_active_at` for the current or most recent
  trip.
- **Neutral**: everything else, which means ignition on with the engine off, cranking, the first 10 s
  after start and the first 30 s after stop. Key-on readings (p50 11.90 V) and just-stopped readings
  are therefore never coloured.

**Tires in v2.1**: once the server publishes a per-wheel cold baseline, the table becomes: normal
within −3/+8 psi of the wheel's baseline; amber from −3 to −5 psi, or when an axle pair differs by
more than 3 psi after the usual offset; red below −5 psi; neutral above +8 psi. `bands.js` already
implements this path. It stays unused until a baseline exists, and tires are never compared against
`days_*` aggregates.

**Colour rules**:

- The bar draws the normal band in the neutral colour, with the marker in white, amber or red.
- Numerals change colour only for a confirmed warning (warning state or critical) on that metric,
  or a live value in a red band.
- The alert in the top bar follows the same two triggers.
- A held value's marker keeps its band colour, so a parked tire below its floor still shows red,
  but its numerals stay dimmed.

### 5.1 Time and number formats

- **Time zone.** The van's zone is US/Mountain and the tablet shares it. The app formats in the
  browser's local zone with fixed formatters. It never uses `toLocaleString`, which is slow on old
  Android and printed en-GB dates in the Pi audits.
- **Times and durations.** Times are 12-hour lowercase: `4:25 pm`, `yesterday 4:25 pm`,
  `Sep 22, 4:25 pm`. Durations read `32 min` or `1 h 05` on tiles and in tables. Inside a
  sentence every unit is written (`fmtDurationLong`): `1 h 05 min`, and from 48 h `2 d 8 h`.
- **Ages** are computed at `minuteClock` resolution: `now` under a minute, then `2 min`, `1.4 h`,
  `2 d`. No view renders a per-second age, `collector.cycles` or `elapsed_seconds`.
- **Decimals.** Numbers use tabular digits and fixed decimals per metric:
  - 0: speed, rpm, temperatures, pressures, power, torque, average power and generator duty.
  - Voltage: **1 on the Drive tile, 2 on the Parked battery card** and elsewhere.
  - Radar angles: 2. Trip distance: 0.1 mi.
- **Thousands separators** for rpm and odometer. Negative numbers use a typographic minus.

### 5.2 View auto-selection

- Automatic mode picks **Drive** when `engine.rpm` is live and driver-qualified, or when
  `vehicle_state` is verified, fresh (≤ 5 s) and `moving`/`running`/`ignition_on`.
- It picks **Parked** when the state is `asleep` or `parked` and fresh (inferred confidence is
  enough).
- Otherwise it keeps the current view. Awake, unknown or stale states never flip views, so fob wakes
  and voltage reads do not bounce the screen.
- A tab tap is a manual choice. It turns automatic mode off **until the engine next starts or stops**
  (`startEngineTracking` re-enables AUTO on the next running↔stopped transition), or until the Auto
  toggle is tapped.
- Both choices persist per device.

## 6. Freshness contract (implemented in `link.js`, verified by unit tests)

The numbered MUST rules from the API-contract audit are kept verbatim in
`dashboard/docs/freshness-contract.md`. Summary:

- **HTTP baseline.** The baseline comes only from HTTP. Validate `web_delivery`, and bound the HTTP
  round trip at 2 s.
- **Server clock.** Map the server's monotonic clock with a midpoint offset plus uncertainty.
- **Stream acceptance.** Accept a stream event only when all of these hold: it comes from the
  established instance; it is in order; its delivery age is ≤ 10 s; and it would not expire any
  available metric or verified vehicle state.
- **Ageing.** Add the delivery age to every `age_ms`, and mark a value stale past
  `stale_after_seconds`. A verified vehicle state older than 5 s becomes unknown.
- **Watchdog.** Every 1 s it advances ages with `performance.now()` deltas and re-renders only the
  affected bindings.
- **Resync.** A stall over 3 s, a stream error or an instance change triggers an HTTP resync. A
  visibility change or `pageshow` invalidates everything and resyncs.
- **Display policy.** Held values follow `RETAIN_LAST_READING`. Ignition is positive-presence only.
  Automatic view selection needs a verified, fresh state.

v2 adds two rules. Rule 19: a `catalog_hash` change triggers an HTTP resync. Rule 20: status fields
read from `status_full` are at most 60 s old. A manual refresh (connection dot) follows the same
resync path, then forces a summary fetch.

## 7. Warnings

### 7.1 Presentation (v2 frontend, works against today's backend)

- **What counts as a warning.** Only `vehicle_health` and owner custom assessments with
  `state ∈ {watch, warning}`, or open episodes, are warnings. `can_infrastructure` and
  `telemetry_quality` go to System notes. Episodes whose latest assessment is inconclusive render as
  `Unconfirmed · engine off since 4:25 pm`, and never say "unavailable".
- **Row template.** `<Metric> <value> <unit>, <comparison> for <duration>. <Action>.`, built
  client-side by `warnings.js`. Example: `RR tire 74 psi, 4 psi under its usual 78 for 5 min. Check
  pressure at the next stop.`
  - The template table is the warnings-audit E table, with one action per rule key.
  - The comparison uses `baseline.median`, else `absolute_threshold`, else `custom_rule` (a generic
    owner template).
  - The duration is omitted, reading `just now`, until `observed ≥ required`.
  - `null` is never rendered.
  - `over` / `under` follow the two numbers shown, never the rule direction; equal rounded
    numbers read `at its usual 78`.
  - A tier-2 notice is the broker's `reason`, capitalised, then the action, under the broker's
    `title`: `RR cold pressure 2.9 psi a week below RL's over 4 cold starts. Check the RR tire
    for a slow leak.` It has no duration clause; the card's `since` label carries the time.
- **Tiers.** `watch` shows only on the Health card. `warning` state and critical severity also drive
  the top-bar alert and the numeral colour (sections 3 and 3.1). The Health tab badge counts open
  items and turns red when any is critical.
- **No evaluator vocabulary on screen** (regime, MAD, deviation, persistence counts, episode
  numbers). `warnings.js` rejects them in tests, and all of it stays in the event dialog's evidence
  sections.

### 7.2 Backend redesign (separate workstream, same repo; see `docs/warnings-redesign.md`)

- **Tier 0**: absolute rules with OEM or owner context and hysteresis. They cover low oil pressure by
  rpm band, hot coolant, hot transmission, charging failure with generator-duty corroboration, low
  battery while parked, a low tire (absolute), and tire pair asymmetry.
- **Tier 1**: relative rules with sigma floors, larger minimum effects, a tire "phase" regime, one
  combined tires rule, and de-escalation at 70 % of the threshold.
- **Tier 2**: a weekly drift job over rollups (slow leak, idle coolant creep, oil-pressure decline,
  resting voltage).
- **Tier 3**: system items, demoted and never repeated.
- **Notifications** are grouped by component, with per-trip caps, cooldowns and quiet hours. Only
  tier 0 repeats.
- **Validation**: offline replay against exported historian trips before any broker restart.
- The v2 frontend consumes the new `group`/`tier`/`action` fields when present, and falls back to
  the templates above otherwise.

## 8. Data the frontend needs that the broker does not expose yet

These are handled in the web tier first, and in the broker later if worth it:

1. **Drive sparklines** come from the client ring and start empty after a load. Seeding them needs a
   `recent_series` (about 15 min at the collector cadence) in `/v2/summary` (v2.1). The 900 s
   `sparkline` buckets are too coarse and have no rpm, speed or torque.
2. **Per-trip statistics** for the last five trips (distance, max speed, max coolant, minimum
   voltage) come from the historian audit's `trip_summaries` loader (~110 ms per trip, cached by trip
   id), exposed through `/v1/history` and `/v2/summary` (v2.1). Until then, Last trip reads
   `65 min · ended 4:25 pm`.
3. **TPMS cold baseline per wheel** is a server product: `baseline{cold_median, mad, trips,
   computed_at}` from the historian audit's D.3.3 cold-regime query (56 ms). It cannot be derived
   from `summary.history`: every stand-in (`days_30.mean`, `prior_trips.median_of_trip_means`,
   `days_30.minimum`) is wrong by 2–3 psi against the true cold median. Until it exists, the client
   does not colour tires against any baseline.

## 9. Gear and other mapping follow-ups

The RPM tile's footer is reserved for a gear value. The DID-mapping audit ranks a ratio-derived gear
estimate as the best offline-testable candidate: `0x1F7` turbine and output speeds against the ZF
9HP nominal ratios. If the offline analysis passes the evidence gate, it becomes a broker metric,
`transmission.gear_estimate`. The footer shows it only while the metric is live at a driver-qualified
quality. A candidate-quality estimate stays in the System catalog's Diagnostic-only disclosure.
Ambient temperature, wheel speeds, and brake and turn-signal leads follow the same route; none of
them changes the v2 layout.

Status 2026-09-27: `transmission.gear_estimate` is a deployed broker metric at **candidate** quality
(`0x1F7` shaft-ratio bands R/1–7 via `tools/gear_ratio_lookup.py`; `R` is suppressed above 6 mph
because 1→2 upshifts cross R's band). It was observed live on 2026-09-24 (7th at 50 mph, ratio
0.699). The RPM footer still waits for a driver-qualified estimate; the offline evidence is in
`projects/ecu_mapping/findings/promaster_2022/2026-09-22_gear_and_oil_life_offline.md`.

## 10. Verification

- **Step 0, tablet probe** (not built): a static probe page with a flat Health mock
  of about 400 nodes. It reports UA, DPR, `innerWidth×innerHeight`, `display-mode` and rAF gaps
  during a flick scroll. The owner opens it on the tablet and sends a screenshot. If it stutters,
  re-examine the rendering assumptions.
- **Node suite**: `npm test` (`node --test test/`) in `dashboard/` is the primary gate on the Pi.
  - It covers freshness rules 1–17, 19 and 20 (rule 18, the compute budget, is checked in the
    browser), the link and store end to end, and bands, including voltage gating and the contiguous
    intervals.
  - It covers warning templates and forbidden words, `autoView` start and stop sequences plus the
    Read-voltage wake, settings, and format.
  - It covers the view helpers (`parked`, `health`, `dtcScan`, and History/System as they land) and
    the dialog helpers.
  - The build smoke test (`test/build.test.mjs`) builds into a temp dir and asserts `index.html` and
    hashed js/css, the size budgets, no `style=` in built HTML, and the docs allowlist.
- **Python**: `tests/test_web_v2.py` covers routes, the traversal guard, gzip, cache headers, stream
  shape, summary trimming (uncapped warning lists, `omitted_count`), and origin behaviour on port
  8765. `tests/test_dashboard_v2_node.py` runs the node suite from the Python runners, and skips
  without Node 20+ or `node_modules`. Both run inside `pi_compute run repo-tests` as the regression
  gate. Owner action: add `node_modules` and `dist` to van_compute's ignored directories, because the
  snapshot otherwise ships 13 MB of dependencies.
- **Browser**: `tools/shoot.mjs` (headless Firefox over WebDriver BiDi) screenshots and measures
  every view at 800×1280 and 1280×800. It checks:
  - document height (Drive ≤ 1 screen in portrait) and DOM nodes (Drive ≤ 350);
  - text under 13 px, tap targets under 44 px, overflow/overlap hits and console errors;
  - a MutationObserver/rAF sample on Drive (≤ 12 writes/s live, 0 when unchanged, no rAF gap over
    50 ms during a scripted scroll).

  Still to add: a recorded live-drive replay, with `window.__perf = {signalWrites, textWrites,
  componentRenders}` counters and `componentRenders == 0` over 10 s of replay.
- **Parity checklist** (not run as a formal pass before the swap): every control in the former static
  app's `index.html`, `event-history.js` and `warning-chat.js` (git history, commit `fa966ba`) is
  reachable in this app. The existing dialog helper tests cover the event and chat
  features (filters, replay, annotations, export, access code, model/effort, saved warnings,
  retry/stop).
- **Manual on the tablet**: a `chrome://inspect` performance profile while flick-scrolling Health.
  Paint flashing should show no full-card repaints.

## 11. Deployment (owner actions)

The dashboard is live on port 8765 (2026-09-27 swap). `dist/` is gitignored build output served
directly by both web units, and the build publishes atomically, so a build is the frontend deploy
step:

```bash
cd /home/pi/dev/obd-things/projects/vehicle_data/dashboard && npm ci && npm run build
cat /home/pi/dev/obd-things/projects/vehicle_data/dashboard/dist/build.json
```

No restart is needed for frontend changes. Restart the two units only when `web_v2.py` or `web.py`
changed (the service refuses to start without `dist/index.html`, and the units restart every 5 s on
failure):

```bash
sudo systemctl restart van-telemetry-web.service van-telemetry-web-tailscale.service
systemctl status van-telemetry-web.service van-telemetry-web-tailscale.service --no-pager
curl -sI http://vanpi.lan:8765/
curl -s http://vanpi.lan:8765/v1/snapshot | head -c 200; echo
curl -sI "http://$(tailscale ip -4):8765/"
```

A manual trial on another port, as user `pi`, leaves the live units untouched:

```bash
cd /home/pi/dev/obd-things && python3 projects/vehicle_data/web_v2.py \
  --socket /run/van-telemetry/api.sock --bind 127.0.0.1 --port 8799 \
  --static-root projects/vehicle_data/dashboard/dist
```

Record the build id from `dist/build.json` in the deployment note.
