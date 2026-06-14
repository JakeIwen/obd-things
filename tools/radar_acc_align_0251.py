#!/usr/bin/env python3
"""Guided runner for the ACC-radar (Bosch DASM / MRR1evo) alignment routine 0x0251.

    python3 tools/radar_acc_align_0251.py                 # PREFLIGHT ONLY (read-only, default)
    python3 tools/radar_acc_align_0251.py --arm           # guided 3-position alignment (gated)
    python3 tools/radar_acc_align_0251.py --arm --option 01
    python3 tools/radar_acc_align_0251.py --abort         # send stopRoutine (31 02 0251)

  --arm runs an interactive, guided loop: physical checklist -> typed confirm -> for each
  mirror tilt POSITION (1/2/3) you set the mirror and it triggers a measurement and prints
  the deviation; iterate, adjusting SCREW 1 (vertical), until vertical deviation -> ~0.

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
  PROCEDURE (from AllData "ACC Alignment Procedure", Bosch MRR; matches DIY reports):
    * Static mirror reflection method. Flat mirror at 120 cm (+/-5 cm), squared to
      the vehicle centerline, mirror CENTER at the radar's vertical-center height.
    * Calibration = THREE measurements, changing the mirror tilt each time:
      POSITION 1 = +2 deg forward, POSITION 2 = 0 deg, POSITION 3 = -2 deg back.
    * Two radar screws (front grille access panel): SCREW 1 = vertical (our fault
      C1418-78), SCREW 2 = horizontal. Tool gives turns/direction; iterate (~7
      rounds typical). Running without the mirror -> "no valid positions found".

  STILL INFERRED -- confirm at runtime, then delete the tag:
    * OPTION/PARAM (ROUTINE_OPTION): we send option 0x01. Whether 0x0251 wants ONE
      start with internal sequencing or a per-position option byte is UNCONFIRMED.
      If start returns 7F3131 (requestOutOfRange), the option is wrong -- record it.
    * ANGLE SCALE: 0x0841 = signed millideg; 0x0845/0x0850 = signed microdeg int32
      pairs (elev,azim). Cross-decode gave 0x0841 ~1000x 0x0845 (RATIO confirmed);
      ABSOLUTE scale still not inclinometer-verified.
    * RESULT ENCODING: decode the positive 71 01 / 71 03 payload here once seen
      (may carry per-position deviation / screw-turn guidance).
    * SECURITY: reads need no 27; 27 01 -> 7F2712 (no level-1 seed in extended
      session). If start returns 7F3133, security IS required -- FLAG, don't brute.
    * COMPLETION: inferred (vertical deviation -> ~0 and C1418-78 clears).
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

# The 3 mirror tilt positions the routine measures at (AllData ACC Alignment Procedure).
POSITIONS = [
    ("1", "Mirror FORWARD  -- tilt cam to +2.0 deg forward"),
    ("2", "Mirror MIDDLE   -- tilt cam to  0.0 deg (centered)"),
    ("3", "Mirror BACK     -- tilt cam to -2.0 deg rearward"),
]
SCREWS = (
    "SCREW 1 = VERTICAL adjustment   <- our fault is vertical (C1418-78)",
    "SCREW 2 = HORIZONTAL adjustment",
)


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
    print("== PHYSICAL CHECKLIST (AllData ACC Alignment Procedure -- do before arming) ==")
    for line in [
        "Owner has consented to running the calibration routine.",
        "Tire pressures correct; steering wheel centered; normal/unladen.",
        "Floor incline <= 3 deg downhill / 1 deg uphill; >= 3 m (10 ft) clear ahead.",
        "Engine RUNNING (stable ~14 V); doors closed, vehicle stationary.",
        "Mirror squared to vehicle centerline at 120 cm (+/-5 cm) from the sensor.",
        "Mirror CENTER set to the same height as the radar's vertical center.",
        "Mirror stand levelled; tilt cam will be set to each POSITION below in turn.",
        "Front grille access panel removed so SCREW 1 (vertical) is reachable.",
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
        print("  -> POSITIVE (71). Measurement accepted.")
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


def show_deviation(s, label="deviation"):
    """Read + print the (stored) deviation angles, flagging vertical in/out of spec.
    NB: these refresh only when a measurement runs -- they do NOT track a screw turn live."""
    a = read_angles(s)
    vert = a.get("vert 0x0841")
    print(f"  -- {label} --")
    print(fmt_angles(a))
    if vert is not None:
        verdict = "WITHIN" if abs(vert) < SPEC_DEG else "OUT OF"
        print(f"     vertical {verdict} spec (|{vert:+.3f}| vs +/-{SPEC_DEG} deg)")
    print()


def measure_at_position(s):
    """Trigger one measurement (31 01 0251 <opt>) and narrate. Whether each mirror position
    needs its own option byte is UNCONFIRMED -- see living-script banner."""
    print(f"  TX  : 31 01 {ROUTINE:04X} {uds.hx(ROUTINE_OPTION)}")
    resp, status = uds.request(s, [0x31, 0x01, *rid_bytes(ROUTINE), *ROUTINE_OPTION], timeout=5.0)
    return interpret_start(resp, status)


def _ask(prompt):
    try:
        return input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "q"


def guided_alignment(s):
    """Walk the 3-mirror-position measurement + screw-adjust loop (AllData ACC procedure).
    The deviation readout each round is feedback the OEM tool does NOT give."""
    print("== GUIDED ALIGNMENT ==")
    print("  Radar adjustment screws (front grille access panel):")
    for ln in SCREWS:
        print(f"    {ln}")
    print("  Adjust in SMALL steps (as little as 1/4 turn). Expect several iterations (~7 normal).")
    print("  The OEM tool shows no numbers -- it just says which screw/turns. We show the")
    print("  deviation DIDs each round, so adjust SCREW 1 to drive vertical -> 0.\n")
    show_deviation(s, "starting deviation")

    iteration = 0
    while True:
        iteration += 1
        print(f"----- ITERATION {iteration}: take the 3 mirror measurements -----")
        for num, desc in POSITIONS:
            while True:
                cmd = _ask(f"  POSITION {num}: set {desc}.  [Enter]=measure  s=skip  q=quit: ")
                if cmd == "q":
                    print("\n  guided alignment ended by user.")
                    return
                if cmd == "s":
                    break
                ok = measure_at_position(s)
                show_deviation(s, f"after position {num}")
                if ok:
                    break
                print("     (not accepted -- re-set the mirror and retry, or s to skip.)")
        show_deviation(s, f"end of iteration {iteration}")
        cmd = _ask("  Adjust SCREW 1 (vertical) per the above, then [Enter] to re-measure, q to finish: ")
        if cmd == "q":
            return


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

        guided_alignment(s)
        post_check(s)
    finally:
        s.close()


if __name__ == "__main__":
    main()
