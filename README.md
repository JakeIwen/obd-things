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
  radar_acc_align_0251.py  ** ACTUATION ** gated runner for alignment routine 0x0251 (31 01) - see Safety
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

## Safety & liability

**Read this before running anything that writes to the vehicle.**

- **Almost everything here is read-only** (`22`, `19`, `31 03`) and safe to run.
- **One tool performs actuation:** `tools/radar_acc_align_0251.py` issues `31 01` (startRoutine)
  to calibrate the ACC radar. It is gated (read-only dry run by default; `--arm` + a typed
  confirmation required to fire) but it is still actuation on a **safety-critical forward-collision
  / ADAS sensor.** A mis-aimed or mis-calibrated radar can cause phantom braking or fail to detect
  an obstacle, on public roads, at speed.

**Conditions of use for the actuation tool (and any `31 01` you derive from it):**
1. **Only on a vehicle you own**, or with the **documented, informed consent of the owner.** Do not
   run actuation on another person's vehicle, a rental, a fleet vehicle, or any vehicle you are not
   authorized in writing to modify.
2. **You are solely responsible** for confirming it is legal where you are to diagnose, calibrate,
   or modify an ADAS/ESC/safety system, and for any inspection/recertification a calibration may
   require. Tampering with safety equipment may carry regulatory, insurance, and liability
   consequences. This project is not legal advice.
3. **Verify alignment before driving.** After any calibration, confirm the result (DTC cleared,
   deviation angles in spec) and treat ACC/FCW as untrusted until proven on a controlled test.
4. The routine's parameter format and angle scaling are **reverse-engineered, not from a Bosch ODX**
   — they may be wrong. See the "living script" banner in the tool.

## License & disclaimer
MIT - see [LICENSE](LICENSE). **Provided "AS IS", WITHOUT WARRANTY OF ANY KIND**; the authors and
contributors accept **no liability** for any damage, injury, loss, or legal consequence arising from
its use (this includes, expressly, use of the actuation tool above). You use it **entirely at your
own risk and on your own responsibility.** If you are not prepared to accept that risk for a
safety-critical system, run only the read-only tools.
