# Gear and oil-life offline follow-up — September 22, 2026

Initial September 22 scope: saved current-vehicle captures, the owner-supplied AlfaOBD APK, and
the exact-vehicle OEM service-document mirror. No live CAN access, service
changes, diagnostic requests, or telemetry promotion occurred in that offline
phase. Subsequent owner-run parked checks are documented below. The latest
successful check returned **82% oil life remaining** without sending a
session-change request and verified passive restoration. Production oil-life
integration is now implemented/tested and enabled: dashboard assets are
published and the owner activated the broker at 2026-10-03 01:47:39Z. No
automatic engine-running sample has been observed at this deployment checkpoint.

## Gear: a concrete passive candidate, not a qualified enum

The existing installed-TCM mappings in `docs/bus-map.md` provide a mechanical
reference within the same C-CAN standard-ID `0x1F7`, DLC-8 frame:

- output-shaft RPM = `(((b0 & 1) << 16) | (b1 << 8) | b2) / 32`;
- turbine/input RPM = `u16be(b4:b6) / 2`;
- engaged ratio = turbine RPM / output-shaft RPM, excluding low shaft speed
  and separately accounting for shifting/slip.

The OEM **948TE/9HP48 transmission gear ratios** page
`data_pages/article/63088/guid/na-cr22vf-GUID-725E2FF1-C21F-4AA5-B0BB-58065EE6082A_html.html`
gives forward ratios `4.713, 2.842, 1.909, 1.382, 1.000, 0.808, 0.699, 0.580,
0.479`; reverse `3.805`, final drive `4.083`. These are transmission ratios,
so do not multiply by final drive when comparing the two transmission shafts.
OEM search job: `20260922T223606Z-b00fa418`.

Existing task `can-event-window`, job `20260922T224631Z-76b23fe4`, extracted
200 exact frames across two seconds from finalized C-CAN
`broker-drive-20260922T042152731314/c-can/chunk_000000_full.candump.zst`.
The matching pair at recorded channel `can0` is:

| Kernel timestamp | Standard ID | DLC-8 data |
|---|---|---|
| 1790051093.019366 | `1F4` | `56 30 03 C0 7D 00 09 A5` |
| 1790051093.029381 | `1F7` | `01 82 A1 28 10 E4 01 5B` |

The shaft ratio is approximately `0.699`; `0x1F4` byte 4's upper nibble is
`7`. This agrees with the OEM seventh ratio and shortlists **Motorola start
bit 39, length 4**. One steady regime does not establish the full enum,
shift timing, stale/invalid states, or PRND. In particular, do not substitute
this numbered-gear candidate for the independently labeled cluster `0107`
field whose only established rendering remains `00 -> P`.

`tools/can_gear_ratio_analyze.py` prepares bounded per-capture shaft-ratio
histograms and all sixteen nibbles of the already-shortlisted `0x1F4`.
It requires the recorded channel, DLC 8, both shafts at least 200 RPM, and
a preceding candidate frame no older than 100 ms; it rejects socket-loss
markers and never assigns gear labels. Three tests passed in
`20260922T223509Z-8bc25c2f` using an isolated source snapshot.
The September 22 proposal was saved under
`tmp/ecu_mapping/gear-ratio-compute-task-proposal.json`. The owner approved
the offline run on October 2; its task was already registered in the newer
repository state and was reused without changing other task definitions.

### October 2 full-recording check: reject this direct gear enum

Task `can-gear-ratio-analyze`, job **`20261002T102715Z-2e91f024`**, completed
on the remote worker using the unchanged field and shaft formulas above.
It scanned **4,187,827 frames**, yielding **42,574** eligible, fresh pairs:
all two chunks of the September 17 recording plus the first ten-minute
chunk of the original September 21 local-time highway recording. This is
one complete independent recording and one development chunk, not two
complete drives. Both source manifests report full-stream completion and
zero detected socket drops. Eligible pairs use the recorded `can0`, standard
IDs, DLC 8, both shafts at least 200 RPM, and a preceding `1F4` no older
than 100 ms. Compressed input hashes agree with the recording manifests.

| Recording / full chunk | Eligible pairs | Byte 4 high nibble | Shaft-ratio evidence |
|---|---:|---|---|
| `20260917T190516494149` / 0 | 2,842 | Always `7` | Median 1.912; repeated modes near 2.844, 1.910, 1.382 |
| `20260917T190516494149` / 1 | 11,459 | Always `7` | Median 1.386; repeated modes near 1.382 and 1.910 |
| `20260922T042152731314` / 0 | 28,273 | `7` for 21,725; `8` for 6,548 | Both groups have median 0.699; `8` has 4,384 samples in the 0.699 bin |

Ratios near 2.842, 1.909, and 1.382 are the established OEM second,
third, and fourth ratios, not seventh (0.699). In the development chunk,
the proposed `8` also occurs overwhelmingly around seventh's ratio, not
eighth's 0.580. These are repeated steady-ratio counterexamples, not just
instantaneous clutch transitions or a strict statistical threshold.

**Conclusion:** reject `gear = (0x1F4.byte4 >> 4)` and any universal
engaged-gear lookup using that nibble alone. The original two-second match
was insufficient and does not generalize. This does not identify the actual
meaning of that field, reject the entire `1F4` frame, or invalidate the
already-established `1F7` shaft decodes. No need to scan more highway chunks
to repeat this counterexample. Do not promote this nibble to telemetry.

Input provenance (under
`/mnt/EXFAT512/obd-things/tmp/captures/three_bus_drive/broker-drive/`):

- `broker-drive-20260917T190516494149/c-can/chunk_000000_full.candump.zst`:
  `7516ec32582ad7f3a36f85e63df2a6c732760c923d8d46b4e2e91361bf20d4fc`;
- `broker-drive-20260917T190516494149/c-can/chunk_000001_full.candump.zst`:
  `a655acf8d826c48e315573e1a20a37ab30c5a64b8d4cd553a3ee896b7895cf23`;
- `broker-drive-20260922T042152731314/c-can/chunk_000000_full.candump.zst`:
  `b025379e7c62362ca06887ee0395b901368493129fda70ea4b66aa808ea8e6da`.

Result: `tmp/compute/done/20261002T102715Z-2e91f024/result/gear-ratios.json`,
SHA-256 `eb0356e67d68534eb620c4f28eed038572a7b154a79c257191e9a8e9f79b9011`.
Current regression run `20261002T102732Z-1fda3355`: **3 passed**, isolated
source snapshot including the newer `lib/candump_io.py` dependency.

The separate shaft-ratio estimator remains useful research. Existing jobs
`20260924T184026Z-535a73a1` and `20260924T184133Z-8efb31d7` fit then score
a frozen lookup; the latter reports 99.02% of 191,211 qualifying samples
inside its bands. This is supporting ratio-estimator evidence, not an
independently labeled gear/PRND decode or automatic telemetry promotion;
standstill, transitions, reverse direction, and unobserved gears still need
explicit treatment before a non-critical approximate display is integrated.

## Oil-life label lookup: the missing language-table split is resolved

The prior whole-file zero-/one-based label heuristic is not valid. In the
retained APK, `AlfaOBDStart.J()` loads the UTF-16 English resource in two
sections separated by `######`: enum entries before the separator populate
`K1[][2]`; plain labels after it populate **zero-based `J1[]`**. Both replace
`~` with line breaks. This is inspected application-loader behavior, not
an index chosen to make the desired label fit.

For this exact resource, the separator is physical line 3278. Verified plain
label indexes include:

| `J1` index | Label |
|---|---|
| 7987 | Engine Oil Life Remaining |
| 7979 | Engine Run Time Since Last Oil Change Reset |
| 8051 | Automatic Oil Change Indicator Odometer When Reset |

APK SHA-256:
`97b0f100280453b134ceffc09025f2c443adb383ad4382afcd0b0fd7a9a853b9`.
English-resource SHA-256:
`01d622104f9528b5afefa85fc8def400a5d568c873686d838eabbabf2be7dac2`.
Loader extraction: job `20260922T224137Z-b8584207`, retained partial JADX
output under `tmp/ecu_mapping/oil-life-loader-qxuPPR/AlfaOBDStart.java`,
method `J()`, lines 1889–2044. The job exited with decompiler errors in
other code; the inspected loader body is present. Do not describe it as a
successful whole-class decompilation.

SQLite job `20260922T224537Z-35795c5e` joins petrol status row `(8051)` to
request `22 0647`, response bit position 24, length 32, scale `0.1`, offset
zero, device ID `15611`. The July 22 current-vehicle map independently
records `62 06 47 FF FF FF FF`; the saved UI's `429496729.60 km` is a
sentinel-like result, not a usable last-oil-change odometer.

The exact oil-life decoder was traced from `J1[7987]`.
The instruction-aware integer-literal lookup in `tools/dex_field_usage.py`
found only `ra.b`, `ra.c`, and `za.y` in `classes.dex` (job
`20260922T225454Z-12663e63`). Literal presence alone does not establish
dataflow or a request; inspect the decoder branch and compare the saved
current-vehicle response. The extension passed all 23 regression cases in
`20260922T225440Z-72f51a84`; opcode formats follow the
[Android bytecode specification](https://source.android.com/docs/core/runtime/dalvik-bytecode).

## PCM `2185`: oil-life request and percentage decoder recovered

Successful simple-mode extraction `20260922T225856Z-14347c26` retains
`tmp/ecu_mapping/oil-life-za-roNhwg/za.java`. Method `za.y()` reads the DID
high byte at response-base + 1. Its `0x21` branch selects low byte signed
`-123` (`0x85`), then renders `J1[7987]`, the **unsigned byte at response-base
+ 3**, and `%`. Relevant lines: method entry 58413, dispatch/label 59371–59376.
Thus the precise vendor decoder is:

- module: installed PCM, C-CAN, TX `18DA10F1`, RX `18DAF110`;
- UDS request: **`22 21 85`**;
- positive response: **`62 21 85 XX`**;
- **oil life remaining (%) = unsigned `XX`**, with no multiplier or offset.

The July 22 current-vehicle session records:

| Log time | Exchange |
|---|---|
| 00:20:13.212–00:20:13.276 | `10 92 -> 50 92` |
| 00:21:18.013 | `22 21 85` |
| 00:21:18.027 | `62 21 85 11` |
| 00:21:18.074 | adapter prompt completes response |

Source `tmp/ecu_mapping/android_tablet/ccan_live_20260722_001010/alfaobd_campaign.decoded.txt`,
lines 30369–30370 and 30699–30701; SHA-256
`6fe10e77ba22cd22355d8aa18603351bb46f686fe4a8606f73bdef8371593eef`.
`ATSH DA10F1` and response filter `18DAF110` precede the current PCM block.
The saved module map independently summarizes the same exchange, and
`pcm_system_status_text.txt` renders **17%**, exactly `0x11`.
UI export SHA-256:
`7af59815b4ed55d66a3f1a9d6274b039bda30b60915408b305a19e16b604ca48`.

This closes the historical **label/request/field/scale** link. Confidence is
vendor decoder plus same-vehicle positive response and matching rendered
value, not an independent measurement of physical oil condition. The older
generic selected profile contains other unsupported/misdecoded rows; this
specific chain does not validate that profile wholesale. The two `ra` label
hits are different branches and were not used to infer this PCM DID.

The historical July 22 value remains **17%**, not a current measurement.
Oil life is a maintenance estimate, separate from oil temperature/pressure
and owner-entered service records. No reset action is in scope.

### Integration boundary and next step

The owner-run check at 2026-10-03 00:30Z now verifies a direct padded `2185`
read without sending session control or TesterPresent. The inherited session
was not independently identified; do not call this a positively proven default
session, or replay historical `10 92`. No further broad support scan is needed.
Next is deliberate integration of this fixed maintenance read into the
existing sequential scheduler at slow cadence; there is no need for 1 Hz.
Keep the exact oil-life label separate from physical oil quality and the
owner-entered service journal. A current cluster-display comparison is useful
additional corroboration when available, not grounds to discard the observed
vendor decoder and successful direct response.
Values outside `0..100` should be unavailable, not clamped into a percentage.
Do not refresh the dashboard with the July snapshot.

### October 2 parked-check preparation: host permission blocker, no TX

After the owner reported ignition on, the broker's fresh passive cache showed
ignition true, engine RPM 0, and road speed 0. Both active helpers were idle;
TPMS fallback remained disabled/inactive. The serial resolver identified the
expected C-CAN Board A CAN1 role on pins 6/14. All three vehicle interfaces
were classical listen-only, ERROR-ACTIVE, `restart-ms 0`, with zero RX/TX
errors; the spare was down. The C-CAN RX-drop counter was already 43, so do
not call the host's cumulative receive history loss-free.

The sandbox's `sudo -n true` failed because `no new privileges` prevents
elevation. No active tool was executed and no arming, request, service stop,
or restart was attempted. C/B/CH TX counters remained 127233/10771/0 across
the checks. This is an execution-environment blocker, not a PCM timeout,
negative response, or failed restoration.

The existing guarded `projects/vehicle_data/pcm_temperature_support.py` now
has a fixed `--profile oil-life`: one padded `22 2185`, exact single-byte
response validation, unsigned percent `0..100`, and the unchanged stationarity,
ownership, inhibit, interface, and cleanup gates. Default temperature behavior
is preserved. Dry-run advertises one request and no session, retry, or flow
control. Offline regression job `20261002T232716Z-96511136` passed **6 tests
and 6 subtests**, including both profiles, failed vehicle gates, failed
restoration, bad echoes/lengths, and out-of-range oil-life values.

Next, from the owner's normal vanpi shell while parked/ignition-on/engine-off:

```bash
python3 /home/pi/dev/obd-things/projects/vehicle_data/pcm_temperature_support.py --profile oil-life --execute --confirm-parked-ignition-on-engine-off
```

Inspect the returned `report_path`, exact `response_hex`/`value_percent`,
and `restored_passive` before documenting success. Reports stay under
`tmp/inventories/pcm/`. No successful live read or current percentage is
claimed at this preparation checkpoint.

### Owner-run lock refusal and cooperative handoff fix

Owner report `tmp/inventories/pcm/oil-life-support-20261002T233039010556Z.json`
contains an empty result list, `restored_passive: null`, and `ChannelLockError`
for `can-role-c-can`. It failed before ownership/arming/transmission; null is
not evidence of restoration failure. The broker PID recorded in the observer
lock metadata matched its live service PID, and `/v1/status` showed the
continuous display receiver receiving in listen-only mode while active-drive
was idle. That receiver intentionally retains a shared role/channel lease
and checks the cooperative handoff gate every 250 ms.

The support check omitted that admission step. It now acquires the existing
bounded C-CAN `active_turn` before the real role/channel locks and retains it
until passive restoration and release finish. The maximum admission wait is
1.25 seconds; all vehicle-state, physical-route, privilege, inhibit, and
interface checks remain. No service stop/restart, lock deletion, lock bypass,
or additional diagnostic request was introduced.

On resumption at 2026-10-03 00:27Z, the broker again reported fresh ignition
on, zero RPM, and zero road speed. All three interfaces were passive and
ERROR-ACTIVE; the same sandbox privilege restriction remained. Regression
job `20261003T002824Z-20e79287` passed **23 tests and 124 subtests**, covering
the support check plus handoff/display-receiver regressions. New assertions
require handoff before arming and restoration before handoff release, and
prove that busy admission causes no CAN socket or route acquisition.

Use the same owner-shell command above to retry while parked/ignition-on/
engine-off. The handoff fix has not yet produced a live `2185` reply at this
checkpoint, and no current oil-life value is asserted.

### Successful owner-run validation — October 3, 2026 00:30Z

The corrected command completed while the owner reported parked, ignition on,
engine off. Report:
`tmp/inventories/pcm/oil-life-support-20261003T003014137255Z.json`, SHA-256
`a951e3ff22ed38c874175de38f4491065afaf48118791321afb31641b65d8818`.

- Started `00:30:14.136988Z`; request attempted `00:30:15.369855Z`;
  completed `00:30:15.905124Z`.
- One fixed-DLC-8 request `03 22 21 85 00 00 00 00`, physical PCM
  `18DA10F1 -> 18DAF110`, resolved C-CAN pins 6/14.
- Exact positive UDS response **`62 21 85 52`**; unsigned `0x52 = 82`:
  **PCM-reported oil life remaining 82%**.
- No session-change, TesterPresent, retry, FlowControl, or reset command.
- `restored_passive: true`, `error: null`.

Independent post-check interface inspection confirmed all three roles
classical/listen-only, ERROR-ACTIVE, `restart-ms 0`, fixed rates, and zero RX/TX
error counters. C-CAN TX increased **127233 -> 127234**; B-CAN stayed 10846
and CAN-CH stayed 0. The broker display receiver resumed `receiving` with
fresh timestamps in listen-only mode. Broker PID 3446533 and recorder PID
3447949 remained active with `NRestarts=0`; no service stop/restart was needed.

This live-validates both the cooperative handoff fix and this single parked
no-session-change read. It does not activate a recurring poll, publish the
value to the dashboard, measure physical oil condition, or establish the
inherited diagnostic session. The validation is complete; the vehicle does
not need to remain ignition-on for this work.

### Production integration implementation — October 3

At the owner's request, the closed running scheduler now supports
`engine.oil_life_remaining` / `pcm.did.2185`, integer percent `0..100`, at
most once per 60 seconds. It has a separate one-use permit, sequential
response-before-next-request execution, optional per-epoch failure isolation,
and immediate invalidation on owner stop. No session, retry, wake or reset
path was added. A separate broker/helper feature flag prevents old broker
processes from receiving an unregistered metric during rolling deployment.

The existing Service card and dated-last-reading policy are connected to the
new registry source; saved service records are unaffected. No historical 82%
was republished as live. Python regression `20261003T013845Z-1e242336` passed
1,631 tests / 2,059 subtests (6 skipped); dashboard regression/build
`20261003T013727Z-8f1f455d` passed 443 tests. Dashboard build `2c99f40ec884` is
published and HTTP-verified. The owner completed the guarded broker restart
at `2026-10-03T01:47:39.555359+00:00`. Independent snapshot/catalog and LAN
`/v2/summary` checks confirm registration and Service-card source availability;
active-drive is enabled, the persistent last-reading store reports no error,
and the broker, recorder and both web listeners are active. All roles remained
passive/ERROR-ACTIVE, with TX counters unchanged at C/B/CH 127234/10846/0 and
zero RX/TX errors. The asleep vehicle correctly produced no cached oil-life
value. Activation is complete, but this is not yet a production-poll observation;
that will come from the next normal engine-running interval.

### Related runtime field reveals a decoder caveat

`za.y()` also links `14F9` to `J1[7979]` and a `0.021333 min/count`
multiplier, but its final byte is added as signed Java `byte`, unlike its
first three masked bytes. Saved `62 14 F9 00 06 DC DE` therefore yields
`(0x0006DCDE - 256) * 0.021333 = 9589.226166 min`, exactly the UI's
`9589.23 min`. An unsigned interpretation would give `9594.687414 min`.
This independently corroborates the recovered code path while exposing
another Alfa rendering defect; do not copy that signed-byte behavior into
telemetry or promote an unvalidated corrected runtime scale.
