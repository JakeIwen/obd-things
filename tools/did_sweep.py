#!/usr/bin/env python3
"""Bounded, resumable-evidence DID inventory for one verified module.

The legacy positional form is preserved, but every invocation is a dry-run unless ``--execute``
is supplied::

    python3 tools/did_sweep.py radar_acc 0800 08FF
    python3 tools/did_sweep.py radar_acc 0800 08FF --execute --confirm-parked \
        --pair 6/14 --conditions "parked, ignition ON, engine OFF"

No DiagnosticSessionControl request is sent by default. ``--session 03`` is an explicit state
change and additionally requires ``--confirm-session-change``; session bytes with the response-
suppression bit set are refused. A full 0000-FFFF inventory needs both ``--full-range`` and
``--confirm-expanded-scan`` for live use.

All results are VIN-redacted and appended to JSONL as they arrive under
``tmp/inventories/<module>/``. A small atomic summary JSON records completion/restoration state;
the historical text format under ``tmp/sweeps/`` is generated only after a clean complete run.
"""

import argparse
from collections import Counter
import datetime
import json
import math
import os
import signal
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

from lib import canbus, diagnostic_safety, uds
from lib.modules import get
from tools.ecu_discover import preflight
from tools.identity_inventory import redact_response_vins


MIN_REQUEST_RATE = 0.1
MAX_REQUEST_RATE = 5.0
MAX_REQUEST_TIMEOUT_S = 5.0
MAX_BOUNDED_DIDS = 512
SERVICE_SHAPE_ABORT_COUNT = 8
TESTER_PRESENT_INTERVAL_S = 2.0
MIN_EXPLICIT_SESSION_RATE_HZ = 1.0 / TESTER_PRESENT_INTERVAL_S


def parse_hex16(text):
    try:
        value = int(text, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid hexadecimal value: {text!r}") from None
    if not 0 <= value <= 0xFFFF:
        raise argparse.ArgumentTypeError("value must be between 0000 and FFFF")
    return value


def parse_session(text):
    try:
        value = int(text, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid hexadecimal session: {text!r}") from None
    # Bit 7 is suppressPosRspMsgIndicationBit. It is incompatible with the exact positive-session
    # echo required before this tool will begin scanning, so suppressed 80-FF values are refused.
    if not 1 <= value <= 0x7F:
        raise argparse.ArgumentTypeError("session must be a hexadecimal byte from 01 through 7F")
    return value


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("module", nargs="?", default="radar_acc", help="verified key from lib/modules.py")
    p.add_argument("start", nargs="?", type=parse_hex16, help="first DID, hexadecimal")
    p.add_argument("end", nargs="?", type=parse_hex16, help="last DID, hexadecimal")
    p.add_argument("--full-range", action="store_true", help="explicitly select 0000-FFFF")
    p.add_argument(
        "--confirm-expanded-scan",
        action="store_true",
        help=f"required live for more than {MAX_BOUNDED_DIDS} DIDs",
    )
    p.add_argument("--execute", action="store_true", help="actually send the planned physical reads")
    p.add_argument("--confirm-parked", action="store_true", help="assert the vehicle is parked")
    p.add_argument("--pair", help="physical DLC/tap pair; required with --execute")
    p.add_argument("--conditions", help="ignition/engine/wake state; required with --execute")
    p.add_argument("--rate", type=float, default=2.0, help="maximum total requests/s (default: 2)")
    p.add_argument("--timeout", type=float, default=0.75, help="seconds per request (default: 0.75)")
    p.add_argument(
        "--session",
        type=parse_session,
        metavar="HEX",
        help=(
            f"explicit diagnostic session byte 01-7F; response suppression is refused and "
            f"--rate must be >= {MIN_EXPLICIT_SESSION_RATE_HZ:g}"
        ),
    )
    p.add_argument(
        "--confirm-session-change",
        action="store_true",
        help="required live with --session",
    )
    return p


def selected_range(args):
    if args.full_range and (args.start is not None or args.end is not None):
        raise ValueError("--full-range cannot be combined with positional START/END")
    if args.full_range or args.start is None:
        start, end = 0x0000, 0xFFFF
    else:
        start = args.start
        # Preserve legacy START-only behavior, but expanded confirmation is still mandatory live.
        end = args.end if args.end is not None else 0xFFFF
    if start > end:
        raise ValueError("START must be <= END")
    return start, end


def classify_did_response(did, response):
    if response is None:
        return "timeout"
    expected = bytes((0x62, did >> 8, did & 0xFF))
    if len(response) >= 3 and response[:3] == expected:
        return "positive"
    if len(response) >= 3 and response[0] == 0x7F and response[1] == 0x22:
        return {
            0x11: "service_not_supported",
            0x12: "subfunction_not_supported",
            0x13: "incorrect_length_or_format",
            0x22: "conditions_not_correct",
            0x31: "out_of_range_current_session",
            0x33: "security_denied",
            0x7E: "session_restricted",
            0x7F: "session_restricted",
        }.get(response[2], "negative_other")
    if response and response[0] == 0x7F:
        return "malformed_or_wrong_service_negative"
    return "unexpected"


def classify_session_response(session, response):
    """Require the positive SID and requested session echo; timing bytes may follow."""
    if not response:
        return "timeout"
    response = bytes(response)
    if len(response) >= 3 and response[:2] == bytes.fromhex("7F 10"):
        return "negative"
    if len(response) >= 2 and response[:2] == bytes((0x50, session)):
        return "positive_echo"
    return "unexpected"


def classify_tester_present_response(response):
    """Validate the non-suppressed TesterPresent response ``7E 00``."""
    if not response:
        return "timeout"
    response = bytes(response)
    if len(response) >= 3 and response[:2] == bytes.fromhex("7F 3E"):
        return "negative"
    if response == bytes.fromhex("7E 00"):
        return "positive_echo"
    return "unexpected"


def request_once(sock, payload, timeout, request_attempts=None, responses_received=None,
                 counter_key=None):
    """Drain and make one UDS call while recording attempt/response semantics honestly.

    An attempt is counted immediately before ``uds.request``. A non-empty response is counted
    only after that call returns, so receive-side exceptions remain visible as attempts without
    being mislabeled as confirmed responses.
    """
    uds.drain(sock)
    if request_attempts is not None:
        request_attempts[counter_key] = request_attempts.get(counter_key, 0) + 1
    response, status = uds.request(sock, payload, timeout=timeout, retries=0)
    if response and responses_received is not None:
        responses_received[counter_key] = responses_received.get(counter_key, 0) + 1
    return response, status


def tester_present(sock, timeout, request_attempts, responses_received, events):
    """Send, validate, and preserve one explicit-session keepalive result."""
    payload = bytes.fromhex("3E 00")
    started = time.monotonic()
    try:
        response, status = request_once(
            sock,
            payload,
            timeout,
            request_attempts=request_attempts,
            responses_received=responses_received,
            counter_key="tester_present",
        )
    except Exception as exc:
        events.append(
            {
                "request_hex": uds.hx(payload),
                "response_hex": None,
                "category": "transport_error",
                "validated_echo": False,
                "status": f"{type(exc).__name__}: {exc}",
                "negative_response": None,
                "elapsed_s": round(time.monotonic() - started, 3),
            }
        )
        raise RuntimeError("TesterPresent transport failure; explicit session is uncertain") from exc
    category = classify_tester_present_response(response)
    event = {
        "request_hex": uds.hx(payload),
        "response_hex": uds.hx(response) if response else None,
        "category": category,
        "validated_echo": category == "positive_echo",
        "status": status,
        "negative_response": uds.negative_response_details(response),
        "elapsed_s": round(time.monotonic() - started, 3),
    }
    events.append(event)
    if not event["validated_echo"]:
        raise RuntimeError(
            f"TesterPresent was not acknowledged with exact 7E 00 echo ({category}); "
            "explicit session is uncertain"
        )
    return event


def query_did(sock, did, timeout, request_attempts=None, responses_received=None):
    request = bytes((0x22, did >> 8, did & 0xFF))
    started = time.monotonic()
    response, status = request_once(
        sock,
        request,
        timeout,
        request_attempts=request_attempts,
        responses_received=responses_received,
        counter_key="did_reads",
    )
    category = classify_did_response(did, response)
    safe_response, vin_redacted = redact_response_vins(did, response)
    data = safe_response[3:] if category == "positive" else b""
    return {
        "did": f"{did:04X}",
        "request_hex": uds.hx(request),
        "response_hex": uds.hx(safe_response) if safe_response else None,
        "data_hex": uds.hx(data) if data else None,
        "ascii": "".join(chr(byte) if 32 <= byte < 127 else "." for byte in data) if data else None,
        "category": category,
        "status": status,
        "negative_response": uds.negative_response_details(response),
        "vin_redacted": vin_redacted,
        "elapsed_s": round(time.monotonic() - started, 3),
    }


def output_paths(module):
    stamp = datetime.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f%z")
    directory = os.path.join(REPO, "tmp", "inventories", module.key)
    stem = os.path.join(directory, f"dids_{stamp}")
    return stem + ".summary.json", stem + ".results.jsonl"


def atomic_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp-{os.getpid()}"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass


def legacy_path(module, start, end):
    full = start == 0 and end == 0xFFFF
    name = (
        f"{module.key}_did_sweep.txt"
        if full
        else f"{module.key}_did_sweep_{start:04X}-{end:04X}.txt"
    )
    return os.path.join(REPO, "tmp", "sweeps", name)


def write_legacy_text(path, module, start, end, jsonl_path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp-{os.getpid()}"
    with open(temporary, "w") as output, open(jsonl_path) as source:
        output.write(f"# {module.key} DID sweep 22 over 0x{start:04X}-0x{end:04X}\n")
        for line in source:
            item = json.loads(line)
            category = item["category"]
            if category == "positive":
                output.write(
                    f"{item['did']} OK  data={item['data_hex'] or ''}  "
                    f"|{item['ascii'] or ''}|\n"
                )
            elif category == "security_denied":
                output.write(f"{item['did']} LOCKED\n")
            elif category == "timeout":
                output.write(f"{item['did']} UNRESOLVED\n")
            elif category != "out_of_range_current_session":
                output.write(
                    f"{item['did']} OTHER {item['response_hex'] or item['status']}\n"
                )
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def base_report(module, args, start, end, summary_path, results_path, started_at):
    return {
        "schema_version": 2,
        "tool": "tools/did_sweep.py",
        "interaction": "active non-mutating UDS ReadDataByIdentifier inventory",
        "status": "running",
        "started_at": started_at,
        "completed_at": None,
        "module": {
            "key": module.key,
            "name": module.name,
            "bus": module.bus,
            "channel": module.channel,
            "bitrate": module.bitrate,
            "addressing_mode": module.addressing_mode,
            "txid": f"{module.txid:X}",
            "rxid": f"{module.rxid:X}",
        },
        "physical_pair": args.pair,
        "conditions": args.conditions,
        "parked_asserted": args.confirm_parked,
        "did_start": f"{start:04X}",
        "did_end": f"{end:04X}",
        "planned_dids": end - start + 1,
        "max_request_rate_hz": args.rate,
        "timeout_s": args.timeout,
        "requested_session": f"{args.session:02X}" if args.session is not None else None,
        "diagnostic_session_policy": (
            "explicit session change" if args.session is not None else "inherited/unknown; no 10 sent"
        ),
        "summary_path": os.path.relpath(summary_path, REPO),
        "results_jsonl": os.path.relpath(results_path, REPO),
        "results_written": 0,
        "category_counts": {},
        # These are calls initiated at the uds.request boundary, not claims of on-wire delivery.
        "request_attempts": {"session_control": 0, "tester_present": 0, "did_reads": 0},
        "responses_received": {"session_control": 0, "tester_present": 0, "did_reads": 0},
        "transmit_counts": {"session_control": 0, "tester_present": 0},
        "count_semantics": (
            "request_attempts counts uds.request calls initiated before the call; "
            "responses_received counts non-empty responses returned, so a receive exception "
            "remains an attempt without a confirmed response; transmit_counts is a legacy "
            "session/keepalive-only mirror of request_attempts, not proof of wire delivery"
        ),
        "session_response": None,
        "tester_present_results": [],
        "session_state": (
            "explicit_session_not_established"
            if args.session is not None
            else "inherited_unknown"
        ),
        "interrupted": False,
        "fatal_error": None,
        "restored_passive": False,
        "legacy_text": None,
    }


def main(argv=None):
    args = parser().parse_args(argv)
    module = get(args.module)
    try:
        start, end = selected_range(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    count = end - start + 1

    if not math.isfinite(args.rate) or not MIN_REQUEST_RATE <= args.rate <= MAX_REQUEST_RATE:
        print(
            f"ERROR: --rate must be between {MIN_REQUEST_RATE:g} and {MAX_REQUEST_RATE:g}",
            file=sys.stderr,
        )
        return 2
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= MAX_REQUEST_TIMEOUT_S:
        print(
            f"ERROR: --timeout must be >0 and <= {MAX_REQUEST_TIMEOUT_S:g} seconds",
            file=sys.stderr,
        )
        return 2
    if args.confirm_session_change and args.session is None:
        print("ERROR: --confirm-session-change requires --session", file=sys.stderr)
        return 2
    if args.session is not None and args.rate < MIN_EXPLICIT_SESSION_RATE_HZ:
        print(
            f"ERROR: --session requires --rate >= {MIN_EXPLICIT_SESSION_RATE_HZ:g}; "
            f"slower rates can exceed the {TESTER_PRESENT_INTERVAL_S:g}s keepalive cadence",
            file=sys.stderr,
        )
        return 2

    estimated = count / args.rate
    print(f"ACTIVE DID INVENTORY PLAN: {module.key} {start:04X}-{end:04X} ({count} physical 22 reads)")
    print(
        f"{module.addressing_mode} {module.bitrate} bit/s TX={module.txid:X} RX={module.rxid:X}; "
        f"max_rate={args.rate:g}/s; minimum request cadence={estimated / 60:.1f} min"
    )
    print(
        "session: "
        + (f"explicit 10 {args.session:02X}" if args.session is not None else "inherited/unknown (no 10 or 3E)")
    )
    if not args.execute:
        print("DRY RUN: no report opened, no CAN socket opened, and nothing transmitted.")
        return 0

    if not args.confirm_parked or not args.pair or not args.conditions:
        print(
            "ERROR: --execute requires --confirm-parked, --pair, and --conditions",
            file=sys.stderr,
        )
        return 2
    if args.start is None and not args.full_range:
        print("ERROR: live full range requires explicit --full-range", file=sys.stderr)
        return 2
    if count > MAX_BOUNDED_DIDS and not args.confirm_expanded_scan:
        print(
            f"ERROR: {count} DIDs exceeds bounded limit {MAX_BOUNDED_DIDS}; "
            "add --confirm-expanded-scan",
            file=sys.stderr,
        )
        return 2
    if args.session is not None and not args.confirm_session_change:
        print("ERROR: --session requires --confirm-session-change with --execute", file=sys.stderr)
        return 2
    errors = preflight(module.channel, module.bitrate)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2

    try:
        diagnostic_lock = diagnostic_safety.acquire_channel_lock(module.channel)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    interval = 1.0 / args.rate
    last_transmit = None
    last_tester_present = None
    categories = Counter()
    result_count = 0
    consecutive_shape_failures = 0
    interrupted = False
    fatal_errors = []
    restored_passive = False
    sock = None
    summary_path = None
    results_path = None
    started_at = None
    report = None
    legacy = None
    received_signal = None
    cleanup_started = False
    old_handlers = {}

    def wait_for_rate():
        nonlocal last_transmit
        if last_transmit is not None:
            time.sleep(max(0.0, interval - (time.monotonic() - last_transmit)))

    def send(payload, timeout, counter_key):
        nonlocal last_transmit
        wait_for_rate()
        last_transmit = time.monotonic()
        return request_once(
            sock,
            payload,
            timeout,
            request_attempts=report["request_attempts"],
            responses_received=report["responses_received"],
            counter_key=counter_key,
        )

    def append_fatal(message):
        fatal_errors.append(message)
        print(f"ERROR: {message}", file=sys.stderr)

    def interrupt_handler(signum, _frame):
        nonlocal received_signal, interrupted
        if received_signal is None:
            received_signal = signum
            interrupted = True
            # Repeated INT/TERM/HUP must not interrupt socket close, passive restoration, final
            # report publication, or lock release. A first signal during cleanup is recorded too.
            if not cleanup_started:
                raise KeyboardInterrupt

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            old_handlers[signum] = signal.signal(signum, interrupt_handler)
        # Every fallible path after lock acquisition lives below this try so setup/write failures
        # still close, restore passive, publish what evidence is available, and release the lock.
        summary_path, results_path = output_paths(module)
        started_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        report = base_report(module, args, start, end, summary_path, results_path, started_at)
        atomic_json(summary_path, report)
        os.makedirs(os.path.dirname(results_path), exist_ok=True)
        last_tester_present = time.monotonic()
        sock = uds.open_module_socket(module, timeout=args.timeout)
        if args.session is not None:
            session_started = time.monotonic()
            response, status = send(
                bytes((0x10, args.session)), args.timeout, "session_control"
            )
            session_category = classify_session_response(args.session, response)
            report["session_response"] = {
                "request_hex": uds.hx(bytes((0x10, args.session))),
                "response_hex": uds.hx(response) if response else None,
                "category": session_category,
                "validated_echo": session_category == "positive_echo",
                "status": status,
                "negative_response": uds.negative_response_details(response),
                "elapsed_s": round(time.monotonic() - session_started, 3),
            }
            if session_category != "positive_echo":
                raise RuntimeError(
                    f"session 10 {args.session:02X} was not acknowledged with exact "
                    f"50 {args.session:02X} echo ({session_category})"
                )
            report["session_state"] = "explicit_session_confirmed"
            last_tester_present = time.monotonic()

        with open(results_path, "a", buffering=1, encoding="utf-8") as results_file:
            for did in range(start, end + 1):
                if (
                    args.session is not None
                    and time.monotonic() - last_tester_present >= TESTER_PRESENT_INTERVAL_S
                ):
                    wait_for_rate()
                    last_transmit = time.monotonic()
                    try:
                        tester_present(
                            sock,
                            min(args.timeout, 0.5),
                            report["request_attempts"],
                            report["responses_received"],
                            report["tester_present_results"],
                        )
                    except Exception:
                        report["session_state"] = "uncertain_after_tester_present_failure"
                        raise
                    last_tester_present = time.monotonic()

                wait_for_rate()
                last_transmit = time.monotonic()
                result = query_did(
                    sock,
                    did,
                    args.timeout,
                    request_attempts=report["request_attempts"],
                    responses_received=report["responses_received"],
                )
                category = result["category"]
                results_file.write(json.dumps(result, sort_keys=True) + "\n")
                # Count only records accepted by the evidence file. A completed request whose
                # write fails remains visible in request_attempts, not results_written.
                result_count += 1
                categories[category] += 1
                if result_count % 64 == 0:
                    results_file.flush()
                    os.fsync(results_file.fileno())

                if category in ("service_not_supported", "subfunction_not_supported", "incorrect_length_or_format"):
                    consecutive_shape_failures += 1
                else:
                    consecutive_shape_failures = 0
                if consecutive_shape_failures >= SERVICE_SHAPE_ABORT_COUNT:
                    raise RuntimeError(
                        f"aborting after {consecutive_shape_failures} consecutive service/shape rejections"
                    )

                if category == "positive":
                    print(f"  {did:04X} OK {result['data_hex'] or ''} |{result['ascii'] or ''}|")
                elif category != "out_of_range_current_session":
                    print(f"  {did:04X} {category}: {result['response_hex'] or result['status']}")
                elif result_count % 256 == 0:
                    print(f"  progress {result_count}/{count}; readable={categories['positive']}")
    except KeyboardInterrupt:
        interrupted = True
        print("Interrupted; preserving checkpointed results.", file=sys.stderr)
    except Exception as exc:
        append_fatal(f"{type(exc).__name__}: {exc}")
    finally:
        cleanup_started = True
        try:
            if sock is not None:
                sock.close()
        except Exception as exc:
            append_fatal(f"socket close failed: {type(exc).__name__}: {exc}")
        finally:
            try:
                restored_passive = bool(canbus.restore_passive(module.channel, module.bitrate))
                if not restored_passive:
                    append_fatal("passive restoration verification failed")
            except Exception as exc:
                restored_passive = False
                append_fatal(f"passive restoration failed: {type(exc).__name__}: {exc}")
            finally:
                complete = (
                    report is not None
                    and result_count == count
                    and not interrupted
                    and not fatal_errors
                    and restored_passive
                )
                if complete:
                    try:
                        legacy = legacy_path(module, start, end)
                        write_legacy_text(legacy, module, start, end, results_path)
                    except Exception as exc:
                        append_fatal(f"legacy output failed: {type(exc).__name__}: {exc}")
                        legacy = None
                        complete = False

                if report is not None:
                    # Preserve the older limited field without pretending DID attempts are proven
                    # transmissions. Canonical accounting is request_attempts/responses_received.
                    report["transmit_counts"] = {
                        "session_control": report["request_attempts"]["session_control"],
                        "tester_present": report["request_attempts"]["tester_present"],
                    }
                    report.update(
                        {
                            "status": (
                                "complete"
                                if complete
                                else "interrupted"
                                if interrupted
                                else "failed"
                            ),
                            "completed_at": datetime.datetime.now().astimezone().isoformat(
                                timespec="seconds"
                            ),
                            "results_written": result_count,
                            "category_counts": dict(sorted(categories.items())),
                            "interrupted": interrupted,
                            "interruption_signal": (
                                signal.Signals(received_signal).name
                                if received_signal is not None
                                else None
                            ),
                            "fatal_error": "; ".join(fatal_errors) if fatal_errors else None,
                            "fatal_errors": fatal_errors,
                            "restored_passive": restored_passive,
                            "legacy_text": os.path.relpath(legacy, REPO) if legacy else None,
                        }
                    )
                    try:
                        atomic_json(summary_path, report)
                    except Exception as exc:
                        append_fatal(
                            f"final summary publication failed: {type(exc).__name__}: {exc}"
                        )

                try:
                    diagnostic_safety.release_channel_lock(diagnostic_lock)
                except Exception as exc:
                    append_fatal(f"diagnostic lock release failed: {type(exc).__name__}: {exc}")
                finally:
                    for signum, old_handler in old_handlers.items():
                        try:
                            signal.signal(signum, old_handler)
                        except Exception as exc:
                            append_fatal(
                                f"signal handler restore failed for {signum}: "
                                f"{type(exc).__name__}: {exc}"
                            )

    if summary_path is not None:
        print(f"summary: {summary_path}")
    if results_path is not None:
        print(f"results: {results_path}")
    print(f"adapter restored passive: {'yes' if restored_passive else 'NO - CHECK IT NOW'}")
    if fatal_errors or not restored_passive:
        return 1
    return 130 if interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
