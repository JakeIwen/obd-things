#!/usr/bin/env python3
"""Read-only perturbation monitor: poll every readable DID in a loop and flag ANY that
changes value. Use it to see whether gently nudging the radar housing shows up in any bit
(i.e. is there a live orientation/accelerometer signal, or does it need driving?).

    python3 projects/radar/perturb_monitor.py          # ~2 Hz, runs until Ctrl-C

While it runs, gently load the radar bracket up/down (do NOT permanently deflect). Voltage
(1006) and temp (0835) drift on their own and are tagged [expected]; anything else that moves
is the interesting result. If nothing but those two ever changes, the angle is driving-derived.
"""
import os, sys, time
# locate repo root (dir containing lib/) regardless of how deep this script lives
_root = os.path.dirname(os.path.abspath(__file__))
while _root != os.path.dirname(_root) and not os.path.isdir(os.path.join(_root, "lib")):
    _root = os.path.dirname(_root)
sys.path.insert(0, _root)
from lib import uds
from lib.modules import get

DIDS = [int(x, 16) for x in ("0103,0835,0840,0841,0842,0845,0850,0851,0857,0858,0861,0862,"
        "0863,0872,1002,1006,1008,1009,102A,1921,2001,2002,2003,2008,2009,200A,200B,200C,"
        "2010,2013,292E,F1A0,F1A1,F1A4,F1A5,FD08").split(",")]
EXPECTED = {0x1006, 0x0835}   # voltage / temp drift on their own
ANGLE = {0x0841, 0x0845, 0x0850, 0x0861}

def read(s, did):
    r, _ = uds.request(s, [0x22, (did >> 8) & 0xFF, did & 0xFF], timeout=0.5)
    return uds.hx(r[3:]) if (r and r[0] == 0x62 and len(r) >= 3) else None

m = get("radar_acc")
s = uds.open_socket(m.txid, m.rxid, m.channel, timeout=0.8)
uds.request(s, [0x10, 0x03], timeout=0.8)
print(f"# perturbation monitor on {m.name} -- gently push the bracket; Ctrl-C to stop")
print(f"# watching {len(DIDS)} DIDs (F18x identity DIDs skipped). [expected]=normal drift\n")

base = {d: read(s, d) for d in DIDS}
print("baseline captured. Now nudge the radar...\n")
last = dict(base)
n = 0
last_tp = time.time()
try:
    while True:
        if time.time() - last_tp > 2.0:
            uds.request(s, [0x3E, 0x00], timeout=0.4); last_tp = time.time()
        for d in DIDS:
            v = read(s, d)
            if v is not None and v != last[d]:
                tag = "[expected]" if d in EXPECTED else ("[* ANGLE *]" if d in ANGLE else "[* CHANGED *]")
                print(f"  {time.strftime('%H:%M:%S')}  {d:04X} {tag}  {last[d]} -> {v}   (base {base[d]})")
                last[d] = v
        n += 1
        if n % 10 == 0:
            print(f"  ... {n} sweeps, still watching (only voltage/temp moving = driving-derived) ...")
        time.sleep(0.3)
except KeyboardInterrupt:
    print("\nstopped.")
finally:
    uds.request(s, [0x10, 0x01], timeout=0.5); s.close()
