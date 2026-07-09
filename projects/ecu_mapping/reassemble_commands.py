#!/usr/bin/env python3
"""Reassemble multi-frame AlfaOBD *commands* (the piece extract_did_map leaves fragmented).

AlfaOBD sends long UDS requests as MANUAL ISO-TP frames over the adapter:
  First Frame       S: '1L LL <6 data>' + trailing ELM responses-hint digit  (17 hex chars)
  ECU Flow Control  R: '30 00 00'
  Consecutive Frame S: '2N <7 data>'    + trailing hint digit                 (17 hex chars)
  ... then the ECU's real response as R.
We drop the trailing hint digit (16 hex = 8-byte frame), strip the PCI byte(s), concatenate,
and truncate to the First Frame's declared length -> the full request. Single-frame requests
(<=7 bytes) are sent whole and handled directly. Responses reassemble as in alfalog.

This surfaces the write / IO-control / routine / security sequences — e.g. the BCM
actuations behind the remote-unlock work. Output is a decoded, interpreted command log
(an extrapolation -> findings/).

Usage: reassemble_commands.py <decoded.txt> <out.txt> [atsh_filter e.g. DA40F1]
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from alfalog import iter_lines, ascii_of, phys_addr

_LINE = re.compile(r'^(\d{2}:\d{2}:\d{2}\.\d{3}) ([SR]): ([0-9A-Fa-f]*)$')
_SEG  = re.compile(r'^([0-9A-Fa-f]):([0-9A-Fa-f]+)$')
_REC  = re.compile(r'Recording data for (.+)')

CMD_SVC = {"2E", "2F", "31", "27", "10", "14", "85", "11"}  # commands (not 22 reads / 3E)
NRC = {"11": "serviceNotSupported", "12": "subFunctionNotSupported",
       "13": "wrongLength", "22": "conditionsNotCorrect", "31": "requestOutOfRange",
       "33": "securityAccessDenied", "35": "invalidKey", "36": "exceedNumberOfAttempts",
       "78": "responsePending", "7E": "subFuncNotSupportedInActiveSession",
       "7F": "serviceNotSupportedInActiveSession"}


def interpret(req, resp):
    svc = req[:2]
    if svc == "2E":
        what = f"WriteDataByIdentifier DID {req[2:6]}  data={req[6:]}"
    elif svc == "2F":
        what = f"IOControl DID {req[2:6]} ctrl={req[6:8]} opt={req[8:]}"
    elif svc == "31":
        sub = {"01": "start", "02": "stop", "03": "result"}.get(req[2:4], req[2:4])
        what = f"Routine {sub} RID {req[4:8]}  {req[8:]}"
    elif svc == "27":
        s = int(req[2:4], 16) if len(req) >= 4 else 0
        kind = "requestSeed" if s % 2 else "sendKey"
        what = f"SecurityAccess {kind} lvl {req[2:4]}  {req[4:]}"
    elif svc == "10":
        what = f"DiagnosticSessionControl session {req[2:4]}"
    elif svc == "14":
        what = f"ClearDiagnosticInformation {req[2:]}"
    else:
        what = f"svc {svc} {req[2:]}"
    r = resp.upper()
    if not r:
        rr = "(no resp captured)"
    elif r.startswith("7F"):
        rr = f"NEG {r[4:6]}={NRC.get(r[4:6], '?')}"
    elif svc == "27" and r.startswith("67"):
        rr = f"POS seed/ack: {r}"
    elif r.startswith(hex(int(svc, 16) + 0x40)[2:].upper().zfill(2)):
        rr = f"POS {r}"
    else:
        rr = r
    return what, rr


def iter_commands(path):
    addr, module, date = "?", None, "????/??/??"
    mode = "idle"                 # 'idle' | 'mf' (collecting multi-frame request)
    ff_ts = None; decl = 0; req_hex = ""
    pend = None                   # single-frame request awaiting response
    rseg = {}; rplain = ""

    def resp():
        return ("".join(rseg[k] for k in sorted(rseg)) if rseg else rplain).upper()

    def accumulate(pay):
        nonlocal rplain
        for part in (pay.split("\r") if "\r" in pay else [pay]):
            part = part.strip()
            if not part or part == ">":
                continue
            sm = _SEG.match(part)
            if sm:
                rseg[int(sm.group(1), 16)] = sm.group(2)
            elif re.fullmatch(r"[0-9A-Fa-f]{2,}", part):
                rplain += part

    for ln in iter_lines(path):
        m = _LINE.match(ln)
        if not m:
            r = _REC.search(ln)
            if r: module = r.group(1).strip()
            elif "Recording closed" in ln: module = None
            d = re.search(r'(\d{4}/\d{2}/\d{2})', ln)
            if d: date = d.group(1)
            continue
        ts, sr, payhex = m.group(1), m.group(2), m.group(3)
        pay = ascii_of(payhex)
        u = pay.strip().upper().replace(" ", "")
        if sr == "S":
            if pend is not None:                      # new send ends a prior single exchange
                yield dict(ts=pend[0], date=date, addr=addr, module=module,
                           req=pend[1], resp=resp()); pend = None; rseg.clear(); rplain = ""
            if u.startswith("ATSH"): addr = u[4:]; continue
            if u.startswith(("AT", "ST")): continue
            if len(u) == 17 and u[0] == "1":          # First Frame
                if mode == "mf" and req_hex:          # previous mf never got a response
                    yield dict(ts=ff_ts, date=date, addr=addr, module=module,
                               req=req_hex[:decl*2] if decl else req_hex, resp="")
                b = u[:16]; decl = int(b[:4], 16) & 0x0FFF
                req_hex = b[4:]; ff_ts = ts; mode = "mf"; rseg.clear(); rplain = ""
                continue
            if mode == "mf" and len(u) == 17 and u[0] == "2":   # Consecutive Frame
                req_hex += u[:16][2:]; continue
            # single-frame request
            if mode == "mf":
                yield dict(ts=ff_ts, date=date, addr=addr, module=module,
                           req=req_hex[:decl*2] if decl else req_hex, resp=""); mode = "idle"
            pend = (ts, u); rseg.clear(); rplain = ""
        else:                                          # R frame
            if mode == "mf":
                if u.startswith("30") or not u or u == ">":     # flow control / prompt
                    if ">" in pay and not u.replace(">", ""):    # bare prompt only
                        pass
                    continue
                accumulate(pay)
                if ">" in pay:
                    full = req_hex[:decl*2] if decl else req_hex
                    yield dict(ts=ff_ts, date=date, addr=addr, module=module,
                               req=full, resp=resp())
                    mode = "idle"; rseg.clear(); rplain = ""
            elif pend is not None:
                accumulate(pay)
                if ">" in pay:
                    yield dict(ts=pend[0], date=date, addr=addr, module=module,
                               req=pend[1], resp=resp()); pend = None; rseg.clear(); rplain = ""
    if pend is not None:
        yield dict(ts=pend[0], date=date, addr=addr, module=module, req=pend[1], resp=resp())


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    src, out = argv[1], argv[2]
    filt = argv[3].upper() if len(argv) > 3 else None
    n = 0
    with open(out, "w") as g:
        g.write("# AlfaOBD reassembled COMMAND log (extrapolation) — multi-frame requests "
                "rebuilt\n")
        g.write(f"# source: decoded log under tmp/ecu_mapping/  filter={filt or 'all modules'}\n")
        g.write("# only command services (2E/2F/31/27/10/14); 22 reads + 3E omitted\n\n")
        last = None
        for ex in iter_commands(src):
            if ex["req"][:2] not in CMD_SVC:
                continue
            if filt and ex["addr"] != filt:
                continue
            hdr = f"{ex['addr']} (0x{phys_addr(ex['addr'])})  {ex['module'] or ''}"
            if hdr != last:
                g.write(f"\n===== {hdr} =====\n"); last = hdr
            what, rr = interpret(ex["req"], ex["resp"])
            g.write(f"  {ex['date']} {ex['ts']}  {what}\n")
            g.write(f"       -> {rr}\n")
            n += 1
    print(f"wrote {out}  ({n} commands)")


if __name__ == "__main__":
    main(sys.argv)
