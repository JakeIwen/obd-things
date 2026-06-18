#!/usr/bin/env python3
"""Read-only drive logger for the ACC radar misalignment angles (+ vehicle speed).

Logs the deviation DIDs + DTC + voltage + vehicle speed at ~1 Hz to a timestamped CSV in dumps/,
and prints a live one-line readout. Speed is read from the radar's own DID 0x1002 (km/h) -- OBD-II
PIDs are unreachable behind the SGW bypass, and the radar re-exposes the speed it consumes for ACC
(this is the value AlfaOBD shows in ACC live data). Purpose: track the radar's measured aim WHILE
DRIVING; the speed column annotates the trace so we can spot stops / steady cruise / traffic.

    python3 projects/radar/radar_acc_drive_log.py        # ~1 Hz until Ctrl-C
    python3 projects/radar/radar_acc_drive_log.py --hz 2

Everything here is read-only (22 ReadDataByIdentifier, 19 ReadDTCInformation). Nothing is started
or written. The active C1418-78 fault has already disabled ACC/FCW, so the radar is inert during
the drive -- no phantom-braking risk. Keep the PCAN on the powered hub (it auto-recovers from drops).

What the trace shows (observed on a sustained 50 mph run, 2026-06-17):
  * 0845 = frozen authoritative elevation, stayed ~-1.254 deg at all speeds (did NOT self-correct)
  * 0850 = noisier elevation estimate, wandered ~-1.16..-1.37, no trend toward 0
  * 0841 = LIVE instantaneous estimate, swings +/-10 deg with vehicle pitch (not the stored value)
  -> normal driving does not align it; misalignment is most likely PHYSICAL (see AGENT_HANDOFF).
"""
import os, sys, time, csv, datetime
# locate repo root (dir containing lib/) regardless of how deep this script lives
_root = os.path.dirname(os.path.abspath(__file__))
while _root != os.path.dirname(_root) and not os.path.isdir(os.path.join(_root, "lib")):
    _root = os.path.dirname(_root)
sys.path.insert(0, _root)
from lib import uds
from lib.modules import get

MILLIDEG = 1.0 / 1000.0
MICRODEG = 1.0 / 1_000_000
KMH_TO_MPH = 0.621371


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


# Vehicle speed: VERIFIED via DID hunt (2026-06-17) -- the radar re-exposes received vehicle speed
# at DID 0x1002 as a 1-byte value in km/h (0 at stops, plateaus matched a sustained 40-50 mph run).
# This is what AlfaOBD shows in ACC live data. OBD-II PIDs are unreachable behind the SGW bypass, so
# this single radar read on the same socket replaces the old OBD path.
SPEED_DID = 0x1002


def sample(s):
    d41, d45, d50, d06 = read_did(s, 0x0841), read_did(s, 0x0845), read_did(s, 0x0850), read_did(s, 0x1006)
    dsp = read_did(s, SPEED_DID)
    kmh = dsp[0] if dsp else None
    return {
        "speed_kmh": kmh,
        "speed_mph": round(kmh * KMH_TO_MPH, 1) if kmh is not None else None,
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
    quiet = "--quiet" in sys.argv          # suppress per-sample line (for unattended/cron)
    stop_idle = float(opt("--stop-after-idle", "0"))   # exit after N s of no radar response (0=never)
    outdir = opt("--out-dir") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "dumps")
    os.makedirs(outdir, exist_ok=True)
    outfile = os.path.abspath(os.path.join(
        outdir, f"radar_acc_drive_{time.strftime('%Y%m%d_%H%M%S')}.csv"))

    s = uds.open_socket(m.txid, m.rxid, m.channel, timeout=1.0)
    uds.request(s, [0x10, 0x03], timeout=1.0)
    cols = ["iso_time", "elapsed_s", "speed_mph", "speed_kmh", "volt",
            "vert_0841", "elev_0845", "azim_0845", "elev_0850", "azim_0850", "c1418"]

    print(f"# drive log -> {outfile}")
    print(f"# {m.name}  ~{hz:g} Hz  (read-only; Ctrl-C to stop)")
    print(f"# speed via radar DID 0x{SPEED_DID:04X} (km/h); 0845/0850 = elevation, DTC = C1418-78\n")

    start = time.time()
    last_tp = start
    last_data = start
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

                # "got data" = radar answered at all; used for idle-exit (vehicle slept).
                got_data = row["volt"] is not None or any(
                    row[k] is not None for k in ("vert_0841", "elev_0845", "elev_0850"))
                if got_data:
                    last_data = t0
                else:
                    # radar silent -> don't write an empty row; exit if idle long enough
                    if stop_idle > 0 and (t0 - last_data) > stop_idle:
                        if not quiet:
                            print()
                        print(f"  idle {stop_idle:g}s (vehicle asleep) -- stopping. {n} samples -> {outfile}")
                        break
                    dt = period - (time.time() - t0)
                    if dt > 0:
                        time.sleep(dt)
                    continue

                elapsed = round(t0 - start, 1)
                rec = {"iso_time": datetime.datetime.now().isoformat(timespec="seconds"),
                       "elapsed_s": elapsed, **row}
                w.writerow(rec)
                f.flush()
                n += 1

                if not quiet:
                    dtc = "----" if row["c1418"] is None else f"0x{row['c1418']:02X}"
                    mph = row["speed_mph"]
                    spd = f"{mph:5.1f}mph" if mph is not None else "  n/a  "
                    def fmt(x): return "  n/a " if x is None else f"{x:+.4f}"
                    print(f"\r  t+{elapsed:6.0f}s  {spd}  {row['volt'] or 0:4.1f}V  "
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
