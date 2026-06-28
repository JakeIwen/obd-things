#!/usr/bin/env python3
"""DIY Service Drive Alignment (SDA) attempt for the ACC radar -- the one experiment we never ran.

OEM alignment = "Service Drive Alignment": a scan tool starts the radar calibration routine, then you
DRIVE and the radar converges. Every prior 0x0251 attempt here was PARKED, so it just sat "RUNNING".
This runner does the missing test: start 0x0251 ONCE, hold the diagnostic session alive with
TesterPresent for the whole drive (never re-send 10 03 -- that RESETS the routine), and log the
elevation / DTC / routine-status while YOU DRIVE. If the radar does the SDA itself, 0845/0850 should
converge toward 0 and C1418-78 clear. If it stays pinned / RUNNING forever, the commit likely needs the
wiTECH cloud/server side and the pure-UDS path is blocked -- either way this answers it, for free.

    python3 projects/radar/radar_acc_sda_drive.py                 # PREFLIGHT ONLY (read-only)
    python3 projects/radar/radar_acc_sda_drive.py --arm           # start routine + hold session + log
    python3 projects/radar/radar_acc_sda_drive.py --arm --minutes 20

  *** ACTUATION (31 01) on a forward-collision radar. Owner-consent only; see README "Safety & liability".
      ACC/FCW is already disabled by the active DTC, so the radar is inert -- no phantom-braking risk during
      the attempt. Start it PARKED (engine running), then drive: straight, steady, ~30-45 mph, clear road,
      minimal turns, for the full duration. No keyboard interaction needed while driving (solo is fine). ***
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
SPEC_DEG = 1.0
DTC_CLEAR_DEBOUNCE = 8          # consecutive VALID 'C1418 absent' reads before declaring it cleared
                                #   (rejects comms-glitch false clears -- see adjustment_1_results_2.md)


def opt(flag, d=None):
    a = sys.argv[1:]
    return a[a.index(flag) + 1] if flag in a else d


def rid(r): return [(r >> 8) & 0xFF, r & 0xFF]


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
    minutes = float(opt("--minutes", "20"))
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

    uds.request(s, [0x31, 0x02, *rid(ROUTINE)], timeout=2.0)          # reset any prior state
    resp, _ = uds.request(s, [0x31, 0x01, *rid(ROUTINE)], timeout=5.0)  # START (no option)
    if not (resp and resp[0] == 0x71):
        print(f"  START failed: {uds.hx(resp) if resp else '(none)'} -- aborting."); s.close(); return
    print(f"  STARTED ({uds.hx(resp)}).  >>> NOW DRIVE: straight/steady ~30-45 mph, clear road, "
          f"~{minutes:g} min. Do not touch the keyboard. <<<\n")

    outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dumps")
    os.makedirs(outdir, exist_ok=True)
    outfile = os.path.join(outdir, f"sda_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    cols = ["iso_time", "elapsed_s", "speed_mph", "speed_kmh", "elev_0845", "elev_0850",
            "vert_0841", "c1418", "routine"]
    start = time.time(); deadline = start + minutes * 60; last_tp = 0; n = 0; base_elev = None
    clear_streak = 0          # consecutive VALID 'C1418 absent' reads (debounces the cleared-detect)
    with open(outfile, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        try:
            while time.time() < deadline:
                t0 = time.time()
                if t0 - last_tp > 2.0:                      # keep session alive -- NEVER 10 03
                    uds.request(s, [0x3E, 0x00], timeout=0.4); last_tp = t0
                a = angles(s); st, st_valid = c1418(s); rs = routine_status(s)
                elevs = [a[k] for k in ("elev_0845", "elev_0850") if a[k] is not None]
                emean = sum(elevs) / len(elevs) if elevs else None
                if base_elev is None and emean is not None:
                    base_elev = emean
                w.writerow({"iso_time": datetime.datetime.now().isoformat(timespec="seconds"),
                            "elapsed_s": round(t0 - start, 1), **a, "c1418": st, "routine": rs})
                f.flush(); n += 1
                dtc = "----" if st is None else f"0x{st:02X}"
                mph = a["speed_mph"]
                conv = "" if (emean is None or base_elev is None) else f" Δ{emean-base_elev:+.3f}"
                print(f"\r  t+{int(t0-start):4d}s {('%4.0f'%mph) if mph is not None else '  ? '}mph  "
                      f"0845 {a['elev_0845']}  0850 {a['elev_0850']}  DTC {dtc}  rt[{rs}]{conv}   ",
                      end="", flush=True)
                if st is None and st_valid:
                    clear_streak += 1
                    if clear_streak >= DTC_CLEAR_DEBOUNCE:
                        print(f"\n\n  *** C1418-78 CLEARED at t+{int(t0-start)}s "
                              f"({clear_streak} consecutive reads) -- SDA appears to have taken! ***")
                        break
                else:
                    clear_streak = 0          # garbled read or DTC still present -> not a confirmed clear
                if emean is not None and abs(emean) < SPEC_DEG * 0.5:
                    print(f"\n\n  *** elevation converged to {emean:+.3f} -- watching... ***")
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\n  stopped by user.")
        finally:
            stf = c1418(s)[0]
            print(f"\n  final: elevation {angles(s)} | C1418-78 "
                  f"{('0x%02X'%stf) if stf is not None else 'CLEARED'} | {n} samples -> {outfile}")
            uds.request(s, [0x10, 0x01], timeout=0.5); s.close()


if __name__ == "__main__":
    main()
