# obd-things

UDS/CAN diagnostics for a 2022 Ram Promaster over a PEAK PCAN-USB + SocketCAN. Started as ACC-radar
(Bosch DASM / MRR1evo) alignment work; structured to extend to other modules (PCM, etc.).

> **New here (human or agent)? Read [`docs/AGENT_HANDOFF.md`](docs/AGENT_HANDOFF.md) first** — it's
> the current-state summary (verified facts, findings, gotchas, open work) so you don't re-derive anything.

## Layout
```
bringup.sh                 generic: bring up can0 @ 500k (listen-only OFF), liveness check
lib/                       generic, module-agnostic plumbing
  uds.py                   ISO-TP socket, UDS request, NRC table, decoders, USB-drop recovery
  modules.py               module registry (addressing). ADD A MODULE HERE to extend.
live_data/                 top-style live viewers
  live_data.py             BASE viewer: pass a module + a Metric table, get a live display
  radar_acc.py             ACC-radar sub-script (its DID metric table); imports the base
tools/                     generic scanners + ad-hoc + per-module baselines
  did_sweep.py             generic ReadDataByIdentifier sweep   -> dumps/<module>_did_sweep.txt
  routine_scan.py          generic RoutineControl scan (31 03, read-only)
  uds_send.py              ad-hoc read-only UDS request
  radar_acc_baseline.py    ACC-radar baseline reproduction (radar-specific)
dumps/                     raw captured data (sweeps + run logs)
findings/                  extracted / inferred data (decoded DID meanings, scaling)
docs/                      narrative docs (handoff, bug report)
```

## Quick start
```bash
./bringup.sh                              # ignition ON (engine running ideal)
python3 tools/radar_acc_baseline.py       # confirm the link
python3 live_data/radar_acc.py            # live alignment gauge @ 5 Hz
```

## Adding another module
1. Add a `Module(...)` entry to `lib/modules.py`.
2. For live data: copy `live_data/radar_acc.py`, swap the module key + `METRICS`.
3. Generic tools already work: `python3 tools/did_sweep.py <key>`, `routine_scan.py <key>`, `uds_send.py <key> ...`.

## Safety
Everything here is read-only (`22`, `19`, `31 03`). `31 01` (startRoutine / actuation) is intentionally
not implemented in any tool. The ACC radar is a forward-collision sensor - see `docs/`.

## License
MIT - see [LICENSE](LICENSE). Provided as-is, no warranty; use at your own risk on your own vehicle.
