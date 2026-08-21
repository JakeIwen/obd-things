#!/usr/bin/env python3
"""Plan a conservative multi-module DTC scan or import completed inventory reports.

The default is an exact OFFLINE plan: one physical ``19 02 FF`` request per selected registry
module, sequentially, with no session change and no DTC clear.  This tool intentionally has no live
execution mode.  Runtime bus-role routes can be recorded in the plan as ``BUS=canN`` without using
the unstable static ``Module.channel`` default.

Completed JSON reports produced by ``tools/dtc_inventory.py`` can be previewed offline with
``--import-report``.  Add ``--commit`` to store those already-completed observations in SQLite and
atomically refresh a cache-only dashboard summary.  Import mode never opens a CAN socket.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

from lib.dtc import DtcHistory, DtcParseError, scan_from_inventory_report, write_cache
from lib.modules import MODULES


BUS_ORDER = ("c-can", "b-can", "can-ch")
BUS_PAIRS = {"c-can": "6/14", "b-can": "3/11", "can-ch": "12/13"}
REQUEST_HEX = "19 02 FF"
DEFAULT_DB = os.path.join(REPO, "tmp", "vehicle_data", "dtc-history.sqlite3")
DEFAULT_CACHE = os.path.join(REPO, "tmp", "vehicle_data", "dtc-cache.json")
MIN_PLAN_RATE_HZ = 0.1
MAX_PLAN_RATE_HZ = 1.0
CHANNEL_RE = re.compile(r"can[0-9]+\Z")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("modules", nargs="*", help="registry module keys (default: all registered modules)")
    p.add_argument(
        "--bus",
        action="append",
        choices=BUS_ORDER,
        help="restrict the plan to one or more logical buses",
    )
    p.add_argument(
        "--route",
        action="append",
        default=[],
        metavar="BUS=canN",
        help="runtime logical-bus route for the offline plan; repeat per bus",
    )
    p.add_argument(
        "--resolve-runtime",
        action="store_true",
        help=(
            "read stable USB identities to resolve the current canN routes for the plan; "
            "still opens no CAN socket"
        ),
    )
    p.add_argument(
        "--rate",
        type=float,
        default=1.0,
        help="planned sequential request ceiling in Hz (0.1..1.0; default: 1)",
    )
    p.add_argument(
        "--import-report",
        action="append",
        default=[],
        metavar="PATH",
        help="preview a completed tools/dtc_inventory.py JSON report; repeatable",
    )
    p.add_argument(
        "--commit",
        action="store_true",
        help="commit imported reports to SQLite and refresh the JSON cache (still no CAN I/O)",
    )
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite history path used with --commit")
    p.add_argument("--cache-out", default=DEFAULT_CACHE, help="cache-only JSON path used with --commit")
    p.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return p


def parse_routes(values: list[str]) -> dict[str, str]:
    routes: dict[str, str] = {}
    channels: dict[str, str] = {}
    for value in values:
        if value.count("=") != 1:
            raise ValueError(f"invalid --route {value!r}; expected BUS=canN")
        bus, channel = value.split("=", 1)
        if bus not in BUS_ORDER:
            raise ValueError(f"unknown logical bus {bus!r}; choose {', '.join(BUS_ORDER)}")
        if not CHANNEL_RE.fullmatch(channel):
            raise ValueError(f"invalid SocketCAN channel {channel!r}; expected canN")
        if bus in routes:
            raise ValueError(f"duplicate route for {bus}")
        if channel in channels:
            raise ValueError(
                f"{channel} cannot resolve both {channels[channel]} and {bus} in one topology"
            )
        routes[bus] = channel
        channels[channel] = bus
    return routes


def selected_modules(keys: list[str], buses: list[str] | None) -> list[Any]:
    unknown = [key for key in keys if key not in MODULES]
    if unknown:
        raise ValueError(f"unknown module(s): {', '.join(unknown)}")
    if len(keys) != len(set(keys)):
        raise ValueError("module keys must not be repeated")
    candidates = [MODULES[key] for key in keys] if keys else list(MODULES.values())
    bus_filter = set(buses or BUS_ORDER)
    selected = [module for module in candidates if module.bus in bus_filter]
    if not selected:
        raise ValueError("module/bus selection is empty")
    order = {bus: index for index, bus in enumerate(BUS_ORDER)}
    registry_order = {key: index for index, key in enumerate(MODULES)}
    return sorted(selected, key=lambda module: (order[module.bus], registry_order[module.key]))


def build_plan(modules: list[Any], routes: dict[str, str], rate_hz: float) -> dict[str, Any]:
    entries = []
    for index, module in enumerate(modules, start=1):
        resolved = routes.get(module.bus)
        entries.append(
            {
                "sequence": index,
                "module_key": module.key,
                "module_name": module.name,
                "logical_bus": module.bus,
                "resolved_channel": resolved,
                "route_state": "resolved" if resolved else "unresolved",
                "physical_pair": BUS_PAIRS[module.bus],
                "bitrate": module.bitrate,
                "addressing_mode": module.addressing_mode,
                "txid": f"{module.txid:X}",
                "rxid": f"{module.rxid:X}",
                "request_hex": REQUEST_HEX,
            }
        )
    return {
        "tool": "tools/dtc_scan.py",
        "mode": "offline_plan",
        "dry_run": True,
        "live_execution_implemented": False,
        "interaction_if_later_executed": "active non-mutating UDS ReadDTCInformation",
        "execution_policy": "sequential_one_request_per_module",
        "request_hex": REQUEST_HEX,
        "status_mask": "FF",
        "max_request_rate_hz": rate_hz,
        "minimum_request_interval_s": round(1.0 / rate_hz, 3),
        "estimated_minimum_duration_s": round(max(0, len(entries) - 1) / rate_hz, 3),
        "module_count": len(entries),
        "all_routes_resolved": all(entry["resolved_channel"] for entry in entries),
        "runtime_routes": routes,
        "registry_static_channel_used": False,
        "clear_dtc_service_implemented": False,
        "diagnostic_session_control_implemented": False,
        "tester_present_implemented": False,
        "functional_broadcast_implemented": False,
        "modules": entries,
    }


def resolve_runtime_routes(modules: list[Any]) -> tuple[dict[str, str], str]:
    """Resolve each available logical bus from one read-only topology snapshot.

    A missing or ambiguous role is deliberately omitted so the offline plan
    still describes modules on independently resolved buses.  This function
    never configures a link or opens CAN.
    """

    from projects.vehicle_data.can_interfaces import PassiveInterfaceManager

    manager = PassiveInterfaceManager()
    topology = manager.topology()
    routes: dict[str, str] = {}
    for bus in dict.fromkeys(module.bus for module in modules):
        resolution = topology.resolution(bus)
        if resolution.state != "resolved" or resolution.channel is None:
            continue
        if resolution.channel in routes.values():
            raise RuntimeError(
                f"one topology resolved multiple logical buses to {resolution.channel}"
            )
        routes[bus] = resolution.channel
    return routes, topology.fingerprint


def load_inventory(path: str) -> tuple[Any, dict[str, Any]]:
    report_path = Path(path)
    raw = report_path.read_bytes()
    try:
        report = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DtcParseError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(report, dict):
        raise DtcParseError(f"{path}: report root must be an object")
    digest = hashlib.sha256(raw).hexdigest()
    # Validate first so the identity is derived from the same normalized timestamps, payload
    # bytes, outcome, and registry metadata that will actually be persisted.  This keeps JSON
    # formatting, equivalent timezone offsets, and hex spelling from creating duplicate scans.
    scan = scan_from_inventory_report(
        report,
        source_key="dtc-inventory-v2:validation-pending",
        source_ref=str(report_path),
    )
    semantic_identity = {
        "identity_version": 2,
        "tool": "tools/dtc_inventory.py",
        "module": {
            "key": scan.module_key,
            "bus": scan.logical_bus,
            "bitrate": scan.bitrate,
        },
        "started_at": scan.started_at,
        "completed_at": scan.completed_at,
        "outcome": scan.outcome,
        "unavailable_reason": scan.unavailable_reason,
        "status_availability_mask": scan.status_availability_mask,
        "request_hex": scan.request_hex,
        "response_hex": scan.response_hex,
        "dtcs": [
            {"raw_dtc": record.raw_dtc, "status": record.status} for record in scan.dtcs
        ],
    }
    semantic_bytes = json.dumps(
        semantic_identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    semantic_digest = hashlib.sha256(semantic_bytes).hexdigest()
    scan = replace(
        scan,
        source_key=f"dtc-inventory-v2:{semantic_digest}",
    )
    preview = {
        "source_ref": str(report_path),
        "source_sha256": digest,
        "source_key": scan.source_key,
        "module_key": scan.module_key,
        "logical_bus": scan.logical_bus,
        "resolved_channel": scan.resolved_channel,
        "completed_at": scan.completed_at,
        "outcome": scan.outcome,
        "unavailable_reason": scan.unavailable_reason,
        "dtc_count": len(scan.dtcs) if scan.outcome == "success" else None,
        "explicit_zero_dtcs": scan.outcome == "success" and not scan.dtcs,
    }
    return scan, preview


def _print_plan(plan: dict[str, Any]) -> None:
    print("OFFLINE DTC SCAN PLAN — no CAN socket opened and nothing transmitted")
    print(
        f"{plan['module_count']} modules; exact request {plan['request_hex']}; "
        f"sequential <= {plan['max_request_rate_hz']:g} request/s"
    )
    print("No 14 clear, 10 session control, 3E tester present, or functional broadcast exists here.")
    for entry in plan["modules"]:
        channel = entry["resolved_channel"] or "UNRESOLVED"
        print(
            f"{entry['sequence']:>2}. {entry['module_key']:<16} "
            f"{entry['logical_bus']:<6} -> {channel:<10} "
            f"pins {entry['physical_pair']:<5} {entry['request_hex']}"
        )
    if not plan["all_routes_resolved"]:
        print("Runtime routes are incomplete; unstable registry canN defaults were deliberately ignored.")


def _print_imports(previews: list[dict[str, Any]], committed: bool, cache_path: str | None) -> None:
    heading = "IMPORTED OFFLINE REPORTS" if committed else "OFFLINE IMPORT PREVIEW — no files written"
    print(heading)
    for preview in previews:
        if preview["outcome"] == "success":
            detail = f"success dtcs={preview['dtc_count']}"
        else:
            detail = f"unavailable reason={preview['unavailable_reason']}"
        print(
            f"{preview['module_key']:<16} {preview['logical_bus']:<6} "
            f"channel={preview['resolved_channel'] or 'unknown':<8} {detail}"
        )
    if committed and cache_path:
        print(f"cache-only summary: {cache_path}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not math.isfinite(args.rate) or not MIN_PLAN_RATE_HZ <= args.rate <= MAX_PLAN_RATE_HZ:
        print(
            f"ERROR: --rate must be between {MIN_PLAN_RATE_HZ:g} and {MAX_PLAN_RATE_HZ:g}",
            file=sys.stderr,
        )
        return 2
    if args.commit and not args.import_report:
        print("ERROR: --commit requires at least one --import-report", file=sys.stderr)
        return 2
    if args.import_report and (args.modules or args.bus or args.route or args.resolve_runtime):
        print(
            "ERROR: report import cannot be mixed with module, bus, or runtime-route planning",
            file=sys.stderr,
        )
        return 2

    if args.import_report:
        try:
            loaded = [load_inventory(path) for path in args.import_report]
        except (OSError, DtcParseError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        scans = [item[0] for item in loaded]
        previews = [item[1] for item in loaded]
        payload: dict[str, Any] = {
            "tool": "tools/dtc_scan.py",
            "mode": "offline_inventory_import",
            "can_io": False,
            "committed": False,
            "reports": previews,
        }
        if args.commit:
            batch_committed = False
            try:
                with DtcHistory(args.db) as history:
                    results = history.record_scans(
                        sorted(scans, key=lambda item: (item.completed_at, item.module_key))
                    )
                    batch_committed = True
                    summary = history.dashboard_summary(compact=True, per_group_limit=25)
            except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
                if batch_committed:
                    print(
                        "ERROR: history batch committed, but cache summary generation failed; "
                        f"rerun this idempotent import after fixing the history/cache reader: {exc}",
                        file=sys.stderr,
                    )
                else:
                    print(f"ERROR: history update failed: {exc}", file=sys.stderr)
                return 1
            try:
                write_cache(args.cache_out, summary)
            except OSError as exc:
                print(
                    "ERROR: history batch committed, but cache refresh failed; "
                    f"rerun this idempotent import after fixing the cache path: {exc}",
                    file=sys.stderr,
                )
                return 1
            payload.update(
                {
                    "committed": True,
                    "database": args.db,
                    "cache": args.cache_out,
                    "inserted_reports": sum(1 for result in results if result["inserted"]),
                    "duplicate_reports": sum(1 for result in results if not result["inserted"]),
                    "coverage": summary["coverage"],
                }
            )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            _print_imports(previews, args.commit, args.cache_out if args.commit else None)
        return 0

    try:
        modules = selected_modules(args.modules, args.bus)
        if args.resolve_runtime and args.route:
            raise ValueError("--resolve-runtime and --route are mutually exclusive")
        topology_fingerprint = None
        if args.resolve_runtime:
            routes, topology_fingerprint = resolve_runtime_routes(modules)
        else:
            routes = parse_routes(args.route)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    plan = build_plan(modules, routes, args.rate)
    plan["runtime_topology_fingerprint"] = topology_fingerprint
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        _print_plan(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
