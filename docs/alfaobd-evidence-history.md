# AlfaOBD evidence history — observed mis-mappings, limitations, and false leads

This is the canonical cross-project ledger for AlfaOBD behavior observed while working on this
2022 Ram ProMaster. It records both confirmed definition/rendering defects and the less obvious
provenance traps that can turn otherwise useful AlfaOBD data into a wrong conclusion.

The scope is deliberately narrow: these findings apply to the named ECU/profile and captured
application version or session. They are not a claim that every AlfaOBD definition is wrong. The
July 2026 tablet campaigns used AlfaOBD 2.4.4.0; the version used for the June radar work was not
preserved in tracked evidence.

## Classification

- **Confirmed mapping/rendering defect** — independent raw UDS, exact ECU identity, or a controlled
  physical state contradicts the AlfaOBD procedure, label, enum, or numeric rendering.
- **Incompatible selected profile** — the chosen definition does not match the installed ECU subtype.
  This can produce positive reads with wrong semantics; when AlfaOBD warned about the mismatch, that
  warning is evidence that the operator continued outside the supported definition.
- **Artifact/provenance limitation** — the recording or catalog can be useful, but its structure does
  not support the conclusion someone might naturally draw from it. This is not necessarily an app bug.
- **Project correction** — our own earlier interpretation was wrong. These entries are retained so the
  error is not rediscovered or incorrectly attributed to AlfaOBD.
- **Unresolved** — suspicious behavior exists, but present evidence cannot distinguish a bad AlfaOBD
  definition from state, session, variant, or an unobserved internal field.

## Chronology

### 2026-06-12 through 2026-06-13 — radar calibration and live-data definitions

This was the first confirmed AlfaOBD mis-mapping in the repository.

**Confirmed mapping defect — wrong calibration procedure for this radar variant.** The AlfaOBD
profile observed in June 2026 called `Active alignment: radar calibration` and sent
`31 01 0250 01`. The Bosch radar rejected it with
`7F 31 31` (`requestOutOfRange`). An independent result-only scan found that `0x0251` was the only
recognized routine in `0x0200-0x03FF` plus `0xFF00-0xFF03`: `31 03 0251` returned `7F 31 24`, while
`31 03 0250` returned `7F 31 31`. Later direct testing proved the working start is
`31 01 0251` in extended session `0x03`, with **no option byte**.

The early description of this as merely an "off-by-one RID" was incomplete. OEM research on
2026-06-18 established that AlfaOBD was invoking a static-mirror car procedure (`0x0250` plus a
mirror-position option), while this ProMaster requires dynamic Service Drive Alignment (`0x0251`,
no mirror and no option). Changing only `0250` to `0251` in the Alfa request would still be wrong.
On 2026-06-27 the direct `0x0251` drive routine reached 100 percent and cleared the active fault,
independently confirming the final procedure diagnosis.

AlfaOBD's separate `PROXI alignment` wording is another nearby semantic trap: PROXI synchronizes
vehicle configuration and can support a retrofit, but it is not radar boresight calibration and
does not clear C1418-78.

**Confirmed mapping defect — the displayed radar misalignment gauges hid the fault.** AlfaOBD
repeatedly polled DIDs `083E`, `083F`, `0846`, `0830`, and `0860`; this firmware rejected them with
`7F 22 31`. The UI consequently showed unsupported or near-zero misalignment values despite an
active C1418-78 vertical-misalignment fault. Independent inventory and drive evidence found useful
angle state in `0841`, `0845`, and `0850`; `0845`/`0850` then tracked the successful physical
adjustment and SDA. Their exact Bosch names/scales and any one-to-one replacement for each failed
AlfaOBD gauge remain reverse-engineered rather than ODX-certified. AlfaOBD's voltage (`1006`) and
temperature (`0835`) gauges did match raw data, which bounds the failure to the variant's alignment
definitions rather than the entire connection.

Sources: [radar AlfaOBD bug report](../projects/radar/docs/radar_acc_alfaobd_bugreport.md),
[radar handoff](../projects/radar/docs/AGENT_HANDOFF.md), and
[successful SDA evidence](../projects/radar/findings/adjustment_1_results_3.md).

### 2026-07-07 through 2026-07-09 — capture provenance and manual ISO-TP

**Artifact/provenance limitation — a recording is not one vehicle or one ECU by construction.**
AlfaOBD Debug bins and text info logs can accumulate across years, vehicles, selected profiles, and
sessions. `Recording data for X` names the operator-selected profile, not confirmed installed
hardware. The recovered 396 MB historical Debug bin is a multi-profile aggregate whose only
F190-identified vehicle is the former 2015 diesel, even though it also contains unrelated later
profile probes. The Body Computer info log is likewise cumulative and mixed: early records belong
to the old van, while only its timestamp-aligned 2026 tail applies to this one.

**Project correction — ISO-TP consecutive frames are not UDS commands.** An early exploratory pass
treated frame fragments beginning `27`, `2A`, and `2B` as SecurityAccess or other service requests.
They were PCI sequence bytes inside a long, manually framed `2E 2023` PROXI/configuration write.
After proper first-frame/consecutive-frame reassembly, the session contains no `27` request. Later
parser hardening also accounted for an interleaved adapter command, a final consecutive frame without
the usual trailing response-hint digit, response-pending, and the eventual positive write response.
This was our parser error, not an AlfaOBD SecurityAccess error.

**Artifact/provenance limitation — labels and time are split across artifacts.** Debug Data contains
raw requests/responses but not a reliable label for every action. Gauges Data has rendered labels and
values but no DID numbers. AlfaOBD may write the full date only when recording closes, so an unclosed
or long-open file cannot safely be backdated from a later close marker. A label-to-DID claim therefore
needs an exact time-aligned join or a controlled one-variable capture, not matching filenames or
nearby menu order.

Source: [ECU-mapping data provenance and parser pipeline](../projects/ecu_mapping/README.md).

### 2026-07-07 through 2026-07-19 — TPMS position labels

**Not attributable to AlfaOBD, but an important rendered-label trap.** Historical C1504 text named
the rear-right sensor, and two replacements may consequently have targeted the wrong physical
corner. Controlled deflation/reinflation later established the actual wheel-to-ID map and showed
crossed RF Hub position records; the eventual live dropout was a different ID physically at rear
left and paired with C1503. Current evidence says the RF Hub's localization/position state can be
wrong or can change, not that AlfaOBD mistranslated the DTC. Treat an AlfaOBD wheel name as the ECU's
reported slot, then identify a service target by sensor ID plus a physical pressure change.

Two related project corrections are also not AlfaOBD defects: `40A6-40A9` were initially treated as
a permanent position table but proved to be fault-linked records, and the local CSV logger rendered
the RF Hub's raw `FFFF` no-pressure sentinel as `950.5 psi` by applying the ordinary scale. Neither
should be cited as an AlfaOBD label or scaling failure.

Source: [TPMS handoff and controlled wheel map](../projects/tpms/README.md).

### 2026-07-21 — APK catalog, B-CAN status, and Climate profile

**Artifact/provenance limitation — model-menu rows are candidates, not installed hardware.** The
installed APK's `RAM PRO MASTER (VF) 2022+` catalog includes mutually exclusive engine and module
profiles. Exact live `F1A5` subtype matches superseded several generic menu rows: for example, the
model menu's generic `Marelli AUTO SHIFT` route ultimately connected to the installed ZF 948TE TCM.
Similarly, a selected/runtime profile name can be an alias or definition-family name rather than an
ECU manufacturer or exact vehicle model-year statement.

**Incompatible selected profile — Climate gauges.** The installed Climate ECU returned
`F1A5=000A702520`, which did not match the selected `COND_MARELLI_EP` definition, and AlfaOBD warned
that model/ISO verification failed. Continuing only for observation produced an eight-DID loop with
two explicit `7F 22 31` responses and six positive responses, but all displayed values were constant,
`NA`, or contradicted known state: `-39 deg C` coolant, `0.160 V` battery, `63.5 deg C` ambient, and
near-zero actuator percentages. None of those labels or scales is evidence for this installed ECU.
A positive `62` response does not make a mismatched profile's decoder valid.

**Confirmed rendering defects — BCM status.** In a current-vehicle snapshot:

- `1008=000389A0` is 231,840 minutes, but AlfaOBD displayed `905`, exactly the first three response
  bytes interpreted after truncating the fourth;
- `1009=0023` should be 525 seconds under the cross-module `raw x 15 s` convention, but displayed
  zero; and
- `2013=02` displayed `Not defined` even though the identical value rendered as locked in other
  current-vehicle module snapshots and in an earlier BCM snapshot.

**Artifact/catalog limitation — internal definitions are not a ready-made ODX.** Numeric language
placeholders cannot be decoded by direct zero- or one-based indexing into the extracted English
resource. The catalog also contains malformed or ambiguous numeric metadata, including slope text
`0.10.0` and 32-bit bounds represented as `0..-1`. Raw request bytes, field positions, and unexpanded
resource IDs remain useful, but the human labels and scaling require validation.

**Artifact/catalog limitation — diagnostic menu labels do not bind actions to payloads.** The exact
BCM menu proves that front/rear lock-relay actions exist, but its tables do not join those labels to
the six captured `2F` DIDs. Likewise, a Climate `31 01 0201` start payload was found in application
code without a defensible routine-name join for the installed subtype. Menu order is not a safe way
to label an actuation, routine, write, or configuration payload.

**Unresolved, do not promote as defects.** Uconnect returned negative responses for several
frequency DIDs while the UI showed zero-like values; those may be UI defaults, but the evidence does
not prove the exact renderer path. AlfaOBD's generic EMCM2 status vocabulary included a
`Power Button`, while controlled raw reads mapped a separate physical Screen button bit; without the
exact field decoder, relabeling that bit as an Alfa error would be premature.

Sources: [APK catalog extraction](../projects/ecu_mapping/findings/promaster_2022/2026-07-21_alfaobd_apk_catalog.md)
and [B-CAN live status correlation](../projects/ecu_mapping/findings/promaster_2022/2026-07-21_alfaobd_live_status_correlation.md).

### 2026-07-22 — C-CAN controlled-state contradictions and recording caches

**Confirmed mapping/rendering defects.** All of these were observed on correctly addressed,
identity-verified ECUs while the van was parked:

- The shifter profile rendered `Lever position: Drive` while the van was in Park and the TCM's
  independently decoded hardwired-shifter state also said Park.
- The BCM continued to render the driver-door switch as Closed while the door was open, even though
  raw `0130` changed `8C -> 88` and restored afterward. It also rendered the brake pedal and stop
  lamps inactive during a held service-brake press, while raw `0132` changed `0020 -> 0023` and
  `0150` changed `000000 -> 006000`.
- The selected TCM `Brake switch` watcher did not expose the pedal change: every complete response
  in its eight-DID loop remained identical during the controlled press. This confirms the selected
  watcher is not a usable pedal-state source on this van; it does not yet isolate whether the defect
  is its DID selection, field join, or decoder. Shorter apparent response variants were truncated
  Debug fragments, not physical-state changes.
- TBM2 rendered backup-battery charge as an impossible `235%`, invalidating that percentage scale.
  The adjacent `1.30 V` value remains unverified rather than automatically wrong.
- The generic PCM profile produced plausible core identity, odometer, oil-life, and ignition data,
  but also rendered configuration/qualification labels for impossible vehicle variants. Those
  semantic fields are invalid here even though the profile remains a useful raw transport/DID source.

**Misleading aliases, not proven data defects.** The selected cluster menu said
`Instrument Panel Marelli/Siemens EP`, the connected runtime title said `Instrument panel
Continental`, and the installed identity fields identify Magneti Marelli. The generic TCM selector
said `Marelli AUTO SHIFT` before resolving to ZF 948TE. Treat these names as route/definition aliases,
not hardware identity.

**Artifact/recording limitation — the Status monitor and Plots/Gauges paths write different
evidence.** The C-CAN parameter selections were made through the Status tab's `Monitor parameters`
surface, not the Plots tab. Debug Data grew normally and the profile `*_Info.log` files accumulated
repeated human-readable label/value blocks, but `Gauges_Data.csv` did not grow. The Info blocks carry
no per-cycle timestamp or DID number, so they need a bounded whole-campaign byte envelope plus
singleton selection or raw request-order evidence; sampled per-segment offsets are not record
boundaries. The unchanged Gauges CSV cannot be passed to the timestamp joiner. Do not interpret an
unchanged Gauges file as proof that the Status monitor produced no labeled evidence.

Separately, `ZF9HP.dat` doubled in size only because every one of its 16 baseline series was
concatenated with an exact copy of itself; all 12 `TIGERSHARK_CUSW.dat` series were unchanged. These
`.dat` files are opaque plot caches without a label/DID/timestamp join, not fresh campaign evidence.
Compare pre/post hashes and series content before using any of them.

**Not a profile defect — `NO DATA` can be power state.** The PCM profile made 24 unanswered
fixed-eight-byte `10 92` attempts after the ignition had slept, then received `50 92` and positive
reads after ignition was rearmed with the engine still off. A timeout alone does not distinguish
wrong profile, routing, framing, session, missing hardware, or a sleeping ECU.

Source: [C-CAN AlfaOBD live correlation](../projects/ecu_mapping/findings/promaster_2022/2026-07-22_ccan_alfaobd_live_correlation.md).

### 2026-07-24 — Android log buffering during singleton monitoring

**Artifact/recording limitation — sampled file sizes are not logical record boundaries.** A
seven-segment guarded cluster campaign sampled the Debug and profile-Info sizes immediately before
and after each one-label monitor run. Both files grew during every accepted segment, but the Android
writers flushed buffered blocks: the Info file advanced in exact 8,192-byte increments and Debug
advanced in larger blocks. A literal per-segment byte slice therefore contained a shifted mixture of
logical monitor runs. The first slice lost part of its current Debug loop, while the campaign's outer
Info range included an earlier tail at the start and omitted an unflushed tail at the end.

This does not invalidate file-size checks as liveness or pulled-artifact provenance. A no-growth
window is only a short-window stability witness when the UI also verifies that monitoring stopped;
buffering alone can hide ongoing writes. The result invalidates the stronger assumption that a
`stat` result timestamps the records before it. The safe singleton join uses sampled sizes only for
a whole-campaign envelope, verifies the Info envelope's contiguous label-run order, and resolves
each request from the independently host-timed passive CAN interval. It does not require Info/Debug
sample counts to match at the two outer boundaries. Internal runs can provide stronger ordinal
evidence when their counts do match.

The same campaign also reconfirmed that Status monitoring need not update `Gauges_Data.csv`: Debug
and `MARELLI_DASH_EP_Info.log` grew while the Gauges file remained fixed. This is an artifact-path
distinction, not evidence that monitoring stopped.

Source: [cluster singleton correlation](../projects/ecu_mapping/findings/promaster_2022/2026-07-24_cluster_singleton_correlation.md).

### 2026-07-25 through 2026-07-26 — false DASM failure during PROXI alignment

**Confirmed participant-address/label defect.** Five complete AlfaOBD PROXI
result blocks rendered `Driver Assist System Module (DASM)... Failure
connecting to the module`. The raw Body-computer recordings never addressed
the installed DASM at `0x2A`. Instead, AlfaOBD made 84 unanswered `10 03`
attempts to `0x26`, which the same APK's exact-model catalog assigns to
optional PAM2/Parking Assist. Direct `0x2A` connection minutes later returned
positive session, identity, DTC, and tester-present responses, while the BCM
continued to report DASM present, active, and `Response OK`.

Treat the rendered failure as a very-high-confidence AlfaOBD
participant-address/label binding bug, not a radar fault. It remains ambiguous
whether the implementation intended a DASM step and used PAM2's address or
correctly tested PAM2 and rendered the wrong label. Do not infer that the
generic 250-byte `2E 2023` payload should be sent to `0x2A`; the capture never
demonstrated the radar's configuration-write contract.

The same raw trace positively acknowledged full `2E 2023` configuration writes
at 15 other installed endpoints across C-CAN, B-CAN, and CAN-CH, making an
offline standalone-alignment model feasible while leaving any live replay
behind the normal configuration-write safety gates.

Source: [PROXI DASM misroute analysis](../projects/vehicle_configuration/findings/2026-07-26_alfaobd_proxi_dasm_misroute.md).

### 2026-07-27 — PCM Plots drive: unsupported legacy rows and an unclosed-log date trap

**Confirmed profile-definition defects.** During a current-vehicle loaded-drive recording with
the installed PCM identified at `18DA10F1 -> 18DAF110`, the selected
`Chrysler Pentastar/Hemi engine Model Year 2021` Plots profile rendered columns named
`Transmission Oil Temperature` and `Turbine speed`. The raw Debug cycle shows that these rows sent
legacy requests `21 18` and `21 62`; the PCM returned `7F 21 31`
(`requestOutOfRange`) to both in every observed drive cycle. Both Gauge columns were consequently
blank for all 1,323 rows. These labels are not usable PCM measurements on this van. Transmission
temperature and turbine speed must be sought from the installed ZF 948TE TCM rather than inferred
from the blank PCM display.

**Artifact/recording limitation — selected rows need not be persisted.** The Plots selector showed
12 selected gauges before the drive, including Output Speed, but the completed
`Gauges_Data.csv` section contained only 11 data columns and no Output Speed column. A selected
checkmark therefore does not prove that AlfaOBD will poll or persist that row. Verify the final CSV
header and raw request loop rather than reconstructing the active set from the selector.

**Artifact/recording limitation — an unclosed Debug archive can retain the preceding date.**
The Gauge section was explicitly dated 2026-07-27, but the cumulative Debug recording was not
cleanly closed and its drive exchanges inherited the preceding full recording date,
2026-07-26. Clock times and exact polling order still aligned all 1,323 Gauge rows with a 1 ms
median absolute boundary offset, but only after an explicit 2026-07-26 Debug-date override. The
join report records both dates and `date_overrides_gauge_section: true`. Never silently force the
Debug date to the Gauge date: retain the mismatch in provenance, use an explicit override only
after bounded raw-order/timing verification, and prefer a clean Plots stop plus Debug close when
possible.

**What worked.** The same synchronized Gauge/Debug method exactly or near-exactly associated nine
changing rendered values with their raw PCM reads, including oil pressure, coolant temperature,
engine speed, loaded positive and negative engine torque, VVT oil temperature, throttle blade,
generator duty, vehicle speed, and battery voltage. This confirms that multi-gauge Plots recording
is a high-yield mapping method when every label is checked against the raw diagnostic trace and an
independent listen-only CAN capture; it does not relax the current-vehicle and scaling validation
rules.

Source: [PCM Plots loaded-drive mapping](../projects/ecu_mapping/findings/promaster_2022/2026-07-27_pcm_plots_loaded_drive_mapping.md).

### 2026-07-27 — ZF9HP static request and decoder recovery

**What worked.** Ordered-table consumer tracing first rejected a coincidental
56-row HVAC table, then tied the real `ZF9HP` branch to `aa.A`, the
`ADDescZF9HP` runtime descriptor, and the 56-row Device 194 parameter catalog.
Tracing the same runtime table into the Plots consumer recovered the exact
profile-specific `n0.z1.r2()` response arithmetic. This yields distinct DIDs,
payload widths, byte order, signedness, offsets, and scales for all 56 outputs.
The owner-priority results include gearbox-oil DID `04FE` as `u8 - 40 °C` and
actual-crankshaft-torque DID `1018` as `u16be - 500 Nm`.

**Applicability boundary.** This is stronger than a database label alone
because request, label/unit order, runtime branch, and response code agree
inside the same APK. It is still vendor-derived static evidence: it does not
prove that the installed TCM supports a row or that the rendered definition is
correct for its exact software. Require a positive physical read and
controlled physical plausibility before dashboard publication.

Source: [AlfaOBD APK catalog and ZF9HP decoder recovery](../projects/ecu_mapping/findings/promaster_2022/2026-07-21_alfaobd_apk_catalog.md#static-zf9hp-decoder-recovery--2026-07-27).

## Current trust model

| AlfaOBD surface | What it can establish | What it cannot establish alone |
|---|---|---|
| Raw Debug request/response | Exact observed address, payload, response, timing, and adapter setup after ISO-TP reassembly | Correct label, scale, installed subtype, or applicability outside that vehicle/session |
| Live `F190` / `F1A5` and identity DIDs | Vehicle/source scope and strong exact-subtype evidence | That every selected-profile definition matches that subtype |
| Selected menu/runtime name | A useful route and definition-family candidate | Installed manufacturer, exact ECU subtype, or exact vehicle model year |
| Status/Plots rendered values | Candidate vocabulary and an efficient controlled-correlation surface | Ground truth when raw bytes, known state, or exact subtype contradict it |
| Status `Monitor parameters` / `*_Info.log` | Repeated labeled values and field groups when the Info file grows; logical label-run order in a bounded campaign envelope | Per-cycle timestamps, DID numbers, or logical record boundaries at sampled file sizes |
| Gauges Data CSV | Rendered labels and sample times when it actually grows | DID numbers; a label-to-DID join without synchronized Debug Data |
| `Data/*.dat` | Opaque cache-series change detection | Freshness, timestamps, DID identity, or label identity |
| APK model/catalog/decoder code | High-yield address, profile, field-layout, routine, and exact vendor-arithmetic candidates | Installed equipment, live DID support, or physical correctness for this ECU/software |
| Diagnostic menu | That an action is offered in a profile | Which raw `2F`/`31`/`2E` payload implements it |
| `NO DATA` / timeout | That the exact attempt got no response | ECU absence or a bad profile without controlled state/session/routing checks |

## Rules derived from this history

1. Scope every capture with live identity. Run the VIN scan first, preserve `F1A5`, and keep DID
   namespaces per ECU.
2. Record selected profile, connected runtime title, physical bus, adapter route, ignition/engine
   state, app version, and exact experimental action separately. None substitutes for the others.
3. Reassemble manual ISO-TP before interpreting service bytes. Preserve negative responses and
   response-pending/final-response sequences rather than collapsing them into timeouts.
4. Raw bytes and independently controlled physical state take precedence over rendered text.
5. Require an exact subtype or controlled proof before promoting a label, enum, scale, routine, or
   action. A positive DID response proves only that the DID exists in that ECU/state.
6. Never infer an actuation payload from menu order, neighboring labels, or timing alone. Capture one
   authorized action at a time and independently verify its effect before considering replay.
7. Baseline every AlfaOBD output artifact before a campaign. Track Debug, Gauges, and profile Info
   files independently; confirm which grew, but treat sampled file sizes as flush/liveness witnesses
   rather than record timestamps. Verify that `.dat` content is newly appended rather than
   unchanged, truncated, or mechanically repeated. Prefer a clean Plots stop and Debug close; if
   an unclosed archive inherits an earlier date, retain both source dates and require an explicit,
   provenance-recorded override after independently verifying the bounded trace.
8. Treat a timeout as conditional evidence. Recheck power, routing, bitrate, session, addressing, and
   framing before calling a module absent or a profile incompatible.

## What AlfaOBD remains good at

AlfaOBD is still one of the project's highest-yield candidate sources. Its raw traces supplied
verified diagnostic endpoints, exact session/framing recipes, hundreds of positive DID candidates,
routine and IO-control payload leads, and enough timestamped polling structure to replace broad blind
sweeps with bounded tests. Its value is greatest when used as a traffic generator and vocabulary
source while PCAN/raw Debug evidence and controlled ground truth validate the interpretation.

When a new issue is found, add it here with the date, exact ECU identity/profile, raw evidence,
independent comparison, classification above, operational consequence, and link to the detailed
project finding. Keep the raw/private capture under `tmp/` and mask the unique VIN serial in tracked
material.
