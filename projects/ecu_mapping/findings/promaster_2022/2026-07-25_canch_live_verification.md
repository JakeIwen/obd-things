# 2026-07-25 CAN-CH live verification

## Result

The 2022 ProMaster's DLC pins 12/13 are now live-verified as the 500-kbit/s CAN-CH / second
high-speed branch. The owner's grey adapter remapped vehicle pins 12/13 to the PEAK and OBDLink
CAN pins. PCAN stayed listen-only while AlfaOBD 2.4.4.0 independently completed identification and
DTC reads from four installed grey-routed modules:

| module | physical request → response | identity evidence |
|---|---|---|
| ABS/ESC | `18DA28F1` → `18DAF128` | `F1A5=0006501520`; `F187=68516283AD` |
| EPS (ZF/TRW) | `18DA30F1` → `18DAF130` | `F1A5=0002507919`; `F187=68509191AD` |
| HALF | `18DA31F1` → `18DAF131` | `F1A5=001E502920`; `F187=68567254AA` |
| ORC / airbag | `18DAC0F1` → `18DAF1C0` | `F1A5=001A507720`; `F187=68518674AC` |

This verifies the bitrate, physical adapter routing, and those four installed endpoints. The catalog's
grey-routed `0x26` park-assist and `0xA0` park-assist candidates were configured absent and were not
probed; they remain unverified.

## 2026-09-24 passive corroboration and gateway routing

An in-vehicle diagnostic client using tester address `F1` read the same four modules during a
drive, while the Pi's C-CAN interface was listen-only. Each module's `22 F1A0` composite record
embeds the same F187 part number and F1A5 subtype as the AlfaOBD identities above:

| module | F187 | sw / F192-like | F194-like | F195-like | F1A5 |
|---|---|---|---|---|---|
| ABS `0x28` | `68516283AD` | `0265957013` | `1267985695` | `0202` | `0006501520` |
| EPS `0x30` | `68509191AD` | `F4C1` | `FI06EB00-00` | `0030` | `0002507919` |
| HALF `0x31` | `68567254AA` | `A012N333` | `006.003.000` | `0203` | `001E502920` |
| ORC `0xC0` | `68518674AC` | `0285015767` | `BB101464` | `1020` | `001A507720` |

The embedded VIN matches this van and is masked in tracked material. ABS and ORC answered
`22 F132` with ten ASCII spaces, and ABS answered `22 F100` with 29 spaces. EPS and HALF returned
`7F 22 31` for both. `19 02 0D` reported ABS `C1200-17` (status `4C`), HALF `B1006-49` (`0C`),
`C14A5-92` (`08`), and `C14A5-97` (`08`), with EPS and ORC clear. `0x26` and `0xA0` were queried
once and stayed silent.

The client transmitted these requests on **C-CAN**. The vehicle forwarded each frame to CAN-CH a
median 0.31 ms later (0.23–2.78 ms). A gateway substituted FlowControl `30 00 0A` (STmin 10 ms) on
CAN-CH, and it copied each CAN-CH reply back onto C-CAN as **`18DAF2xx`** (target byte `F2`). The
physical request IDs above can therefore appear on C-CAN as well. Only the `18DAF1xx` replies are
CAN-CH-exclusive. See
[`2026-09-24_in_vehicle_f1_scan.md`](2026-09-24_in_vehicle_f1_scan.md).

## Passive capture evidence

The ignored raw capture is:

`tmp/proxi_safety/20260724_2033_baseline/canch_20260725/candump-2026-07-25_020614.log`

- 2,740,037 frames over approximately 24 minutes
- 500 kbit/s, listen-only, ERROR-ACTIVE
- final PCAN TX/RX error counters zero
- SHA-256 `8cd598cd5fb99fb05692b9603bf8e4dfeee2e8701eb3ebf26c56628bffece860`

Captured physical exchange counts were:

| identifier | frames | identifier | frames |
|---|---:|---|---:|
| `18DA28F1` | 492 | `18DAF128` | 529 |
| `18DA30F1` | 114 | `18DAF130` | 136 |
| `18DA31F1` | 252 | `18DAF131` | 287 |
| `18DAC0F1` | 112 | `18DAF1C0` | 136 |

The capture also established an awake-bus signature distinct from the same campaign's ordinary
pins-6/14 reference capture. IDs `0x0DA`, `0x0DC`, `0x0F1`, `0x106`, `0x10E`, `0x117`, and
`0x1F6` each appeared at high rate on CAN-CH and were absent from that C-CAN reference. Because
some other identifiers are gateway-forwarded onto both branches, software classification requires
at least three members of this set, or one of the verified physical diagnostic identifiers above.

## AlfaOBD result boundary

- EPS and ORC reported no faults.
- ABS C1200 was intermittent/history, its last test passed, and its freeze-frame came from an earlier
  driving event; it did not establish current charger overvoltage.
- HALF had historical/intermittent B1006 and C1436 plus current C14A5 sensor
  blinded/performance. These observations are inventory only; no DTC was cleared.
- No active diagnostic, reset, calibration, configuration write, or PROXI alignment was performed.

## Safety consequence

A silent 500-kbit/s bus cannot be passively distinguished between ordinary C-CAN and CAN-CH. No
CAN-CH wake method is established, and C-CAN/B-CAN wake traffic must not be tried on pins 12/13.
The unattended voltage monitor now observes only an already-UP listen-only interface, reports an
awake CAN-CH signature as “grey adapter connected,” and exits without interface changes or CAN
transmission.
