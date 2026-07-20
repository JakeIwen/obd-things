#!/usr/bin/env python3
"""Safety-gated ad-hoc physical UDS request to one verified module.

Dry-run is the default and never opens a CAN socket::

    python3 tools/uds_send.py radar_acc 22 F1 A5
    python3 tools/uds_send.py radar_acc 31 03 02 51

Live diagnostic reads require ``--execute``, a parked assertion, physical-pair provenance,
and recorded conditions. Diagnostic-session traffic and mutation/actuation services require
additional explicit confirmations shown by the dry-run plan. Every attempted live request is
serialized by SocketCAN channel and restores verified listen-only mode before returning.
"""

import argparse
import math
import os
import string
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from lib import canbus, diagnostic_safety, uds
from lib.modules import get
from tools.ecu_discover import preflight


MUTATING_SERVICES = {
    0x11: "ECUReset",
    0x14: "ClearDiagnosticInformation",
    0x27: "SecurityAccess",
    0x28: "CommunicationControl",
    0x2E: "WriteDataByIdentifier",
    0x2F: "InputOutputControlByIdentifier",
    0x85: "ControlDTCSetting",
}
READ_SERVICES = {
    0x19: "ReadDTCInformation",
    0x1A: "ReadECUIdentification",
    0x22: "ReadDataByIdentifier",
}


def parse_hex_byte(text):
    if len(text) != 2 or any(character not in string.hexdigits for character in text):
        raise argparse.ArgumentTypeError(
            f"{text!r} is not one byte; pass exactly two hexadecimal digits per byte"
        )
    return int(text, 16)


def classify_payload(payload):
    """Return ``(safety_class, label)`` for the exact raw request.

    Unknown shapes fail closed into the mutation/actuation confirmation path. This helper is
    deliberately conservative: an arbitrary manufacturer-specific SID may change vehicle state.
    """
    payload = bytes(payload)
    sid = payload[0]
    if sid in READ_SERVICES:
        return "diagnostic_read", READ_SERVICES[sid]
    if sid == 0x31:
        if len(payload) >= 2 and payload[1] == 0x03:
            return "diagnostic_read", "RoutineControl/requestRoutineResults"
        if len(payload) >= 2 and payload[1] in (0x01, 0x02):
            action = "startRoutine" if payload[1] == 0x01 else "stopRoutine"
            return "mutation_actuation", f"RoutineControl/{action}"
        return "mutation_actuation", "RoutineControl/unknown-or-malformed-subfunction"
    if sid == 0x10:
        return "session_change", "DiagnosticSessionControl"
    if sid == 0x3E:
        return "session_maintenance", "TesterPresent"
    if sid in MUTATING_SERVICES:
        return "mutation_actuation", MUTATING_SERVICES[sid]
    return "mutation_actuation", f"unclassified SID 0x{sid:02X} (fail-closed)"


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("module", help="verified key from lib/modules.py")
    p.add_argument("payload", nargs="+", type=parse_hex_byte, metavar="BYTE")
    p.add_argument("--execute", action="store_true", help="send the planned physical request")
    p.add_argument("--confirm-parked", action="store_true", help="assert the vehicle is parked")
    p.add_argument("--confirm-engine-off", action="store_true", help="assert the engine is stopped")
    p.add_argument(
        "--confirm-session-change",
        action="store_true",
        help="acknowledge diagnostic-session state or maintenance traffic",
    )
    p.add_argument(
        "--confirm-no-active-routine",
        action="store_true",
        help="assert no in-progress routine state must survive a DiagnosticSessionControl request",
    )
    p.add_argument(
        "--confirm-actuation",
        action="store_true",
        help="explicitly authorize the exact mutation/actuation payload shown in the plan",
    )
    p.add_argument("--pair", help="physical DLC/tap pair; required with --execute")
    p.add_argument("--conditions", help="ignition/engine/wake state; required with --execute")
    p.add_argument("--timeout", type=float, default=0.75, help="initial response timeout (default: 0.75s)")
    return p


def confirmation_errors(args, safety_class):
    errors = []
    if not args.confirm_parked:
        errors.append("--confirm-parked")
    if not args.pair:
        errors.append("--pair")
    if not args.conditions:
        errors.append("--conditions")
    if safety_class in ("session_change", "session_maintenance"):
        if not args.confirm_engine_off:
            errors.append("--confirm-engine-off")
        if not args.confirm_session_change:
            errors.append("--confirm-session-change")
    if safety_class == "session_change" and not args.confirm_no_active_routine:
        errors.append("--confirm-no-active-routine")
    if safety_class == "mutation_actuation":
        if not args.confirm_engine_off:
            errors.append("--confirm-engine-off")
        if not args.confirm_session_change:
            errors.append("--confirm-session-change")
        if not args.confirm_actuation:
            errors.append("--confirm-actuation")
    return errors


def required_live_flags(safety_class):
    """Return the exact flag names the dry-run plan must advertise for this request class."""
    flags = ["--execute", "--confirm-parked", "--pair PAIR", "--conditions DESCRIPTION"]
    if safety_class in ("session_change", "session_maintenance"):
        flags.extend(("--confirm-engine-off", "--confirm-session-change"))
    if safety_class == "session_change":
        flags.append("--confirm-no-active-routine")
    if safety_class == "mutation_actuation":
        flags.extend(
            ("--confirm-engine-off", "--confirm-session-change", "--confirm-actuation")
        )
    return flags


def main(argv=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    module = get(args.module)
    payload = bytes(args.payload)
    safety_class, label = classify_payload(payload)

    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 10.0:
        print("ERROR: --timeout must be finite, >0, and <=10 seconds", file=sys.stderr)
        return 2

    print(f"ACTIVE PHYSICAL UDS PLAN: {module.key} ({module.name})")
    print(
        f"{module.addressing_mode} {module.bitrate} bit/s "
        f"TX={module.txid:X} RX={module.rxid:X}"
    )
    print(f"payload: {uds.hx(payload)}")
    print(f"classification: {safety_class} ({label})")
    print("required live flags: " + " ".join(required_live_flags(safety_class)))
    if not args.execute:
        print("DRY RUN: no preflight, CAN socket, or transmission occurred.")
        return 0

    missing = confirmation_errors(args, safety_class)
    if missing:
        print(
            "ERROR: live request is missing required confirmation(s): " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2

    errors = preflight(module.channel, module.bitrate)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2

    try:
        lock = diagnostic_safety.acquire_channel_lock(module.channel)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: refusing to transmit: {exc}", file=sys.stderr)
        return 2

    sock = None
    response = None
    status = None
    fatal_error = None
    interrupted = False
    restored_passive = False
    with diagnostic_safety.interrupt_on_termination() as termination:
        try:
            sock = uds.open_module_socket(module, timeout=args.timeout)
            uds.drain(sock)
            response, status = uds.request(
                sock,
                payload,
                timeout=args.timeout,
                retries=0,
                response_pending_timeout=max(5.0, args.timeout * 5.0),
                max_pending_responses=32,
            )
        except KeyboardInterrupt:
            interrupted = True
            print("Interrupted; cleaning up without replaying the request.", file=sys.stderr)
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
            finally:
                try:
                    restored_passive = bool(canbus.restore_passive(module.channel, module.bitrate))
                except Exception as exc:
                    if fatal_error is None:
                        fatal_error = f"passive restore failed: {type(exc).__name__}: {exc}"
                finally:
                    diagnostic_safety.release_channel_lock(lock)

    if termination.received_signal is not None:
        interrupted = True

    if not restored_passive:
        print("ERROR: passive restoration could not be verified; check the interface now", file=sys.stderr)
    if response is not None:
        print(f"TX  : {uds.hx(payload)}")
        print(f"RX  : {uds.hx(response)}")
        print(f"stat: {status}")
    elif not interrupted and fatal_error is None:
        print(f"TX  : {uds.hx(payload)}")
        print("RX  : (none)")
        print(f"stat: {status}")
    print(f"adapter restored passive: {'yes' if restored_passive else 'NO - CHECK IT NOW'}")

    if fatal_error or not restored_passive:
        return 1
    return 130 if interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
