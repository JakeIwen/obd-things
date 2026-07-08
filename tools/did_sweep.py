#!/usr/bin/env python3
"""Generic read-only ReadDataByIdentifier (22 <did>) sweep for any module.

    python3 tools/did_sweep.py <module_key> [start_hex] [end_hex]
    python3 tools/did_sweep.py radar_acc                  # full 0000-FFFF
    python3 tools/did_sweep.py radar_acc 0800 08FF

Records every DID that returns positive (62) or securityAccessDenied (7F2233 = exists but locked);
skips requestOutOfRange (7F2231 = not implemented). USB-drop resilient. Writes tmp/sweeps/<key>_did_sweep.txt
(gitignored scratch -- promote a sweep worth keeping into projects/<x>/findings/).
Pure reads, safe.
"""
import os
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)
from lib import uds
from lib.modules import get


def dump_dids(module, start, end, outfile):
    s = uds.open_socket(module.txid, module.rxid, module.channel, timeout=0.4)
    uds.request(s, [0x10, 0x03], timeout=1.0)
    last_tp = time.time()
    found, locked, unresolved = [], [], []
    total = end - start + 1
    print(f"# DID sweep 22 over 0x{start:04X}-0x{end:04X} on {module.key} ({total} DIDs)\n")
    log = open(outfile, "w")
    log.write(f"# {module.key} DID sweep 22 over 0x{start:04X}-0x{end:04X}\n")
    for did in range(start, end + 1):
        if (did & 0x0FFF) == 0:
            print(f"  ... 0x{did:04X}  (found={len(found)} locked={len(locked)} unresolved={len(unresolved)})")
            log.flush()
        while True:
            try:
                if time.time() - last_tp > 2.0:
                    uds.request(s, [0x3E, 0x00], timeout=0.5)
                    last_tp = time.time()
                resp, _ = uds.request(s, [0x22, (did >> 8) & 0xFF, did & 0xFF], timeout=0.4, retries=2)
                break
            except OSError as e:
                print(f"  !! socket error at 0x{did:04X} ({e}); recovering...")
                try: s.close()
                except Exception: pass
                s = uds.recover_socket(module.txid, module.rxid, module.channel)
                uds.request(s, [0x10, 0x03], timeout=1.0)
                last_tp = time.time()
        if resp is None:
            unresolved.append(did); log.write(f"{did:04X} UNRESOLVED\n")
        elif resp[0] == 0x62:
            data = resp[3:]
            found.append((did, bytes(data)))
            ascii_s = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
            line = f"{did:04X} OK  data={uds.hx(data)}  |{ascii_s}|"
            print(f"  {line}"); log.write(line + "\n")
        elif resp[0] == 0x7F and len(resp) >= 3 and resp[2] == 0x33:
            locked.append(did)
            print(f"  {did:04X} LOCKED (7F2233)"); log.write(f"{did:04X} LOCKED\n")
        elif resp[0] == 0x7F and len(resp) >= 3 and resp[2] in (0x31, 0x12, 0x13):
            pass  # not implemented - skip
        else:
            log.write(f"{did:04X} OTHER {uds.hx(resp)}\n")
    s.close(); log.close()
    print("\n" + "=" * 60)
    print(f"  swept     : {total}")
    print(f"  readable  : {len(found)}")
    print(f"  locked    : {len(locked)}")
    print(f"  unresolved: {len(unresolved)} {'(re-run those)' if unresolved else ''}")
    print(f"  full log  : {outfile}")
    print("=" * 60)


if __name__ == "__main__":
    key = sys.argv[1] if len(sys.argv) > 1 else "radar_acc"
    module = get(key)
    start = int(sys.argv[2], 16) if len(sys.argv) > 2 else 0x0000
    end = int(sys.argv[3], 16) if len(sys.argv) > 3 else 0xFFFF
    # full 0000-FFFF is the canonical dump; a partial range gets its own file so it
    # can't clobber the full sweep.
    full = (start == 0x0000 and end == 0xFFFF)
    name = f"{key}_did_sweep.txt" if full else f"{key}_did_sweep_{start:04X}-{end:04X}.txt"
    out = os.path.join(REPO, "tmp", "sweeps", name)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    dump_dids(module, start, end, out)
