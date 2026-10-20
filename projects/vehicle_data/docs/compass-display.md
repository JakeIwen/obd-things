# Compass display and night dimming

The Drive footer shows only the compass letters, right-aligned opposite the
light icons on both pages. It uses a bold monospace font stack, distinct from
the dashboard's Roboto/system sans-serif. A retained direction stays visible
at 70% opacity. No direction has been observed yet means no letters; N is a
real value (index zero), never a fallback.

## Compass data contract

`vehicle.compass_direction` is an integer index 0–7, unit `direction`, from
`bcan.broadcast.0x088`. `compass.json` declares the shared ordered labels
N/NE/E/SE/S/SW/W/NW, exact frame ID/DLC, source and ten-second freshness period.
`compass.py` owns the registry definition and fixed decoder. It accepts only
standard `0x088`, DLC 5, in the observed `00 00 03 (C0 + 8*index) 00` family.
The owner-route-referenced direction labels are allowlisted as verified;
sector thresholds, true versus magnetic north, validity flags and sender
identity remain unmapped. Do not turn the index into a measured degree bearing.

The [owner video finding](../../ecu_mapping/findings/promaster_2022/2026-10-14_compass_direction_video.md)
is the evidence. This frame announces changes; gaps over five minutes were
observed. The ten-second freshness period concerns the age of the last actual
announcement, not proof that direction changed or that the sender failed.
There is no synthetic periodic sample, timestamp renewal or diagnostic query.

`compass_receiver.py` runs a continuous filtered receive socket **only while
the broker proves that its existing B-CAN auxiliary helper owns the armed
route**. It reuses `broker_secondary_route` and `admit_secondary_route`, including
fresh identity/interface checks, and also bounds the interval to the same
helper PID. `LocalBrokerStatusClient` implements the reused checker’s exact
status-GET request contract against the broker callback, without HTTP I/O.
Cancellation is rechecked after route validation before publication. It acquires no passive lease, so it cannot hold up the helper's
arming or restoration. It never configures a link, transmits or starts an owner.
With the helper disabled, blocked or stopped, no compass socket is opened.
This means engine-off accessory-only compass changes are not currently collected.

`display_receivers.py` coordinates the two receiver lifecycles. Its C-CAN
member retains the existing pause/resume and cooperative wake handoffs.
`display_receiver.receive_display_frames` shares timestamped, exact-ID/DLC
receive mechanics; the compass path additionally monitors receive queue loss.
`display_contract.display_results` validates all observations before the broker
updates its cache under the existing lock. Compass is not added to the C-CAN
active-helper or external-publisher allowlists.

Only actual kernel receipt timestamps can create a fresh reading. Socket
loss, malformed compass data, receive overflow, missing/old receipt evidence,
route/owner change or receiver shutdown invalidates the live cache. The
original dated last reading remains in `LastReadings` and its persistence file.
The owner explicitly requested keeping stale directions, including after a
restart or later drive. These restored values stay dimmed until a new accepted
announcement. A page reconnect likewise cannot promote a retained value to live.

The frontend uses its existing expiry and retention mechanisms. It shows the
same letters for live and stale values, with the latter slightly dimmer; the
accessible label/title identifies a retained value as last reported. No
additional tile, visible label, unit, needle or direction-dependent rotation
is added. The System catalog formats the index as letters as well.

## Dim button

Dim is the per-device night option. Previously it changed a few text/band
colours and applied 85% opacity to readings, while leaving card backgrounds
and red, amber, green and blue accents largely unchanged.

`dashboard/src/styles/night.css` now gives every shared palette colour a
substantially darker night value: near-black backgrounds, muted text, darker
separators, warning colours, light icons and charts. Dialogs inherit the same
tokens; solid alert badges and the chat bubble also have night treatments.
The old reading-only opacity rule is removed. The implementation uses colour
tokens rather than a full-screen compositing filter. Day colours are unchanged,
and the existing Dim setting still persists per browser/device.

This controls rendered app colours, not the physical tablet backlight or
browser/system chrome. OS brightness controls remain independent.

## Validation and activation

- Backend feature tests: 103 passed, 295 subtests, job
  `20261015T045333Z-008e8d25` (compass, display frames/receiver, lighting,
  broker pure internals and recorder routes).
- Final frontend: 491 passed; dashboard build job `20261015T050910Z-9faa46e2`,
  build `323cf2c5dc06`.
- Full Python run: 1,901 passed and 12 skipped. The new catalog fixture was
  updated; a corrected-worker retry passed 120 tests plus 39 subtests, with
  two 50 ms socket-output stress tests still failing. Comparison job
  `20261015T050402Z-eb654ea3` reproduces both failures with unchanged HEAD
  runtime on the same Mac worker (identical test names/errors). They are not
  introduced by this change.
- Code-shape ratchet: no regressions, new exceptions or parse errors, job
  `20261015T050007Z-2e36cc38`. Broker file and main function shrank; no baseline
  was changed.
- Final backend callback/catalog check: 27 passed, 125 subtests, job
  `20261015T050712Z-6a1f81dd`.
- Playwright CLI job `20261015T050710Z-dd81863d`: 12 cases across both Drive
  pages at 800×1280, 1280×800 and 400×800; no horizontal overflow, compass
  right-aligned on the light-icons row, different font, and stale NW retained
  at 0.7 opacity. Dialog colours match the dark palette. Final-build browser job
  `20261015T051221Z-8e93bebd` repeats all 12 cases without errors and verifies
  native checkbox accent `rgb(57, 127, 92)` in the dim dialog.

Frontend build `323cf2c5dc06` was published and verified through the LAN
listener on 2026-10-15 at about 05:14Z. Existing hashed assets were retained for
already-open clients; previous index/build metadata are under
`tmp/vehicle_data/compass-night/previous-6197d03e445a/`.

The broker restart is confirmed: instance `2026-10-15T09:42:45.536758+00:00`
loads the compass catalog entry and receiver status. On the October 15 09:52Z
read-only check all three telemetry services were active, with no automatic
restarts. No additional activation restart is required for this implementation.

The owner's ignition-on check had not produced a compass reading: the receiver
reported zero frames, no last receipt and `waiting for the B-CAN drive owner`.
The last ignition/RPM observations showed ignition on with RPM zero; by the
inspection the broker reported asleep. This is consistent with the implemented
engine-running-only observer. Ignition-on while stationary does not populate a
first direction. The first received direction announcement during a normal
engine-running interval remains the live-validation milestone. Once received,
the original dated direction is retained and dimmed after expiry or shutdown.
Evidence: `tmp/vehicle_data/compass-night/activation-20261015T095203.json`.
No historical video/capture value was injected as a live or retained observation.

## Strict review follow-up

The session review found and corrected a status-client contract mismatch before
broker activation. An ad hoc object exposed `status`, but the reused admission
checker calls `request("GET", "/v1/status")`; the named local client now provides
that exact read-only contract. A new real-admission test first reproduced the
failure (job `20261015T052626Z-7b774569`). The corrected backend passed 107 tests
and 295 subtests in `20261015T052745Z-554dec85`, including cancellation during
revalidation. Repeated optional loss-handler branches and an assembly-only
factory were removed. The published frontend is unchanged.

The final post-review code-shape ratchet (`20261015T053003Z-3d2d94da`) passed
without regressions or exceptions. Review reports are retained at
`/tmp/obd-things-thermo-2321/`; all in-scope findings were resolved. The backend was subsequently restarted outside the agent environment; the
confirmed instance and remaining live-validation milestone are recorded above.
