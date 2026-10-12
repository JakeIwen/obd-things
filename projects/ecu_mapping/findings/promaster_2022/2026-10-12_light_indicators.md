# Owner-referenced lights and high-beam telltales — 2026-10-12

The owner wants the telemetry app to repeat the green lights and blue high-beam
indicators because the steering wheel obscures the IPC. Two controlled parked,
ignition-on/engine-off cycles establish a direct boolean correspondence on
standard C-CAN `0x73E`, exact DLC 4:

| Field (zero-based byte, LSB bit numbering) | Meaning | Observed off / on |
|---|---|---|
| byte 0 bit 1 (`0x02`) | Green parking/headlights telltale during the tested manual settings | `0` / `1` |
| byte 0 bit 2 (`0x04`) | Blue steady high-beam telltale | `0` / `1` |

The selected payloads were `40000020` with both off, `42008020` with green on
and blue off, and `46008020` with both on. Other bits are not decoded.
These are telltale observations, not bulb-health or measured light-output
feedback. The green symbol includes parking lights in the
[2022 owner's manual](https://vehicleinfo.mopar.com/assets/publications/en-us/Ram/2022/ProMaster/104359_22_VF_OM_EN_USC_DIGITAL_E3_V1.pdf)
(printed page 55, accessed October 12); do not label it as proof of low beams
specifically. The owner subsequently confirmed there is **no AUTO setting**
on this van's light switch. Separate parking-only operation and flash-to-pass
were not exercised. First driving validation of the app remains pending.

## Reference and acquisition

The owner explicitly reported each of nine stable states, following two
off → low → high → low → off sequences. Each high-beam transition left the
green indicator on, providing a counterexample to treating the two telltales
as one headlights flag. Each low-beam transition was separately returned to
off before repeating the cycle.

`tools/three_bus_capture.py` recorded the three serial-resolved roles with
shared passive leases. All were classical CAN, listen-only, ERROR-ACTIVE and
error-free. The final TX packet counts were unchanged from preflight:
C-CAN 67,783; B-CAN 5,648; CAN-CH 0. No services or links were changed, and the
Pi transmitted no frames for this experiment.

The usable capture is
`tmp/captures/lighting-20261012/session_20261012T071746.827258Z_2/`.
It spans approximately 07:17:47–07:28:06 UTC. The first two five-minute chunks
per bus finalized normally with no reported drops. The exec interrupt ended
the process group during chunk three; those files are frozen and parseable,
but there is no final supervisor summary/drop certification for that tail.
The preceding 06:48–07:08 session was silent and is not signal evidence.

`owner-events.jsonl` records instruction and acknowledgment times. These are
bounds, not exact physical switch timestamps. Stable scored windows begin
one second after each acknowledgment and end one second before the next
instruction. The final window ends six seconds after its acknowledgment.

## Results and competing fields

Both selected bits matched every one of the **357 frames in nine stable
windows**. The first five windows cover the first cycle (200 frames); the
second cycle covers the remaining four windows (157 frames). Each bit has
exactly four transitions plus its initial state across the entire recording,
with every transition inside the matching instruction/acknowledgment bounds.

| Transition | Green state transition, UTC | Blue state transition, UTC |
|---|---|---|
| First on | 07:18:37.650887 | 07:19:52.790148 |
| First off | 07:21:41.021768 | 07:20:46.570443 |
| Second on | 07:24:34.826483 | 07:25:16.567085 |
| Second off | 07:27:44.009555 | 07:26:36.306016 |

The frame had 2,480 observations over 618.498 seconds, about 4 Hz with
on-change delivery. The maximum observed gap was 0.750 seconds, including
chunk boundaries. The telemetry freshness limit is two seconds; silence never
becomes a decoded off state.

The bounded initial search returned 19 matching bits, so matching alone was
not treated as physical identity. In particular, `0x41A` byte 0 is already
verified battery voltage: its bits merely followed the electrical load and
are rejected as light fields. `0x46C` byte 5 candidates are not promoted.

C-CAN `0x384` byte 3 bit 7 and byte 5 bit 5 independently repeated the green
and blue patterns, with matching CAN-CH copies. B-CAN `0x354` byte 0 bits 1/2
also repeated them. C-CAN `0x73E` is selected because its two independent
boolean fields share one compact, continuously broadcast frame and work with
the existing C-CAN display receiver. Its other correlated bit, byte 2 bit 7,
is left unnamed. Neither an exact sender identity nor a gateway direction is
inferred from this experiment.

Raw source hashes, stable windows, selected/corroborating frame examples and
complete selected-bit transitions are in
[`2026-10-12_light_indicators_evidence.json`](2026-10-12_light_indicators_evidence.json).
The reproducible offline analyzer is `projects/ecu_mapping/lighting_reference.py`,
run through named task `lighting-owner-reference`. Analysis job
`20261012T073018Z-e0853180` completed successfully. Synthetic public-entry
tests passed in job `20261012T073122Z-637b49d1` (2 passed), including inverted
bits, separate bus/namespace identity and missing-reference rejection.

## Telemetry integration

`projects/vehicle_data/lighting.json` is the shared source/bit/metric contract.
`lighting.py` decodes only exact standard `0x73E` DLC 4 and defines boolean
metrics `lights.parking_headlights_on` and `lights.high_beam_on`. The existing
continuous display receiver supplies kernel receipt times and both values
enter the broker cache together. It adds no diagnostic request, socket owner,
or interface mode change. The public observation-publisher route is disabled
for this source.

Both Drive pages show compact, icon-only lights aligned to the left edge of
the Gauges/Sensors row, with the selector independently centered on screen.
The owner requested removal of visible labels/state text and a 30%
size reduction. A fresh true value lights the corresponding icon; fresh false,
missing, expired, non-boolean, retained or disconnected data all look dim/off.
This is presentation only: the API retains its explicit freshness/availability.
Read-only October 12 checks confirmed the original build `fbe357e8fd0a` and
the new boolean metrics active, with kernel-timestamped passive observations.
First driving validation remains pending.

Offline integration verification: 140 focused Python tests and 197 subtests
passed (`20261012T075320Z-2d4991aa`), and 474 frontend tests plus build
`fbe357e8fd0a` passed (`20261012T075448Z-930ec757`). The initial wider Python
selection hit the Mac worker's AF_UNIX socket-path length limit in the
unchanged API concurrency suites; the focused rerun excludes those suites.
Headless Firefox checked the two Drive pages, green/blue/off/unknown states,
portrait and landscape tablet layouts, and 390/320 px phone widths using
synthetic browser-only observations. The Sensors grid reserves enough space
for its radar/history rows. The code-shape ratchet passed without
exceptions or rebaselining.

The owner-requested icon-only footer revision was published on October 12
after build `72f83da9dbad` passed all 474 frontend tests (compute job
`20261012T082618Z-f937ea5b`). Headless Firefox verified both Drive pages and pager navigation,
no visible icon text, unknown rendered off, 28 px tablet / 22.4 px phone icon
widths, alignment with the selector, no horizontal overflow, and no JavaScript
errors. Tablet portrait and landscape stayed within one screen. The live LAN
listener served the expected new HTML and byte-identical CSS. This revision
replaced frontend assets only; the broker remained on its 08:09:29Z instance.
The preceding backend activation was confirmed by live boolean observations
from `ccan.broadcast.0x73e`. The old `/tmp/clip/lights-0158` command is a
historical activation handoff and is not needed for this frontend revision.

The alignment follow-up is live as build `896da709ab7f` (compute job
`20261012T090222Z-068a6936`, 474 frontend tests passed). Firefox measured the
buttons' midpoint at exactly half of each checked viewport width: 400/800,
640/1280, 195/390 and 160/320 pixels. Icons remained at the 16 px left gutter
on the same row, clear of the buttons. Both pager directions, all light states
and the radar layout passed; no JavaScript exceptions occurred. The live LAN
HTML referenced the new assets and the served CSS matched the built file.
