# Compass direction from the owner's October 14 video

B-CAN standard ID **`0x088`, DLC 5**, carries an eight-direction heading
announcement. The owner's `direction_mapping.MOV` matches the sequence
**N → NW → W → SW → S → SE → E → NE → N** around four left turns.
This is an event-driven direction indication, not a continuous bearing in
degrees. The subsequent [telemetry integration](../../../vehicle_data/docs/compass-display.md)
adds `vehicle.compass_direction` and a retained letter-only Drive indicator.
The research below records the source evidence independently of that implementation.

## Field and scope

Bytes are zero-based. In the observed payload family `00 00 03 XX 00`:

```python
index = (data[3] >> 3) & 0x07
# index: 0=N, 1=NE, 2=E, 3=SE, 4=S, 5=SW, 6=W, 7=NW
```

| Byte 3 | Index | Direction |
|---|---|---|
| `C0` | 0 | N |
| `C8` | 1 | NE |
| `D0` | 2 | E |
| `D8` | 3 | SE |
| `E0` | 4 | S |
| `E8` | 5 | SW |
| `F0` | 6 | W |
| `F8` | 7 | NW |

The entire remaining payload was constant across all **92 frames from two
complete drives**: `data[:3] == 00 00 03`, `data[3] & C7 == C0`, and
`data[4] == 00`. Treat that as the observed family, not an understood validity
or calibration flag. Other payloads and DLCs remain unmapped. Preserve the
standard/extended identifier distinction and B-CAN physical routing.

The cardinal labels are independently established from the road orientations
in the video, checked against OpenStreetMap's street geometry. The video shows
Google Maps, **not a compass readout**. Intermediate labels follow the
observed adjacent steps through each quarter turn; exact sector thresholds,
hysteresis, magnetic-versus-true north, sensor type and sender ECU are not
established. This is a strong owner-route-referenced direction decode; do not
infer a numeric bearing, navigation accuracy, or validity bits from it.

## Video and raw-frame correspondence

The file starts at `2026-10-15T02:22:31Z` (October 14 locally) and lasts
88.386667 s. Metadata has one-second precision. Its Google Maps speed is
consistent with the recorded C-CAN speed over the same window; no fitted
clock shift was used. The following settled road segments were assigned
cardinal directions independently of the CAN bytes:

| Requested video time | Road direction | Most recent `0x088` | Age of announcement |
|---|---|---|---|
| 0 s | N | `00 00 03 C0 00` | 72.379 s |
| 20 s | W | `00 00 03 F0 00` | 6.276 s |
| 40 s | S | `00 00 03 E0 00` | 10.365 s |
| 65 s | E | `00 00 03 D0 00` | 4.073 s |
| 80 s | N | `00 00 03 C0 00` | 6.680 s |

All eight announcements during the clip, relative to its metadata start:

| Time (s) | Byte 3 | Interpreted transition |
|---|---|---|
| 11.325 | `F8` | N → NW |
| 13.716 | `F0` | NW → W |
| 27.035 | `E8` | W → SW |
| 29.635 | `E0` | SW → S |
| 58.620 | `D8` | S → SE |
| 60.920 | `D0` | SE → E |
| 70.015 | `C8` | E → NE |
| 73.320 | `C0` | NE → N |

The [selected reference JSON](2026-10-14_compass_direction_video.json)
retains exact kernel timestamps, raw payloads, source indices/line numbers,
actual still presentation times and capture hashes. The N value at video
start comes from a real earlier announcement in the same uninterrupted
recording, not an invented default or a frame received at video time zero.

## Event delivery and app integration

This source must not be treated like a periodic speed or temperature frame:

- The town recording has **40 announcements in 38 minutes**, with a maximum
  inter-announcement gap of **325.508 s**. The earlier independent recording
  has **52 in about 29.5 minutes**, maximum gap **254.806 s**. Both contain all
  eight codes and the same payload family. The earlier recording corroborates
  encoding/cadence, not independently labeled absolute headings.
- The 170 s three-bus search window contains ten B-CAN `0x088` frames, zero
  on C-CAN or CAN-CH. Eight occur inside the clip. The other two occur after
  it. The Jeep-family `0x358` compass lead was absent on all three buses.
- Brief sampled receive windows can miss every direction change. The existing
  continuous display receiver owns C-CAN only; the active B-CAN helper's
  five-second ICS odometer reads are not a continuous compass observer.
- A future app path needs a continuous, filtered B-CAN receiver coordinated
  with the existing B-CAN owner. Keep the real last-announcement timestamp.
  Do not repeatedly timestamp an old direction as a newly received value.
- Invalidate continuity after socket loss/overflow, owner or route changes,
  capture interruptions, power-cycle boundaries and process restart. Wait for
  a new announcement; do not initialize to N or restore a previous trip's
  heading as current. A continuous observation epoch can retain the latest
  announcement between changes, but it does not prove the compass sender or
  its position source remains healthy. Define the displayed last-reported
  status and age explicitly before calling a held value current.

The subsequent implementation keeps the true announcement age, expires freshness
after ten seconds, and retains old letters slightly dimmed, as explicitly
requested by the owner. Known reception loss clears only the live cache.
No live CAN traffic, CAN socket, interface or service change was needed
to establish this mapping.

## Search and provenance

Initial coarse byte/nibble/word screens required a recent frame and therefore
excluded `0x088`. A separate sparse/event-driven inspection found it. This is
a reusable lesson for future searches: retain event-driven candidates instead
of rejecting all streams with long inter-frame gaps. B-CAN `0x5C0` byte 6 was
an initial false match: its monotonic steps happened before/during the route
segments and failed the return-to-start-direction check; it is not compass.

- Video: `direction_mapping.MOV`, 267,562,172 bytes, SHA-256
  `d87e2e0b42ffbf78dbefe035598730eeb63ae1899b90aadc4b5529d2be16f906`.
- Capture root: `/mnt/EXFAT512/obd-things/tmp/captures/three_bus_drive/broker-drive/`.
  Reference drive `broker-drive-20261015T015240015334`, B-CAN chunks 0–3.
  Physical pair 3/11, 125 kbit/s, recorded interface `can1` (provenance only).
  402,815 parsed B-CAN frames, zero unparsed lines; capture-end manifest
  reports zero detected socket drops and complete raw streams.
- `can-event-window` jobs `20261015T035704Z-f6733225` (reference drive) and
  `20261015T035704Z-eaa545e6` (independent `broker-drive-20261014T180442821084`,
  B-CAN chunks 0–2): select exact `11:088`; preserve payloads and line numbers.
- Initial array jobs: `20261015T034148Z-99839ded` (C-CAN),
  `20261015T034147Z-5ab1e939` (B-CAN), `20261015T034147Z-aa619ea0` (CAN-CH).
  Window 02:22:10–02:25:00Z: 462,833 / 30,125 / 319,195 frames respectively,
  zero malformed lines. B-CAN full-drive array job: `20261015T035231Z-83525ccd`.
- Still job `20261015T034149Z-7efb7c6d`: 18 native AVFoundation stills at
  five-second requested intervals, exact seek, actual PTS in stdout. Temporary
  code/tasks remain reproducible from the compute source snapshots.
- Working files: `tmp/ecu_mapping/compass-20261015/`. It holds the source
  scripts, reports, selected frames, stills and private street geometry.
  No GPS coordinates, route street names, complete video images or VIN are
  promoted to the reference JSON.
- Orientation source: [OpenStreetMap](https://www.openstreetmap.org/copyright),
  API 0.6 street geometry retrieved 2026-10-15, retained privately in the
  working directory. Earlier Jeep-family search material supplied no layout
  applicable to this ProMaster. Local OEM EVIC documentation lists a compass
  display but does not establish this frame or its transmitting module.
