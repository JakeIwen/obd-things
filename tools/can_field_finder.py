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

Consume finalized logs made by the role-aware passive recorder, for example
``tools/passive_drive_capture.py --bus b-can``.  Supply captures made at two independently
measured target values; this offline tool neither establishes a bus state nor assumes how the
vehicle network was awakened::

    python3 tools/can_field_finder.py /tmp/v_off.log=12.5 /tmp/v_run.log=14.2

Without "=value" tags it falls back to ranking whatever changed most between the captures
(decoded under common voltage scales) so you can eyeball candidates. Accepts candump's
"(ts) INTERFACE ID  [n]  b0 b1 ..", "(ts) INTERFACE ID#HEX", and untimestamped variants.
"""
import os
import re
import sys
import glob
import struct
from dataclasses import dataclass
from typing import Iterable

# Matches: optional ``(ts)``, interface, hex ID, then either
# ``[len] b0 b1 ...`` or ``#HEXBYTES``.  The textual identifier width is
# load-bearing: candump renders SFF identifiers with three digits and EFF
# identifiers with eight, even when their numeric values are equal.
_LINE = re.compile(
    r"(?:\(\s*[\d.]+\)\s*)?"
    r"(?P<channel>[A-Za-z0-9_.-]+)\s+"
    r"(?P<can_id>[0-9A-Fa-f]+)"
    r"(?:"
    r"\s+\[(?P<declared_dlc>\d+)\]\s*(?P<spaced>(?:[0-9A-Fa-f]{2}(?:\s+|$))*)"
    r"|\s*#(?!#)(?P<compact>[0-9A-Fa-f]*)"
    r")"
)


@dataclass(frozen=True, order=True)
class StreamKey:
    """One exact captured CAN stream; no component is safe to discard."""

    channel: str
    id_bits: int
    can_id: int
    dlc: int

    @property
    def namespace(self) -> str:
        return "SFF" if self.id_bits == 11 else "EFF"


def _identifier_bits(text: str, can_id: int) -> int | None:
    if len(text) <= 3 and can_id <= 0x7FF:
        return 11
    if len(text) <= 8 and can_id <= 0x1FFFFFFF:
        return 29
    return None


def parse(path):
    """Return frames keyed by channel, namespace, numeric ID, and exact DLC."""
    rows = {}
    with open(path, errors="ignore") as f:
        for ln in f:
            m = _LINE.search(ln)
            if not m:
                continue
            try:
                can_id_text = m.group("can_id")
                cid = int(can_id_text, 16)
                id_bits = _identifier_bits(can_id_text, cid)
                if id_bits is None:
                    continue
                if m.group("spaced") is not None:
                    data = bytes(
                        int(x, 16) for x in m.group("spaced").split()
                    )
                    if len(data) != int(m.group("declared_dlc")):
                        continue
                else:
                    h = m.group("compact")
                    data = bytes.fromhex(h) if h else b""
            except ValueError:
                continue
            if len(data) > 8:
                continue
            key = StreamKey(
                channel=m.group("channel"),
                id_bits=id_bits,
                can_id=cid,
                dlc=len(data),
            )
            rows.setdefault(key, []).append(data)
    return rows


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def field_value(frames: Iterable[bytes], off, width, endian):
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
    print(f"# {len(common)} exact CAN streams common to all captures\n")

    known = truths is not None
    results = []
    for stream in sorted(common):
        per = [c[stream] for c in caps]
        minlen = stream.dlc
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
                        results.append((score, r2, stream, off, width, endian, a, b, vals))
                    else:
                        # no ground truth: rank by movement, decoded sane under x0.1 / x0.01
                        a = 0.1 if width == 1 else 0.01
                        if spread == 0 or not all(V_SANE[0] <= a * v <= V_SANE[1] for v in vals):
                            continue
                        results.append((spread, 0.0, stream, off, width, endian, a, 0.0, vals))

    results.sort(reverse=True)
    if known:
        print(
            f"{'r2':>6}  {'interface':<10} {'ns':<3} {'ID':>8} {'dlc':>3} "
            f"{'off':>3} {'w':>1} {'e':>2}  {'V/LSB':>8} {'offset':>8}  "
            "decoded-per-capture"
        )
    else:
        print(
            f"{'spread':>6}  {'interface':<10} {'ns':<3} {'ID':>8} {'dlc':>3} "
            f"{'off':>3} {'w':>1} {'e':>2}  assuming  decoded-per-capture"
        )
    print("-" * 108)
    for sc, r2, stream, off, width, endian, a, b, vals in results[:top]:
        dec = "  ".join(f"{a * v + b:5.2f}" for v in vals)
        e = "" if width == 1 else ("BE" if endian == ">" else "LE")
        can_id = (
            f"{stream.can_id:03X}"
            if stream.id_bits == 11
            else f"{stream.can_id:08X}"
        )
        if known:
            print(
                f"{r2:6.3f}  {stream.channel:<10} {stream.namespace:<3} "
                f"{can_id:>8} {stream.dlc:>3} {off:>3} {width} {e:>2}  "
                f"{a:8.4f} {b:8.3f}  [{dec}] V"
            )
        else:
            print(
                f"{sc:6.1f}  {stream.channel:<10} {stream.namespace:<3} "
                f"{can_id:>8} {stream.dlc:>3} {off:>3} {width} {e:>2}  "
                f"x{a:<6}  raw->[{dec}] V"
            )
    if not results:
        print("(no plausible voltage field -- did the bus carry voltage in BOTH states? "
              "if the body bus shows nothing, voltage may only be on C-CAN with ignition on.)")
    print(
        "\nTop row's interface/namespace/ID/DLC/offset/width/scale is the "
        "candidate field; keep that complete identity in downstream evidence."
    )


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
