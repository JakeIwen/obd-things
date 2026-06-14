#!/usr/bin/env python3
"""Generic read-only RoutineControl scan (requestRoutineResults, 31 03 <rid>) for any module.

    python3 tools/routine_scan.py <module_key> [start_hex] [end_hex]
    python3 tools/routine_scan.py radar_acc                 # 0200-03FF + FF00-FF03

Classifies each routine id; retries empties; prints a CLEAN/DIRTY verdict. 31 03 is read-only and
safe; 31 01 (startRoutine) is actuation and is deliberately NOT part of this tool.

Interpretation:
  7F 31 31 (requestOutOfRange)    -> routine NOT implemented
  7F 31 24 (requestSequenceError) -> routine EXISTS but not started   (the signal we want)
  71 03 ..                        -> routine exists, returned results
  empty after retries             -> UNRESOLVED (makes the scan 'dirty')
"""
import os
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)
from lib import uds
from lib.modules import get


def scan_routines(module, start, end, retries=3, extra=(0xFF00, 0xFF01, 0xFF02, 0xFF03)):
    s = uds.open_socket(module.txid, module.rxid, module.channel, timeout=0.6)
    uds.request(s, [0x10, 0x03], timeout=1.0)
    rids = list(range(start, end + 1)) + list(extra)
    exists, absent, unresolved, other = [], [], [], []
    last_tp = time.time()
    print(f"# routine scan 31 03 over 0x{start:04X}-0x{end:04X} + extras on {module.key} (retries={retries})\n")
    for rid in rids:
        while True:
            try:
                if time.time() - last_tp > 2.0:
                    uds.request(s, [0x3E, 0x00], timeout=0.5)
                    last_tp = time.time()
                resp, status = uds.request(s, [0x31, 0x03, (rid >> 8) & 0xFF, rid & 0xFF],
                                           timeout=0.6, retries=retries)
                break
            except OSError as e:
                print(f"  !! socket error at 0x{rid:04X} ({e}); recovering...")
                try: s.close()
                except Exception: pass
                s = uds.recover_socket(module.txid, module.rxid, module.channel)
                uds.request(s, [0x10, 0x03], timeout=1.0)
                last_tp = time.time()
        if resp is None:
            unresolved.append(rid); tag = "UNRESOLVED (empty after retries)"
        elif resp[0] == 0x7F and len(resp) >= 3 and resp[2] == 0x31:
            absent.append(rid); tag = "absent (7F3131)"
        elif resp[0] == 0x7F and len(resp) >= 3 and resp[2] == 0x24:
            exists.append((rid, "7F3124 not-started")); tag = "** EXISTS (7F3124 requestSequenceError) **"
        elif resp[0] == 0x71:
            exists.append((rid, "71 positive")); tag = f"** EXISTS (positive {uds.hx(resp)}) **"
        else:
            other.append((rid, uds.hx(resp))); tag = f"OTHER {status}: {uds.hx(resp)}"
        if resp is None or "EXISTS" in tag or tag.startswith("OTHER"):
            print(f"  0x{rid:04X}: {tag}")
    s.close()
    clean = (len(unresolved) == 0)
    print("\n" + "=" * 60)
    print(f"  scanned        : {len(rids)}")
    print(f"  absent (7F3131): {len(absent)}")
    print(f"  EXISTS         : {len(exists)} -> " +
          (", ".join(f"0x{r:04X}({why})" for r, why in exists) or "none"))
    print(f"  other          : {len(other)}")
    print(f"  UNRESOLVED     : {len(unresolved)} -> " +
          (", ".join(f"0x{r:04X}" for r in unresolved) or "none"))
    print(f"  VERDICT        : {'CLEAN' if clean else 'DIRTY (re-run; unresolved above)'}")
    print("=" * 60)


if __name__ == "__main__":
    key = sys.argv[1] if len(sys.argv) > 1 else "radar_acc"
    module = get(key)
    start = int(sys.argv[2], 16) if len(sys.argv) > 2 else 0x0200
    end = int(sys.argv[3], 16) if len(sys.argv) > 3 else 0x03FF
    scan_routines(module, start, end)
