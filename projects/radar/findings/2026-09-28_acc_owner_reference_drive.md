# ACC owner-referenced drive — 2026-09-28 (notes recorded, raw-frame analysis pending)

This records the owner's cluster observations for the drive of 2026-09-28. It answers the
"Remaining owner/recording asks" in the
[September 27 owner-referenced drive](2026-09-27_acc_speed_limit_owner_reference.md). Only the
notes, the capture coverage and a coarse historian cross-check are recorded here. The saved raw
frames have not been analysed yet. Nothing was transmitted, and no service, link or configuration
was changed.

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

## Coarse cross-check against the historian (not the raw frames)

Source: `/var/lib/van-telemetry/history.sqlite3`, opened read-only, trip 67, metrics
`acc.set_speed` (`verified`), `acc.state` (`candidate`) and `vehicle.speed`. Samples are 5–6 s
apart, so a press shorter than that can be missing and times are late by up to one sample. This is
also the first live observation of the two ACC metrics since they were deployed on 2026-09-27.

| owner note (CDT) | historian transition (UTC) | reading |
|---|---|---|
| 7:01 ACC off, on again after about 10 s | none between 23:59:24Z and 00:05:19Z | not seen at this cadence; check `0x2FA` B0 bit0 and `0x5A0` state in the raw frames |
| 7:05 regular cruise on, set 65, cancel, off | 00:05:19Z set speed 73→65; 00:05:22Z state engaged→off; 00:06:46Z off→engaged, set speed 60 | value and minute match; state reads `off` while fixed cruise is in use |
| 7:08 accelerator override, about 15 s | 00:06:51Z engaged→override; 00:06:57Z override→engaged | one override only, about a minute earlier than noted and shorter |
| 7:10 resumed to 61 | 00:11:36Z set speed 59→61 | value matches |
| 7:11 lead vehicle 15–20 s, icon shown | 00:11:51Z engaged→standby at 43 mph | target icon is not a historian field |
| 7:14 resume, 52 to 61 mph | 00:13:17Z standby→engaged at 53 mph; 00:13:49Z set speed 62; 00:14:00Z engaged at 61 mph | consistent |
| 7:19 resumed to 63 from 53 | 00:19:14Z set speed 62→63 | value matches |
| 7:44 to just before 7:47 lead vehicle; set speed lowered until the icon went out | 00:44:16Z 63→66; 00:46:23Z 66→60; 00:46:28Z 60→58; 00:46:56Z 58→52 | the lowering matches in time and direction |

Also seen: while the state is `standby` or `override`, `acc.set_speed` takes values near the road
speed (34, 33, 49 at 00:34–00:35Z; 38, 31, 32 at 00:49–00:51Z). Consumers already gate the set
speed on `acc.state`; the raw-frame pass should say what the cluster shows in those moments.

## Open for the raw-frame analysis

1. Gap: `0x5A0` raw gap against the noted bars 1→4→1 at 00:00Z, to test `bars = raw + 1`.
2. Distance buttons: `0x2FA` B1 bit7 (proposed Distance Increase) and the still unobserved
   Distance Decrease bit, from the same presses.
3. Target icon: HUD index 6–9 at about 00:11Z and 00:44–00:47Z, including the moment the icon went
   out as the set speed came down.
4. ACC off/on at 00:01Z, and fixed cruise on, set, cancel and off at about 00:05Z (four presses
   about 5 s apart).
5. Accelerator override and the blinking set speed, against `0x5A0` state.
6. RES presses at about 00:10Z, 00:14Z and 00:19Z.
7. Speed: the owner finds the broker's `vehicle.speed` agrees with GPS and the cluster reads about
   1 mph high. `vehicle.speed` quality is `observed_alfa_scale`; this is an owner observation, not
   a calibration.

Evidence tiers are unchanged until that analysis is done.
