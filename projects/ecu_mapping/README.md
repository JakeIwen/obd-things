# ecu_mapping — mining AlfaOBD debug logs into ECU DID / routine / actuation maps

Goal: turn AlfaOBD's own diagnostic sessions into per-module maps for our 2022 Ram Promaster
(**VIN `3C6LRVDG4NE######`**) — which DIDs each ECU exposes, what routines/actuations
AlfaOBD runs, and their addressing — to feed the radar, TPMS, and BCM/remote-unlock work
without re-deriving from scratch. AlfaOBD is a high-yield *candidate source*, not an infallible
DID oracle: a mismatched profile can poll unsupported DIDs or apply the wrong labels/scaling.
Treat its raw request/response trace as evidence and verify rendered interpretations against the
installed subtype or controlled ground truth.

> **Operational topology update (2026-08-21):** the permanent Pi installation
> has simultaneous serial-resolved C-CAN, B-CAN, and CAN-CH roles. Historical
> campaigns below accurately preserve the PCAN observer, physical pair, and
> evidence provenance that produced their findings, but their `can0`,
> `bringup.sh`, cable-moving, and mutually exclusive service instructions are
> not current operations. Never translate those commands to an arbitrary
> `gs_usb` netdev. Current passive telemetry belongs to the installed, enabled,
> and active role-aware vehicle-data broker. Maintained live diagnostic tools
> now use the shared, tool-scoped role owner: each resolves and exclusively owns its exact
> role/channel, arms only for the bounded operation, and restores passive state.
> There is no standalone command that leaves a role armed, and completed legacy
> drive campaigns must not be reconstructed around one.

The cross-project [`AlfaOBD evidence history`](../../docs/alfaobd-evidence-history.md) is the
canonical chronology of confirmed mis-mappings, incompatible profiles, recording/catalog traps,
project parser corrections, and the trust rules derived from them.

The local
[`legacy FCA Windows/CDA archive triage`](findings/promaster_2022/2026-07-29_legacy_fca_windows_archive.md)
recovers historical vendor labels for exact current-DID overlaps, including BCM PROXI/EOL records
and the RF Hub TPMS family. Its 2007-2011 engineering bundles and September 2022 report for the
owner's prior model-year 2015 diesel VF are valuable candidate/corroboration sources, not 2022
authorities; direct conflicts between legacy variants are preserved in the finding. A focused
[`legacy PCM comparison`](findings/promaster_2022/2026-07-30_legacy_pcm_cda_overlap.md)
finds all 14 service-`22` requests observed in current PCM recordings and 167/190 unique requests
from the AlfaOBD PCM catalog; 157/187 already established current-positive requests also occur in
the strongest old profiles. It corroborates ten changing mappings, preserves a fuel-level
ambiguity, and rejects incompatible legacy throttle-blade and vehicle-speed scales. A separate
[`legacy module comparison`](findings/promaster_2022/2026-07-30_legacy_module_cda_overlap.md)
recovers shifter/GSM lifecycle-layout candidates while bounding ABS, EPS, and ORC results to
identity continuity.

The permanent three-tap data and active-poll readiness analysis is recorded in
[`2026-08-27 three-bus telemetry readiness`](findings/promaster_2022/2026-08-27_three_bus_telemetry_readiness.md).
Its bounded, owner-authorized APK work joins the current ABS `F1A5` subtype to
the exact eleven-entry AlfaOBD Plots request array: `1002`, `1004`, and
`0880-0888` cover speed, voltage, four wheel speeds, pressure, steering angle,
yaw, and lateral/longitudinal acceleration. These are installed-variant
vendor-derived candidates, not live-verified DIDs; the finding preserves the
candidate formulas for offline work. The owner subsequently reported that an
AlfaOBD ABS connection illuminates multiple IPC warnings, so the prepared ABS
support pass was withdrawn and ordinary CAN-CH telemetry is now passive-only.
The enabled broker-owned recorder now
requires separate synchronized raw C-CAN, B-CAN, and CAN-CH streams on every
future qualified drive. A later parked no-session check admitted only ICS
`2001` to implementation: it is exposed as starred candidate-quality
`vehicle.odometer` because its first live decode was 11.140 mi below the dash.
The independent fixed B-CAN helper and recorder-companion path preserve raw
evidence for resolving that relationship; Uconnect temperature was omitted by
owner direction.

The current owner-priority C-CAN roadmap—oil pressure, coolant/oil temperature,
torque and derived power, OEM operating context, and the targeted PCM/TCM
acquisition order—is recorded in
[`2026-07-25_priority_telemetry_targets.md`](findings/promaster_2022/2026-07-25_priority_telemetry_targets.md).
The subsequent
[`2026-07-26 PCM Plots idle mapping`](findings/promaster_2022/2026-07-26_pcm_plots_idle_mapping.md)
qualified passive oil-pressure and coolant-temperature dashboard sources and
mapped the selected PCM gauge set. The
[`2026-07-27 loaded-drive mapping`](findings/promaster_2022/2026-07-27_pcm_plots_loaded_drive_mapping.md)
then established exact diagnostic engine RPM, signed loaded torque, and VVT-oil-temperature
scales, while proving that this PCM profile's transmission-temperature and turbine-speed rows are
unsupported. Passive torque, true sump-oil temperature, and derived power
remain open. A later
[`PCM generator-duty direct-read follow-up`](findings/promaster_2022/2026-07-30_pcm_generator_duty_direct_read.md)
verified two fixed-DLC-8 zero-padded physical `22 01A1` reads without an
explicit session change, both returning exactly 100.000%. Its corrected
arbitrary-bitfield searches over `0x100` and `0x412` still failed to produce a
cross-regime passive carrier. The vehicle-data project now implements that
direction as the narrowly allowlisted `generator.field_duty` source
`pcm.did.01a1`: the broker supervises one engine-running armed interval,
continues the existing broadcast telemetry during it, sends no session-control
or tester-present traffic, and requires exact listen-only restoration. The
former PCAN deployment live-validated approximately one-hertz dashboard
updates while recording PCM and RF Hub request/positive-response pairs. The
serial-resolved dual-USBCANFD broker was installed and passively live-validated
on 2026-08-21. Its normal active-drive feature is enabled, but the vehicle was
asleep during commissioning, so no helper ran and no active diagnostic polling
was validated; consult the vehicle-data deployment section before treating that
path as exercised.
The subsequent
[`broker-drive poll validation`](findings/promaster_2022/2026-08-04_broker_drive_poll_validation.md)
accounts for 8,514 complete production scheduler cycles across two zero-drop
drives: all 17,028 PCM requests and all 8,514 RF Hub requests received exact
physical positive responses. It also bounds the three-read scheduler's added
C-CAN duty below 0.2% and records that the attempted B-CAN leg captured no
frames because its interface was down.
A later
[`signed 0x1F7 byte-3 mapping`](findings/promaster_2022/2026-07-29_tcm_oil_temperature_candidate.md)
found the strongest transmission-oil carrier candidate so far. The candidate
failed its precommitted narrow-range blind R²/scaling gates; a later
zero-drop cold-start leg then passed the frozen broad-range carrier and
affine gates across 33–85 °C. The predeclared
`°C = 0.375 × signed_i8 + 57` formula also passed directly with 0.863 °C
RMSE, 0.135 °C absolute mean bias, and 1.0 °C p95 absolute error over 2,027
exact samples. The leg nevertheless failed the separate requirement to beat
TCU-chip-temperature R² by at least 0.10. A subsequent predeclared hot-soak
counterexample resolved that ambiguity: the fixed formula produced 0.838 °C
RMSE against gearbox oil versus 13.106 °C against chip temperature, with a
0.207 R² advantage and 1,164 consecutive paired cycles separated by at least
3 °C. Every frozen integrity, scale, and identity gate passed. Passive
`0x1F7` byte 3 is now telemetry-allowlisted as transmission-oil temperature
using `°C = 0.375 × signed_i8 + 57`, displayed in °F without an invented
warning threshold. The reviewed `--profile tcm-thermal` mode in
the now-retired `cluster_drive_log.py` recorded the paired `04FE/0301`
thermal-discrimination drives at two total requests per second with integrated
loss-accounted raw evidence. The executable was deleted after the completed
campaign; the evidence and conclusions remain in the linked findings.

The related-profile PCM thermal families are now closed. The installed PCM
entered session `92` but returned NRC `12` for `3159`, `315A`, `B010`,
`B011`, and `B012`; the latter trio also has independent padded on-wire
evidence. Preserve their recovered vendor labels as research provenance, but
do not repeat or broaden around them. True engine-oil temperature remains
open and now requires an installed-calibration or separately standardized
source, not another cross-profile adjacency guess.

The full AlfaOBD `TIGERSHARK_CUSW` PCM Plots request alignment is now an
executable, ECU-scoped 193-row/190-DID catalog in
[`pcm_tigershark.py`](pcm_tigershark.py). Its names and request identifiers are
vendor-derived navigation evidence; only the independently observed anchors
and separately documented decodes are current-vehicle facts.

## ⚠️ Provenance — two vans in the data (read before trusting anything)

AlfaOBD debug files accumulate across every vehicle a tablet has touched. We have two dumps:

| source (in `~/claude/shared-files/`) | vehicle | use |
|---|---|---|
| `old.AlfaOBD_Debug.bin` (~396 MB, 2022–2024) | multi-profile **aggregate**; only F190-identified vehicle is the prior **2015** diesel `3C6TRVDD2FE######` | reference only — NOT 2022 ProMaster |
| `AlfaOBD logs and data July 8 2026/` | **2022 ProMaster** `3C6LRVDG4NE######` (fresh, 2026-07-07) | ground truth |

**"Recording data for X" is the AlfaOBD *profile the operator selected*, not confirmed
hardware.** Many entries are near-empty probes — check `reads=` in the map. The 396 MB `old.`
bin is a multi-year, multi-profile **aggregate**: its only F190-identified vehicle is the 2015
diesel, but it also carries unrelated profiles (e.g. 2024 "Chrysler Pentastar 2021" sessions —
`3E01` keepalive only, no F190 — which are NOT the diesel, and are a *gas* Pentastar profile,
possibly an early poke at 2022 ProMaster or another vehicle). So the promaster_2015_diesel map header reads
"F190-identified VIN", not "the vehicle", and a profile name may not match that VIN.

Within the fresh folder, `AlfaOBD_Debug.bin` (2.9 MB) is **100 % 2022 ProMaster**, and
`RFH_FGA_Info.log` / `ADAPTIVE_CRUISE_Info.log` / `TIGERSHARK_CUSW_Info.log` are 2022 ProMaster.
**Gotcha:** `BCDELPHI_Info.log` is a cumulative, mixed-vehicle Body Computer text log. Its early
status/DTC snapshots include the old 2015 van and cannot be applied wholesale to this vehicle, but
its 2026-06-22 tail aligns within seconds with the current-van debug trace's BCM configuration write
and DTC clear. Use only timestamp-correlated entries, and capture a fresh single-module log when a
label matters. Always run `vin_scan.py` on any new debug bin first. See memory
`[[alfaobd-debug-bin-other-van]]`.

## The AlfaOBD debug format

`*_Debug.bin` (Preferences → "Debug Data recording") is an ASCII **hex** string whose bytes
are the **ones-complement (XOR 0xFF)** of the log text. Decoded, it's a timestamped ELM/STN
adapter trace: `HH:MM:SS.mmm S:/R: <hex>`, where each payload is hex-encoded ASCII (a UDS
message like `22F190`, or an `AT`/`ST` command). Multi-frame responses come back as a length
line + indexed `0:`/`1:`/`2:` segments. `ATSH <hdr>` lines set the target module address.
Because AlfaOBD may write the full date only at `Recording closed`, the parsers pre-index
clock-ordered header/close pairs before streaming exchanges; long-open or unclosed recordings keep
the prior best-known date instead of being blindly backdated from a later close marker.

To capture a fresh one on the tablet: enable **Debug Data recording** (raw) and ideally
**Gauges data recording** (labeled CSV, commonly `Gauges_Data.csv` or `Gauges_Data.log`) in
Preferences, then run the **Plots** scan and verify actual file growth. AlfaOBD's Plots start
handler automatically starts its CSV writer when the preference is enabled and also enables the
separate manual recording toggle. The 2026-07-22 current-van campaign used **Status**, not Plots,
so its checked recording preferences and unchanged `Gauges_Data.csv` are consistent rather than a
recording failure. This APK appends buffered UTF-8 rows and explicitly flushes/closes the CSV on a
clean Plots stop rather than after every row, so verify growth after stopping and then stable size;
do not require immediate live file growth. Pull the resulting files from
`/sdcard/Android/data/com.android.AlfaOBD/files/logs/`.

## Pipeline

```
tools/alfaobd_decode.py  <in.bin> [out.txt]      # generic: .bin -> decoded text (reusable)
tools/alfaobd_gauges.py  <Gauges_Data.csv>       # offline section/profile/metric inventory -> tmp/
tools/alfaobd_gauge_join.py <Gauges_Data.csv> <decoded.txt> --section N  # offline DID candidates
tools/candump_diagnostic_wire.py <capture...> --module pcm   # exact 22/62 wire stream
tools/can_timeseries_correlate.py <capture...> --module pcm  # DID-to-broadcast candidates
tools/alfaobd_dat.py <post.dat> --baseline <pre.dat>  # detect cached/duplicated plot series
tools/alfaobd_apk_db.py  <base.apk>              # reconstruct catalog DB + label resource -> tmp/
tools/alfaobd_catalog.py <db> <labels> --device-id N  # read-only model/device export -> tmp/
tools/alfaobd_bcm_decode.py                   # apply current-BCM field layouts to existing evidence
tools/fca_hsql_decode.py <db.data> --script <db.script> --properties <db.properties>  # legacy FCA .eng DB -> JSON
tools/alfaobd_singleton_campaign.py plan <plan.json>  # guarded one-label Status monitor
tools/passive_drive_capture.py --out-root <path>      # bounded C-CAN recorder; plan by default
tools/alfaobd_singleton_join.py <campaign> --capture-set <json>  # offline evidence join
tools/alfaobd_singleton_infer.py <join.json>          # offline candidate scale/enum inference
projects/ecu_mapping/vin_scan.py        <decoded.txt> [vin]   # which van? (run FIRST)
projects/ecu_mapping/extract_did_map.py <decoded.txt> <out>   # per-module DID/service map
projects/ecu_mapping/alfalog.py                  # shared log parser + ELM reassembly
```

`alfaobd_gauges.py` understands that a single Gauges Data file is a concatenation of many
independently headed CSV recordings. It distinguishes explicit selected-profile markers from
blank markers whose sections inherit the most recent named profile, counts corrupt/partial rows,
and keeps identically named metrics separated by selected-profile namespace. By default it writes
`inventory.json`, `sections.csv`, and `metrics.csv` under
`tmp/inventories/alfaobd_gauges/`; use `--out-dir` to choose another machine-output directory.
Gauge labels and rendered values do **not** carry diagnostic identifiers, so a label-to-DID or
label-to-local-identifier claim still requires a time-aligned Debug Data trace or a controlled
one-variable capture.

`alfaobd_gauge_join.py` performs that time alignment for one bounded Gauge section at a time. It
preserves every original sample row, column index, and comma/semicolon/tab delimiter; uses the
response-completion timestamp from the decoded trace; splits repeated-identifier polling loops; and
explicitly scores preceding/current/following-cycle lag hypotheses. Exact `22 DDDD -> 62 DDDD`
reads enter the DID namespace, while exact legacy `21 LL -> 61 LL` reads enter a distinct
one-byte-local-identifier namespace. Keys such as `22:00A1` and `21:A1` cannot collide, and a wrong
echo or other response prefix never becomes a payload candidate. Constants, `NA`, and fewer than
three varying values remain unidentifiable. Reports and evidence JSONL default under
`tmp/inventories/alfaobd_gauge_join/` and always say `candidate_only`. Pass exactly one decoded
debug source per run—historic conflict files are overlapping cumulative snapshots, not independent
samples—and label old-van work explicitly. If an unclosed cumulative Debug recording inherited the
preceding session's date, `--debug-date YYYY-MM-DD` may deliberately select that raw date while the
Gauge section keeps its own date. The report records both dates and
`date_overrides_gauge_section: true`; use this only after the bounded clock times, polling order,
and exact raw exchanges prove the association:

The decoded-log parser buffers all `R:` callbacks through the adapter prompt because AlfaOBD can
split one indexed ISO-TP row between callbacks. It validates index order and row widths, applies the
ELM byte-count header, and fails closed on incomplete, malformed, or oversized response blocks.
Only prompt-completed exact positive echoes supply bytes to the fitter. In schema-2 reports,
`polling.cycle_boundary_key` is the authoritative service-qualified boundary; the legacy
`polling.cycle_boundary` remains DID-only and is null for a `21` local-identifier boundary.

```bash
python3 tools/alfaobd_gauge_join.py Gauges_Data.csv decoded.txt \
  --section 2 --address DA10F1 --source-scope historical-other-vehicle
```

As an offline regression check, section 2 of the recovered historical diesel snapshot reproduces
`386F` byte 0 as `displayed = 50 * raw` across 2,370 samples and `18DE` bytes 0–1 as
`displayed = 0.02 * raw - 40`. Both remain old-vehicle reference mappings, not current-van facts.

The joiner caps retained matching debug exchanges at 100,000 and candidate hypotheses per metric
at 20,000 by default so an unexpectedly large cumulative source fails before consuming the Pi's
memory. Narrow by section/profile/address, `--did`, or `--local-id` first; raise either limit only
for a deliberate larger run. It also refuses to guess when prompt-completion timestamps do not
identify one polling boundary; inspect the trace and supply `--boundary-did` or
`--boundary-local-id` explicitly in that case.

Even an exact historical fit is reference-only until the same ECU/DID/scaling is established on
the current van; correlation by itself is not controlled ground truth.

`alfaobd_dat.py` inspects the separate `Data/*.dat` plot caches, whose observed format alternates
an opaque decimal series ID with a semicolon-delimited value row. These files carry no timestamps
or verified DID/label mapping. Supply a pre-campaign `--baseline` to classify each series as
unchanged, appended, truncated, changed, or an exact mechanical repetition; use `--json` only with
an explicit report path under `tmp/`. The 2026-07-22 C-CAN campaign is the motivating case:
all 16 ZF series became exact twofold repetitions, while all 12 PCM series were unchanged, so
neither file was fresh labeled evidence.

The APK tools operate only on an owner-supplied local package and default all generated artifacts
under `tmp/`. The catalog exporter opens SQLite in read-only mode, preserves source hashes and raw
fields, and marks its mechanical English-resource substitutions as unverified: the APK's numeric
placeholders have another unresolved runtime indirection, so raw placeholder IDs win. Do not commit
or redistribute the APK, reconstructed database, or extracted application resource.

`alfaobd_bcm_decode.py` opens the reconstructed database read-only and mechanically applies the
current BCM subtype's bit layouts to already captured responses. It verifies every inventory's
paired summary against the BCM module key and exact TX/RX endpoint, carries diagnostic-session and
vehicle-condition provenance into each observation, and keeps all names/units as unresolved raw
catalog references. Its default JSON report lives under `tmp/ecu_mapping/android_tablet/`; it does
not open CAN or ADB.

## Capture and correlation workflows

### Current AlfaOBD + role-aware passive C-CAN capture workflow

The July campaign results and PCAN condition strings remain in dated findings
and capture manifests as evidence provenance. The operating examples here use
the maintained permanent-role path: no adapter moves, no saved netdev, and no
manual CAN link bring-up.

Two guarded tools now turn the useful part of the 2026-07-22 AlfaOBD experiment into a repeatable
campaign:

- `alfaobd_singleton_campaign.py` operates only on an already-connected ECU's **System status**
  page. It selects exactly one visible `Monitor parameters` label, records Debug/Info byte
  boundaries, and repeats anchor labels. It never chooses a vehicle/profile, connects an ECU,
  enters Active Diagnostics, changes Android settings, or touches network/proxy configuration.
- `passive_drive_capture.py` records one persistent `candump` stream into ten-minute zstd chunks.
  Its built-in `ccan-correlation` priority stream includes the verified speed/brake/door/voltage
  anchors, the qualified `0x0FC` RPM, `0x2ED` coolant, and `0x41D` oil-pressure
  sources, the leading `0x100` torque, `0x412` temperature, and `0x41B`
  throttle candidates, and all registered C-CAN diagnostic request/response
  IDs. Its maintained live path resolves C-CAN by exact USB identity, holds
  shared role/channel locks, never configures or transmits, and stops if the
  identity or exact passive classical-CAN state changes.

Both hold shared observer locks, so they may run together while an exclusive
owner or interface reconfiguration on that same role/channel remains excluded.
They do not stop, restart, or globally block `tpms-logger`, `tpms-drivesniff`,
or an owner of another bus; normal lock contention is scoped to the resolved
role in use.

The tracked five-signal cluster shakedown was completed on 2026-07-24:

```bash
python3 tools/alfaobd_singleton_campaign.py plan \
  projects/ecu_mapping/configs/alfaobd_cluster_singleton_shakedown.json
```

Before executing it, connect the tablet by USB, connect AlfaOBD to
`Instrument panel Continental`, open System status with `Monitor parameters` enabled, and leave
the red **play triangle** visible (monitor stopped). AlfaOBD Debug Data recording must already be
enabled, and both `AlfaOBD_Debug.bin` and `MARELLI_DASH_EP_Info.log` must already exist so their
starting offsets are unambiguous. Inspect the broker, recorders, TPMS services,
and role state before the campaign; stop an active participating owner through
its current handoff. `passive_drive_capture.py` resolves C-CAN itself and
requires the exact passive classical-CAN state. It neither configures the link
nor stops/restarts a service. Do not precede it with raw `ip link`, a historical
bring-up command, or an adapter move.

The 2026-07-23 vanpi storage audit (`findmnt`, numeric ownership, and the kernel exFAT warning)
found that EXFAT512 needed a clean fsck/remount and that its automatic exFAT mount lacked
`uid=1000,gid=1000`; without those options it maps files to UID/GID 65534 and is not writable by
`pi`. The volume was repaired and remounted on 2026-07-25, and the persistent exFAT branch in
`/home/pi/scripts/mount_disks.sh` now obtains `pi`'s numeric UID/GID and applies
`fmask=0022,dmask=0022`. At the checkpoint, the host reported the filesystem read-write with
58 GiB free, the capture tree belonged to `pi:pi`, and the disk-health watchdog's create/remove
probe succeeded as `pi`. Recheck the exact mount, ownership, writability, and free space after every
reconnect; these are temporal host facts, not permanent disk guarantees.

For a simultaneous shakedown, choose one safe timestamped identifier (for example
`cluster-shakedown-20260724-120000`), replace `RUN_ID` with that exact value in both panes, and
start the recorder pane first. Use at least 20 minutes: the Android 7 tablet's repeated guarded UI
dumps made the seven-segment run take about 15 minutes 40 seconds even though the actual monitor
dwell was only 12 seconds per segment. Run both commands in detached `tmux` sessions for live work;
an SSH/tool-session lifetime must not determine capture lifetime.

```bash
python3 tools/passive_drive_capture.py \
  --out-root /mnt/EXFAT512/obd-things/tmp/captures/ccan/drive-correlation \
  --require-mount /mnt/EXFAT512 \
  --campaign "RUN_ID" \
  --duration-seconds 1200 \
  --soft-free-gib 30 --hard-free-gib 25 \
  --execute --confirm-passive \
  --conditions "parked; ignition ON; engine OFF; serial-resolved C-CAN 6/14 passive; OBDLink MX+ parallel"
```

The AlfaOBD pane is:

```bash
python3 tools/alfaobd_singleton_campaign.py run \
  projects/ecu_mapping/configs/alfaobd_cluster_singleton_shakedown.json \
  --campaign-id "RUN_ID" \
  --out-root /mnt/EXFAT512/obd-things/tmp/ecu_mapping/alfaobd-drive \
  --require-mount /mnt/EXFAT512 \
  --execute --confirm-read-only-diagnostics --confirm-parked-shakedown \
  --confirm-monitor-stopped \
  --conditions "parked; ignition ON; engine OFF; cluster System-status page"
```

For a hands-off ordinary-driving scaling run, use the dedicated plan instead of
the short parked shakedown plan:

```bash
python3 tools/alfaobd_singleton_campaign.py plan \
  projects/ecu_mapping/configs/alfaobd_cluster_scaling_drive.json
```

It schedules Battery Voltage (+30), Engine speed, Vehicle speed, Actual Gear,
and Outside temperature, then repeats Engine speed, Vehicle speed, and Battery
Voltage (+30). The already-qualified battery signal deliberately occupies both
outer positions: this tablet's 8 KiB Info buffering can contaminate the first
and truncate the last rendered run, while both high-value RPM and speed
observations now remain interior. Each singleton has a 45-second requested
dwell so the changing RPM/speed signals have useful range despite the tablet's
slow UI observations. The eight-segment run is expected to take roughly 21–25
minutes on the current tablet. Start it while parked, with the engine running
and AlfaOBD already connected to `Instrument panel Continental` on the stopped
System-status monitor page. After both supervisors have passed their
parked-start preflights, ordinary driving requires no tablet interaction.

Use one new timestamped `RUN_ID` in both panes. Start the passive recorder
first and give it enough time to cover the complete Alfa run:

```bash
python3 tools/passive_drive_capture.py \
  --out-root /mnt/EXFAT512/obd-things/tmp/captures/ccan/drive-correlation \
  --require-mount /mnt/EXFAT512 \
  --campaign "RUN_ID" \
  --duration-seconds 1800 \
  --soft-free-gib 30 --hard-free-gib 25 \
  --execute --confirm-passive \
  --conditions "ordinary driving; parked start; engine running; serial-resolved C-CAN 6/14 passive; OBDLink MX+ parallel"

python3 tools/alfaobd_singleton_campaign.py run \
  projects/ecu_mapping/configs/alfaobd_cluster_scaling_drive.json \
  --campaign-id "RUN_ID" \
  --out-root /mnt/EXFAT512/obd-things/tmp/ecu_mapping/alfaobd-drive \
  --require-mount /mnt/EXFAT512 \
  --execute --confirm-read-only-diagnostics --confirm-ordinary-driving \
  --confirm-monitor-stopped \
  --conditions "ordinary driving; parked start; engine running; cluster System-status page; no driver tablet interaction"
```

The drive should include normal acceleration, deceleration, a steady-speed
interval, and a complete stop where convenient. `Actual Gear` is currently a
PRND-selector candidate, not a proven automatic-ratio signal, so ordinary
transmission shifts may leave it at `D`; treat any new selector state as
opportunistic evidence and collect controlled parked/foot-brake PRND changes
separately if needed. No special maneuver is worth distracting the driver. The
supervisor changes only the whitelisted System-status monitor selection; it
cannot enter Active Diagnostics or send AlfaOBD actuation commands.

The supervisor requires at least one configured activity witness to grow during the early
post-start liveness check and requires **both** Debug and profile-Info growth by the end of every
segment. The distinction matters on this tablet: Debug grew within two seconds, while the Info
writer flushed later in 8 KiB increments. Only the profile Info file must become stable after the
stop tap (the connected app can continue writing non-parameter traffic to Debug). The play
triangle must be present before start, the white
stop-hand while running, and the play triangle again after stop. If a tap, crash, modal, UI
version, log-growth check, tablet-space check, or state transition is ambiguous, it sends no
guessed compensating tap and leaves `manual_reconcile: true` in `state.json`. It also rechecks
EXFAT512's mount identity and writability before creating output and before every final artifact
pull; a missing, short, or failed pull prevents campaign completion. There is deliberately no
automatic resume from that state.

For a passive/AlfaOBD pane during the upcoming 20-hour drive, the passive recorder can use
`--duration-seconds 72000` with an ordinary-driving conditions note. The measured C-CAN rate is
about 653 MB/hour as uncompressed candump text, or roughly 13.1 GB for 20 hours before zstd.
Keep the 30/25 GiB soft/hard floors so ordinary use retains at least 25 GiB, and verify EXFAT512
is actually mounted writable; the tool
requires the named mount and rechecks its device identity so a missing external disk cannot
silently redirect logs onto the Pi's root filesystem. It enables SocketCAN drop accounting and
stops rather than silently accepting a reported drop. A socket/driver drop, pre-duration process
signal, unexpected `candump`
exit, forced child termination, compressor/verification failure, mount change, stalled chunk
finalization, or pre-duration hard-floor stop makes the command fail and the final manifest says
`success: false`. A deliberate soft-floor transition can continue with the bounded priority
stream, but the manifest then says `full_stream_complete: false`. Raw output remains under the
external disk's `obd-things/tmp/` tree and is never committed in place.

### Role-aware one-shot passive C-CAN mapping-drive service

`tools/ignition_triggered_passive_capture.py` removes the SSH/Codex-session
dependency from one ordinary drive. Its default is an inert plan. When explicitly
executed, it resolves the permanent C-CAN role by USB identity and holds shared
role/channel ownership while listening for the verified ignition-presence frame
`0x2EF`; it never changes the link or transmits. The first fresh
`0x2EF` starts `passive_drive_capture.py` on EXFAT512. After `0x2EF` has first
been seen, 20 seconds of absence cleanly ends the run, finalizes and verifies all
zstd chunks, and records `reason: tracked_id_absent` with `success: true`.
External termination, CAN-interface drift, new SocketCAN drops, storage failure,
or the hard disk floor still produce a failed campaign.

The tracked `promaster-mapping-drive.service` now uses that serial-resolved
passive path and contains no fixed channel or adapter movement. It is a one-shot
campaign unit, not a boot telemetry service: it still does not configure the
interfaces, control AlfaOBD, or stop other services, and the external mount plus
exact passive C-CAN role must already be available. Its privileged `ExecStartPre` raises only
`net.core.rmem_max` to the recorder's guarded 16 MiB socket reserve. This
quadruples the former reserve after one 46-minute capture reported 3,728
socket drops during a transient consumer/storage stall; drop detection and
the zero-drop evidence gate remain mandatory.

The tracked role-aware unit replaced the historical installed copy on
2026-08-21 and is disabled/inactive. It was not started or capture-validated
during broker commissioning. Before a future owner-authorized campaign, inspect
the effective unit and verify the role reconciler and exact passive C-CAN role
are available.

### Broker-coordinated automatic drive capture

The one-shot `promaster-mapping-drive.service` above is for a deliberately
bounded passive campaign. The maintained, automatically rearming production
recorder is instead
[`projects/vehicle_data/drive_recorder.py`](../vehicle_data/drive_recorder.py),
tracked as `van-drive-recorder.service`. Its installed copy matches the tracked
role-aware unit as of 2026-08-21 but remains disabled/inactive; no new
dual-USBCANFD drive recording was validated during passive broker commissioning.

This companion is receive-only: it does not take the channel lock, configure
or restore the serial-resolved C-CAN channel, control the broker, or transmit.
It waits until broker status
proves that the reviewed active-drive helper owns healthy armed C-CAN, requires
an initial `0x2EF` within five seconds, then records full and priority
loss-accounted zstd streams on EXFAT512. `candump -D` survives the broker's
expected end-of-interval SocketCAN restoration; twenty seconds without `0x2EF`
ends and verifies that drive. After successful finalization the daemon returns
to its broker-status wait and automatically starts a new timestamped campaign
on the next drive. In dual mode it binds the broker-reported netdev only after
rechecking the exact USB serial/`dev_id`; the dated campaigns below used the
former PCAN `can0` and remain historical deployment evidence.

The live mid-drive deployment on 2026-07-30 used overlapping receive-only raw
coverage and did not restart the broker or reconfigure the interface. The
hardened campaign began as
`broker-drive-20260731T001704814083` under
`/mnt/EXFAT512/obd-things/tmp/captures/ccan/broker-drive/`. It contains PCM
`01A1` and RF Hub request/positive-response pairs with no observed candump
drop marker while telemetry duty and all four tire pressures continued
refreshing. It cleanly finalized after 52 minutes 22 seconds and 8,514,259
full-stream frames: all six full and priority rotations completed, no partial
remained, and the manifest reported zero socket drops,
`full_stream_complete=true`, `reason=tracked_id_absent`, and `success=true`.
The broker restored verified 500-kbit/s listen-only C-CAN without an inhibit,
and the enabled recorder returned automatically to its idle broker-ownership
wait. The next drive therefore needs no manual recorder arming; actual creation
of its new campaign awaits qualified engine-running evidence.

That automatic rearm then succeeded for two subsequent legs. Campaigns
`broker-drive-20260731T012751675165` and
`broker-drive-20260731T030218233898` independently started on later qualified
engine-running intervals and finalized normally after 81 minutes 3 seconds and
66 minutes 40 seconds. Across all three clean campaigns the recorder retained
32,555,907 full-stream frames and 16,273,335 priority-stream frames in 22
complete rotations, with zero detected socket drops and no partials. Offline
exact-wire extraction recovered 2,400 PCM `01A1` positives from each leg's
first four chunks. The cross-leg passive-field comparison and its compute
provenance are in the
[`PCM generator-duty finding`](findings/promaster_2022/2026-07-30_pcm_generator_duty_direct_read.md).
The bounded bit search found no transferable passive carrier: even the
repeatable `0x417 u8be@18` geometry failed no-refit validation at 12.8–13.9
percentage points RMSE. Continue using the guarded direct `01A1` read.

### Role-aware B-CAN awake-interval recorder

[`bcan_drive_recorder.py`](bcan_drive_recorder.py) is a receive-only recorder
for the permanent Board A CAN2 role. Each probe resolves B-CAN from exact USB
serial/`dev_id`, acquires shared logical-role and resolved-channel locks, and
requires an exact 125-kbit/s classical-CAN, FD-off, listen-only,
ERROR-ACTIVE, `restart-ms 0` readback. It periodically revalidates identity
through capture and never configures a link, wakes a bus, transmits, or issues a
diagnostic request.

A successful probe requires at least three IDs from the verified B-CAN
signature set and retains the same ownership through recorder startup and the
complete interval. A silent probe releases ownership for the retry wait.
`0x46C` must appear within five seconds after the recorder opens; thirty
seconds without it ends and verifies the interval. Short fob-wake intervals are
kept deliberately because they remain useful wake/sleep evidence.

Output lives below
`/mnt/EXFAT512/obd-things/tmp/captures/bcan/auto-drive/`; state lives at
`tmp/ecu_mapping/bcan-drive-recorder-state.json`. Full and priority streams
retain the 30/25 GiB soft/hard free-space floors.

The tracked `promaster-bcan-recorder.service` now orders after/wants the
role-aware telemetry broker, has no fixed-interface bring-up or mutual-bus
Conflicts, and may coexist with independent C-CAN ownership. On 2026-08-21 this
tracked unit replaced the legacy installed unit and its old enablement; it is
disabled/inactive and was not capture-validated during broker commissioning.
Before a future authorized campaign, inspect the effective unit and then
validate its first passive B-CAN interval without disturbing the other roles.

The original 2026-08-07 PCAN deployment and its captures remain valid
provenance: it verified the 125-kbit/s signature, automatic interval rearming,
zero-error passive readback, and a guarded historical `0x46C` voltage sample.
Its adapter-moving, fixed-`can0`, wake-assisted-voltage, and mutually exclusive
service procedure is retired and must not be reconstructed.

The automatic passive recording remains valuable if AlfaOBD is absent, but it
cannot by itself attach labels to unresolved signals. For the intended PCM
correlation run, AlfaOBD must be connected through the OBDLink MX+, on the
current-vehicle Pentastar/Hemi **Plots** page, with the chosen gauges scanning
and Gauges Data recording enabled. Keep the Android tablet connected to the Pi
by USB: Bluetooth carries AlfaOBD's vehicle traffic, while USB gives ADB control,
keeps the tablet powered/awake, permits fail-closed page/state checks, and lets
the Pi pull Debug and `Gauges_Data.csv` artifacts after a clean stop. A scan
already running in AlfaOBD may continue without USB, but the Pi then cannot
supervise, stop, or retrieve it reliably; that is not the prepared campaign.

AlfaOBD cold-start navigation is not attached to the ignition trigger. Before
driving, while safely parked, connect the tablet by USB, connect AlfaOBD to the
PCM, select the reviewed gauge set, and start Plots recording. A future guarded
automation may start/stop a verified already-staged Plots page, but it must not
guess through profile selection, Bluetooth connection dialogs, or stale UI
state. PCM and TCM profiles also require separate AlfaOBD sessions.

For the first loaded PCM run, use at most these twelve gauges so the trace
excites the high-value unresolved mappings without diluting the polling rate:

1. Vehicle speed
2. Engine speed
3. Current engine torque
4. Coolant temperature
5. Engine oil pressure
6. VVT Oil Temperature
7. Transmission Oil Temperature
8. Turbine speed
9. Output Speed
10. Throttle Blade Position
11. Generator Duty Cycle
12. Battery voltage

This replaces the idle campaign's low-value or already-redundant Fuel Level,
Throttle Position Sensor Percent, Target Charging Voltage, Voltage Sense, and
VVT Oil Pressure rows. The separate TCM profile remains the follow-up for
independent transmission-temperature verification.

An interrupted run retains `.zst.partial` evidence. Recovery is offline, plan-only by default,
restricted to one exact existing campaign, and never deletes a partial that fails zstd
verification:

```bash
python3 tools/passive_drive_capture.py \
  --out-root /mnt/EXFAT512/obd-things/tmp/captures/ccan/drive-correlation \
  --require-mount /mnt/EXFAT512 \
  --campaign EXACT_EXISTING_CAMPAIGN \
  --recover-partials --execute --confirm-recovery
```

The singleton supervisor intentionally supports one complete, visible parameter dialog at a time.
It is sufficient for the eight-row cluster **System status** page and the initial singleton proof.
The owner-priority PCM scalars are on a different surface, **Plots → Select gauges to scan**, whose
Device-190 catalog contains 193 rows.

`alfaobd_plots_catalog.py` is the separate catalog-first guard for that surface. It validates the
exact Plots page, the Pentastar/Hemi PCM connection banner, and stopped red-triangle state. The
generic `Device model not determined` banner is deliberately rejected because it was also observed
on the incompatible Climate profile. The tool opens only the gauge selector, seeks both list
boundaries, inventories it forward and backward with bounded overlapping swipes, requires two
matching parsed dialog states after every swipe, hashes the exact rendered Unicode label order, and exits
with Android BACK. It never taps a gauge row, the dialog's OK button, the scan toggle, a
vehicle/module selector, or Active Diagnostics. The first successful live
result was reviewed and its hash is now pinned in the tracked discovery plan.

The plan-only check is inert:

```bash
python3 tools/alfaobd_plots_catalog.py plan \
  projects/ecu_mapping/configs/alfaobd_pcm_plots_catalog.json
```

For the first live catalog pass, park the van, connect AlfaOBD to the PCM profile, open the Plots
page, and leave the red play triangle visible. `audit` sends no input:

```bash
python3 tools/alfaobd_plots_catalog.py audit \
  projects/ecu_mapping/configs/alfaobd_pcm_plots_catalog.json
```

Only after that audit succeeds, use a new exact run ID:

```bash
RUN_ID='pcm-plots-catalog-YYYYMMDD-HHMMSS'
python3 tools/alfaobd_plots_catalog.py inventory \
  projects/ecu_mapping/configs/alfaobd_pcm_plots_catalog.json \
  --campaign-id "$RUN_ID" \
  --execute --confirm-read-only-navigation --confirm-parked \
  --confirm-scan-stopped \
  --conditions "parked; PCM connected; Plots page; red play triangle visible"
```

Machine evidence goes under `tmp/ecu_mapping/alfaobd_plots_catalog/$RUN_ID/`. A count, boundary,
required-label, traversal, UI-stability, or optional pinned-hash mismatch fails closed and preserves
evidence; it does not repair live strings from the SQLite prior. This inventory tool does **not**
select or scan a scalar.

`tools/alfaobd_plots_scalar_campaign.py`, with draft/review plans at
`projects/ecu_mapping/configs/alfaobd_{pcm,tcm}_plots_scalars.json`, is currently an
**offline-gated scaffold**, not enabled live scalar automation. `plan` validates and expands the
candidate schedule, `audit` checks the referenced catalog plan and any configured reviewed report,
and `status` only reads an existing `state.json`; none of those modes constructs an ADB client or
accesses CAN, services, mounts, network, proxy settings, or output. The live
PCM catalog and review fields are pinned. The TCM plan deliberately remains
unpinned until the live 56-row selector inventory completes:

```bash
python3 tools/alfaobd_plots_scalar_campaign.py plan \
  projects/ecu_mapping/configs/alfaobd_pcm_plots_scalars.json

python3 tools/alfaobd_plots_scalar_campaign.py audit \
  projects/ecu_mapping/configs/alfaobd_pcm_plots_scalars.json

python3 tools/alfaobd_plots_scalar_campaign.py plan \
  projects/ecu_mapping/configs/alfaobd_tcm_plots_scalars.json
```

The scalar gate requires a reviewed live catalog before it can even pass its offline readiness
check: a non-null catalog hash, the exact reviewed `catalog.json` and its hash, the sibling
completion `state.json` and its hash, the catalog-plan source hash, the scalar-plan review hash,
review provenance, and an exact
`(display_order_key, zero_based_index, label)` triple for every scheduled target. Even if all pins
and CLI confirmations pass, `run` is intentionally disabled in this version and exits before ADB,
CAN, service, mount, or output access.

The internal selector mutation primitive is now implemented and covered by synthetic safety tests,
but is not reachable from that CLI. It consumes only a fresh complete `CatalogInventory` whose
ordered-label hash matches the pinned catalog, verifies every visible page as an exact contiguous
catalog slice, removes prior checked rows, selects one exact target triple, rejects stale
coordinates or collateral check-state changes, commits through the sole verified `OK` control, and
proves that the returned Plots page contains only the target while the scan remains stopped. A
separate pure post-stop validator requires every configured artifact to remain present and
non-shrinking, requires Debug/CSV growth from their pre-start offsets, and requires the final
configured number of CSV-size observations to be identical. The internal scan-segment primitive
now also verifies the selected/stopped Plots page, starts the scan, requires a live Debug/CSV
activity witness, performs the bounded dwell, stops cleanly, applies the buffered-artifact gate,
and reconciles an ambiguous start or stop return without retrying the input. Its synthetic tests
prove both ambiguous-return paths leave scanning stopped. The remaining implementation work is the
outer campaign supervisor: UI/channel locks, same-boot operation inhibit, mount identity,
passive-capture binding, artifact pulls/hashes, and atomic checkpoints. The CLI must stay disabled
until that layer exists and the TCM catalog below is inventoried, reviewed, and pinned.

Offline static analysis has recovered the ZF9HP request table without
relaxing that live-UI gate. DEX consumer tracing first rejected a coincidental
56-entry HVAC table, then proved that the `ZF9HP` branch loads `aa.A` into
AlfaOBD's runtime request table. It aligns the priority rows with candidate
DIDs `F40C`, `0500`, `2102`, `2103`, `F405`, `0301`, `04FE`, and
`1018/101A/101B/101D/101F/1020`; five independently live-verified PCM table
anchors validate the ordered-table method, while the branch trace prevents
length-only misidentification. See
[`2026-07-21_alfaobd_apk_catalog.md`](findings/promaster_2022/2026-07-21_alfaobd_apk_catalog.md#static-request-table-recovery--2026-07-27).
The profile-specific `n0.z1.r2()` decoder is now recovered too:
`F405/0301/04FE` use `u8 - 40 °C`, the speed and torque rows have exact
big-endian signedness/scales, and
[`zf9hp.py`](zf9hp.py) preserves the complete 56-output native-unit catalog.
Physical support reads and grouped live-catalog captures of the priority set
are complete. The resulting exact DID associations and independent-drive
carrier tests are recorded in the dated PCM/TCM findings; do not repeat the
support inventory merely to rediscover them. Passive transmission-oil
temperature subsequently passed the separately predeclared hot-soak
counterexample after the cold-start challenge could not distinguish it from
chip temperature. `0x1F7` signed byte 3 is now a receive-only telemetry source
using `°C = 0.375 × raw + 57`; no temperature-warning threshold is implied.

`tools/did_sweep.py` accepts repeatable `--did` options, which was used to
read exactly the thirteen reviewed ZF9HP candidates without traversing the
large gaps between them. It remains available for reproducibility, is dry-run
by default, requires the normal parked/live gates for `--execute`, and sends
no diagnostic session change unless one is separately specified and
confirmed. A saved checkpointed JSONL can be decoded without touching the
vehicle:

```bash
python3 tools/zf9hp_results.py \
  tmp/inventories/tcm/dids_<stamp>.results.jsonl
```

The default report is `tmp/ecu_mapping/zf9hp_support_decode.json`. It preserves
AlfaOBD's native values and also supplies display values in °F, lb-ft, mph, and
psi where applicable. A positive response establishes installed-TCM support;
the report deliberately retains the separate physical-plausibility gate before
any decoded signal is allowlisted for the dashboard.

Catalog campaign `pcm-plots-catalog-20260726T224830Z` completed a matching
forward/reverse 193-row traversal without manual reconciliation. The separate
simultaneous eleven-gauge idle recording then preserved the owner's existing
Plots list and produced the mappings in
[`2026-07-26_pcm_plots_idle_mapping.md`](findings/promaster_2022/2026-07-26_pcm_plots_idle_mapping.md).
The follow-on loaded-drive recording produced
[`2026-07-27_pcm_plots_loaded_drive_mapping.md`](findings/promaster_2022/2026-07-27_pcm_plots_loaded_drive_mapping.md)
and crossed both the positive-load and negative-overrun torque regions. These efficient
multi-gauge results supersede the need to run the scalar supervisor for the rows they resolved.
Keep the disabled one-at-a-time scaffold for future ambiguity resolution and for gauges that
cannot be separated by fixed polling order.

`alfaobd_singleton_join.py` is the strictly offline verifier for a completed singleton campaign.
It validates campaign state and artifact hashes, requires successful zero-drop passive-recorder
coverage across every segment, merges explicitly hash-pinned overlapping recorder runs, and refuses
to overwrite a prior report. On this tablet and campaign, AlfaOBD's Android writers flushed Debug
and Info in 8 KiB blocks, so sampled file sizes are activity/provenance witnesses rather than
logical segment boundaries. The joiner instead verifies whole-envelope Info label-run order and
independent Debug send/receive streams, then resolves each singleton from its host-timed passive-wire
interval. Every wire segment must contain one distinct `22 DID` request payload, repeated with
strictly alternating exact-positive `62` responses and no competing non-TesterPresent traffic;
repeated anchors must resolve identically.

For the completed run, `capture-set.json` binds the two overlapping recorder directories and their
`run.json`, checkpoint, and `manifest.jsonl` hashes:

```bash
python3 tools/alfaobd_singleton_join.py \
  /mnt/EXFAT512/obd-things/tmp/ecu_mapping/alfaobd-drive/cluster-shakedown-20260724-005100 \
  --capture-set /mnt/EXFAT512/obd-things/tmp/ecu_mapping/alfaobd-drive/cluster-shakedown-20260724-005100/capture-set.json \
  --output /mnt/EXFAT512/obd-things/tmp/sweeps/cluster-shakedown-20260724-005100-singleton-join.json
```

For a new ordinary-driving run covered by one complete passive recorder, join
the two directories using the exact shared run ID:

```bash
RUN_ID='your-exact-run-id'
python3 tools/alfaobd_singleton_join.py \
  "/mnt/EXFAT512/obd-things/tmp/ecu_mapping/alfaobd-drive/${RUN_ID}" \
  "/mnt/EXFAT512/obd-things/tmp/captures/ccan/drive-correlation/${RUN_ID}" \
  --output "/mnt/EXFAT512/obd-things/tmp/sweeps/${RUN_ID}-singleton-join.json"
```

The positional order is Alfa campaign directory first, passive-capture
directory second. Use `--capture-set` only when multiple hash-pinned recorder
runs jointly provide continuous coverage.

`alfaobd_singleton_infer.py` is the next strictly offline step for a schema-2
join report. It searches the preserved raw-response and rendered-value
distributions for bounded affine candidates, uses exact interior Debug order
only as lag corroboration, consolidates repeated numeric anchors, and can
recover only the observed portion of an enum. It opens no ADB, CAN, network,
service, or subprocess resource:

```bash
python3 tools/alfaobd_singleton_infer.py \
  "/mnt/EXFAT512/obd-things/tmp/sweeps/${RUN_ID}-singleton-join.json" \
  --output "/mnt/EXFAT512/obd-things/tmp/sweeps/${RUN_ID}-singleton-inference.json" \
  --kind 'Actual Gear=enum'
```

The inference report refuses to overwrite an existing result and validates the
inference-critical schema, addressing, label/DID echoes, distributions, counts,
and Debug/wire corroboration from the hash-verified join step. Contradictory
anchors and insufficient variation remain unidentifiable. Observationally
equivalent signed/unsigned or endian interpretations remain enumerated on a
selected candidate rather than being silently collapsed. Every selected
formula or enum row is explicitly `candidate_only`, with physical verification
and telemetry promotion disabled. The explicit gear override is required
because Alfa may render a mixed categorical set such as `P`, `1`, `2`, and
`D`; numeric-looking gear labels are not a physical scalar. The absent Android
timestamps mean even a very clean affine fit establishes AlfaOBD's observed
rendering candidate, not independent physical truth.

The join output remains `candidate_only`: exact scheduled Info label-run order plus independent
Debug/wire payload corroboration establishes a strong label-to-DID association but cannot by itself
prove a non-observed scale or enum. The completed schema-2 report resolved all seven scheduled
segments, passed both repeated-anchor checks, and has SHA-256
`8f7e198ea2a9fedf55a64b4d1c44e970eadb48d69136b3a2d11acb058c21f1e1`.

### Current role-aware parked cluster viewer

The follow-up direct comparison proved the five mapped DIDs are compatible with both explicit
default session `01` and extended session `03`; a session-unchanged pass also succeeded. The
resulting parked viewer defaults to physical `22` reads and sends no DiagnosticSessionControl or
TesterPresent. Repeated `22` traffic can refresh S3 and may therefore prolong an inherited session;
the viewer does not claim that inherited state is default or that it forces an S3 timeout. An
explicit `--session 03` override is available only behind the normal session-change confirmations.
It is dry-run-first and bounded. Live mode resolves C-CAN by USB identity,
holds exclusive role/channel ownership, arms only for the viewer, and restores
the exact passive baseline. It is not a drive logger:

```bash
python3 projects/ecu_mapping/cluster_live.py

python3 projects/ecu_mapping/cluster_live.py \
  --execute --confirm-parked --confirm-engine-off --pair 6/14 \
  --conditions "parked, ignition ON, engine OFF"
```

Its RPM, speed, gear, and temperature rows remain raw/candidate displays. Battery alone uses the
qualified Alfa `raw x 0.1 V` rendering; the raw bytes remain visible for every row.

## Historical bounded cluster-drive campaign

The completed single-PCAN `cluster_drive_log.py` campaign is preserved only as
evidence; its executable was deleted during the permanent-role migration rather
than carrying forward a moving-vehicle/manual-arm path. Do not reconstruct its
fixed-channel command from git history.

The 2026-07-25 parked/idling shakedown
`cluster-drive-shakedown-20260726T050955Z` produced 3,114/3,114 exact
positives, two verified raw chunks containing 1,960,920 frames, zero drops,
exact wire/high-level counts, and verified passive restoration. The campaign
layout and acceptance evidence remain documented in the
[idling logger finding](findings/promaster_2022/2026-07-25_cluster_idle_logger_shakedown.md);
the later packed-field conclusions below remain valid historical results.

### Current packed-field mapping doctrine

The first three phases are current doctrine, with the first two implemented as
the default operational path:

1. **Establish evidence and timing.** Research the exact ECU/tool context, bind
   the label and physical scale to its ECU-scoped DID, and use the exact
   kernel-timestamped diagnostic response as the reference (including PCAN
   captures where that was the historical observer). Alfa CSV is
   label/scale evidence, not a sample-held timebase.
2. **Discover coarsely, then refine narrowly.** Run the stable coarse field
   profile over the bus, shortlist no more than two exact stream identities,
   then enumerate arbitrary DBC/cantools bit geometry only on those IDs.
   Validate a recovered field on complete independent drive legs; never split
   adjacent samples from one leg into train and holdout sets.
3. **Resolve semantic ambiguity by regime.** For unresolved actual torque,
   compare DID `1018` with `101A/101B/101F`, RPM, converter slip, shaft speeds,
   throttle, and the relevant passive fields across idle, pull, cruise, lift,
   and negative overrun. A high one-field fit is not semantic identity.

Reference/OCR infrastructure and automated DBC/template export are explicitly
out of scope. Do not add Spearman, sentinel inference, calibration machinery,
or time-series null tests until a named whole-leg benchmark failure shows that
the simpler field search and affine score are insufficient.

Transmission-temperature discrimination is the first Phase-2 application.
The tracked whole-leg benchmark starts with the original TCM development leg,
the continuation validation leg, and the newer 72- and 45-minute blind legs.
It pairs `04FE` gearbox oil and `0301` TCU-chip temperature against `0x417`
bytes 2–3 on every leg rather than treating warm-up covariance with either
reference as identity. Exact extraction found no usable TCM exchanges in the
continuation leg, but recovered 24,166 exchanges from the 72-minute blind leg
and 17,870 from the 45-minute blind leg. The first blind comparison rejects
the former `0x417` gearbox-oil identity and instead retains only a
candidate-only chip-temperature association. The second leg independently
rejects both interpretations: its oil fit falls to R² 0.44824, while the chip
comparator reaches only R² 0.55372 at rank 27 with materially different affine
scaling. `0x417` remains an unresolved thermal/state field, not a temperature
telemetry source.

The same 72-minute blind leg independently recovered all three established TCM
controls: vehicle speed `0x101 u12be@0`, turbine speed `0x1F7 u16be@39`, and
target crankshaft torque `0x100 u11be@31` each passed the tracked rank,
coverage, and R² gates. The provenance-bound aggregate now passes seven
known-field cases across development, validation, and blind splits. This
confirms that the temperature rejection is not an analyzer failure.

The older torque motivation is superseded: `0x100` bytes 3–4 is TCM target
crankshaft torque from DID `101B`, and `0x0F0` is maximum engine torque
requested by the transmission from `101F`. Actual torque `1018`, converter
slip `0500`, and a passive gearbox-oil temperature remain unresolved. The
frozen regime analysis now covers all five operating states on 2,014 blind
`1018` samples and rejects the tested `0x100 u13be@4`, `0x1F4`, and `0x0FC`
fields as universal actual-torque identities. The paired `101A`
torque-without-TCU-requests pass also rejects `0x100 u13be@4` as that stage.
A later production-drive search found the distinct adjacent `0x100 u13be@9`
field as a replicated, candidate-only current-torque proxy; see the
[`2026-08-04 broker-drive validation`](findings/promaster_2022/2026-08-04_broker_drive_poll_validation.md).
Measured horsepower remains intentionally unavailable pending a reviewed
promotion decision.

`tools/can_timeseries_correlate.py` performs the offline broadcast-candidate
search against one exact-positive module DID. It streams saved plain or zstd
candump chunks, verifies every selected reference against its exact global
raw-frame sequence/timestamp/ID/payload, and excludes 29-bit and standard OBD
diagnostic IDs by default so the diagnostic response cannot become a trivial
perfect match. A candidate stream is keyed by channel, SFF/EFF namespace, CAN
ID, and DLC; reports retain source path/hash provenance and make the symmetric
match radius an explicit maximum-staleness gate.

The default candidate views remain bytes, overlapping 16-bit integers, aligned
32-bit integers, and the two established Stellantis packed 13-/17-bit forms.
For an explicitly shortlisted `sff:` or `eff:` ID, `--bit-search-id` replaces
that coarse profile with bounded 1–32-bit Intel/Motorola DBC geometry. No more
than two IDs and 6,000 fields per ID are accepted. It requires
at least 50 percent reference coverage and four distinct candidate values by
default, then ranks by `R² × coverage` before the remaining deterministic
tie-breakers. A predeclared candidate can additionally be scored without
refitting by supplying `--fixed-formula-field`, `--fixed-formula-scale`, and
`--fixed-formula-intercept`; its report includes exact coverage, signed bias,
MAE, RMSE, and conservative nearest-rank p95 absolute error while remaining
explicitly candidate-only. The portable direct invocation for the completed
idling shakedown is:

```bash
python3 tools/can_timeseries_correlate.py \
  --wire tmp/captures/ccan/cluster-drive/cluster-drive-shakedown-20260726T050955Z/cluster_wire.jsonl \
  --did 1000 \
  --reference-field u16be:0 \
  --match nearest \
  --radius-ms 100 \
  --minimum-samples 20 \
  --top 100 \
  --output tmp/sweeps/cluster-drive-shakedown-20260726T050955Z-did1000-broadcast.json \
  tmp/captures/ccan/cluster-drive/cluster-drive-shakedown-20260726T050955Z/chunk_000000_full.candump.zst \
  tmp/captures/ccan/cluster-drive/cluster-drive-shakedown-20260726T050955Z/chunk_000001_full.candump.zst
```

On vanpi, submit that heavy saved-log analysis through the fixed repository
task instead of running it on the Pi:

```bash
python3 /home/pi/van_compute/scripts/pi_compute.py run \
  can-timeseries-correlate-two-chunks \
  --source-root /home/pi/dev/obd-things \
  --input /home/pi/dev/obd-things/tmp/captures/ccan/cluster-drive/cluster-drive-shakedown-20260726T050955Z/cluster_wire.jsonl \
  --input /home/pi/dev/obd-things/tmp/captures/ccan/cluster-drive/cluster-drive-shakedown-20260726T050955Z/chunk_000000_full.candump.zst \
  --input /home/pi/dev/obd-things/tmp/captures/ccan/cluster-drive/cluster-drive-shakedown-20260726T050955Z/chunk_000001_full.candump.zst \
  --wait --stdout
```

The task accepts exactly those three input roles and fixes DID `1000`, field
`u16be:0`, the matching thresholds, and its declared `report.json` output; it
does not accept caller-supplied arguments. The first successful report found
`0x0FC` bytes 0–1 as the full-coverage unit-slope raw candidate. See the
[`2026-07-26 broadcast correlation finding`](findings/promaster_2022/2026-07-26_cluster_did1000_broadcast_correlation.md).

For an already shortlisted packed field, specify exact namespace and geometry
instead of expanding every frame. This example is illustrative and should
still be submitted through the matching named compute task for a full log:

```bash
python3 tools/can_timeseries_correlate.py \
  --module tcm \
  --wire tmp/ecu_mapping/tcm-drive-analysis/tcm_wire.jsonl \
  --did 101B --reference-field u16be:0 \
  --bit-search-id sff:100:8 \
  --bit-search-length 11 \
  --bit-search-byte-order big \
  --bit-search-signedness unsigned \
  --radius-ms 100 --minimum-samples 20 --top 100 \
  --output tmp/sweeps/tcm-101b-bit-refinement.json \
  tmp/ecu_mapping/compute-inputs/pcm-plots-drive-20260728T002525Z/chunk_000000_full.candump.zst \
  tmp/ecu_mapping/compute-inputs/pcm-plots-drive-20260728T002525Z/chunk_000001_full.candump.zst \
  tmp/ecu_mapping/compute-inputs/pcm-plots-drive-20260728T002525Z/chunk_000002_full.candump.zst \
  tmp/ecu_mapping/compute-inputs/pcm-plots-drive-20260728T002525Z/chunk_000003_full.candump.zst
```

Do not run that full search on vanpi. Use the existing
`can-timeseries-correlate-tcm-four-chunks` compute task and pass the DID,
reference, and bit-search options through its arguments.

Phase 3 is an opt-in slice of the same correlator, not a second discovery
engine. `--regime-analysis` classifies each exact DID response as idle,
positive pull, steady cruise, lift transition, negative overrun, or
unclassified by three explicitly supplied passive fields. Speed and throttle
changes are rates between consecutive exact diagnostic timestamps; the tool
does not sample-hold Alfa CSV values. It runs per-regime regressions only for
at most four exact shortlisted streams and retains all candidate-only gates.
Configuration validation projects the field count across all five regimes and
rejects a selection that could exceed the 50,000-regression state cap before
any capture is opened.

Thresholds are raw-field units and must be selected on the development leg,
recorded, then frozen before validation/blind legs. This development example
uses the established `0x101` and `0x0FC` geometries plus the loaded-drive
`0x41B` throttle lead; its numeric thresholds remain experimental classifier
choices, not vehicle calibrations:

```bash
--regime-analysis \
--regime-speed-field sff:101:8=bits:big:0:12:unsigned \
--regime-rpm-field sff:0FC:8=bits:big:7:14:unsigned \
--regime-throttle-field sff:41B:8=u16be:4 \
--regime-candidate-id sff:0FC:8 \
--regime-candidate-id sff:1F4:8 \
--regime-candidate-id sff:100:8 \
--regime-stopped-speed-max 0 \
--regime-moving-speed-min 80 \
--regime-idle-rpm-min 500 \
--regime-pull-speed-rate-min 8 \
--regime-pull-throttle-min 200 \
--regime-steady-speed-rate-max 4 \
--regime-steady-throttle-rate-max 20 \
--regime-lift-throttle-rate-max -50 \
--regime-overrun-speed-rate-max 0 \
--regime-overrun-throttle-max 100 \
--regime-minimum-samples 5
```

The classifier applies idle first, then lift, positive pull, negative
overrun, and steady cruise. The report preserves classification counts,
thresholds, exact stream geometry, and a separate ranking for each named
regime. A regime-dependent fit may rule out semantic equivalence; it still
cannot prove that a passive field is actual crankshaft torque. The first
implementation and development-leg results are recorded in the
[`2026-07-28 packed-field benchmark finding`](findings/promaster_2022/2026-07-28_signal_field_engine_benchmark.md).

`tools/can_signal_benchmark.py` validates the tracked whole-leg benchmark
manifest without opening raw logs:

```bash
python3 tools/can_signal_benchmark.py plan \
  projects/ecu_mapping/configs/signal_field_benchmark_v1.json
```

It evaluates reports produced by named compute tasks, requires the supplied
report bytes to match the compute manifest's `report.json` SHA-256, and checks
exact wire linkage plus channel/SFF-EFF/ID/DLC/provenance/staleness fields.
Split independence is enforced by artifact ID, normalized path hint, and
pinned digest so aliases cannot leak one capture into development and
holdout. A negative control with no eligible candidates is an explicit
empty-set pass; a null maximum alongside reported candidates is invalid. The
tool writes only candidate-only aggregate results below `tmp/` and never runs
the heavy correlation itself.

The manifest currently contains 24 positive, negative, carrier-only, proxy,
and pending cases, including paired `04FE`/`0301` temperature challenges and
the coarse `1018` Phase-3 gate. Existing fixed tasks still cover the original
PCM/TCM two-/four-chunk legs. Owner-approved bounded variadic tasks now accept
one exact wire stream plus 1–16 chronological capture chunks, which covers the
new blind legs without a task per capture count. The cluster two-chunk task
still fixes its original coarse DID-1000 search and cannot express the packed
RPM benchmark.

This tool performs no CAN, ADB, service, or network access. Its ranked rows are deliberately marked
`candidate_only`, `physical_identity_verified: false`, `scale_verified: false`, and
`telemetry_promotion_allowed: false`. A high correlation means only that a saved broadcast field
tracked the selected raw DID during that experiment; it does not establish identity, units,
physical scaling, safety thresholds, or causality. The low-excitation idling trace is a lead
generator, not promotion evidence; confirm candidates with independent driving variation and
ground truth. The report exact-links raw frames but deliberately states that it does not itself
validate the chunk manifest, socket-drop accounting, or final campaign summary; retain and review
the completed `summary.json` alongside it.

This mechanical fail-safe no longer means every useful field must immediately
pass verified-decode gates. The repository-wide
[`CAN evidence-tier policy`](../../docs/can-evidence-tiers.md) distinguishes:

- `exploratory_candidate` for fast discovery from one representative capture;
- `operational_proxy` for a frozen field/formula that passes explicit
  non-critical whole-leg error tolerances; and
- `verified_decode` for independently established identity and scale eligible
  for canonical-map and ordinary telemetry promotion.

`can_timeseries_correlate.py` records the exploratory or proxy-evaluation tier
while preserving all candidate-only promotion flags.
`can_signal_benchmark.py` accepts an `operational_proxy` expectation with a
declared `trend`, `state_detection`, or `approximate_display` use, error-unit
conversion, minimum coverage, maximum RMSE, maximum p95 absolute error, and an
optional bias limit. Passing permits only the declared proxy use; it does not
assert physical identity or enable verified telemetry promotion.

`reassemble_commands.py <decoded.txt> <out.txt> [atsh]` — rebuilds multi-frame COMMANDS.
AlfaOBD sends long requests as MANUAL ISO-TP frames: First Frame `1L LL <6 data>` + a trailing
ELM responses-hint digit (17 hex chars), ECU Flow Control `30 00 00`, Consecutive Frames
`2N <7 data>` + hint. This tool drops the hint digit, strips the PCI byte(s), concatenates,
truncates to the FF length, and pairs with the response — then interprets each command
(`2E`/`2F`/`31`/`27`/`10`/`14`). `extract_did_map` handles single-frame `22` *reads* and now
skips the manual-frame scraps; use this tool for the commands.

**VIN handling (publish-safe):** the real VIN is never hardcoded — scripts read it from the
`OBD_VIN` env var (`export OBD_VIN=3C6…`), and tracked outputs mask the unique serial
(`…######`) in every form (ASCII + hex), so committed files carry only the non-identifying
model descriptor. Raw logs under `tmp/` (gitignored) keep the full VIN.

## Data layout (per repo convention)

- **`tmp/ecu_mapping/`** (gitignored): `raw/` = copied `.bin`/`.log`; decoded `*.decoded.txt`.
  Raw CAN/log data is never git-tracked.
- **`findings/`** (tracked): *extrapolations* only — the derived maps.
  - `promaster_2022/module_did_map.txt` — historical 2022 trace inventory; use the dated live
    findings for current canonical module evidence
  - `promaster_2015_diesel/module_did_map.txt` — 2015 reference van (same family; candidate cross-ref)
  - `promaster_2022/command_log.txt` — reassembled + interpreted command sequences (2022 ProMaster)

## Findings so far (current 2022 ProMaster evidence)

Modules seen (ATSH → phys addr): radar `DA2AF1`/0x2A, **BCM `DA40F1`/0x40**, RFH `DAC7F1`/0xC7,
trans `DA18F1`/0x18, engine `DA10F1`/0x10 + `7E0`, shifter `DA1FF1`/0x1F.

Direct live discovery on 2026-07-19 independently verified C-CAN endpoints `0x18`, `0x1F`,
`0x2A`, `0x40`, `0x60`, `0xC6`, and `0xC7`. A fixed-DLC-8 legacy-session probe on 2026-07-21
then independently verified PCM `0x10` while parked with the engine idling. A 2026-07-22 AlfaOBD
follow-up repeated the positive legacy session and live-data reads ignition-on/engine-off, proving
engine running is not required; ordinary default-session reads remain unsupported/unresolved. See
[`2026-07-19_live_ecu_discovery.md`](findings/promaster_2022/2026-07-19_live_ecu_discovery.md).
The companion [`ODX/PDX source research`](findings/promaster_2022/2026-07-19_odx_pdx_source_research.md)
records the free local toolchain, searched sources, and remaining acquisition paths.
The [`2026-07-21 read-only module inventory`](findings/promaster_2022/2026-07-21_readonly_module_inventory.md)
completes inherited-session `18DAxxF1` address coverage on the pins-6/14 C-CAN branch and records bounded DTC/result-only routine
responses for all seven default-session C-CAN modules in that campaign. It found no additional
address responder on that branch; DTC state and routine-response leads are kept per module there.
The follow-on [`candidate DID inventory`](findings/promaster_2022/2026-07-21_candidate_did_inventory.md)
records complete `F100-F1FF` pages for TCM, shifter, BCM, cluster, and telematics plus a direct
recheck of 61 current-van AlfaOBD BCM candidates. It established 135 positive identity-page
responses and reverified 59 BCM candidates. A controlled follow-up proved BCM `40A3`/`40A6` are
session-gated: both returned `7F 22 31` in the inherited state and positive data after validated
`10 03 -> 50 03 00 32 01 F4` under otherwise unchanged conditions. The completed session-03
`4000-40FF` page then found only five positives: default-visible `40A1`, `40A2`, and `40AA`, plus
session-gated `40A3` and `40A6`. No other hidden DID appeared in that page.
The subsequent four-page BCM pass completed 1,024/1,024 reads and found one additional positive,
`2023`, whose complete 250-byte readback matches the later captured AlfaOBD PROXI/configuration write
payload at every unredacted byte. It also preserved four condition-gated DIDs
and the first controlled key-cycle evidence for dynamic BCM values.
The [`related-platform passive bus leads`](findings/promaster_2022/2026-07-19_related_platform_bus_leads.md)
record a 50-kbit/s/29-bit 2020 Citroën Jumper cabin-bus hypothesis. It is now explicitly superseded
for this van's DLC 3/11 branch: the labeled B-CAN pigtail and passive captures live-verified that pair
at 125 kbit/s on 2026-07-20. See the
[`B-CAN pair verification`](findings/promaster_2022/2026-07-20_bcan_pair_verification.md).
That analysis also rejects the old high 11-bit candidates as fixed-rate application broadcasts;
the follow-on [`B-CAN live ECU discovery`](findings/promaster_2022/2026-07-21_bcan_live_ecu_discovery.md)
then verified four independent 29-bit endpoints on pins 3/11: ICS `0x85`, Uconnect `0x87`,
Climate `0x98`, and EMCM2 `0xD9`. It records identity, non-clearing DTC, result-only routine, and
complete inherited-session `F100-F1FF` inventories for each. Trailer `0x4A`, blind-spot `0x62/65`,
and display `0x6A` timed out to both F1A5 and F187 and remain unresolved/possibly optional.
The follow-on [`live AlfaOBD status correlation`](findings/promaster_2022/2026-07-21_alfaobd_live_status_correlation.md)
adds exact common environmental scalars and bounded status-group candidates for ICS, Uconnect, and
EMCM2 while a listen-only PCAN observed the B-CAN branch. It also rejects the selected Climate
profile's eight gauge labels/scales for this unmatched ECU variant and records stronger, but still
non-conclusive, timeout evidence for the four optional profiles. Its controlled EMCM2 follow-up
maps `2A00` to independent left/right rotary bytes and `2A01` to the discrete Mute and Screen-button
states. Right-knob counterclockwise remains unresolved, and an OEM-described knob-press behavior
conflicts with the installed controls' observed feel; do not force either knob.
The companion [`C-CAN AlfaOBD live correlation`](findings/promaster_2022/2026-07-22_ccan_alfaobd_live_correlation.md)
ties the installed cluster, TBM2, shifter, TCM, BCM, and PCM identities to their runtime profiles
and recovers bounded raw polling loops for cluster, TCM, and PCM. Controlled driver-door and brake
changes map BCM `0130/0152` and `0132/0150` groups plus passive C-CAN candidates led by door frame
`0x4B1` and brake frames `0x1FA`, `0x0FA`, and `0x10F`. It also documents that the Gauges CSV did
not grow and the ZF `.dat` update merely duplicated cached series, so those files are not fresh
labeled evidence.
The guarded [`cluster singleton correlation`](findings/promaster_2022/2026-07-24_cluster_singleton_correlation.md)
then independently discriminates cluster `1000/1002/0107/1004/1005` as the Engine-speed,
Vehicle-speed, Actual-Gear, Battery-voltage, and Outside-temperature associations. Repeated Engine
and Battery anchors agree, and Battery raw values `0x76-0x79` support AlfaOBD's `raw x 0.1 V`
rendering. An Alfa-closed direct comparison then returned byte-identical DID results after exact
`10 01` and `10 03` positive echoes, establishing that default session suffices and extended
session adds no access for this set. A separate session-unchanged pass also succeeded without `10`
or `3E`, although its inherited session was not positively identified and the `22` reads may have
refreshed its S3 timer. RPM/speed scales, non-P gear values, and the temperature formula remain
bounded follow-ups.
The [`2026-07-19 passive drive analysis`](findings/promaster_2022/2026-07-19_ccan_drive_signal_analysis.md)
corrects CAN ID `0x101` from the old odometer hypothesis to a packed instantaneous-speed field,
corroborated by `0x0EE`; the exact `/16`-versus-`/32` km/h scale still needs one known-speed reference.
The later
[`cluster DID 1000 broadcast correlation`](findings/promaster_2022/2026-07-26_cluster_did1000_broadcast_correlation.md)
exact-linked all 623 idling Engine-speed-associated DID samples to the raw
capture and ranked `0x0FC` bytes 0–1 (`u16be`) first: 100 percent coverage,
R² 0.9998896, unit-slope affine fit, and 6.42 raw-count RMSE. This is a strong
passive raw engine-speed candidate, but the low-excitation idle trace does not
verify the `/4` rpm scale or authorize public telemetry promotion.

- **Radar (0x2A)** confirms the radar project's story: `31 01 0250` → `7F3131` (wrong RID),
  alignment-gauge DIDs (`083E/083F/0846/0830/0860`) → `7F2231` "not supported". DID `0850`
  returns real bytes (`FF ED 44 D4 FF FF 7E 86`) — decode target. See `../radar/`.
- **BCM (0x40)** — real commands (from the reassembled log): `2F` IO-control actuations that
  **succeeded** (`2F5115/5118/5120/5040/5041/5050` → `6F..03`/`6F..00` return-control), each run
  as `ctrl=03` (shortTermAdjustment) `opt=01`/`02` then `ctrl=00` (release); routine `31 01 0200`
  → `7F..22` conditionsNotCorrect (power-mode gated); two large, positively acknowledged
  **PROXI config writes** (`2E 2023`, 250-byte payloads, each `7F 2E 78` then `6E 20 23`);
  `10 03` session, `14` ClearDTC. **Correction:** the
  `27`/`2A`/`2B` "commands" an earlier pass reported were **not** SecurityAccess — they were
  Consecutive Frames of the `2E 2023` write (nibble-2 PCI). **No `27` in this session.** With the
  SGW bypassed (`[[sgw-bypass-always]]`) the successful `2F` actuations are the remote-unlock
  lead; next is identifying *which* `2F` DID drives the door lock (correlate with what was
  actuated in AlfaOBD) and verifying on 2022 ProMaster via the tap before replaying.
  The installed AlfaOBD 2.4.4.0 APK has now been copied with the owner's authorization and its split
  SQLite catalog reconstructed offline. The model-code-88 catalog matches the app's `RAM PRO MASTER
  (VF) 2022+` selection, includes the correct BCM profile, and exposes a 67-entry action menu with
  front/rear door-lock relay labels. It still does not directly associate those menu labels with the
  six captured `2F` DIDs, so a fresh, one-action-at-a-time AlfaOBD session with
  the maintained role-aware passive C-CAN recorder in parallel remains the next
  evidence-producing step for unlock labels; do not guess them from menu order
  or command timing. See
  [`2026-07-21_alfaobd_apk_catalog.md`](findings/promaster_2022/2026-07-21_alfaobd_apk_catalog.md).
  **Application-message update (2026-08-26):** a separate passive three-bus
  campaign mapped the fob-result C-CAN `0x1EF` action bodies for lock-all,
  front-unlock, and rear/cargo-unlock, plus the `0..15` counter and
  CRC-8/SAE-J1850. Front unlock was independently repeated with a different
  counter/checksum. This path avoids the parked UDS power-mode gate, but replay
  was then live-verified: one separately authorized counter-current,
  CRC-correct front-unlock frame unlocked the front doors without a fob press,
  and exact passive restoration completed with zero errors/inhibits. The
  purpose-built composite remains front-unlock-only and plan-only by default.
  See
  [`2026-08-26_rfh_lock_unlock_can.md`](findings/promaster_2022/2026-08-26_rfh_lock_unlock_can.md).
- **RFH (0xC7)** full ID block + TPMS; pair with labeled `RFH_FGA_Info.log` (current faults
  `U0001/B1040/C1502-FR/C1501-FL`) for the TPMS project. See `../tpms/`.
- **PCM (0x10)** is independently live-verified at `18DA10F1 -> 18DAF110`: fixed-DLC-8 padded
  `10 92 -> 50 92`, then `1A 87 -> 5A 87 ... 68532157AI`. The successful run was parked with the
  engine idling; the later AlfaOBD pass repeated `50 92` and positive live reads with ignition on
  and the engine off. Engine running is not required. Fixed-DLC-8 padding remains part of the
  known-good direct recipe; use the specialized legacy probe until default-session/DID behavior is
  mapped. `tools/did_sweep.py` now supports this exact legacy case with
  `--session 92`: only the `pcm` registry key is accepted, the ISO-TP socket
  uses zero padding, the exact `50 92` echo is mandatory, and the tool does
  not inject an unverified `3E 00`.

## Next steps

1. **PCM and BCM session follow-up completed:** the PCM endpoint is verified and the BCM session-03
   `4000-40FF` namespace is bounded. Do not repeat either scan without a new experimental question.
   The AlfaOBD engine-off success has eliminated engine-running state as a requirement; a direct
   unpadded repeat is not useful unless testing framing itself.
2. **Unlock:** the narrow C-CAN `0x1EF` front-unlock proof is complete. Keep
   `rke_front_unlock.py` plan-only by default and fixed to front-unlock; any
   production/dashboard integration needs a separate threat-model and owner
   decision, must retain fixed RF-Hub wake ownership, valid sequential
   counter/CRC synchronization, one-frame send, parked/ignition/engine/recovery
   gates, physical/B-CAN verification, and exact passive restoration. Do not
   expose caller-selected payloads or generalize the wake session into an
   arbitrary post-wake transmit API. The AlfaOBD `2F` work is now fallback-only.
   The production service is named **Vonstar**:
   `van-dashboard -> /run/vonstar/api.sock -> vonstar.service`. It exposes only
   `lock_all`, `unlock_front`, and `unlock_cargo`, plus one aggregate
   `POST /v1/access-state` read. The read uses at most one fixed C-CAN wake,
   one no-retry BCM `0130` request in that same session, and returns every
   supported lock/door field and the bounded raw observations. B-CAN `0x5E2`
   byte1 is now verified exact-vehicle front/cargo-domain feedback after three
   independent repeat cycles plus a failed-lock control; the former
   `0x46C`/`0x5B2` lock interpretations were withdrawn. Driver ajar is
   exposed as a one-trial candidate; other individual door lock/ajar fields
   remain explicit `null` until mapped. Two independently authorized
   silent-start aggregate reads have now reproduced three identical locked
   `0x5E2` samples, exact positive BCM `0130`, zero B-CAN/CAN-CH TX, and clean
   passive restoration. All
   operations have request-id idempotency, a three-second cooldown, no
   automatic retry, and an audit log. On 2026-08-27 `vonstar.service` was
   installed/enabled/started and the port-8788 dashboard was restarted with its
   private client and three confirmed controls. Deployment added zero CAN TX;
   both services stayed active with zero restarts, and all roles remained exact
   passive/error-free with no inhibit. Front unlock and aggregate state read are
   live-validated. Lock-all and cargo-unlock remain controlled-capture-mapped,
   not independently replay-validated; their exposed status metadata says so.
3. **BCM structural decode completed:** all 75 definitions are represented in the offline report;
   55 DIDs have positive trace evidence and 20 are negative. Continue with controlled scaling/name
   validation, not another live sweep of the same requests.
4. **B-CAN discovery, inventories, and broad AlfaOBD status observation completed:** do not repeat
   the same eight-target/F1xx campaign or broad status pass without a new state/session question.
   Next, use controlled one-variable refreshes of the already bounded ICS `027E/027F/0300` and
   Uconnect `180C/1820/1821` candidates. EMCM2 `2A00/2A01` control mapping is complete except for
   right-knob counterclockwise and the unresolved OEM-versus-installed knob-press discrepancy. The
   selected Climate profile failed live variant verification, so its gauge labels/scales are invalid
   here. Climate result-only RID `0201` remains an offline identification lead only; do not start/stop it.
5. **C-CAN cluster singleton, session, and logger shakedown proofs completed:** do not repeat the
   six-profile broad pass, parked five-signal shakedown, or default/session-03 comparison without
   a new question. The
   buffered-envelope join discriminates the five cluster DIDs and repeats both anchors without a wire
   mismatch. Direct PCAN reads then produced identical results after exact `10 01` and `10 03` echoes,
   proving default compatibility without requiring extended session. The bounded standalone viewer
   may leave the inherited session unchanged and fail closed rather than sending `10`/`3E`; do not
   label that inherited state as positively identified default. The retired
   `cluster_drive_log.py` completed its 12-minute parked/idling rotation shakedown with exact
   request/wire accounting, zero drops, and passive restoration. Its nonzero `1000` range is
   consistent with `raw / 4` RPM but remains unverified. That completed moving
   workflow was deleted during migration and must not be reconstructed; use a
   newly reviewed role-aware campaign only if a new scaling question warrants it.
   Preserve the validated singleton Status workflow for its bounded labels. The owner-priority PCM
   Plots catalog and simultaneous eleven-gauge idle mapping are complete: passive `0x41D` oil
   pressure and `0x2ED` coolant are telemetry sources. Loaded evidence now supports a packed
   `0x100` bytes0–1 torque-stage field, but its exact semantic stage remains unresolved and
   engine-oil temperature remains unmapped. The separate
   [`ZF 948TE loaded-drive mapping`](findings/promaster_2022/2026-07-27_tcm_plots_loaded_drive_mapping.md)
   completed the 12-gauge label/DID join and promoted passive vehicle speed,
   turbine speed, output-shaft speed, and explicitly labeled TCM target crank
   torque. It also resolved `0x101` to `/16 km/h` and `0x0EE` to `/128 km/h`.
   Two independent blind drives have now rejected `0x417` bytes2–3 as
   gearbox-oil temperature: its prior scale did not transfer, its fit to
   `04FE` collapsed, and the first blind leg's stronger `0301` covariance did
   not reproduce on the second. Retain `0x417` only as an unresolved
   thermal/state field with no verified temperature identity. Do not repeat
   the idle campaign merely to rediscover those DIDs. The separate scalar tool
   remains an offline pin-validation scaffold and
   its `run` path is intentionally disabled; use it only after implementing and reviewing a genuine
   one-at-a-time need. Also use
   passenger-door plus parking-brake discriminators to refine the passive and BCM candidates.
   Alfa's shifter `Drive` rendering in Park and TCM `Brake switch` watcher remain explicitly
   invalid. The selected PCM Plots catalog contains no true engine-sump
   oil-temperature row. Related Alfa profiles supplied `3159` (`u8 - 40 °C`)
   with adjacent sensor-voltage DID `315A`, plus the
   `B010/B011/B012` calculated/measured thermal family. The installed PCM
   entered the verified padded session `92` but returned NRC `12` for all five.
   The historical two-DID reproduction command is:

   ```bash
   python3 tools/did_sweep.py pcm \
     --did 3159 --did 315A \
     --session 92 --confirm-session-change \
     --pair 6/14 \
     --conditions "parked; ignition ON; engine OFF; related-profile EOT support check"
   ```

   This is a dry run unless `--execute --confirm-parked` is added. Do not run it
   again without a new experimental reason, and do not expand around the
   rejected addresses. The ZF9HP catalog inventory and loaded scalar campaigns
   are complete; do not rerun the broad 56-row inventory. The reviewed direct
   `tcm-thermal` logger completed a 52 °C cold-start challenge with AlfaOBD
   closed, exact `04FE/0301` polling, and a complete zero-drop C-CAN stream.
   `0x1F7` byte 3 passed the frozen carrier/affine gates but not the required
   0.10 R² advantage over chip temperature. The later hot-soak discriminator
   supplied the missing counterexample and passed all frozen scale/identity
   gates, so that byte is now the allowlisted passive transmission-oil source.
   Do not repeat either completed thermal trajectory or revive the old `0x417`
   scale. Converter slip and actual measured crank torque remain unresolved
   passive targets. The
   saved `ZF9HP.dat` remains
   unlabeled/untimestamped and is not a substitute for the fresh joined trace.
6. Once a DID/address/routine is *verified on 2022 ProMaster*, promote it into the canonical maps
   (`../../docs/bus-map.md`, `../../lib/modules.py`, project DID maps) per the maintenance rule.
