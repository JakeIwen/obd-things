#!/usr/bin/env python3
"""Bounded ACTIVE diagnostic ECU discovery using one non-mutating identity read per target.

This is not passive or OBD broadcast discovery. It transmits a physical UDS
ReadDataByIdentifier request (22 F187, spare-part number), may wake the vehicle network and
accessory rails, and must only be used after a passive bus survey confirms the physical pair
and bitrate.

Dry-run the current-van C-CAN verified-endpoint profile (default; sends nothing):

    python3 tools/ecu_discover.py

After stopping tpms-logger and explicitly arming C-CAN, execute it with recorded conditions:

    sudo systemctl stop tpms-logger
    ./bringup.sh --tx
    python3 tools/ecu_discover.py --execute --confirm-parked --pair 6/14 \
        --conditions "ignition ON, engine OFF, PCAN on SGW-bypass C-CAN"

The tool refuses to run while tpms-logger is active, the interface is listen-only/down, or
the bitrate differs. It never uses functional broadcast. It restores listen-only mode even
after an interrupted/failed scan and writes a JSON report under tmp/discovery/.

For an explicitly researched 11-bit pair, replace the profile with one or more targets:

    python3 tools/ecu_discover.py --target bcm_guess=760:768 \
        --addressing-mode normal_11bits --bitrate 125000 --bus b-can

Custom targets are dry-run by default too. Exact 11-bit pairings are intentionally not guessed.

An expanded 29-bit normal-fixed address-byte sweep is available only with an explicit flag and
confirmation. It still sends physical 0x18DAxxF1 requests, never functional broadcast:

    python3 tools/ecu_discover.py --all-29bit-targets
    python3 tools/ecu_discover.py --all-29bit-targets --confirm-expanded-scan \
        --execute --confirm-parked --pair 6/14 \
        --conditions "ignition ON, engine OFF, PCAN on SGW-bypass C-CAN"

FCA modules using legacy ECU identification can be surveyed separately with
``--probe legacy-1a87``. This sends ReadECUIdentification, not a write or session change.
"""
import argparse
import datetime
import json
import math
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

from lib import canbus, uds
from lib import diagnostic_safety
from lib.modules import MODULES, Module, NORMAL_11BITS, NORMAL_29BITS


DISCOVERY_DID = 0xF187  # standardized vehicle-manufacturer spare-part number; deliberately not VIN
MIN_REQUEST_RATE = 0.1
MAX_REQUEST_RATE = 5.0
MAX_REQUEST_TIMEOUT_S = 5.0
PROBE_PAYLOADS = {
    "uds-f187": bytes((0x22, DISCOVERY_DID >> 8, DISCOVERY_DID & 0xFF)),
    "legacy-1a87": bytes.fromhex("1A 87"),
}


@dataclass(frozen=True)
class Candidate:
    label: str
    name: str
    txid: int
    rxid: int
    source: str
    bus: str = "c-can"
    bitrate: int = 500000
    addressing_mode: str = NORMAL_29BITS

    def module(self, channel):
        return Module(
            key=self.label,
            name=self.name,
            txid=self.txid,
            rxid=self.rxid,
            channel=channel,
            bus=self.bus,
            note=f"Discovery target metadata; source: {self.source}",
            bitrate=self.bitrate,
            addressing_mode=self.addressing_mode,
        )


def normal_29bit_candidate(label, name, target_address, source):
    return Candidate(
        label=label,
        name=name,
        txid=0x18DA0000 | (target_address << 8) | 0xF1,
        rxid=0x18DAF100 | target_address,
        source=source,
    )


def registry_candidate(key, source):
    """Copy one independently verified registry endpoint into the discovery profile."""
    module = MODULES[key]
    return Candidate(
        label=module.key,
        name=module.name,
        txid=module.txid,
        rxid=module.rxid,
        source=source,
        bus=module.bus,
        bitrate=module.bitrate,
        addressing_mode=module.addressing_mode,
    )


DEFAULT_PROFILE_SOURCE = (
    "independently live-verified on the current van 2026-07-19; "
    "executable addressing from lib/modules.py"
)
PROMASTER_CCAN_CANDIDATES = (
    registry_candidate("tcm", DEFAULT_PROFILE_SOURCE),
    registry_candidate("shifter", DEFAULT_PROFILE_SOURCE),
    registry_candidate("radar_acc", DEFAULT_PROFILE_SOURCE),
    registry_candidate("bcm_ccan", DEFAULT_PROFILE_SOURCE),
    registry_candidate("cluster", DEFAULT_PROFILE_SOURCE),
    registry_candidate("telematics", DEFAULT_PROFILE_SOURCE),
    registry_candidate("rf_hub", DEFAULT_PROFILE_SOURCE),
)


def parse_can_id(value):
    try:
        can_id = int(value, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid hexadecimal CAN ID: {value!r}") from None
    if can_id < 0:
        raise argparse.ArgumentTypeError("CAN IDs cannot be negative")
    return can_id


def custom_candidate(spec, args, index):
    """Parse LABEL=TX:RX (or TX:RX) into a provenance-labeled candidate."""
    if "=" in spec:
        label, pair = spec.split("=", 1)
        if not label:
            raise argparse.ArgumentTypeError("custom target label cannot be empty")
    else:
        label, pair = f"custom_{index}", spec
    try:
        tx_text, rx_text = pair.split(":", 1)
    except ValueError:
        raise argparse.ArgumentTypeError("target must be LABEL=TX:RX or TX:RX (hex IDs)") from None
    candidate = Candidate(
        label=label,
        name=f"Custom candidate {label}",
        txid=parse_can_id(tx_text),
        rxid=parse_can_id(rx_text),
        source="operator-supplied explicit TX/RX pair",
        bus=args.bus,
        bitrate=args.bitrate,
        addressing_mode=args.addressing_mode,
    )
    if candidate.addressing_mode == NORMAL_11BITS and (
        candidate.txid == 0x7DF or candidate.rxid == 0x7DF
    ):
        raise argparse.ArgumentTypeError(
            "0x7DF is a functional-broadcast CAN ID; discovery requires a physical pair"
        )
    if candidate.addressing_mode == NORMAL_29BITS and (
        ((candidate.txid >> 16) & 0xFF) == 0xDB
        or ((candidate.rxid >> 16) & 0xFF) == 0xDB
    ):
        raise argparse.ArgumentTypeError(
            "0x18DBxxxx is functional addressing; discovery requires a physical pair"
        )
    # Reuse the registry's ID-width and bitrate validation before any live work.
    candidate.module(args.channel)
    return candidate


def build_targets(args):
    if args.target:
        targets = [custom_candidate(spec, args, i) for i, spec in enumerate(args.target, 1)]
        seen_pairs = set()
        for target in targets:
            pair = (target.addressing_mode, target.bitrate, target.txid, target.rxid)
            if pair in seen_pairs:
                raise argparse.ArgumentTypeError(
                    f"duplicate physical TX/RX pair {target.txid:X}:{target.rxid:X}"
                )
            seen_pairs.add(pair)
        return targets
    if args.all_29bit_targets:
        return [
            normal_29bit_candidate(
                f"address_{address:02X}",
                f"Unverified physical address 0x{address:02X}",
                address,
                "explicit exhaustive 29-bit normal-fixed address-byte enumeration",
            )
            for address in range(0x100)
            if address != 0xF1  # tester source address; would make TX and RX CAN IDs identical
        ]
    return list(PROMASTER_CCAN_CANDIDATES)


def service_active(name):
    return subprocess.run(
        ["systemctl", "is-active", "--quiet", name],
        capture_output=True,
    ).returncode == 0


def tpms_logger_active():
    return service_active("tpms-logger")


def preflight(channel, bitrate):
    errors = []
    if tpms_logger_active():
        errors.append("tpms-logger is active; stop it first: sudo systemctl stop tpms-logger")
    if service_active("promaster-drive-capture"):
        errors.append(
            "promaster-drive-capture is active; finish/stop the passive drive capture before "
            "arming diagnostics"
        )
    current_bitrate = canbus.iface_bitrate(channel)
    if current_bitrate is None:
        errors.append(f"{channel} is missing or down; explicitly arm the intended bus first")
    elif current_bitrate != bitrate:
        errors.append(f"{channel} bitrate is {current_bitrate}, expected {bitrate}")
    if current_bitrate is not None and canbus.is_listen_only(channel):
        errors.append(
            f"{channel} is listen-only; discovery is active diagnostic traffic, so arm it explicitly"
        )
    state = canbus.controller_state(channel)
    if state != "ERROR-ACTIVE":
        errors.append(
            f"{channel} controller state is {state or 'unknown'}, expected ERROR-ACTIVE"
        )
    if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode != 0:
        errors.append(
            "noninteractive sudo is unavailable; passive restoration cannot be guaranteed"
        )
    return errors


def classify_response(request_payload, response, status):
    if not response:
        return "timeout"
    if response[0] == 0x7F:
        return "negative"
    expected_prefix = bytes((request_payload[0] + 0x40,)) + request_payload[1:]
    if response[:len(expected_prefix)] == expected_prefix:
        return "positive"
    return "unexpected"


def scan_target(candidate, channel, timeout, request_payload=None):
    request_payload = request_payload or PROBE_PAYLOADS["uds-f187"]
    started = time.monotonic()
    sock = None
    request_attempted = False
    try:
        sock = uds.open_module_socket(candidate.module(channel), timeout=timeout)
        # A newly bound socket can still receive a late response from an earlier use of the
        # same physical pair. Empty it before associating any response with this request.
        uds.drain(sock)
        # This is an initiated uds.request() call, not a claim that the CAN frame reached the wire.
        # Increment before calling so a receive-side transport exception cannot erase the attempt.
        request_attempted = True
        response, status = uds.request(sock, request_payload, timeout=timeout, retries=0)
        category = classify_response(request_payload, response, status)
        return {
            **asdict(candidate),
            "request_hex": uds.hx(request_payload),
            "response_hex": uds.hx(response) if response else None,
            "category": category,
            "present": bool(response),
            "request_attempted": request_attempted,
            "response_received": bool(response),
            "status": status,
            "negative_response": uds.negative_response_details(response),
            "elapsed_s": round(time.monotonic() - started, 3),
        }
    except OSError as exc:
        return {
            **asdict(candidate),
            "request_hex": uds.hx(request_payload),
            "response_hex": None,
            "category": "transport_error",
            "present": False,
            "request_attempted": request_attempted,
            "response_received": False,
            "status": f"{type(exc).__name__}: {exc}",
            "negative_response": None,
            "elapsed_s": round(time.monotonic() - started, 3),
        }
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def scan_targets(targets, channel, timeout, request_rate, results, quiet_timeouts=False,
                 request_payload=None):
    interval = 1.0 / request_rate
    for index, candidate in enumerate(targets):
        result = scan_target(candidate, channel, timeout, request_payload=request_payload)
        results.append(result)
        response = result["response_hex"] or "(none)"
        if not quiet_timeouts or result["category"] != "timeout":
            print(
                f"{candidate.label:<22} TX={candidate.txid:X} RX={candidate.rxid:X} "
                f"{result['category']:<15} {response}"
            )
        elif (index + 1) % 32 == 0:
            present = sum(item["present"] for item in results)
            print(f"  progress {index + 1:>3}/{len(targets)}; responding targets={present}")
        if index + 1 < len(targets):
            time.sleep(max(0.0, interval - result["elapsed_s"]))


def report_path():
    stamp = datetime.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f%z")
    return os.path.join(REPO, "tmp", "discovery", f"ecu_discovery_{stamp}.json")


def write_report(path, report):
    """Atomically publish a complete or explicitly partial discovery report."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp-{os.getpid()}"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
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


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--execute", action="store_true", help="actually send the listed diagnostic reads")
    p.add_argument("--channel", default="can0")
    p.add_argument(
        "--probe",
        choices=tuple(PROBE_PAYLOADS),
        default="uds-f187",
        help="non-mutating presence request (default: uds-f187)",
    )
    target_group = p.add_mutually_exclusive_group()
    target_group.add_argument("--target", action="append", help="explicit LABEL=TX:RX; replaces default profile")
    target_group.add_argument(
        "--all-29bit-targets",
        action="store_true",
        help=(
            "enumerate the 255 usable physical 0x18DAxxF1 target addresses; "
            "0xF1 is reserved for the tester"
        ),
    )
    p.add_argument(
        "--confirm-expanded-scan",
        action="store_true",
        help="required with --execute --all-29bit-targets",
    )
    p.add_argument(
        "--confirm-custom-physical",
        action="store_true",
        help="required for live custom targets; asserts every supplied TX/RX pair is physical",
    )
    p.add_argument("--addressing-mode", choices=(NORMAL_29BITS, NORMAL_11BITS), default=NORMAL_29BITS)
    p.add_argument("--bitrate", type=int, default=500000)
    p.add_argument("--bus", default="c-can", help="bus label recorded for custom targets")
    p.add_argument("--pair", help="physical DLC/tap pair, required with --execute (for example 6/14)")
    p.add_argument("--conditions", help="ignition/engine/wake/adapter conditions, required with --execute")
    p.add_argument("--confirm-parked", action="store_true", help="assert the vehicle is parked")
    p.add_argument("--rate", type=float, default=1.0, help="maximum requests/second (default: 1)")
    p.add_argument("--timeout", type=float, default=0.75, help="seconds per target (default: 0.75)")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if not math.isfinite(args.rate) or not MIN_REQUEST_RATE <= args.rate <= MAX_REQUEST_RATE:
        print(
            f"ERROR: --rate must be between {MIN_REQUEST_RATE:g} and "
            f"{MAX_REQUEST_RATE:g} requests/second",
            file=sys.stderr,
        )
        return 2
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= MAX_REQUEST_TIMEOUT_S:
        print(
            f"ERROR: --timeout must be >0 and <= {MAX_REQUEST_TIMEOUT_S:g} seconds",
            file=sys.stderr,
        )
        return 2
    if args.confirm_expanded_scan and not args.all_29bit_targets:
        print("ERROR: --confirm-expanded-scan requires --all-29bit-targets", file=sys.stderr)
        return 2
    if args.confirm_custom_physical and not args.target:
        print("ERROR: --confirm-custom-physical requires at least one --target", file=sys.stderr)
        return 2
    if not args.target and (
        args.addressing_mode != NORMAL_29BITS or args.bitrate != 500000 or args.bus != "c-can"
    ):
        print(
            "ERROR: --addressing-mode, --bitrate, and --bus only apply with --target",
            file=sys.stderr,
        )
        return 2
    try:
        targets = build_targets(args)
    except (argparse.ArgumentTypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    request_payload = PROBE_PAYLOADS[args.probe]
    print(
        f"ACTIVE DIAGNOSTIC ECU DISCOVERY "
        f"(physical {uds.hx(request_payload)}; never functional broadcast)"
    )
    print(f"channel={args.channel} targets={len(targets)} max_rate={args.rate:g}/s")
    for candidate in targets:
        print(
            f"  {candidate.label:<22} {candidate.addressing_mode:<13} "
            f"{candidate.bitrate:>6} bit/s TX={candidate.txid:X} RX={candidate.rxid:X}"
        )

    if not args.execute:
        print("\nDRY RUN: no CAN sockets opened and nothing transmitted. Add --execute only after passive survey.")
        return 0
    if args.all_29bit_targets and not args.confirm_expanded_scan:
        print(
            "ERROR: expanded mode requires --confirm-expanded-scan with --execute",
            file=sys.stderr,
        )
        return 2
    if args.target and not args.confirm_custom_physical:
        print(
            "ERROR: live custom targets require --confirm-custom-physical",
            file=sys.stderr,
        )
        return 2
    if not args.confirm_parked or not args.pair or not args.conditions:
        print(
            "ERROR: --execute requires --confirm-parked, --pair, and --conditions",
            file=sys.stderr,
        )
        return 2
    bitrates = {target.bitrate for target in targets}
    if len(bitrates) != 1:
        print("ERROR: one scan cannot mix target bitrates", file=sys.stderr)
        return 2
    bitrate = bitrates.pop()
    errors = preflight(args.channel, bitrate)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2

    try:
        diagnostic_lock = diagnostic_safety.acquire_channel_lock(args.channel)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    results = []
    interrupted = False
    fatal_errors = []
    restored_passive = False
    started_at = None
    path = None
    report = None
    received_signal = None
    cleanup_started = False
    old_handlers = {}

    def append_fatal(message):
        fatal_errors.append(message)
        print(f"\nERROR: {message}", file=sys.stderr)

    def interrupt_handler(signum, _frame):
        nonlocal received_signal, interrupted
        if received_signal is None:
            received_signal = signum
            interrupted = True
            # During the scan, convert INT/TERM/HUP to the normal partial-report path. Once cleanup
            # starts, recording (and ignoring) the first/repeated signal protects close/restore,
            # report publication, and lock release from being interrupted a second time.
            if not cleanup_started:
                raise KeyboardInterrupt

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            old_handlers[signum] = signal.signal(signum, interrupt_handler)
        started_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        path = report_path()
        scan_targets(
            targets,
            args.channel,
            args.timeout,
            args.rate,
            results,
            quiet_timeouts=args.all_29bit_targets,
            request_payload=request_payload,
        )
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted; preserving partial results.", file=sys.stderr)
    except Exception as exc:
        append_fatal(f"{type(exc).__name__}: {exc}")
    finally:
        cleanup_started = True
        try:
            restored_passive = bool(canbus.restore_passive(args.channel, bitrate))
            if not restored_passive:
                append_fatal("passive restoration verification failed")
        except Exception as exc:
            restored_passive = False
            append_fatal(f"passive restoration failed: {type(exc).__name__}: {exc}")
        finally:
            try:
                report = {
                    "schema_version": 2,
                    "tool": "tools/ecu_discover.py",
                    "interaction": "active diagnostic read (not passive)",
                    "started_at": started_at,
                    "completed_at": datetime.datetime.now().astimezone().isoformat(
                        timespec="seconds"
                    ),
                    "channel": args.channel,
                    "physical_pair": args.pair,
                    "conditions": args.conditions,
                    "parked_asserted": args.confirm_parked,
                    "probe": args.probe,
                    "request": uds.hx(request_payload),
                    "functional_broadcast": False,
                    "custom_pairs_asserted_physical": bool(args.target),
                    "diagnostic_session_control_sent": False,
                    "ecu_session": "inherited/unknown",
                    "target_selection": (
                        "all_29bit_physical_addresses"
                        if args.all_29bit_targets
                        else "custom_explicit_pairs"
                        if args.target
                        else "promaster_ccan_verified_endpoints"
                    ),
                    "max_request_rate_hz": args.rate,
                    "timeout_s": args.timeout,
                    "interrupted": interrupted,
                    "interruption_signal": (
                        signal.Signals(received_signal).name
                        if received_signal is not None
                        else None
                    ),
                    "partial": (
                        interrupted
                        or bool(fatal_errors)
                        or len(results) != len(targets)
                        or not restored_passive
                    ),
                    "fatal_error": "; ".join(fatal_errors) if fatal_errors else None,
                    "fatal_errors": fatal_errors,
                    "restored_passive": restored_passive,
                    "request_attempts": sum(
                        bool(result.get("request_attempted")) for result in results
                    ),
                    "responses_received": sum(
                        bool(result.get("response_received")) for result in results
                    ),
                    "count_semantics": (
                        "request_attempts counts uds.request calls initiated before the call; "
                        "responses_received counts non-empty responses returned, so a receive "
                        "exception remains an attempt without a confirmed response"
                    ),
                    "results": results,
                }
                if path is not None:
                    write_report(path, report)
            except Exception as exc:
                append_fatal(f"report publication failed: {type(exc).__name__}: {exc}")
            finally:
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

    if path is not None:
        print(f"report: {path}")
    print(f"adapter restored passive: {'yes' if restored_passive else 'NO - CHECK IT NOW'}")
    print("When the manual CAN campaign is finished: sudo systemctl start tpms-logger")
    if not restored_passive or fatal_errors:
        return 1
    return 130 if interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
