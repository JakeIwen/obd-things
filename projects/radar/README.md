# projects/radar — 2022 Promaster ACC radar (Bosch DASM / MRR1evo14F)

Reverse-engineering + alignment work for the forward-looking ACC/FCW radar. The radar has an
**active vertical-misalignment fault (DTC C1418-78, ≈ −1.26° elevation)** that disables ACC/FCW.

> **Start with [`docs/AGENT_HANDOFF.md`](docs/AGENT_HANDOFF.md)** — current-state summary incl. a
> prominent **⛔ RULED OUT (do-not-retry)** list, the verified model, the fix path, `0x0251` mechanics,
> and the ⚠ TEARDOWN section. Also read [`docs/oem/`](docs/oem/) (authoritative — trust over inference)
> and the repo-root [`README.md`](../../README.md) (bus facts + RESEARCH-FIRST method).

## Current conclusion (reconciled — see AGENT_HANDOFF for the full ruled-out list)
The radar stores a vertical boresight error ≈ **−1.26°** → DTC C1418-78 → ACC/FCW off. The OEM fix is a
**dynamic "Service Drive Alignment" (SDA), NOT a static mirror** (that premise was a Giulia doc, ruled
out). The radar self-aligns small deviations while driving but only within a **limited window**; −1.26°
is beyond it (a 2-hr highway drive did not move it). So the **gate is physical**: re-seat/level the mount
to get back inside the window, then normal driving or the SDA finishes it. **Van = home → no shop.** Full
detail + decoded DIDs in `findings/radar_acc_did_findings.md` and `docs/oem/`.

## Scripts (run from the repo root, e.g. `python3 projects/radar/<script>`)
| script | what | writes |
|---|---|---|
| `radar_acc_baseline.py` | reproduce UDS baseline: session, key DIDs, serial, DTCs (read-only) | — |
| `radar_acc_live.py` | top-style live alignment/health gauge @5 Hz (read-only) | — |
| `radar_acc_drive_log.py` | log angles + DTC + **speed (DID 0x1002)** to CSV while driving (read-only) | `dumps/` |
| `auto_drive_logger.py` | cron supervisor: passively auto-logs each drive (read-only) — **temporary, see TEARDOWN** | `../../tmp/` |
| `radar_acc_sda_drive.py` | **⚠ ACTUATION** — DIY **Service Drive Alignment**: start `0x0251` + hold session + log while you drive. **This is the alignment tool to use.** | `dumps/` |
| `radar_acc_align_0251.py` | **⚠ ACTUATION** — older gated `31 01` runner whose guided flow is the **static-mirror (WRONG method here)** — superseded by `sda_drive`; kept for its preflight/abort plumbing | — |
| `perturb_monitor.py` | flag any DID that changes while nudging the housing (read-only) — **done: no live orientation signal** | — |
| `did_hunt_log.py` | log ALL readable DIDs to a wide CSV (read-only) — **dormant**, reusable to hunt new signals via the `tmp/HUNT_DIDS` marker | `../../tmp/` |

Generic discovery tools (`tools/did_sweep.py`, `routine_scan.py`, `uds_send.py`, `signal_correlate.py`)
live at the repo root and take the module key `radar_acc`.

## Subdirs
- `docs/` — `AGENT_HANDOFF.md` (read first), `radar_acc_handoff.md` (original investigation),
  `radar_acc_alfaobd_bugreport.md` (AlfaOBD mis-mapping evidence).
  - `docs/oem/` — **OEM / authoritative sources** (FCA STAR TSB for C1418-78, etc.); trust over our
    inferred findings. The TSB says C1418-78 is a seating/bumper-contact fault → fix mechanics, then calibrate.
- `findings/` — `did_map.md` (**canonical map of all 56 DIDs + sessions/security/routines/DTCs**),
  `radar_acc_did_findings.md` (narrative/analysis: angle scaling, 0x0251 mechanics, drive results).
- `dumps/` — kept captures (`radar_acc_did_sweep.txt`).

**Safety:** `radar_acc_align_0251.py` is the only actuation in the whole repo. Read the Safety &
liability section of the root README before using it.
