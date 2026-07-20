#!/usr/bin/env python3
"""Inventory bounded ECU identity DIDs for one verified registry module.

This sends ACTIVE, non-mutating UDS ReadDataByIdentifier (0x22) requests. It is not a
passive capture. The default set contains standardized identification DIDs already observed
in this repository or useful for exact ODX/PDX matching. VIN (F190) is excluded unless
``--include-vin`` is explicitly selected. Some FCA composite identity records (notably F1A0)
can embed a VIN anyway; VIN-shaped values are redacted to the tracked-data convention of
preserving the first 11 characters and replacing the unique serial with ``######``.

Dry run (nothing transmitted):

    python3 tools/identity_inventory.py radar_acc

Live use requires tpms-logger stopped and the intended bus explicitly armed:

    python3 tools/identity_inventory.py radar_acc --execute --confirm-parked --pair 6/14 \
        --conditions "ignition ON, engine OFF, SGW-bypass C-CAN"

The interface is restored to listen-only mode after every attempted live run. Machine output
lands under tmp/inventories/<module>/ and must be reviewed/masked before promotion.
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
VIN_DID = 0xF190
VIN_ALLOWED = frozenset(b"ABCDEFGHJKLMNPRSTUVWXYZ0123456789")
VIN_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)
VIN_TRANSLITERATION = {
    **{ord(str(number)): number for number in range(10)},
    **{
        ord(letter): value
        for letter, value in {
            "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
            "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
            "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
        }.items()
    },
}

# ISO 14229 identity namespace, restricted to candidates already useful in this repository.
# Labels describe the standardized namespace; an ECU's actual payload format remains per-ECU.
IDENTITY_DIDS = (
    (0xF180, "boot_software_identification"),
    (0xF181, "application_software_identification"),
    (0xF182, "application_data_identification"),
    (0xF183, "boot_software_fingerprint"),
    (0xF184, "application_software_fingerprint"),
    (0xF185, "application_data_fingerprint"),
    (0xF186, "active_diagnostic_session"),
    (0xF187, "vehicle_manufacturer_spare_part_number"),
    (0xF188, "vehicle_manufacturer_ecu_software_number"),
    (0xF189, "vehicle_manufacturer_ecu_software_version"),
    (0xF18A, "system_supplier_identifier"),
    (0xF18B, "ecu_manufacturing_date"),
    (0xF18C, "ecu_serial_number"),
    (0xF18D, "supported_functional_units"),
    (0xF18E, "vehicle_manufacturer_kit_assembly_part_number"),
    (0xF191, "vehicle_manufacturer_ecu_hardware_number"),
    (0xF192, "system_supplier_ecu_hardware_number"),
    (0xF193, "system_supplier_ecu_hardware_version"),
    (0xF194, "system_supplier_ecu_software_number"),
    (0xF195, "system_supplier_ecu_software_version"),
    (0xF196, "exhaust_regulation_or_type_approval_number"),
    (0xF197, "system_name_or_engine_type"),
    (0xF19E, "odx_file_identifier"),
    (0xF19F, "entity_identifier"),
    (0xF132, "fca_observed_firmware_fingerprint_candidate"),
    (0xF1A0, "fca_observed_identity_candidate_f1a0"),
    (0xF1A4, "fca_observed_identity_candidate_f1a4"),
    (0xF1A5, "fca_observed_identity_candidate_f1a5"),
)


def parse_did(text):
    try:
        did = int(text, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid hexadecimal DID: {text!r}") from None
    if not 0 <= did <= 0xFFFF:
        raise argparse.ArgumentTypeError("DID must be between 0000 and FFFF")
    return did


def selected_dids(args):
    if args.did:
        items = [(did, "operator_supplied") for did in args.did]
    else:
        items = list(IDENTITY_DIDS)
    if args.include_vin and all(did != VIN_DID for did, _ in items):
        items.append((VIN_DID, "vehicle_identification_number"))
    # Preserve request order while removing duplicates.
    return list(dict.fromkeys(items))


def printable_ascii(data):
    return "".join(chr(byte) if 32 <= byte < 127 else "." for byte in data)


def valid_vin_checksum(candidate):
    candidate = bytes(candidate).upper()
    if len(candidate) != 17 or any(byte not in VIN_ALLOWED for byte in candidate):
        return False
    total = sum(VIN_TRANSLITERATION[byte] * weight for byte, weight in zip(candidate, VIN_WEIGHTS))
    expected = ord("X") if total % 11 == 10 else ord(str(total % 11))
    return candidate[8] == expected


def mask_embedded_vins(data):
    """Redact checksum-valid VIN windows without changing record length or byte offsets."""
    original = bytes(data)
    masked = bytearray(original)
    for start in range(max(0, len(original) - 16)):
        candidate = original[start:start + 17]
        if valid_vin_checksum(candidate):
            masked[start + 11:start + 17] = b"######"
    return bytes(masked)


def redact_response_vins(did, response):
    """Return ``(safe_response, redacted)`` for a UDS identity response."""
    if response is None:
        return None, False
    original = bytes(response)
    safe = mask_embedded_vins(original)
    if (
        did == VIN_DID
        and len(original) >= 20
        and original[:3] == bytes((0x62, VIN_DID >> 8, VIN_DID & 0xFF))
    ):
        # Start from the already-scrubbed buffer so a second VIN elsewhere in a composite F190
        # response is not reintroduced while masking the direct 17-byte field.
        direct = bytearray(safe)
        direct[3 + 11:3 + 17] = b"######"
        safe = bytes(direct)
    return safe, safe != original


def classify_identity_response(did, response):
    if response is None:
        return "timeout", b""
    if len(response) >= 3 and response[0] == 0x62 and response[1:3] == did.to_bytes(2, "big"):
        return "positive", bytes(response[3:])
    if len(response) >= 3 and response[:2] == bytes.fromhex("7F 22"):
        return "negative", b""
    return "unexpected", b""


def query_identity(sock, did, label, timeout, accounting=None):
    payload = bytes((0x22, did >> 8, did & 0xFF))
    started = time.monotonic()
    uds.drain(sock)
    if accounting is not None:
        accounting["request_attempts"] += 1
    response, status = uds.request(sock, payload, timeout=timeout, retries=0)
    if accounting is not None and response:
        accounting["responses_received"] += 1
    category, data = classify_identity_response(did, response)
    safe_response, vin_redacted = redact_response_vins(did, response)
    safe_data = bytes(safe_response[3:]) if category == "positive" else b""
    response_hex = uds.hx(safe_response) if safe_response else None
    data_hex = uds.hx(safe_data) if safe_data else None
    ascii_value = printable_ascii(safe_data) if safe_data else None
    return {
        "did": f"{did:04X}",
        "label": label,
        "request_hex": uds.hx(payload),
        "response_hex": response_hex,
        "data_hex": data_hex,
        "ascii": ascii_value,
        "category": category,
        "status": status,
        "negative_response": uds.negative_response_details(response),
        "vin_redacted": vin_redacted,
        "elapsed_s": round(time.monotonic() - started, 3),
    }


def report_path(module):
    stamp = datetime.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f%z")
    return os.path.join(REPO, "tmp", "inventories", module.key, f"identity_{stamp}.json")


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
    p.add_argument("--execute", action="store_true", help="actually send the listed identity reads")
    p.add_argument("--did", action="append", type=parse_did, help="explicit hex DID; repeatable")
    p.add_argument("--include-vin", action="store_true", help="also request F190 (masked in output)")
    p.add_argument("--pair", help="physical pair/tap description; required with --execute")
    p.add_argument("--conditions", help="ignition/engine/wake state; required with --execute")
    p.add_argument("--confirm-parked", action="store_true", help="assert the vehicle is parked")
    p.add_argument("--rate", type=float, default=2.0, help="maximum requests/second (default: 2)")
    p.add_argument("--timeout", type=float, default=0.75, help="seconds per request (default: 0.75)")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    module = get(args.module)
    dids = selected_dids(args)
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

    print(f"ACTIVE ECU IDENTITY INVENTORY: {module.key} ({module.name})")
    print(
        f"{module.addressing_mode} {module.bitrate} bit/s "
        f"TX={module.txid:X} RX={module.rxid:X}; {len(dids)} physical 22 reads"
    )
    print("DIDs: " + " ".join(f"{did:04X}" for did, _ in dids))
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
            for index, (did, label) in enumerate(dids):
                result = query_identity(sock, did, label, args.timeout, accounting=accounting)
                results.append(result)
                value = result["ascii"] or result["response_hex"] or "(none)"
                print(f"{did:04X} {result['category']:<10} {value}")
                if index + 1 < len(dids):
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
        "tool": "tools/identity_inventory.py",
        "interaction": "active non-mutating UDS ReadDataByIdentifier",
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
        "direct_f190_requested": any(did == VIN_DID for did, _ in dids),
        "diagnostic_session_control_sent": False,
        "ecu_session": "inherited/unknown",
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
            or len(results) != len(dids)
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
