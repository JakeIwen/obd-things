#!/usr/bin/env python3
"""Guided runner for the ACC-radar (Bosch DASM / MRR1evo) alignment routine 0x0251.

    python3 projects/radar/radar_acc_align_0251.py        # PREFLIGHT ONLY (read-only, default)
    python3 projects/radar/radar_acc_align_0251.py --arm  # guided 3-position alignment (gated)
    python3 projects/radar/radar_acc_align_0251.py --arm --option 01
    python3 projects/radar/radar_acc_align_0251.py --abort  # send stopRoutine (31 02 0251)

  --arm runs an interactive, guided loop: physical checklist -> typed confirm -> for each
  mirror tilt POSITION (1/2/3) you set the mirror and it triggers a measurement and prints
  the deviation (0x0845/0x0850 elevation = authoritative). Whether you then NULL the error by
  turning a mechanical aim screw, or the routine STORES a software correction itself, is
  radar-dependent and UNVERIFIED for this unit -- see "MECHANICAL vs ELECTRONIC" below.

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
    * Running without the mirror -> "no valid positions found".

  MECHANICAL vs ELECTRONIC -- UNVERIFIED FOR THIS RADAR (decides if you touch a screw):
    * The "two aim screws (vertical/horizontal)" step came from the AllData/Giulia
      procedure for a DIFFERENT FCA vehicle; it is NOT confirmed for this Promaster
      Bosch MRR1evo. Owner reports NO visible external aim screws on this unit.
    * Modern Bosch MRR units are commonly FIXED-MOUNT: the routine MEASURES boresight
      against the mirror and STORES a software correction -- no screw to turn.
    * DIAGNOSTIC: after the first measurement, watch 0x0845/0x0850 elevation. If it
      moves toward 0 on its own -> electronic (no screw; just finish the 3 positions).
      If it only reports and never changes -> mechanical adjustment is expected; STOP
      and source the real Promaster (RU-body) service procedure before disassembly.

  SESSION + PARAM + LIFECYCLE (VERIFIED at runtime):
    * 31 01 0251 with NO option byte, in EXTENDED session 0x03, returns 71 01 0251.
    * Sending an option byte (we tried '01' from AlfaOBD's 0x0250) -> 7F3131/7F3113.
    * The earlier 7F317F was the 0x03 session TIMING OUT to default mid-procedure.
    * SINGLE-START routine, NOT call-per-position: a 2nd 31 01 while running -> 7F3124
      (requestSequenceError). 31 02 stops it. Re-sending 10 03 (session re-entry) also
      RESETS it -> that was the 'counter stuck at 01' bug (each capture silently restarted).
    * Status via 31 03 0251 -> 71 03 0251 <rec>: running='01 01 00 02', idle/stopped=
      '00 04 00 02'. Stayed RUNNING for 16s with a static mirror (did not self-complete).
    * CORRECT DRIVE: start ONCE, hold session with 3E (never 10 03/31 01 again), present
      the 3 mirror tilts while it runs, watch the record + stored angle. (Implemented.)

  STILL INFERRED -- confirm at runtime, then delete the tag:
    * COMPLETION criteria: never observed the record leave 'RUNNING' -- whether moving the
      mirror through +2/0/-2 advances/completes it (static-mirror model) or this unit needs
      DYNAMIC/drive alignment is UNRESOLVED. Do not assume completion stores a good value.
    * RESULT RECORD field meaning (B0..B3) beyond running/idle not decoded.
    * ANGLE SCALE: 0x0841 = signed millideg; 0x0845/0x0850 = signed microdeg int32
      pairs (elev,azim). Cross-decode gave 0x0841 ~1000x 0x0845 (RATIO confirmed);
      ABSOLUTE scale still not inclinometer-verified.
    * RESULT ENCODING: decode the positive 71 01 / 71 03 payload here once seen
      (may carry per-position deviation / screw-turn guidance).
    * SECURITY: reads need no 27. In session 0x60, 27 01/03/07/11 -> 7F2712 (absent)
      and 27 05 -> 7F277E (exists but wrong session) -- so no grantable level here,
      suggesting 31 01 0251 needs no unlock. If start returns 7F3133, security IS
      required after all -- FLAG, do NOT brute force a seed on a safety device.
    * COMPLETION: inferred (0x0845/0x0850 elevation -> ~0 and C1418-78 clears).
      NB: 0x0841 is a volatile online estimate (seen ~0 while DTC stayed active) --
      judge success off the 0x0845/0x0850 elevation pair, not 0x0841.
  ###################################################################
"""
import os
import sys
import time

# locate repo root (dir containing lib/) regardless of how deep this script lives
REPO = os.path.dirname(os.path.abspath(__file__))
while REPO != os.path.dirname(REPO) and not os.path.isdir(os.path.join(REPO, "lib")):
    REPO = os.path.dirname(REPO)
sys.path.insert(0, REPO)
from lib import uds
from lib.modules import get

ROUTINE = 0x0251
ROUTINE_OPTION = []               # VERIFIED: 0x0251 takes NO option byte. Sending one (we tried
                                  # AlfaOBD's 0x0250 '01') -> 7F3131 requestOutOfRange / 7F3113.
SPEC_DEG = 1.0                     # ~Bosch class static-alignment window (target: |vert| < this)
CONFIRM_PHRASE = "ALIGN THE RADAR"  # must be typed verbatim to arm
# VERIFIED at runtime: 31 01 0251 (no option) runs in EXTENDED session 0x03 and returns 71 01 0251.
# It is a STATEFUL multi-step calibration (capture 3 mirror positions). The earlier 7F317F was a
# RED HERRING: the 0x03 session had timed out (S3 ~5s) back to default while the operator worked
# the prompts, so 31 01 hit default. FIX: re-enter 0x03 immediately before every 31 01/31 02.
# (0x60/0x40 are real sessions but NOT where this routine starts.)
ROUTINE_SESSION = 0x03            # extended -- routine + reads both live here
READ_SESSION = 0x03

# Deviation-angle DIDs (INFERRED names/scale; see findings/radar_acc_did_findings.md).
MILLIDEG = 1.0 / 1000.0
MICRODEG = 1.0 / 1_000_000

# The 3 mirror tilt positions the routine measures at (AllData ACC Alignment Procedure).
POSITIONS = [
    ("1", "Mirror FORWARD  -- tilt cam to +2.0 deg forward"),
    ("2", "Mirror MIDDLE   -- tilt cam to  0.0 deg (centered)"),
    ("3", "Mirror BACK     -- tilt cam to -2.0 deg rearward"),
]
# Mechanical aim adjusters -- UNVERIFIED for this radar (owner reports none visible).
# Only relevant if this unit turns out to be mechanically aimed; see banner "MECHANICAL
# vs ELECTRONIC". If it stores a correction electronically, there is nothing to turn.
SCREWS = (
    "SCREW 1 = VERTICAL adjustment   <- our fault is vertical (C1418-78)",
    "SCREW 2 = HORIZONTAL adjustment",
)


def rid_bytes(rid):
    return [(rid >> 8) & 0xFF, rid & 0xFF]


def enter_session(s, sub):
    """DiagnosticSessionControl 10 <sub>. Return True on positive 50 <sub>."""
    resp, _ = uds.request(s, [0x10, sub], timeout=1.0)
    ok = bool(resp and resp[0] == 0x50)
    if not ok:
        print(f"  !! 10 {sub:02X} (session) -> {uds.hx(resp) if resp else '(none)'}  "
              f"-- could not enter session 0x{sub:02X}")
    return ok


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
    enter_session(s, READ_SESSION)              # extended -- reads only

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
        "Clear line of sight from the radar to the mirror. NO disassembly needed --",
        "  0x0251 stores the correction electronically; this radar has no aim screws.",
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
            0x7F: "serviceNotSupportedInActiveSession -> wrong session; 31 01 needs session 0x60 (enter_session should have done this -- check 10 60 was granted).",
        }
        print(f"     {hints.get(nrc, 'See UDS NRC table; routine NOT started.')}")
        return False
    print("  -> Unexpected response; routine NOT started.")
    return False


def show_deviation(s, label="deviation"):
    """Read + print the (stored) deviation angles, flagging vertical in/out of spec.
    NB: these refresh only when a measurement runs -- they do NOT track a screw turn live.

    The spec verdict is judged off the 0x0845/0x0850 ELEVATION pair, not 0x0841: 0x0841 is a
    volatile online estimate (observed drifting to ~0 while the DTC stays active), whereas
    0x0845/0x0850 are the authoritative misalignment the freeze-frame + C1418-78 track. Drive
    the elevation pair -> 0 with SCREW 1."""
    a = read_angles(s)
    print(f"  -- {label} --")
    print(fmt_angles(a))
    elevs = [a[k] for k in ("elev 0x0845", "elev 0x0850") if k in a]
    if elevs:
        elev = sum(elevs) / len(elevs)
        verdict = "WITHIN" if abs(elev) < SPEC_DEG else "OUT OF"
        print(f"     elevation {verdict} spec (mean |{elev:+.3f}| vs +/-{SPEC_DEG} deg)  <- authoritative")
    if "vert 0x0841" in a:
        print(f"     (0x0841 online estimate {a['vert 0x0841']:+.3f} deg -- reference only, volatile)")
    print()


def routine_results(s):
    """Poll 31 03 0251 (read-only) and print the status record (e.g. 71 03 0251 01 01 00 02)."""
    resp, _ = uds.request(s, [0x31, 0x03, *rid_bytes(ROUTINE)], timeout=2.0)
    if resp and resp[0] == 0x71 and len(resp) > 4:
        print(f"     results 31 03 {ROUTINE:04X}: status record = {uds.hx(resp[4:])}")
    elif resp:
        print(f"     results 31 03 {ROUTINE:04X}: {uds.hx(resp)}")
    return resp


def decode_record(resp):
    """Best-effort label for the 31 03 0251 status record. VERIFIED: running='01 01 ..',
    idle/stopped='00 04 ..'. Other states (completed/error) still UNVERIFIED."""
    if not resp or resp[0] != 0x71 or len(resp) < 6:
        return "(no record)"
    b = resp[4:]
    if b[0] == 0x01 and b[1] == 0x01:
        return f"RUNNING ({uds.hx(b)})"
    if b[0] == 0x00 and b[1] == 0x04:
        return f"idle/stopped ({uds.hx(b)})"
    if b[1] == 0x02:
        return f"possibly COMPLETED ({uds.hx(b)})"
    return f"state? ({uds.hx(b)})"


def _ask(prompt):
    try:
        return input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "q"


def guided_alignment(s):
    """Run 0x0251 as a SINGLE continuous routine (verified lifecycle): START ONCE, hold the
    session alive with TesterPresent, and present the 3 mirror tilts WHILE it runs. Do NOT
    re-send 10 03 or 31 01 mid-run -- either RESETS the routine (that was the 'counter stuck
    at 01' bug). Status record: running='01 01 ..', idle='00 04 ..'. We watch it evolve and
    read angles/DTC at the end. If nothing changes, the static-mirror model is likely wrong
    for this unit (could be DYNAMIC/drive alignment) -- stop and reassess, don't re-run blindly."""
    HOLD = 10  # seconds held at each mirror position while the routine samples
    print("== GUIDED ALIGNMENT (single continuous run -- follow the TIMED cues) ==")
    print("  Once started the run is TIMED: move the mirror on each cue while it keeps sampling.")
    print("  Do NOT touch the keyboard during the run (Ctrl-C aborts). The session is held alive")
    print("  with TesterPresent; restarting would wipe progress, so we start exactly once.\n")
    show_deviation(s, "starting deviation")

    cmd = _ask("  Set mirror to POSITION 1 (+2.0 deg forward). Press Enter to BEGIN (q=quit): ")
    if cmd == "q":
        return

    if not enter_session(s, ROUTINE_SESSION):
        print("  could not enter session; aborting."); return
    uds.request(s, [0x31, 0x02, *rid_bytes(ROUTINE)], timeout=2.0)   # reset any prior state
    print(f"  TX  : 31 01 {ROUTINE:04X} (start, no option)")
    resp, status = uds.request(s, [0x31, 0x01, *rid_bytes(ROUTINE), *ROUTINE_OPTION], timeout=5.0)
    if not interpret_start(resp, status):
        print("  start rejected; aborting."); return

    phases = [
        ("HOLD at POSITION 1  (+2.0 deg forward)", HOLD),
        (">>> MOVE NOW to POSITION 2 (0.0 deg center) <<<", HOLD),
        (">>> MOVE NOW to POSITION 3 (-2.0 deg rearward) <<<", HOLD),
        ("HOLD steady -- finalizing", HOLD),
    ]
    prev = None
    for label, secs in phases:
        print(f"\n  {label}")
        for t in range(secs, 0, -1):
            uds.request(s, [0x3E, 0x00], timeout=0.5)        # keep session alive (no 10 03!)
            rec = routine_results(s)
            tag = decode_record(rec)
            changed = "  ** record CHANGED **" if (rec and prev and bytes(rec) != bytes(prev)) else ""
            print(f"     t-{t:2d}s  {tag}{changed}")
            prev = rec
            time.sleep(1.0)

    print()
    show_deviation(s, "final deviation")
    st = c1418_status(s)
    if st is None:
        print("  C1418-78: CLEARED.")
    else:
        print(f"  C1418-78 status = 0x{st:02X} ({'still failing' if st & 0x01 else 'dormant'}).")
    print("\n  Read the trace: if the record stayed 'RUNNING' and elevation never moved, the")
    print("  static-mirror sequencing is likely not how this unit calibrates -- STOP and report")
    print("  the trace rather than re-running. Do not keep firing it blindly.")


def abort(s):
    """stopRoutine 31 02 0251 -- the safe 'cancel' direction (still actuation)."""
    enter_session(s, ROUTINE_SESSION)           # 31 02 needs the routine session too
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
