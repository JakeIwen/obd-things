# B-CAN / CAN-IHS physical pair verification — 2026-07-20

## Setup and provenance

The vehicle OBD connector feeds a purpose-built pigtail with two labeled DB9 outputs: one for
C-CAN and one for B-CAN. The PCAN-USB was connected to the pigtail's **B-CAN DB9**. The owner
confirmed that the DIY AlfaOBD yellow adapter was not used for these captures and has never been
used on this van.

The 2022 ProMaster DLC diagram identifies **CAN IHS on pins 3/11** and CAN C on pins 6/14. The
labeled pigtail establishes which documented pair was selected; no adapter-pin inference is needed.

## Passive evidence

`can0` was explicitly configured at **125000 bit/s** with **listen-only on**. It remained
ERROR-ACTIVE with TX/RX error counters at zero. Raw captures remain gitignored under
`tmp/captures/bcan/events/`:

| condition | capture | parsed frames | result |
|---|---|---:|---|
| ignition off, front-door fob unlock | `pair3-11_125k_ignition-off_fob-front-unlock_20260720_235157.log` | 2,317 | 14-second wake burst; established B-CAN signature present |
| ignition off to ignition on, engine off | `pair3-11_125k_ignition-off-to-on_engine-off_20260720_235404.log` | 15,587 | ignition-on schedule appears cleanly |
| steady ignition on, engine off | `pair3-11_125k_ignition-on_engine-off_steady_20260720_235629.log` | 13,163 | 74.98 seconds at 175.5 frames/s |

The captures contain the established body-bus identifiers including `0x46C`, `0x0A0`, `0x0E0`,
`0x2EA`, `0x354`, `0x356`, `0x3DC`–`0x3E6`, and `0x5B2`. The ignition transition adds traffic
including `0x41A`, `0x4F6`, `0x5A2`, `0x5B4`, and network-management identifiers. This matches
the previous comfort/body captures and proves that the selected pair and 125-kbit/s setup are the
same B-CAN network used by the existing tools.

## Diagnostic-address check

Offline timing and payload analysis rejects the formerly suggested high 11-bit IDs as diagnostic
pair evidence:

| ID | steady ignition-on behavior | payload observation |
|---:|---:|---|
| `0x75C` | 1.000 s, 75/75 frames | fixed two-byte `0518` |
| `0x760` | 1.000 s, 75/75 frames | fixed six-byte `FFF141CD2260` |
| `0x762` | 1.000 s, 75/75 frames | fixed six-byte zero payload |
| `0x764` | 1.000 s, 75/75 frames | six-byte date/status-like payload; two values |
| `0x768` | 1.000 s, 75/75 frames | fixed eight-byte `FF` payload |
| `0x7C0` | 2.000 s, 38/38 frames | fixed three-byte zero payload |

The fob-unlock capture contains one `0x7B8#33323331` frame. Its bytes are ASCII `3231`; it is
not an ISO-TP positive or negative response. None of the three new captures contains a plausible
UDS response frame.

There is also an older 111-second capture named
`tmp/tpms/rfh_alfaobd_sniff_bcan_20260707_164055.log`, recorded while AlfaOBD exercised the RF
Hub. Its 19,648 frames contain the ordinary B-CAN schedule and no ISO-TP exchange. The matching
current-van AlfaOBD adapter trace selects 29-bit CAN and uses `18DAC7F1`/`18DAF1C7` for the RF Hub
and `18DA40F1`/`18DAF140` for the BCM. Together, those observations support the existing topology:
AlfaOBD diagnostics are visible on C-CAN and B-CAN exposes the resulting body-bus state changes.

## Conclusions and limits

- **Verified:** B-CAN / CAN-IHS is exposed on DLC pins **3/11** through the labeled pigtail and is
  live at **125 kbit/s** on this van.
- **Superseded:** the prior 50-kbit/s candidate and “physical pair unresolved” notes for this bus.
- **Not established:** any direct B-CAN diagnostic endpoint. The earlier 11-bit candidates are
  periodic application broadcasts, not evidence of an ISO-TP request/response pair. Do not probe
  inferred `+8` pairs from these IDs.
- **Current diagnostic path:** use the independently verified 29-bit C-CAN endpoints for module
  identity, DID, DTC, and result-only routine inventories. Keep B-CAN passive for signal/event
  mapping unless a real scan-tool request/response trace proves direct diagnostic addressing.
- **Separate branch:** CAN CH on DLC pins 12/13 remains unverified and is outside the current
  C-CAN/B-CAN priority.
