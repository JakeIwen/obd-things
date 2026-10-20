# Fuel economy: source investigation before dashboard integration

## Result and scope

> **Update 2026-10-14:** a broadcast fuel-rate source was found: C-CAN `0x2ED`,
> nominal `raw/512` L/h, checked against the owner's cluster instant-mpg video.
> See [the October 14 finding](2026-10-14_fuel_rate_cluster_video.md). The
> sections below record the earlier diagnostic search and remain accurate for it.

The owner requested fuel economy and trends in the telemetry dashboard. The
offline pass did **not establish a usable gasoline fuel-consumption signal
for this van**. The owner subsequently completed two bounded parked injector
support checks: all six DIDs returned exact four-byte positive replies and
both runs restored passive state. No fuel metric or dashboard value is
enabled. This is a source-mapping prerequisite, not evidence that the
vehicle lacks the capability.

The [2022 owner's manual](https://vehicleinfo.mopar.com/assets/publications/en-us/Ram/2022/ProMaster/104359_22_VF_OM_EN_USC_DIGITAL_E3_V1.pdf),
printed page 48, describes instantaneous and average consumption for the trip
computer. It does not specify diagnostic requests or CAN fields.

The existing telemetry speed/trip/history plumbing can support the requested
views after a source is verified. Trip MPG must be distance divided by fuel
used, not an arithmetic average of instantaneous MPG. Gaps, resets, idle
consumption and any display saturation need explicit treatment before totals
or historical backfill are presented as reliable.

## Catalog and installed-variant checks

The owner's AlfaOBD 2.4.4.0 resources were read offline. Plain response labels
use the `J1` bank after the unique `######` separator. Parenthesized enum
pairs refer to the separate `K1` bank and remain unresolved in the report.
The oil-life label anchors agree with the September 22 investigation.

`projects/ecu_mapping/fuel_catalog.py`, run with the `fuel-catalog-leads`
compute task, preserves raw database fields, label references and source
hashes. Its output is explicitly a candidate inventory, not installed-ECU
support. It rejects output paths that alias an input and incompatible catalog
schemas, so a successful empty result means a valid scan with no matches.
The scan found 163 matching fields: 27 petrol and 136 engine rows;
none in the scanned IPC or BCM tables. The APK has additional hard-coded
decoders, so absence from those tables is not a complete support test.

| Lead | Result for this task |
|---|---|
| PCM Plots catalog, `TIGERSHARK_CUSW` | The established 193-row catalog has measured air flow `01CA`, accumulated air mass `0106`, injector pulse widths and fuel-level fields, but no gasoline consumption row identified. `0DA5` is explicitly CNG fuel volume; it is not a gasoline source. |
| Petrol status DID `1942` | One row labels u16 fuel consumption at `1/512 L/h`; other variants have different layouts/scales. No installed-PCM subtype match was established, and the known current PCM profile IDs are not in these rows. Do not add it to polling from this label alone. |
| Instant/average economy labels `J1[11059/11060]` | APK references lead to `IPC_PN`, old non-PowerNet `CCN`, or `TGW_PN` profiles, not the installed `MARELLI_DASH_EP` subtype `8801`. |
| Total/trip fuel labels `J1[16645/16674]` | The inspected decoder branch selects `7372`, a `DIESEL_CUMMINS` profile. It does not apply to this gasoline van. |
| Current cluster decoder | `oa.X` dispatches `MARELLI_DASH_EP` to `qa.b0` except subtype `10407`; the installed `8801` is explicitly handled within `qa.b0`. Inspection found no direct instantaneous/average fuel-economy label or consumption formula there. This does not rule out a broadcast source or an unexposed diagnostic record. |
| PCM fuel level `0227` | Its existing scale ambiguity remains. Tank percentage is not an established consumption measurement. |
| Current positive PCM `0137` | The follow-up CDA join labels this as a 32-bit time-from-start counter at ×0.01 seconds. This is a historical runtime candidate, not a fuel lead; see below. |

The installed cluster identity/subtype comes from the existing July 21
inventory, not from a new read. Generic `PENTASTAR` model-menu membership and
the Plots catalog's device IDs are separate namespaces; neither selects a
current internal-PCM status decoder by itself.

## Reproducible evidence

Private sources remain under `tmp/ecu_mapping/android_tablet/`; no APK,
database, decompiled source, full catalog, or unique VIN is promoted here.

| Source | SHA-256 |
|---|---|
| `alfaobd.db` | `073fd4c46c438d4591e590d9fc2556bc5da3c1aff2e8008c504a9ef1f0398be5` |
| `alfaobd5_en.txt` | `01d622104f9528b5afefa85fc8def400a5d568c873686d838eabbabf2be7dac2` |
| APK `base.apk` | `97b0f100280453b134ceffc09025f2c443adb383ad4382afcd0b0fd7a9a853b9` |

Selected compute jobs (all completed successfully):

- `20261012T214331Z-daa0ac5b`: candidate inventory plus label-bank search.
- `20261012T213650Z-6fff2931`: Plots labels joined to profile names.
- `20261012T214436Z-614a74ff`: DEX references to instant-economy label `11059`.
- `20261012T215214Z-fb478d4e`: CCN/Cummins subtype joins.
- `20261012T215241Z-21c24f27`: telematics subtype `8516`, `TGW_PN`.
- `20261012T215016Z-a78299f7`: response dispatcher `oa`.
- `20261012T215302Z-05f81db9`: current cluster decoder `qa`.
- `20261012T214330Z-4f9c0200`: three fuel-catalog tests passed.

Working reports and decompiler results are below
`tmp/vehicle_data/fuel-economy/`. `qa.b0` occupies lines 35824–40580 in this
specific simple-mode decompilation. Other inspected handlers were `bb.b/d`,
`xa.o`, `za.s`, and `va.m`; their profile gates are essential to interpreting
the attractive fuel labels.

## Injector-estimate follow-up

The owner subsequently proposed using injector flow/pulse widths and requested
parallel research. This is a viable **estimation route**, subject to support,
semantics and calibration checks; a dash video is not the only possible
reference. No drive is currently planned.

The [official Mopar catalog for the 2022 ProMaster 2500 3.6L](https://store.mopar.com/v-2022-ram-promaster-2500--base--3-6l-v6-flex/fuel-and-accelerator-pedal--fuel-rail-and-injectors)
lists injector `5281427AA`. The bounded research did not recover an
authoritative static flow rating, reference differential pressure, voltage
offset table or short-pulse curve for this part. Older Pentastar or aftermarket
injector data must not be substituted as its calibration.

The OEM P0201–P0206 procedures describe individual cylinder pulse widths
computed by the PCM. The P0461 procedure refers to accumulated injected fuel.
This supports pursuing an internal fuel-total model, but does not identify an
exposed DID or CAN field. Saved current-vehicle Alfa Info also reports no
direct injection; this is consistent with port injection.

| Input | Evidence and limit |
|---|---|
| `0234–0239`, cylinders 1–6 pulse width | Alfa Plots labels plus 2011 CDA candidate encoding: unsigned 32-bit big-endian after the response echo, raw microseconds. Two owner-run checks on October 13 UTC confirmed current support and four-byte response shape, all zero with engine off. Running semantics and physical scaling remain unverified. |
| `01D5`, engine RPM | Independently verified current raw u16 RPM; production also has passive RPM. |
| `0174`, manifold pressure | Catalog/legacy u16be × 0.2 kPa candidate; no current support established in the checked inventory. |
| `0B9C` / `0BA0`, actual/desired rail pressure | Catalog labels; no current support or exact conversion established. |
| `0232`, fuel-control status | Current positive `62 02 32 00`, 183 reads in July 25 map. The old label does not establish a deceleration fuel-cut bit; do not use it as one. |

The current evidence is the PCM section of
`tmp/ecu_mapping/canch_20260725/all_module_did_map.txt` and its
`alfaobd_post_reads.decoded.txt` source. The 16-request
`current-target-joins.json` report is a limited manual-read subset, **not** the
187-request current-positive union. An absence from that small report cannot
establish absence from the current inventory. Historical injector conversions
are in `tmp/fca_pcm_survey/PCM-PCM-60-00-005/tigershark-catalog-joins.json`.

For one injection event per cylinder per four-stroke cycle, the model is
injection events per second (`RPM / 120`) multiplied by the sum of each
cylinder's fuel volume per event. Volume per event depends on pulse width,
injector flow, voltage and rail-minus-manifold pressure. Confirm whether the
reported pulse width includes opening-delay compensation or multiple pulses
before using this model. The manufacturer's
[pulse-width characterization discussion](https://injectordynamics.com/articles/low-pulse-tech/)
explains why a static flow rating alone does not describe short pulses.

If exact calibration remains unavailable, fit an effective model to complete
reference trips or fill-up intervals and validate on different intervals.
Fill-up totals constrain integrated fuel use; they do not independently prove
instantaneous accuracy. A synchronized dash reference can check that response.
Label the result estimated, report observed error, and test idle, cruise,
acceleration and overrun. Check fuel cut explicitly. Six sequential reads
also need acquisition-skew and gap handling before integration; the existing
one-second active-drive cycle does not make them simultaneous.

OEM corpus jobs: `20261013T025919Z-fe5bbdc8` (injector operation),
`20261013T025718Z-4d2d8afd` (fuel correction), and
`20261013T030030Z-d0e600ff` (accumulated injected fuel). Selected outputs are
retained under `tmp/vehicle_data/fuel-economy/`. No production estimator or
new live read was added by this follow-up.

## Direct-source follow-up without owner experiments

The owner requested minimal hands-on effort: no required videos, special
drives or manual fill-up logging. The next offline pass used the complete
July 25 positive PCM request list, rather than the earlier 16-request subset.
The selected 175 requests contain the canonical 173 read-positive base
(167 service `22`, six `1A`) plus two service `21` records (`21E5`, `21EA`).
The 14 later reads in the July 30 finding's 187-request union were not part of
this new join. No direct fuel-rate, fuel-used or injection-quantity label was
found among the records matched to the 2011 PCM profile. Unmatched records
remain unresolved; this is not proof that the PCM exposes no fuel counter.

`22 0137` maps to historical **Time From Start Run**: a 32-bit field at bit 24,
mask `FFFFFFFF`, conversion 529, slope 0.01 and unit seconds. The current July
reply is zero; the September 24 reply `62 01 37 00 01 3A F5` would be 806.29 s.
This is sufficient to drop it from fuel-counter shortlisting, but does not
promote a physically verified current runtime decode. The old conversion's
upper bound is `2147483647`; do not infer unrestricted current signedness.

`0231` is a historical Current Fuel Shutoff group with a DFSO flag, but its
current reply has four data bytes while the old definition occupies two.
The known width conflict prevents transferring the old bit positions.
`0232` remains the separate fuel-control status group. Neither is a verified
deceleration fuel-cut decoder. `055C` is a tank-capacity candidate, not use.

Standard Mode 01 fuel-rate PID 5E is not a low-effort alternative on the current
connection: the root README records no responses through the internal-bus
SGW-bypass taps. Do not ask the owner to move cables or change hardware to
revisit that path for this feature.

Reproduce with the offline `fuel-fca-hsql-query` compute task, which reuses
`tools/fca_hsql_decode.py` and four read-only inputs. Jobs
`20261013T034415Z-02e59694`, `20261013T034529Z-d17261d1` and
`20261013T034541Z-4590a11c` split the 175 requests into 80/80/15.
Selected results are `pcm-positive-join-{1,2,3}.json` under the fuel working
directory. The current map SHA-256 is
`af2aeba079fe9093160133e0214ace088f78c4a060c429852f5f50742b4f6001`;
the historical data-file SHA-256 is
`fdda8bc5984ddf6c4e6a264d746f036981f2782dc5259f6b0b8410f7deba88e0`.
The report metadata retains the remaining source hashes and joins.

## Prepared parked injector support check

`projects.vehicle_data.pcm_temperature_support --profile injector-pulse-width`
prints an inert plan by default. Its pure fixed profiles and decoders are in
`pcm_support_profiles.py`; the existing CLI retains all ownership effects.
With explicit execution and parked/ignition-on/engine-off confirmation, it can
send only these six padded physical C-CAN requests to `18DA10F1`:

```text
03 22 02 34 00 00 00 00
03 22 02 35 00 00 00 00
03 22 02 36 00 00 00 00
03 22 02 37 00 00 00 00
03 22 02 38 00 00 00 00
03 22 02 39 00 00 00 00
```

There is at least one second between requests, a 0.75 s response timeout per
request, and no retry, session change, TesterPresent, wake, or FlowControl.
Cooperative handoff, serial-resolved role/channel ownership, fresh vehicle
witnesses before arming and every send, topology/inhibit checks and verified
passive restoration are preserved. New replies must be exact PCM single
frames with the matching echo and four data bytes. They remain raw candidate
values: the check does not claim microseconds or publish telemetry.

The JSON report records raw received SocketCAN bytes before decoding, so an
unexpected shape can be investigated without immediately repeating the owner
check. Malformed replies stop the sequence and still take the restoration
path. Reports live under `tmp/inventories/pcm/injector-pulse-width-support-*`.
Existing temperature/oil-life plans and successful decoded values remain
unchanged; raw receive-frame evidence is an additional report field.

Focused preparation job `20261013T035011Z-b0d71276` passed 11 tests and nine
subtests, including no-I/O dry runs, exact requests/replies, gate loss after
two sends, and cleanup after a malformed reply. Final job
`20261013T035334Z-640ad58f` also passed 11 tests and nine subtests, including
the raw-reply preservation assertion. A broader worker run had 32 environment failures (packaged checkout
git-ignore and API Unix-socket path/readiness tests); the focused support
suite passed. The following owner-run checks supersede the original untested
live status.

At 03:46 UTC on October 13, the broker reported inferred sleep and an idle
active-drive helper; recorder and broker were running, TPMS fallback inactive.
That is a temporal observation, not proof of physical parking or future
conditions. The agent shell has no effective/bounding capabilities and
NoNewPrivs=1 at preparation time, so the initial arm/restore path needed the
owner's normal SSH shell. Standing authorization in docs/agent-context.md already
covers reviewed physical 22 reads; a separate conversational permission for
each reading is not needed.

## Owner-run support result — October 13 UTC

The owner ran the prepared script twice while parked, ignition on and engine
off. Both reports have `error: null`, six positive replies, and
`restored_passive: true`. The first run completed at 04:22:36.210890Z and the
second at 04:22:56.889914Z. Each reply has the exact requested DID echo followed
by four zero data bytes:

| DID | Positive response, excluding SingleFrame PCI |
|---|---|
| `0234` | `62 02 34 00 00 00 00` |
| `0235` | `62 02 35 00 00 00 00` |
| `0236` | `62 02 36 00 00 00 00` |
| `0237` | `62 02 37 00 00 00 00` |
| `0238` | `62 02 38 00 00 00 00` |
| `0239` | `62 02 39 00 00 00 00` |

This establishes current support and response width for the six candidate
fields without a session change. It does not establish which diagnostic
session was inherited, prove that the fields populate when running, or
validate the historical microsecond scale. Zero is consistent with the
engine-off condition but is not enough to distinguish a working measurement
from a zero-filled compatibility field. Do not request another engine-off
check or publish these zeros as live fuel telemetry.

Reports under `tmp/inventories/pcm/`:

- `injector-pulse-width-support-20261013T042227823099Z.json`, SHA-256
  `5d7f354cbc58ea2ff248317d681f4d3ef48377a254630ac701d71a32fbeab27b`.
- `injector-pulse-width-support-20261013T042249425208Z.json`, SHA-256
  `3c24b23734fb58614e7c24afee9156d0ac00ad9843efba51106af4d17f72e74c`.

At 04:25UTC, the broker's serial-resolved role status and a read-only interface
inspection confirmed all three connected roles in listen-only, classical CAN,
ERROR-ACTIVE, restart-ms 0 at their expected rates. C-CAN had no active inhibit
or restoration failure. Broker and recorder were running; TPMS fallback was
inactive. These are post-check observations, not a promise about later state.

The owner also restarted the agent with broader permissions. Current
NoNewPrivs=0 and a successful `sudo -n true` confirm the prior shell privilege
block no longer applies. This removes a need for owner-run commands when an
already-reviewed action and its fresh vehicle gates otherwise permit agent
execution; it does not waive those gates.

## Validation paths

The parked support check is complete. The next evidence needed is variation
during ordinary engine-running use, collected without another owner test
session. No automatic injector collection is enabled yet. A bounded design
has been reviewed conceptually: reuse the existing active-drive PCM owner and
socket, issue one fixed injector read per cycle for at most five complete
six-cylinder sweeps, with a fresh gate and DID-specific one-use permit for
each send. Keep raw evidence in gitignored files, outside the telemetry
registry. Implementation, offline regression and failure-path review are
required before enabling it; do not add a competing active owner.

An identified PCM fuel-rate/counter source would avoid
reconstructing injector behavior; calibrated pulse-width estimation remains
an alternative for the requested non-critical economy/trend display.

Prioritize ordinary automatically recorded driving for subsequent validation.
If the owner later volunteers a factory-display reference, synchronize it
with the raw recording and check units, display update rate and saturation.
Do not ask the driver to record values or handle a phone while moving.
Shortlist at most two identifiers, freeze any proposed formula, and test it
on a separate complete drive before promoting it. A video, special drive or
manual fill-up log is not a prerequisite imposed on the owner.

Check recording readiness before the reference drive. The recorder service
was running and waiting for a broker drive interval. The agent's restricted
mount view reports `ro`, while `/proc/self/mountinfo` identifies the underlying
EXFAT superblock as `rw`; the root filesystem has the same restriction.
This does **not** establish a read-only-disk fault or justify a repair.
The host recorder PID is outside the agent's PID namespace, so its mount view
was not inspected. No mount, storage, service or CAN configuration changed.

If no broadcast candidate survives, continue ECU-scoped source research or
design an explicitly authorized, bounded support read for a well-founded
candidate. Air-flow or injector-based estimates need their own assumptions
and validation; this pass does not substitute them for measured fuel use.
