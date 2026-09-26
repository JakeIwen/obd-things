# Gear and oil-life offline follow-up — September 22, 2026

Scope: saved current-vehicle captures, the owner-supplied AlfaOBD APK, and
the exact-vehicle OEM service-document mirror. No live CAN access, service
changes, diagnostic requests, or telemetry promotion occurred.

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
The new compute-task proposal remains under
`tmp/ecu_mapping/gear-ratio-compute-task-proposal.json`, pending explicit
owner approval; `.van-compute.json` has not been changed.

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

Do not replay the historical session entry or add a production poller yet.
The saved success occurred after acknowledged session `92`; default-session
or no-session-change support has not been established for `2185`. Next is
one owner-authorized parked, ignition-on/engine-off physical `22 2185` read
through a reviewed role-aware path, with exact echo/length checks, fixed
padding, a bounded timeout, and passive restoration. If it succeeds, compare
the current oil-life display when available and integrate a slow maintenance
poll into the existing sequential scheduler; there is no need for 1 Hz.
Values outside `0..100` should be unavailable, not clamped into a percentage.
Do not refresh the dashboard with the July snapshot.

### Related runtime field reveals a decoder caveat

`za.y()` also links `14F9` to `J1[7979]` and a `0.021333 min/count`
multiplier, but its final byte is added as signed Java `byte`, unlike its
first three masked bytes. Saved `62 14 F9 00 06 DC DE` therefore yields
`(0x0006DCDE - 256) * 0.021333 = 9589.226166 min`, exactly the UI's
`9589.23 min`. An unsigned interpretation would give `9594.687414 min`.
This independently corroborates the recovered code path while exposing
another Alfa rendering defect; do not copy that signed-byte behavior into
telemetry or promote an unvalidated corrected runtime scale.
