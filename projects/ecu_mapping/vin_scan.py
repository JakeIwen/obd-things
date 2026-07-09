#!/usr/bin/env python3
"""Which vehicle(s) is a decoded AlfaOBD log from? Reassembles every F190 (VIN) read and
prints the VIN timeline + counts. Use this FIRST — AlfaOBD debug files accumulate across
whatever vehicles the tablet touched (our 396 MB reference bin was a different Promaster).

Usage: vin_scan.py <decoded.txt> [expected_vin]
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from alfalog import iter_exchanges, decode_vin

OUR_VIN = os.environ.get("OBD_VIN", "")   # set locally; kept out of git


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    path = argv[1]
    expect = argv[2] if len(argv) > 2 else OUR_VIN
    counts = Counter()
    timeline = []
    for ex in iter_exchanges(path):
        if not ex["req"].startswith("22F190"):
            continue
        vin = decode_vin(ex["resp"])
        if vin:
            counts[vin] += 1
            timeline.append((ex["date"], ex["ts"], vin))
    print(f"F190 VIN reads: {sum(counts.values())}\n")
    print("=== distinct VINs ===")
    for v, n in counts.most_common():
        tag = "  <== EXPECTED" if v == expect else ""
        print(f"  {v!r:24} x{n}{tag}")
    print("\n=== timeline ===")
    for d, ts, v in timeline:
        print(f"  {d} {ts}  {v}{'  *' if v == expect else ''}")
    only = set(counts)
    verdict = ("ONLY the expected van" if only == {expect}
               else "expected van + others" if expect in only
               else "expected van ABSENT")
    print(f"\nverdict: {verdict}  (distinct VINs: {len(only)})")


if __name__ == "__main__":
    main(sys.argv)
