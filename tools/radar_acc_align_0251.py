#!/usr/bin/env python3
"""Guided runner for the ACC-radar (Bosch DASM / MRR1evo) alignment routine 0x0251.

    python3 tools/radar_acc_align_0251.py                 # PREFLIGHT ONLY (read-only, default)
    python3 tools/radar_acc_align_0251.py --arm           # actually start the routine (gated)
    python3 tools/radar_acc_align_0251.py --arm --option 01
    python3 tools/radar_acc_align_0251.py --abort         # send stopRoutine (31 02 0251)

================================================================================
  THIS IS THE ONE TOOL IN THIS REPO THAT PERFORMS ACTUATION (UDS 31 01).
  Every other tool is read-only by design. Running 0x0251 calibrates a
  forward-collision radar; a mis-aimed radar causes phantom braking / missed
  detection. Requires: owner consent, a flat mirror at 120 cm (+/-5 cm) squared
  to vehicle centerline, level ground, engine running (~14 V), normal tire
  pressure and load. See docs/AGENT_HANDOFF.md sec "Open work" item 3.

  LIABILITY: run ONLY on a vehicle you own or are authorized IN WRITING to
  modify. You are solely responsible for the legality and consequences of
  calibrating a safety/ADAS system where you are. Provided AS IS, NO WARRANTY,
  NO LIABILITY (MIT LICENSE + README "Safety & liability"). Use at your own risk.
================================================================================

  ### LIVING SCRIPT -- UPDATE THIS FILE AS RUNTIME TEACHES US MORE ###
  Much of the 0x0251 protocol below is INFERRED, not confirmed against a Bosch
  ODX. The moment we run it for real (or finish the perturbation test, or get an
  AlfaOBD cross-check), edit the relevant constant/branch here and delete the
  "(GUESS)" / "(INFERRED)" tag so the next run is authoritative. Specifically:

    * OPTION/PARAM FORMAT (ROUTINE_OPTION): we mirror AlfaOBD's 0x0250 call and
      send option byte 0x01 ("position 1"). 0x0251's real routineControlOption-
      Record is unconfirmed. If start returns 7F31 31 (requestOutOfRange), the
      option bytes are wrong -- record what works.
    * ANGLE SCALE (MILLIDEG/MICRODEG): inferred from internal consistency + the
      DTC, not a data dictionary. Pin it via the perturbation test, then fix the
      divisors in read_angles().
    * PROGRESS/RESULT ENCODING: we don't know what 31 03 0251 returns mid-run.
      Capture the positive 71 03 payload and decode it here once seen.
    * SECURITY ACCESS: prior reads needed no 27 unlock. If start returns 7F31 33
      (securityAccessDenied), a seed/key is required -- FLAG IT, do not brute
      force; record the requirement and stop.
    * COMPLETION CRITERIA: "done" is currently inferred (angles -> ~0 and/or
      C1418-78 clears). Replace with the real terminal response once observed.
  ###################################################################
"""
import os
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)
from lib import uds
from lib.modules import get

ROUTINE = 0x0251
ROUTINE_OPTION = [0x01]            # (GUESS) mirrors AlfaOBD's 31 01 0250 01; confirm at runtime
SPEC_DEG = 1.0                     # ~Bosch class static-alignment window (target: |vert| < this)
CONFIRM_PHRASE = "ALIGN THE RADAR"  # must be typed verbatim to arm

# Deviation-angle DIDs (INFERRED names/scale; see findings/radar_acc_did_findings.md).
MILLIDEG = 1.0 / 1000.0
MICRODEG = 1.0 / 1_000_000


def rid_bytes(rid):
    return [(rid >> 8) & 0xFF, rid & 0xFF]


def read_did(s, did):
    """Return the data bytes after `62 <did_hi> <did_lo>`, or None on any non-positive reply."""
    resp, _ = uds.request(s, [0x22, *rid_bytes(did)])
    if resp and resp[0] == 0x62 and len(resp) >= 3:
        return resp[3:]
    return None


def read_angles(s):
    """Current deviation angles in degrees. Scale is INFERRED -- see banner."""
    out = {}
    d = read_did(s, 0x0841)
    if d and len(d) >= 2:
        out["vert 0x0841"] = uds.s16(d, 0) * MILLIDEG
    d = read_did(s, 0x0845)
    if d and len(d) >= 8:
        out["elev 0x0845"] = uds.s32(d, 0) * MICRODEG
        out["azim 0x0845"] = uds.s32(d, 4) * MICRODEG
    d = read_did(s, 0x0850)
    if d and len(d) >= 8:
        out["elev 0x0850"] = uds.s32(d, 0) * MICRODEG
        out["azim 0x0850"] = uds.s32(d, 4) * MICRODEG
    return out


def fmt_angles(a):
    if not a:
        return "    (no angle DIDs answered)"
    return "\n".join(f"    {k:14s}: {v:+.4f} deg" for k, v in a.items())


def c1418_status(s):
    """Return the status byte of DTC C1418-78 (bytes 54 18 78) from 19 02 FF, or None."""
    resp, _ = uds.request(s, [0x19, 0x02, 0xFF])
    if not resp or resp[0] != 0x59:
        return None
    body = resp[3:]
    for i in range(0, len(body) - 3, 4):
        if body[i] == 0x54 and body[i + 1] == 0x18 and body[i + 2] == 0x78:
            return body[i + 3]
    return None  # not present (would mean cleared)


def preflight(s):
    """All read-only. Print bus health, confirm 0x0251 exists, show starting angles + DTC."""
    print("== PREFLIGHT (read-only) ==\n")
    uds.request(s, [0x10, 0x03], timeout=1.0)   # extended session

    volt = read_did(s, 0x1006)
    temp = read_did(s, 0x0835)
    v = volt[0] * 0.1 if volt else None
    t = temp[0] - 40 if temp else None
    print(f"  Control-module voltage : {v:.1f} V" if v is not None else "  voltage: (no reply)")
    print(f"  ECU internal temp      : {t} C" if t is not None else "  temp: (no reply)")
    if v is not None and v < 13.0:
        print("  !! voltage low -- engine should be RUNNING for a stable ~14 V during alignment.")

    resp, status = uds.request(s, [0x31, 0x03, *rid_bytes(ROUTINE)])
    print(f"\n  RoutineControl 31 03 {ROUTINE:04X}: {uds.hx(resp) if resp else '(none)'}  -> {status}")
    routine_ok = False
    if resp and resp[0] == 0x7F and len(resp) >= 3 and resp[2] == 0x24:
        print("     7F 31 24 = requestSequenceError -> routine EXISTS, not started. GOOD.")
        routine_ok = True
    elif resp and resp[0] == 0x71:
        print("     71 .. = routine answered results (may already be running). Inspect before arming.")
        routine_ok = True
    else:
        print("     !! Did NOT get the expected 7F3124. Do not arm until this is understood.")

    st = c1418_status(s)
    if st is None:
        print("\n  DTC C1418-78: NOT present (already cleared?) -- alignment may be unnecessary.")
    else:
        active = bool(st & 0x01)  # testFailed bit
        print(f"\n  DTC C1418-78 status = 0x{st:02X} ({'ACTIVE/failing' if active else 'dormant'})")

    print("\n  Starting deviation angles:")
    print(fmt_angles(read_angles(s)))
    print()
    return routine_ok


def checklist():
    print("== PHYSICAL CHECKLIST (do these before arming) ==")
    for line in [
        "Vehicle on LEVEL ground, normal tire pressures, normal/empty load.",
        "Engine RUNNING (stable ~14 V); doors closed, vehicle stationary.",
        "Flat mirror squared to the vehicle centerline, 120 cm (+/-5 cm) from the sensor.",
        "Mirror in POSITION 1 (forward position), as AlfaOBD's instructions specify.",
        "Owner has consented to running the calibration routine.",
    ]:
        print(f"  [ ] {line}")
    print()


def confirm_armed():
    print("== ARMING ==")
    print(f"  About to send actuation: 31 01 {ROUTINE:04X} {uds.hx(ROUTINE_OPTION)}  (startRoutine)")
    print("  This calibrates a forward-collision radar and can invalidate current alignment state.")
    try:
        typed = input(f'  Type "{CONFIRM_PHRASE}" to proceed (anything else aborts): ').strip()
    except (EOFError, KeyboardInterrupt):
        typed = ""
    if typed != CONFIRM_PHRASE:
        print("  -> not confirmed; aborting without sending anything.\n")
        return False
    return True


def interpret_start(resp, status):
    """Narrate the start response, mapping NRCs to what to do next."""
    print(f"\n  RX  : {uds.hx(resp) if resp else '(none)'}")
    print(f"  stat: {status}")
    if not resp:
        print("  -> No response. Check link/ignition; routine NOT started.")
        return False
    if resp[0] == 0x71:
        print("  -> POSITIVE (71). Routine STARTED. Monitoring convergence; Ctrl-C to stop.")
        return True
    if resp[0] == 0x7F and len(resp) >= 3:
        nrc = resp[2]
        name = uds.NRC.get(nrc, "?")
        print(f"  -> NEGATIVE 7F31 {nrc:02X} ({name}).")
        hints = {
            0x31: "requestOutOfRange -> option/param bytes are wrong for 0x0251. Try other ROUTINE_OPTION; record what works (see banner).",
            0x22: "conditionsNotCorrect -> preconditions unmet (engine/voltage/motion/mirror). Fix and retry.",
            0x33: "securityAccessDenied -> a 27 seed/key unlock is required. FLAG IT, do NOT brute force. Stop and record.",
            0x24: "requestSequenceError -> wrong sequence/session; ensure extended session (10 03) first.",
            0x12: "subFunctionNotSupported -> startRoutine not allowed here; re-verify the routine.",
            0x13: "len/format -> ROUTINE_OPTION length wrong.",
        }
        print(f"     {hints.get(nrc, 'See UDS NRC table; routine NOT started.')}")
        return False
    print("  -> Unexpected response; routine NOT started.")
    return False


def monitor(s, interval=1.0):
    """Read 31 03 0251 + deviation angles in a loop; keepalive 3E 00; until Ctrl-C / convergence."""
    print("\n== MONITORING (read-only loop) ==  Ctrl-C to stop.\n")
    last_tp = time.time()
    try:
        while True:
            if time.time() - last_tp > 2.0:
                uds.request(s, [0x3E, 0x00], timeout=0.5)
                last_tp = time.time()
            resp, status = uds.request(s, [0x31, 0x03, *rid_bytes(ROUTINE)])
            a = read_angles(s)
            vert = a.get("vert 0x0841")
            conv = "" if vert is None else ("  <- WITHIN SPEC" if abs(vert) < SPEC_DEG else "  (out of spec)")
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] 31 03 {ROUTINE:04X}: {uds.hx(resp) if resp else '(none)'} ({status})")
            print(fmt_angles(a) + conv + "\n")
            # COMPLETION CRITERIA below is INFERRED -- replace once the real terminal response is known.
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n  monitoring stopped by user.")


def abort(s):
    """stopRoutine 31 02 0251 -- the safe 'cancel' direction (still actuation)."""
    uds.request(s, [0x10, 0x03], timeout=1.0)
    resp, status = uds.request(s, [0x31, 0x02, *rid_bytes(ROUTINE)])
    print(f"stopRoutine 31 02 {ROUTINE:04X}")
    print(f"  RX  : {uds.hx(resp) if resp else '(none)'}")
    print(f"  stat: {status}")


def post_check(s):
    print("== POST-RUN CHECK (read-only) ==")
    st = c1418_status(s)
    if st is None:
        print("  DTC C1418-78: NOT present -> CLEARED. (Confirm ACC/FCW restored after a drive cycle.)")
    else:
        active = bool(st & 0x01)
        print(f"  DTC C1418-78 status = 0x{st:02X} ({'still ACTIVE' if active else 'dormant'}).")
    print("  Final deviation angles:")
    print(fmt_angles(read_angles(s)))
    print()


def main():
    args = sys.argv[1:]
    key = next((a for a in args if not a.startswith("-")), "radar_acc")
    armed = "--arm" in args
    do_abort = "--abort" in args
    if "--option" in args:
        i = args.index("--option")
        ROUTINE_OPTION[:] = [int(x, 16) for x in args[i + 1].split()]

    module = get(key)
    print(f"# {module.name}  TX={module.txid:08X} RX={module.rxid:08X}")
    print(f"# routine 0x{ROUTINE:04X}  option={uds.hx(ROUTINE_OPTION)}\n")
    s = uds.open_socket(module.txid, module.rxid, module.channel, timeout=2.0)

    try:
        if do_abort:
            abort(s)
            return

        routine_ok = preflight(s)

        if not armed:
            print("== DRY RUN ==  (no actuation sent)")
            print(f"  To start for real: python3 {os.path.relpath(__file__, REPO)} --arm")
            print(f"  Would send: 31 01 {ROUTINE:04X} {uds.hx(ROUTINE_OPTION)}  (startRoutine)\n")
            return

        if not routine_ok:
            print("Refusing to arm: preflight did not confirm 0x0251. Investigate first.\n")
            return

        checklist()
        if not confirm_armed():
            return

        print(f"\n  TX  : 31 01 {ROUTINE:04X} {uds.hx(ROUTINE_OPTION)}")
        resp, status = uds.request(s, [0x31, 0x01, *rid_bytes(ROUTINE), *ROUTINE_OPTION], timeout=5.0)
        if interpret_start(resp, status):
            monitor(s)
            post_check(s)
    finally:
        s.close()


if __name__ == "__main__":
    main()
