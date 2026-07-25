# Cluster singleton AlfaOBD/CAN correlation — 2026-07-24

## Outcome

A guarded parked shakedown selected one AlfaOBD Instrument Cluster status parameter at a time while
an independent listen-only PCAN recorder captured the cluster's C-CAN diagnostic traffic. All seven
segments completed, including repeated Engine-speed and Battery-voltage anchors. Every host-timed
segment contained one distinct non-TesterPresent `22 DID` request payload, repeated with strict
request/response alternation:

| AlfaOBD label | exact request | positive response prefix | singleton result |
|---|---|---|---|
| Engine speed | `22 1000` | `62 1000` | repeated anchor selected `1000` both times |
| Vehicle speed | `22 1002` | `62 1002` | unique request in its segment |
| Actual Gear | `22 0107` | `62 0107` | unique request in its segment |
| Battery Voltage (+30) | `22 1004` | `62 1004` | repeated anchor selected `1004` both times |
| Outside temperature | `22 1005` | `62 1005` | unique request in its segment |

The mappings remain classified as candidates because a positive read plus an AlfaOBD label does not
by itself prove every unit, scale, or enum. They are nevertheless strong label-to-DID associations:
the supervisor verified the selected singleton label and monitor state, the Info log preserves the
planned label-run order, the independent wire windows contain no competing DID, every request has an
immediate exact positive echo, and both repeated anchors are consistent. This independently
discriminates the same five associations suggested by the 2026-07-22 eight-item round-robin trace.

No diagnostic actuation occurred. Apart from TesterPresent, the bounded wire windows contain only
physical `22` reads and `62` responses.

AlfaOBD was already connected before the bounded campaign began. The capture therefore starts in an
inherited Alfa-held diagnostic session and shows repeated `3E 00 -> 7E 00`, not the session-opening
exchange. The earlier 2026-07-22 cluster connection used `10 03`, so the singleton capture alone did
not establish the standalone session recipe. The direct comparison later in this finding closes
that gap: all five reads work after an explicit, positively acknowledged `10 01`, and their payloads
are identical after `10 03`.

## Conditions and topology

- Van parked in Park, ignition on, engine off.
- AlfaOBD 2.4.4.0 ran on the USB-connected Android 7 tablet and was already connected to the
  `Instrument panel Continental` runtime profile.
- The OBDLink MX+ occupied the OBD branch of the SGW-bypass Y splitter.
- PCAN independently listened on the pigtail C-CAN DB9 (DLC 6/14) at 500 kbit/s, listen-only and
  ERROR-ACTIVE.
- The campaign ID was `cluster-shakedown-20260724-005100`.
- Raw artifacts remain gitignored under
  `/mnt/EXFAT512/obd-things/tmp/ecu_mapping/alfaobd-drive/` and
  `/mnt/EXFAT512/obd-things/tmp/captures/ccan/drive-correlation/`.

The first passive recorder completed its requested 600 seconds, but the tablet's conservative
Android-7 UI validation made the seven-segment campaign last about 15 minutes 40 seconds. A second
900-second recorder was therefore started with a deliberate overlap. Both runs completed their full
duration with zero reported SocketCAN drops and complete streams. Their union continuously covers
all seven singleton event intervals; identical observations in the overlap can be de-duplicated
without a payload conflict.

### Artifact integrity

| evidence | SHA-256 |
|---|---|
| pulled `AlfaOBD_Debug.bin` | `742527225cf30fbceb6726c9a1be0fe55d8b974e69a8fa5c6b06fea3634a3af1` |
| pulled `MARELLI_DASH_EP_Info.log` | `a0a1470a2e9dafa8f200755d972dcf6efc9a8a849ca6c37e412c8969e15a228f` |
| unchanged pulled `Gauges_Data.csv` | `a9710f326142e7d2c105ca17f24104930e79cacc063d1d9ab460382f013357ab` |
| explicit two-recorder `capture-set.json` | `cad8f0e3c6758a42b57e51b8a22ab645d6b7bf82c10f9b44efed9b3a4eba5c0f` |
| final schema-2 singleton-join report | `8f7e198ea2a9fedf55a64b4d1c44e970eadb48d69136b3a2d11acb058c21f1e1` |
| first recorder `run.json` / checkpoint / `manifest.jsonl` | `76eb116023f599c4989684fd3d5ba01c8db4a1d27f3fd2dd975912986885aad8` / `72645b86da99bc93c76a6da4792dafb5c643b2fe5db605211dc37adc32fcb89d` / `15edc223c2965f5e5292b05df578bf79ab05fb6665b87694926cd1f6c458c3e8` |
| overlap recorder `run.json` / checkpoint / `manifest.jsonl` | `f66bd656ee0d7a36fecbc699ef68c931f89da113f5b0adbb533143ab65df931f` / `6ad503c4ae7a0a2ef36ebfbb20770757f43f809ad2d439ef4648aa3ba46b4754` / `0a701f5e11b8096dcc433953b6c6b9b1a8f8a33cf93ad65cc988a7e467bf5c35` |

## Per-segment evidence

TesterPresent exchanges are excluded below. Each listed request count equals its exact positive
response count, request and response messages strictly alternate, and there are no extra
non-keepalive cluster diagnostic messages inside the event interval.

| seq | label | DID | request/response pairs | response data | Info rows / rendered values |
|---:|---|---:|---:|---|---|
| 0 | Engine speed | `1000` | 497 | `0000` x497 | 1,025 complete rows, all `0.00 rpm`; leading buffered contamination |
| 1 | Vehicle speed | `1002` | 507 | `00` x507 | 507 rows, all `0.00 km/h` |
| 2 | Actual Gear | `0107` | 528 | `00` x528 | 528 rows, all `P` |
| 3 | Battery Voltage (+30) | `1004` | 487 | `0x77` x330; `0x76` x157 | 330 x `11.90 V`; 157 x `11.80 V` |
| 4 | Outside temperature | `1005` | 499 | `7A` x499 | 499 rows, all `21.00 °C` |
| 5 | Engine speed | `1000` | 480 | `0000` x480 | 480 rows, all `0.00 rpm` |
| 6 | Battery Voltage (+30) | `1004` | 482 | `0x76` x479; `0x77` x3 | 280 complete rows: 278 x `11.80 V`; 2 x `11.90 V`; one incomplete trailing `11.` line excluded |

The internal five Info runs have exactly the same counts as their wire request loops. Only the
outer first and last runs are over-/under-inclusive because the campaign began and ended between
log-writer flushes.

### Scaling boundary

- Battery DID `1004` is now supported at four adjacent raw points across the July 22 and July 24
  captures: `0x76 -> 11.80 V`, `0x77 -> 11.90 V`, `0x78 -> 12.00 V`, and
  `0x79 -> 12.10 V`. This establishes AlfaOBD's `raw x 0.1 V` rendered conversion over the observed
  range; no independent voltmeter was joined to this trial.
- Engine DID `1000` and vehicle-speed DID `1002` were observed only at zero in this parked run. The
  singleton association is strong, but nonzero driving points are still required to prove their
  scales.
- Gear DID `0107` observed only `00 -> P`. Ordinary PRND transitions are still required before
  assigning the rest of the enum.
- Temperature DID `1005` now has two Alfa-rendered points across the July 22 ordinal trace and July
  24 singleton run: `0x77 -> 19.50 °C` and `0x7A -> 21.00 °C`. They exactly follow
  `raw x 0.5 - 40 °C`. The installed APK catalog contains a one-byte `22 1005` IPC decoder
  candidate with that slope and offset, and its length/result match this capture. The catalog also
  contains other IPC variants and its profile-to-row indirection remains unresolved, so this
  establishes AlfaOBD's observed rendering over two points, not independently ground-truthed
  physical temperature.

## AlfaOBD buffered-artifact finding

The Android file-size checkpoints are valid activity and provenance witnesses, but they are not
logical record boundaries on this tablet. During the campaign:

- every sampled Debug slice or inter-segment gap advanced in an 8,192-byte multiple, with most
  segment slices roughly 90–106 kB;
- the profile Info file advanced in exact 8,192-byte multiples; and
- `Gauges_Data.csv` remained unchanged at 5,830 bytes.

Consequently, slicing each segment at its sampled file sizes mixes a tail from one logical monitor
run with the next and can omit a buffered tail. The first attempted join correctly failed instead
of forcing a false per-segment Debug/Info pairing.

The safe join model is:

1. validate the completed campaign, pulled-artifact hashes, passive-recorder completion, continuous
   coverage, and zero-drop provenance;
2. use the first-before through last-after artifact range only as a whole-campaign envelope;
3. require the Info envelope's contiguous exact-label run order to equal the planned schedule;
4. resolve each DID from the independently host-timed wire interval, with strict request/response
   pairing and no competing non-keepalive request; and
5. use repeated anchors and the whole Debug envelope as independent consistency checks, never as
   fabricated per-byte timestamps.

The whole Debug envelope provides that independent check. After discarding TesterPresent and one
empty leading prompt, its 3,403 sent requests are an exact payload prefix of the 3,480 host-window
wire requests; 3,402 prompt-complete responses are the corresponding exact prefix of 3,480 wire
responses. The first six planned runs match in full. The pulled envelope ends partway through the
final Battery run with 405 requests and 404 complete responses versus 482 wire pairs, without a
contradictory request or response. AlfaOBD can split one logical response across multiple `R` log
callbacks and occasionally records an `R` callback just before its `S` line, so the safe parser
compares independently reconstructed send and prompt-delimited receive streams rather than pairing
adjacent text lines.

## Standalone default-versus-extended-session follow-up

With AlfaOBD closed and its Android process absent, the same cluster endpoint was queried directly
through PCAN while parked, ignition on, and engine off. The C-CAN interface was 500 kbit/s,
ERROR-ACTIVE, and error-free. Six checkpointed `did_sweep.py` runs covered `0107` and `1000-1005`
in three states:

1. after a diagnostic-idle interval, with no session or TesterPresent request;
2. after exact `10 03 -> 50 03 00 32 01 F4`; and
3. after exact `10 01 -> 50 01 00 32 01 F4`.

The longer explicit-session runs also received exact `3E 00 -> 7E 00` keepalive echoes. Every run
completed, received a response to every request attempt, and restored `can0` to verified
listen-only mode. All seven DID results were byte-for-byte identical in all three states:

| DID | response in idle/inherited state | response after `10 01` | response after `10 03` | interpretation |
|---:|---|---|---|---|
| `0107` | `62 01 07 00` | same | same | mapped Actual Gear candidate; `00 -> P` in Park |
| `1000` | `62 10 00 00 00` | same | same | mapped Engine-speed candidate; zero while stopped |
| `1001` | `62 10 01 64` | same | same | prior ordinal trace suggests Fuel level; not singleton-discriminated |
| `1002` | `62 10 02 00` | same | same | mapped Vehicle-speed candidate; zero while stopped |
| `1003` | `7F 22 31` | same | same | negative control; unavailable in both sessions |
| `1004` | `62 10 04 73` | same | same | mapped Battery-voltage candidate; Alfa renders `11.5 V` |
| `1005` | `62 10 05 75` | same | same | mapped Outside-temperature candidate; Alfa formula not independently proven here |

This establishes default-session access rather than merely inferring it from an S3 timeout.
Extended session `03` is accepted by this IPC, but it is neither required for these five mapped
reads nor sufficient to expose control DID `1003`. The no-session runs prove the same physical
reads also work when the reader leaves the inherited ECU session unchanged; those runs must not be
misreported as a positively identified default session. A bounded standalone viewer can therefore
use a fail-closed, session-unchanged policy—physical `22` requests only, with no gratuitous `10` or
`3E` traffic—because both explicitly tested sessions are compatible. Repeated `22` requests may
refresh S3 and prolong the inherited state even though they do not select a session, so
“session-unchanged” must not be read as “forced to default.” The viewer should not silently enter
session `03` if a future vehicle-state check fails.

### Direct-comparison artifact integrity

The raw checkpointed reports remain gitignored under `tmp/inventories/cluster/`. Each row below
binds one complete summary to its append-only result stream:

| state / DID range | summary SHA-256 | results JSONL SHA-256 |
|---|---|---|
| idle/inherited `0107` | `9b2d7f0d52a7f84391fead72156e51f9807fcd0a69f4dda1fc69d7bceccb81c0` | `e9347d94980edb10c14f0086ebbc43dc7ca442ddc779f978a102d8ef70035602` |
| idle/inherited `1000-1005` | `594adaef9a570d1c2e8ece69199edbf725d7bf13ed52a3251fd4c95eb5b45af1` | `a279e01f051d81346f9ba3c9984589dff5d7d6ee7806ed41deb6f30761471d7d` |
| explicit session `03`, `0107` | `aeeaa3dd060b14621ef70cb432a25508b06fb2a20f79fceb5a2d74cd8a3b47df` | `1416718ebe1b8d7d1d9d20e1e660304a9187637ec128748ac331001b4a149d4a` |
| explicit session `03`, `1000-1005` | `fb71755d49774558a3a766ad49ee9039575e73750d315efb6edbef27a00446db` | `07a161eafd7fed3ff7b8e38cd878d78c7c70ad6b64d35798bf41e95366912124` |
| explicit session `01`, `0107` | `c216b0c652dcb67c673ea255a3d29b7b04783be74771ca627a7cbd7f078af6f4` | `420cea3672b88ea563380ebbd5e6f7164e24ae7c7b61985d0af15b21e4466e6b` |
| explicit session `01`, `1000-1005` | `91aa7919664d071e0c2f6ff8d9e507aae08a03590e1920dcb7534bc6546e46e1` | `abc330d686dcf978c55650418c6a7a0e22f447459c993eba071f9284d825af08` |

## What this buys

The cluster endpoint `18DA60F1 -> 18DAF160` now has a small, exact default-session request set from
which to build an Alfa-independent monitor:

- RPM candidate: `22 1000`
- road-speed candidate: `22 1002`
- PRND candidate: `22 0107`
- battery-voltage candidate: `22 1004`, with Alfa rendering `raw x 0.1 V`
- outside-temperature candidate: `22 1005`

Default session is sufficient, and a session-unchanged bounded pass also succeeded without `10` or
`3E`. Its repeated `22` reads may nevertheless have refreshed S3. The next evidence step is to
collect these exact DIDs during ordinary driving to establish nonzero RPM/speed ranges and normal
gear transitions, while a stable ambient-temperature change can test the `1005` formula.

`projects/ecu_mapping/cluster_drive_log.py` now implements that fixed-profile collection without
reusing the parked viewer. It leaves the inherited session unchanged, sends only those five
physical `22` requests at most five total attempts per second, and owns an integrated full-bus
recorder because an armed transmitter cannot coexist with the separate listen-only recorder on
one PCAN. Append-only DID attempts are count-cross-checked by echoed DID against
kernel-timestamped cluster wire records; the retained payload/sequence fields permit a stricter
offline join. Full CAN evidence is stored as independently validated ten-minute zstd chunks. This
is a prepared collection path, not new vehicle evidence: it still needs a parked/idling
real-hardware rotation shakedown before the long drive.

The resulting correlation remains candidate-only for absolute scaling. Nonzero co-movement can
establish association and relative behavior, but radar/GPS/tachometer or another controlled
reference is still needed for absolute RPM/speed; observed gear states and temperature range bound
only the states actually encountered. The validated singleton workflow can then be expanded in
bounded groups to the remaining cluster labels, retaining repeated anchors and independent wire
coverage.

Source context: [2026-07-22 C-CAN AlfaOBD live correlation](2026-07-22_ccan_alfaobd_live_correlation.md)
and [installed APK catalog extraction](2026-07-21_alfaobd_apk_catalog.md).
