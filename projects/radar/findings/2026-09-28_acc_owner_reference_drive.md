# ACC owner-referenced drive — 2026-09-28

This records the owner's cluster observations for the drive of 2026-09-28 and the raw-frame
analysis that checked them. It answers the "Remaining owner/recording asks" in the
[September 27 owner-referenced drive](2026-09-27_acc_speed_limit_owner_reference.md) and
promotes most of the Tier 1 candidates of the
[September 24 passive mapping](2026-09-25_acc_passive_mapping.md). The analysis used saved
captures only. Nothing was transmitted, and no service, link or configuration was changed for it.

## Owner's notes (as given, cluster clock)

| # | asked | owner's note |
|---|---|---|
| 1 | Follow distance: bars after each press | "at 7:00 PM I incremented the distance from one bar to four back down to one in five second intervals" |
| 2 | Lead-vehicle icon on/off | "at 7:11 I had a vehicle in front of me for 15 to 20 seconds and the icon show"; "7:44 there was a vehicle until just before 7:47. Towards the end of that time, I slowed down the ACC speed until the vehicle icon disappeared" |
| 3 | ACC off/on by button | "at 7:01 I turned the AC off and then back on about 10 seconds later" (ACC is meant) |
| 4 | Regular (non-adaptive) cruise | "at just about 705 I turned the regular cruise control on and set it to 65 mph. Cancel it and turn it off in five second intervals." |
| 5 | Accelerator override | "at 7:08 I pressed the gas and overrode the ACC for maybe 15 seconds. The ACC speed on that console and blinked during this time." |
| 6 | RES after a cancel | "at 7:10, I resumed the speed to 61" |
| 7 | SET− before and after | "around the turn of 714 I pressed resume and went from 52 to 61 mph"; "at 7:19, I resumed to 63 mph from 53 mph" (the owner describes resume presses, not SET−) |

Owner's remark on speed: "the speed displayed on the app is more accurate than the speed displayed
on the console. The app is consistent with my GPS. The IPC console usually says about 1 mph faster
than I am actually going."

## Time zone and capture coverage

- The notes are again in **CDT (UTC−5)**, as on September 27; the Pi logs in MDT (UTC−6). So
  7:00–7:47 PM in the notes is 00:00–00:47Z on 2026-09-29. The cross-check below confirms it: read
  as MDT, the notes would not match any recorded transition.
- Historian trip 67 opened at 23:11:37Z and was still open at 01:47Z.
- Capture set: `/mnt/EXFAT512/obd-things/tmp/captures/three_bus_drive/broker-drive/broker-drive-20260928T231134826208`.
  Its `route-events.jsonl` shows B-CAN and CAN-CH segment 0 from 23:11:34Z to 01:38:30Z (B-CAN
  ended `route_lost`, CAN-CH `completed`), and the newest chunk on every role is `chunk_000014`,
  written at 01:38Z. **Every note falls inside that coverage.** Whether further segments followed
  01:38:30Z was not checked.

## Method and working outputs

Seven bounded extractions ran through the `can-event-window` compute task (jobs
`20260929T220842Z-06337e4c` to `20260929T220846Z-4ec6dbba`): `0x5A0`, `0x4AF` and `0x5A5` for the
whole drive, every `0x2FA` frame for the whole drive, and `0x101` speed and the `0x1FA` brake
candidate for 23:59:30–00:21:30Z and 00:42:30–00:48:30Z. Gitignored outputs and the offline
scripts are in `tmp/radar/acc-passive-20260928/` (`presses.csv`, `press_effects.csv`,
`acc_5a0_transitions.csv`, `acc_4af_transitions.csv`, `analysis.json`, `scripts/`).

`0x2FA` integrity over the drive: 440,072 frames, CRC-8/SAE-J1850 over B0–B1 correct on all of
them, no counter break, B3 always `0x0E`.

## The callouts against the raw frames

Times are CDT as the owner noted them; UTC is five hours later.

| # | owner's note | raw frames | result |
|---|---|---|---|
| 1 | 7:00, distance one bar to four and back, 5 s apart | `0x2FA` B1 bit7 at 7:00:07.90, :15.00, :21.38 stepped the graphic index 10→11→12→13; B0 bit6 at 7:00:26.62, :31.18, :35.28 stepped it 13→12→11→10 | six presses, six steps, 4–7 s apart. Bars = index offset + 1. B1 bit7 is Distance Increase, B0 bit6 is Distance Decrease |
| 3 | 7:01, ACC off, on again after about 10 s | B0 bit0 at 7:01:39.46: engaged → off (index 1, popup `0x35`), `0x4AF` engaged flag cleared. B0 bit0 at 7:01:53.72: off → ready (index 2, popup `0x36`) | match, 14 s apart. First observed switch-off |
| 4 | about 7:05, regular cruise on, set 65, cancel, off, 5 s apart | B0 bit1 at 7:04:55.93: off → ready, index 28. SET+ at 7:05:04.01: engaged, index 29, set speed 65. CANC at 7:05:14.09: standby, index 27. B0 bit1 at 7:05:22.07: off, index 31 | match in order and value, 8–10 s apart. B0 bit1 is the fixed-cruise on/off button; indices 27–32 appear nowhere else in the drive |
| 5 | 7:08, gas override about 15 s, set speed blinking | state override (index 23, popup `0x4A`) from 7:08:06.88 to 7:08:33.56 while speed rose from 57.4 to 65.7 mph over a set speed of 58 | match; the override lasted 27 s |
| 6 | 7:10, resumed to 61 | CANC at 7:09:53.29 (standby, 61 kept), RES (B0 bit3) at 7:10:09.17: engaged at 61 | match |
| 2 | 7:11, vehicle ahead 15–20 s, icon shown | brake cancel at 7:10:53.96; RES at 7:11:20.75 engaged into index 6; indices 6–9 until a brake cancel at 7:11:51.28. `0x4AF` B3 bit2 set at 7:11:34.97–40.65 and 7:11:49.79 | match; the target family lasted 30.5 s |
| 7 | about 7:14, resume, 52 to 61 mph | SET+ at 7:13:10.13 engaged at 52 (51.6 mph), ten SET+ presses to 62, CANC at 7:13:44.25, RES at 7:13:55.35 engaged at 62; speed 56.0 → 60.5 mph | match in time; the set speed was 62, the road speed reached 61 |
| 7 | 7:19, resumed to 63 from 53 mph | brake cancel at 7:17:43.93; RES at 7:19:13.95 at 53.5 mph: engaged at 63 | match in both values |
| 2 | 7:44 to just before 7:47, vehicle ahead; set speed lowered until the icon went out | index 13 → 9 at 7:44:16.09; fourteen SET− presses from 7:46:19.67 to 7:46:53.67 took the set speed 66 → 52; index 9 → 13 at 7:46:56.21 | match; the icon family ended 2.5 s after the last SET− |

The owner also pressed the distance buttons thirteen times at 7:11:31–45 and eight times at
7:44:51–57 without noting it. Those presses stepped the index inside the target family (6–9) and
stopped at both ends.

## Earlier coarse check, corrected

The first version of this file compared the notes with the five-second historian. Two of its
readings were wrong and are replaced by the table above:

- "state reads `off` while fixed cruise is in use" is false. The state was ready, engaged and
  standby during fixed cruise; the historian had no sample of the 27 s episode's first part.
- The override at 00:06:51Z was a separate 16 s episode right after a SET+ with the accelerator
  still down. The owner's 7:08 override is the one at 00:08:06Z.

The historian's `acc.state` series shows no `off` between 00:04:23Z and
00:04:55Z, where the raw frames have one. The September 29 follow-up established
that this particular loss is upstream of the five-second historian cadence:
the broker's saved observation time remained 00:00:40.322708Z, with `engaged`,
until a new `off` observation at 00:05:22.138837Z.

### September 29 capture reliability audit

Read-only trip-67 queries, restricted to `vehicle_running=1`, reproduced:

| metric | fresh / running snapshots | percent |
|---|---|---|
| vehicle speed | 2,239 / 2,239 | 100.000% |
| oil pressure | 2,239 / 2,239 | 100.000% |
| speed limit | 1,863 / 2,239 | 83.207% |
| ACC state | 998 / 2,239 | 44.573% |
| ACC set speed | 993 / 2,239 | 44.350% |

Among distinct ACC-state observations in those running snapshots there were
41 gaps over 15 s and 35 over 30 s; the maximum was 1,309.053 s. The earlier
prompt's 48 gaps over 15 s was not reproduced with this running-only filter.
`tools/acc_freshness_audit.py --trip 67` reproduces this bounded query and saves
`tmp/vehicle_data/acc-freshness-trip-67.json`.

Independent raw check: `can-event-window` job `20260929T225356Z-95b780ad`
read only C-CAN full chunk 5. Its 00:01:35.582237Z–00:05:22.578330Z window
contains 263 `0x5A0` frames, median spacing 0.999986 s and maximum spacing
1.003830 s. It includes `off` at 00:04:23.678571Z, fixed-cruise ready at
00:04:55.959186Z and engaged at 00:05:04.237949Z. The cluster was transmitting
through the broker's observation gap.

The source code supplies a mechanism for this loss: each active helper cycle
opens a bounded broadcast socket for at most 0.35 s and is scheduled about once
per second; `LowRateFrameWait` cannot extend its deadline, and backs off for
30 s after repeated misses. The raw/historian comparison proves capture loss;
exact phase locking is a supported explanation rather than a measured helper
socket trace. Other effects, including the late-drive host stall, are separate.

The September 29 receiver change continuously filters the two display IDs,
uses actual kernel receipt timestamps, and follows passive lease / armed-owner
rules. Its synthetic timing tests do not establish real-drive acceptance.
Post-deployment running-snapshot freshness remains unmeasured until a new drive.

## `0x2FA` buttons

| bit | button | presses this drive | effect in `0x5A0` | evidence |
|---|---|---|---|---|
| B0 bit0 | ACC on/off | 5 | off ↔ ready, engaged → off | owner note 3, both directions |
| B0 bit1 | fixed-speed cruise on/off | 2 | off → ready (index 28); standby (27) → off (31) | owner note 4; new |
| B0 bit3 | RES | 6 | standby → engaged at the remembered speed, 6 of 6 | owner notes 6 and 7, three presses |
| B0 bit4 | SET− | 37 | −1 mph per press while engaged | owner note 2 (set speed lowered) |
| B0 bit5 | SET+ | 92 | +1 mph per press; sets the current speed from ready or standby | owner-referenced 2026-09-27; note 4 (set 65) |
| B0 bit6 | Distance Decrease | 8 | one bar fewer, 6 of 6 above one bar; no change at one bar, 2 of 2 | owner note 1; new |
| B0 bit7 | CANC | 3 | engaged → standby, 3 of 3 | owner note 4 ("cancel it") |
| B1 bit7 | Distance Increase | 20 | one bar more, 9 of 9 below four bars; no change at four bars, 11 of 11 | owner note 1 |

The clamp at four bars explains the two unexplained presses of 2026-09-24 (17:54:28–29 MDT).
B0 bit2 has never been set.

## `0x5A0` graphic index

`index = B6 >> 2`. Each display state has its own run of indices, and the popup byte B1 is
`0x34 + index` for the message that announces a new display.

| state (`acc.state`) | index | mode | lead-vehicle icon | bars |
|---|---|---|---|---|
| off | 1 | — | — | — |
| ready | 2–5 | adaptive | — | index − 1 |
| engaged | 6–9 | adaptive | shown | index − 5 |
| engaged | 10–13 | adaptive | not shown | index − 9 |
| standby | 14–17 | adaptive | — | index − 13 |
| override | 23–26 | adaptive | not shown | index − 22 |
| standby | 27 | fixed cruise | — | — |
| ready | 28 | fixed cruise | — | — |
| engaged | 29 | fixed cruise | — | — |
| off (after fixed cruise) | 31 | — | — | — |
| override | 32 | fixed cruise | — | — |
| override | 37–40 | adaptive | shown | index − 36 |

Whole-drive checks (2 h 27 min, 304 display changes):

- Every state and index pair falls in the table except raw state 3, which lasts 80–240 ms while
  cancelling or switching off (0.7 s in total).
- The bars never changed without a distance press within one second, and they carried over every
  state change.
- Seven target episodes, 301 s in total. The owner noted two; both match. The other five were
  not noted: 6:30:48 (11 s), 6:40:15 (8 s), 6:59:24 (33 s), 7:48:23 (11 s) and 7:48:43 (48 s).
- Set speed km/h and mph agree on every frame.
- Cancel and override popups are their own codes: `0x46` for a brake or CANC cancel, `0x4A` for
  accelerator override.
- At the four set presses with a speed reference, the set speed was 0.3 to 1.2 mph above the
  `0x101` speed (mean 0.6). That agrees with the owner's remark that the cluster reads about
  1 mph high.

## Evidence tiers and promotion

| field | tier | basis |
|---|---|---|
| `0x5A0` state enum (off, ready, engaged, override, standby) | **Tier 3** | each state has an owner-noted event in this drive; replicated on four earlier drives |
| following distance, bars = index offset + 1 | **Tier 3** | owner-noted 1 → 4 → 1 matched six presses; clamps at both ends |
| lead-vehicle icon, indices 6–9 and 37–40 | **Tier 3** | both owner-noted episodes match to the second, including the moment the icon went out |
| cruise mode, fixed for indices 27–29 and 32 | **Tier 3**, one episode | four owner-noted steps in order with the exact set speed; the indices appear nowhere else |
| `0x2FA` B0 bits 0, 1, 3, 4, 6, 7 and B1 bit7 | **Tier 3** | owner-noted presses, see the button table |
| `0x4AF` B1 bit3 engaged, B3 bit2 ACC deceleration | Tier 1 (unchanged) | engaged flag followed every transition again; the deceleration bit was set only while following a vehicle |
| `0x5A0` B0, B4, B5 | unresolved | B0 `0x25`/`0x2D`, B4 `0x00`/`0x03`/`0x3F`, B5 `0x34`/`0x3C` |
| range and relative speed of the lead vehicle | not found | unchanged from the September 24 screen |

**Promoted 2026-09-29 (owner request):** `docs/bus-map.md` has the `0x2FA` button rows and the
`0x5A0` state and graphic-index rows. The broker publishes `acc.state` as `verified` and adds
`acc.mode`, `acc.follow_distance` and `acc.lead_vehicle`. Drive page 2 shows them under the ACC
set speed. Tests use the real frames of this drive (`tests/test_display_frames.py`). These are
what the cluster displays. No warning, notification or safety logic uses them.

## Still open

1. Indices 18–22, 30 and 33–36 have not been seen. The decoder publishes only the state for them.
2. Ready at two to four bars (indices 3–5) and fixed cruise were each seen on one drive.
3. Lead-vehicle range and relative speed are still not found on C-CAN or CAN-CH.
4. Speed: the owner finds the broker's `vehicle.speed` agrees with GPS and the cluster reads about
   1 mph high. `vehicle.speed` quality stays `observed_alfa_scale`; this is an owner observation,
   not a calibration.
