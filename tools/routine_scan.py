#!/usr/bin/env python3
"""Safely inventory RoutineControl result identifiers for one verified module.

The positional CLI remains ``MODULE [START] [END]`` (hexadecimal bounds):

    python3 tools/routine_scan.py radar_acc
    python3 tools/routine_scan.py radar_acc 0200 03FF

The default is a dry run. A live scan is ACTIVE diagnostic traffic and requires all of
``--execute``, ``--confirm-parked``, ``--pair``, and ``--conditions``. Routine discovery sends
only requestRoutineResults (``31 03 <RID>``); this tool cannot generate startRoutine (31 01) or
stopRoutine (31 02).

By default the ECU's inherited session is left untouched and no TesterPresent is sent. An
explicit ``--session HEX`` sends one DiagnosticSessionControl request before the scan and then
bounded TesterPresent messages to hold that explicitly selected session. It is separately gated
because changing/re-entering a session can discard an active routine's state. There is no socket
recovery or session replay. Live plans above 512 unique RIDs additionally require
``--confirm-expanded-scan``. Each completed result is durably checkpointed to JSONL before the
next RID is attempted.
"""
import argparse
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


DEFAULT_START = 0x0200
DEFAULT_END = 0x03FF
EXTRA_RIDS = (0xFF00, 0xFF01, 0xFF02, 0xFF03)
MIN_REQUEST_RATE = 0.1
MAX_REQUEST_RATE = 5.0
MAX_RESPONSE_TIMEOUT_S = 5.0
MAX_RETRIES = 2
MAX_BOUNDED_RIDS = 512
TESTER_PRESENT_INTERVAL_S = 2.0
MIN_EXPLICIT_SESSION_RATE_HZ = 1.0 / TESTER_PRESENT_INTERVAL_S


def parse_rid(value):
    try:
        rid = int(value, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid hexadecimal routine ID: {value!r}") from None
    if not 0 <= rid <= 0xFFFF:
        raise argparse.ArgumentTypeError("routine ID must be between 0000 and FFFF")
    return rid


def parse_session(value):
    try:
        session = int(value, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid hexadecimal session byte: {value!r}") from None
    # Bit 7 is suppressPosRspMsgIndicationBit. It is disallowed because this tool requires a
    # positive response with the requested session echoed before it will scan.
    if not 1 <= session <= 0x7F:
        raise argparse.ArgumentTypeError("session must be a hexadecimal byte from 01 through 7F")
    return session


def build_rids(start, end, extra=EXTRA_RIDS):
    """Return the inclusive range plus extras once each, preserving first-seen order."""
    if start > end:
        raise ValueError("START must be less than or equal to END")
    ordered = []
    seen = set()
    for rid in (*range(start, end + 1), *extra):
        if not 0 <= rid <= 0xFFFF:
            raise ValueError(f"routine ID out of range: {rid!r}")
        if rid not in seen:
            ordered.append(rid)
            seen.add(rid)
    return ordered


def routine_payload(rid):
    """Build the tool's sole RoutineControl payload: requestRoutineResults (31 03)."""
    if not isinstance(rid, int) or isinstance(rid, bool) or not 0 <= rid <= 0xFFFF:
        raise ValueError("routine ID must be an integer between 0000 and FFFF")
    return bytes((0x31, 0x03, rid >> 8, rid & 0xFF))


def classify_routine_response(rid, response):
    """Classify one response, requiring the complete positive subfunction/RID echo."""
    if not response:
        return "timeout"
    response = bytes(response)
    if len(response) >= 3 and response[:2] == bytes.fromhex("7F 31"):
        return {
            0x11: "service_not_supported",
            0x12: "subfunction_not_supported",
            0x13: "incorrect_length_or_format",
            # 0x24 describes request ordering. It does not, by itself, prove the RID exists.
            0x24: "request_sequence_error_candidate",
            0x31: "out_of_range_current_session",
            0x22: "conditions_not_correct",
            0x33: "security_denied",
            0x7E: "subfunction_not_supported_active_session",
            0x7F: "service_not_supported_active_session",
        }.get(response[2], "negative_other")
    if len(response) >= 4 and response[:4] == bytes(
        (0x71, 0x03, rid >> 8, rid & 0xFF)
    ):
        return "positive_results"
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
                 counter_key=None, retries=0):
    """Drain and initiate one UDS request with honest attempt/response counters."""
    uds.drain(sock)
    if request_attempts is not None:
        request_attempts[counter_key] = request_attempts.get(counter_key, 0) + 1
    response, status = uds.request(sock, payload, timeout=timeout, retries=retries)
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


def enter_session(sock, session, timeout, request_attempts=None, responses_received=None):
    """Send one gated session request. This function never retries or replays it."""
    payload = bytes((0x10, session))
    started = time.monotonic()
    response, status = request_once(
        sock,
        payload,
        timeout,
        request_attempts=request_attempts,
        responses_received=responses_received,
        counter_key="session_control",
    )
    category = classify_session_response(session, response)
    return {
        "request_hex": uds.hx(payload),
        "response_hex": uds.hx(response) if response else None,
        "category": category,
        "validated_echo": category == "positive_echo",
        "status": status,
        "negative_response": uds.negative_response_details(response),
        "elapsed_s": round(time.monotonic() - started, 3),
    }


def query_routine(sock, rid, timeout, retries=0, request_attempts=None,
                  responses_received=None):
    payload = routine_payload(rid)
    started = time.monotonic()
    # scan_routines passes retries=0 because it owns checkpointed retry policy; direct callers
    # retain the historical bounded uds.request retry argument.
    response, status = request_once(
        sock,
        payload,
        timeout,
        request_attempts=request_attempts,
        responses_received=responses_received,
        counter_key="routine_results",
        retries=retries,
    )
    return {
        "rid": f"{rid:04X}",
        "request_hex": uds.hx(payload),
        "response_hex": uds.hx(response) if response else None,
        "category": classify_routine_response(rid, response),
        "status": status,
        "negative_response": uds.negative_response_details(response),
        "elapsed_s": round(time.monotonic() - started, 3),
    }


def scan_routines(sock, rids, timeout, retries, request_rate, results, keep_session=False,
                  transmit_counts=None, checkpoint=None, responses_received=None,
                  tester_present_results=None):
    """Scan finite RIDs, appending in-memory results and an optional durable checkpoint."""
    interval = 1.0 / request_rate
    last_transmit = None
    last_tester_present = time.monotonic()
    # ``transmit_counts`` is retained as a call-API compatibility name. Its values are initiated
    # uds.request calls, not proof of on-wire transmission; the report labels them accordingly.
    request_attempts = transmit_counts if transmit_counts is not None else {}
    request_attempts.setdefault("tester_present", 0)
    request_attempts.setdefault("routine_results", 0)
    responses_received = responses_received if responses_received is not None else {}
    responses_received.setdefault("tester_present", 0)
    responses_received.setdefault("routine_results", 0)
    tester_present_results = (
        tester_present_results if tester_present_results is not None else []
    )
    for index, rid in enumerate(rids):
        attempt_history = []
        result = None
        for attempt in range(retries + 1):
            if keep_session and time.monotonic() - last_tester_present >= TESTER_PRESENT_INTERVAL_S:
                if last_transmit is not None:
                    time.sleep(max(0.0, interval - (time.monotonic() - last_transmit)))
                last_transmit = time.monotonic()
                tester_present(
                    sock,
                    min(timeout, 0.5),
                    request_attempts,
                    responses_received,
                    tester_present_results,
                )
                last_tester_present = time.monotonic()
            if last_transmit is not None:
                time.sleep(max(0.0, interval - (time.monotonic() - last_transmit)))
            last_transmit = time.monotonic()
            result = query_routine(
                sock,
                rid,
                timeout,
                retries=0,
                request_attempts=request_attempts,
                responses_received=responses_received,
            )
            attempt_history.append(
                {
                    "attempt": attempt + 1,
                    "category": result["category"],
                    "response_hex": result.get("response_hex"),
                    "status": result.get("status"),
                }
            )
            retryable_timeout = (
                result["category"] == "timeout"
                and str(result.get("status", "")).startswith("NO_RESPONSE (timeout/empty")
            )
            if not retryable_timeout:
                break
        result["attempt_count"] = len(attempt_history)
        result["attempt_history"] = attempt_history
        results.append(result)
        if checkpoint is not None:
            checkpoint(result)
        if result["category"] != "out_of_range_current_session":
            response = result["response_hex"] or "(none)"
            print(f"  0x{rid:04X}: {result['category']} {response}")


def report_path(module):
    stamp = datetime.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f%z")
    return os.path.join(REPO, "tmp", "inventories", module.key, f"routines_{stamp}.json")


def checkpoint_path(summary_path):
    """Derive the append-only per-result evidence path from the atomic summary path."""
    stem, extension = os.path.splitext(summary_path)
    return f"{stem}.results.jsonl" if extension == ".json" else f"{summary_path}.results.jsonl"


def append_result_checkpoint(path, result):
    """Durably append one completed RID result before the next request is attempted."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_report(path, report):
    """Atomically publish a complete or explicitly marked partial JSON report."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp-{os.getpid()}"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        # os.replace removes the temporary path. Clean it only when an earlier write/replace failed.
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # Keep the historical no-argument radar default while preserving MODULE [START] [END].
    p.add_argument("module", nargs="?", default="radar_acc", help="verified key from lib/modules.py")
    p.add_argument("start", nargs="?", type=parse_rid, default=DEFAULT_START, metavar="START")
    p.add_argument("end", nargs="?", type=parse_rid, default=DEFAULT_END, metavar="END")
    p.add_argument("--execute", action="store_true", help="send the displayed result-only requests")
    p.add_argument(
        "--confirm-expanded-scan",
        action="store_true",
        help=f"required live for more than {MAX_BOUNDED_RIDS} unique routine IDs",
    )
    p.add_argument(
        "--confirm-parked",
        action="store_true",
        help="required with --execute; asserts the vehicle will remain parked for the scan",
    )
    p.add_argument("--pair", help="physical DLC/tap pair; required with --execute")
    p.add_argument("--conditions", help="ignition/engine/wake state; required with --execute")
    p.add_argument("--rate", type=float, default=1.0, help="maximum requests/second (default: 1)")
    p.add_argument("--timeout", type=float, default=0.75, help="seconds per request (default: 0.75)")
    p.add_argument(
        "--retries", type=int, default=0,
        help=(
            f"retries after a true no-response timeout, 0-{MAX_RETRIES}; "
            "responsePending exhaustion is never replayed (default: 0)"
        ),
    )
    p.add_argument(
        "--session",
        type=parse_session,
        metavar="HEX",
        help=(
            f"send one 10 HEX request before scanning; requires --rate >= "
            f"{MIN_EXPLICIT_SESSION_RATE_HZ:g} (default: inherit current session)"
        ),
    )
    p.add_argument(
        "--confirm-session-change",
        action="store_true",
        help="required with --session; acknowledges diagnostic-session state change",
    )
    p.add_argument(
        "--confirm-no-active-routine",
        action="store_true",
        help="required with --session; asserts no routine state needs to be preserved",
    )
    return p


def _append_error(errors, message):
    errors.append(message)
    print(f"ERROR: {message}", file=sys.stderr)


def main(argv=None):
    args = parser().parse_args(argv)
    module = get(args.module)

    try:
        rids = build_rids(args.start, args.end)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not math.isfinite(args.rate) or not MIN_REQUEST_RATE <= args.rate <= MAX_REQUEST_RATE:
        print(
            f"ERROR: --rate must be between {MIN_REQUEST_RATE:g} and {MAX_REQUEST_RATE:g}",
            file=sys.stderr,
        )
        return 2
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= MAX_RESPONSE_TIMEOUT_S:
        print(
            f"ERROR: --timeout must be finite, >0, and <= {MAX_RESPONSE_TIMEOUT_S:g} seconds",
            file=sys.stderr,
        )
        return 2
    if args.retries < 0 or args.retries > MAX_RETRIES:
        print(f"ERROR: --retries must be between 0 and {MAX_RETRIES}", file=sys.stderr)
        return 2
    if args.session is not None:
        if not args.confirm_session_change or not args.confirm_no_active_routine:
            print(
                "ERROR: --session requires --confirm-session-change and "
                "--confirm-no-active-routine",
                file=sys.stderr,
            )
            return 2
        if args.rate < MIN_EXPLICIT_SESSION_RATE_HZ:
            print(
                f"ERROR: --session requires --rate >= {MIN_EXPLICIT_SESSION_RATE_HZ:g}; "
                f"slower rates can exceed the {TESTER_PRESENT_INTERVAL_S:g}s keepalive cadence",
                file=sys.stderr,
            )
            return 2
    elif args.confirm_session_change or args.confirm_no_active_routine:
        print("ERROR: session confirmation flags require --session", file=sys.stderr)
        return 2

    print(f"ACTIVE RESULT-ONLY ROUTINE INVENTORY: {module.key} ({module.name})")
    print(
        f"{module.addressing_mode} {module.bitrate} bit/s TX={module.txid:X} RX={module.rxid:X}; "
        f"{len(rids)} unique physical 31 03 requests"
    )
    print(f"range={args.start:04X}-{args.end:04X}; extras=FF00 FF01 FF02 FF03 (deduplicated)")
    print(
        f"session={'inherited/unknown (no 10 or 3E)' if args.session is None else f'{args.session:02X} (one 10 request + bounded 3E keepalive)'}"
    )
    print("Routine start (31 01) and stop (31 02) are not implemented by this tool.")
    if not args.execute:
        print("DRY RUN: no preflight, CAN socket, or transmission occurred.")
        return 0

    if not args.confirm_parked or not args.pair or not args.conditions:
        print(
            "ERROR: --execute requires --confirm-parked, --pair, and --conditions",
            file=sys.stderr,
        )
        return 2
    if len(rids) > MAX_BOUNDED_RIDS and not args.confirm_expanded_scan:
        print(
            f"ERROR: {len(rids)} routine IDs exceeds bounded limit {MAX_BOUNDED_RIDS}; "
            "add --confirm-expanded-scan",
            file=sys.stderr,
        )
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

    results = []
    session_result = None
    fatal_errors = []
    interrupted = False
    restored_passive = False
    sock = None
    request_attempts = {"session_control": 0, "tester_present": 0, "routine_results": 0}
    responses_received = {"session_control": 0, "tester_present": 0, "routine_results": 0}
    tester_present_results = []
    started_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    path = report_path(module)
    results_path = checkpoint_path(path)

    with diagnostic_safety.interrupt_on_termination() as termination:
        try:
            sock = uds.open_module_socket(module, timeout=args.timeout)
            if args.session is not None:
                session_result = enter_session(
                    sock,
                    args.session,
                    args.timeout,
                    request_attempts=request_attempts,
                    responses_received=responses_received,
                )
                if not session_result["validated_echo"]:
                    raise RuntimeError(
                        f"session {args.session:02X} was not acknowledged with exact 50 "
                        f"{args.session:02X} echo ({session_result['category']})"
                    )
            if args.session is None:
                scan_routines(
                    sock,
                    rids,
                    args.timeout,
                    args.retries,
                    args.rate,
                    results,
                    transmit_counts=request_attempts,
                    responses_received=responses_received,
                    tester_present_results=tester_present_results,
                    checkpoint=lambda result: append_result_checkpoint(results_path, result),
                )
            else:
                scan_routines(
                    sock,
                    rids,
                    args.timeout,
                    args.retries,
                    args.rate,
                    results,
                    keep_session=True,
                    transmit_counts=request_attempts,
                    responses_received=responses_received,
                    tester_present_results=tester_present_results,
                    checkpoint=lambda result: append_result_checkpoint(results_path, result),
                )
        except KeyboardInterrupt:
            interrupted = True
            print("Interrupted; preserving partial results.", file=sys.stderr)
        except Exception as exc:
            _append_error(fatal_errors, f"{type(exc).__name__}: {exc}")
        finally:
            termination.begin_cleanup()
            try:
                if sock is not None:
                    sock.close()
            except Exception as exc:
                _append_error(fatal_errors, f"socket close failed: {type(exc).__name__}: {exc}")
            finally:
                try:
                    restored_passive = bool(canbus.restore_passive(module.channel, module.bitrate))
                    if not restored_passive:
                        _append_error(fatal_errors, "passive restoration verification failed")
                except Exception as exc:
                    _append_error(
                        fatal_errors,
                        f"passive restore failed: {type(exc).__name__}: {exc}",
                    )
                finally:
                    diagnostic_safety.release_channel_lock(diagnostic_lock)

    if termination.received_signal is not None:
        interrupted = True
    received_signal = termination.received_signal

    counts = {}
    for result in results:
        category = result["category"]
        counts[category] = counts.get(category, 0) + 1
    report = {
        "tool": "tools/routine_scan.py",
        "interaction": "active non-mutating UDS RoutineControl requestRoutineResults",
        "routine_start_stop_implemented": False,
        "routine_request_service": "31 03",
        "started_at": started_at,
        "completed_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
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
        "confirmed_parked": args.confirm_parked,
        "expanded_scan_confirmed": args.confirm_expanded_scan,
        "range": {"start": f"{args.start:04X}", "end": f"{args.end:04X}"},
        "extra_rids": [f"{rid:04X}" for rid in EXTRA_RIDS],
        "unique_rid_count": len(rids),
        "max_request_rate_hz": args.rate,
        "timeout_s": args.timeout,
        "retries": args.retries,
        "diagnostic_session_control_requested": args.session is not None,
        "diagnostic_session_control_attempted": request_attempts["session_control"] > 0,
        # Retained for report compatibility; count_semantics defines this as an attempted call.
        "diagnostic_session_control_sent": request_attempts["session_control"] > 0,
        "tester_present_attempted": request_attempts["tester_present"] > 0,
        "tester_present_response_received": responses_received["tester_present"] > 0,
        "tester_present_sent": request_attempts["tester_present"] > 0,
        # Backward-compatible field name: these are initiated calls, not proof of wire delivery.
        "transmit_counts": request_attempts,
        "request_attempts": request_attempts,
        "responses_received": responses_received,
        "count_semantics": (
            "request_attempts/transmit_counts count uds.request calls initiated before the call; "
            "responses_received counts non-empty responses returned, so a receive exception "
            "remains an attempt without a confirmed response"
        ),
        "requested_session": f"{args.session:02X}" if args.session is not None else None,
        "ecu_session": f"requested/{args.session:02X}" if args.session is not None else "inherited/unknown",
        "session_result": session_result,
        "tester_present_results": tester_present_results,
        "session_state": (
            "inherited_unknown"
            if args.session is None
            else "uncertain_after_tester_present_failure"
            if any(not item["validated_echo"] for item in tester_present_results)
            else "explicit_session_confirmed"
            if session_result is not None and session_result["validated_echo"]
            else "explicit_session_not_established"
        ),
        "interrupted": interrupted,
        "interruption_signal": (
            signal.Signals(received_signal).name if received_signal is not None else None
        ),
        "partial": interrupted or bool(fatal_errors) or len(results) != len(rids),
        "fatal_error": "; ".join(fatal_errors) if fatal_errors else None,
        "fatal_errors": fatal_errors,
        "restored_passive": restored_passive,
        "classification_counts": counts,
        "results_jsonl": os.path.relpath(results_path, REPO),
        "results": results,
    }
    write_report(path, report)
    print(f"report: {path}")
    print(f"adapter restored passive: {'yes' if restored_passive else 'NO - CHECK IT NOW'}")
    print("When the manual CAN campaign is finished: sudo systemctl start tpms-logger")
    if fatal_errors or not restored_passive:
        return 1
    return 130 if interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
