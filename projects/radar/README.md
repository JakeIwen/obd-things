# projects/radar — 2022 Promaster ACC radar (Bosch DASM / MRR1evo14F)

Reverse-engineering + alignment work for the forward-looking ACC/FCW radar. The radar has an
**active vertical-misalignment fault (DTC C1418-78, ≈ −1.26° elevation)** that disables ACC/FCW.

> **Start with [`docs/AGENT_HANDOFF.md`](docs/AGENT_HANDOFF.md)** — the current-state summary
> (verified facts, the 0x0251 routine mechanics, the static-mirror negative result, open work, and a
> ⚠ TEARDOWN section for the temporary cron data-collector). Universal bus facts/gotchas are in the
> repo-root [`README.md`](../../README.md).

## Current conclusion
The −1.26° is stable across drive cycles and unmoved by the static-mirror routine or by physically
nudging the radar — most likely a **physical mount misalignment** beyond the radar's self-align range,
i.e. a mechanical fix, not a UDS routine. A drive-data logger is collecting traces to confirm
physical-vs-dynamic. See `findings/radar_acc_did_findings.md`.

## Scripts (run from the repo root, e.g. `python3 projects/radar/<script>`)
| script | what | writes |
|---|---|---|
| `radar_acc_baseline.py` | reproduce UDS baseline: session, key DIDs, serial, DTCs (read-only) | — |
| `radar_acc_live.py` | top-style live alignment/health gauge @5 Hz (read-only) | — |
| `radar_acc_drive_log.py` | log angles + DTC + speed to CSV while driving (read-only) | `dumps/` |
| `auto_drive_logger.py` | cron supervisor: passively auto-logs each drive (read-only) — **temporary, see TEARDOWN** | `../../tmp/` |
| `perturb_monitor.py` | flag any DID that changes while nudging the housing (read-only) | — |
| `radar_acc_align_0251.py` | **⚠ ACTUATION** — gated `31 01` alignment-routine runner (`--arm` + typed confirm) | — |

Generic discovery tools (`tools/did_sweep.py`, `routine_scan.py`, `uds_send.py`, `signal_correlate.py`)
live at the repo root and take the module key `radar_acc`.

## Subdirs
- `docs/` — `AGENT_HANDOFF.md` (read first), `radar_acc_handoff.md` (original investigation),
  `radar_acc_alfaobd_bugreport.md` (AlfaOBD mis-mapping evidence).
  - `docs/oem/` — **OEM / authoritative sources** (FCA STAR TSB for C1418-78, etc.); trust over our
    inferred findings. The TSB says C1418-78 is a seating/bumper-contact fault → fix mechanics, then calibrate.
- `findings/` — `radar_acc_did_findings.md`: decoded DIDs, angle scaling, 0x0251 mechanics.
- `dumps/` — kept captures (`radar_acc_did_sweep.txt`).

**Safety:** `radar_acc_align_0251.py` is the only actuation in the whole repo. Read the Safety &
liability section of the root README before using it.
