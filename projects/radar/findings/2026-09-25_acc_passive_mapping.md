# ACC passive mapping — September 24 drives (draft, 2026-09-25)

Status: **draft; every field below is a Tier 1 exploratory candidate** under
[`docs/can-evidence-tiers.md`](../../../docs/can-evidence-tiers.md). The fields replicate across three
drive legs, but no independent labeled reference exists yet. Do not add them to `docs/bus-map.md` as
verified decodes or to the telemetry allowlist until the owner confirmations listed at the end are in.

> **Update 2026-09-27:** an owner-annotated drive verified the `0x5A0` set speed (61→66 mph) and the
> `0x2FA` SET+ bit, and added the speed-limit display field `0x0E0` B0 (mph). See
> [2026-09-27 owner-referenced drive](2026-09-27_acc_speed_limit_owner_reference.md).
>
> **Update 2026-09-29:** the owner's callouts on the 2026-09-28 drive verified the state enum, the
> gap (bars = raw + 1), the target family and every button below, and added Distance Decrease
> (B0 bit6), the fixed-cruise button (B0 bit1) and the fixed-cruise indices 27–32. See
> [2026-09-28 owner-referenced drive](2026-09-28_acc_owner_reference_drive.md). `0x4AF`, `0x5A5`,
> `0x1F2`, `0x5E4` and the lead-object rows below remain Tier 1.

## Scope and provenance

This is offline analysis of saved full-stream captures only. It sent no CAN traffic and made no
service, link or configuration change. The owner reported using adaptive cruise during these drives.
Capture sets are under `/mnt/EXFAT512/obd-things/tmp/captures/three_bus_drive/broker-drive/`:

| capture set | approx. MDT (2026-09-24) | notes |
|---|---|---|
| `broker-drive-20260924T210710082916` | 15:07–15:54 | all three roles `full_stream_complete`, 0 drops |
| `broker-drive-20260924T221840459273` | 16:18–18:00 | full streams complete, 0 drops; C-CAN/B-CAN wrapper ended `reason: error` (ownership) |
| `broker-drive-20260925T002934026936` | 18:29–20:00 | same as above |
| `broker-drive-20260925T02*`/`T03*` (4 short sets) | 20:45–21:15 | no ACC button activity; used only for the 0x2FA integrity check |

Reproducible working outputs (gitignored) are in `tmp/radar/acc-passive-20260924/`:
`timeline.csv`, `press_sequences.txt`, `acc_5a0_transitions.csv`, `candidates.md`, rankings, and the
offline scripts that produced them.

Method: 0x2FA press events were extracted, then per-(ID, byte/nibble/bit) change points were computed
over every C-CAN, CAN-CH and B-CAN identifier. Units were ranked by change enrichment near presses,
then against the decoded set-speed, state and engaged events (recall/precision within ±0.35 s).
Context signals are the established `0x101` speed, `0x0FC` rpm and `0x100` TCM target crankshaft
torque, plus the existing `0x1FA`/`0x0FA` service-brake candidates.

## 0x2FA — ACC steering-wheel buttons (C-CAN; identical frames on CAN-CH)

DLC 4, 50 Hz. The frame is protected like `0x101`:

- B1 bits 3:0 are a rolling counter.
- B2 is CRC-8/SAE-J1850 (poly `0x1D`, init `0xFF`, xorout `0xFF`) over B0–B1. It matched all
  755,774 frames in all seven capture sets.
- B3 is constant `0x0E`, and B1 bits 6:4 are constant `011`.

The related-platform CUSW `CRUISE_BUTTONS` layout (DLC 3, counter in the high nibble, different bit
assignments) does not transfer.

| bit | inferred button | presses | behavioural evidence (response in `0x5A0`) |
|---|---|---|---|
| B0 bit0 | ACC on/off | 3 | off → ready each time; never observed switching off |
| B0 bit3 | RES | 1 | 19:27:24 MDT: standby → engaged at the remembered 61 mph with no set change; 57.5 → 60.4 mph |
| B0 bit4 | SET− | 16 | 14/14 engaged taps step the set speed −1 mph; from standby it sets the current speed |
| B0 bit5 | SET+ | 176 | 117/117 engaged taps step +1 mph; three 0.8–1.4 s holds jump to the next multiple of 5 (manual: 5 mph hold steps); from ready/standby it sets the current speed |
| B0 bit7 | CANC | 5 | 5/5 engaged/override → standby with popup `0x45`/`0x46` and no brake |
| B1 bit7 | distance | 5 | 3 presses stepped the gap by one; 2 presses at 17:54:28–29 MDT produced no change (unexplained) |

The fixed-speed-cruise on/off button was not pressed, so its bit is unmapped. SET+ presses at
16:24:05–06 MDT, while ACC was still off, correctly produced no response.

## 0x5A0 — cluster ACC frame (C-CAN)

DLC 8. It is sent at 1 Hz and also immediately on any change. It carries the owner-manual display
states (off, ready, set speed, following distance, target detected).

| signal (DBC Motorola notation) | candidate meaning | evidence |
|---|---|---|
| `SET_SPEED_KPH : 23\|8@0+` (B2) | set speed, km/h | km/h = round(mph × 1.609344) for 52/52 distinct pairs |
| `SET_SPEED_MPH : 31\|8@0+` (B3) | set speed, mph | 131/131 engaged SET± taps step ±1; at SET, set = `0x101` speed +0.1 to +1.2 mph (45/46), consistent with cluster over-read |
| `ACC_STATE : 48\|3@0+` (B6 bit0, B7 bits 7:6) | 0 off, 1 ready, 2 engaged, 4 accelerator override, 5 standby (cancelled, speed in memory); 3 seen once for 80 ms at CANC | every ACC button and brake transition; 41/41 non-CANC cancels coincide with `0x1FA` B3 bit1 = 1 |
| `HUD_INDEX : 55\|6@0+` (B6 bits 7:2) | graphic index = base + gap. Bases: ready 2, engaged-with-target 6, engaged-no-target 10, standby 14, override-no-target 23, override-with-target 37; off shows 1 | each gap press steps the index by one within its base; ready/engaged popup code = `0x34 + index` |
| gap (derived: index − base) | following-distance setting 0..3 | 0→1 (15:19:20), 1→2 (17:54:01), 2→3 (17:54:09 MDT); retained across ignition cycles (each drive started at the previous drive's final value) |
| `POPUP : 15\|8@0+` (B1) | transient message code, cleared ~5 s later | `0x46` brake cancel (38 of 41 brake-coincident cancels); `0x45`/`0x46` CANC; `0x4A` accelerator override; `0x55` SET refused while braking; `0x35` key-off |

The target interpretation is behavioural. In drives 1–2, engaged time on indices 6–9 ran a mean
8.8–13.6 mph below set speed, with 46–49% of samples more than 2 mph below, and it covered an ACC
slow-down from 54 to 16 mph at 17:54 MDT. Indices 10–13 ran about 0.7 mph below set speed.

B0 bit3, B4 bits 1:0 and B5 bit3 dropped together in six 8–45 s episodes in drive 3 only (18:36,
18:53, 18:56, 18:58, 18:59 and 19:29 MDT). ACC stayed functional during them, and their meaning is
unresolved.

## Related candidates

- **CAN-CH `0x5E4`**: gateway repack of `0x5A0`. B3 = km/h and B4 = mph; popup, state and index
  bits are repacked into B5–B7.
- **`0x4AF` B1 bit3** (same frame on C-CAN and CAN-CH): ACC engaged. It matched every engage and
  disengage exactly in all three legs (17/17, 23/23, 54/54).
- **`0x4AF` B3 bit2**: ACC deceleration/brake-request candidate. In all 21 episodes speed fell while
  the `0x1FA` driver-brake candidate stayed off and `0x0FA` stayed `0x80`.
- **`0x5A5`**: B0 bit7 and B2 bit5 = engaged; B2 bit6 = override. B4–B5 is an unrelated slow
  counter.
- **`0x1F2`** (50 Hz): B6 bit5 = engaged, and B6 bits 7:6 cycle while engaged. A signed field with
  its MSB at B4 bit2 (length unresolved, 10–13 bits) is populated only while engaged. There it
  correlates with `0x100` TCM target crankshaft torque at r = 0.90 (36 Nm RMSE), so it is an
  exploratory ACC longitudinal-request lead only.
- **CAN-CH `0x22A`** B2 bit7 mirrors engaged.
- **B-CAN**: no ACC mirror found.
- **Related-platform leads**: `0x2EC` was all-zero throughout, and `0x3EE` was absent.

**This passive evidence also supports the existing service-brake candidates.** All 41 non-CANC ACC
cancellations coincided with `0x1FA` B3 bit1 = 1. The ACC-deceleration episodes did not set that bit.

## Lead-object data

No range or relative-speed candidate was found; the HUD target family is the only lead-related
field. A 4 Hz byte-level screen compared engaged-with-target against engaged-without-target periods
on C-CAN and CAN-CH. It surfaced only drive-cycle and regime confounds: `0x4B1` B4, `0x5BE` B3 and
`0x5AE` B4 switch at engine start/stop, and `0x416` carries text.

This rejects only that coarse screen. It did not fit continuous 16-bit fields against an independent
range reference, and object data may travel on a radar/camera private link.

## Owner confirmations that would promote these

1. **Button labels.** Confirm the button pressed at these MDT times:
   - 15:17:35 ACC on/off, then SET+
   - 15:19:20 distance
   - 17:54:01 and 17:54:09 distance, and whether 17:54:28–29 was also distance
   - 18:36:21 three SET− presses
   - CANC at 18:36:44, 18:37:04, 18:37:21, 18:51:05 and 19:39:50
   - RES at 19:27:24
2. **Gap bars.** Report the distance bars the cluster shows now; the last observed raw gap is 3.
   Any single display reference fixes the gap-to-bars mapping.
3. **One annotated leg (passenger-recorded, normal driving, no bench interaction).** Record
   displayed set speeds and a full cycle of all four distance settings, plus ACC off and
   fixed-cruise on/off presses.
4. **Target icon.** Note when the cluster shows the target-detected icon. This separates the index
   6–9 versus 10–13 families.

Items 1–2 would be independent labeled references for `0x2FA` bits, set speed and gap, taking them
to Tier 3. Item 4 does the same for the target flag. Until then, keep them exploratory. Do not use
any of them for safety or alerting.
