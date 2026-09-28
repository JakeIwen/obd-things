# ACC and speed-limit owner-referenced drive — 2026-09-27 (draft)

This extends the [September 24 passive ACC mapping](2026-09-25_acc_passive_mapping.md) with a drive
the owner annotated from the cluster display. The analysis used saved captures only: no CAN
transmission, and no service, link or configuration change.

## Captures and time-zone alignment

Capture sets are under `/mnt/EXFAT512/obd-things/tmp/captures/three_bus_drive/broker-drive/`. Every
role reports `full_stream_complete: true` with 0 socket drops.

| capture set | UTC coverage | notes |
|---|---|---|
| `broker-drive-20260927T205938144339` | 20:59:38–21:03:58Z | parked, no ACC activity |
| `broker-drive-20260927T211829293005` | 21:18:29–21:20:23Z | parked, no ACC activity |
| `broker-drive-20260927T212949433889` | 21:29:49–22:00:10Z | C-CAN/B-CAN wrapper ended on the known B-CAN ownership-loss error |

The engine-running interval continued to about 22:34Z. Nothing after 22:00:10Z was captured.

The owner's notes are in **CDT (UTC−5)**, while the Pi logs in MDT (UTC−6). The data confirms this:

- The ACC on/off press (`0x2FA` B0 bit0) is at 21:41:28Z.
- The first SET+ at 60.4 mph is at 21:41:43Z and produces `0x5A0` set speed 61 mph.
- This matches the note "4:41 set to 61".

Working outputs are in `tmp/radar/acc-passive-20260927/`: `timeline.csv`, `acc_5a0_transitions.csv`,
`2fa_integrity.json`, `extract/sl_frames.log`, and change-point files.

## Verified against the owner's notes

**ACC set speed, 4:41 CDT, "61, then +1 mph to 66 at about 5 s intervals".**

- SET+ (`0x2FA` B0 bit5) at 21:41:43Z set `0x5A0` B3 = 61 mph, with B2 = 98 km/h.
- Five further SET+ presses at 21:41:50, :57, 21:42:05, :13 and :22Z (7–8 s apart) stepped B3 through
  62, 63, 64, 65 and 66 (B2 100, 101, 103, 105, 106 km/h).
- The value, the steps and the timing all match the notes.
- `0x2FA` CRC-8/SAE-J1850 and the counter held on 107,557/107,557 new frames.

**Speed limit, `0x0E0` B0 in raw mph.** The frame appears on C-CAN, CAN-CH and B-CAN with the same B0
(DLC 4). It holds the previous trip's value at wake and reads 0 at key-off.

| owner note (CDT) | `0x0E0` B0 change (UTC) | match |
|---|---|---|
| (value before 4:44 = 45) | 55→45 at 21:43:51Z | consistent; not separately noted |
| 4:44, 45→30 | 45→30 at 21:44:33Z | yes |
| 4:45, →45 | 30→45 at 21:45:17Z | yes |
| "→55 a few seconds before 4:46" | 45→55 at 21:45:50Z | yes |
| 4:52, 55→45 | 55→45 at 21:52:44Z | yes |
| — | 45→35 at 21:53:23Z, →45 at 21:54:38Z, →60 at 21:54:52Z | not noted by owner |

The September 24 drives replicate the behaviour. There the field only ever took multiples of 5 from
20 to 65 mph (e.g. 40, 60, 30, 20, 50, 45, 55, 25, 35, 65), and it read 0 at key-off.

**Camera-side companion, `0x380` B0 (C-CAN and CAN-CH).** It changes 13–36 ms before `0x0E0` at
every step. Values 0x2D/0x25/0x19/0x1D/0x31 map to 88/72/48/56/96 km/h (55/45/30/35/60 mph) under
`km/h = (B0 − 1) × 2`; B0 bit0 has always been 1. Across the three September 24 drives it matched
`0x0E0` within 5 km/h in all but 37 of about 48,000 paired samples, and those are transition timing.
This is likely the forward camera's native km/h value, with `0x0E0` as the cluster's mph copy.
`0x380` B3 bit6 toggles at irregular sign-related times and is unresolved.

**Also consistent with the September 24 decode:**

- All seven non-CANC cancellations coincided with the `0x1FA` brake candidate.
- ACC state and HUD index behaved as before.
- The gap stayed at raw 3 for the whole capture (ready index 5, engaged 13, standby 17), carried over
  from September 24.
- No target-detected index (6–9) occurred.
- ACC was engaged at a 60 mph set speed when the capture ended.

## Not verifiable (after 22:00:10Z)

These notes fall after the capture ended:

- speed limit 22:06Z (55→45) and 22:10Z (55→65)
- ACC set to 74 at 22:11Z
- follow-distance toggling between three and four bars at 22:25Z
- lead-vehicle icon flashing and mostly solid at 22:26–22:28Z
- cancel at 22:29Z
- ACC off at 22:30Z
- regular (fixed-speed) cruise enabled and set to 72 at 22:32Z

The historian (`/var/lib/van-telemetry/history.sqlite3`, opened read-only) has 323 snapshots for
22:00–22:34Z at 5–6 s cadence. Its metrics are speed, rpm, torque, temperatures, TPMS, radar
alignment and odometer. It has no ACC, button, HUD or speed-limit fields and no raw frames.

- Speed held about 69–77 mph after 22:11Z and about 71 mph around 22:32Z. That is loosely consistent
  with the 74 and 72 mph notes.
- It cannot distinguish ACC from manual driving, the gap bars, the target icon, ACC off, or fixed
  cruise.

## Gap mapping inference (unverified)

On September 24 the `0x2FA` B1 bit7 presses stepped the raw gap 0→1→2→3. Two further presses at
raw 3 produced no change. That clamp, plus the owner toggling "between three and four bars" from the
saved raw 3, suggests the following:

- B1 bit7 is the manual's **Distance Increase** button.
- Raw 3 is **four bars** (longest), so bars = raw + 1.
- The Distance Decrease bit is still unobserved.

This is Tier 1 until a captured press sequence with known bars exists.

## Evidence tiers

| field | tier | basis |
|---|---|---|
| `0x5A0` B3 set speed (mph), B2 (km/h) | **Tier 3 proposed** (review decision) | exact owner-displayed values 61→66 plus 3 replicated drives; km/h = round(mph × 1.609344) |
| `0x2FA` B0 bit5 SET+ | **Tier 3 proposed** | owner-noted presses matched one-for-one in time and effect |
| `0x2FA` B0 bit0 ACC on/off | strong Tier 1 | off→ready at 4:41:28 CDT, just before the noted set; owner did not note the press itself |
| `0x0E0` B0 speed limit (mph) | **Tier 3 proposed** | 4/4 captured owner-noted changes exact in value and minute; replicated plausible range on 3 other drives |
| `0x380` B0 speed limit (km/h, (B0−1)×2) | Tier 1 | tracks `0x0E0` consistently; no direct km/h reference |
| `0x5A0` state, HUD index, target family; `0x4AF`; other buttons | Tier 1 (unchanged) | no new independent reference in the captured window |
| gap bars = raw + 1; B1 bit7 = Distance Increase | Tier 1 inference | clamp behaviour; owner's 5:25 toggling not captured |

Tier 3 here means the evidence meets the "trusted labeled source" criterion. Promotion into
`docs/bus-map.md` and the telemetry allowlist remains a reviewed decision; no safety use.

**Promoted 2026-09-27 (owner request):** `docs/bus-map.md` now has rows for `0x0E0` B0, `0x5A0`
B3/B2 and `0x2FA` B0 bit5. The broker publishes `vehicle.speed_limit` and `acc.set_speed`
(`verified`) plus `acc.state` (`candidate`), and Drive page 2 shows them. Decoder replays of this
capture reproduce every transition in the tables above; tests use its real frames
(`tests/test_display_frames.py`).

## Remaining owner/recording asks

1. Capture a drive covering distance toggles, the target icon, ACC off and fixed cruise. The
   recorder must stay up past the B-CAN ownership failure, or the notes must fall inside its
   coverage. Note the bars shown after each distance press.
2. Map the unobserved bits: Distance Decrease and fixed-cruise on/off.
