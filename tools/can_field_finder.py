#!/usr/bin/env python3
"""Find the broadcast CAN frame+field that tracks a known scalar (e.g. battery voltage).

PASSIVE / OFFLINE. Operates on candump logs you captured with the interface in listen-only
mode -- nothing here ever touches the bus. It's the broadcast-frame analogue of
signal_correlate.py (which does the same for UDS DIDs): brute-force every byte/word slice of
every CAN id across two-or-more captures taken at *different known values* of the target
signal, and keep the slices whose decoded value fits an affine map value->signal with a
plausible scale. The field that moves the right amount between states is the signal; config
constants and unrelated bytes don't fit.

Why two states: a single capture of a resting van is nearly all-constant, so a real voltage
byte is indistinguishable from a default like 0x80. Capture the SAME bus at two clearly
different voltages and the voltage field is the one that moved.

Workflow (battery voltage on the 125k body bus -- the bus that wakes on a door open):

    ./bringup.sh --bcan                      # body bus, PASSIVE (listen-only, never TX)
    # 1) ENGINE OFF, open/close a door a few times to wake the body bus, ~60 s:
    candump -ta can0 > /tmp/v_off.log        # Ctrl-C after ~60 s
    # 2) START ENGINE, idle (alternator ~14.2 V), re-bring-up passive, ~60 s:
    ./bringup.sh --bcan ; candump -ta can0 > /tmp/v_run.log

    # tag each log with the voltage at the battery during it (a multimeter, the dash gauge,
    # or a one-off armed read of radar DID 0x1006 all work as ground truth):
    python3 tools/can_field_finder.py /tmp/v_off.log=12.5 /tmp/v_run.log=14.2

Without "=value" tags it falls back to ranking whatever changed most between the captures
(decoded under common voltage scales) so you can eyeball candidates. Accepts candump's
"(ts) can0 ID  [n]  b0 b1 .."  , "(ts) can0 ID#HEX" , and untimestamped variants.
"""
import os
import re
import sys
import glob
import struct

# matches: optional "(ts)", iface, hex id, then either "[len] b b b.." or "#HEXBYTES"
_LINE = re.compile(r'(?:\(\s*[\d.]+\)\s*)?\w+\s+([0-9A-Fa-f]+)(?:\s+\[\d+\]\s+(.*)|\s*#([0-9A-Fa-f]*))')


def parse(path):
    """candump log -> {can_id: [bytes, ...]} for whatever format the file is in."""
    rows = {}
    with open(path, errors="ignore") as f:
        for ln in f:
            m = _LINE.search(ln)
            if not m:
                continue
            try:
                cid = int(m.group(1), 16)
                if m.group(2) is not None:
                    data = bytes(int(x, 16) for x in m.group(2).split())
                else:
                    h = m.group(3)
                    data = bytes.fromhex(h) if h else b""
            except ValueError:
                continue
            rows.setdefault(cid, []).append(data)
    return rows


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def field_value(frames, off, width, endian):
    """Median decoded value of bytes[off:off+width] over all frames long enough; None if none."""
    fmt = endian + {1: "B", 2: "H"}[width]
    vals = [struct.unpack_from(fmt, d, off)[0] for d in frames if len(d) >= off + width]
    return median(vals) if vals else None


def lstsq_affine(xs, ys):
    """Fit ys = a*xs + b (least squares); return (a, b, r2). Exact for 2 points (r2=1)."""
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    den = n * sxx - sx * sx
    if den == 0:
        return None
    a = (n * sxy - sx * sy) / den
    b = (sy - a * sx) / n
    ss_tot = sum((y - sy / n) ** 2 for y in ys)
    ss_res = sum((y - (a * x + b)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return a, b, r2


# plausible volts-per-LSB so we don't "discover" a counter or a flag as a voltage
SLOPE_BAND = {1: (0.02, 0.30), 2: (0.0003, 0.02)}
V_SANE = (8.0, 16.5)        # decoded result must look like a 12 V system rail


def find(logs, truths, top):
    caps = [parse(p) for p in logs]
    for p, c in zip(logs, caps):
        n = sum(len(v) for v in c.values())
        print(f"# {os.path.basename(p)}: {n} frames, {len(c)} ids"
              + (f", truth={truths[logs.index(p)]} V" if truths else ""))
    common = set(caps[0])
    for c in caps[1:]:
        common &= set(c)
    print(f"# {len(common)} CAN ids common to all captures\n")

    known = truths is not None
    results = []
    for cid in sorted(common):
        per = [c[cid] for c in caps]
        minlen = min(min(len(d) for d in frs) for frs in per)
        for width in (1, 2):
            lo, hi = SLOPE_BAND[width]
            for off in range(minlen - width + 1):
                for endian in (">", "<") if width == 2 else (">",):
                    vals = [field_value(frs, off, width, endian) for frs in per]
                    if any(v is None for v in vals):
                        continue
                    spread = max(vals) - min(vals)
                    if known:
                        if max(truths) - min(truths) >= 1.0 and spread == 0:
                            continue                      # must move when voltage moved
                        fit = lstsq_affine(vals, truths)
                        if not fit:
                            continue
                        a, b, r2 = fit
                        if not (lo <= abs(a) <= hi) or a <= 0:
                            continue
                        if not all(V_SANE[0] <= a * v + b <= V_SANE[1] for v in vals):
                            continue
                        score = r2 - 0.001 * abs(off)     # tie-break toward earlier bytes
                        results.append((score, r2, cid, off, width, endian, a, b, vals))
                    else:
                        # no ground truth: rank by movement, decoded sane under x0.1 / x0.01
                        a = 0.1 if width == 1 else 0.01
                        if spread == 0 or not all(V_SANE[0] <= a * v <= V_SANE[1] for v in vals):
                            continue
                        results.append((spread, 0.0, cid, off, width, endian, a, 0.0, vals))

    results.sort(reverse=True)
    if known:
        print(f"{'r2':>6}  {'ID':>3} {'off':>3} {'w':>1} {'e':>1}  {'V/LSB':>8} {'offset':>8}  decoded-per-capture")
    else:
        print(f"{'spread':>6}  {'ID':>3} {'off':>3} {'w':>1} {'e':>1}  assuming  decoded-per-capture")
    print("-" * 78)
    for sc, r2, cid, off, width, endian, a, b, vals in results[:top]:
        dec = "  ".join(f"{a * v + b:5.2f}" for v in vals)
        e = "" if width == 1 else ("BE" if endian == ">" else "LE")
        if known:
            print(f"{r2:6.3f}  {cid:03X} {off:>3} {width} {e:>2}  {a:8.4f} {b:8.3f}  [{dec}] V")
        else:
            print(f"{sc:6.1f}  {cid:03X} {off:>3} {width} {e:>2}  x{a:<6}  raw->[{dec}] V")
    if not results:
        print("(no plausible voltage field -- did the bus carry voltage in BOTH states? "
              "if the body bus shows nothing, voltage may only be on C-CAN with ignition on.)")
    print("\nTop row's ID/off/width/scale is the field to wire into the passive monitor.")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    top = int(next((a.split("=")[1] for a in sys.argv[1:] if a.startswith("--top=")), "20"))
    if len(args) < 2:
        sys.exit("usage: can_field_finder.py log1[=volts] log2[=volts] ... [--top=N]\n"
                 "       (>=2 captures of the SAME bus at different voltages; see docstring)")
    logs, truths = [], []
    for a in args:
        path, _, v = a.partition("=")
        if "*" in path:
            path = sorted(glob.glob(path))[-1]
        if not os.path.exists(path):
            sys.exit(f"no such capture: {path}")
        logs.append(path)
        truths.append(float(v) if v else None)
    truths = truths if all(t is not None for t in truths) else None
    find(logs, truths, top)


if __name__ == "__main__":
    main()
