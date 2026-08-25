#!/usr/bin/env python3
"""Dry-run-first, fixed-payload DTC batch worker for the installed CAN roles.

Default operation is an offline plan.  Live execution is restricted to one
physical ``19 02 FF`` request per selected, registered module.  It cannot send
session control, tester present, DTC clear, functional addressing, or an
operator-supplied payload.  PCM remains explicitly unsupported until its
padding/framing is separately reviewed.

The live worker requires an operator Park assertion plus independently fresh
C-CAN evidence for ignition ON, engine OFF, and zero speed.  It resolves every
role by exact USB serial/dev_id, takes the logical-role lock before the current
channel lock, revalidates all gates immediately before every request, and
restores/verifies listen-only mode before importing any result from that role.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any, Sequence

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib import can_operation_state, can_runtime_route, canbus, diagnostic_safety, uds
from lib.dtc import DtcHistory, READ_DTC_BY_STATUS_REQUEST, write_cache
from lib.dtc_batch import (
    BUS_PAIRS,
    BatchPolicyError,
    BrokerGateError,
    FINAL_JOB_STATES,
    JobStore,
    VehicleGateError,
    atomic_json,
    build_plan,
    classify_response,
    inventory_compatible_report,
    modules_by_bus,
    select_modules,
    utc_now,
    validate_broker_idle,
    validate_initial_broker_vehicle_state,
    validate_vehicle_snapshot,
)
from lib.dtc_web import DEFAULT_JOB_ROOT
from lib.modules import bind_channel
from projects.vehicle_data import ccan_powertrain
from projects.vehicle_data.api import TelemetryClient
from projects.vehicle_data.broker import DEFAULT_SOCKET
from tools.dtc_scan import DEFAULT_CACHE, DEFAULT_DB, load_inventory


DEFAULT_REPORT_ROOT = REPO / "tmp" / "inventories"
MAX_TIMEOUT_S = 5.0
STATE_SNAPSHOT_TIMEOUT_S = 0.75
STATE_RPM_SAMPLES = 3
JOB_LOCK_NAME = "dtc-batch-worker"


class BatchCancelled(RuntimeError):
    pass


class RestorationFailure(RuntimeError):
    pass


class FatalBatchError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("modules", nargs="*", help="registered module keys (default: all)")
    parser.add_argument(
        "--bus", action="append", choices=("c-can", "b-can", "can-ch")
    )
    parser.add_argument("--rate", type=float, default=1.0, help="0.1..1 request/s")
    parser.add_argument("--timeout", type=float, default=1.0, help="response timeout, <=5 s")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--execute", action="store_true", help="run the guarded live batch")
    parser.add_argument(
        "--confirm-parked", action="store_true", help="assert the vehicle is parked"
    )
    parser.add_argument(
        "--confirm-park-gear",
        action="store_true",
        help="assert the transmission selector is in Park",
    )
    parser.add_argument(
        "--confirm-ignition-on-engine-off",
        action="store_true",
        help="assert ignition ON and engine OFF; fresh bus evidence is also required",
    )
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--cache-out", default=DEFAULT_CACHE)
    parser.add_argument("--job-root", default=str(DEFAULT_JOB_ROOT))
    parser.add_argument("--report-root", default=str(DEFAULT_REPORT_ROOT))
    parser.add_argument("--job-id", help="safe caller-selected job id for later UI supervision")
    control = parser.add_mutually_exclusive_group()
    control.add_argument("--status", metavar="JOB_ID", help="read one saved job; no CAN I/O")
    control.add_argument("--cancel", metavar="JOB_ID", help="request cooperative cancellation")
    return parser


def _job_id() -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"dtc-{stamp}-{uuid.uuid4().hex[:8]}"


def _status(job_root: Path, job_id: str, *, as_json: bool) -> int:
    try:
        record = JobStore(job_root, job_id).read()
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        progress = record.get("progress", {})
        print(
            f"{record['job_id']} state={record['state']} "
            f"queried={progress.get('queried', 0)}/"
            f"{progress.get('requestable', 0)} imported={progress.get('imported', 0)}"
        )
        if record.get("current_bus") or record.get("current_module"):
            print(
                f"current bus={record.get('current_bus')} "
                f"module={record.get('current_module')}"
            )
        if record.get("failure"):
            print(f"failure: {record['failure']}")
    return 0


def _cancel(job_root: Path, job_id: str, *, as_json: bool) -> int:
    store = JobStore(job_root, job_id)
    try:
        record = store.read()
        if record.get("state") in FINAL_JOB_STATES:
            raise RuntimeError(f"job is already {record.get('state')}")
        created = store.request_cancel()
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    payload = {"job_id": job_id, "cancel_requested": True, "created": created}
    print(json.dumps(payload, indent=2, sort_keys=True) if as_json else f"cancel requested: {job_id}")
    return 0


def _exact_interface_error(channel: str, bitrate: int, *, armed: bool) -> str | None:
    state = canbus.interface_state(channel)
    if not isinstance(state, canbus.InterfaceState) or state.channel != channel:
        return "SocketCAN interface identity/state is invalid"
    if not state.present or not state.up:
        return f"{channel} is missing or down"
    if state.bitrate != bitrate:
        return f"{channel} bitrate is {state.bitrate}, expected {bitrate}"
    if state.fd_enabled is not False:
        return f"{channel} does not prove classical CAN with FD off"
    if state.one_shot is not False:
        return f"{channel} does not prove one-shot retransmission is off"
    expected_listen_only = not armed
    if state.listen_only is not expected_listen_only:
        expected = "armed" if armed else "listen-only"
        return f"{channel} is not in the expected {expected} mode"
    if state.controller_state != "ERROR-ACTIVE":
        return f"{channel} controller is {state.controller_state!r}, expected ERROR-ACTIVE"
    if state.restart_ms != 0:
        return f"{channel} restart-ms is {state.restart_ms}, expected 0"
    return None


def _topology_error(channel: str, bus: str, pair: str) -> str | None:
    topology = can_operation_state.load_topology(channel)
    if not topology.usable:
        return f"same-boot topology for {bus}/{channel} is unusable: {topology.reason}"
    if topology.bus != bus or topology.pair != pair:
        return (
            f"same-boot topology for {channel} is {topology.bus} pins "
            f"{topology.pair!r}, expected {bus} pins {pair}"
        )
    return None


def _inhibit_error(channel: str) -> str | None:
    inhibits = can_operation_state.active_inhibits(channel)
    if not inhibits:
        return None
    names = ",".join(str(item.get("name", "invalid")) for item in inhibits)
    return f"active diagnostic traffic is inhibited by {names}"


def _exact_sudo_link_permission_errors(
    channel: str,
    bitrate: int,
    *,
    run=subprocess.run,
    ip_path: str | None = None,
) -> tuple[str, ...]:
    """Prove sudo policy for every literal arm/restore link command.

    ``sudo -n true`` is deliberately insufficient: a least-privilege host may
    permit these exact ``ip`` commands without permitting an arbitrary root
    command. This check only lists authorization; it does not mutate a link.
    """

    resolved_ip = ip_path or shutil.which("ip") or "/usr/sbin/ip"
    command_tails = (
        ("link", "set", channel, "down"),
        (
            "link", "set", channel, "up", "type", "can", "bitrate",
            str(bitrate), "fd", "off", "listen-only", "off", "one-shot", "off",
            "restart-ms", "0",
        ),
        (
            "link", "set", channel, "up", "type", "can", "bitrate",
            str(bitrate), "fd", "off", "listen-only", "on", "one-shot", "off",
            "restart-ms", "0",
        ),
    )
    errors = []
    for tail in command_tails:
        argv = ["sudo", "-n", "-l", "--", resolved_ip, *tail]
        try:
            result = run(argv, capture_output=True, text=True, check=False)
        except OSError as exc:
            errors.append(
                f"cannot inspect noninteractive sudo policy for {channel}: "
                f"{type(exc).__name__}: {exc}"
            )
            break
        if result.returncode != 0:
            errors.append(
                "noninteractive sudo policy does not authorize exact link "
                f"command: {resolved_ip} {' '.join(tail)}"
            )
    return tuple(errors)


@dataclass
class SafetyGate:
    client: TelemetryClient
    manager: Any
    ccan_channel: str
    ccan_pair: str
    ccan_role_owner: Any
    ccan_route: Any | None = None
    target_owner: Any | None = None

    def broker_status(self, *, require_initial_vehicle_state: bool = False) -> dict[str, Any]:
        status_code, payload = self.client.request("GET", "/v1/status")
        if status_code != 200:
            raise BrokerGateError(f"broker status returned HTTP {status_code}")
        checked = validate_broker_idle(payload)
        if require_initial_vehicle_state:
            checked["vehicle_state"] = validate_initial_broker_vehicle_state(payload)
        return checked

    def _revalidate_ccan(self, *, armed: bool) -> None:
        if self.target_owner is not None and self.target_owner.route.role == "c-can":
            can_runtime_route.revalidate_module_route(
                self.target_owner.route, manager=self.manager
            )
        elif self.ccan_route is not None:
            can_runtime_route.revalidate_module_route(
                self.ccan_route, manager=self.manager
            )
        else:
            self.ccan_role_owner.revalidate()
        interface_error = _exact_interface_error(
            self.ccan_channel, 500000, armed=armed
        )
        if interface_error:
            raise VehicleGateError(interface_error)
        topology_error = _topology_error(self.ccan_channel, "c-can", self.ccan_pair)
        if topology_error:
            raise VehicleGateError(topology_error)
        inhibit_error = _inhibit_error(self.ccan_channel)
        if inhibit_error:
            raise VehicleGateError(inhibit_error)

    def fresh_vehicle_state(self, *, ccan_armed: bool) -> dict[str, Any]:
        self._revalidate_ccan(armed=ccan_armed)
        snapshot = ccan_powertrain.read_broadcast_snapshot(
            self.ccan_channel,
            timeout=STATE_SNAPSHOT_TIMEOUT_S,
            required_rpm_samples=STATE_RPM_SAMPLES,
        )
        return validate_vehicle_snapshot(
            snapshot, minimum_rpm_samples=STATE_RPM_SAMPLES
        )

    def target_ready(self) -> None:
        if self.target_owner is None:
            raise RuntimeError("target ownership is not established")
        route = self.target_owner.route
        can_runtime_route.revalidate_module_route(route, manager=self.manager)
        interface_error = _exact_interface_error(
            route.channel, route.module.bitrate, armed=True
        )
        if interface_error:
            raise FatalBatchError(interface_error)
        topology_error = _topology_error(route.channel, route.role, route.pair)
        if topology_error:
            raise FatalBatchError(topology_error)
        inhibit_error = _inhibit_error(route.channel)
        if inhibit_error:
            raise FatalBatchError(inhibit_error)

    def before_tx(self, store: JobStore) -> dict[str, Any]:
        if store.cancellation_requested():
            raise BatchCancelled("cooperative cancellation requested")
        self.target_ready()
        broker = self.broker_status()
        vehicle = self.fresh_vehicle_state(
            ccan_armed=self.target_owner.route.role == "c-can"
        )
        # The independent C-CAN snapshot can consume most of its 750 ms bound.
        # Recheck cancellation and the target role after it so USB identity,
        # interface state, topology, and inhibits are the final gates before TX.
        if store.cancellation_requested():
            raise BatchCancelled("cooperative cancellation requested")
        self.target_ready()
        return {"broker": broker, "vehicle": vehicle}


def _report_path(report_root: Path, module_key: str, job_id: str) -> Path:
    return report_root / module_key / f"dtcs_batch_{job_id}_{module_key}.json"


def _import_reports(
    paths: Sequence[Path], *, database: Path, cache_path: Path
) -> tuple[int, dict[str, Any]]:
    loaded = [load_inventory(str(path)) for path in paths]
    scans = [item[0] for item in loaded]
    with DtcHistory(database) as history:
        results = history.record_scans(
            sorted(scans, key=lambda item: (item.completed_at, item.module_key))
        )
        summary = history.dashboard_summary(compact=True, per_group_limit=25)
    write_cache(cache_path, summary)
    return sum(1 for result in results if result["inserted"]), summary


class BatchRunner:
    def __init__(
        self,
        *,
        planned,
        rate_hz: float,
        timeout_s: float,
        store: JobStore,
        report_root: Path,
        database: Path,
        cache_path: Path,
        socket_path: str,
        monotonic=time.monotonic,
        sleep=time.sleep,
    ):
        self.planned = tuple(planned)
        self.rate_hz = float(rate_hz)
        self.timeout_s = float(timeout_s)
        self.store = store
        self.report_root = Path(report_root)
        self.database = Path(database)
        self.cache_path = Path(cache_path)
        self.client = TelemetryClient(socket_path, timeout=2.0)
        self.manager = None
        self.monotonic = monotonic
        self.sleep = sleep
        self.last_request_at: float | None = None
        self.cancelled = False

    def _wait_rate_limit(self) -> None:
        if self.last_request_at is None:
            return
        remaining = (1.0 / self.rate_hz) - (self.monotonic() - self.last_request_at)
        if remaining > 0:
            self.sleep(remaining)

    def _progress(self, *, queried: int = 0, imported: int = 0, unavailable: int = 0) -> None:
        record = self.store.read()
        progress = dict(record["progress"])
        progress["queried"] += queried
        progress["imported"] += imported
        progress["unavailable"] += unavailable
        record["progress"] = progress
        self.store.write(record)

    def _initial_gate(self) -> None:
        code, payload = self.client.request("GET", "/v1/status")
        if code != 200:
            raise BrokerGateError(f"broker status returned HTTP {code}")
        validate_broker_idle(payload)
        validate_initial_broker_vehicle_state(payload)

    def _prearm_errors(self, gate: SafetyGate, *, target_bus: str) -> tuple[str, ...]:
        errors = []
        if self.store.cancellation_requested():
            raise BatchCancelled("cooperative cancellation requested")
        try:
            gate.broker_status()
            gate.fresh_vehicle_state(ccan_armed=False)
        except Exception as exc:
            errors.append(f"pre-arm vehicle/broker gate failed: {type(exc).__name__}: {exc}")
        try:
            topology = self.manager.topology()
            resolution = topology.resolution(target_bus)
            channel = resolution.require_channel()
            errors.extend(
                _exact_sudo_link_permission_errors(
                    channel,
                    int(resolution.spec.bitrate),
                )
            )
            target_error = _topology_error(channel, target_bus, BUS_PAIRS[target_bus])
            if target_error:
                errors.append(target_error)
        except Exception as exc:
            errors.append(f"target role pre-arm validation failed: {type(exc).__name__}: {exc}")
        return tuple(errors)

    def _query_module(self, module, gate: SafetyGate) -> tuple[dict[str, Any], float, bool]:
        bound = bind_channel(module, gate.target_owner.route.channel)
        sock = None
        request_attempted = False
        started = self.monotonic()
        try:
            sock = uds.open_module_socket(bound, timeout=self.timeout_s)
            uds.drain(sock)
            self._wait_rate_limit()
            # The state/identity checks intentionally follow socket setup and
            # rate waiting so they are the last operations before transmission.
            gate.before_tx(self.store)
            # The only transport call in this worker.  The payload is a module
            # constant, never an argument, config value, or job-record field.
            self.last_request_at = self.monotonic()
            request_attempted = True
            response, status = uds.request(
                sock,
                READ_DTC_BY_STATUS_REQUEST,
                timeout=self.timeout_s,
                retries=0,
            )
            result, fatal = classify_response(response, status)
        except BatchCancelled:
            raise
        except Exception as exc:
            result = {
                "category": "transport_error",
                "response_hex": None,
                "status": f"{type(exc).__name__}: {exc}",
                "negative_response": None,
                "parsed": None,
            }
            fatal = True
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception as exc:
                    if "result" not in locals():
                        result = {
                            "category": "transport_error",
                            "response_hex": None,
                            "status": f"socket close failed: {type(exc).__name__}: {exc}",
                            "negative_response": None,
                            "parsed": None,
                        }
                    else:
                        result = dict(result)
                        result["transport_cleanup_error"] = (
                            f"{type(exc).__name__}: {exc}"
                        )
                    fatal = True
        result["request_attempted"] = request_attempted
        return result, self.monotonic() - started, fatal

    def _finalize_role_reports(
        self,
        *,
        bus: str,
        pending: Sequence[dict[str, Any]],
        target_owner: Any,
        restored: bool,
        role_error: BaseException | None,
    ) -> list[Path]:
        finalized_paths: list[Path] = []
        for item in pending:
            module = item["module"]
            if not restored:
                fatal_text = f"passive restoration for {bus} was not verified"
            elif item["fatal"]:
                fatal_text = (
                    str(role_error)
                    if role_error is not None
                    else "module transport/response-shape fault"
                )
            else:
                fatal_text = None
            report = inventory_compatible_report(
                module=bind_channel(module, target_owner.route.channel),
                channel=target_owner.route.channel,
                topology_fingerprint=target_owner.route.topology_fingerprint,
                physical_pair=target_owner.route.pair,
                started_at=item["started_at"],
                completed_at=item["completed_at"],
                result=item["result"],
                elapsed_s=item["elapsed_s"],
                job_id=self.store.job_id,
                restored_passive=restored,
                max_request_rate_hz=self.rate_hz,
                fatal_error=fatal_text,
            )
            path = _report_path(self.report_root, module.key, self.store.job_id)
            atomic_json(path, report)
            finalized_paths.append(path)
            self.store.update_module(
                module.key,
                state=("reported" if restored else "restore_unverified"),
                report=str(path),
            )
            record = self.store.read()
            record["reports"].append(str(path))
            self.store.write(record)
        return finalized_paths

    @staticmethod
    def _restoration_failure_detail(
        bus: str,
        *,
        inhibit_error: BaseException | None = None,
        report_error: BaseException | None = None,
    ) -> str:
        detail = f"could not verify passive restoration for {bus}"
        if inhibit_error is None:
            detail += "; same-boot batch inhibit latched"
        else:
            detail += (
                "; batch inhibit latch failed: "
                f"{type(inhibit_error).__name__}: {inhibit_error}"
            )
        if report_error is not None:
            detail += (
                "; restoration evidence persistence also failed: "
                f"{type(report_error).__name__}: {report_error}"
            )
        return detail

    def _run_role(self, bus: str, members) -> None:
        ccan_observer = None
        target_owner = None
        gate = None
        pending: list[dict[str, Any]] = []
        role_error: BaseException | None = None
        restored = True
        operation_termination = None
        finalized_paths: list[Path] = []

        # Arming and restoration are capability-transfer boundaries: a signal
        # must never unwind either one before the ownership object is available
        # to this caller.  The outer per-role guard therefore records signals
        # without raising during setup/cleanup.  Once ownership is established,
        # a fresh inner guard makes active work interruptible.  Each role gets
        # new guards so cleanup of one bus cannot suppress termination on the
        # next bus.
        with diagnostic_safety.interrupt_on_termination() as role_termination:
            role_termination.begin_cleanup()
            try:
                if bus != "c-can":
                    ccan_observer = can_runtime_route.acquire_passive_bus_route(
                        "c-can", asserted_pair=BUS_PAIRS["c-can"], manager=self.manager
                    )
                    ccan_channel = ccan_observer.route.channel
                    ccan_pair = ccan_observer.route.pair
                    ccan_role_owner = ccan_observer
                else:
                    ccan_route = can_runtime_route.resolve_module_route(
                        members[0].module, manager=self.manager
                    )
                    ccan_channel = ccan_route.channel
                    ccan_pair = ccan_route.pair
                    ccan_role_owner = None
                if role_termination.received_signal is not None:
                    raise BatchCancelled("termination signal received during role setup")
                gate = SafetyGate(
                    client=self.client,
                    manager=self.manager,
                    ccan_channel=ccan_channel,
                    ccan_pair=ccan_pair,
                    ccan_role_owner=ccan_role_owner,
                    ccan_route=ccan_route if bus == "c-can" else None,
                )
                target_owner = can_runtime_route.acquire_armed_module_route(
                    members[0].module,
                    asserted_pair=BUS_PAIRS[bus],
                    prearm_check=lambda: self._prearm_errors(gate, target_bus=bus),
                    manager=self.manager,
                )
                gate.target_owner = target_owner
                if role_termination.received_signal is not None:
                    raise BatchCancelled("termination signal received during role arming")
                self.store.update(current_bus=bus)

                with diagnostic_safety.interrupt_on_termination() as operation_termination:
                    try:
                        if role_termination.received_signal is not None:
                            raise BatchCancelled(
                                "termination signal received before role operation"
                            )
                        for item in members:
                            if self.store.cancellation_requested():
                                raise BatchCancelled(
                                    "cooperative cancellation requested"
                                )
                            module = item.module
                            self.store.update(current_module=module.key)
                            self.store.update_module(
                                module.key, state="querying", reason=None
                            )
                            started_at = utc_now()
                            result, elapsed_s, fatal = self._query_module(module, gate)
                            completed_at = utc_now()
                            if not result.get("request_attempted"):
                                self.store.update_module(
                                    module.key,
                                    state="failed_before_tx",
                                    outcome=None,
                                    reason=str(
                                        result.get("status") or "pre-transmit failure"
                                    ),
                                )
                                raise FatalBatchError(
                                    f"{module.key} failed before the fixed request was attempted"
                                )
                            pending.append(
                                {
                                    "module": module,
                                    "started_at": started_at,
                                    "completed_at": completed_at,
                                    "result": result,
                                    "elapsed_s": elapsed_s,
                                    "fatal": fatal,
                                }
                            )
                            self.store.update_module(
                                module.key,
                                state="queried_pending_restore",
                                outcome=result["category"],
                            )
                            self._progress(
                                queried=1,
                                unavailable=(
                                    0
                                    if result["category"] == "positive" and not fatal
                                    else 1
                                ),
                            )
                            if fatal:
                                raise FatalBatchError(
                                    f"{module.key} returned a transport or response-shape fault"
                                )
                    except KeyboardInterrupt:
                        role_error = BatchCancelled("termination signal received")
                    except BaseException as exc:
                        role_error = exc
                    finally:
                        operation_termination.begin_cleanup()
            except KeyboardInterrupt:
                role_error = BatchCancelled("termination signal received")
            except BaseException as exc:
                role_error = exc
            finally:
                # ``role_termination`` has been non-raising since before setup,
                # so the first as well as repeated signals cannot cut through
                # exact passive restoration or observer-lock release.
                if target_owner is not None:
                    try:
                        restored = bool(target_owner.release())
                    except Exception:
                        restored = False
                if ccan_observer is not None:
                    try:
                        ccan_observer.release()
                    except Exception as exc:
                        if role_error is None:
                            role_error = FatalBatchError(
                                "C-CAN observer release failed: "
                                f"{type(exc).__name__}: {exc}"
                            )

            restoration_problem = not restored or isinstance(
                role_error, canbus.PassiveRestoreError
            )
            inhibit_error = None
            if restoration_problem:
                try:
                    can_operation_state.begin_inhibit(
                        "dtc-batch-restoration-failed",
                        channel="*",
                        reason=(
                            f"DTC batch could not verify {bus} passive restoration; "
                            "inspect every permanent CAN role before manually clearing inhibits"
                        ),
                    )
                except Exception as exc:
                    inhibit_error = exc
            try:
                finalized_paths = self._finalize_role_reports(
                    bus=bus,
                    pending=pending,
                    target_owner=target_owner,
                    restored=restored,
                    role_error=role_error,
                )
            except Exception as exc:
                if restoration_problem:
                    raise RestorationFailure(
                        self._restoration_failure_detail(
                            bus,
                            inhibit_error=inhibit_error,
                            report_error=exc,
                        )
                    ) from exc
                raise
            if restoration_problem:
                raise RestorationFailure(
                    self._restoration_failure_detail(
                        bus, inhibit_error=inhibit_error
                    )
                )

        if role_error is None and (
            role_termination.received_signal is not None
            or (
                operation_termination is not None
                and operation_termination.received_signal is not None
            )
        ):
            role_error = BatchCancelled("termination signal received during role cleanup")
        if finalized_paths:
            inserted, _summary = _import_reports(
                finalized_paths, database=self.database, cache_path=self.cache_path
            )
            for item, path in zip(pending, finalized_paths):
                module = item["module"]
                parsed = item["result"].get("parsed") or {}
                dtcs = parsed.get("dtcs")
                authoritative = (
                    not item["fatal"]
                    and item["result"].get("category") == "positive"
                )
                self.store.update_module(
                    module.key,
                    state="imported",
                    report=str(path),
                    outcome=(
                        "inventory_error"
                        if item["fatal"]
                        else item["result"]["category"]
                    ),
                    dtc_count=(
                        len(dtcs)
                        if authoritative and isinstance(dtcs, list)
                        else None
                    ),
                )
            self._progress(imported=inserted)
        if role_error is not None:
            raise role_error

    def run(self, termination) -> dict[str, Any]:
        self._initial_gate()
        from lib.vehicle_can_roles import InstalledCanRoleResolver

        self.manager = InstalledCanRoleResolver()
        self.store.update(state="running", started_at=utc_now(), failure=None)
        try:
            for bus, members in modules_by_bus(self.planned):
                if termination.received_signal is not None:
                    raise BatchCancelled("termination signal received between roles")
                if self.store.cancellation_requested():
                    raise BatchCancelled("cooperative cancellation requested")
                self._run_role(bus, members)
        except BatchCancelled as exc:
            self.cancelled = True
            return self.store.update(
                state="cancelled",
                cancel_requested=True,
                failure=str(exc),
                current_bus=None,
                current_module=None,
                completed_at=utc_now(),
            )
        except RestorationFailure as exc:
            return self.store.update(
                state="restoration_failed",
                restoration_failure=str(exc),
                failure=str(exc),
                current_bus=None,
                current_module=None,
                completed_at=utc_now(),
            )
        except Exception as exc:
            return self.store.update(
                state="failed",
                failure=f"{type(exc).__name__}: {exc}",
                current_bus=None,
                current_module=None,
                completed_at=utc_now(),
            )
        return self.store.update(
            state="completed",
            current_bus=None,
            current_module=None,
            completed_at=utc_now(),
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    job_root = Path(args.job_root)
    if args.status:
        return _status(job_root, args.status, as_json=args.json)
    if args.cancel:
        return _cancel(job_root, args.cancel, as_json=args.json)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= MAX_TIMEOUT_S:
        print(f"ERROR: --timeout must be finite, >0, and <= {MAX_TIMEOUT_S:g}", file=sys.stderr)
        return 2
    try:
        planned = select_modules(args.modules, args.bus)
        plan = build_plan(planned, rate_hz=args.rate)
    except (BatchPolicyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not args.execute:
        if args.json:
            print(json.dumps(plan, indent=2, sort_keys=True))
        else:
            print("OFFLINE DTC BATCH PLAN — no CAN socket opened and nothing transmitted")
            print(
                f"{plan['request_count']} fixed physical {plan['request_hex']} request(s), "
                f"grouped by role, <= {plan['max_request_rate_hz']:g}/s"
            )
            print("No 10, 3E, 14, functional broadcast, or arbitrary payload path exists.")
            for row in plan["modules"]:
                request = row["request_hex"] or "UNSUPPORTED"
                print(
                    f"{row['sequence']:>2}. {row['module_key']:<16} "
                    f"{row['logical_bus']:<6} pins {row['physical_pair']:<5} {request}"
                )
                if row["unsupported_reason"]:
                    print(f"    {row['unsupported_reason']}")
        return 0
    if not (
        args.confirm_parked
        and args.confirm_park_gear
        and args.confirm_ignition_on_engine_off
    ):
        print(
            "ERROR: --execute requires --confirm-parked, --confirm-park-gear, "
            "and --confirm-ignition-on-engine-off",
            file=sys.stderr,
        )
        return 2
    if plan["request_count"] == 0:
        print(
            "ERROR: the selected modules contain no currently supported DTC request",
            file=sys.stderr,
        )
        return 2
    job_id = args.job_id or _job_id()
    store = JobStore(job_root, job_id)
    try:
        store.create(plan, command=list(argv if argv is not None else sys.argv[1:]))
    except (OSError, ValueError) as exc:
        print(f"ERROR: could not create job: {exc}", file=sys.stderr)
        return 2
    print(f"DTC batch job: {job_id}", flush=True)
    campaign_lock = None
    record = None
    try:
        campaign_lock = diagnostic_safety.acquire_channel_lock(JOB_LOCK_NAME)
        runner = BatchRunner(
            planned=planned,
            rate_hz=args.rate,
            timeout_s=args.timeout,
            store=store,
            report_root=Path(args.report_root),
            database=Path(args.db),
            cache_path=Path(args.cache_out),
            socket_path=args.socket,
        )
        with diagnostic_safety.interrupt_on_termination() as termination:
            record = runner.run(termination)
    except KeyboardInterrupt:
        record = store.update(
            state="cancelled",
            cancel_requested=True,
            failure="termination signal received before a role was armed",
            completed_at=utc_now(),
        )
    except Exception as exc:
        record = store.update(
            state="failed", failure=f"{type(exc).__name__}: {exc}", completed_at=utc_now()
        )
    finally:
        diagnostic_safety.release_channel_lock(campaign_lock)
    if args.json:
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        print(
            f"job {job_id}: {record['state']} "
            f"queried={record['progress']['queried']}/"
            f"{record['progress']['requestable']} imported={record['progress']['imported']}"
        )
        if record.get("failure"):
            print(f"failure: {record['failure']}", file=sys.stderr)
    return 0 if record["state"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
