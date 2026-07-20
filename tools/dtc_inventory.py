#!/usr/bin/env python3
"""Inventory DTC capabilities/state for one verified registry module without clearing anything.

This sends ACTIVE, non-mutating UDS ReadDTCInformation (0x19) requests. The bounded default set is:

* 19 01 FF — count DTCs matching all supported status bits
* 19 02 FF — list matching stored/current DTCs
* 19 03    — list available snapshot record identifiers

``--include-supported`` additionally sends 19 0A, which can return a much larger inventory of
every DTC the ECU knows about. Keeping that request opt-in makes the default quick and predictable.

It never sends 0x14 ClearDiagnosticInformation, never changes session, and never requests unknown
snapshot/extended-data record contents. Output is per ECU under tmp/inventories/<module>/.
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

from lib import canbus, uds
from lib import diagnostic_safety
from lib.modules import get
from tools.ecu_discover import preflight


MIN_REQUEST_RATE = 0.1
MAX_REQUEST_RATE = 5.0
MAX_RESPONSE_TIMEOUT_S = 5.0
DEFAULT_REQUESTS = (
    ("count_by_status", bytes.fromhex("19 01 FF")),
    ("dtcs_by_status", bytes.fromhex("19 02 FF")),
    ("snapshot_identifiers", bytes.fromhex("19 03")),
)
SUPPORTED_DTCS_REQUEST = ("supported_dtcs", bytes.fromhex("19 0A"))


def selected_requests(args):
    requests = list(DEFAULT_REQUESTS)
    if args.include_supported:
        requests.append(SUPPORTED_DTCS_REQUEST)
    return requests

STATUS_BITS = (
    (0x01, "test_failed"),
    (0x02, "test_failed_this_operation_cycle"),
    (0x04, "pending"),
    (0x08, "confirmed"),
    (0x10, "test_not_completed_since_last_clear"),
    (0x20, "test_failed_since_last_clear"),
    (0x40, "test_not_completed_this_operation_cycle"),
    (0x80, "warning_indicator_requested"),
)


def decode_status(status):
    return [name for mask, name in STATUS_BITS if status & mask]


def fca_dtc_name(dtc_bytes):
    first, second, failure_type = dtc_bytes
    letter = "PCBU"[(first >> 6) & 0x03]
    code = ((first & 0x3F) << 8) | second
    return f"{letter}{code:04X}-{failure_type:02X}"


def parse_dtc_records(body):
    records = []
    trailing = b""
    for offset in range(0, len(body) - 3, 4):
        dtc = bytes(body[offset:offset + 3])
        status = body[offset + 3]
        records.append(
            {
                "raw_dtc": dtc.hex().upper(),
                "fca_display": fca_dtc_name(dtc),
                "status": f"{status:02X}",
                "status_flags": decode_status(status),
            }
        )
    consumed = len(records) * 4
    if consumed != len(body):
        trailing = bytes(body[consumed:])
    return records, trailing


def parse_snapshot_identifiers(body):
    records = []
    trailing = b""
    for offset in range(0, len(body) - 3, 4):
        dtc = bytes(body[offset:offset + 3])
        records.append(
            {
                "raw_dtc": dtc.hex().upper(),
                "fca_display": fca_dtc_name(dtc),
                "snapshot_record": f"{body[offset + 3]:02X}",
            }
        )
    consumed = len(records) * 4
    if consumed != len(body):
        trailing = bytes(body[consumed:])
    return records, trailing


def parse_positive_response(request, response):
    subfunction = request[1]
    if len(response) < 2 or response[:2] != bytes((0x59, subfunction)):
        return {"parse_error": "positive SID/subfunction echo mismatch"}
    if subfunction == 0x01:
        if len(response) < 6:
            return {"parse_error": "short reportNumberOfDTCByStatusMask response"}
        return {
            "status_availability_mask": f"{response[2]:02X}",
            "dtc_format_identifier": f"{response[3]:02X}",
            "dtc_count": int.from_bytes(response[4:6], "big"),
            "trailing_hex": uds.hx(response[6:]) if len(response) > 6 else None,
        }
    if subfunction in (0x02, 0x0A):
        if len(response) < 3:
            return {"parse_error": "short DTC-list response"}
        records, trailing = parse_dtc_records(response[3:])
        return {
            "status_availability_mask": f"{response[2]:02X}",
            "dtcs": records,
            "trailing_hex": uds.hx(trailing) if trailing else None,
        }
    if subfunction == 0x03:
        records, trailing = parse_snapshot_identifiers(response[2:])
        return {
            "snapshots": records,
            "trailing_hex": uds.hx(trailing) if trailing else None,
        }
    return {"parse_error": "unsupported local parser subfunction"}


def query(sock, label, payload, timeout, accounting=None):
    started = time.monotonic()
    uds.drain(sock)
    if accounting is not None:
        accounting["request_attempts"] += 1
    response, status = uds.request(sock, payload, timeout=timeout, retries=0)
    if accounting is not None and response:
        accounting["responses_received"] += 1
    if response is None:
        category = "timeout"
        parsed = None
    elif len(response) >= 3 and response[0] == 0x7F and response[1] == 0x19:
        category = "negative"
        parsed = None
    elif len(response) >= 2 and response[:2] == bytes((0x59, payload[1])):
        category = "positive"
        parsed = parse_positive_response(payload, response)
    else:
        category = "unexpected"
        parsed = None
    return {
        "label": label,
        "request_hex": uds.hx(payload),
        "response_hex": uds.hx(response) if response else None,
        "category": category,
        "status": status,
        "negative_response": uds.negative_response_details(response),
        "parsed": parsed,
        "elapsed_s": round(time.monotonic() - started, 3),
    }


def report_path(module):
    stamp = datetime.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f%z")
    return os.path.join(REPO, "tmp", "inventories", module.key, f"dtcs_{stamp}.json")


def write_report(path, report):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp-{os.getpid()}"
    with open(temporary, "w") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("module", help="verified key from lib/modules.py")
    p.add_argument("--execute", action="store_true", help="actually send the listed reads")
    p.add_argument(
        "--include-supported",
        action="store_true",
        help="also request 19 0A (potentially much larger supported-DTC list)",
    )
    p.add_argument("--pair", help="physical pair/tap description; required with --execute")
    p.add_argument("--conditions", help="ignition/engine/wake state; required with --execute")
    p.add_argument("--confirm-parked", action="store_true", help="assert the vehicle is parked")
    p.add_argument("--rate", type=float, default=1.0, help="maximum requests/second (default: 1)")
    p.add_argument("--timeout", type=float, default=1.0, help="seconds per request (default: 1)")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    module = get(args.module)
    requests = selected_requests(args)
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

    print(f"ACTIVE NON-MUTATING DTC INVENTORY: {module.key} ({module.name})")
    print(
        f"{module.addressing_mode} {module.bitrate} bit/s "
        f"TX={module.txid:X} RX={module.rxid:X}"
    )
    print("requests: " + ", ".join(uds.hx(payload) for _, payload in requests))
    print("ClearDiagnosticInformation (14) is not implemented by this tool.")
    if not args.execute:
        print("DRY RUN: no CAN socket opened and nothing transmitted.")
        return 0
    if not args.confirm_parked or not args.pair or not args.conditions:
        print(
            "ERROR: --execute requires --confirm-parked, --pair, and --conditions",
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
    fatal_error = None
    interrupted = False
    restored_passive = False
    sock = None
    interval = 1.0 / args.rate
    started_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    accounting = {"request_attempts": 0, "responses_received": 0}

    with diagnostic_safety.interrupt_on_termination() as termination:
        try:
            sock = uds.open_module_socket(module, timeout=args.timeout)
            for index, (label, payload) in enumerate(requests):
                result = query(sock, label, payload, args.timeout, accounting=accounting)
                results.append(result)
                parsed = result["parsed"] or {}
                count = parsed.get("dtc_count")
                if count is None:
                    count = len(parsed.get("dtcs", parsed.get("snapshots", [])))
                print(f"{uds.hx(payload):<10} {result['category']:<10} records/count={count}")
                if index + 1 < len(requests):
                    time.sleep(max(0.0, interval - result["elapsed_s"]))
        except KeyboardInterrupt:
            interrupted = True
            print("Interrupted; preserving partial results.", file=sys.stderr)
        except Exception as exc:
            fatal_error = f"{type(exc).__name__}: {exc}"
            print(f"ERROR: {fatal_error}", file=sys.stderr)
        finally:
            termination.begin_cleanup()
            try:
                if sock is not None:
                    sock.close()
            except Exception as exc:
                if fatal_error is None:
                    fatal_error = f"socket close failed: {type(exc).__name__}: {exc}"
                    print(f"ERROR: {fatal_error}", file=sys.stderr)
            finally:
                try:
                    restored_passive = bool(canbus.restore_passive(module.channel, module.bitrate))
                    if not restored_passive and fatal_error is None:
                        fatal_error = "passive restoration verification failed"
                except Exception as exc:
                    restored_passive = False
                    if fatal_error is None:
                        fatal_error = f"passive restoration failed: {type(exc).__name__}: {exc}"
                finally:
                    diagnostic_safety.release_channel_lock(diagnostic_lock)

    if termination.received_signal is not None:
        interrupted = True
    received_signal = termination.received_signal

    report = {
        "tool": "tools/dtc_inventory.py",
        "interaction": "active non-mutating UDS ReadDTCInformation",
        "clear_service_implemented": False,
        "supported_dtc_inventory_requested": args.include_supported,
        "diagnostic_session_control_sent": False,
        "ecu_session": "inherited/unknown",
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
        "parked_asserted": args.confirm_parked,
        "max_request_rate_hz": args.rate,
        "timeout_s": args.timeout,
        "request_attempts": accounting["request_attempts"],
        "responses_received": accounting["responses_received"],
        "count_semantics": (
            "request_attempts increments immediately before each uds.request call; "
            "responses_received counts non-empty responses returned"
        ),
        "interrupted": interrupted,
        "interruption_signal": (
            signal.Signals(received_signal).name if received_signal is not None else None
        ),
        "partial": (
            interrupted
            or fatal_error is not None
            or not restored_passive
            or len(results) != len(requests)
        ),
        "fatal_error": fatal_error,
        "restored_passive": restored_passive,
        "results": results,
    }
    path = report_path(module)
    write_report(path, report)
    print(f"report: {path}")
    print(f"adapter restored passive: {'yes' if restored_passive else 'NO - CHECK IT NOW'}")
    print("When the manual CAN campaign is finished: sudo systemctl start tpms-logger")
    if fatal_error or not restored_passive:
        return 1
    return 130 if interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
