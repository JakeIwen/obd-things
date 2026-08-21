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

from lib import can_operation_state, can_runtime_route, canbus, diagnostic_safety, uds
from lib.dtc import (
    STATUS_BITS,
    decode_status,
    fca_dtc_name,
    parse_dtc_records,
    parse_positive_response,
    parse_snapshot_identifiers,
)
from lib.modules import get
from tools.ecu_discover import prearm_conflict_errors, preflight


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


def active_inhibit_detail(channel):
    """Return one fail-closed reason when active diagnostics are inhibited."""
    try:
        inhibits = can_operation_state.active_inhibits(channel)
    except (OSError, RuntimeError, ValueError) as exc:
        return (
            "same-boot external-operation inhibit state is unavailable: "
            f"{type(exc).__name__}: {exc}"
        )
    if not inhibits:
        return None
    names = ",".join(str(item.get("name", "invalid")) for item in inhibits)
    return f"active diagnostic traffic is inhibited by {names}"


def active_interface_detail(channel, bitrate):
    """Require one exact, healthy classical-CAN transmit state."""
    try:
        state = canbus.interface_state(channel)
    except (OSError, RuntimeError, ValueError) as exc:
        return (
            "SocketCAN interface state is unavailable: "
            f"{type(exc).__name__}: {exc}"
        )
    if not isinstance(state, canbus.InterfaceState) or state.channel != channel:
        return "SocketCAN interface state identity is invalid"
    if not state.present or not state.up:
        return f"{channel} is missing or down"
    if state.bitrate != bitrate:
        return f"{channel} bitrate is {state.bitrate}, expected {bitrate}"
    if state.fd_enabled is not False:
        return (
            f"{channel} must prove classical CAN MTU with FD off before "
            "ReadDTCInformation traffic"
        )
    if state.listen_only:
        return f"{channel} is listen-only; active DTC reads require an explicit arm"
    if state.controller_state != "ERROR-ACTIVE":
        return (
            f"{channel} controller state is "
            f"{state.controller_state or 'unknown'}, expected ERROR-ACTIVE"
        )
    if state.restart_ms != 0:
        return (
            f"{channel} restart-ms is {state.restart_ms}; the dual-USBCANFD "
            "classical-CAN policy requires 0"
        )
    return None


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("module", help="verified key from lib/modules.py")
    p.add_argument("--execute", action="store_true", help="actually send the listed reads")
    p.add_argument(
        "--resolve-runtime",
        action="store_true",
        help=(
            "compatibility flag; live execution always resolves the stable "
            "dual-USBCANFD serial/dev_id route"
        ),
    )
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
    topology_fingerprint = None
    expected_pair = None
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
    print(
        f"route: {module.bus} -> runtime USB serial/dev_id resolution on --execute"
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
    try:
        ownership = can_runtime_route.acquire_armed_module_route(
            module,
            asserted_pair=args.pair,
            prearm_check=prearm_conflict_errors,
        )
        module = ownership.route.module
        expected_pair = ownership.route.pair
        topology_fingerprint = ownership.route.topology_fingerprint
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: stable runtime route/arm failed: {exc}", file=sys.stderr)
        return 2
    inhibit_detail = active_inhibit_detail(module.channel)
    if inhibit_detail is not None:
        restored = ownership.release()
        print(f"ERROR: {inhibit_detail}", file=sys.stderr)
        return 2 if restored else 1
    interface_detail = active_interface_detail(module.channel, module.bitrate)
    if interface_detail is not None:
        restored = ownership.release()
        print(f"ERROR: {interface_detail}", file=sys.stderr)
        return 2 if restored else 1
    errors = preflight(module.channel, module.bitrate)
    if errors:
        restored = ownership.release()
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2 if restored else 1

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
                can_runtime_route.revalidate_module_route(
                    ownership.route,
                    manager=ownership.manager,
                )
                inhibit_detail = active_inhibit_detail(module.channel)
                if inhibit_detail is not None:
                    fatal_error = inhibit_detail
                    print(f"ERROR: {fatal_error}", file=sys.stderr)
                    break
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
                    restored_passive = ownership.release()
                    if not restored_passive and fatal_error is None:
                        fatal_error = "passive restoration verification failed"
                except Exception as exc:
                    restored_passive = False
                    if fatal_error is None:
                        fatal_error = f"passive restoration failed: {type(exc).__name__}: {exc}"
                finally:
                    pass

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
            "route_source": "usb_serial_and_dev_id",
            "topology_fingerprint": topology_fingerprint,
            "expected_physical_pair": expected_pair,
            "bitrate": module.bitrate,
            "addressing_mode": module.addressing_mode,
            "txid": f"{module.txid:X}",
            "rxid": f"{module.rxid:X}",
        },
        "physical_pair": args.pair,
        "conditions": args.conditions,
        "parked_asserted": args.confirm_parked,
        "same_boot_inhibits_checked": True,
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
    print(
        "Restart only a same-role service deliberately stopped for this campaign, "
        "and only through its current deployment handoff."
    )
    if fatal_error or not restored_passive:
        return 1
    return 130 if interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
