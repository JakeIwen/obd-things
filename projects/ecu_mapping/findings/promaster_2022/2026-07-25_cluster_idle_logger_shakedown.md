# Cluster idling drive-logger shakedown — 2026-07-25

## Outcome

The fixed-profile cluster drive logger completed its first real-hardware
12-minute run while the van idled in Park. The complete run contained 3,114
physical `22` requests and 3,114 exact positive responses. All five DIDs passed
the startup profile, no negative or unexpected response appeared, the
ten-minute raw-capture rotation completed, count-level wire cross-validation
matched exactly, and `can0` was restored to 500 kbit/s listen-only
ERROR-ACTIVE before the exclusive lock was released.

This run validates the logger, raw recorder, rotation/finalization path, strict
telemetry publisher, and dashboard integration. It used the Pi's ext4
filesystem under repository `tmp/` so repair of `EXFAT512` could proceed
independently. It therefore does **not** replace the writable-mount/free-space
preflight or prove the current exFAT volume for a long campaign.

After this run, the repaired external volume passed the separate host-side
mount check: exFAT was read-write with `uid=1000,gid=1000`,
`fmask=0022,dmask=0022`; the expected capture tree was owned by `pi:pi`; and
the disk-health watchdog created and removed its probe as `pi`. About 58 GiB
was free at that checkpoint. Those checks make the external path ready for a
later campaign, but the logger must still repeat its own sticky mount,
writability, and free-space preflight at launch.

## Conditions and safety boundary

- Campaign: `cluster-drive-shakedown-20260726T050955Z`
- Local time: 2026-07-25 23:09–23:21 MDT
- Vehicle parked in Park, engine running
- PCAN on C-CAN pins 6/14 at 500 kbit/s
- AlfaOBD closed; no other diagnostic client active
- Cluster endpoint `18DA60F1 -> 18DAF160`
- Physical reads only: `22 1000`, `22 1002`, `22 0107`, `22 1004`,
  and `22 1005`
- No session control, TesterPresent, functional request, retry, DTC, routine,
  write, IO control, security, reset, wake, or interface recovery
- Requested duration 720 seconds; stop reason `duration_limit`

The logger observed the verified `0x2EF` ignition-presence frame before and
during the run. Its requested five-attempts-per-second ceiling was respected.
The last cycle ended at the duration boundary after DID `1004`, so `1005` has
622 samples while each other DID has 623; the independent wire stream has the
same per-DID counts.

## Acceptance results

| invariant | result |
|---|---|
| status / duration | `complete`; `duration_complete=true`; `duration_limit` |
| startup profile | all five exact-length positives |
| high-level requests / responses | 3,114 / 3,114 |
| wire requests / positives | 3,114 / 3,114 |
| NRC / pending / other endpoint frames | 0 / 0 / 0 |
| wire cross-validation | complete; no mismatch |
| full-bus frames | 1,960,920 |
| chunks | 2 complete zstd frames; first rotation at 600.96 s |
| socket drops | 0 reported; interface RX dropped stayed at its baseline 22 |
| raw internal accounting | 1,960,920 ingested = 1,960,920 chunk frames |
| restoration | passive verified; lock released |
| fatal errors | none |

## DID observations

These are raw observations, not newly promoted physical scales:

| Alfa-associated DID | samples | observed raw range | interpretation boundary |
|---|---:|---:|---|
| Engine speed `1000` | 623 | `2936..6136` (94 values) | initial `5944`, peak `6136`, then a stable tail near `3000`; `raw / 4` would be about 734–1,534 rpm and a 753 rpm median, strongly plausible but not yet Alfa- or tachometer-confirmed |
| Vehicle speed `1002` | 623 | `00` only | expected while parked; no scale evidence |
| Actual Gear `0107` | 623 | `00` only | agrees with the prior `00 -> P` observation; no non-P enum evidence |
| Battery `1004` | 623 | `80..8E` | Alfa-observed `raw / 10` gives 12.8–14.2 V, median 13.8 V |
| Outside temperature `1005` | 622 | `7E..7F` | existing Alfa/catalog formula would give 23.0–23.5 °C; this run had no independent thermometer or Alfa rendering |

The nonzero `1000` shape is new behavioral evidence: it fell from an
initial high, warm-up-like value to a narrow idle band while the speed and Park DIDs
remained fixed. It strengthens the Engine-speed association but does not by
itself promote `/4` as a physically verified scale.

## Telemetry/dashboard result

The logger submitted 3,733 bounded best-effort observations: the five DID
values plus rate-limited fresh ignition presence. The broker accepted 3,728.
The first five attempts saw `ENOENT` because the launcher deliberately started
the logger/lock before restarting the telemetry service; later publication
recovered and the capture was unaffected. No value was rejected, superseded,
or left pending, and the publisher thread exited.

The Chrome 119 tablet dashboard selected Driving from verified ignition
evidence, displayed fresh cluster battery voltage with `ALFA SCALE` quality,
and kept raw RPM/speed/gear candidates out of the driving hero. The live
screenshot remains gitignored at
`tmp/cluster-idle-dashboard-live-2.png` (SHA-256
`611b86e0e9e6189859561e4d147317af6939591334f53f68f0b8ddf4ea01e7d3`).

The live run also exposed two presentation-only cleanup items, now addressed
in code for future runs: use division instead of binary-noisy multiplication
for the one-decimal battery value, and clear a stale publisher
`last_error` after a later successful publication while retaining the
historical error count.

A later Chrome 119 tab-suspension check exposed a more important delivery
issue: the browser could replay buffered SSE snapshots after resuming and make
old relative ages look fresh. The web protocol now carries process-instance,
sequence, wall-clock, and monotonic generation metadata. The browser rejects
queued/out-of-order/retired-instance events and performs a cache-bypassing HTTP
resynchronization before reopening SSE after visibility or page restoration.
The original screenshot was taken during the live run; it does not by itself
validate the later resume-from-suspension path.

## Artifact integrity

Raw artifacts remain gitignored under:

- `tmp/ecu_mapping/cluster-drive/cluster-drive-shakedown-20260726T050955Z/`
- `tmp/captures/ccan/cluster-drive/cluster-drive-shakedown-20260726T050955Z/`

| artifact | SHA-256 |
|---|---|
| `run.json` | `52c5c1351cbde2ace63b8c2a3f4506f2fb7af8ae0926b99bb3375111951ddac0` |
| final `summary.json` | `d6f1d2ca64e88b2748476196ec3974a67766cfb2acca59fc7078de73314ac36a` |
| `samples.jsonl` | `85db8b8b7cddc4dacd1b612c304ec0f69eb8125ecec8a487d43f5ee078565b63` |
| raw `manifest.jsonl` | `4167e1116ee664bd663136d0ddb767dd1d00ed0914ec0aaaf65d4a06f4af1cfa` |
| `cluster_wire.jsonl` | `e61c02ffabc7e84da58587f79e25746eb0dd6033127f645322f8979ceb2ab29b` |
| chunk 0 | `d9e854660f3a6d27bdc2eda49ddd3834c5ebc964ec35fe03503693f3ceb90bed` |
| chunk 1 | `9663f6f5bfaf1055bfe54f77692a30a0dbfdbcaed2f0e9e1d709b8ba2e7f1c08` |

## Next evidence step

Run the guarded AlfaOBD singleton scaling plan during ordinary driving with a
listen-only PCAN capture. Its two interior labeled RPM/speed passes can test the
`/4` RPM hypothesis and determine the cluster speed rendering; the
already-qualified battery signal occupies the two buffer-prone outer
boundaries. `Actual Gear` is presently a PRND-selector candidate, so automatic
ratio changes while it remains in `D` may add no enum evidence; use a separate
controlled parked/foot-brake selector exercise if needed. Keep all resulting
conversions at candidate/Alfa-observed quality until the intended external
reference or controlled ground truth is joined.
