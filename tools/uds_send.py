#!/usr/bin/env python3
"""Ad-hoc read-only UDS request to any module (debugging helper).

    python3 tools/uds_send.py <module_key> <hexbyte> [hexbyte ...]
    python3 tools/uds_send.py radar_acc 22 F1 A5
    python3 tools/uds_send.py radar_acc 31 03 0251

Intended for reads / requestRoutineResults. Does not stop you from typing a write/startRoutine,
so know what you're sending.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from lib import uds
from lib.modules import get

if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: uds_send.py <module_key> <hexbyte> [hexbyte ...]")
    module = get(sys.argv[1])
    payload = [int(x, 16) for x in sys.argv[2:]]
    s = uds.open_socket(module.txid, module.rxid, module.channel)
    resp, status = uds.request(s, payload)
    print(f"TX  : {uds.hx(payload)}")
    print(f"RX  : {uds.hx(resp) if resp else '(none)'}")
    print(f"stat: {status}")
    s.close()
