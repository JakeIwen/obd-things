#!/usr/bin/env python3
"""Reassemble ISO-TP request/response streams for one addr pair from a candump -L log."""
import sys, re

LOG = sys.argv[1]
REQ, RSP = "18DAC7F1", "18DAF1C7"

SVC = {
    0x10: "DiagnosticSessionControl", 0x11: "ECUReset", 0x14: "ClearDiagnosticInformation",
    0x19: "ReadDTCInformation", 0x22: "ReadDataByIdentifier", 0x23: "ReadMemoryByAddress",
    0x27: "SecurityAccess", 0x28: "CommunicationControl", 0x2E: "WriteDataByIdentifier",
    0x2F: "InputOutputControl", 0x31: "RoutineControl", 0x3E: "TesterPresent",
    0x85: "ControlDTCSetting",
}

def frames(canid):
    out = []
    pat = re.compile(r"\(([\d.]+)\) \S+ " + canid + r"#([0-9A-Fa-f]+)")
    for ln in open(LOG, errors="ignore"):
        m = pat.match(ln)
        if m:
            out.append((float(m.group(1)), bytes.fromhex(m.group(2))))
    return out

def reassemble(fr):
    """Yield (ts, payload) for each complete ISO-TP PDU."""
    msgs, cur, need = [], None, 0
    for ts, d in fr:
        pci = d[0] >> 4
        if pci == 0:                       # single frame
            n = d[0] & 0xF
            msgs.append((ts, d[1:1+n]))
        elif pci == 1:                     # first frame
            need = ((d[0] & 0xF) << 8) | d[1]
            cur = [ts, bytearray(d[2:])]
        elif pci == 2 and cur:             # consecutive
            cur[1] += d[1:]
            if len(cur[1]) >= need:
                msgs.append((cur[0], bytes(cur[1][:need])))
                cur = None
        elif pci == 3:                     # flow control (from the other side) — skip
            pass
    return msgs

def label(p, is_req):
    if not p:
        return "(empty)"
    sid = p[0]
    if sid == 0x7F:
        return f"NRC {p[2]:02X} to svc {p[1]:02X} ({SVC.get(p[1],'?')})"
    base = sid & 0x7F if not is_req else sid
    name = SVC.get(base, f"svc {base:02X}")
    return ("REQ " if is_req else "RSP ") + name

reqs = reassemble(frames(REQ))
rsps = reassemble(frames(RSP))
allm = sorted([(t, "REQ", p) for t, p in reqs] + [(t, "RSP", p) for t, p in rsps])
t0 = allm[0][0] if allm else 0
for t, kind, p in allm:
    hx = p.hex(" ")
    if len(hx) > 120:
        hx = hx[:120] + f"... ({len(p)} bytes)"
    print(f"{t-t0:8.3f} {kind} {label(p, kind=='REQ'):40} {hx}")
