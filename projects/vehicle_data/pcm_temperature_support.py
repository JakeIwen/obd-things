#!/usr/bin/env python3
"""Parked fixed PCM support checks; dry-run by default.

At most six padded physical SingleFrame requests, no retry, session control,
TesterPresent, wake, or ISO-TP FlowControl. Uses the reviewed role owner and
requires fresh ignition-on, zero RPM, and zero road speed before arming and
before each request. This tool does not publish telemetry.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import struct
import time

REPO = Path(__file__).resolve().parents[2]

from lib import can_handoff, can_operation_state, can_runtime_route, canbus, diagnostic_safety
from lib.modules import MODULES
from projects.vehicle_data import ccan_powertrain, pcm_electrical as pcm
from projects.vehicle_data import pcm_support_profiles as profiles
from tools.ecu_discover import prearm_conflict_errors
from tools.passive_drive_capture import atomic_write_json

CHECK_DIDS = profiles.TEMPERATURE_DIDS
CHECK_PROFILES = profiles.CHECK_PROFILES
TIMEOUT_SECONDS = 0.75
decode_reply = profiles.decode_reply


def stationary_errors(snapshot) -> list[str]:
    values = {o.metric: o.value for o in snapshot.observations}
    if (
        snapshot.frame_count <= 0
        or len(snapshot.rpm_samples) < 3
        or any(rpm != 0 for rpm in snapshot.rpm_samples)
        or values.get("vehicle.ignition_on") is not True
        or values.get("vehicle.speed") != 0
    ):
        return ["fresh ignition-on, three zero-RPM samples, and zero speed are required"]
    return []


def vehicle_gate(route) -> list[str]:
    return stationary_errors(ccan_powertrain.read_broadcast_snapshot(
        route.channel, timeout=0.5, required_rpm_samples=3,
    ))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(CHECK_PROFILES), default="temperature")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-parked-ignition-on-engine-off", action="store_true")
    args = parser.parse_args(argv)
    dids = CHECK_PROFILES[args.profile]
    plan = profiles.build_plan(args.profile, TIMEOUT_SECONDS, can_handoff.ACTIVE_WAIT_SECONDS)
    if not args.execute:
        print(json.dumps({"dry_run": True, **plan}, indent=2))
        return 0
    if not args.confirm_parked_ignition_on_engine_off:
        parser.error("--execute requires --confirm-parked-ignition-on-engine-off")
    report = {**plan, "started_at": datetime.now(timezone.utc).isoformat(),
              "results": [], "restored_passive": None, "error": None}
    output = REPO / "tmp" / "inventories" / "pcm" / (
        args.profile + "-support-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json"
    )
    atomic_write_json(output, report)
    owner = None
    try:
        # Let cooperating continuous/bounded broker observers release their
        # shared leases before taking the authoritative role/channel locks.
        # Keep admission closed until passive restoration and lock release.
        with (
            diagnostic_safety.interrupt_on_termination() as termination,
            can_handoff.active_turn(MODULES["pcm"].bus),
        ):
            try:
                owner = can_runtime_route.acquire_armed_bus_route(
                    MODULES["pcm"].bus, asserted_pair="6/14",
                    prearm_check=prearm_conflict_errors, passive_prearm_check=vehicle_gate,
                )
                report["channel"] = owner.route.channel
                with socket.socket(pcm.AF_CAN, socket.SOCK_RAW, pcm.CAN_RAW) as sock:
                    sock.setsockopt(pcm.SOL_CAN_RAW, pcm.CAN_RAW_FILTER,
                                    pcm.VVT_OIL_TEMPERATURE_PROFILE.response_filter)
                    sock.bind((owner.route.channel,))
                    for index, did in enumerate(dids):
                        if index:
                            time.sleep(1.0)
                        errors = vehicle_gate(owner.route)
                        can_runtime_route.revalidate_bus_route(owner.route, manager=owner.manager)
                        diagnostic_safety.validate_channel_lock(owner.channel_lock, owner.route.channel)
                        state = canbus.interface_state(owner.route.channel)
                        topology = can_operation_state.load_topology(owner.route.channel)
                        if (errors or not can_runtime_route._is_exact_armed_state(
                            state, owner.route.channel, 500000
                        ) or can_operation_state.active_inhibits(owner.route.channel)
                            or not topology.usable or topology.bus != "c-can" or topology.pair != "6/14"):
                            raise RuntimeError("pre-send vehicle/interface/topology gate failed: " + "; ".join(errors))
                        request = profiles.request_bytes(did)
                        row = {"did": f"{did:04X}", "request_hex": request.hex(" ").upper(),
                               "attempted_at": datetime.now(timezone.utc).isoformat()}
                        report["results"].append(row)
                        wire = struct.pack(pcm.CAN_FRAME_FORMAT, pcm.CAN_EFF_FLAG | MODULES["pcm"].txid, 8, request)
                        sock.settimeout(TIMEOUT_SECONDS)
                        if sock.send(wire) != len(wire):
                            raise RuntimeError("short PCM request send")
                        try:
                            received = sock.recv(pcm.CAN_FRAME_SIZE)
                            row["received_frame_hex"] = received.hex(" ").upper()
                            row.update(decode_reply(did, received))
                        except TimeoutError:
                            row["status"] = "timeout"
                        atomic_write_json(output, report)
            finally:
                termination.begin_cleanup()
                if owner is not None:
                    report["restored_passive"] = owner.release()
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(output, report)
    print(json.dumps({"report_path": str(output), **report}, indent=2))
    return 0 if not report["error"] and report["restored_passive"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
