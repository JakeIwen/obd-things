#!/usr/bin/env python3
"""Offline DID correlator with a safety-gated active UDS capture companion.

Adapted technique from TUM-FTM's "Holistic Approach for Automated Reverse Engineering of UDS
Data" (Apache-2.0): brute-force every interpretation of a DID's bytes and least-squares-fit it
against a ground-truth signal. Their collection half is DoIP; ours is CAN/ISO-TP, so only the
analysis idea is reused (reimplemented here, no vendored code).

We use the INTERNAL-CROSS-DECODE variant: pick one DID byte-slice as the reference (ground truth)
and regress every other slice against it. The fitted slope gives the *relative* scale between two
DIDs -- e.g. confirm 0x0841 is millideg vs 0x0845 microdeg by a slope ~1000. (This pins the RATIO,
not the absolute unit; absolute scale needs an external inclinometer.)

Workflow:
    # 1. dry-run a bounded capture plan:
    python3 tools/signal_correlate.py capture radar_acc --seconds 60
    # 2. parked live capture after explicitly arming the bus:
    python3 tools/signal_correlate.py capture radar_acc --seconds 60 --execute \
            --confirm-parked --confirm-session-change --confirm-no-active-routine \
            --pair 6/14 --conditions "ignition ON, engine OFF, SGW-bypass C-CAN"
    #    (default DIDs = the angle candidates + sanity rows; Ctrl-C to stop early)

    # 3. analyze: regress every captured slice against a reference slice
    python3 tools/signal_correlate.py analyze tmp/sweeps/radar_acc_correlate_*.json \\
            --ground '0845:0:4:>i4'

Capture mode enters an extended session and sends TesterPresent plus non-mutating 22
ReadDataByIdentifier requests; it is ACTIVE, not passive, and takes the channel diagnostic lock.
"""
import os
import sys
import argparse
import glob
import json
import math
import time
import datetime

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)
from lib import canbus, diagnostic_safety, uds
from lib.modules import get
from tools.ecu_discover import preflight

try:
    import numpy as np
except ImportError:
    raise SystemExit("Missing dependency: pip3 install --break-system-packages numpy")

# Default DIDs to log: the inferred angle candidates + the two VERIFIED sanity rows.
DEFAULT_DIDS = [0x0841, 0x0845, 0x0850, 0x0861, 0x1006, 0x0835]
SESSION = 0x03
MAX_CAPTURE_SECONDS = 600.0
MAX_REQUEST_RATE = 10.0
MIN_REQUEST_RATE = 0.5
MAX_REQUESTS = 5000
MAX_DIDS = 64
TESTER_PRESENT_INTERVAL_S = 2.0

# Datatype hypotheses per slice length (signed/unsigned int + float, both endiannesses).
DTYPES = {
    1: ["<u1", "<i1"],
    2: ["<u2", "<i2", "<f2", ">u2", ">i2", ">f2"],
    4: ["<u4", "<i4", "<f4", ">u4", ">i4", ">f4"],
    8: ["<u8", "<i8", "<f8", ">u8", ">i8", ">f8"],
}


# --- capture (active diagnostic reads) --------------------------------------
class CaptureError(RuntimeError):
    pass


def _response_category(payload, response):
    if response is None:
        return "timeout"
    response = bytes(response)
    if len(response) >= 3 and response[0] == 0x7F and response[1] == payload[0]:
        return "negative"
    if payload == bytes.fromhex("10 03") and response[:2] == bytes.fromhex("50 03"):
        return "positive"
    if payload == bytes.fromhex("3E 00") and response[:2] == bytes.fromhex("7E 00"):
        return "positive"
    if len(payload) == 3 and payload[0] == 0x22 and response[:3] == bytes((0x62, payload[1], payload[2])):
        return "positive"
    return "unexpected"


def capture(
    module,
    dids,
    seconds,
    outfile,
    *,
    request_rate=5.0,
    max_requests=1000,
    timeout=0.75,
    pair=None,
    conditions=None,
    confirmed_parked=False,
    confirmed_session_change=False,
    confirmed_no_active_routine=False,
):
    """Execute one already-confirmed bounded capture and return its final report.

    This is the active core used by the dry-run-default CLI. Direct callers must provide the same
    safety assertions; there is no compatibility path that silently transmits.
    """
    if not math.isfinite(seconds) or not 0 <= seconds <= MAX_CAPTURE_SECONDS:
        raise CaptureError(f"seconds must be finite and between 0 and {MAX_CAPTURE_SECONDS:g}")
    if not math.isfinite(request_rate) or not MIN_REQUEST_RATE <= request_rate <= MAX_REQUEST_RATE:
        raise CaptureError(
            f"request_rate must be between {MIN_REQUEST_RATE:g} and {MAX_REQUEST_RATE:g}"
        )
    if not isinstance(max_requests, int) or isinstance(max_requests, bool) or not 1 <= max_requests <= MAX_REQUESTS:
        raise CaptureError(f"max_requests must be between 1 and {MAX_REQUESTS}")
    if not math.isfinite(timeout) or not 0 < timeout <= 5.0:
        raise CaptureError("timeout must be finite, >0, and <=5 seconds")
    dids = list(dict.fromkeys(dids))
    if not dids or len(dids) > MAX_DIDS or any(
        not isinstance(did, int) or isinstance(did, bool) or not 0 <= did <= 0xFFFF
        for did in dids
    ):
        raise CaptureError(f"capture requires 1-{MAX_DIDS} unique 16-bit integer DIDs")
    if not confirmed_parked or not pair or not conditions:
        raise CaptureError("live capture requires parked, pair, and conditions assertions")
    if not confirmed_session_change or not confirmed_no_active_routine:
        raise CaptureError("live capture requires explicit session-state confirmations")
    errors = preflight(module.channel, module.bitrate)
    if errors:
        raise CaptureError("; ".join(errors))

    try:
        lock = diagnostic_safety.acquire_channel_lock(module.channel)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CaptureError(f"refusing to capture: {exc}") from None

    samples = []
    started_wall = time.time()
    started_mono = time.monotonic()
    last_send = None
    last_tp = started_mono
    request_attempts = 0
    category_counts = {}
    sock = None
    interrupted = False
    fatal_error = None
    restored_passive = False
    stop_reason = None

    def report(status="running"):
        return {
            "tool": "tools/signal_correlate.py capture",
            "interaction": "active diagnostic session plus physical ReadDataByIdentifier",
            "status": status,
            "module": module.key,
            "channel": module.channel,
            "bitrate": module.bitrate,
            "addressing_mode": module.addressing_mode,
            "txid": f"{module.txid:X}",
            "rxid": f"{module.rxid:X}",
            "physical_pair": pair,
            "conditions": conditions,
            "parked_asserted": confirmed_parked,
            "session": "03",
            "session_change_confirmed": confirmed_session_change,
            "no_active_routine_confirmed": confirmed_no_active_routine,
            "starttime": datetime.datetime.fromtimestamp(started_wall).astimezone().isoformat(),
            "dids": [f"{did:04X}" for did in dids],
            "duration_limit_s": seconds,
            "request_rate_limit_hz": request_rate,
            "request_limit": max_requests,
            "request_attempts": request_attempts,
            "response_categories": dict(sorted(category_counts.items())),
            "stop_reason": stop_reason,
            "interrupted": interrupted,
            "fatal_error": fatal_error,
            "restored_passive": restored_passive,
            "samples": samples,
        }

    def send(payload, response_timeout=None):
        nonlocal last_send, request_attempts
        if request_attempts >= max_requests:
            raise CaptureError("request budget exhausted")
        now = time.monotonic()
        interval = 1.0 / request_rate
        if last_send is not None:
            time.sleep(max(0.0, interval - (now - last_send)))
        uds.drain(sock)
        last_send = time.monotonic()
        request_attempts += 1
        response, status = uds.request(
            sock,
            payload,
            timeout=response_timeout or timeout,
            retries=0,
            response_pending_timeout=max(5.0, timeout * 5.0),
            max_pending_responses=32,
        )
        category = _response_category(bytes(payload), response)
        category_counts[category] = category_counts.get(category, 0) + 1
        return response, status, category

    with diagnostic_safety.interrupt_on_termination() as termination:
        try:
            sock = uds.open_module_socket(module, timeout=timeout)
            response, _, category = send(bytes.fromhex("10 03"))
            if category != "positive":
                raise CaptureError(
                    "extended session was not acknowledged with exact 50 03 echo "
                    f"({uds.hx(response) if response else category})"
                )
            last_tp = time.monotonic()
            _dump(module, dids, started_wall, samples, outfile, metadata=report())
            print(
                f"# capturing {[f'0x{did:04X}' for did in dids]} on {module.key} "
                f"for at most {seconds:g}s / {max_requests} requests"
            )
            print("# >>> NOW gently perturb one controlled variable through several sweeps <<<\n")

            deadline = started_mono + seconds
            while time.monotonic() < deadline:
                if request_attempts >= max_requests:
                    stop_reason = "request_limit"
                    break
                if time.monotonic() - last_tp >= TESTER_PRESENT_INTERVAL_S:
                    response, _, category = send(bytes.fromhex("3E 00"), min(timeout, 0.5))
                    if category != "positive":
                        raise CaptureError(
                            "TesterPresent was not acknowledged with exact 7E 00 echo "
                            f"({uds.hx(response) if response else category})"
                        )
                    last_tp = time.monotonic()

                row = {}
                for did in dids:
                    if time.monotonic() >= deadline or request_attempts >= max_requests:
                        break
                    payload = bytes((0x22, did >> 8, did & 0xFF))
                    response, _, category = send(payload)
                    if category == "positive":
                        row[f"{did:04X}"] = bytes(response[3:]).hex().upper()
                    elif category == "unexpected":
                        raise CaptureError(
                            f"DID {did:04X} received a response without exact 62 {did:04X} echo"
                        )
                samples.append({"t": time.time(), "data": row})
                if len(samples) % 10 == 0:
                    print(f"\r  samples: {len(samples)}  requests: {request_attempts}", end="")
                    _dump(module, dids, started_wall, samples, outfile, metadata=report())
            if stop_reason is None:
                stop_reason = "duration_limit" if time.monotonic() >= deadline else "complete"
        except KeyboardInterrupt:
            interrupted = True
            stop_reason = "interrupted"
            print("\n  capture stopped by user.")
        except Exception as exc:
            fatal_error = f"{type(exc).__name__}: {exc}"
            stop_reason = "failed"
            print(f"\nERROR: {fatal_error}", file=sys.stderr)
        finally:
            termination.begin_cleanup()
            try:
                if sock is not None:
                    sock.close()
            except Exception as exc:
                if fatal_error is None:
                    fatal_error = f"socket close failed: {type(exc).__name__}: {exc}"
                    stop_reason = "failed"
            finally:
                try:
                    restored_passive = bool(canbus.restore_passive(module.channel, module.bitrate))
                except Exception as exc:
                    if fatal_error is None:
                        fatal_error = f"passive restore failed: {type(exc).__name__}: {exc}"
                        stop_reason = "failed"
                finally:
                    diagnostic_safety.release_channel_lock(lock)

    if termination.received_signal is not None:
        interrupted = True
        if fatal_error is None:
            stop_reason = "interrupted"

    status = (
        "interrupted"
        if interrupted and fatal_error is None and restored_passive
        else "complete"
        if fatal_error is None and restored_passive
        else "failed"
    )
    final_report = report(status)
    _dump(module, dids, started_wall, samples, outfile, metadata=final_report)
    print(f"\n  wrote {len(samples)} samples -> {outfile}")
    print(f"  adapter restored passive: {'yes' if restored_passive else 'NO - CHECK IT NOW'}")
    return final_report


def _dump(module, dids, start, samples, outfile, metadata=None):
    """Atomically checkpoint a capture so interruption cannot leave truncated JSON."""
    payload = metadata or {
        "module": module.key,
        "starttime": datetime.datetime.fromtimestamp(start).isoformat(),
        "dids": [f"{did:04X}" for did in dids],
        "samples": samples,
    }
    directory = os.path.dirname(outfile) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = f"{outfile}.tmp-{os.getpid()}"
    with open(temporary, "w") as handle:
        json.dump(payload, handle)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, outfile)


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
def parse_dids(text):
    values = []
    for item in text.split(","):
        try:
            did = int(item, 16)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid hexadecimal DID: {item!r}") from None
        if not 0 <= did <= 0xFFFF:
            raise argparse.ArgumentTypeError("DIDs must be between 0000 and FFFF")
        if did not in values:
            values.append(did)
    if not values:
        raise argparse.ArgumentTypeError("at least one DID is required")
    if len(values) > MAX_DIDS:
        raise argparse.ArgumentTypeError(f"at most {MAX_DIDS} unique DIDs may be captured")
    return values


def cli_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)

    capture_parser = sub.add_parser("capture", help="plan or execute a bounded active DID capture")
    capture_parser.add_argument("module", nargs="?", default="radar_acc")
    capture_parser.add_argument("--dids", type=parse_dids, default=list(DEFAULT_DIDS))
    capture_parser.add_argument("--seconds", type=float, default=60.0)
    capture_parser.add_argument("--rate", type=float, default=5.0, help="maximum total UDS requests/s")
    capture_parser.add_argument("--max-requests", type=int, default=1000)
    capture_parser.add_argument("--timeout", type=float, default=0.75)
    capture_parser.add_argument("--session", default="03", choices=("03",), help="fixed reviewed session")
    capture_parser.add_argument("-o", "--output")
    capture_parser.add_argument("--execute", action="store_true")
    capture_parser.add_argument("--confirm-parked", action="store_true")
    capture_parser.add_argument("--confirm-session-change", action="store_true")
    capture_parser.add_argument("--confirm-no-active-routine", action="store_true")
    capture_parser.add_argument("--pair")
    capture_parser.add_argument("--conditions")

    analyze_parser = sub.add_parser("analyze", help="analyze one completed capture offline")
    analyze_parser.add_argument("paths", nargs="+", help="capture path or quoted glob")
    analyze_parser.add_argument("--ground", default="0845:0:4:>i4")
    analyze_parser.add_argument("--top", type=int, default=25)
    analyze_parser.add_argument("--r2-min", type=float, default=0.5)
    return p


def _resolve_analysis_path(path_args):
    matches = []
    for value in path_args:
        if glob.has_magic(value):
            matches.extend(glob.glob(value))
        elif os.path.exists(value):
            matches.append(value)
    files = sorted({path for path in matches if os.path.isfile(path)})
    return files[-1] if files else None


def main(argv=None):
    args = cli_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.mode == "analyze":
        path = _resolve_analysis_path(args.paths)
        if path is None:
            print(
                "ERROR: analyze matched no existing capture files; quote globs so the tool can resolve them",
                file=sys.stderr,
            )
            return 2
        if args.top <= 0 or not math.isfinite(args.r2_min):
            print("ERROR: --top must be positive and --r2-min must be finite", file=sys.stderr)
            return 2
        print(f"analyze selected: {path}")
        analyze(path, args.ground, args.top, args.r2_min)
        return 0

    module = get(args.module)
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= MAX_CAPTURE_SECONDS:
        print(f"ERROR: --seconds must be >0 and <= {MAX_CAPTURE_SECONDS:g}", file=sys.stderr)
        return 2
    if not math.isfinite(args.rate) or not MIN_REQUEST_RATE <= args.rate <= MAX_REQUEST_RATE:
        print(
            f"ERROR: --rate must be between {MIN_REQUEST_RATE:g} and {MAX_REQUEST_RATE:g}",
            file=sys.stderr,
        )
        return 2
    if not isinstance(args.max_requests, int) or not 2 <= args.max_requests <= MAX_REQUESTS:
        print(f"ERROR: --max-requests must be between 2 and {MAX_REQUESTS}", file=sys.stderr)
        return 2
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 5.0:
        print("ERROR: --timeout must be finite, >0, and <=5 seconds", file=sys.stderr)
        return 2

    out = args.output or os.path.join(
        REPO,
        "tmp",
        "sweeps",
        f"{module.key}_correlate_{time.strftime('%Y%m%d_%H%M%S')}.json",
    )
    print(f"ACTIVE SIGNAL-CORRELATION PLAN: {module.key}")
    print(
        f"session=10 03; DIDs={' '.join(f'{did:04X}' for did in args.dids)}; "
        f"duration<={args.seconds:g}s; rate<={args.rate:g}/s; requests<={args.max_requests}"
    )
    print(f"output: {out}")
    if not args.execute:
        print("DRY RUN: no directory, checkpoint, preflight, CAN socket, or transmission occurred.")
        return 0
    if (
        not args.confirm_parked
        or not args.pair
        or not args.conditions
        or not args.confirm_session_change
        or not args.confirm_no_active_routine
    ):
        print(
            "ERROR: --execute requires --confirm-parked, --pair, --conditions, "
            "--confirm-session-change, and --confirm-no-active-routine",
            file=sys.stderr,
        )
        return 2

    try:
        final_report = capture(
            module,
            args.dids,
            args.seconds,
            out,
            request_rate=args.rate,
            max_requests=args.max_requests,
            timeout=args.timeout,
            pair=args.pair,
            conditions=args.conditions,
            confirmed_parked=True,
            confirmed_session_change=True,
            confirmed_no_active_routine=True,
        )
    except CaptureError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0 if final_report["status"] == "complete" else 130 if final_report["interrupted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
