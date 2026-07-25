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
