# projects/radar — 2022 Promaster ACC radar (Bosch DASM / MRR1evo14F)

Reverse-engineering + alignment work for the forward-looking ACC/FCW radar. Its former
**vertical-misalignment fault (DTC C1418-78, ≈ −1.26° elevation)** was repaired; ACC/FCW is functional.

> **Start with [`docs/AGENT_HANDOFF.md`](docs/AGENT_HANDOFF.md)** for the resolved fault, ruled-out
> list, and `0x0251` evidence, but obey its 2026-08-20 topology warning: its PCAN/`can0` commands
> document the completed legacy campaign and are not current dual-USBCANFD instructions. Also read
> [`docs/oem/`](docs/oem/) (authoritative — trust over inference) and the repo-root
> [`README.md`](../../README.md) (current bus ownership + RESEARCH-FIRST method).

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

## Maintained runtime

`radar_acc_live.py` is the only maintained radar-specific executable. Direct
mode is dry-run by default. Its gated live mode uses the shared scoped route
owner: it resolves C-CAN from USB identity, arms only while it owns the logical
role and channel, and restores the exact passive state. It is parked-only and
changes to session `03`, so every printed vehicle/session/routine confirmation
is mandatory. `--follow [csv]` is bus-free and only tails an existing historical
CSV; it neither starts a logger nor proves the file is fresh.

| script | what | writes |
|---|---|---|
| `radar_acc_live.py` | dry-run-first bounded parked direct view, or bus-free historical CSV follow | — |

The former baseline/drive logger, cron supervisor, DID hunt, perturbation
monitor, and both `0x0251` actuation scripts were retired and deleted after the
fault was resolved. Their filenames remain in dated findings solely to identify
how the evidence was produced. Do not recreate them from git history as a
current alignment workflow.

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

**Safety:** the dedicated radar actuators no longer exist. Generic gated
`tools/uds_send.py` can still construct arbitrary authorized payloads; that
capability is not authorization to repeat alignment. Read the root README's
Safety & liability section before any live diagnostic use.
