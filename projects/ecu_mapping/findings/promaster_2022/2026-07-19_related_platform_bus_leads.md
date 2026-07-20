# Related-platform passive bus leads — 2026-07-19

## Why this is only a candidate list

The 2022 US ProMaster is related to the Fiat Ducato/Citroën Jumper platform, but model year,
market, ECU generation, bus branch, identifiers, and scaling can all differ. Nothing below is a
verified ProMaster decode. These leads are useful only for prioritizing **passive** searches after
the PCAN is physically moved to DLC pairs 3/11 and 12/13. A matching identifier is still not enough
to promote a signal: require payload behavior plus a controlled ground-truth event on this van.

## Public 2020 Citroën Jumper implementation

The GPL-3.0 project [`danielfett/miqro_can`](https://github.com/danielfett/miqro_can), inspected at
commit `aba4bd856c9cf98db9b0da583f5e6a44f7aa6bc3`, says it was tested on a 2020 Citroën Jumper's
"entertainment CAN bus." Its current code configures SocketCAN at **50 kbit/s** and listens for
29-bit identifiers. The exact-vehicle local OEM `COMMUNICATION / CAN BUS DESCRIPTION` independently
declares CAN-C at 500K and its lower-speed CAN-B at **50K**. That makes 50 kbit/s the leading rate
for the ProMaster CAN-IHS survey, but still not a live-established rate for DLC pins 3/11: the OEM
documents mix CAN IHS, CAN-B/BH, and branch labels without explicitly tying that rate sentence to
the auxiliary DLC pair. The topology puts radio, telematics, cluster, HVAC, and other cabin modules
on CAN IHS, which is why pins 3/11 are the first passive place to test it.

High-value identifier/decode candidates from that implementation are:

| 29-bit ID | related-platform interpretation | candidate field |
|---|---|---|
| `04214006` | speed / ignition activity | low nibble of byte 2 + byte 3, `/16` km/h |
| `02294000` | remote-lock events/state | bits in bytes 5-6 |
| `04394100` | pitch and roll | packed 12-bit signed-looking fields, `/10` degrees |
| `06214000` | doors, handbrake, exterior lights | status bits in bytes 0-3 |
| `0621401A` | driver seatbelt | byte 2 |
| `06254000` | maintenance distances | packed fields in bytes 1-5 |
| `06314000` | start/stop disabled | byte 6 |
| `063D4000` | outside temperature and voltage | `(byte0 - 80)/2`; byte1 `/6` |
| `0C054003` | fuel consumption, odometer, range | packed nibbles/bytes 0-5 |
| `08014000` | cruise-control status/button | bits in byte 0 |
| `04214001` | reverse | byte 7 low-state candidate |

These formulas are third-party reverse-engineering claims, not OEM definitions. Their greatest
near-term value is as a signature set: if several of the exact IDs appear together at 50 kbit/s on
one physical pair, that is much stronger evidence of a related cabin-bus dialect than any one ID.

## Comparison with the current C-CAN capture

The completed ignition-transition summary
`tmp/captures/ccan/pair6-14_500k_vehicle-off-through-drive_20260719_153340.summary.json` contains
18 unique 29-bit identifiers, but none of the eleven related-platform IDs above. That is consistent
with (but does not prove) these frames belonging to a different physical branch. Do not add them to
the C-CAN map.

## Public diagnostic catalogs as label priors

Two public third-party diagnostic pages expose useful *names* for related-platform modules and live
data, but neither exposes request bytes, numeric DIDs, response layouts, or scaling formulas:

- [ScanDoc's Citroen Jumper 3 Euro 5 / Euro 6 BCM demo](https://scandoc.online/last/0/18/26/2?lng=EN)
  shows a real-looking BCM identification (`BC250I.010`, software reference `0444000251`) and a
  large data-stream label set including individual doors, locking requests, wiper state, ignition
  position, lighting requests/outputs, parking brake, fuel level, vehicle speed, battery voltage,
  and ignition counter. Its displayed DTC list separately names CAN-C and comfort/body CAN-B.
- [FiCOM's Citroen Jumper 2011-2026 (250/290) support catalog](https://obdtester.com/ficom-eculist/citroen/jumper_2011_2026_%5B250%2C290%5D)
  lists related-platform ECU families and advertised live-data coverage for BCM, cluster, TPMS,
  ABS/ESP, EPS, parking aid, airbag, HVAC, telematics, radio, and others.
- [Multiecuscan's current supported-vehicle database](https://www.multiecuscan.net/supportedvehicleslist.aspx)
  has a particularly close `Fiat Ducato (type 290MCA)` entry: Silatech electronic shifter, ZF
  9HP48 transmission, Bosch DASM radar, Marelli cluster, and Aptiv BCM match the supplier/role
  pattern independently observed on this van. It also lists Bosch ABS/ESP, ZF lane camera,
  airbag and electric steering, Continental blind-spot sensors, several Bosch parking-controller
  variants, and dedicated TPMS variants. Those are useful missing-module candidates for the
  pins-12/13 survey, not evidence that this US gasoline configuration contains every item.

Use these pages only as a vocabulary/checklist when planning controlled ground-truth experiments.
They can help recognize what an unknown field *might* represent, but they do not establish that the
2022 US ProMaster implements the same diagnostic item, address, encoding, or bus placement. In
particular, do not turn the ScanDoc values shown from another vehicle into expected constants for
this van. Multiecuscan's `INFO/DTC/PRM/ACT/ADJ` support flags likewise expose capabilities, not
wire-level CAN identifiers, request bytes, numeric DIDs, layouts, or scaling.

## Parked physical-pair survey order

1. Repin the PCAN to DLC 3/11 with the vehicle parked; probe passively at observed/error-safe rates,
   starting with 50 kbit/s. [AlfaOBD's current hardware guide](https://alfaobd.com/) describes this
   as the middle-speed pair for its supported PowerNet/CUSW layout, while its 2022+ ProMaster notes
   separately call pins 12/13 the second high-speed CAN bus. Do not use the related project's
   automatic recovery code because it reconfigures the interface and is not a passive survey workflow.
2. If several candidate IDs appear, capture ignition-off through ignition-on and controlled events
   (one door, parking brake, reverse selection only while stationary, known outside temperature).
3. Survey DLC 12/13 independently, starting at 500 kbit/s because AlfaOBD identifies it as the
   second high-speed pair for 2022+ ProMaster. The exact rate remains a live-measurement question;
   do not assume CAN-CH and CAN-IHS share a rate or identifier set.
4. Promote only this-van evidence into `docs/bus-map.md`, with the capture path and experiment.
