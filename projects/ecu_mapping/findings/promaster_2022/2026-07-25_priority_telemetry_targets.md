# C-CAN priority telemetry targets and operating context — 2026-07-25

## Outcome

The owner-priority engine signals are:

1. engine oil pressure, interpreted against engine speed, temperature, and
   dual-stage pump state;
2. engine coolant and engine-oil temperatures;
3. actual crankshaft torque and RPM, with power derived from those two inputs;
4. transmission-oil temperature as a first-tier owner priority, followed by
   other transmission and electrical measurements that the stock IPC hides or
   reduces to a warning lamp.

Update 2026-07-26: the
[simultaneous PCM Plots/wire campaign](2026-07-26_pcm_plots_idle_mapping.md)
mapped oil-pressure DID `022A` and coolant DID `011D`, then found strong
passive `0x41D` and `0x2ED` forms. Those two receive-only sources are now
qualified as `observed_alfa_scale` dashboard metrics. Engine-oil temperature,
transmission-oil temperature, loaded torque, and power remain unresolved; the
torque campaign produced a strong idle-range `0x100` lead whose wrap/mode needs
driving excitation.

Update 2026-07-27: the
[loaded-drive PCM campaign](2026-07-27_pcm_plots_loaded_drive_mapping.md)
established exact diagnostic engine speed (`01D5`, raw rpm), signed loaded
torque (`06DA`, i16be × 0.04 Nm), and VVT oil temperature (`069F`, raw - 64 °C).
It independently qualified passive `0x0FC` bytes 0–1, low two bits masked,
`/4 rpm` for the dashboard.
The VVT temperature is not a substitute for sump-oil temperature, and the
selected PCM profile's transmission-temperature/turbine-speed legacy requests
were rejected by the PCM. Passive torque, true engine-oil temperature,
transmission temperature, and derived power remain unresolved.

The acquisition path is nevertheless strong. The exact-vehicle OEM corpus
confirms real oil-pressure and oil-temperature sensors monitored by the PCM,
not merely an oil-pressure switch. The PCM is already live-verified on C-CAN at
`18DA10F1 -> 18DAF110`, including its fixed-DLC-8 padded legacy `10 92`
session behavior. The TCM at `18DA18F1 -> 18DAF118` has an engine-torque and
transmission-thermal candidate vocabulary in its selected Alfa profile. The
next high-value campaign is now narrowed to the TCM: inventory its 56-row live
Plots selector, then correlate a small owner-priority gauge set. It is not a
broad blind DID sweep.

Update 2026-07-27 (static APK request-table recovery): five independently
verified PCM anchors proved that AlfaOBD's ordered literal two-byte tables can
align with the database's ordered Plots catalogs, but table length alone is
not sufficient. DEX consumer tracing rejected a coincidental 56-row HVAC table
and located the actual ZF9HP runtime table. It identifies `F40C` for engine
speed, `0500` for converter slip, `2102` for turbine speed, `2103` for output
speed, `F405/0301/04FE` for water/TCU/gearbox-oil temperatures, and
`1018/101A/101B/101D/101F/1020` for the six torque quantities. These are
high-confidence vendor-derived DID candidates, not yet live-verified TCM
decodes. This reduces the next vehicle pass to bounded support and scale
checks for individual known requests; it does not justify publishing
transmission temperature or torque yet.

Update 2026-07-27 (static ZF9HP decoder recovery): the profile-specific
AlfaOBD response routine is now recovered as well. It defines `F405`, `0301`,
and `04FE` as one-byte `raw - 40 °C`; `F40C`, `2102`, and `2103` as
big-endian `raw × 0.25 rpm`; `0500` as signed big-endian rpm; five priority
torque DIDs as `u16be - 500 Nm`; and `101D` as
`u16be × 0.25 - 500 Nm`. The complete 56-output executable catalog is
[`projects/ecu_mapping/zf9hp.py`](../../zf9hp.py). These are exact
vendor-derived formulas, but the installed TCM still must positively answer
the requests and produce physically plausible values before the dashboard
allowlists transmission temperature or torque.

Update 2026-07-27 (bounded true-EOT lead): the selected
`TIGERSHARK_CUSW` PCM profile contains no true engine-sump oil-temperature
row; its `069F` definition is specifically VVT oil temperature. AlfaOBD's
related `SIEMENS_GPEC` PCM profile does pair its **Engine oil temperature**
row with DID `3159`, and the matching decoder is `u8 - 40 °C`. The adjacent
sensor-voltage cross-check is `315A`, decoded `u16be × 0.004888 V`. These are
vendor-derived candidates from a different profile, not installed-vehicle
support evidence. The next ignition-on check is therefore only the sparse
`3159/315A` pair through the PCM's verified zero-padded `10 92` session.

Update 2026-07-29 (EOT support result and replacement lead): that exact
`3159/315A` support check had already run on 2026-07-27. The PCM confirmed
session `92`, then returned NRC `12` for both DIDs, rejecting the pair for this
installed ECU. Continued DEX-backed catalog mining found a more relevant
`SOHC_V6` thermal cluster: calculated transmission oil `B010`, measured oil
thermistor `B011`, and calculated oil `B012`. All three use Alfa's
`((s16be × 0.015625) - 32) / 1.8 °C` decoder. They are related-profile
candidates only; the next engine-oil step is one sparse parked support check
of `B010/B011/B012`, with `B011` the physical-sensor priority. Do not repeat
`3159/315A`.

Update 2026-07-29 (SOHC_V6 support result): the installed PCM confirmed
session `92` but returned NRC `12` for `B010`, `B011`, and `B012`. A
simultaneous full-bus capture independently retained every padded request and
response, so the negative is stronger than the earlier high-level-only
`3159/315A` evidence. All five cross-profile thermal DIDs are now rejected for
this ECU. True engine-oil temperature remains open; further work must locate
an installed-calibration or separately standardized source rather than
expanding around these unsupported related-profile addresses.

## Engine-oil pressure: exact OEM context

The OEM `OIL PRESSURE – UPGRADE ENGINE` table applies only when coolant is
between 89 and 100 °C:

| operating point | OEM expected pressure |
|---|---:|
| curb idle, approximately 650 RPM | 103.4–234.4 kPa / **15–34 psi** |
| 1,000–3,000 RPM | 193–241.3 kPa / **28–35 psi** |
| above 3,500 RPM | 448.2–551.6 kPa / **65–80 psi** |

The same table says that zero pressure at idle means **do not continue running
the engine**. The P06DD factory diagnostic description adds:

- approximately **12 psi is the minimum while the engine is operating**;
  pressure below that could damage critical moving parts;
- the mechanical relief valve limits maximum pressure to approximately
  **145 psi**;
- the scan tool can monitor the Engine Oil Pressure sensor directly.

These values do not support one fixed green/yellow/red gauge. This engine has a
PCM-controlled dual-stage oil pump. Energizing its solenoid selects the usual
low-pressure mode; de-energizing it selects high-pressure mode. Engine load can
request high mode before a simple RPM boundary, and a mode transition can
produce an abrupt pressure step. The future evaluator must use fresh oil
pressure together with at least RPM, coolant/oil temperature, and desired pump
state. The 3,000–3,500 RPM transition region must not be filled in with an
invented linear threshold.

No alert rule should be enabled until the DID, units, scale, live-state gate,
and staleness behavior have all been verified. Once they are, the OEM
below-approximately-12 psi statement and zero-at-idle caution justify a
prominent critical condition; the warm expected bands are better shown as
operating context than as universal limits. The critical evaluator must also
require separately verified **engine-running** state and a defined
startup/cranking grace period. Verified ignition-on alone is not an
engine-running safety gate.

OEM provenance in the local exact-vehicle corpus:

- `~/dev/ram_2022_GAS/vehicle/engine,_cooling_and_exhaust/engine/specifications/pressure,_vacuum_and_temperature/engine_-_specifications.html`,
  `OIL PRESSURE – UPGRADE ENGINE`, lines 72–109;
- `~/dev/ram_2022_GAS/vehicle/all_diagnostic_trouble_codes_(_dtc_)/testing_and_inspection/p_code_charts/p06dd/powertrain_control_module_(pcm)_-_engine_oil_pressure_control_circuit_stuck_off.html`,
  theory/specification material beginning at lines 25–36.

The current-vehicle Alfa Info artifact independently reports `Oil Pressure
ABS: Yes`, `Dual Stage Oil Pump Equipped: Yes`, `Oil Pressure Sensor:
Enabled`, and the old oil-pressure-switch input as not enabled. It also
contains observable desired-state rows for the dual-stage pump and cooling-fan
relays. Source:
`tmp/ecu_mapping/raw/TIGERSHARK_CUSW_Info.log`, SHA-256
`86554c7fedc31044d344d9f70b55f1bce9a6cd6ebde6e81673fe0a56ff34a7f7`.
The current-vehicle identity block begins near line 620; the configuration
claims are at lines 748, 794, 804, and 812, and the observed fan/pump desired
states are at lines 1153–1154 and 1162. The preceding claims and line
provenance are preserved here because the raw file itself is intentionally
gitignored.
That raw artifact is a navigation aid, not a DID/scale proof; Alfa profile
renderings remain subject to the incompatibilities recorded in the evidence
history.

## Temperature context

The OEM thermostat description says it guarantees a minimum engine operating
temperature of approximately **88–93 °C** (about **190–199 °F** by direct
conversion; the OEM text prints 192–199 °F) and becomes approximately fully
open near **104 °C / 220 °F**. Above that point, coolant temperature is
governed by radiator, fan, load, ambient temperature, and vehicle speed rather
than thermostat restriction. Thus 104 °C is a useful cooling-system context
boundary, but it is **not** by itself an OEM overheat or damage threshold.

No exact-vehicle general overtemperature warning/derate threshold was found in
this pass. The first dashboard should show the actual temperature and
thermostat/cooling context without inventing a critical limit. Mapping
low/high fan commands and a PCM overtemperature state is preferable to a
generic red line.

The OEM P0298 procedure confirms a physical two-wire Engine Oil Temperature
sensor whose PCM voltage is converted to temperature. Its diagnostic compares
actual EOT with a model: the monitor starts below a predicted 58 °C and stops
above a predicted 88 °C; a difference greater than 40 °C at that point is a
fault condition. The implied temperature is a model-comparison trigger, **not
a general safe oil-temperature limit**, and must not be presented as one.

OEM provenance:

- `~/dev/ram_2022_GAS/vehicle/engine,_cooling_and_exhaust/cooling_system/thermostat/description_and_operation/components/engine_coolant_thermostat_-_operation.html`,
  line 14;
- `~/dev/ram_2022_GAS/vehicle/all_diagnostic_trouble_codes_(_dtc_)/testing_and_inspection/p_code_charts/p0298/powertrain_control_module_(pcm)_-_engine_oil_temperature_too_high.html`,
  lines 38–50.

## Torque and derived power

The selected TCM/Alfa profile's diagnostic-event snapshot vocabulary includes
actual, target, and pre-intervention crankshaft torque, transmission torque
intervention, turbine speed, torque-converter slip/estimated temperature, and
gearbox-oil temperature. This makes PCM/TCM C-CAN correlation the right path.
It does not make the existing renderings trustworthy: that snapshot reported
impossible values such as `62988 Nm` for actual crankshaft torque. Source:
`tmp/ecu_mapping/raw/ZF9HP_Info.log`, especially lines 93 and 103–123. Its
SHA-256 is
`07f73ace06bd44da1d1ec01ed2e63c037114fa6bf13e78ed389a755f9bf9a9dd`;
the conflicting scale is recorded here so it is not silently reused.

After actual crankshaft torque and RPM are independently qualified, derived
power is:

```text
kW ≈ torque_Nm × RPM / 9549.3
hp_SAE ≈ torque_Nm × RPM / 7121
hp_SAE ≈ torque_lb_ft × RPM / 5252.1
```

The dashboard must call this **ECU-estimated crankshaft power** (or similarly
explicit wording), not measured wheel horsepower. The ECU torque itself is
normally model-derived, and driveline/accessory losses mean this is not a
chassis-dynamometer result. Torque and RPM must also be contemporaneous and
fresh; combining stale or skewed samples can create false power spikes even
when both individual scales are correct.

The public dashboard follows the owner's US-unit preference: publish qualified
torque in lb-ft (`lb_ft = Nm x 0.737562149`) and power in SAE horsepower.
Keep the canonical ECU decode in Nm in findings and raw-evidence tools.

## Transmission temperature: exact OEM context

The exact-vehicle 948TE/9HP48 service corpus confirms that this is a real sump
measurement, not a generic estimated-temperature label. The Transmission Oil
Temperature Sensor:

- is located in the transmission sump;
- is exposed to the factory scan tool as `Oil Temperature Sensor`;
- is a two-wire negative-temperature-coefficient thermistor supplied by the
  TCM; and
- is integral to the transmission internal wire harness.

The P0711 rationality monitor rejects a measured change greater than 10 °C in
less than one second. That is a useful future telemetry plausibility gate:
an isolated jump of that size should be marked suspect rather than rendered as
real fluid heating. The source's parenthetical Fahrenheit value for that
10 °C delta is internally inconsistent, so it is intentionally not reused.

The OEM procedures provide operating windows but do **not** reveal the
calibrated over-temperature threshold:

- the transmission verification test warms the fluid to 43 °C / 110 °F;
- the adaptation drive requires 50–110 °C / 122–230 °F; and
- DTC P176D sets only when measured oil temperature exceeds an undisclosed
  calibrated value for an undisclosed calibrated time.

Therefore 122–230 °F is a service-procedure/adaptation window, not a blanket
dashboard “safe range,” and 230 °F must not silently become a redline. Until an
authoritative threshold is found, the dashboard should show the measured value
and trend, identify the OEM adaptation window as context only, and separately
surface a real P176D/warning-lamp state if available.

OEM provenance:

- `~/dev/ram_2022_GAS/vehicle/all_diagnostic_trouble_codes_(_dtc_)/testing_and_inspection/p_code_charts/p0713/p0713-00/transmission_control_module_(tcm)_(948te/9hp48)_-_transmission_fluid_temperature_sensor_"a"_circuit_high.html`;
- `~/dev/ram_2022_GAS/vehicle/all_diagnostic_trouble_codes_(_dtc_)/testing_and_inspection/p_code_charts/p0711/p0711-00/transmission_control_module_(tcm)_(948te/9hp48)_-_transmission_fluid_temperature_sensor_"a"_circuit_range/performance.html`;
- `~/dev/ram_2022_GAS/vehicle/all_diagnostic_trouble_codes_(_dtc_)/testing_and_inspection/verification_tests/transmission_verification_test_-_948te_9hp48.html`;
- `~/dev/ram_2022_GAS/vehicle/powertrain_management/transmission_control_systems/relays_and_modules_-_transmission_and_drivetrain/relays_and_modules_-_a/t/control_module/testing_and_inspection/programming_and_relearning/948te/9hp48_tcm_adaptation.html`; and
- `~/dev/ram_2022_GAS/vehicle/all_diagnostic_trouble_codes_(_dtc_)/testing_and_inspection/p_code_charts/p176d/p176d-00/transmission_control_module_(tcm)_(948te/9hp48)_-_transmission_fluid_temperature_too_high.html`.

## Acquisition and verification order

AlfaOBD has two separate selector surfaces. Scalar values are under
**Plots → Select gauges to scan**; with Gauges Data recording enabled, the
campaign must witness `Gauges_Data.csv` growth. The existing guarded supervisor
operates **System status → Select parameters to monitor** and its profile Info
log. Extending only the Status selector would not reach the desired scalar
gauges.

The saved 2026-07-22 preferences had CSV recording enabled, but that campaign
used Status and therefore did not grow the Plots CSV. APK control flow confirms
that starting a Plots scan with `RecordScanData` enabled automatically starts
the CSV writer and enables the separate manual `bRecording` toggle. The scalar
supervisor should avoid an unnecessary recording-button tap: it must verify
the auto-recording visual state, prove per-segment CSV growth after each clean
stop, and prove post-stop size stability. APK class `i2` appends UTF-8
`Gauges_Data.csv` sections but does not flush per row; its explicit flush/close
happens on stop and has no `fsync`, so live growth may lag or arrive in buffered
chunks.

The offline `alfaobd_gauge_join.py` path now preserves this CSV's
comma/semicolon/tab dialect and keeps exact `22 DDDD → 62 DDDD` DIDs separate
from exact legacy `21 LL → 61 LL` local identifiers. Its canonical correlation
keys include the request service (`22:DDDD` or `21:LL`), so numerically similar
identifiers cannot be merged or mislabeled as DIDs. This prepares the offline
half of the Plots campaign; it does not supply any live PCM mapping by itself.

The offline `tools/can_timeseries_correlate.py` path can independently rank
passive C-CAN byte/u16/aligned-u32 fields against one exact cluster DID from the
integrated drive logger. It exact-joins every selected DID response back to the
global raw frame sequence and excludes diagnostic IDs by default. Every result
remains promotion-disabled candidate evidence: the idling trace can locate a
likely broadcast RPM field, but its shared warm-up/time trend cannot prove the
field's identity or scale. Sparse and low-variation matches are gated before
the `R² × coverage` ranking. The report does not independently revalidate the
logger's manifest, drop accounting, or campaign summary, so those completed
artifacts remain part of the evidence review. Driving excitation and
independent ground truth are still required.

The separate `tools/alfaobd_plots_scalar_campaign.py` and draft
`projects/ecu_mapping/configs/alfaobd_pcm_plots_scalars.json` prepare only the
offline safety gates for the eventual one-scalar runner. `plan` and `audit`
read and validate local plans/review evidence without ADB, CAN, subprocess,
service, mount, network, proxy, or output access; `status` only reads an
existing checkpoint:

```bash
python3 tools/alfaobd_plots_scalar_campaign.py plan \
  projects/ecu_mapping/configs/alfaobd_pcm_plots_scalars.json

python3 tools/alfaobd_plots_scalar_campaign.py audit \
  projects/ecu_mapping/configs/alfaobd_pcm_plots_scalars.json
```

Readiness requires the non-null live catalog hash, the exact reviewed catalog
report and its SHA-256, the sibling clean-completion `state.json` and its
SHA-256, the catalog-plan source SHA-256, the scalar-plan review SHA-256 and
review provenance, plus the exact one-based order key, zero-based index, and
live label for every scheduled scalar. Those fields are
deliberately unpinned now. More importantly, `run` remains intentionally
disabled even after all pins and confirmations pass: this scaffold performs no
live selector mutation or scan and is not evidence that live scalar automation
exists.

The selected generic `TIGERSHARK_CUSW` Device-190 Plots catalog supplies these
high-yield navigation candidates:

| UI order key | catalog label | catalog unit | qualification |
|---:|---|---|---|
| 7 | Engine speed | rpm | anchor candidate |
| 13 | Current engine torque | Nm | candidate; not yet a trustworthy scale |
| 15 | Coolant temperature | `|C` (UI typically renders °C) | candidate |
| 16 | Desired PWM Radiator Fan | % | candidate |
| 17 | Engine oil pressure | `KPa` | owner-priority candidate |
| 18 | Oil pressure sensor | V | useful cross-check candidate |
| 19 | VVT Oil Pressure | `KPa` | distinct unresolved candidate |
| 20 | VVT Oil Temperature | `|C` | **not** established as physical engine-oil temperature |
| 44 | Target Charging Voltage | V | electrical target candidate |
| 45 | Generator Duty Cycle | % | electrical target candidate |
| 47 | Battery voltage | V | correlation anchor candidate |
| 188 | Transmission Oil Temperature | `|C` | late catalog candidate |
| 191 | Turbine speed | rpm | late catalog candidate |
| 192 | Output Speed | rpm | late catalog candidate |

The order keys are UI navigation indices, not DIDs or parameter IDs. The
catalog has no proven physical EOT label: inventorying the full live Plots
dialog must precede any claim that key 20 represents the OEM-confirmed EOT
sensor.

1. The separate guarded catalog walker is now implemented as
   `tools/alfaobd_plots_catalog.py`, with the tracked discovery plan at
   `projects/ecu_mapping/configs/alfaobd_pcm_plots_catalog.json`. It never taps
   a gauge row, OK, or scan; it inventories exact live strings forward and
   backward with bounded overlapping swipes, requires two stable parsed states
   per swipe, verifies the stopped scan icon before and after, and cancels with
   BACK. Its offline and synthetic bidirectional traversal tests pass. This is
   implementation readiness, not live PCM catalog evidence.
2. Run the no-input audit and then inventory the complete live PCM Plots list
   before selecting anything. The SQLite prior predicts 193 rows from
   `Vehicle speed, km/h` through `Transfer speed, rpm`; a count, boundary,
   required-label, traversal, or exact-string mismatch must fail closed rather
   than being repaired by assumption. This reviewed live catalog is the
   current next live dependency. Review its complete `catalog.json` and
   clean-completion `state.json`, then pin the catalog, report, state,
   catalog-plan, scalar-plan, review-provenance, and exact target-triple
   fields. Pinning clears only offline blockers; it does not
   enable the intentionally disabled scalar `run` path.
3. The live catalog search found no true engine-oil-temperature label in the
   selected PCM profile. Do not substitute `VVT Oil Temperature`. Instead,
   make the sparse direct `3159/315A` related-profile support check documented
   above; a negative response ends that lead, while a positive response still
   requires plausibility and cold/warm validation before promotion.
4. Use the existing System-status singleton path separately for discrete
   `Dual Stage Oil Pump Desired State` and fan desired/relay groups recorded in
   the current Info artifact.
5. Inventory the live TCM selector with
   `projects/ecu_mapping/configs/alfaobd_tcm_plots_catalog.json`. The APK prior
   predicts 56 rows, and the current-vehicle UI already pins the exact
   connected profile text. High-value UI order keys are 6 engine speed, 7
   converter slip, 8 turbine speed, 9 gearbox output speed, 15 TCU chip
   temperature, 16 gearbox oil temperature, and 17–22 torque quantities.
   Current/target gear are not in this Plots catalog and remain separate
   System-status candidates. Retain the warning that a historical diagnostic
   event snapshot from this profile rendered impossible torque values; exact
   subtype selection alone does not validate scaling.
   The exact 14-target follow-up scope is tracked in
   `projects/ecu_mapping/configs/alfaobd_tcm_plots_scalars.json`, with gearbox
   oil temperature repeated as the outer anchor. It deliberately retains null
   catalog/review hashes until the live selector inventory is complete, and
   the scalar runner CLI remains offline-only. Its unreachable internal
   selection primitive now fail-closes on a catalog-hash mismatch, stale
   coordinates, a non-contiguous live page, or any collateral check-state
   change; it proves the committed Plots page contains only the intended target
   and that scanning is still stopped. The accompanying post-stop artifact
   validator requires Debug/CSV growth and a stable buffered CSV tail. The
   unreachable scan-segment primitive now adds a running activity oracle,
   bounded dwell, clean stop, and fail-closed cleanup after ambiguous start or
   stop returns. The campaign-wide lock/inhibit/mount/pull/checkpoint supervisor
   is still intentionally absent, so this implementation progress does not
   authorize or enable a live run.
   Once a simultaneous drive capture exists, van-compute tasks
   `candump-diagnostic-wire-tcm-four-chunks` and
   `can-timeseries-correlate-tcm-four-chunks` provide the same bounded
   extraction/correlation path already used for the PCM.
   The static request-table recovery changes the efficient order within this
   step: first physically read `F40C`, `0500`, `2102`, `2103`, `F405`, `0301`,
   `04FE`, and `1018/101A/101B/101D/101F/1020` to establish support and
   response lengths; then record the grouped priority gauges together while
   preserving exact Debug cycles and passive C-CAN. Each priority row now has
   a separate candidate DID and a recovered vendor formula, so singleton
   repeats are needed only where support, response shape, or physical
   plausibility remains ambiguous. Evidence and provenance are in
   [`2026-07-21_alfaobd_apk_catalog.md`](2026-07-21_alfaobd_apk_catalog.md#static-request-table-recovery--2026-07-27).
   The guarded sparse plan is:

   ```bash
   python3 tools/did_sweep.py tcm \
     --did F40C --did 0500 --did 2102 --did 2103 \
     --did F405 --did 0301 --did 04FE \
     --did 1018 --did 101A --did 101B --did 101D --did 101F --did 1020 \
     --pair 6/14 \
     --conditions "parked; ignition ON; engine OFF; ZF9HP priority support check"
   ```

   This is a dry run unless `--execute --confirm-parked` is added. It sends
   thirteen physical service-`22` reads, no functional broadcast, session
   change, keepalive, routine, IO control, write, or clear request.
6. Reproduce each resolved request using a bounded physical read to the
   verified ECU endpoint and required session behavior. Do not promote a
   label merely because AlfaOBD rendered it.
7. Validate physical behavior:
   after a sufficiently long cold soak, coolant and oil should begin near
   ambient; during normal cold-start warm-up, coolant should rise smoothly
   through the thermostat region and oil should generally lag coolant; oil
   pressure should track RPM and pump-mode changes. Torque requires natural
   road load—idling establishes only a baseline.
8. Search the simultaneous full C-CAN stream for passive equivalents. A
   verified broadcast signal is preferable for week-long monitoring because it
   avoids continuous diagnostic polling.
9. Add a metric only with exact ECU namespace, source, units, scale, quality,
   provenance, staleness, valid vehicle-state gate, and context-aware display
   policy.

Standard Mode 01 OBD-II values are semantic priors only. They have not
responded on this internal SGW-bypass C-CAN tap and should not be retried as
the acquisition plan.

## Additional high-value dashboard targets

After the owner priorities, the most useful mechanically/electrically oriented
targets are:

- turbine speed, converter slip/temperature, current/target gear, and torque
  intervention, after the first-tier transmission-oil-temperature mapping;
- per-cylinder misfire counters and current/pending DTC state;
- short/long fuel trims, commanded/actual lambda, MAP, IAT, barometric
  pressure, throttle/pedal/load, ignition timing, and knock retard;
- VVT desired-versus-actual angles and cooling-fan command;
- catalyst temperature;
- charging voltage plus generator/battery current, duty, and state of charge
  if the installed configuration exposes them;
- oil life, engine hours/runtime, and other maintenance counters;
- verified speed, RPM, gear, brake, doors, and four wheel-position tire
  pressures.

These are ranked research targets, not claims that every signal is already
available or correctly scaled. A useful eventual presentation should group
signals that explain one another rather than show isolated numbers:

- oil pressure, oil-pressure sensor voltage, pump mode, RPM, and temperatures;
- coolant temperature, fan command, vehicle speed, and ambient temperature;
- actual/target charging voltage, generator duty/current, and state of charge;
- torque, RPM, load/throttle, ignition timing, and knock retard; and
- transmission temperature, turbine/output speeds, converter slip, and gear.
