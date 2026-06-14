#!/usr/bin/env python3
"""Reproduce the ACC-radar (Bosch DASM) UDS baseline: session, key DIDs, serial, DTCs.

    python3 projects/radar/radar_acc_baseline.py

ACC/radar-specific (uses the radar's known DIDs and FCA DTC decoding). For a generic DID sweep use
tools/did_sweep.py; for ad-hoc requests use tools/uds_send.py.
"""
import os
import sys
import time

# locate repo root (dir containing lib/) regardless of how deep this script lives
_root = os.path.dirname(os.path.abspath(__file__))
while _root != os.path.dirname(_root) and not os.path.isdir(os.path.join(_root, "lib")):
    _root = os.path.dirname(_root)
sys.path.insert(0, _root)
from lib import uds
from lib.modules import MODULES

M = MODULES["radar_acc"]


def fca_dtc(dtc):
    letter = "PCBU"[(dtc[0] >> 6) & 0x3]
    code = ((dtc[0] & 0x3F) << 8) | dtc[1]
    return f"  (~{letter}{code:04X}-{dtc[2]:02X})"


def decode_dtcs(resp):
    body = resp[3:]
    print(f"   DTCs ({len(body)//4}):")
    for i in range(0, len(body) - 3, 4):
        dtc, st = body[i:i+3], body[i+3]
        print(f"     {dtc[0]:02X}{dtc[1]:02X}{dtc[2]:02X} status=0x{st:02X}{fca_dtc(dtc)}")


def baseline():
    s = uds.open_socket(M.txid, M.rxid)
    tests = [
        ("DiagnosticSessionControl extended (10 03)", [0x10, 0x03], "50 03 00 32 01 F4"),
        ("ReadDataByID F1A5 (22 F1 A5)", [0x22, 0xF1, 0xA5], "62 F1 A5 00 39 50 16 20"),
        ("ReadDataByID F195 SW (22 F1 95)", [0x22, 0xF1, 0x95], "62 F1 95 04 00"),
        ("ReadDataByID F193 HW (22 F1 93)", [0x22, 0xF1, 0x93], "62 F1 93 01"),
        ("ReadDataByID F18C serial (22 F1 8C)", [0x22, 0xF1, 0x8C], "62 F1 8C 'TD5730292062400'"),
        ("ReadDataByID F191 family (22 F1 91)", [0x22, 0xF1, 0x91], "62 F1 91 'MRR1evo14F'"),
        ("ReadDTCInformation (19 02 FF)", [0x19, 0x02, 0xFF], "59 02 ... 8 DTCs"),
    ]
    print(f"# {M.name} baseline  TX={M.txid:08X} RX={M.rxid:08X}\n")
    for name, payload, expect in tests:
        resp, status = uds.request(s, payload)
        print(f"## {name}")
        print(f"   TX  : {uds.hx(payload)}")
        print(f"   RX  : {uds.hx(resp) if resp else '(none)'}")
        print(f"   stat: {status}")
        print(f"   want: {expect}")
        if resp and payload[0] == 0x19 and status == "POSITIVE":
            decode_dtcs(resp)
        print()
        time.sleep(0.1)
    s.close()


if __name__ == "__main__":
    baseline()
