# projects/radar — 2022 Promaster ACC radar (Bosch DASM / MRR1evo14F)

Reverse-engineering + alignment work for the forward-looking ACC/FCW radar. Its former
**vertical-misalignment fault (DTC C1418-78, ≈ −1.26° elevation)** was repaired; ACC/FCW is functional.

> **Start with [`docs/AGENT_HANDOFF.md`](docs/AGENT_HANDOFF.md)** — current-state summary incl. a
> prominent **⛔ RULED OUT (do-not-retry)** list, the verified model, the fix path, `0x0251` mechanics,
> and the ⚠ TEARDOWN section. Also read [`docs/oem/`](docs/oem/) (authoritative — trust over inference)
> and the repo-root [`README.md`](../../README.md) (bus facts + RESEARCH-FIRST method).

## ✅ RESOLVED (2026-06-27) — see `findings/adjustment_1_results_3.md`
C1418-78 is **cleared and ACC/FCW works again.** Path that fixed it: a **physical nudge** (~1.3°) brought
the −1.26° boresight back inside the radar's auto-align window (drive #1: −1.26°→+0.28°), then the **DIY
Service Drive Alignment** — `radar_acc_sda_drive.py --arm` started routine `0x0251`, held the session with
`3E`, and we drove steady ~40 mph ~17 min. The routine's **progress counter** (status byte[2], 0–100%) hit
100% and committed: DTC `0x8F`→`0x0E` (testFailed + warning cleared), held on the next drive. **Pure-UDS,
local, no wiTECH / no shop.** Below is the original investigation (kept for context).

## Original conclusion (kept for context — superseded by RESOLVED above)
The radar stored a vertical boresight error ≈ **−1.26°** → DTC C1418-78 → ACC/FCW off. The OEM fix is a
**dynamic "Service Drive Alignment" (SDA), NOT a static mirror** (that premise was a Giulia doc, ruled
out). The radar self-aligns small deviations while driving but only within a **limited window**; −1.26°
was beyond it (a 2-hr highway drive did not move it). So the **gate was physical**: re-seat/level the mount
to get back inside the window, then the SDA finishes it. **Van = home → no shop.** Full detail + decoded
DIDs in `findings/radar_acc_did_findings.md` and `docs/oem/`.

## Scripts (run from the repo root, e.g. `python3 projects/radar/<script>`)
| script | what | writes |
|---|---|---|
| `radar_acc_baseline.py` | reproduce UDS baseline: session, key DIDs, serial, DTCs (active non-mutating diagnostics) | — |
| `radar_acc_live.py` | dry-runs a bounded parked direct-view plan by default; live direct mode needs all printed gates. **`--follow [csv]`** is the only bus-free/mid-drive view and tails an existing CSV; it neither starts a logger nor proves the file is fresh | — |
| `radar_acc_drive_log.py` | log angles + DTC + **speed (DID 0x1002)** to CSV while driving (active non-mutating UDS) | `../../tmp/radar/` |
| `auto_drive_logger.py` | cron supervisor: passive trigger, then auto-arms and starts a non-mutating but active UDS drive logger — **temporary, see TEARDOWN** | `../../tmp/` |
| `radar_acc_sda_drive.py` | **⚠ ACTUATION** — DIY **Service Drive Alignment**: start `0x0251` + hold session + log while you drive. **This is the alignment tool to use.** | `../../tmp/radar/` |
| `radar_acc_align_0251.py` | **⚠ ACTUATION** — older gated `31 01` runner whose guided flow is the **static-mirror (WRONG method here)** — superseded by `sda_drive`; kept for its preflight/abort plumbing | — |
| `perturb_monitor.py` | flag any DID that changes while nudging the housing (active non-mutating UDS) — **done: no live orientation signal** | — |
| `did_hunt_log.py` | log ALL readable DIDs to a wide CSV (active non-mutating UDS) — **dormant**, reusable to hunt new signals via the `tmp/HUNT_DIDS` marker | `../../tmp/` |

Generic discovery tools (`tools/did_sweep.py`, `routine_scan.py`, `uds_send.py`, `signal_correlate.py`)
live at the repo root and take the module key `radar_acc`.

## Subdirs
- `docs/` — `AGENT_HANDOFF.md` (read first), `radar_acc_handoff.md` (original investigation),
  `radar_acc_alfaobd_bugreport.md` (AlfaOBD mis-mapping evidence).
  - `docs/oem/` — **OEM / authoritative sources** (FCA STAR TSB for C1418-78, etc.); trust over our
    inferred findings. The TSB says C1418-78 is a seating/bumper-contact fault → fix mechanics, then calibrate.
- `findings/` — `did_map.md` (**canonical map of all 56 DIDs + sessions/security/routines/DTCs**),
  `radar_acc_did_findings.md` (narrative/analysis: angle scaling, 0x0251 mechanics, drive results),
  plus promoted captures (`radar_acc_did_sweep.txt`/`.log`, `sda_20260627_225708.csv`).

**Safety:** `radar_acc_sda_drive.py` and `radar_acc_align_0251.py` are the dedicated radar actuators;
generic gated `tools/uds_send.py` can also send arbitrary authorized payloads. Read the Safety & liability
section of the root README before using any of them.
