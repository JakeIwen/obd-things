# projects/radar — 2022 Promaster ACC radar (Bosch DASM / MRR1evo14F)

Reverse-engineering + alignment work for the forward-looking ACC/FCW radar. The radar has an
**active vertical-misalignment fault (DTC C1418-78, ≈ −1.26° elevation)** that disables ACC/FCW.

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
| `radar_acc_baseline.py` | reproduce UDS baseline: session, key DIDs, serial, DTCs (read-only) | — |
| `radar_acc_live.py` | live alignment/health gauge. Direct @5 Hz = a bus tester (don't run during cron logging). **`--follow`** tails the newest cron drive CSV (NO bus access) — use this to watch `0845` live mid-drive | — |
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
