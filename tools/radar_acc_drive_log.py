#!/usr/bin/env python3
"""Read-only drive logger for the ACC radar misalignment angles.

Logs the deviation DIDs + DTC + voltage at ~1 Hz to a timestamped CSV in dumps/, and prints a
live one-line readout. Purpose: watch what the radar measures its own aim to be WHILE DRIVING
(0x0841 is a target-derived online estimate that only updates with a moving scene).

    python3 tools/radar_acc_drive_log.py                 # ~1 Hz until Ctrl-C
    python3 tools/radar_acc_drive_log.py --hz 2

Everything here is read-only (22 ReadDataByIdentifier + 19 ReadDTCInformation). Nothing is
started or written. The active C1418-78 fault has already disabled ACC/FCW, so the radar is
inert during the drive -- no phantom-braking risk. Bring a passenger to watch; don't drive solo
while operating the Pi. Keep the PCAN on the powered hub (it auto-recovers from USB drops).

What the trace should show:
  * 0841 climbs from ~0 toward ~-1.26 deg     -> online estimate confirms the real misalignment
  * 0845/0850 stay ~-1.26, DTC stays 0x8F     -> radar sees it but won't self-correct (physical)
  * 0845/0850 drift toward 0                   -> dynamic self-align is working; keep driving
  * 0841 never moves either                    -> no measurement w/o the routine running
"""
import os, sys, time, csv, datetime
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from lib import uds
from lib.modules import get

MILLIDEG = 1.0 / 1000.0
MICRODEG = 1.0 / 1_000_000


def opt(flag, default=None):
    a = sys.argv[1:]
    return a[a.index(flag) + 1] if flag in a else default


def read_did(s, did):
    r, _ = uds.request(s, [0x22, (did >> 8) & 0xFF, did & 0xFF], timeout=0.8)
    return r[3:] if (r and r[0] == 0x62 and len(r) >= 3) else None


def c1418(s):
    r, _ = uds.request(s, [0x19, 0x02, 0xFF], timeout=1.0)
    if not r or r[0] != 0x59:
        return None
    b = r[3:]
    for i in range(0, len(b) - 3, 4):
        if b[i] == 0x54 and b[i + 1] == 0x18 and b[i + 2] == 0x78:
            return b[i + 3]
    return None  # not present = cleared


def sample(s):
    d41, d45, d50, d06 = read_did(s, 0x0841), read_did(s, 0x0845), read_did(s, 0x0850), read_did(s, 0x1006)
    return {
        "volt":  round(d06[0] * 0.1, 1) if d06 else None,
        "vert_0841": round(uds.s16(d41, 0) * MILLIDEG, 4) if d41 and len(d41) >= 2 else None,
        "elev_0845": round(uds.s32(d45, 0) * MICRODEG, 4) if d45 and len(d45) >= 8 else None,
        "azim_0845": round(uds.s32(d45, 4) * MICRODEG, 4) if d45 and len(d45) >= 8 else None,
        "elev_0850": round(uds.s32(d50, 0) * MICRODEG, 4) if d50 and len(d50) >= 8 else None,
        "azim_0850": round(uds.s32(d50, 4) * MICRODEG, 4) if d50 and len(d50) >= 8 else None,
        "c1418": c1418(s),
    }


def main():
    m = get("radar_acc")
    hz = float(opt("--hz", "1"))
    period = 1.0 / hz
    outdir = os.path.join(os.path.dirname(__file__), "..", "dumps")
    outfile = os.path.abspath(os.path.join(
        outdir, f"radar_acc_drive_{time.strftime('%Y%m%d_%H%M%S')}.csv"))

    s = uds.open_socket(m.txid, m.rxid, m.channel, timeout=1.0)
    uds.request(s, [0x10, 0x03], timeout=1.0)
    cols = ["iso_time", "elapsed_s", "volt", "vert_0841", "elev_0845", "azim_0845",
            "elev_0850", "azim_0850", "c1418"]

    print(f"# drive log -> {outfile}")
    print(f"# {m.name}  ~{hz:g} Hz  (read-only; Ctrl-C to stop)")
    print(f"# watch 0841 climb toward ~-1.26 and whether 0845/0850 ever move / DTC clears\n")

    start = time.time()
    last_tp = start
    n = 0
    with open(outfile, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        try:
            while True:
                t0 = time.time()
                if t0 - last_tp > 2.0:
                    try:
                        uds.request(s, [0x3E, 0x00], timeout=0.4)
                    except OSError:
                        pass
                    last_tp = t0
                try:
                    row = sample(s)
                except OSError:
                    print("\n  !! socket/link error -- recovering (USB brownout?) ...")
                    s = uds.recover_socket(m.txid, m.rxid, m.channel)
                    uds.request(s, [0x10, 0x03], timeout=1.0)
                    continue

                elapsed = round(t0 - start, 1)
                rec = {"iso_time": datetime.datetime.now().isoformat(timespec="seconds"),
                       "elapsed_s": elapsed, **row}
                w.writerow(rec)
                f.flush()
                n += 1

                dtc = "----" if row["c1418"] is None else f"0x{row['c1418']:02X}"
                def fmt(x): return "  n/a " if x is None else f"{x:+.4f}"
                print(f"\r  t+{elapsed:6.0f}s  {row['volt'] or 0:4.1f}V  "
                      f"0841 {fmt(row['vert_0841'])}  "
                      f"0845e {fmt(row['elev_0845'])}  0850e {fmt(row['elev_0850'])}  "
                      f"DTC {dtc}   (n={n})", end="", flush=True)

                dt = period - (time.time() - t0)
                if dt > 0:
                    time.sleep(dt)
        except KeyboardInterrupt:
            print(f"\n  stopped. {n} samples -> {outfile}")
        finally:
            try:
                uds.request(s, [0x10, 0x01], timeout=0.5)
                s.close()
            except OSError:
                pass


if __name__ == "__main__":
    main()
