#!/usr/bin/env python3
"""Allowlisted vehicle telemetry broker and passive collection daemon."""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib import can_operation_state, diagnostic_safety
from projects.vehicle_data.metrics import METRICS, MetricDefinition
from projects.vehicle_data.models import (
    AcquisitionResult,
    ScalarValue,
    failure,
    success,
)
from projects.vehicle_data.sources import VoltageAcquirer


DEFAULT_SOCKET = "/run/van-telemetry/api.sock"
RETUNE_HELPER = REPO / "projects" / "vehicle_data" / "retune.py"
ACTIVE_DRIVE_HELPER = REPO / "projects" / "vehicle_data" / "active_drive.py"
MAX_ACTIVE_EVENT_BYTES = 64 * 1024
ACTIVE_DRIVE_RESTORATION_INHIBIT = "vehicle-data-restoration-failed"
ACTIVE_DRIVE_SOURCES = frozenset(
    {
        "ccan.broadcast.0x0fc",
        "ccan.broadcast.0x100",
        "ccan.broadcast.0x101",
        "ccan.broadcast.0x1f7",
        "ccan.broadcast.0x2ed",
        "ccan.broadcast.0x2ef",
        "ccan.broadcast.0x41a",
        "ccan.broadcast.0x41d",
        "pcm.did.01a1",
        "rf_hub.did.31d0",
        "rf_hub.did.31d1",
        "rf_hub.did.31d2",
        "rf_hub.did.31d3",
    }
)
ACTIVE_DRIVE_FAILURE_REASONS = frozenset(
    {
        "adapter_unhealthy",
        "bus_asleep",
        "can_busy",
        "engine_not_running",
        "helper_failed",
        "helper_protocol_error",
        "helper_start_failed",
        "inhibited",
        "malformed_response",
        "response_rejected",
        "response_timeout",
        "restoration_failed",
        "session_required",
        "wrong_bus",
    }
)


def _retune_failure(reason: str, detail: str) -> dict[str, object]:
    return {
        "state": "failed",
        "reason": reason,
        "detail": detail,
        "from_bitrate": None,
        "to_bitrate": None,
        "bus": None,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


class PassiveAutoRetuner:
    """Run the termination-safe passive retune helper as a child process."""

    _STATES = frozenset(("switched", "blocked", "failed", "no_change"))

    def __init__(
        self,
        *,
        channel: str,
        probe_seconds: float,
        timeout_seconds: float = 15.0,
    ):
        self.channel = channel
        self.probe_seconds = probe_seconds
        self.timeout_seconds = timeout_seconds

    def attempt(self, expected_bitrate: int) -> dict[str, object]:
        command = [
            sys.executable,
            str(RETUNE_HELPER),
            "--channel",
            self.channel,
            "--expected-bitrate",
            str(expected_bitrate),
            "--probe-seconds",
            str(self.probe_seconds),
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            return _retune_failure(
                "helper_start_failed", f"could not start retune helper: {exc}"
            )
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            return _retune_failure(
                "helper_timeout",
                "retune helper exceeded its bounded runtime and was terminated",
            )
        if process.returncode != 0:
            detail = (stderr or stdout).strip()[-500:]
            return _retune_failure(
                "helper_failed",
                detail or f"retune helper exited {process.returncode}",
            )
        lines = [line for line in stdout.splitlines() if line.strip()]
        try:
            payload = json.loads(lines[-1]) if lines else None
        except json.JSONDecodeError:
            payload = None
        if (
            not isinstance(payload, dict)
            or payload.get("state") not in self._STATES
            or not isinstance(payload.get("reason"), str)
            or not isinstance(payload.get("detail"), str)
        ):
            return _retune_failure(
                "invalid_helper_response",
                "retune helper did not return one valid result object",
            )
        return payload


class ActiveDriveSupervisor:
    """Supervise the termination-safe active-drive owner subprocess."""

    def __init__(
        self,
        *,
        channel: str,
        event_handler,
        popen_factory=subprocess.Popen,
        shutdown_timeout_seconds: float = 10.0,
        event_silence_timeout_seconds: float = 10.0,
        queue_poll_seconds: float = 0.1,
    ):
        self.channel = channel
        self.event_handler = event_handler
        self.popen_factory = popen_factory
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.event_silence_timeout_seconds = event_silence_timeout_seconds
        self.queue_poll_seconds = queue_poll_seconds
        if (
            self.shutdown_timeout_seconds <= 0
            or self.event_silence_timeout_seconds <= 0
            or self.queue_poll_seconds <= 0
        ):
            raise ValueError("active-drive supervisor timeouts must be positive")
        self._lock = threading.Lock()
        self._process = None

    def stop(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except (OSError, ProcessLookupError):
                pass

    def _terminate_bounded(self, process) -> str:
        """Best-effort child termination that never drops the process handle."""
        details = []
        try:
            running = process.poll() is None
        except Exception as exc:
            running = True
            details.append(f"poll failed: {type(exc).__name__}: {exc}")
        if running:
            try:
                process.terminate()
            except (OSError, ProcessLookupError) as exc:
                details.append(
                    f"terminate failed: {type(exc).__name__}: {exc}"
                )
        try:
            process.wait(timeout=self.shutdown_timeout_seconds)
        except subprocess.TimeoutExpired:
            details.append("graceful termination timed out; child was killed")
            try:
                process.kill()
            except (OSError, ProcessLookupError) as exc:
                details.append(f"kill failed: {type(exc).__name__}: {exc}")
            try:
                process.wait(timeout=self.shutdown_timeout_seconds)
            except Exception as exc:
                details.append(
                    f"post-kill wait failed: {type(exc).__name__}: {exc}"
                )
        except Exception as exc:
            details.append(f"wait failed: {type(exc).__name__}: {exc}")
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                pass
            try:
                process.wait(timeout=self.shutdown_timeout_seconds)
            except Exception:
                pass
        return "; ".join(details)

    @staticmethod
    def _unverified_restoration(detail: str) -> dict[str, object]:
        return {
            "type": "final",
            "state": "restoration_failed",
            "reason": "restoration_failed",
            "detail": detail,
            "interface_mode": "armed_diagnostic",
            "restored": False,
        }

    def run(self, stop_event: threading.Event) -> dict[str, object]:
        command = [
            sys.executable,
            str(ACTIVE_DRIVE_HELPER),
            "--channel",
            self.channel,
            "--expected-parent-pid",
            str(os.getpid()),
        ]
        try:
            process = self.popen_factory(
                command,
                stdout=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            return {
                "type": "final",
                "reason": "helper_start_failed",
                "detail": f"could not start active-drive helper: {exc}",
                "restored": None,
            }
        with self._lock:
            self._process = process
        if stop_event.is_set() and process.poll() is None:
            try:
                process.terminate()
            except (OSError, ProcessLookupError):
                pass
        final_event = None
        protocol_error = None
        supervisor_error = None
        return_code = None
        output_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        reader_thread = None

        def read_output() -> None:
            try:
                if process.stdout is None:
                    output_queue.put(
                        (
                            "error",
                            RuntimeError(
                                "active-drive helper stdout pipe is unavailable"
                            ),
                        )
                    )
                    return
                for line in process.stdout:
                    output_queue.put(("line", line))
            except Exception as exc:
                output_queue.put(("error", exc))
            finally:
                output_queue.put(("eof", None))

        try:
            reader_thread = threading.Thread(
                target=read_output,
                name="van-telemetry-active-drive-output",
                daemon=True,
            )
            reader_thread.start()
            last_event_at = time.monotonic()
            termination_started_at = None
            eof_seen = False
            while True:
                now = time.monotonic()
                if stop_event.is_set() and termination_started_at is None:
                    termination_started_at = now
                    try:
                        if process.poll() is None:
                            process.terminate()
                    except (OSError, ProcessLookupError):
                        pass
                if (
                    termination_started_at is None
                    and now - last_event_at
                    > self.event_silence_timeout_seconds
                ):
                    protocol_error = (
                        "active-drive helper exceeded the bounded event-silence "
                        "interval"
                    )
                    termination_started_at = now
                    try:
                        if process.poll() is None:
                            process.terminate()
                    except (OSError, ProcessLookupError):
                        pass
                if (
                    termination_started_at is not None
                    and now - termination_started_at
                    > self.shutdown_timeout_seconds
                    and process.poll() is None
                ):
                    protocol_error = protocol_error or (
                        "active-drive helper did not finish bounded cleanup "
                        "after termination"
                    )
                    try:
                        process.kill()
                    except (OSError, ProcessLookupError):
                        pass
                    break
                try:
                    item_type, item = output_queue.get(
                        timeout=self.queue_poll_seconds
                    )
                except queue.Empty:
                    if eof_seen and process.poll() is not None:
                        break
                    continue
                if item_type == "error":
                    error = item
                    supervisor_error = (
                        "active-drive output reader failed: "
                        f"{type(error).__name__}: {error}"
                    )
                    if termination_started_at is None:
                        termination_started_at = time.monotonic()
                        try:
                            if process.poll() is None:
                                process.terminate()
                        except (OSError, ProcessLookupError):
                            pass
                    continue
                if item_type == "eof":
                    eof_seen = True
                    if process.poll() is not None:
                        break
                    if termination_started_at is None:
                        termination_started_at = time.monotonic()
                        if final_event is None:
                            protocol_error = (
                                "active-drive helper closed stdout without a "
                                "final restoration event"
                            )
                            try:
                                process.terminate()
                            except (OSError, ProcessLookupError):
                                pass
                    continue
                line = item
                if not isinstance(line, str):
                    protocol_error = (
                        "active-drive helper emitted non-text output"
                    )
                elif protocol_error is None and supervisor_error is None:
                    last_event_at = time.monotonic()
                    if len(line.encode("utf-8", errors="replace")) > MAX_ACTIVE_EVENT_BYTES:
                        protocol_error = "active-drive helper event exceeded size limit"
                    else:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            protocol_error = (
                                "active-drive helper emitted invalid JSON"
                            )
                        else:
                            if not isinstance(event, dict):
                                protocol_error = (
                                    "active-drive helper event is not an object"
                                )
                            elif event.get("type") == "final":
                                if final_event is not None:
                                    protocol_error = (
                                        "active-drive helper emitted more than one "
                                        "final restoration event"
                                    )
                                else:
                                    # The broker applies this returned event once,
                                    # after supervision has completed. Streaming it
                                    # here would duplicate status/cache/inhibit side
                                    # effects in _run_active_drive_if_ready().
                                    final_event = event
                            else:
                                try:
                                    self.event_handler(event)
                                except Exception as exc:
                                    protocol_error = (
                                        "active-drive event rejected: "
                                        f"{type(exc).__name__}: {exc}"
                                    )
                if (
                    (protocol_error or supervisor_error)
                    and termination_started_at is None
                ):
                    termination_started_at = time.monotonic()
                    try:
                        if process.poll() is None:
                            process.terminate()
                    except (OSError, ProcessLookupError):
                        pass
            try:
                return_code = process.wait(
                    timeout=self.shutdown_timeout_seconds
                )
            except Exception as exc:
                if not isinstance(exc, subprocess.TimeoutExpired):
                    supervisor_error = (
                        "active-drive helper wait failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                else:
                    protocol_error = protocol_error or (
                        "active-drive helper remained alive after bounded "
                        "supervision shutdown"
                    )
                cleanup_detail = self._terminate_bounded(process)
                if cleanup_detail:
                    existing = supervisor_error or protocol_error or ""
                    if supervisor_error:
                        supervisor_error = f"{existing}; {cleanup_detail}"
                    else:
                        protocol_error = f"{existing}; {cleanup_detail}"
        except Exception as exc:
            supervisor_error = (
                "active-drive supervision failed: "
                f"{type(exc).__name__}: {exc}"
            )
            cleanup_detail = self._terminate_bounded(process)
            if cleanup_detail:
                supervisor_error = f"{supervisor_error}; {cleanup_detail}"
        finally:
            if reader_thread is not None:
                reader_thread.join(timeout=self.queue_poll_seconds)
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except Exception:
                    pass
            try:
                process_exited = process.poll() is not None
            except Exception:
                process_exited = False
            with self._lock:
                if self._process is process and process_exited:
                    self._process = None
        if supervisor_error:
            return self._unverified_restoration(
                f"{supervisor_error}; the parent could not verify the "
                "helper's final listen-only restoration"
            )
        if protocol_error:
            return self._unverified_restoration(
                f"{protocol_error}; the parent could not verify the "
                "helper's final listen-only restoration"
            )
        if final_event is not None:
            return final_event
        return self._unverified_restoration(
            (
                "active-drive helper exited without a final restoration event "
                f"(status {return_code}); listen-only restoration is unverified"
            )
        )


@dataclass
class _Inflight:
    event: threading.Event = field(default_factory=threading.Event)
    result: AcquisitionResult | None = None


class TelemetryBroker:
    """Thread-safe cache and serialized acquisition scheduler.

    All public GET-style methods are cache-only. Only :meth:`acquire` can invoke
    a telemetry source, and its metric/mode inputs are allowlisted.
    """

    def __init__(
        self,
        *,
        acquirer=None,
        definitions: dict[str, MetricDefinition] | None = None,
        monotonic=time.monotonic,
        collector_interval_seconds: float = 1.0,
        wake_min_interval_seconds: float | None = None,
        acquisition_wait_seconds: float = 20.0,
        auto_retuner=None,
        auto_retune_enabled: bool = True,
        auto_retune_trigger: int = 3,
        auto_retune_cooldown_seconds: float = 30.0,
        passive_powertrain_reader=None,
        active_drive_supervisor=None,
        active_drive_enabled: bool = False,
    ):
        self.acquirer = acquirer or VoltageAcquirer()
        self.definitions = definitions or METRICS
        self.monotonic = monotonic
        self.collector_interval_seconds = collector_interval_seconds
        self.wake_min_interval_seconds = wake_min_interval_seconds
        self.acquisition_wait_seconds = acquisition_wait_seconds
        if auto_retune_trigger < 1:
            raise ValueError("auto-retune trigger must be at least one")
        if auto_retune_cooldown_seconds < 0:
            raise ValueError("auto-retune cooldown cannot be negative")
        self.auto_retune_enabled = auto_retune_enabled
        self.auto_retune_trigger = auto_retune_trigger
        self.auto_retune_cooldown_seconds = auto_retune_cooldown_seconds
        self.auto_retuner = auto_retuner or PassiveAutoRetuner(
            channel=getattr(self.acquirer, "channel", "can0"),
            probe_seconds=getattr(self.acquirer, "probe_seconds", 0.75),
        )
        self.passive_powertrain_reader = passive_powertrain_reader
        self.active_drive_supervisor = active_drive_supervisor
        self.active_drive_enabled = bool(
            active_drive_enabled and active_drive_supervisor is not None
        )

        self._lock = threading.RLock()
        # Prevent the passive collector from racing a client-triggered active
        # request before the cross-process observer/exclusive lock is reached.
        self._source_lock = threading.Lock()
        self._cache: dict[str, AcquisitionResult] = {}
        self._last_error: dict[str, AcquisitionResult] = {}
        self._last_attempt: dict[tuple[str, str], float] = {}
        self._inflight: dict[tuple[str, str], _Inflight] = {}
        self._interface_status: dict[str, object] = {
            "channel": getattr(self.acquirer, "channel", "can0"),
            "adapter_present": None,
            "up": None,
            "bitrate": None,
            "listen_only": None,
            "controller_state": None,
            "topology": {
                "bus": "unknown",
                "usable": False,
                "reason": "collector has not sampled interface state",
            },
            "active_inhibits": [],
        }
        self._collector_state = "stopped"
        self._collector_failure_detail: str | None = None
        self._collector_cycles = 0
        self._collector_last_cycle_at: str | None = None
        self._collector_thread: threading.Thread | None = None
        self._collector_stop = threading.Event()
        self._passive_engine_evidence = "unknown"
        self._passive_stop_evidence_streak = 0
        self._passive_unknown_evidence_streak = 0
        self._active_drive_blocked_reason: str | None = None
        self._active_drive_restoration_latched = False
        self._active_drive: dict[str, object] = {
            "enabled": self.active_drive_enabled,
            "state": "idle" if self.active_drive_enabled else "disabled",
            "reason": (
                "engine_not_running"
                if self.active_drive_enabled
                else "disabled_by_configuration"
            ),
            "detail": (
                "waiting for qualified passive engine-running evidence"
                if self.active_drive_enabled
                else "coordinated active-drive collection is disabled"
            ),
            "interface_mode": "listen_only",
            "helper_pid": None,
            "last_event_at": None,
            "restoration_failed": False,
        }
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._vehicle_state_observed_monotonic: float | None = None
        self._vehicle_state: dict[str, object] = {
            "state": "unknown",
            "running": None,
            "confidence": "unknown",
            "basis": "no_passive_observation",
            "detail": (
                "the broker has not observed enough passive bus evidence to "
                "describe vehicle state"
            ),
            "observed_at": None,
        }
        self._auto_retune_last_attempt_monotonic: float | None = None
        self._auto_retune: dict[str, object] = {
            "enabled": self.auto_retune_enabled,
            "state": "monitoring" if self.auto_retune_enabled else "disabled",
            "detail": (
                "waiting for repeated passive wrong-rate evidence"
                if self.auto_retune_enabled
                else "disabled by broker configuration"
            ),
            "wrong_rate_streak": 0,
            "trigger_after": self.auto_retune_trigger,
            "cooldown_seconds": self.auto_retune_cooldown_seconds,
            "last_attempt": None,
        }

    def list_metrics(self) -> dict[str, object]:
        return {
            "metrics": [
                self.definitions[name].public_dict()
                for name in sorted(self.definitions)
            ]
        }

    def snapshot_response(self) -> dict[str, object]:
        """Return one cache-only dashboard snapshot.

        Keeping status, the public metric catalog, and every cached metric in a
        single response lets the web tier remain registry-driven as the
        allowlist grows. This method never invokes an acquisition source.
        """
        return {
            "status": self.status_response(),
            "catalog": self.list_metrics()["metrics"],
            "metrics": {
                name: self.metric_response(name)
                for name in sorted(self.definitions)
            },
        }

    def _serialize(
        self, result: AcquisitionResult, definition: MetricDefinition
    ) -> dict[str, object]:
        return result.as_dict(
            now_monotonic=self.monotonic(),
            stale_after_seconds=definition.stale_after_seconds,
        )

    def metric_response(self, metric: str) -> dict[str, object]:
        definition = self.definitions.get(metric)
        if definition is None:
            return {
                "metric": metric,
                "available": False,
                "reason": "unknown_metric",
                "detail": "metric is not in the public allowlist",
            }
        with self._lock:
            current = self._cache.get(metric)
            last_error = self._last_error.get(metric)
        if current is not None:
            payload = self._serialize(current, definition)
            if last_error is not None:
                payload["last_acquisition_error"] = {
                    "reason": last_error.reason,
                    "detail": last_error.detail,
                }
            return payload
        if last_error is not None:
            return self._serialize(last_error, definition)
        return {
            "metric": metric,
            "available": False,
            "unit": definition.unit,
            "reason": "stale",
            "detail": "no observation has been cached",
        }

    @staticmethod
    def _value_error(
        definition: MetricDefinition, value: object
    ) -> str | None:
        value_type = definition.value_type
        if value_type == "boolean":
            valid = type(value) is bool
        elif value_type == "integer":
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif value_type == "number":
            valid = (
                isinstance(value, int)
                and not isinstance(value, bool)
            ) or (
                isinstance(value, float)
                and math.isfinite(value)
            )
        elif value_type == "string":
            valid = isinstance(value, str)
        else:
            return f"registry has unsupported value_type {value_type!r}"
        if not valid:
            return f"value must have registry type {value_type}"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if definition.minimum is not None and value < definition.minimum:
                return f"value is below minimum {definition.minimum}"
            if definition.maximum is not None and value > definition.maximum:
                return f"value is above maximum {definition.maximum}"
        return None

    @staticmethod
    def _source_for(definition: MetricDefinition, source_name: object):
        return next(
            (
                source
                for source in definition.sources
                if source.name == source_name
            ),
            None,
        )

    def _validate_acquirer_result(
        self,
        metric: str,
        definition: MetricDefinition,
        result: AcquisitionResult,
    ) -> AcquisitionResult:
        """Reject a misrouted source result before it can enter another cache."""
        if not result.available:
            if result.metric == metric and result.unit == definition.unit:
                return result
            return failure(
                metric=metric,
                unit=definition.unit,
                reason="invalid_source_result",
                detail="acquirer returned failure metadata for another metric",
            )
        source = self._source_for(definition, result.source)
        mismatches = []
        if result.metric != metric:
            mismatches.append("metric")
        if source is None:
            mismatches.append("source")
        if result.unit != definition.unit:
            mismatches.append("unit")
        if source is not None and result.bus != source.bus:
            mismatches.append("bus")
        if source is not None and result.quality != source.quality:
            mismatches.append("quality")
        value_error = self._value_error(definition, result.value)
        if value_error is not None:
            mismatches.append("value")
        if mismatches:
            return failure(
                metric=metric,
                unit=definition.unit,
                reason="invalid_source_result",
                detail=(
                    "acquirer result failed allowlist validation: "
                    + ", ".join(mismatches)
                ),
            )
        return result

    def publish_observation(
        self,
        metric: str,
        *,
        value: ScalarValue,
        unit: str,
        source: str,
        bus: str,
        quality: str,
    ) -> AcquisitionResult:
        """Accept one exact allowlisted observation over the local Unix API.

        Publisher timestamps are intentionally not accepted. The broker stamps
        both wall-clock and monotonic receipt time so cache age cannot be
        forged or accidentally inherited from a logger's clock domain.
        """
        received_monotonic = self.monotonic()
        definition = self.definitions.get(metric)
        if definition is None:
            return failure(
                metric=metric,
                unit="",
                reason="unknown_metric",
                detail="metric is not in the public allowlist",
            )
        source_definition = self._source_for(definition, source)
        if source_definition is None:
            return failure(
                metric=metric,
                unit=definition.unit,
                reason="invalid_observation",
                detail="source is not allowlisted for this metric",
            )
        if not source_definition.publisher_allowed:
            return failure(
                metric=metric,
                unit=definition.unit,
                reason="source_not_publishable",
                detail="source may only be populated by its in-process acquirer",
            )
        mismatches = []
        if unit != definition.unit:
            mismatches.append("unit")
        if bus != source_definition.bus:
            mismatches.append("bus")
        if quality != source_definition.quality:
            mismatches.append("quality")
        value_error = self._value_error(definition, value)
        if value_error is not None:
            mismatches.append(value_error)
        elif (
            source_definition.publisher_values is not None
            and value not in source_definition.publisher_values
        ):
            mismatches.append(
                "value is not permitted for publication by this source"
            )
        if mismatches:
            return failure(
                metric=metric,
                unit=definition.unit,
                reason="invalid_observation",
                detail="observation failed allowlist validation: "
                + ", ".join(mismatches),
            )
        result = success(
            metric=metric,
            unit=definition.unit,
            value=value,
            source=source_definition.name,
            bus=source_definition.bus,
            acquisition=source_definition.acquisition_class,
            quality=source_definition.quality,
            observed_monotonic=received_monotonic,
            detail="received from an allowlisted local observation publisher",
        )
        with self._lock:
            self._cache[metric] = result
            # Publisher success does not prove that a prior in-process CAN
            # acquisition failure (for example can_busy or restoration_failed)
            # has recovered. Keep that diagnostic until the acquirer itself
            # completes successfully.
        self._update_vehicle_state(result)
        return result

    def _store_active_observation(self, event: dict[str, object]) -> None:
        metric = event.get("metric")
        source_name = event.get("source")
        if not isinstance(metric, str) or not isinstance(source_name, str):
            raise ValueError("active observation metric/source must be strings")
        if source_name not in ACTIVE_DRIVE_SOURCES:
            raise ValueError("active observation source is outside the helper allowlist")
        definition = self.definitions.get(metric)
        if definition is None:
            raise ValueError("active observation metric is not registered")
        source = self._source_for(definition, source_name)
        if source is None:
            raise ValueError("active observation source is not registered for metric")
        if event.get("unit") != definition.unit:
            raise ValueError("active observation unit does not match registry")
        if event.get("bus") != source.bus or event.get("quality") != source.quality:
            raise ValueError("active observation source metadata does not match registry")
        value = event.get("value")
        value_error = self._value_error(definition, value)
        if value_error is not None:
            raise ValueError(value_error)
        result = success(
            metric=metric,
            unit=definition.unit,
            value=value,
            source=source.name,
            bus=source.bus,
            acquisition=source.acquisition_class,
            quality=source.quality,
            observed_monotonic=self.monotonic(),
            detail=str(event.get("detail") or "coordinated active-drive observation"),
            interface_mode="armed_diagnostic",
        )
        with self._lock:
            self._cache[metric] = result
            self._last_error.pop(metric, None)
        self._update_vehicle_state(result)

    def _record_active_failure(
        self,
        reason: str,
        detail: str,
        *,
        interface_mode: str = "listen_only",
    ) -> None:
        if reason not in ACTIVE_DRIVE_FAILURE_REASONS:
            reason = "helper_failed"
        definition = self.definitions.get("generator.field_duty")
        if definition is None:
            return
        source = definition.sources[0]
        result = failure(
            metric=definition.name,
            unit=definition.unit,
            reason=reason,
            detail=detail,
            bus=source.bus,
            acquisition=source.acquisition_class,
            interface_mode=interface_mode,
        )
        with self._lock:
            # Generator command duty must become inactive immediately when its
            # engine-running diagnostic owner stops. Do not leave a still-fresh
            # pre-stop sample looking live for the remainder of its TTL.
            self._cache.pop(definition.name, None)
            self._last_error[definition.name] = result

    def handle_active_drive_event(self, event: dict[str, object]) -> None:
        """Validate one trusted helper-pipe event; never expose this as an API."""
        event_type = event.get("type")
        if event_type == "observation":
            if event.get("interface_mode") != "armed_diagnostic":
                raise ValueError("active observation must report armed_diagnostic mode")
            self._store_active_observation(event)
            return
        if event_type not in ("status", "failure", "final"):
            raise ValueError("unsupported active-drive event type")
        reason = event.get("reason")
        detail = event.get("detail")
        if not isinstance(reason, str) or not isinstance(detail, str):
            raise ValueError("active-drive status reason/detail must be strings")
        interface_mode = event.get("interface_mode", "listen_only")
        if interface_mode not in (
            "listen_only",
            "armed_diagnostic",
            "unknown",
        ):
            raise ValueError("invalid active-drive interface mode")
        state = event.get("state")
        if state is None:
            state = (
                "restoration_failed"
                if reason == "restoration_failed"
                else ("idle" if event_type == "final" else event_type)
            )
        if not isinstance(state, str):
            raise ValueError("active-drive state must be a string")
        if event_type == "status":
            if (
                state != "armed_diagnostic"
                or reason != "running_gate_satisfied"
                or interface_mode != "armed_diagnostic"
            ):
                raise ValueError("active-drive armed status is inconsistent")
        elif event_type == "failure":
            if reason not in ACTIVE_DRIVE_FAILURE_REASONS:
                raise ValueError("active-drive failure reason is not allowlisted")
        else:
            restored = event.get("restored")
            if restored is not None and type(restored) is not bool:
                raise ValueError("active-drive final restored must be boolean or null")
            if reason not in ACTIVE_DRIVE_FAILURE_REASONS:
                raise ValueError("active-drive final reason is not allowlisted")
            if state not in ("idle", "restoration_failed"):
                raise ValueError("active-drive final state is invalid")
            if restored is True and interface_mode != "listen_only":
                raise ValueError("restored final must report listen_only mode")
            if restored is False and (
                reason != "restoration_failed"
                or state != "restoration_failed"
                or interface_mode != "armed_diagnostic"
            ):
                raise ValueError("failed restoration final is inconsistent")
            if reason == "restoration_failed" and restored is not False:
                raise ValueError("restoration failure must carry restored=false")
            if restored is None and interface_mode == "armed_diagnostic":
                raise ValueError("unverified final cannot claim armed ownership")
        with self._lock:
            self._active_drive.update(
                {
                    "state": state,
                    "reason": reason,
                    "detail": detail,
                    "interface_mode": interface_mode,
                    "last_event_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            if isinstance(event.get("pid"), int) and not isinstance(
                event.get("pid"), bool
            ):
                self._active_drive["helper_pid"] = event["pid"]
            if event_type == "final":
                self._active_drive["helper_pid"] = None
        if event_type in ("failure", "final") and reason in ACTIVE_DRIVE_FAILURE_REASONS:
            self._record_active_failure(
                reason,
                detail,
                interface_mode=interface_mode,
            )
        if reason == "restoration_failed" or event.get("restored") is False:
            with self._lock:
                self._active_drive_restoration_latched = True
                self._active_drive["restoration_failed"] = True
            try:
                can_operation_state.begin_inhibit(
                    ACTIVE_DRIVE_RESTORATION_INHIBIT,
                    channel=getattr(self.acquirer, "channel", "can0"),
                    reason=(
                        "broker could not verify active-drive listen-only "
                        f"restoration: {detail}"
                    ),
                )
            except Exception as exc:
                with self._lock:
                    self._active_drive["detail"] = (
                        f"{detail}; persistent restoration inhibit could not "
                        f"be recorded: {type(exc).__name__}: {exc}"
                    )

    def _refresh_interface_status(self) -> None:
        try:
            snapshot = self.acquirer.status_snapshot()
        except Exception as exc:
            snapshot = {
                "channel": getattr(self.acquirer, "channel", "can0"),
                "adapter_present": None,
                "up": None,
                "bitrate": None,
                "listen_only": None,
                "controller_state": None,
                "topology": {
                    "bus": "unknown",
                    "usable": False,
                    "reason": f"status snapshot failed: {exc}",
                },
                "active_inhibits": ["status-unavailable"],
            }
        with self._lock:
            self._interface_status = snapshot

    def status_response(self) -> dict[str, object]:
        with self._lock:
            interface = json.loads(json.dumps(self._interface_status))
            inflight = [
                {"metric": metric, "mode": mode}
                for metric, mode in sorted(self._inflight)
            ]
            last_errors = {
                metric: {
                    "reason": result.reason,
                    "detail": result.detail,
                }
                for metric, result in self._last_error.items()
            }
            cached = {
                metric: self._serialize(result, self.definitions[metric])
                for metric, result in self._cache.items()
            }
            collector = {
                "state": self._collector_state,
                "cycles": self._collector_cycles,
                "last_cycle_at": self._collector_last_cycle_at,
                "interval_seconds": self.collector_interval_seconds,
                "failure_detail": self._collector_failure_detail,
            }
            auto_retune = json.loads(json.dumps(self._auto_retune))
            active_drive = json.loads(json.dumps(self._active_drive))
            last_retune = self._auto_retune_last_attempt_monotonic
            vehicle_state = json.loads(json.dumps(self._vehicle_state))
            vehicle_observed = self._vehicle_state_observed_monotonic
        cooldown_remaining = 0.0
        if last_retune is not None:
            cooldown_remaining = max(
                0.0,
                self.auto_retune_cooldown_seconds
                - (self.monotonic() - last_retune),
            )
        auto_retune["cooldown_remaining_seconds"] = round(
            cooldown_remaining, 1
        )
        vehicle_state["age_ms"] = (
            round(max(0.0, self.monotonic() - vehicle_observed) * 1000)
            if vehicle_observed is not None
            else None
        )
        ignition_definition = self.definitions.get("vehicle.ignition_on")
        if (
            vehicle_state.get("basis") == "ccan_0x2ef_ignition_gate"
            and vehicle_observed is not None
            and ignition_definition is not None
            and self.monotonic() - vehicle_observed
            > ignition_definition.stale_after_seconds
        ):
            vehicle_state.update(
                {
                    "state": "unknown",
                    "running": None,
                    "confidence": "stale",
                    "basis": "stale_ccan_0x2ef_ignition_gate",
                    "detail": (
                        "the last verified ignition-on gate observation is "
                        "stale; current ignition state is unknown"
                    ),
                }
            )
        rpm_definition = self.definitions.get("engine.rpm")
        if (
            vehicle_state.get("basis") == "qualified_ccan_0x0fc_engine_speed"
            and vehicle_observed is not None
            and rpm_definition is not None
            and self.monotonic() - vehicle_observed
            > rpm_definition.stale_after_seconds
        ):
            vehicle_state.update(
                {
                    "state": "unknown",
                    "running": None,
                    "confidence": "stale",
                    "basis": "stale_ccan_0x0fc_engine_speed",
                    "detail": (
                        "the last qualified passive engine-speed observation "
                        "is stale; current running state is unknown"
                    ),
                }
            )
        topology = interface.get("topology") or {}
        inhibits = interface.get("active_inhibits") or []
        busy_error = next(
            (
                error
                for error in last_errors.values()
                if error.get("reason") == "can_busy"
            ),
            None,
        )
        active_drive_owns = (
            active_drive.get("interface_mode") == "armed_diagnostic"
            and active_drive.get("state")
            not in ("idle", "disabled", "restoration_failed")
        )
        active_drive_reserved = active_drive_owns or active_drive.get(
            "state"
        ) == "starting"
        if active_drive_owns:
            # The last normal status probe predates the child-owned interval.
            # Overlay the helper's verified mode so status never calls an armed
            # adapter listen-only merely because the normal collector is
            # synchronously waiting for the child.
            interface["listen_only"] = False
            interface["mode"] = "armed_diagnostic"
        else:
            interface["mode"] = (
                "listen_only"
                if interface.get("listen_only") is True
                else "armed_or_unknown"
            )
        active_permitted = bool(
            interface.get("adapter_present")
            and interface.get("up")
            and interface.get("listen_only")
            and interface.get("controller_state") == "ERROR-ACTIVE"
            and topology.get("usable")
            and topology.get("bus") in ("c-can", "b-can")
            and not inhibits
            and not inflight
            and busy_error is None
            and not active_drive_reserved
            and not active_drive.get("restoration_failed")
        )
        if active_drive_reserved:
            current_owner = {
                "kind": "broker_active_drive",
                "detail": active_drive.get("detail"),
            }
        elif inhibits:
            current_owner = {
                "kind": "external_inhibit",
                "names": inhibits,
            }
        elif inflight:
            current_owner = {
                "kind": "broker",
                "operations": inflight,
            }
        elif busy_error is not None:
            current_owner = {
                "kind": "participating_or_external_can_user",
                "detail": busy_error["detail"],
            }
        else:
            current_owner = None
        return {
            "service": "van-telemetry",
            "started_at": self._started_at,
            "interface": interface,
            "current_owner": current_owner,
            "active_acquisition_permitted": active_permitted,
            "collector": collector,
            "active_drive": active_drive,
            "auto_retune": auto_retune,
            "vehicle_state": vehicle_state,
            "inflight": inflight,
            "last_acquisition_errors": last_errors,
            "cached_metrics": cached,
        }

    def _update_vehicle_state(self, result: AcquisitionResult) -> None:
        """Record only state conclusions supported by passive acquisition.

        Awake traffic does not distinguish an idling engine from ignition-on,
        a fob wake, or a charger-powered module wake. In particular, battery
        voltage is never used as an engine-running heuristic.
        """
        if result.metric not in (
            "battery.voltage",
            "engine.rpm",
            "vehicle.ignition_on",
        ):
            return
        if (
            result.metric == "engine.rpm"
            and result.available
            and result.source == "ccan.broadcast.0x0fc"
            and isinstance(result.value, (int, float))
            and not isinstance(result.value, bool)
        ):
            running = float(result.value) >= 400.0
            state = {
                "state": "running" if running else "ignition_on",
                "running": running,
                "confidence": "verified",
                "basis": "qualified_ccan_0x0fc_engine_speed",
                "detail": (
                    f"qualified passive 0x0FC engine speed is "
                    f"{float(result.value):.0f} rpm"
                ),
                "observed_at": (
                    result.observed_at.isoformat()
                    if result.observed_at is not None
                    else datetime.now(timezone.utc).isoformat()
                ),
            }
            with self._lock:
                self._vehicle_state = state
                self._vehicle_state_observed_monotonic = (
                    result.observed_monotonic
                    if result.observed_monotonic is not None
                    else self.monotonic()
                )
            return
        if (
            result.metric == "battery.voltage"
            and result.acquisition == "physical_read_data_by_identifier"
        ):
            # A solicited cluster response proves neither passive bus activity
            # nor current ignition state. The separate verified 0x2EF
            # observation is the authority during a cluster logger run.
            return
        if result.metric == "battery.voltage":
            with self._lock:
                current_basis = self._vehicle_state.get("basis")
                current_observed = self._vehicle_state_observed_monotonic
            ignition_stale_after = self.definitions.get(
                "vehicle.ignition_on"
            )
            if (
                current_basis == "ccan_0x2ef_ignition_gate"
                and current_observed is not None
                and ignition_stale_after is not None
                and self.monotonic() - current_observed
                <= ignition_stale_after.stale_after_seconds
            ):
                return
        state = None
        if result.available and result.metric == "vehicle.ignition_on":
            ignition_on = result.value is True
            state = {
                "state": "ignition_on" if ignition_on else "parked",
                "running": None if ignition_on else False,
                "confidence": "verified",
                "basis": "ccan_0x2ef_ignition_gate",
                "detail": (
                    "verified C-CAN ignition-on gate is present"
                    if ignition_on
                    else "verified C-CAN ignition-on gate is absent"
                ),
            }
        elif result.available:
            wake_assisted = result.acquisition == "wake_assisted"
            state = {
                "state": "awake",
                "running": None,
                "confidence": "observed",
                "basis": (
                    "broker_wake_activity"
                    if wake_assisted
                    else "passive_bus_activity"
                ),
                "detail": (
                    f"{result.bus or 'vehicle bus'} traffic is present; "
                    + (
                        "the broker just performed the approved wake, so this "
                        "activity is not evidence that the engine is running"
                        if wake_assisted
                        else (
                            "running versus ignition-on versus a temporary "
                            "wake is not yet distinguished"
                        )
                    )
                ),
            }
        elif result.reason == "bus_asleep":
            state = {
                "state": "asleep",
                "running": False,
                "confidence": "inferred",
                "basis": "passive_bus_silence",
                "detail": (
                    "no frames arrived at the approved bitrate; this is "
                    "consistent with a sleeping vehicle, but an unplugged "
                    "physical leg is not distinguishable from silence"
                ),
            }
        elif result.bus == "can-ch":
            state = {
                "state": "awake",
                "running": None,
                "confidence": "observed",
                "basis": "passive_can_ch_activity",
                "detail": (
                    "CAN-CH traffic is present; no verified running-state "
                    "metric is available on this branch"
                ),
            }
        elif result.bus == "wrong-rate":
            state = {
                "state": "awake",
                "running": None,
                "confidence": "inferred",
                "basis": "wrong_rate_rx_activity",
                "detail": (
                    "RX errors show traffic at another bitrate; vehicle "
                    "running state cannot be determined"
                ),
            }
        if state is None:
            return
        state["observed_at"] = (
            result.observed_at.isoformat()
            if result.observed_at is not None
            else datetime.now(timezone.utc).isoformat()
        )
        with self._lock:
            self._vehicle_state = state
            self._vehicle_state_observed_monotonic = (
                result.observed_monotonic
                if result.observed_monotonic is not None
                else self.monotonic()
            )

    def _limit_for(self, definition: MetricDefinition, mode: str) -> float:
        if mode == "wake_if_asleep":
            return (
                self.wake_min_interval_seconds
                if self.wake_min_interval_seconds is not None
                else definition.wake_min_interval_seconds
            )
        return definition.passive_min_interval_seconds

    def acquire(self, metric: str, mode: str) -> AcquisitionResult:
        definition = self.definitions.get(metric)
        if definition is None:
            return failure(
                metric=metric,
                unit="",
                reason="unknown_metric",
                detail="metric is not in the public allowlist",
            )
        if mode not in definition.allowed_acquisition_modes:
            return failure(
                metric=metric,
                unit=definition.unit,
                reason="unsupported_mode",
                detail=f"unsupported acquisition mode {mode!r}",
            )

        key = (metric, mode)
        owner = False
        with self._lock:
            entry = self._inflight.get(key)
            if entry is None:
                now = self.monotonic()
                minimum = self._limit_for(definition, mode)
                previous = self._last_attempt.get(key)
                if previous is not None and now - previous < minimum:
                    remaining = max(0.0, minimum - (now - previous))
                    return failure(
                        metric=metric,
                        unit=definition.unit,
                        reason="rate_limited",
                        detail=f"retry in {remaining:.1f} seconds",
                    )
                self._last_attempt[key] = now
                entry = _Inflight()
                self._inflight[key] = entry
                owner = True

        if not owner:
            if not entry.event.wait(self.acquisition_wait_seconds):
                return failure(
                    metric=metric,
                    unit=definition.unit,
                    reason="acquisition_timeout",
                    detail="timed out waiting for the coalesced acquisition",
                )
            return (
                entry.result.with_coalesced()
                if entry.result is not None
                else failure(
                    metric=metric,
                    unit=definition.unit,
                    reason="acquisition_timeout",
                    detail="coalesced acquisition ended without a result",
                )
            )

        result: AcquisitionResult | None = None
        try:
            try:
                with self._source_lock:
                    result = self.acquirer.acquire(mode)
            except Exception as exc:
                result = failure(
                    metric=metric,
                    unit=definition.unit,
                    reason="source_unavailable",
                    detail=f"acquirer failed closed: {exc}",
                )
            try:
                result = self._validate_acquirer_result(
                    metric, definition, result
                )
                self._refresh_interface_status()
                self._update_vehicle_state(result)
            except Exception as exc:
                result = failure(
                    metric=metric,
                    unit=definition.unit,
                    reason="invalid_source_result",
                    detail=(
                        "source result validation failed closed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )
        finally:
            completed = result or failure(
                metric=metric,
                unit=definition.unit,
                reason="source_unavailable",
                detail="acquisition ended before producing a source result",
            )
            with self._lock:
                if completed.available:
                    self._cache[metric] = completed
                    self._last_error.pop(metric, None)
                elif completed.reason != "rate_limited":
                    self._last_error[metric] = completed
                entry.result = completed
                entry.event.set()
                self._inflight.pop(key, None)
            result = completed
        return result

    def start_collector(self) -> None:
        with self._lock:
            if self._collector_thread is not None:
                return
            self._collector_state = "starting"
            self._collector_failure_detail = None
            self._collector_stop.clear()
            thread = threading.Thread(
                target=self._collector_loop,
                name="van-telemetry-passive",
                # Active-drive cleanup must finish before process exit; a
                # daemon thread could be discarded while can0 is still armed.
                daemon=False,
            )
            self._collector_thread = thread
            thread.start()

    def _set_auto_retune_state(
        self,
        state: str,
        detail: str,
        *,
        streak: int | None = None,
        last_attempt: dict[str, object] | None = None,
    ) -> None:
        with self._lock:
            self._auto_retune["state"] = state
            self._auto_retune["detail"] = detail
            if streak is not None:
                self._auto_retune["wrong_rate_streak"] = streak
            if last_attempt is not None:
                self._auto_retune["last_attempt"] = last_attempt

    def _consider_auto_retune(self, result: AcquisitionResult) -> None:
        if not self.auto_retune_enabled:
            return
        with self._lock:
            streak = int(self._auto_retune["wrong_rate_streak"])
            interface = json.loads(json.dumps(self._interface_status))

        if result.available:
            self._set_auto_retune_state(
                "monitoring",
                "current bitrate passively identifies an approved voltage bus",
                streak=0,
            )
            return

        wrong_rate = (
            result.reason == "wrong_bus" and result.bus == "wrong-rate"
        )
        degraded_after_wrong_rate = (
            streak > 0
            and result.reason == "source_unavailable"
            and (
                "ERROR-WARNING" in result.detail
                or "ERROR-PASSIVE" in result.detail
            )
        )
        if not wrong_rate and not degraded_after_wrong_rate:
            if result.reason in ("can_busy", "adapter_absent"):
                self._set_auto_retune_state(
                    "blocked", result.detail, streak=0
                )
            elif result.reason == "source_unavailable" and (
                "down" in result.detail
                or "controller" in result.detail
            ):
                self._set_auto_retune_state(
                    "blocked", result.detail, streak=0
                )
            elif result.reason == "bus_asleep":
                self._set_auto_retune_state(
                    "waiting",
                    "bus is silent; passive auto-retune will not guess a "
                    "physical leg without wrong-rate evidence",
                    streak=0,
                )
            else:
                self._set_auto_retune_state(
                    "monitoring",
                    "no qualifying wrong-rate evidence; interface left unchanged",
                    streak=0,
                )
            return

        streak += 1
        if streak < self.auto_retune_trigger:
            evidence = (
                "passive wrong-rate evidence"
                if wrong_rate
                else "wrong-rate followed by a degraded listen-only controller"
            )
            self._set_auto_retune_state(
                "evidence_accumulating",
                f"{evidence} {streak}/"
                f"{self.auto_retune_trigger}; waiting before any interface change",
                streak=streak,
            )
            return

        inhibits = interface.get("active_inhibits") or []
        if inhibits:
            self._set_auto_retune_state(
                "blocked",
                "auto-retune inhibited by " + ",".join(map(str, inhibits)),
                streak=streak,
            )
            return

        now = self.monotonic()
        with self._lock:
            previous = self._auto_retune_last_attempt_monotonic
        if (
            previous is not None
            and now - previous < self.auto_retune_cooldown_seconds
        ):
            remaining = self.auto_retune_cooldown_seconds - (now - previous)
            self._set_auto_retune_state(
                "cooldown",
                f"previous retune attempt is cooling down; retry in "
                f"{remaining:.1f} seconds",
                streak=streak,
            )
            return

        bitrate = interface.get("bitrate")
        if bitrate not in (125000, 500000):
            self._set_auto_retune_state(
                "blocked",
                f"cannot auto-retune from unsupported or unreadable bitrate "
                f"{bitrate}",
                streak=streak,
            )
            return

        self._set_auto_retune_state(
            "switching",
            f"fresh helper recheck pending from {bitrate} bit/s",
            streak=streak,
        )
        with self._lock:
            self._auto_retune_last_attempt_monotonic = now
        with self._source_lock:
            attempt = self.auto_retuner.attempt(int(bitrate))
        self._refresh_interface_status()
        attempt_state = str(attempt.get("state", "failed"))
        detail = str(attempt.get("detail", "auto-retune returned no detail"))
        self._set_auto_retune_state(
            attempt_state,
            detail,
            streak=0 if attempt_state in ("switched", "no_change") else streak,
            last_attempt=attempt,
        )

    def _collect_passive_powertrain(
        self, bus_result: AcquisitionResult
    ) -> int:
        self._passive_engine_evidence = "unknown"
        if not (
            bus_result.available
            and bus_result.bus == "c-can"
            and bus_result.acquisition == "passive"
            and self.passive_powertrain_reader is not None
        ):
            self._passive_stop_evidence_streak = 0
            self._passive_unknown_evidence_streak += 1
            return 0
        try:
            observations = self.passive_powertrain_reader.read()
        except Exception:
            self._passive_stop_evidence_streak = 0
            self._passive_unknown_evidence_streak += 1
            return 0
        accepted = 0
        rpm_evidence = None
        for observation in observations:
            result = self.publish_observation(
                observation.metric,
                value=observation.value,
                unit=observation.unit,
                source=observation.source,
                bus="c-can",
                quality=observation.quality,
            )
            accepted += int(result.available)
            if (
                observation.metric == "engine.rpm"
                and result.available
                and observation.source == "ccan.broadcast.0x0fc"
                and isinstance(observation.value, (int, float))
                and not isinstance(observation.value, bool)
            ):
                rpm_evidence = (
                    "running"
                    if float(observation.value) >= 400.0
                    else "stopped"
                )
        if rpm_evidence == "running":
            self._passive_engine_evidence = "running"
            self._passive_stop_evidence_streak = 0
            self._passive_unknown_evidence_streak = 0
        elif rpm_evidence == "stopped":
            self._passive_engine_evidence = "stopped"
            self._passive_stop_evidence_streak += 1
            self._passive_unknown_evidence_streak = 0
        else:
            self._passive_stop_evidence_streak = 0
            self._passive_unknown_evidence_streak += 1
        return accepted

    def _run_active_drive_if_ready(self) -> None:
        if not self.active_drive_enabled or self.active_drive_supervisor is None:
            return
        if self._active_drive_restoration_latched:
            self._record_active_failure(
                "restoration_failed",
                "active-drive polling is latched off after failed passive restoration",
            )
            return
        if self._passive_engine_evidence != "running":
            epoch_ended = (
                self._passive_engine_evidence == "stopped"
                and self._passive_stop_evidence_streak >= 2
            ) or (
                self._passive_engine_evidence == "unknown"
                and self._passive_unknown_evidence_streak >= 5
            )
            if (
                self._active_drive_blocked_reason is not None
                and epoch_ended
            ):
                self._active_drive_blocked_reason = None
            with self._lock:
                self._active_drive.update(
                    {
                        "state": "idle",
                        "reason": "engine_not_running",
                        "detail": (
                            "waiting for qualified passive 0x0FC engine-running "
                            "evidence; a failed running epoch is cleared only "
                            "after two explicit stopped samples or five "
                            "consecutive cycles without fresh RPM"
                        ),
                        "interface_mode": "listen_only",
                    }
                )
            self._record_active_failure(
                "engine_not_running",
                "qualified passive engine-running evidence is absent",
            )
            return
        if self._active_drive_blocked_reason is not None:
            with self._lock:
                self._active_drive.update(
                    {
                        "state": "blocked_until_engine_stop",
                        "reason": self._active_drive_blocked_reason,
                        "detail": (
                            "active-drive polling remains disabled until "
                            "qualified passive evidence observes the engine stop"
                        ),
                        "interface_mode": "listen_only",
                    }
                )
            self._record_active_failure(
                self._active_drive_blocked_reason,
                "active-drive polling remains disabled until the engine stops",
            )
            return
        with self._lock:
            self._active_drive.update(
                {
                    "state": "starting",
                    "reason": "running_gate_satisfied",
                    "detail": "starting the coordinated active-drive helper",
                    "interface_mode": "listen_only",
                    "last_event_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        outcome = self.active_drive_supervisor.run(self._collector_stop)
        try:
            self.handle_active_drive_event(outcome)
        except (KeyError, TypeError, ValueError) as exc:
            outcome = {
                "type": "final",
                "state": "restoration_failed",
                "reason": "restoration_failed",
                "detail": (
                    f"active-drive final event rejected: {exc}; the parent "
                    "could not verify final listen-only restoration"
                ),
                "restored": False,
                "interface_mode": "armed_diagnostic",
            }
            self.handle_active_drive_event(outcome)
        reason = str(outcome.get("reason", "helper_failed"))
        if outcome.get("restored") is True or reason in {
            "malformed_response",
            "response_rejected",
            "response_timeout",
            "session_required",
        }:
            self._active_drive_blocked_reason = reason
        if reason == "restoration_failed" or outcome.get("restored") is False:
            self._active_drive_restoration_latched = True
        self._refresh_interface_status()

    def _collector_loop(self) -> None:
        with self._lock:
            self._collector_state = "running"
        try:
            while not self._collector_stop.is_set():
                result = self.acquire("battery.voltage", "passive")
                self._collect_passive_powertrain(result)
                self._consider_auto_retune(result)
                self._run_active_drive_if_ready()
                with self._lock:
                    self._collector_cycles += 1
                    self._collector_last_cycle_at = datetime.now(
                        timezone.utc
                    ).isoformat()
                self._collector_stop.wait(
                    self.collector_interval_seconds
                )
        except Exception as exc:
            with self._lock:
                self._collector_state = "failed"
                self._collector_failure_detail = (
                    f"{type(exc).__name__}: {exc}"
                )
            self._record_active_failure(
                "helper_failed",
                "telemetry collector stopped after an unexpected exception: "
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            if self.active_drive_supervisor is not None:
                self.active_drive_supervisor.stop()
            with self._lock:
                if self._collector_state != "failed":
                    self._collector_state = "stopped"

    def stop_collector(self, timeout: float = 10.0) -> None:
        self._collector_stop.set()
        if self.active_drive_supervisor is not None:
            self.active_drive_supervisor.stop()
        with self._lock:
            thread = self._collector_thread
        if thread is not None:
            thread.join(timeout)
        with self._lock:
            if thread is not None and thread.is_alive():
                # Retain the non-daemon handle so a caller can inspect or retry
                # shutdown; losing it would make an armed child cleanup
                # impossible to supervise.
                self._collector_state = "stop_timeout"
            else:
                self._collector_thread = None
                if self._collector_state != "failed":
                    self._collector_state = "stopped"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve allowlisted cached vehicle telemetry over a Unix socket."
    )
    parser.add_argument("command", choices=("serve",))
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--socket-mode", default="0660")
    parser.add_argument("--channel", default="can0")
    parser.add_argument("--collector-interval", type=float, default=1.0)
    parser.add_argument("--probe-seconds", type=float, default=0.75)
    parser.add_argument("--read-timeout", type=float, default=2.0)
    parser.add_argument("--wake-min-interval", type=float, default=900.0)
    parser.add_argument(
        "--no-auto-retune",
        action="store_true",
        help="never retune between approved CAN bitrates after wrong-rate evidence",
    )
    parser.add_argument("--auto-retune-trigger", type=int, default=3)
    parser.add_argument("--auto-retune-cooldown", type=float, default=30.0)
    parser.add_argument("--no-collector", action="store_true")
    parser.add_argument(
        "--no-active-drive",
        action="store_true",
        help="disable the coordinated engine-running diagnostic collector",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.collector_interval <= 0 or args.probe_seconds <= 0:
        raise SystemExit("collector/probe intervals must be positive")
    if (
        args.read_timeout <= 0
        or args.wake_min_interval < 0
        or args.auto_retune_trigger < 1
        or args.auto_retune_cooldown < 0
    ):
        raise SystemExit("read/wake intervals are invalid")
    try:
        socket_mode = int(args.socket_mode, 8)
    except ValueError:
        raise SystemExit("--socket-mode must be an octal value") from None
    if socket_mode & 0o007:
        raise SystemExit("refusing a world-accessible broker Unix socket")

    from projects.vehicle_data.api import serve_unix
    from projects.vehicle_data.ccan_powertrain import CcanPowertrainReader

    acquirer = VoltageAcquirer(
        channel=args.channel,
        probe_seconds=args.probe_seconds,
        read_timeout=args.read_timeout,
    )
    broker_holder: dict[str, TelemetryBroker] = {}
    active_supervisor = ActiveDriveSupervisor(
        channel=args.channel,
        event_handler=lambda event: broker_holder[
            "broker"
        ].handle_active_drive_event(event),
    )
    broker = TelemetryBroker(
        acquirer=acquirer,
        collector_interval_seconds=args.collector_interval,
        wake_min_interval_seconds=args.wake_min_interval,
        auto_retune_enabled=not args.no_auto_retune,
        auto_retune_trigger=args.auto_retune_trigger,
        auto_retune_cooldown_seconds=args.auto_retune_cooldown,
        passive_powertrain_reader=CcanPowertrainReader(
            channel=args.channel,
            probe_seconds=min(args.probe_seconds, 0.25),
            read_timeout=min(args.read_timeout, 0.5),
        ),
        active_drive_supervisor=active_supervisor,
        active_drive_enabled=not args.no_active_drive,
    )
    broker_holder["broker"] = broker
    if not args.no_collector:
        broker.start_collector()
    with diagnostic_safety.interrupt_on_termination() as termination:
        try:
            serve_unix(broker, args.socket, mode=socket_mode)
        finally:
            termination.begin_cleanup()
            broker.stop_collector()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
