#!/usr/bin/env python3
"""Automated DID byte-slice -> physical-signal correlator (read-only).

Adapted technique from TUM-FTM's "Holistic Approach for Automated Reverse Engineering of UDS
Data" (Apache-2.0): brute-force every interpretation of a DID's bytes and least-squares-fit it
against a ground-truth signal. Their collection half is DoIP; ours is CAN/ISO-TP, so only the
analysis idea is reused (reimplemented here, no vendored code).

We use the INTERNAL-CROSS-DECODE variant: pick one DID byte-slice as the reference (ground truth)
and regress every other slice against it. The fitted slope gives the *relative* scale between two
DIDs -- e.g. confirm 0x0841 is millideg vs 0x0845 microdeg by a slope ~1000. (This pins the RATIO,
not the absolute unit; absolute scale needs an external inclinometer.)

Workflow:
    # 1. capture while you gently perturb the radar bracket up/down a few times (~30-60 s):
    python3 tools/signal_correlate.py capture radar_acc --seconds 60
    #    (default DIDs = the angle candidates + sanity rows; Ctrl-C to stop early)

    # 2. analyze: regress every captured slice against a reference slice
    python3 tools/signal_correlate.py analyze dumps/radar_acc_correlate_*.json \\
            --ground 0845:0:4:>i4

Everything here is read-only (only 22 ReadDataByIdentifier on the wire).
"""
import os
import sys
import glob
import json
import time
import datetime

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)
from lib import uds
from lib.modules import get

try:
    import numpy as np
except ImportError:
    raise SystemExit("Missing dependency: pip3 install --break-system-packages numpy")

# Default DIDs to log: the inferred angle candidates + the two VERIFIED sanity rows.
DEFAULT_DIDS = [0x0841, 0x0845, 0x0850, 0x0861, 0x1006, 0x0835]

# Datatype hypotheses per slice length (signed/unsigned int + float, both endiannesses).
DTYPES = {
    1: ["<u1", "<i1"],
    2: ["<u2", "<i2", "<f2", ">u2", ">i2", ">f2"],
    4: ["<u4", "<i4", "<f4", ">u4", ">i4", ">f4"],
    8: ["<u8", "<i8", "<f8", ">u8", ">i8", ">f8"],
}


# --- capture (read-only) -----------------------------------------------------
def capture(module, dids, seconds, outfile):
    s = uds.open_socket(module.txid, module.rxid, module.channel, timeout=1.0)
    uds.request(s, [0x10, 0x03], timeout=1.0)
    samples = []
    start = time.time()
    deadline = start + seconds if seconds > 0 else float("inf")
    print(f"# capturing {[f'0x{d:04X}' for d in dids]} on {module.key} "
          f"for {'until Ctrl-C' if seconds <= 0 else f'{seconds}s'}")
    print("# >>> NOW gently tilt the radar bracket up and down through several sweeps <<<\n")
    last_tp = time.time()
    try:
        while time.time() < deadline:
            if time.time() - last_tp > 2.0:
                uds.request(s, [0x3E, 0x00], timeout=0.5)
                last_tp = time.time()
            row = {}
            for did in dids:
                resp, _ = uds.request(s, [0x22, (did >> 8) & 0xFF, did & 0xFF])
                if resp and resp[0] == 0x62 and len(resp) >= 3:
                    row[f"{did:04X}"] = uds.hx(resp[3:]).replace(" ", "")
            samples.append({"t": time.time(), "data": row})
            n = len(samples)
            if n % 10 == 0:
                print(f"\r  samples: {n}  ({time.time()-start:.1f}s)", end="")
                _dump(module, dids, start, samples, outfile)  # checkpoint
    except KeyboardInterrupt:
        print("\n  capture stopped by user.")
    finally:
        s.close()
    _dump(module, dids, start, samples, outfile)
    print(f"\n  wrote {len(samples)} samples -> {outfile}")


def _dump(module, dids, start, samples, outfile):
    with open(outfile, "w") as f:
        json.dump({"module": module.key,
                   "starttime": datetime.datetime.fromtimestamp(start).isoformat(),
                   "dids": [f"{d:04X}" for d in dids],
                   "samples": samples}, f)


# --- analyze (offline) -------------------------------------------------------
def _slice_array(samples, did_key, start_byte, length, dtype):
    """Per-sample numeric value of bytes[start:start+length] viewed as dtype; None where absent.
    Returns (values_list, mask_list) aligned to samples."""
    vals, mask = [], []
    for smp in samples:
        hexs = smp["data"].get(did_key)
        if hexs is None:
            vals.append(0.0); mask.append(False); continue
        raw = bytes.fromhex(hexs)
        if start_byte + length > len(raw):
            vals.append(0.0); mask.append(False); continue
        seg = np.frombuffer(raw[start_byte:start_byte + length], dtype=np.uint8)
        vals.append(float(seg.view(dtype)[0])); mask.append(True)
    return np.array(vals), np.array(mask)


def _fit(x, y):
    """lstsq y = a*x + b; return (slope, intercept, r2) or None if degenerate.
    Float views of integer bytes can yield inf/nan/huge values -> guard and skip."""
    if len(x) < 3 or not np.all(np.isfinite(x)) or np.std(x) == 0 or np.std(y) == 0:
        return None
    X = np.vstack([x, np.ones(len(x))]).T
    try:
        (a, b), *_ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    yhat = a * x + b
    if not np.all(np.isfinite(yhat)):
        return None
    ss_res = float(np.sum((y - yhat) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return a, b, r2


def analyze(capture_file, ground_spec, top, r2_min):
    with open(capture_file) as f:
        cap = json.load(f)
    samples = cap["samples"]
    print(f"# {capture_file}: {len(samples)} samples, dids {cap['dids']}\n")

    g_did, g_start, g_len, g_dtype = ground_spec.split(":")
    g_start, g_len = int(g_start), int(g_len)
    y_all, y_mask = _slice_array(samples, g_did, g_start, g_len, g_dtype)
    if y_mask.sum() < 3:
        sys.exit(f"ground {ground_spec} present in <3 samples; nothing to fit.")
    if np.std(y_all[y_mask]) == 0:
        sys.exit(f"ground {ground_spec} did not vary during capture -- perturbation too small?")
    print(f"GROUND = {ground_spec}  (range {y_all[y_mask].min():.0f}..{y_all[y_mask].max():.0f}, "
          f"n={int(y_mask.sum())})\n")

    results = []
    np.seterr(over="ignore", invalid="ignore")  # float/u8 byte-views can overflow; guarded in _fit
    for did_key in cap["dids"]:
        # longest byte length seen for this did
        lens = [len(bytes.fromhex(s["data"][did_key])) for s in samples if did_key in s["data"]]
        if not lens:
            continue
        nbytes = max(lens)
        for length, dtypes in DTYPES.items():
            for start_byte in range(nbytes - length + 1):
                for dtype in dtypes:
                    x_all, x_mask = _slice_array(samples, did_key, start_byte, length, dtype)
                    m = x_mask & y_mask
                    if m.sum() < 3:
                        continue
                    if did_key == g_did and start_byte < g_start + g_len and g_start < start_byte + length:
                        continue  # skip slices overlapping the ground field (trivially collinear)
                    fit = _fit(x_all[m], y_all[m])
                    if fit is None:
                        continue
                    a, b, r2 = fit
                    if r2 >= r2_min:
                        results.append((r2, did_key, start_byte, length, dtype, a, b, int(m.sum())))

    results.sort(reverse=True)
    print(f"{'r2':>7}  {'DID':>5} {'off':>3} {'len':>3} {'dtype':>5}  {'slope':>14} {'intercept':>12} {'n':>4}")
    print("-" * 70)
    for r2, did_key, sb, ln, dt, a, b, n in results[:top]:
        print(f"{r2:7.4f}  {did_key:>5} {sb:>3} {ln:>3} {dt:>5}  {a:14.6g} {b:12.4g} {n:>4}")
    if not results:
        print("(no fits above r2_min; try a larger/cleaner perturbation or lower --r2-min)")
    print("\nInterpretation: a high-r2 row whose DID/slice differs from GROUND measures the same")
    print("physical quantity; |slope| is its scale RELATIVE to ground (e.g. ~1000 => millideg vs")
    print("microdeg). Record confirmed scales in findings/radar_acc_did_findings.md.")


# --- cli ---------------------------------------------------------------------
def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("capture", "analyze"):
        sys.exit("usage: signal_correlate.py {capture <module> | analyze <file>} [opts]  (-h in docstring)")
    mode = sys.argv[1]
    args = sys.argv[2:]

    def opt(flag, default=None):
        return args[args.index(flag) + 1] if flag in args else default

    if mode == "capture":
        module = get(args[0] if args and not args[0].startswith("-") else "radar_acc")
        dids = ([int(x, 16) for x in opt("--dids").split(",")] if opt("--dids") else DEFAULT_DIDS)
        seconds = float(opt("--seconds", "0"))
        out = opt("-o") or os.path.join(
            REPO, "dumps", f"{module.key}_correlate_{time.strftime('%Y%m%d_%H%M%S')}.json")
        capture(module, dids, seconds, out)
    else:
        path = args[0] if args and not args[0].startswith("-") else None
        if path and "*" in path:
            path = sorted(glob.glob(path))[-1]
        if not path or not os.path.exists(path):
            sys.exit("analyze: pass an existing capture file (or glob)")
        ground = opt("--ground", "0845:0:4:>i4")
        top = int(opt("--top", "25"))
        r2_min = float(opt("--r2-min", "0.5"))
        analyze(path, ground, top, r2_min)


if __name__ == "__main__":
    main()
