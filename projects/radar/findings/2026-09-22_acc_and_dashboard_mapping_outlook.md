# ACC and dashboard mapping outlook — 2026-09-22

## Available drive evidence

After September 6, the archive contains 28 new intervals through September 22:
7.031 hours of C-CAN, with comparable B-CAN/CAN-CH coverage. Their full streams
contain 68,162,080 / 4,463,239 / 47,349,875 frames (119,975,194 total).
All 84 role checkpoints report complete full streams and zero detected drops.
Twenty-six capture sets completed normally; the September 15 19:54Z and
September 22 03:10Z sets retain ownership-loss failures at wrapper level.
Do not relabel those two sets complete or infer coverage outside recorded intervals.

Inventory source is each role's checkpoint/manifest under
`/mnt/EXFAT512/obd-things/tmp/captures/three_bus_drive/broker-drive/`, from
`broker-drive-20260910T211505044696` through `broker-drive-20260922T162227961102`.
The EXFAT mount was writable with about 41 GiB free. Broker/recorder were active,
recorder waiting, vehicle asleep. This review sent no CAN traffic and changed
no service or production configuration. Kernel names below are capture provenance.

## Dashboard gaps

| Item | Current evidence | Useful next step |
|---|---|---|
| Raw cluster `1000` | RPM label established; later raw/broadcast comparison supports `/4 rpm`; ordinary RPM already works | Unpopulated diagnostic input, not another unresolved RPM gauge |
| Raw cluster `1002` | Vehicle-speed label established; current one-byte scale unresolved; ordinary speed works from `0x101` | Low priority unless an independent diagnostic source has a specific use |
| Cluster `0107` / Gear | Controlled `00 -> P`; other enum values unverified | Labeled P/R/N/D references; distinguish selected range from engaged gear |
| Cluster `1005` / Outside temperature | Current Alfa pairs `77 -> 19.5 C`, `7A -> 21 C`, matching `raw * 0.5 - 40 C`; catalog candidate agrees | Near-ready under `observed_alfa_scale`; fresh support/plausibility check and integration |
| Oil life | Offline follow-up establishes PCM `2185`, unsigned byte in percent; historical `11` matches 17% | Bounded parked no-session-change support check, then slow polling; [evidence](../../ecu_mapping/findings/promaster_2022/2026-09-22_gear_and_oil_life_offline.md) |
| Odometer | ICS `2001` and passive `0x760` have replicated relative-distance evidence; cluster offset unresolved | Passive starred fallback and availability work |
| Fuel level | PCM `0227` supported; original plotted samples all full | Below-full reference to discriminate two surviving formulas |

The four raw cluster registry slots accept a local publisher; the production
poller does not acquire those DIDs automatically. Empty raw slots, genuinely
unmapped gauges, stale readings, and acquisition failures are different issues.
References are ECU-mapping findings `2026-07-24_cluster_singleton_correlation.md`,
`2026-07-26_cluster_did1000_broadcast_correlation.md`,
`2026-07-30_legacy_pcm_cda_overlap.md`, and
`2026-09-16_service_dashboard_evidence.md`. The latter's four saved catalog
queries were checked: unresolved resource indirection prevents a simple English
oil-label search; an empty query does not prove the measurement is absent.

## New production wire validation

Campaign `broker-drive-20260922T042152731314` covers approximately September 21
22:22–23:28 MDT. Exact diagnostic extraction of C-CAN priority chunks 0 and 6
found 935 request/positive pairs each for PCM `01A1` and `06DA`, plus 161 pairs
for `069F` spanning 81–92 C / 177.8–197.6 F. There were no physical cluster or
TCM diagnostic exchanges in these sampled windows, so they provide no fresh
label/reference for those remaining dashboard slots.

Radar `0845` had 85 requests, 85 expected first frames, 85 fixed FlowControls,
and 85 sequence-1 completions with exact 11-byte `62 08 45` replies.
Contiguous inter-request intervals were 10.107–11.149 seconds and maximum
complete-response latency was 34.140 ms. Under the inferred decoder,
elevation ranged -0.023621..+0.018857 degrees and azimuth +0.143079..+0.164332.
This verifies the running transport alongside PCM polling, not independent
angle scaling or a firmware fault band. The unsampled middle is not evidence
of uninterrupted diagnostic polling.

Job `20260922T211640Z-0f943711`, `window.json` SHA-256:
`8cc59706a3276dc066733729887329d41c4808223cc2bd244f29e4c19977fc80`.
The new VVT reference samples also support a bounded offline passive-carrier search.

That coarse screen completed as `20260922T213503Z-4144c772`: all 161
references were verified against exact original frame sequence, timestamp,
namespace, identifier and payload in priority chunks 0 and 6. The maximum
eligible R-squared was only 0.528362 (`0x0EE` byte 5; fitted RMSE 2.049 C),
with closely ranked `0x101` speed-related views. No convincing direct VVT
carrier was identified. This result covers the configured coarse fields in
the priority streams, not every full-bus bitfield or multiplexed encoding;
retain the working direct `069F` source rather than escalating an unpromising
shortlist. Exact reference file:
`tmp/ecu_mapping/vvt-sep22-v2/pcm_wire.jsonl`, formatted from the filtered
window by `tmp/ecu_mapping/build_vvt_wire_sep22.py`. Earlier preparation jobs
rejected the basename, field spelling, or one-based sequence number before
analysis; those failures are not vehicle evidence. The successful run uses
the required basename, `byte:0`, and verified zero-based global frame sequence.

## ACC settings and target data

The [2022 ProMaster manual](https://vehicleinfo.mopar.com/assets/publications/en-us/Ram/2022/ProMaster/P5580858-22_VF_OM_EN_USC_DIGITAL_V3.pdf),
printed pages 70–72, establishes cluster-visible off/ready/set states, set speed,
and four following-distance levels. These are suitable first read-only targets.
A gap level is not measured range; set speed alone cannot distinguish adaptive
from fixed-speed cruise.

The maintained PCM Plots catalog provides concrete vendor request candidates:

| PCM DID | Label | Boundary |
|---|---|---|
| `0891` | Target cruise speed | Support, physical scale, and cruise-mode semantics still unverified |
| `06E3` | Estimated gear | Not a verified PRND or engaged-gear decoder |
| `0127` | Outside temperature | Alternative candidate; cluster `1005` has stronger current evidence |

The installed radar's previously matched Alfa subtype is Device 8905,
`ADAPTIVE_CRUISE`. Its common Plots group is Device 220: vehicle speed,
target cruise speed, voltage, internal temperature, brake pressure, steering
angle, and six alignment/deviation labels. Related groups 232 and 330 were
also inspected. None labels lead range or relative target velocity. That does
not exclude other status data or broadcasts. This profile already contains
incompatible alignment definitions, so a row's presence/order is not a radar
DID or support proof.

Catalog query job `20260922T211817Z-f4470555` joins `Param_devices`,
`Devices_params_units`, `Param_names`, and `Units`; stdout SHA-256:
`121b830dc6555fb1d40a27ce90835b7f328c9d03524a3af83f3b27985553578b`.
Metadata jobs: `20260922T211450Z-146a9a68`, `20260922T211642Z-1742274b`.
Proprietary database and application artifacts remain under ignored `tmp/`.

### Related-platform screening

The primary [opendbc CUSW definition](https://raw.githubusercontent.com/commaai/opendbc/master/opendbc/dbc/chrysler_cusw.dbc)
suggests related-platform leads `0x3EE` (ACC display), `0x2EC` (ACC state),
and `0x2FA` (buttons). Its [platform registry](https://raw.githubusercontent.com/commaai/opendbc/master/opendbc/car/chrysler/values.py)
assigns that definition to a Jeep Cherokee family, not this ProMaster.

In the selected drive's first ten minutes, `0x3EE` was absent on every tap.
`0x2EC` appeared on C-CAN/CAN-CH at 20 Hz but remained all-zero. `0x2FA`
appeared on both at 50 Hz with DLC 4 versus the related definition's DLC 3.
The related gear ID `0x1F4` has DLC 8 here versus 7 there. This rules out
copying that decoder unchanged; it does not establish current frame meanings
or prove ACC was off. Owner confirmation of ACC use during this leg was
requested but unavailable at this checkpoint.

Summary jobs: C-CAN `20260922T212020Z-4b30bba8`, B-CAN
`20260922T212020Z-462db24d`, CAN-CH `20260922T212020Z-c22c9378`.

## Bounded next steps

1. The saved oil-life decoder link is now recovered: PCM `2185`, unsigned
   percent byte. Verify no-session-change support in a bounded parked check
   before adding a slow production poll; July's 17% is not current telemetry.
2. Integrate the near-ready outside-temperature mapping and resolve the gear
   enum using explicit references; avoid redundant RPM/speed polling merely
   to populate the old diagnostic slots.
3. Map ACC off/ready/engaged, set speed, and selected gap first. Start with a
   bounded PCM `0891` support/scale question and passive captures of normal
   button changes. Physical buttons/display supply the references; no CLI
   substitutes for those observations.
4. A short normal-driving segment with known set speed supplies the moving
   reference. Logging can be staged beforehand or annotated by a passenger;
   do not require driver interaction with logging equipment or provoke FCW.
5. Pursue target-present/range/relative-speed after exact current definitions
   or independent references exist. No verified current DID assigns these
   meanings. Routine, calibration, configuration, SecurityAccess and ABS
   polling are outside this telemetry plan.
