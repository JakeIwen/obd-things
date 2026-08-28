#!/usr/bin/env python3
"""Guarded one-frame C-CAN front-unlock proof for this exact ProMaster.

Dry-run is the default. The live path has no caller-selectable bus, interface,
identifier, action body, bitrate, counter, CRC, or send count. It uses the
reviewed fixed RF-Hub wake while retaining the same exclusive ownership, waits
for three ordinary CRC-valid sequential 0x1EF frames, sends exactly one
counter-current front-unlock frame, and restores the captured passive baseline.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import struct
import sys
import time


REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib import (  # noqa: E402
    can_handoff,
    can_operation_state,
    can_runtime_route,
    can_wake,
    canbus,
    uds,
)
from lib.modules import MODULES  # noqa: E402


ROLE = "c-can"
PAIR = "6/14"
B_CAN_ROLE = "b-can"
B_CAN_PAIR = "3/11"
ACTION_ID = 0x1EF
ACTION_PREFIXES = {
    "lock_all": bytes.fromhex("42 04 00 00 10 10"),
    "unlock_front": bytes.fromhex("42 04 00 00 11 E0"),
    "unlock_cargo": bytes.fromhex("42 04 00 00 11 F0"),
}
ACTION_PREFIX = ACTION_PREFIXES["unlock_front"]
ORDINARY_PREFIX = bytes.fromhex("42 00 00 00 00 00")
IGNITION_ID = 0x2EF
RPM_ID = 0x0FC
RPM_LIMIT = 400.0
REQUIRED_STREAK = 3
SYNC_TIMEOUT_SECONDS = 1.5
COP_MARKER = Path("/run/van-dashboard/cop-alert.active")
IGNITION_MARKER = Path("/home/pi/hooks/ignition_is_on")
COP_STATUS = Path("/run/van-cop-can-wake/status.json")
OUT_DIR = REPO / "tmp" / "ecu_mapping" / "rke_front_unlock"
ACCESS_STATE_SAMPLE_SECONDS = 1.0
B_CAN_ACCESS_SAMPLE_SECONDS = 2.5
MIN_LOCK_DOMAIN_SAMPLES = 2
B_CAN_ACCESS_IDS = (0x46C, 0x5B2, 0x5E2)
C_CAN_DOOR_CANDIDATE_IDS = (0x419, 0x4B1)
BCM_DOOR_INPUT_DID = 0x0130
BCM_DOOR_INPUT_REQUEST = bytes.fromhex("22 01 30")
LOCK_DOMAIN_STATES = {
    0x02: ("locked", True, True),
    0x06: ("front_unlocked_cargo_locked", False, True),
    0x00: ("front_and_cargo_unlocked", False, False),
}


class ReplayError(RuntimeError):
    pass


def crc8_sae_j1850(data: bytes) -> int:
    crc = 0xFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc ^ 0xFF


def build_action(action: str, counter: int) -> bytes:
    try:
        prefix = ACTION_PREFIXES[action]
    except KeyError as exc:
        raise ValueError(f"unsupported fixed action {action!r}") from exc
    if not 0 <= counter <= 0x0F:
        raise ValueError("counter must be a 4-bit value")
    body = prefix + bytes((counter,))
    return body + bytes((crc8_sae_j1850(body),))


def build_front_unlock(counter: int) -> bytes:
    return build_action("unlock_front", counter)


def _filter(can_id: int) -> bytes:
    return struct.pack(
        "=II",
        can_id,
        can_wake.CAN_SFF_MASK | can_wake.CAN_EFF_FLAG | can_wake.CAN_RTR_FLAG,
    )


def _parse_raw(raw: bytes) -> tuple[int, bytes] | None:
    if len(raw) != 16:
        return None
    can_id, dlc, data = struct.unpack("=IB3x8s", raw)
    if can_id & (can_wake.CAN_EFF_FLAG | can_wake.CAN_RTR_FLAG | can_wake.CAN_ERR_FLAG):
        return None
    return can_id & can_wake.CAN_SFF_MASK, data[: min(dlc, 8)]


def _ordinary_counter(data: bytes) -> int | None:
    if len(data) != 8 or data[:6] != ORDINARY_PREFIX or data[6] & 0xF0:
        return None
    if crc8_sae_j1850(data[:7]) != data[7]:
        return None
    return data[6] & 0x0F


def synchronize_and_send(
    sock, *, action: str = "unlock_front", clock=time.monotonic
) -> tuple[bytes, tuple[int, ...]]:
    if action not in ACTION_PREFIXES:
        raise ValueError(f"unsupported fixed action {action!r}")
    deadline = clock() + SYNC_TIMEOUT_SECONDS
    streak: list[int] = []
    while clock() < deadline:
        sock.settimeout(max(0.01, deadline - clock()))
        try:
            raw = sock.recv(16)
        except socket.timeout:
            break
        parsed = _parse_raw(raw)
        if parsed is None:
            continue
        can_id, data = parsed
        if can_id == IGNITION_ID:
            raise ReplayError("ignition witness 0x2EF appeared during synchronization")
        if can_id == RPM_ID and len(data) >= 2:
            rpm = (int.from_bytes(data[:2], "big") & 0xFFFC) / 4.0
            if rpm >= RPM_LIMIT:
                raise ReplayError(f"engine speed became {rpm:.0f} rpm")
            continue
        if can_id != ACTION_ID:
            continue
        counter = _ordinary_counter(data)
        if counter is None:
            streak.clear()
            continue
        if streak and counter != ((streak[-1] + 1) & 0x0F):
            streak[:] = [counter]
        else:
            streak.append(counter)
        if len(streak) < REQUIRED_STREAK:
            continue
        next_counter = (streak[-1] + 1) & 0x0F
        payload = build_action(action, next_counter)
        frame = struct.pack("=IB3x8s", ACTION_ID, 8, payload)
        sent = sock.send(frame)
        if sent != len(frame):
            raise ReplayError(f"single frame send length {sent} was not {len(frame)}")
        return payload, tuple(streak[-REQUIRED_STREAK:])
    raise ReplayError("no three-frame CRC-valid sequential 0x1EF streak before timeout")


def prearm_conflicts() -> tuple[str, ...]:
    conflicts = []
    if COP_MARKER.exists():
        conflicts.append("COP ALERT marker is active")
    if IGNITION_MARKER.exists():
        conflicts.append("ignition marker is active")
    try:
        status = json.loads(COP_STATUS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        status = {}
    except (OSError, json.JSONDecodeError):
        conflicts.append("COP wake status is unreadable")
        status = {}
    if status.get("transaction_in_progress") or status.get("marker_active"):
        conflicts.append("COP wake supervisor is active")
    return tuple(conflicts)


def _summarize_frames(
    frames: list[tuple[int, bytes]], identifiers: tuple[int, ...]
) -> dict[str, dict[str, object]]:
    summaries: dict[str, dict[str, object]] = {}
    for can_id in identifiers:
        matching = [data for observed_id, data in frames if observed_id == can_id]
        distinct_hex = []
        for data in matching:
            encoded = data.hex(" ").upper()
            if encoded not in distinct_hex:
                distinct_hex.append(encoded)
        summaries[f"{can_id:03X}"] = {
            "count": len(matching),
            "first_hex": matching[0].hex(" ").upper() if matching else None,
            "last_hex": matching[-1].hex(" ").upper() if matching else None,
            "distinct_hex": distinct_hex,
        }
    return summaries


def _sample_passive_role(
    role: str,
    pair: str,
    identifiers: tuple[int, ...],
    *,
    duration: float = ACCESS_STATE_SAMPLE_SECONDS,
) -> dict[str, object]:
    with can_runtime_route.acquire_passive_bus_route(
        role, asserted_pair=pair
    ) as ownership:
        frames = can_wake._recv_standard_frames(
            ownership.route.channel, identifiers, duration
        )
        ownership.revalidate()
    return {
        "role": role,
        "pair": pair,
        "sample_seconds": duration,
        "frames": _summarize_frames(frames, identifiers),
        "error": None,
    }


def _sample_active_c_can(channel: str) -> dict[str, object]:
    frames = can_wake._recv_standard_frames(
        channel, C_CAN_DOOR_CANDIDATE_IDS, ACCESS_STATE_SAMPLE_SECONDS
    )
    return {
        "role": ROLE,
        "pair": PAIR,
        "sample_seconds": ACCESS_STATE_SAMPLE_SECONDS,
        "frames": _summarize_frames(frames, C_CAN_DOOR_CANDIDATE_IDS),
        "error": None,
    }


def _read_bcm_door_inputs(channel: str) -> dict[str, object]:
    sample = {
        "did_hex": f"{BCM_DOOR_INPUT_DID:04X}",
        "request_count": 0,
        "response_hex": None,
        "data_hex": None,
        "status": None,
        "error": None,
    }
    sock = None
    try:
        sock = uds.open_module_socket(
            MODULES["bcm_ccan"], timeout=0.75, channel=channel
        )
        uds.drain(sock)
        sample["request_count"] = 1
        response, status = uds.request(
            sock, BCM_DOOR_INPUT_REQUEST, timeout=0.75, retries=0
        )
        sample["status"] = status
        sample["response_hex"] = (
            bytes(response).hex(" ").upper() if response else None
        )
        expected = bytes.fromhex("62 01 30")
        if response is None:
            sample["error"] = "BCM 0130 timed out"
        elif len(response) != 4 or not bytes(response).startswith(expected):
            sample["error"] = "BCM 0130 did not return the exact one-byte positive response"
        else:
            sample["data_hex"] = f"{response[3]:02X}"
    except Exception as exc:
        sample["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
    return sample


def _failed_sample(
    role: str,
    pair: str,
    identifiers: tuple[int, ...],
    exc: BaseException,
    *,
    duration: float = ACCESS_STATE_SAMPLE_SECONDS,
) -> dict[str, object]:
    return {
        "role": role,
        "pair": pair,
        "sample_seconds": duration,
        "frames": _summarize_frames([], identifiers),
        "error": f"{type(exc).__name__}: {exc}",
    }


def decode_lock_domains(b_can_sample: dict[str, object]) -> dict[str, object]:
    frame_summary = b_can_sample.get("frames", {}).get("5E2", {})
    count = frame_summary.get("count", 0)
    distinct = frame_summary.get("distinct_hex", [])
    if not isinstance(count, int) or count < MIN_LOCK_DOMAIN_SAMPLES:
        return {
            "state": "unknown",
            "front_locked": None,
            "cargo_locked": None,
            "quality": "insufficient_samples",
            "source": "b-can.0x5e2.byte1",
        }
    if not isinstance(distinct, list) or len(distinct) != 1:
        return {
            "state": "unknown",
            "front_locked": None,
            "cargo_locked": None,
            "quality": "unstable_sample",
            "source": "b-can.0x5e2.byte1",
        }
    raw = distinct[0]
    try:
        data = bytes.fromhex(raw) if isinstance(raw, str) else b""
    except ValueError:
        data = b""
    decoded = LOCK_DOMAIN_STATES.get(data[1]) if len(data) >= 2 else None
    if decoded is None:
        return {
            "state": "unknown",
            "front_locked": None,
            "cargo_locked": None,
            "quality": "unavailable" if not raw else "unmapped_value",
            "source": "b-can.0x5e2.byte1",
        }
    state, front_locked, cargo_locked = decoded
    return {
        "state": state,
        "front_locked": front_locked,
        "cargo_locked": cargo_locked,
        "quality": "verified",
        "source": "b-can.0x5e2.byte1",
    }


def _decode_doors(bcm_door_inputs: dict[str, object]) -> dict[str, dict[str, object]]:
    unmapped = {
        "locked": None,
        "ajar": None,
        "lock_quality": "unmapped_individual_door",
        "ajar_quality": "unmapped_individual_door",
    }
    doors = {
        "driver": dict(unmapped),
        "passenger": dict(unmapped),
        "sliding": dict(unmapped),
        "rear": dict(unmapped),
    }
    doors["sliding"].update(
        {
            "reported_closed": True,
            "physical_state_observable": False,
            "ajar_quality": "hardware_bypass_forced_closed",
        }
    )
    raw = bcm_door_inputs.get("data_hex")
    try:
        value = int(raw, 16) if isinstance(raw, str) else None
    except ValueError:
        value = None
    if value is not None:
        doors["driver"].update(
            {
                "ajar": not bool(value & 0x04),
                "ajar_quality": "candidate_one_controlled_trial",
                "ajar_source": "bcm.did.0130.mask.0x04_inverted",
            }
        )
    return doors


def read_access_state_once() -> dict[str, object]:
    """Use one held C-CAN wake to collect the bounded access-state snapshot.

    Exactly one fixed BCM door-input read follows the wake. All remaining
    observations are passive. Missing traffic produces explicit unknown fields
    and never causes a second wake or an automatic diagnostic retry.
    """

    handoff = None
    handoff_acquired = False
    session = None
    wake_result = None
    bcm_door_inputs = None
    b_can = None
    c_can = None
    with can_wake._termination_guard() as termination:
        try:
            handoff = can_handoff.active_turn(ROLE)
            handoff.__enter__()
            handoff_acquired = True
            session = can_wake._open_wake_session(
                ROLE, prearm_check=prearm_conflicts
            )
            wake_result = session.trigger()
            session._ensure_active()
            route = session._ownership.route
            bcm_door_inputs = _read_bcm_door_inputs(route.channel)
            session._ensure_active()
            try:
                c_can = _sample_active_c_can(route.channel)
            except Exception as exc:
                c_can = _failed_sample(ROLE, PAIR, C_CAN_DOOR_CANDIDATE_IDS, exc)
            session._ensure_active()
            try:
                b_can = _sample_passive_role(
                    B_CAN_ROLE,
                    B_CAN_PAIR,
                    B_CAN_ACCESS_IDS,
                    duration=B_CAN_ACCESS_SAMPLE_SECONDS,
                )
            except Exception as exc:
                b_can = _failed_sample(
                    B_CAN_ROLE,
                    B_CAN_PAIR,
                    B_CAN_ACCESS_IDS,
                    exc,
                    duration=B_CAN_ACCESS_SAMPLE_SECONDS,
                )
            session._ensure_active()
        finally:
            termination.begin_cleanup()
            try:
                if session is not None:
                    session.close()
            finally:
                if handoff_acquired and handoff is not None:
                    handoff.__exit__(None, None, None)

    if wake_result is None or bcm_door_inputs is None or b_can is None or c_can is None:
        raise ReplayError("access-state collection ended without a complete bounded result")

    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "complete": False,
        "wake": {
            "count": 1,
            "role": wake_result.role,
            "source": wake_result.source,
            "detail": wake_result.detail,
            "additional_tx_after_wake": bcm_door_inputs["request_count"],
            "restored_passive_after_sampling": True,
        },
        "lock_domains": decode_lock_domains(b_can),
        "doors": _decode_doors(bcm_door_inputs),
        "observations": {
            "b_can": b_can,
            "c_can": c_can,
            "bcm_door_inputs": bcm_door_inputs,
        },
        "limitations": [
            "individual door lock booleans are not mapped",
            "only driver ajar has a controlled candidate; passenger/sliding/rear remain unmapped",
            "sliding-door factory ajar input is hardware-bypassed closed and cannot prove physical closure",
            "0x419 and 0x4B1 are returned raw only",
        ],
    }


def execute_once(action: str = "unlock_front") -> dict[str, object]:
    if action not in ACTION_PREFIXES:
        raise ValueError(f"unsupported fixed action {action!r}")
    handoff = None
    handoff_acquired = False
    session = None
    payload = None
    counters = None
    restored = False
    with can_wake._termination_guard() as termination:
        try:
            handoff = can_handoff.active_turn(ROLE)
            handoff.__enter__()
            handoff_acquired = True
            session = can_wake._open_wake_session(
                ROLE, prearm_check=prearm_conflicts
            )
            wake_result = session.trigger()
            session._ensure_active()
            route = session._ownership.route
            sock = socket.socket(can_wake.AF_CAN, socket.SOCK_RAW, can_wake.CAN_RAW)
            try:
                filters = b"".join(
                    _filter(can_id) for can_id in (ACTION_ID, IGNITION_ID, RPM_ID)
                )
                sock.setsockopt(can_wake.SOL_CAN_RAW, can_wake.CAN_RAW_FILTER, filters)
                sock.bind((route.channel,))
                payload, counters = synchronize_and_send(sock, action=action)
            finally:
                sock.close()
            return {
                "success": True,
                "wake_detail": wake_result.detail,
                "role": route.role,
                "pair": route.pair,
                "payload_hex": payload.hex(" ").upper(),
                "counter_streak": list(counters),
                "send_count": 1,
                "action": action,
            }
        finally:
            termination.begin_cleanup()
            try:
                if session is not None:
                    session.close()
                    restored = True
            finally:
                if handoff_acquired and handoff is not None:
                    handoff.__exit__(None, None, None)
            if payload is not None and not restored:
                raise canbus.PassiveRestoreError(
                    "front-unlock frame sent but passive restoration was not verified"
                )


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--pair")
    ap.add_argument("--conditions")
    ap.add_argument("--confirm-parked", action="store_true")
    ap.add_argument("--confirm-ignition-off", action="store_true")
    ap.add_argument("--confirm-engine-off", action="store_true")
    ap.add_argument("--confirm-front-unlock-only", action="store_true")
    ap.add_argument("--confirm-recovery-access", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    plan = {
        "mode": "execute" if args.execute else "dry_run",
        "role": ROLE,
        "pair": PAIR,
        "can_id_hex": f"{ACTION_ID:03X}",
        "payload_template": "42 04 00 00 11 E0 CC CRC",
        "counter_policy": "next after three CRC-valid sequential ordinary frames",
        "send_count": 1,
        "restores_passive": True,
        "transmits_can_frames": bool(args.execute),
    }
    if not args.execute:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    required = {
        "--pair 6/14": args.pair == PAIR,
        "--conditions TEXT": bool(args.conditions and args.conditions.strip()),
        "--confirm-parked": args.confirm_parked,
        "--confirm-ignition-off": args.confirm_ignition_off,
        "--confirm-engine-off": args.confirm_engine_off,
        "--confirm-front-unlock-only": args.confirm_front_unlock_only,
        "--confirm-recovery-access": args.confirm_recovery_access,
    }
    missing = [name for name, present in required.items() if not present]
    if missing:
        print("error: missing live gates: " + ", ".join(missing), file=sys.stderr)
        return 2
    try:
        result = execute_once()
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    result["conditions"] = args.conditions
    result["completed_at"] = datetime.now(timezone.utc).isoformat()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"proof_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
