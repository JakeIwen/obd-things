"""Safety policy and durable records for a bounded multi-module DTC batch.

This module does not import SocketCAN or open a transport.  The live wrapper in
``tools/dtc_batch.py`` is deliberately the only caller that can turn the fixed
plan into traffic.  Keeping selection, state-gate validation, job records, and
the inventory-compatible report schema here makes the dangerous boundary small
and straightforward to test offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from lib.dtc import READ_DTC_BY_STATUS_REQUEST, parse_dtc_list_response
from lib.modules import MODULES, NORMAL_11BITS, NORMAL_29BITS, Module


BUS_ORDER = ("c-can", "b-can", "can-ch")
BUS_PAIRS = {"c-can": "6/14", "b-can": "3/11", "can-ch": "12/13"}
REQUEST_HEX = "19 02 FF"
PCM_UNSUPPORTED_REASON = (
    "PCM 19 02 framing/padding has not been reviewed on the permanent "
    "dual-USBCANFD path"
)
DTC_BATCH_SUPPORTED_KEYS = frozenset(
    (
        "radar_acc",
        "rf_hub",
        "tcm",
        "shifter",
        "bcm_ccan",
        "cluster",
        "telematics",
        "ics_bcan",
        "uconnect_bcan",
        "climate_bcan",
        "emcm2_bcan",
        "abs_canch",
        "eps_canch",
        "half_canch",
        "orc_canch",
    )
)
JOB_SCHEMA_VERSION = 1
JOB_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
FINAL_JOB_STATES = frozenset(
    ("completed", "cancelled", "failed", "restoration_failed")
)


class BatchPolicyError(ValueError):
    """A requested batch violates the fixed scan policy."""


class VehicleGateError(RuntimeError):
    """Fresh passive evidence does not prove the required parked state."""


class BrokerGateError(RuntimeError):
    """The telemetry broker does not prove its active-drive path is idle."""


@dataclass(frozen=True)
class PlannedModule:
    module: Module
    supported: bool
    unsupported_reason: str | None = None

    def as_dict(self, sequence: int) -> dict[str, Any]:
        return {
            "sequence": sequence,
            "module_key": self.module.key,
            "module_name": self.module.name,
            "logical_bus": self.module.bus,
            "physical_pair": BUS_PAIRS[self.module.bus],
            "bitrate": self.module.bitrate,
            "addressing_mode": self.module.addressing_mode,
            "txid": f"{self.module.txid:X}",
            "rxid": f"{self.module.rxid:X}",
            "request_hex": REQUEST_HEX if self.supported else None,
            "execution_state": "planned" if self.supported else "unsupported",
            "unsupported_reason": self.unsupported_reason,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def select_modules(
    keys: Sequence[str], buses: Sequence[str] | None = None
) -> tuple[PlannedModule, ...]:
    """Return only immutable registry modules in stable bus/registry order."""

    unknown = [key for key in keys if key not in MODULES]
    if unknown:
        raise BatchPolicyError(f"unknown module(s): {', '.join(unknown)}")
    if len(keys) != len(set(keys)):
        raise BatchPolicyError("module keys must not be repeated")
    requested_buses = tuple(buses or BUS_ORDER)
    if any(bus not in BUS_ORDER for bus in requested_buses):
        raise BatchPolicyError("unsupported logical bus selection")
    if len(requested_buses) != len(set(requested_buses)):
        raise BatchPolicyError("logical buses must not be repeated")
    candidates: Iterable[Module]
    candidates = (MODULES[key] for key in keys) if keys else MODULES.values()
    bus_filter = set(requested_buses)
    selected = [module for module in candidates if module.bus in bus_filter]
    if not selected:
        raise BatchPolicyError("module/bus selection is empty")
    bus_rank = {bus: index for index, bus in enumerate(BUS_ORDER)}
    registry_rank = {key: index for index, key in enumerate(MODULES)}
    selected.sort(key=lambda item: (bus_rank[item.bus], registry_rank[item.key]))
    for module in selected:
        require_physical_module(module)
    return tuple(
        PlannedModule(
            module,
            supported=module.key in DTC_BATCH_SUPPORTED_KEYS,
            unsupported_reason=(
                None
                if module.key in DTC_BATCH_SUPPORTED_KEYS
                else (
                    PCM_UNSUPPORTED_REASON
                    if module.key == "pcm"
                    else "module is not in the reviewed DTC batch allowlist"
                )
            ),
        )
        for module in selected
    )


def require_physical_module(module: Module) -> None:
    """Reject functional or malformed addressing even if a registry edit adds it."""

    if module.addressing_mode == NORMAL_29BITS:
        tx_target = (module.txid >> 8) & 0xFF
        tx_source = module.txid & 0xFF
        rx_target = (module.rxid >> 8) & 0xFF
        rx_source = module.rxid & 0xFF
        if (
            (module.txid >> 16) != 0x18DA
            or (module.rxid >> 16) != 0x18DA
            or tx_source != 0xF1
            or rx_target != 0xF1
            or tx_target != rx_source
            or tx_target == 0xF1
        ):
            raise BatchPolicyError(
                f"{module.key} is not an exact 29-bit normal-fixed physical tester/ECU pair"
            )
        return
    if module.addressing_mode == NORMAL_11BITS:
        if module.txid == 0x7DF or module.rxid == 0x7DF:
            raise BatchPolicyError(
                f"{module.key} uses the 11-bit functional broadcast identifier"
            )
        return
    raise BatchPolicyError(f"{module.key} has unsupported diagnostic addressing")


def build_plan(
    modules: Sequence[PlannedModule], *, rate_hz: float
) -> dict[str, Any]:
    if not isinstance(rate_hz, (int, float)) or isinstance(rate_hz, bool):
        raise BatchPolicyError("request rate must be numeric")
    if not math.isfinite(float(rate_hz)) or not 0.1 <= float(rate_hz) <= 1.0:
        raise BatchPolicyError("request rate must be between 0.1 and 1 request/s")
    rows = [item.as_dict(index) for index, item in enumerate(modules, 1)]
    supported = [row for row in rows if row["execution_state"] == "planned"]
    return {
        "tool": "tools/dtc_batch.py",
        "mode": "offline_plan",
        "dry_run": True,
        "live_execution_requires_explicit_confirmations": True,
        "interaction_if_executed": (
            "active non-mutating physical UDS ReadDTCInformation"
        ),
        "request_hex": REQUEST_HEX,
        "request_bytes": list(READ_DTC_BY_STATUS_REQUEST),
        "execution_policy": "sequential_grouped_by_logical_bus",
        "max_request_rate_hz": float(rate_hz),
        "minimum_request_interval_s": 1.0 / float(rate_hz),
        "selected_module_count": len(rows),
        "request_count": len(supported),
        "unsupported_module_count": len(rows) - len(supported),
        "estimated_minimum_request_span_s": (
            max(0, len(supported) - 1) / float(rate_hz)
        ),
        "registered_modules_only": True,
        "physical_addressing_only": True,
        "clear_dtc_service_implemented": False,
        "diagnostic_session_control_implemented": False,
        "tester_present_implemented": False,
        "functional_broadcast_implemented": False,
        "arbitrary_payload_implemented": False,
        "pcm_supported": False,
        "modules": rows,
    }


def modules_by_bus(
    modules: Sequence[PlannedModule],
) -> tuple[tuple[str, tuple[PlannedModule, ...]], ...]:
    groups = []
    for bus in BUS_ORDER:
        members = tuple(
            item for item in modules if item.supported and item.module.bus == bus
        )
        if members:
            groups.append((bus, members))
    return tuple(groups)


def validate_broker_idle(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Require a live telemetry broker whose active-drive owner is idle."""

    if payload.get("service") != "van-telemetry":
        raise BrokerGateError("broker status did not identify van-telemetry")
    active = payload.get("active_drive")
    if not isinstance(active, Mapping):
        raise BrokerGateError("broker status has no active-drive state")
    if active.get("enabled") is not True:
        raise BrokerGateError("broker active-drive supervision is not enabled")
    if active.get("state") != "idle":
        raise BrokerGateError(
            f"broker active-drive state is {active.get('state')!r}, expected 'idle'"
        )
    if active.get("interface_mode") != "listen_only":
        raise BrokerGateError("broker active-drive interface is not listen-only")
    if active.get("restoration_failed") is not False:
        raise BrokerGateError("broker reports an active-drive restoration fault")
    return {
        "state": "idle",
        "interface_mode": "listen_only",
        "restoration_failed": False,
        "checked_at": utc_now(),
    }


def validate_initial_broker_vehicle_state(
    payload: Mapping[str, Any], *, maximum_age_ms: int = 5000
) -> dict[str, Any]:
    """Require the broker's independent initial ignition-on/engine-off view."""

    vehicle = payload.get("vehicle_state")
    if not isinstance(vehicle, Mapping):
        raise VehicleGateError("broker status has no vehicle state")
    age = vehicle.get("age_ms")
    if (
        not isinstance(age, (int, float))
        or isinstance(age, bool)
        or not math.isfinite(float(age))
        or float(age) < 0
        or float(age) > maximum_age_ms
    ):
        raise VehicleGateError("broker vehicle state is not fresh")
    if vehicle.get("state") != "ignition_on" or vehicle.get("running") is not False:
        raise VehicleGateError(
            "broker does not prove ignition ON with the engine OFF"
        )
    if vehicle.get("confidence") != "verified":
        raise VehicleGateError("broker vehicle state is not verified")
    if vehicle.get("basis") != "qualified_ccan_0x0fc_engine_speed":
        raise VehicleGateError("broker engine-off state lacks qualified 0x0FC evidence")
    return {
        "state": "ignition_on",
        "running": False,
        "age_ms": float(age),
        "basis": vehicle.get("basis"),
        "checked_at": utc_now(),
    }


def validate_vehicle_snapshot(
    snapshot: Any,
    *,
    minimum_rpm_samples: int = 3,
    maximum_engine_off_rpm: float = 50.0,
    maximum_stationary_speed_mph: float = 0.1,
) -> dict[str, Any]:
    """Require new C-CAN frames proving ignition on, engine off, and zero speed."""

    observations = getattr(snapshot, "observations", None)
    rpm_samples = getattr(snapshot, "rpm_samples", None)
    frame_count = getattr(snapshot, "frame_count", None)
    completed = getattr(snapshot, "completed_monotonic", None)
    if not isinstance(observations, tuple) or not isinstance(rpm_samples, tuple):
        raise VehicleGateError("fresh C-CAN snapshot has an invalid shape")
    if not isinstance(frame_count, int) or isinstance(frame_count, bool) or frame_count <= 0:
        raise VehicleGateError("fresh C-CAN snapshot contained no accepted frames")
    if not isinstance(completed, (int, float)) or isinstance(completed, bool):
        raise VehicleGateError("fresh C-CAN snapshot has no completion timestamp")
    by_metric = {
        getattr(item, "metric", None): item
        for item in observations
        if isinstance(getattr(item, "metric", None), str)
    }
    ignition = by_metric.get("vehicle.ignition_on")
    speed = by_metric.get("vehicle.speed")
    rpm = by_metric.get("engine.rpm")
    if ignition is None or getattr(ignition, "value", None) is not True:
        raise VehicleGateError("fresh 0x2EF ignition-on evidence is missing")
    if rpm is None:
        raise VehicleGateError("fresh 0x0FC engine-speed evidence is missing")
    if len(rpm_samples) < minimum_rpm_samples:
        raise VehicleGateError(
            f"only {len(rpm_samples)} fresh RPM samples were received; "
            f"need {minimum_rpm_samples}"
        )
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        for value in rpm_samples
    ):
        raise VehicleGateError("fresh RPM samples are malformed")
    maximum_rpm = max(float(value) for value in rpm_samples)
    if maximum_rpm > maximum_engine_off_rpm:
        raise VehicleGateError(
            f"engine is not off: fresh RPM reached {maximum_rpm:.1f}"
        )
    if speed is None:
        raise VehicleGateError("fresh 0x101 vehicle-speed evidence is missing")
    speed_value = getattr(speed, "value", None)
    if (
        not isinstance(speed_value, (int, float))
        or isinstance(speed_value, bool)
        or not math.isfinite(float(speed_value))
    ):
        raise VehicleGateError("fresh vehicle-speed value is malformed")
    if abs(float(speed_value)) > maximum_stationary_speed_mph:
        raise VehicleGateError(
            f"vehicle is not stationary: fresh speed is {float(speed_value):.3f} mph"
        )
    return {
        "ignition_on": True,
        "engine_off": True,
        "maximum_rpm": maximum_rpm,
        "rpm_sample_count": len(rpm_samples),
        "speed_mph": float(speed_value),
        "frame_count": frame_count,
        "snapshot_completed_monotonic": float(completed),
        "checked_at": utc_now(),
    }


def classify_response(
    response: bytes | None, status: Any
) -> tuple[dict[str, Any], bool]:
    """Classify one response to the sole permitted request.

    The boolean return value indicates whether a transport/shape fault should
    stop the current role after the result has been durably recorded.
    """

    if response is None:
        return (
            {
                "category": "timeout",
                "response_hex": None,
                "status": status,
                "negative_response": None,
                "parsed": None,
            },
            False,
        )
    raw = bytes(response)
    response_hex = raw.hex(" ").upper()
    if len(raw) >= 3 and raw[:2] == bytes.fromhex("7F 19"):
        return (
            {
                "category": "negative",
                "response_hex": response_hex,
                "status": status,
                "negative_response": {"service": "19", "nrc": f"{raw[2]:02X}"},
                "parsed": None,
            },
            False,
        )
    if len(raw) >= 2 and raw[:2] == bytes.fromhex("59 02"):
        try:
            availability_mask, records = parse_dtc_list_response(raw)
        except ValueError as exc:
            return (
                {
                    "category": "unexpected",
                    "response_hex": response_hex,
                    "status": status,
                    "negative_response": None,
                    "parsed": None,
                    "parse_error": f"{type(exc).__name__}: {exc}",
                },
                True,
            )
        return (
            {
                "category": "positive",
                "response_hex": response_hex,
                "status": status,
                "negative_response": None,
                "parsed": {
                    "status_availability_mask": f"{availability_mask:02X}",
                    "dtcs": [record.as_dict() for record in records],
                    "trailing_hex": None,
                },
            },
            False,
        )
    return (
        {
            "category": "unexpected",
            "response_hex": response_hex,
            "status": status,
            "negative_response": None,
            "parsed": None,
        },
        True,
    )


def inventory_compatible_report(
    *,
    module: Module,
    channel: str,
    topology_fingerprint: str,
    physical_pair: str,
    started_at: str,
    completed_at: str,
    result: Mapping[str, Any],
    elapsed_s: float,
    job_id: str,
    restored_passive: bool,
    max_request_rate_hz: float,
    fatal_error: str | None = None,
) -> dict[str, Any]:
    """Build one atomic report accepted by the existing offline importer."""

    effective_fatal_error = fatal_error
    if not restored_passive and effective_fatal_error is None:
        effective_fatal_error = "passive restoration was not verified"
    observed_category = result.get("category")
    authoritative_for_history = (
        effective_fatal_error is None and restored_passive
    )
    normalized_result = {
        "label": "dtcs_by_status",
        "request_hex": REQUEST_HEX,
        "response_hex": result.get("response_hex"),
        # Preserve the wire classification separately, but never let an
        # otherwise positive response become an authoritative zero/presence
        # observation when transport cleanup or passive restoration failed.
        "category": (
            observed_category if authoritative_for_history else "inventory_error"
        ),
        "observed_category": observed_category,
        "authoritative_for_history": authoritative_for_history,
        "status": result.get("status"),
        "negative_response": result.get("negative_response"),
        "parsed": result.get("parsed"),
        "elapsed_s": round(float(elapsed_s), 3),
    }
    if result.get("parse_error"):
        normalized_result["parse_error"] = result["parse_error"]
    return {
        # This is the schema name consumed by tools/dtc_scan.py.  Producer is
        # separate so provenance remains explicit without widening the offline
        # importer to arbitrary report formats.
        "tool": "tools/dtc_inventory.py",
        "producer": "tools/dtc_batch.py",
        "producer_job_id": job_id,
        "interaction": "active non-mutating UDS ReadDTCInformation",
        "clear_service_implemented": False,
        "supported_dtc_inventory_requested": False,
        "diagnostic_session_control_sent": False,
        "tester_present_sent": False,
        "functional_broadcast_sent": False,
        "arbitrary_payload_supported": False,
        "ecu_session": "inherited/unknown",
        "started_at": started_at,
        "completed_at": completed_at,
        "module": {
            "key": module.key,
            "name": module.name,
            "bus": module.bus,
            "channel": channel,
            "route_source": "usb_serial_and_dev_id",
            "topology_fingerprint": topology_fingerprint,
            "expected_physical_pair": physical_pair,
            "bitrate": module.bitrate,
            "addressing_mode": module.addressing_mode,
            "txid": f"{module.txid:X}",
            "rxid": f"{module.rxid:X}",
        },
        "physical_pair": physical_pair,
        "conditions": "Park asserted; ignition ON; engine OFF; fresh speed zero",
        "parked_asserted": True,
        "park_gear_asserted": True,
        "same_boot_inhibits_checked": True,
        "active_drive_idle_checked": True,
        "fresh_state_checked_before_tx": True,
        "max_request_rate_hz": float(max_request_rate_hz),
        "request_attempts": 1 if result.get("request_attempted") else 0,
        "responses_received": 1 if result.get("response_hex") else 0,
        "interrupted": False,
        "partial": effective_fatal_error is not None or not restored_passive,
        "fatal_error": effective_fatal_error,
        "restored_passive": restored_passive,
        "results": [normalized_result],
    }


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class JobStore:
    """One atomic progress record plus a separate cooperative cancel flag."""

    def __init__(self, root: Path, job_id: str):
        if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
            raise BatchPolicyError("unsafe DTC batch job id")
        self.root = Path(root)
        self.job_id = job_id
        self.directory = self.root / job_id
        self.record_path = self.directory / "job.json"
        self.cancel_path = self.directory / "cancel.request"

    def create(self, plan: Mapping[str, Any], *, command: Sequence[str]) -> dict[str, Any]:
        if self.directory.exists():
            raise FileExistsError(f"DTC batch job already exists: {self.job_id}")
        created = utc_now()
        modules = []
        for row in plan["modules"]:
            modules.append(
                {
                    "module_key": row["module_key"],
                    "logical_bus": row["logical_bus"],
                    "state": row["execution_state"],
                    "reason": row["unsupported_reason"],
                    "report": None,
                    "outcome": None,
                    "dtc_count": None,
                }
            )
        record = {
            "schema_version": JOB_SCHEMA_VERSION,
            "job_id": self.job_id,
            "tool": "tools/dtc_batch.py",
            "state": "created",
            "created_at": created,
            "updated_at": created,
            "started_at": None,
            "completed_at": None,
            "current_bus": None,
            "current_module": None,
            "cancel_requested": False,
            "failure": None,
            "restoration_failure": None,
            "command": list(command),
            "policy": {
                "request_hex": REQUEST_HEX,
                "registered_modules_only": True,
                "maximum_request_rate_hz": plan["max_request_rate_hz"],
                "grouped_role_windows": True,
                "pcm_supported": False,
            },
            "progress": {
                "selected": len(modules),
                "requestable": plan["request_count"],
                "queried": 0,
                "imported": 0,
                "unavailable": 0,
                "unsupported": plan["unsupported_module_count"],
            },
            "modules": modules,
            "reports": [],
        }
        atomic_json(self.record_path, record)
        return record

    def read(self) -> dict[str, Any]:
        payload = json.loads(self.record_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("job_id") != self.job_id:
            raise RuntimeError("DTC batch job record is malformed")
        # Cancellation is a separate atomically-created control file so a UI
        # request never races a worker read/modify/write of the progress ledger.
        # Derive the public field from that authority immediately; subsequent
        # worker writes will naturally persist the true value as well.
        payload["cancel_requested"] = bool(payload.get("cancel_requested")) or (
            self.cancel_path.is_file()
        )
        return payload

    def write(self, payload: Mapping[str, Any]) -> None:
        record = dict(payload)
        record["cancel_requested"] = bool(record.get("cancel_requested")) or (
            self.cancel_path.is_file()
        )
        record["updated_at"] = utc_now()
        atomic_json(self.record_path, record)

    def update(self, **changes: Any) -> dict[str, Any]:
        record = self.read()
        record.update(changes)
        self.write(record)
        return record

    def update_module(self, module_key: str, **changes: Any) -> dict[str, Any]:
        record = self.read()
        matched = False
        for row in record["modules"]:
            if row["module_key"] == module_key:
                row.update(changes)
                matched = True
                break
        if not matched:
            raise KeyError(module_key)
        self.write(record)
        return record

    def request_cancel(self, *, reason: str = "operator_request") -> bool:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.cancel_path.exists():
            return False
        descriptor, temporary = tempfile.mkstemp(
            prefix=".cancel.", suffix=".tmp", dir=self.directory
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    {"requested_at": utc_now(), "reason": str(reason)},
                    handle,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.cancel_path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        return True

    def cancellation_requested(self) -> bool:
        return self.cancel_path.is_file()
