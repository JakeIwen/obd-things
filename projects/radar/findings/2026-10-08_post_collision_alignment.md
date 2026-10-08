# October 1 collision report, subsequent radar drift and ACC engagement

Owner reports a minor front collision on **Thursday October 1, approximately
9 pm Central**, parking 5–10 minutes afterward, then a separate ~5-minute drive
before remaining parked for at least 24 hours. This corrected date supersedes
the initial October 4 estimate. The exact impact time and any subsequent
physical adjustments have not been established. The drive analysis used saved
evidence only. A separately owner-authorized parked DTC scan subsequently
completed October 8; its results and passive restoration are recorded below.
No calibration, DTC clearing, or service configuration changes were made.

## Result

The staggered changes are present in the ECU's `0845` replies, before the
dashboard's averaging. Adaptive ACC engagement is also recorded while the
inferred vertical deviation exceeds 3° and horizontal deviation exceeds 1.3°.
Neither the dashboard's ±1° monitoring reference nor the historical fault at
approximately −1.26° establishes a firmware shutoff threshold.

The completed October 8 parked scan returned **C1418-78 status `08`**, unchanged
from September 24: confirmed history, without current-test-failed, pending, or
warning-requested bits. No horizontal-misalignment DTC was returned. This adds
a current parked fault observation, not a running alignment acceptance test.

The existing signed big-endian i32 pair / 1,000,000 interpretation remains
inferred. These values are not an independently measured bracket angle, an
established correction command, or proof of safe ACC/FCW performance.

## Recorded timeline

### Owner clarification: likely collision drive and subsequent parking

The reported sequence closely matches trips 71 and 72, followed by the long
gap before trip 73. All times in this small table are **CDT (UTC−5)**:

| Trip | Recorded activity | Interpretation |
|---|---|---|
| 71 | Thu Oct 1, 8:20:49–8:45:59 pm | Likely collision drive, based on the owner's sequence |
| 72 | Thu Oct 1, 8:57:03–9:02:57 pm | Separate 5 min 54 s interval, matching the short follow-up drive |
| 73 | Sat Oct 3, 10:28:34–10:46:09 pm | Next recorded drive, after a 49 h 26 min gap |

The C-CAN manifests for trips 71/72 independently place their last ignition
frames near those activity endpoints; both captures completed without detected
socket drops and ended after the configured 20-second ignition-ID absence.
If trip 71 was the collision drive, parking 5–10 minutes afterward would place
the impact around **8:36–8:41 pm CDT**, rather than exactly 9 pm. This is a
sequence-based estimate, not an impact identified from CAN or a precise timestamp
supplied by the owner. Unrecorded drives still cannot be excluded from logs alone.

The alignment readings stayed near their previous values during both October 1
intervals: trip 71 elevation +0.186160° → +0.176938°, azimuth +0.110194° →
+0.121172°; trip 72 elevation exactly +0.176938° throughout 31 fresh distinct
observations, azimuth +0.120980° → +0.120392°. Trip 73 starts with the same
elevation +0.176938° and near-identical azimuth +0.121021°, then its large
horizontal change begins several minutes into that drive.

This clarification places the horizontal drift **after the reported collision**
and makes delayed updating during subsequent driving a stronger explanation.
The two-day calendar delay includes the long parking interval; the recorded
end/start estimates provide no evidence of a large change across that interval.
The first major horizontal change is on the next recorded drive, rather than
requiring several additional recorded drives to appear. Physical stability and
the firmware's exact learning rules remain unproven.

### Full retained drive summary

Times below are **UTC**. Subtract six hours for the Pi's configured MDT, or
five for CDT. In particular, the first major horizontal change is on
**October 3 evening locally**, despite the capture's October 4 UTC date.
That is consistent with the owner's corrected October 1 collision date.

Values are the first → last fresh, distinct historian observations per trip,
except the final row's endpoint, which is the last complete raw ECU reply.

| Trip | Recorded interval, UTC | Vertical / elevation | Horizontal / azimuth |
|---|---|---|---|
| 69 | Oct 1 02:09–02:22 | +0.206 → +0.192° | +0.107 → +0.117° |
| 70 | Oct 1 23:55–Oct 2 00:18 | +0.192 → +0.186° | +0.117 → +0.110° |
| 71 | Oct 2 01:21–01:46 | +0.186 → +0.177° | +0.110 → +0.121° |
| 72 | Oct 2 01:57–02:03 | +0.177 → +0.177° | +0.121 → +0.120° |
| 73 | Oct 4 03:29–03:46 | +0.177 → +0.168° | +0.121 → −1.116° |
| 74 | Oct 4 21:38–21:53 | +0.168 → +0.129° | −1.116 → −1.336° |
| 75 | Oct 4 23:27, only ~28 s of trip activity | +0.142 → +0.140° | −1.295 → −1.295° |
| 76 | Oct 5 00:18–00:35 | +0.142 → +0.056° | −1.295 → −1.308° |
| 77 | Oct 7 22:57–23:17 | −1.423 → −2.920° | −1.319 → −1.351° |
| 78 | Oct 7 23:45–23:55 | −2.654 → −2.886° | −1.349 → −1.355° |
| 79 | Oct 8 00:29–00:34 | −2.885 → −2.886° | −1.353 → −1.354° |
| 80 | Oct 8 03:22–03:40 | −2.886 → −3.300° | −1.354 → −1.377° |

Trip 73's raw azimuth stays around +0.12° for the first several minutes,
then changes mostly during 03:35–03:44 UTC: +0.117292° at 03:35:00,
−0.046041° at 03:36:39, −0.510695° at 03:40:08, and −1.083796° at 03:44:10.
Elevation remains close to +0.17° through that drive.

Trip 77 contains 112 distinct elevation observations: all 111 changes are
negative, totaling −1.496869° in about 20 minutes. At the next startup,
however, the reading is **−2.653799° instead of −2.919840°**, a 0.266041°
movement back toward zero across the unobserved shutdown/startup interval.
Trip 80 holds elevation near −2.886° early, then falls much faster around
03:35 UTC. The recorded series is not a uniform physical creep rate.

## Independent wire check and engagement

Four complete C-CAN full-stream chunks cover trips 73 and 80. Filtering the
saved streams for radar TX/RX and `0x5A0` produces 195 complete `62 08 45`
replies. Of these, 193 have an exact decoded-value match to a historian pair
within 100 ms of the raw response; the other two lack a matching historian
pair under that criterion. The final raw reading agrees exactly with retained
`radar-alignment.json` and is one polling interval newer than the final fresh
trip-80 historian pair (−3.294701°, −1.377313°).

Selected exact replies, with the established candidate decode:

| UTC timestamp | UDS payload | Elevation, azimuth |
|---|---|---|
| Oct 4 03:28:47.640676 | `62 08 45 00 02 B3 2A 00 01 D8 BD` | +0.176938°, +0.121021° |
| Oct 4 03:45:59.390319 | `62 08 45 00 02 91 06 FF EE F8 9C` | +0.168198°, −1.116004° |
| Oct 8 03:37:16.380421 | `62 08 45 FF CF AE 74 FF EB 03 3B` | −3.166604°, −1.375429° |
| Oct 8 03:39:46.410854 | `62 08 45 FF CD A4 A3 FF EA FB DF` | −3.300189°, −1.377313° |

From **03:37:19.141313 to 03:37:38.341629 UTC October 8**, 34 `0x5A0`
frames report state 2 (engaged), graphic index 10 (adaptive, no lead icon),
and set speeds 21–23 mph. Their latest preceding radar observations, at most
12 seconds old, have elevation −3.166604° or −3.182331° and azimuth below
−1.375°. Two fresh historian snapshots independently corroborate adaptive
engagement. This establishes reported engagement, not correct target tracking
or safe braking. It is a short observed engagement, not a highway validation.

## Gaps and fault-code limits

- No recorded trip lies between trips 76 and 77. Elevation changes from
  +0.056389° to −1.422971° across that gap. Missing drives reported by the
  owner could contain its onset; do not interpolate this interval or claim
  that the radar changed while parked. A separate indexed check found no fresh
  non-trip radar samples from October 1 onward that could fill this gap.
- Trip 75 has only three fresh radar observations. Its C-CAN manifest confirms
  ~46 seconds of capture ending after 20 seconds without the ignition ID,
  rather than proving a longer drive was recorded.
- September 28 trip rows remain, but their raw samples have aged out under
  the seven-day historian retention policy. They are not evidence of a
  broker failure. September 30/October 1 local drives provide the retained
  baseline before the large reported-angle change. The October 1 readings are
  not all necessarily pre-collision observations, given the owner's clarification.
- Before the fresh scan below, the newest saved radar DTC observation was
  **September 24 21:25:34Z**:
  C1418-78 status `08` (confirmed history, not current). A cache generated
  October 8 still contains that old observation. It does not establish the
  post-collision DTC state or explain the owner's failed engagement. The later
  October 8 scan supplies the new observation; it cannot timestamp an earlier
  temporary fault during a drive.
- Completed selected EXFAT chunks remained readable and their manifests were
  complete. The sandbox mount view reports `ro`, while mountinfo reports the
  underlying EXFAT superblock as `rw`; the sandbox root is also read-only.
  This does not establish a host disk failure or explain missing recordings.
  The recorder reports active, zero restarts, and waiting for drive ownership.
  Available service journals start October 6, so they do not establish any
  earlier broker-failure sequence. No storage changes were attempted.

## Interpretation and next diagnostic step

### Saved module fault review

Follow-up inspection found no populated per-module `dtc` result in the saved
October `tmp/vehicle_data/van_scans/scans/broker-drive-202610*.json` reports.
Recent report filenames are not proof of a recent fault scan: for example,
the October 8 03:39:08Z report is classified `other_f1_traffic` and contains
only a positive PCM oil-life DID `2185` response. Its `dtc` field is null even
though the report has a top-level `dtc_request` schema field.

Before the fresh read below, the latest actual module scan was September 24.
These are its statuses **at that observation**, not current October diagnoses:

| Module | Latest saved fault evidence, September 24 |
|---|---|
| Radar | C1418-78 vertical misalignment, status `08`: confirmed history; testFailed/pending clear. No C1417-78 returned in that scan. |
| BCM | B104D-15, B104E-15, B162A-15, B162E-15, B1632-15, B1636-15 had testFailed set. Reviewed labels identify DRL/low-beam/high-beam circuits for the first five; B1636's exact label is unresolved. The owner previously confirmed both low-beam codes come from the LED conversion. |
| Shifter | P1C73-24 had testFailed set; P081C-64 was confirmed history. Exact module-scoped labels remain unresolved. |
| Uconnect | B1577-13 had testFailed set; B210A-16 was pending/confirmed without testFailed; four other codes were confirmed history. |
| ABS | C1200-17 was pending/confirmed, without testFailed, and test not completed this cycle. |
| HALF | B1006-49 was pending/confirmed without testFailed; C14A5-92 and C14A5-97 were confirmed history. |
| Climate / cluster / RF Hub | Confirmed-history communication/other codes; no testFailed bit in their returned records. |
| PCM / TCM / EPS / ORC / ICS / EMCM2 | No DTCs returned in that scan's supported query. |

Sources: `tmp/vehicle_data/van-scan-cache.json` per-module `observed_at` and
status bytes, the [September 24 scan finding](../../ecu_mapping/findings/promaster_2022/2026-09-24_in_vehicle_f1_scan.md),
and reviewed module-scoped descriptions in `projects/vehicle_data/dtc_descriptions.py`.
The separate Pi DTC cache was older (July) before the successful owner scan.
Successful `0845` data replies establish diagnostic responsiveness, not an
absence of fault codes. These pre-collision scans alone did not establish
post-collision module-fault state.

### October 8 attempted parked DTC scan

The owner explicitly confirmed parked, ignition on, engine off, and selector
in Park. Fresh broker evidence corroborated zero RPM/speed and ignition on;
the active-drive helper was idle. Serial-resolved C-CAN, B-CAN and CAN-CH were
classical/listen-only/ERROR-ACTIVE, at their expected bitrates with restart-ms
zero, no errors, and no operation inhibits. The offline batch plan covers 15
modules using only physical `19 02 FF`; PCM remains unsupported.

The installed guarded HTTP-to-systemd worker was used without changing its
configuration. Job `dtc-web-20261008T085752Z-497b6ab9` refused the C-CAN role
lock before any request. The continuous display receiver holds a cooperative
shared lease; the batch tool itself does not reserve `can_handoff.active_turn`.
For a second attempt, the existing cooperative handoff was reserved for the
three roles in the batch and held until the worker terminated. Job
`dtc-web-20261008T090007Z-b02b0c46` got past contention but refused its
noninteractive sudo authorization check for the exact C-CAN down/arm/restore
commands. Its underlying privilege-policy cause was not established; do not
infer it solely from this session's sandbox or change worker restrictions.

Both ledgers show **queried=0, imported=0**, with no restoration failure.
After release, TX counters were unchanged on all four channels (C 10,721,
B 865, CH 0, spare 0), every vehicle role remained exact passive/error-free,
the spare remained down, and no inhibit existed. The broker resumed reporting
fresh ignition-on/zero-RPM state. Evidence and the subsequent normal-SSH handoff
are under `tmp/radar/post-collision-20261008/`. These two worker attempts did
not scan any module.
An initial SSH handoff unnecessarily included `sudo -v`, which prompted the
owner for a password before the scan. It was superseded by a syntax-checked
handoff that uses only the scanner's existing noninteractive sudo checks and
link commands. Sudo policy was not modified. The exact reason the installed
worker's authorization checks failed was not isolated.

### Completed October 8 parked scan

The owner executed the corrected handoff in the normal SSH session. Job
`dtc-owner-20261008T090715Z` ran **09:12:42–09:13:00 UTC** (04:12:42–04:13:00
CDT). All 15 supported modules returned strictly parsed positive responses;
all 15 reports were imported after verified passive restoration. PCM was
explicitly skipped as unsupported. Requests were fixed physical `19 02 FF`
with the existing state/identity/lock/rate gates, plus required ISO-TP flow
control. No session change, TesterPresent, routine, reset or clear was sent.

Radar's complete reply:

```text
59 02 CF 54 25 45 40 54 22 66 40 54 22 49 40 54 18 78 08
54 29 68 40 54 29 66 40 54 20 25 40 54 20 66 40
```

The only radar record with fault-history bits is **C1418-78 = `08`**,
identical to its September 24 status. There is no pending or testFailed bit,
no warning request, and no C1417-78 horizontal-misalignment record. The other
seven entries are `40` (test not completed this operation cycle only), not
seven additional failures. Engine-off reading does not itself exercise the
OEM running/moving alignment monitor or certify the physical mounting.

| Module | Non-incomplete records on October 8 | Comparison / interpretation |
|---|---|---|
| Radar | C1418-78 `08` | Same confirmed-history status as September 24; no active/pending radar DTC |
| BCM | B104D-15, B104E-15, B162A-15, B162E-15, B1632-15, B1636-15 all `4D` | Same six testFailed codes existed before the collision; low-beam LED context remains applicable |
| Shifter | P1C73-24 `0F`; P081C-64 `08` | Same active and historical records as September 24 |
| ABS | C1200-17 `4C` | Same pending/confirmed, last-test-not-failed state as September 24 |
| HALF | B1006-49 `0C` | Pre-existing pending/confirmed record; earlier C14A5 records not returned |
| RF Hub | B1040-64 `0C`; C1503-31 `08` | Operational-mode plausibility pending; left-rear TPMS no-signal history. Both codes were recorded in July; not newly established collision faults |
| Climate | U1427-87 `0E`; B10E8-1C `08` | Missing-message subtype pending, testFailed clear; U1427 was already recorded in July. B10E8 is newly observed stored history with no reviewed component label; onset/collision relation unknown |
| Uconnect | B143A-11, B1570-19, B1577-13, B210A-16, B280B-02, U0155-86 all `08` | Confirmed history only; B1577 was active and B210A pending in September |
| Cluster | U1741-87 `08` | Same confirmed-history record |
| TCM / EPS / ORC / telematics / ICS / EMCM2 | No current, pending or confirmed-history records | Some return only `40` incomplete-test entries; that is not a fault count |

Across the 15 modules, the 300 returned status records comprise 7 current,
4 additional pending, 11 confirmed-history, and **278 incomplete-only** records.
The current-code set consists entirely of the same six BCM codes plus shifter
P1C73 seen before the collision. First-seen timestamps in the Pi history alone
can be misleading for CAN-CH modules, whose September observations are stored
in the separate in-vehicle-scan tables; comparison above uses both sources.

Independent post-scan checks confirmed C/B/CH classical CAN, listen-only,
ONE-SHOT/FD absent, ERROR-ACTIVE, correct rates, restart-ms zero, and zero RX,
TX and controller error counters. The spare remained down. TX deltas from
the pre-scan baseline were **14 / 6 / 7 / 0** for C/B/CH/spare. No operation
inhibit existed; the broker returned HTTP 200 with fresh ignition-on/zero-RPM
state and an idle helper. No diagnostic retry is needed for this completed job.

During the follow-up the owner changed this agent's permissions mode. A fresh
read-only check found `NoNewPrivs: 0` and authorized exact noninteractive sudo
down/arm/restore commands for every resolved role. Recheck effective permissions
before delegating commands to the owner; do not add broad `sudo -v` prompts
when the reviewed path uses noninteractive exact-command checks.

Per-module reports are `tmp/inventories/<module>/dtcs_batch_dtc-owner-20261008T090715Z_<module>.json`;
the job ledger is under `tmp/inventories/dtc-batch/dtc-owner-20261008T090715Z/`.
`tmp/radar/post-collision-20261008/dtc-scan-summary.json` retains report hashes,
per-module statuses and restoration flags. `dtcs-after-scan.json` preserves
the enriched cache response; `dtc-after-owner-interfaces.json` and
`dtc-after-owner-status.json` preserve the independent restoration check.

### Interpretation

**Leading explanation, not a firmware determination:** `0845` exposes learned
or filtered alignment estimates. A step in actual aim can produce gradual
changes in those estimates as the ECU accumulates useful driving evidence.
Azimuth and elevation need not become observable at the same time or share
the same filtering/acceptance rules. The within-drive plateaus, ramps and
between-drive reversions fit this interpretation. Continued physical movement
of a displaced mount, changed loading, and environment-dependent bias remain
possible; these logs alone cannot separate them.

Bosch's [elevation-estimation patent US20160161597A1](https://patents.google.com/patent/US20160161597A1/en)
describes selecting reliable target observations, accumulating a sliding-time
histogram, and applying a confidence threshold before reporting an estimate.
It supports this mechanism as plausible supplier practice; it is **not** an
identified implementation specification for this MRR1evo14F firmware.

The local 2022 ProMaster C1418-78 chart defines the fault in terms of invalid
or missing calibration while engine-running and speed conditions are present;
it gives no ±1° or ±3° threshold. The old faulted −1.26° drive therefore
cannot define a universal capture window or instantaneous disable threshold.
The [vehicle-specific OEM summary](../docs/oem/alldata_ram2022_C1418-78_and_acc_alignment.md)
and [FCA STAR S2123000064](https://static.nhtsa.gov/odi/tsbs/2021/MC-10204352-9999.pdf)
prioritize mounting/seating inspection before calibration; the latter also
describes bumper contact, but its illustrated geometry is not independently
confirmed for this van. Inspect the actual bracket/bumper before choosing a
correction. The completed parked scan above does not exercise a moving monitor.
Do not use the inferred degree magnitude/sign as a physical adjustment recipe.
ACC accepting engagement is not an alignment acceptance test.

## Reproduction and provenance

Working evidence: `tmp/radar/post-collision-20261008/`. Inputs are unchanged.
Bounded, indexed trip/metric exports used `tools.warning_replay.ReadOnlySource`
with `mode=ro`, `query_only`, one read transaction, and per-statement deadlines.
An initial broad export hit its four-second limit and was abandoned; indexed
per-trip reads completed with a maximum statement time of 0.199 seconds.
Fresh rows were deduplicated by observation timestamp for each axis/trip.
All subsequent analysis and full CAN searches ran through named compute tasks.

- Historian analysis: `20261008T064717Z-2db5383d`; 1,518 fresh rows per axis,
  927 distinct observations per axis across trips 69–80. Original inputs:
  `samples.csv` SHA-256 `0e953c5bf1ee7218ab59aded92ecb907554ee7f3472ae5e4312b1470e9fb07f3`;
  `metadata.json` SHA-256 `8578b96641270aabaf3f1b2467751e1272220aa8b2b54a0b35aca4ce31fd22cf`.
- Raw extraction: existing `can-event-window` task, `20261008T064857Z-acbb0371`.
  Sources: `broker-drive-20261004T032837024401/c-can/` and
  `broker-drive-20261008T032157578587/c-can/`, full chunks 0 and 1 in each,
  under `/mnt/EXFAT512/obd-things/tmp/captures/three_bus_drive/broker-drive/`.
  Job metadata retains every input hash. Selectors: `29:18DA2AF1`,
  `29:18DAF12A`, `11:5A0`; maximum 10,000 selected frames.
- Wire/historian cross-check: `20261008T065120Z-8a5a062c`, exact DID/length,
  FirstFrame plus consecutive-frame reconstruction within 200 ms.
- Interactive historian chart: `20261008T065256Z-4a046eff`,
  `tmp/radar/post-collision-20261008/alignment-trend.html`; no line across trips.
- Local OEM chart was re-read from the available `~/dev/ram_2022_GAS` mirror.
  Web references above were checked October 8, 2026. The newer 08-065-25
  bulletin concerns selected 2024 ProMasters and was not applied to this 2022.
