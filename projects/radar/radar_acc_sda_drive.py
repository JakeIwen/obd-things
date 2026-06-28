#!/usr/bin/env python3
"""DIY Service Drive Alignment (SDA) for the ACC radar -- *** PROVEN: this fixed C1418-78 on 2026-06-27. ***

OEM alignment = "Service Drive Alignment": start the radar calibration routine, then DRIVE and the radar
converges to completion. This runner: start 0x0251 ONCE, hold the session with TesterPresent for the whole
drive (never re-send 10 03 -- that RESETS the routine), log + show live progress while YOU DRIVE.

How it works / what to watch (verified -- see findings/adjustment_1_results_3.md):
  * Routine-status byte[2] is a 0-100% PROGRESS counter -- shown live as `SDA NN%`. It climbs while you
    drive a steady profile and COMMITS at 100% (~17 min at ~40 mph in the proven run).
  * At commit, C1418-78 flips 0x8F -> 0x0E (testFailed + warning bits clear) and ACC returns. The script
    detects this (testFailed dropping, debounced) AND progress==100, then plays the SUCCESS chime + stops.
  * There is NO fixed time limit (the routine itself has none). It runs until: COMMIT (success), progress
    STALLS for 10 min with no gain, the routine goes IDLE/reset, or the link drops -- the non-success ends
    play the TIMEOUT chime. Optional --minutes N adds a hard cap (default: none).
  No keyboard interaction needed while driving -- just listen for one of the two chimes (Sonos/van audio).

    python3 projects/radar/radar_acc_sda_drive.py                 # PREFLIGHT ONLY (read-only)
    python3 projects/radar/radar_acc_sda_drive.py --arm           # start + hold + log; ends on progress, not a clock
    python3 projects/radar/radar_acc_sda_drive.py --arm --minutes 30   # optional hard cap

  *** ACTUATION (31 01) on a forward-collision radar. Owner-consent only; see README "Safety & liability".
      Start it PARKED (engine running), then drive: straight, steady, ~30-45 mph, clear road, minimal turns,
      for the full duration. PAUSE the cron auto-logger first (its per-minute 10 03 RESETS the routine). ***
"""
import os, sys, time, csv, datetime
_root = os.path.dirname(os.path.abspath(__file__))
while _root != os.path.dirname(_root) and not os.path.isdir(os.path.join(_root, "lib")):
    _root = os.path.dirname(_root)
sys.path.insert(0, _root)
from lib import uds
from lib.modules import get

ROUTINE = 0x0251
SESSION = 0x03                  # verified: 0x0251 starts in extended; re-entering it RESETS the routine
CONFIRM = "RUN SDA"
MICRODEG = 1.0 / 1_000_000
MILLIDEG = 1.0 / 1000.0
KMH_TO_MPH = 0.621371
DTC_CLEAR_DEBOUNCE = 8          # consecutive VALID 'testFailed clear' reads before declaring it committed
                                #   (rejects comms-glitch false clears -- see adjustment_1_results_2.md)
SUCCESS_SOUND = "success.mp3"   # chime when the SDA commits (DTC testFailed clears / progress hits 100%)
TIMEOUT_SOUND = "warn.mp3"      # chime when the run ends WITHOUT committing (stall / routine reset / link lost)
# The routine has NO inherent time limit -- it runs while we hold the session. So instead of a fixed wall
# clock we end on PROGRESS behavior: it stalls, the routine goes idle, or the link drops.
STALL_TIMEOUT = 600             # end if best progress %% hasn't advanced for this long (10 min of no gain)
PROGRESS_ARM  = 5               # only arm idle/reset detection once progress has passed this %% (skip warmup)
IDLE_DEBOUNCE = 10              # consecutive 'routine idle' reads (B1==04) => routine was reset/aborted


def opt(flag, d=None):
    a = sys.argv[1:]
    return a[a.index(flag) + 1] if flag in a else d


def rid(r): return [(r >> 8) & 0xFF, r & 0xFF]


def fire_chime(sound):
    """Fire-and-forget the user's Sonos/van chime (play_alert in ~/canbus_funcs.sh). Needs an
    interactive bash so its aliases resolve. Never blocks/raises -- audible cue only, no keyboard needed."""
    try:
        import subprocess
        subprocess.Popen(["bash", "-ic", f"play_alert {sound}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def dtc_active(st):
    """C1418-78 currently FAILING = testFailed bit (0x01) set (0x8F). testFailed clear (0x0E) or
    absent (None) => not active. The 0x8F->0x0E commit is exactly testFailed dropping (ACC restored)."""
    return st is not None and (st & 0x01) != 0


def progress_pct(rs):
    """SDA progress 0-100 from the routine-status hex string 'B0 B1 B2 B3' -- B2 = 0x00..0x64 (verified
    2026-06-27: hits 0x64=100% at commit). None if unparseable. See findings/adjustment_1_results_3.md."""
    if not rs:
        return None
    parts = rs.split()
    if len(parts) < 3:
        return None
    try:
        return int(parts[2], 16)
    except ValueError:
        return None


def routine_idle(rs):
    """True if the routine reports NOT running. Status byte B1: 01=running, 03=completed, 04=idle/not-
    started (idle baseline '00 04 00 02'). A garbled read won't cleanly show B1==04, so this is a valid
    'routine reset/aborted' signal -- distinct from an unreadable status (which the stall timer catches)."""
    if not rs:
        return False
    p = rs.split()
    return len(p) >= 4 and p[1] == "04"


def read_did(s, did):
    r, _ = uds.request(s, [0x22, *rid(did)], timeout=0.6, retries=0)
    return r[3:] if (r and r[0] == 0x62 and len(r) >= 3) else None


def angles(s):
    d45, d50, d41, dsp = read_did(s, 0x0845), read_did(s, 0x0850), read_did(s, 0x0841), read_did(s, 0x1002)
    kmh = dsp[0] if dsp else None
    return {
        "speed_kmh": kmh,
        "speed_mph": round(kmh * KMH_TO_MPH, 1) if kmh is not None else None,
        "elev_0845": round(uds.s32(d45, 0) * MICRODEG, 4) if d45 and len(d45) >= 8 else None,
        "elev_0850": round(uds.s32(d50, 0) * MICRODEG, 4) if d50 and len(d50) >= 8 else None,
        "vert_0841": round(uds.s16(d41, 0) * MILLIDEG, 4) if d41 and len(d41) >= 2 else None,
    }


def c1418(s):
    """Read C1418-78 status. Returns (status, valid):
      (byte, True)  -> present;  (None, True) -> valid 0x59 response, DTC absent = genuinely cleared;
      (None, False) -> no/garbled response -> UNKNOWN, NOT a clear. The valid gate + debounce stop a
                       comms glitch from falsely declaring 'CLEARED' and ending the drive early."""
    r, _ = uds.request(s, [0x19, 0x02, 0xFF], timeout=1.0)
    if not r or r[0] != 0x59:
        return None, False
    b = r[3:]
    for i in range(0, len(b) - 3, 4):
        if b[i] == 0x54 and b[i + 1] == 0x18 and b[i + 2] == 0x78:
            return b[i + 3], True
    return None, True


def routine_status(s):
    r, _ = uds.request(s, [0x31, 0x03, *rid(ROUTINE)], timeout=1.0)
    return uds.hx(r[4:]) if (r and r[0] == 0x71 and len(r) > 4) else (uds.hx(r) if r else None)


def main():
    m = get("radar_acc")
    armed = "--arm" in sys.argv
    minutes = float(opt("--minutes", "0"))   # optional hard cap; 0 = none (end on progress behavior)
    s = uds.open_socket(m.txid, m.rxid, m.channel, timeout=1.5)
    uds.request(s, [0x10, SESSION], timeout=1.0)

    # preflight
    a = angles(s); st = c1418(s)[0]
    print(f"# {m.name}  SDA-drive attempt")
    print(f"  voltage   : need engine running (~14V)")
    print(f"  elevation : 0845={a['elev_0845']}  0850={a['elev_0850']}  (target -> ~0)")
    print(f"  C1418-78  : 0x{st:02X}" if st is not None else "  C1418-78  : not present")
    print(f"  routine   : {routine_status(s)}")
    if not armed:
        print("\n== DRY RUN ==  start for real with --arm, then DRIVE the profile in the header.")
        s.close(); return

    print("\n== ARMING ==  this starts 0x0251 and holds the session while you drive.")
    try:
        if input(f'  type "{CONFIRM}" to start: ').strip() != CONFIRM:
            print("  not confirmed; aborting."); s.close(); return
    except (EOFError, KeyboardInterrupt):
        print("  aborted."); s.close(); return

    # The confirm prompt above can block past the S3 timeout (~5s), dropping us back to the default
    # session where RoutineControl is rejected (7F 31 7F = serviceNotSupportedInActiveSession). Re-enter
    # extended RIGHT BEFORE start. Safe here -- no routine is running yet (the "10 03 resets a running
    # routine" rule only bites mid-drive). From here on the session is held with 3E, never 10 03.
    sresp, _ = uds.request(s, [0x10, SESSION], timeout=1.0)
    if not (sresp and sresp[0] == 0x50):
        print(f"  could not enter extended session 0x{SESSION:02X}: {uds.hx(sresp) if sresp else '(none)'}"
              f" -- aborting."); s.close(); return

    uds.request(s, [0x31, 0x02, *rid(ROUTINE)], timeout=2.0)          # reset any prior state
    resp, _ = uds.request(s, [0x31, 0x01, *rid(ROUTINE)], timeout=5.0)  # START (no option)
    if not (resp and resp[0] == 0x71):
        nrc = resp[2] if (resp and resp[0] == 0x7F and len(resp) > 2) else None
        hint = {0x7F: "service-not-in-session (S3 timed out again?)", 0x33: "security needed -> Fix path #2b",
                0x22: "conditions-not-correct (engine running? in 0x03?)", 0x24: "sequence (already running?)",
                0x31: "request-out-of-range"}.get(nrc, "")
        print(f"  START failed: {uds.hx(resp) if resp else '(none)'}"
              f"{('  -- ' + hint) if hint else ''} -- aborting."); s.close(); return
    cap_msg = f"hard cap {minutes:g} min" if minutes > 0 else "no time limit"
    print(f"  STARTED ({uds.hx(resp)}).  >>> NOW DRIVE: straight/steady ~30-45 mph, clear road. Runs until "
          f"it commits (SUCCESS chime), or progress stalls {STALL_TIMEOUT/60:g} min / routine resets "
          f"(TIMEOUT chime); {cap_msg}. Do not touch the keyboard. <<<\n")

    outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dumps")
    os.makedirs(outdir, exist_ok=True)
    outfile = os.path.join(outdir, f"sda_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    cols = ["iso_time", "elapsed_s", "speed_mph", "speed_kmh", "elev_0845", "elev_0850",
            "vert_0841", "c1418", "routine"]
    start = time.time(); last_tp = 0; n = 0
    cap_s = minutes * 60 if minutes > 0 else None   # optional hard cap; None = run until commit/stall/idle
    dtc_was_active = False     # latch: did we ever see C1418-78 actively failing (testFailed)?
    commit_streak = 0          # consecutive VALID 'testFailed clear' reads (debounces the commit detect)
    best_prog = -1; best_prog_t = start            # high-water progress + when it last advanced (stall timer)
    idle_streak = 0            # consecutive 'routine idle' reads after real progress (reset/abort detect)
    prog = None; success = False; end = None
    with open(outfile, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        try:
            while True:
                t0 = time.time()
                try:
                    if t0 - last_tp > 2.0:                  # keep session alive -- NEVER 10 03
                        uds.request(s, [0x3E, 0x00], timeout=0.4); last_tp = t0
                    a = angles(s); st, st_valid = c1418(s); rs = routine_status(s)
                except OSError:                             # USB brownout or vehicle asleep -> link gone
                    end = "link lost (USB brownout / vehicle asleep)"; break
                prog = progress_pct(rs)
                w.writerow({"iso_time": datetime.datetime.now().isoformat(timespec="seconds"),
                            "elapsed_s": round(t0 - start, 1), **a, "c1418": st, "routine": rs})
                f.flush(); n += 1

                # commit tracking: SDA is done when testFailed drops (0x8F->0x0E) -- NOT only when the DTC
                # goes fully absent (the real commit leaves a 0x0E history record). Debounced + valid-gated.
                if dtc_active(st):
                    dtc_was_active = True; commit_streak = 0
                elif st_valid:                              # valid read, testFailed bit clear
                    commit_streak += 1
                else:
                    commit_streak = 0                       # garbled read -> don't count

                # progress high-water (stall timer) + routine-idle/reset detection (only after real progress)
                if prog is not None and prog > best_prog:
                    best_prog = prog; best_prog_t = t0
                if best_prog >= PROGRESS_ARM and routine_idle(rs):
                    idle_streak += 1
                else:
                    idle_streak = 0

                dtc = "----" if st is None else f"0x{st:02X}"
                mph = a["speed_mph"]
                pstr = f"SDA {prog:3d}%" if prog is not None else "SDA  ??%"
                noadv = int(t0 - best_prog_t)               # seconds since progress last advanced
                print(f"\r  t+{int(t0-start):4d}s {('%4.0f'%mph) if mph is not None else '  ? '}mph  "
                      f"0845 {a['elev_0845']}  DTC {dtc}  {pstr}(max {max(best_prog,0)}%, +{noadv}s)  rt[{rs}]   ",
                      end="", flush=True)

                # --- end conditions (no fixed time limit; the routine has none) ---
                committed = dtc_was_active and commit_streak >= DTC_CLEAR_DEBOUNCE
                if committed or (prog is not None and prog >= 100):
                    success = True
                    shown = "absent" if st is None else f"0x{st:02X}"
                    why = "testFailed cleared" if committed else "progress 100%"
                    print(f"\n\n  *** SDA COMMITTED ({why}; DTC {shown}; progress {prog}%) at "
                          f"t+{int(t0-start)}s -- ACC should return! -- playing {SUCCESS_SOUND} ***")
                    fire_chime(SUCCESS_SOUND); break
                if idle_streak >= IDLE_DEBOUNCE:
                    end = f"routine went IDLE at {best_prog}% (reset/aborted -- session drop? re-arm)"; break
                if (t0 - best_prog_t) >= STALL_TIMEOUT:
                    end = f"progress STALLED at {best_prog}% for {STALL_TIMEOUT/60:g} min (drive steadier / retry)"; break
                if cap_s is not None and (t0 - start) >= cap_s:
                    end = f"hit --minutes {minutes:g} cap at {best_prog}%"; break
                time.sleep(1.0)
            if not success:
                print(f"\n\n  *** SDA did NOT commit -- {end} -- (max progress {best_prog}%) "
                      f"-- playing {TIMEOUT_SOUND} ***")
                fire_chime(TIMEOUT_SOUND)
        except KeyboardInterrupt:
            print("\n  stopped by user.")
        finally:
            stf = c1418(s)[0]
            print(f"\n  final: elevation {angles(s)} | C1418-78 "
                  f"{('0x%02X'%stf) if stf is not None else 'CLEARED'} | {n} samples -> {outfile}")
            uds.request(s, [0x10, 0x01], timeout=0.5); s.close()


if __name__ == "__main__":
    main()
