#!/usr/bin/env python3
"""TEMPORARY speed-DID hunt logger (read-only). Logs EVERY readable radar DID (raw hex) at ~1 Hz
to a wide CSV while driving, so we can find the DID that tracks vehicle speed -- AlfaOBD shows
speed in the ACC live data, so the radar re-exposes the received speed as a 22 DID. Once the
speed DID is identified (it reads ~0 at every stop and ramps with motion; cross-check against the
broadcast odometer ID 0x101 in the matching tmp/canraw burst), wire just that DID into
radar_acc_drive_log.py and delete this script + the tmp/HUNT_DIDS marker.

Same CLI as radar_acc_drive_log.py (--quiet --out-dir --stop-after-idle) so the cron supervisor
can launch it in place of the angle logger while the marker exists.
"""
import os, sys, time, csv, datetime
_root = os.path.dirname(os.path.abspath(__file__))
while _root != os.path.dirname(_root) and not os.path.isdir(os.path.join(_root, "lib")):
    _root = os.path.dirname(_root)
sys.path.insert(0, _root)
from lib import uds
from lib.modules import get

# All readable non-identity DIDs from the sweep (candidates for a live speed signal).
DIDS = [int(x, 16) for x in ("0103,0835,0840,0841,0842,0845,0850,0851,0857,0858,0861,0862,"
        "0863,0872,1002,1006,1008,1009,102A,1921,2001,2002,2003,2008,2009,200A,200B,200C,"
        "2010,2013,292E").split(",")]


def opt(flag, default=None):
    a = sys.argv[1:]
    return a[a.index(flag) + 1] if flag in a else default


def read_did(s, did):
    r, _ = uds.request(s, [0x22, (did >> 8) & 0xFF, did & 0xFF], timeout=0.4, retries=0)
    return uds.hx(r[3:]).replace(" ", "") if (r and r[0] == 0x62 and len(r) >= 3) else ""


def main():
    m = get("radar_acc")
    hz = float(opt("--hz", "1"))
    period = 1.0 / hz
    quiet = "--quiet" in sys.argv
    stop_idle = float(opt("--stop-after-idle", "0"))
    outdir = opt("--out-dir") or os.path.join(_root, "tmp", "radar")
    os.makedirs(outdir, exist_ok=True)
    outfile = os.path.abspath(os.path.join(outdir, f"hunt_{time.strftime('%Y%m%d_%H%M%S')}.csv"))

    s = uds.open_socket(m.txid, m.rxid, m.channel, timeout=1.0)
    uds.request(s, [0x10, 0x03], timeout=1.0)
    cols = ["iso_time", "elapsed_s"] + [f"{d:04X}" for d in DIDS]
    if not quiet:
        print(f"# DID-hunt log -> {outfile}  ({len(DIDS)} DIDs ~{hz:g} Hz, read-only)")

    start = time.time(); last_tp = start; last_data = start; n = 0
    with open(outfile, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        try:
            while True:
                t0 = time.time()
                if t0 - last_tp > 2.0:
                    try: uds.request(s, [0x3E, 0x00], timeout=0.4)
                    except OSError: pass
                    last_tp = t0
                try:
                    row = {f"{d:04X}": read_did(s, d) for d in DIDS}
                except OSError:
                    s = uds.recover_socket(m.txid, m.rxid, m.channel)
                    uds.request(s, [0x10, 0x03], timeout=1.0); continue
                if any(row.values()):
                    last_data = t0
                elif stop_idle > 0 and (t0 - last_data) > stop_idle:
                    print(f"  idle {stop_idle:g}s -- stopping. {n} samples -> {outfile}")
                    break
                else:
                    if period - (time.time() - t0) > 0: time.sleep(period - (time.time() - t0))
                    continue
                rec = {"iso_time": datetime.datetime.now().isoformat(timespec="seconds"),
                       "elapsed_s": round(t0 - start, 1), **row}
                w.writerow(rec); f.flush(); n += 1
                dt = period - (time.time() - t0)
                if dt > 0: time.sleep(dt)
        except KeyboardInterrupt:
            print(f"\n  stopped. {n} samples -> {outfile}")
        finally:
            try: uds.request(s, [0x10, 0x01], timeout=0.5); s.close()
            except OSError: pass


if __name__ == "__main__":
    main()
