#!/usr/bin/env python3
"""Per-module DID/service inventory from a decoded AlfaOBD log.

Groups UDS request->response exchanges by (ATSH address, service, DID/sub) and keeps a few
sample reassembled responses. Single-frame `22` reads reassemble cleanly; multi-frame
COMMANDS (long 2E/2F/31, 27 key) are fragmented by ISO-TP consecutive frames here and show
up as scraps -> use reassemble_commands.py (TODO) for those.

Output is an *extrapolation* (a derived map), so it belongs in findings/; the raw/decoded
logs it reads stay under tmp/ecu_mapping/.

Usage: extract_did_map.py <decoded.txt> <out.txt> [vehicle_vin]
"""
import os
import re
import sys
from collections import defaultdict, Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from alfalog import iter_exchanges, phys_addr, redact_vin

OUR_VIN = os.environ.get("OBD_VIN", "")   # real VIN stays out of git; set it locally
MAXSAMP = 4
SKIP = {"3E"}  # TesterPresent keepalive noise


def keyfor(u):
    svc = u[:2]
    if svc in ("22", "2E", "2F") and len(u) >= 6:
        return f"{svc}{u[2:6]}"
    if svc == "31" and len(u) >= 8:
        return f"31.{u[2:4]}.{u[4:8]}"
    if svc in ("19", "27", "10", "14", "85") and len(u) >= 2:
        return u[:4] if len(u) >= 4 else u
    return u[:8]


def printable(hx):
    try:
        b = bytes.fromhex(hx)
    except ValueError:
        return ""
    return "".join(chr(x) if 32 <= x < 127 else "." for x in b)


_VINRUN = re.compile(rb'3C6[0-9A-HJ-NPR-Z]{5,14}')   # VIN / partial-VIN in a response


def scrub_hex(hx):
    """Mask any VIN-like run embedded in a sample response (identity DIDs F190/F1A0/1A90/…)
    so no vehicle identifier lands in a tracked findings file, in hex or ascii. Tolerates
    odd-length hex (truncated reassembly) by processing the even prefix."""
    odd = len(hx) % 2
    core = hx[:-1] if odd else hx
    try:
        b = bytearray.fromhex(core)
    except ValueError:
        return hx
    s = bytes(c if 32 <= c < 127 else 0 for c in b)
    for m in _VINRUN.finditer(s):
        for i in range(m.start(), m.end()):
            b[i] = 0x23   # '#'
    return b.hex().upper() + (hx[-1] if odd else "")


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    src, out = argv[1], argv[2]
    vin = argv[3] if len(argv) > 3 else OUR_VIN
    agg = defaultdict(lambda: {"n": 0, "resp": []})   # (addr,key) -> counts+samples
    addr_mod = defaultdict(Counter)
    nreq = 0
    for ex in iter_exchanges(src):
        u = ex["req"]
        if ex["module"]:
            addr_mod[ex["addr"]][ex["module"]] += 1
        # skip manual ISO-TP frames (8 bytes + ELM hint = 17 chars, PCI nibble 1/2);
        # those are multi-frame command pieces -> reassemble_commands.py, not real DIDs.
        if len(u) == 17 and u[0] in "12":
            continue
        svc = u[:2]
        if svc in SKIP:
            continue
        nreq += 1
        rec = agg[(ex["addr"], keyfor(u))]
        rec["n"] += 1
        r = scrub_hex(ex["resp"])
        if r and r not in [x for x, _ in rec["resp"]] and len(rec["resp"]) < MAXSAMP:
            rec["resp"].append((r, printable(r)))

    by_addr = defaultdict(list)
    for (addr, k), rec in agg.items():
        by_addr[addr].append((k, rec))
    ours = vin == OUR_VIN
    with open(out, "w") as g:
        g.write(f"# AlfaOBD {'OUR-VAN' if ours else 'reference-van'} module/DID map "
                f"(extrapolation)\n")
        g.write(f"# F190-identified VIN: {redact_vin(vin)}"
                f"{'   (our van — ground truth)' if ours else '   (NOT our van)'}\n")
        g.write(f"# source: decoded AlfaOBD debug log under tmp/ecu_mapping/. "
                f"requests={nreq:,}\n")
        g.write(f"# NOTE: 'module(s)' names are AlfaOBD SELECTED-PROFILE labels (what the operator\n")
        g.write(f"#   picked in AlfaOBD), NOT confirmed hardware — many are near-empty probes "
                f"(check reads=).\n")
        g.write(f"#   The VIN above is only the F190-identified vehicle; a multi-session log can mix\n")
        g.write(f"#   profiles AND vehicles, so a profile name may not match that VIN.\n")
        g.write(f"# NOTE: single-frame 22 reads reassemble clean; multi-frame commands "
                f"(2E/2F/31/27) are fragmented -> reassemble_commands.py.\n")
        g.write(f"# line: <svc/DID>  reads=N  resp=<hex> |ascii|\n\n")
        for addr in sorted(by_addr, key=lambda a: -sum(r["n"] for _, r in by_addr[a])):
            items = sorted(by_addr[addr], key=lambda x: -x[1]["n"])
            total = sum(r["n"] for _, r in items)
            mods = ", ".join(f"{n} (x{c})" for n, c in addr_mod[addr].most_common()) \
                or "(no recording header)"
            g.write("=" * 78 + "\n")
            g.write(f"## ATSH {addr}   (phys 0x{phys_addr(addr)})   "
                    f"reads={total:,}   distinct={len(items)}\n")
            g.write(f"   module(s): {mods}\n" + "-" * 78 + "\n")
            for k, rec in items:
                s = rec["resp"][0] if rec["resp"] else ("", "")
                extra = f"  (+{len(rec['resp'])-1} more)" if len(rec["resp"]) > 1 else ""
                g.write(f"  {k:<14} reads={rec['n']:<7} "
                        f"resp={s[0][:48]:<48} |{s[1][:24]}|{extra}\n")
            g.write("\n")
    print(f"wrote {out}  (requests={nreq:,}, addresses={len(by_addr)}, keys={len(agg):,})")


if __name__ == "__main__":
    main(sys.argv)
