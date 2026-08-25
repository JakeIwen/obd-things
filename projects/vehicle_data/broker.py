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
DEFAULT_SOCKET = "/run/van-telemetry/api.sock"
DEFAULT_HISTORY_DB = REPO / "tmp" / "vehicle_data" / "history.sqlite3"
DEFAULT_DTC_CACHE = REPO / "tmp" / "vehicle_data" / "dtc-cache.json"
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
        "pcm.did.06da",
        "rf_hub.did.31d0",
        "rf_hub.did.31d1",
        "rf_hub.did.31d2",
        "rf_hub.did.31d3",
    }
)
ACTIVE_DRIVE_OPTIONAL_METRICS = frozenset(
    {"engine.crankshaft_torque"}
)
DERIVED_POWER_METRIC = "engine.crankshaft_power"
DERIVED_POWER_SOURCE = "derived.pcm_06da_x_ccan_0x0fc"
DERIVED_POWER_INPUT_MAX_SKEW_SECONDS = 1.5
LBFT_RPM_PER_HORSEPOWER = 5252.113122
TRANSMISSION_OIL_TEMPERATURE_METRIC = "transmission.oil_temperature"
MAX_TRANSMISSION_TEMPERATURE_DELTA_F = 18.0
TRANSMISSION_TEMPERATURE_DELTA_WINDOW_SECONDS = 1.0
MAX_RECENT_DATA_QUALITY_EVENTS = 32
USB_REMOVAL_EVENT_KINDS = frozenset(
    (
        "usb_parent_hub_removed",
        "usb_can_adapter_removed",
        "usb_can_netdev_removed",
    )
)
USB_NONREMOVAL_EVENT_KINDS = frozenset(
    (
        "usb_parent_hub_added",
        "usb_can_adapter_added",
        "usb_can_netdev_added",
        "usb_can_recovered",
    )
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
class ActiveDriveSupervisor:
    """Supervise the termination-safe active-drive owner subprocess."""

    def __init__(
        self,
        *,
        channel: str,
        event_handler,
        expected_usb_serial: str,
        expected_dev_id: int,
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
        self.expected_usb_serial = expected_usb_serial
        self.expected_dev_id = expected_dev_id
        if (
            not isinstance(self.expected_usb_serial, str)
            or not self.expected_usb_serial
        ):
            raise ValueError("active-drive USB serial is required")
        if (
            not isinstance(self.expected_dev_id, int)
            or isinstance(self.expected_dev_id, bool)
            or self.expected_dev_id < 0
        ):
            raise ValueError("active-drive dev_id must be a non-negative integer")
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
        command.extend(
            [
                "--expected-usb-serial",
                self.expected_usb_serial,
                "--expected-dev-id",
                hex(self.expected_dev_id),
            ]
        )
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
        acquisition_wait_seconds: float = 20.0,
        passive_powertrain_reader=None,
        active_drive_supervisor=None,
        active_drive_enabled: bool = False,
        interface_reconciler=None,
        insights=None,
        history_interval_seconds: float = 5.0,
        usb_can_monitor=None,
    ):
        if acquirer is None:
            raise ValueError(
                "telemetry broker requires an explicit serial-role-aware acquirer"
            )
        self.acquirer = acquirer
        self.definitions = definitions or METRICS
        self.monotonic = monotonic
        self.collector_interval_seconds = collector_interval_seconds
        self.acquisition_wait_seconds = acquisition_wait_seconds
        if history_interval_seconds <= 0:
            raise ValueError("history interval must be positive")
        self.passive_powertrain_reader = passive_powertrain_reader
        self.active_drive_supervisor = active_drive_supervisor
        self.active_drive_enabled = bool(
            active_drive_enabled and active_drive_supervisor is not None
        )
        self.interface_reconciler = interface_reconciler
        self.insights = insights
        self.history_interval_seconds = history_interval_seconds
        self.usb_can_monitor = usb_can_monitor

        self._lock = threading.RLock()
        # Prevent the passive collector from racing a client-triggered active
        # request before the cross-process observer/exclusive lock is reached.
        self._source_lock = threading.Lock()
        self._cache: dict[str, AcquisitionResult] = {}
        self._last_error: dict[str, AcquisitionResult] = {}
        self._last_attempt: dict[tuple[str, str], float] = {}
        self._inflight: dict[tuple[str, str], _Inflight] = {}
        self._interface_status: dict[str, object] = {
            "channel": getattr(self.acquirer, "channel", "c-can-unresolved"),
            "adapter_present": None,
            "up": None,
            "bitrate": None,
            "fd_enabled": None,
            "one_shot": None,
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
        self._history_thread: threading.Thread | None = None
        self._history_stop = threading.Event()
        self._insights_closed = False
        self._history_sequence = 0
        self._history_recorder: dict[str, object] = {
            "enabled": insights is not None,
            "state": "stopped" if insights is not None else "disabled",
            "interval_seconds": history_interval_seconds,
            "snapshots_stored": 0,
            "last_stored_at": None,
            "last_error": None,
        }
        self._interface_reconcile: dict[str, object] = {
            "enabled": interface_reconciler is not None,
            "ready": None,
            "detail": "waiting for serial-aware passive interface reconciliation",
        }
        self._passive_engine_evidence = "unknown"
        self._passive_stop_evidence_streak = 0
        self._passive_unknown_evidence_streak = 0
        self._active_drive_blocked_reason: str | None = None
        self._active_drive_restoration_latched = False
        self._wake_authorization_deadline: float | None = None
        self._wake_authorization_owner: int | None = None
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
        self._data_quality_sequence = 0
        self._active_data_quality: dict[str, dict[str, object]] = {}
        self._recent_data_quality: list[dict[str, object]] = []
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

    def history_response(self) -> dict[str, object]:
        """Return bounded SQLite-derived history without touching CAN."""
        if self.insights is None:
            return {
                "available": False,
                "reason": "history_disabled",
                "detail": "telemetry history is disabled by broker configuration",
            }
        try:
            return self.insights.history_response()
        except Exception as exc:
            return {
                "available": False,
                "reason": "history_unavailable",
                "detail": f"history query failed: {type(exc).__name__}: {exc}",
            }

    def health_response(self) -> dict[str, object]:
        """Return explainable, history-relative advisory assessments only."""
        if self.insights is None:
            return {
                "available": False,
                "reason": "early_warning_disabled",
                "detail": "early-warning evaluation is disabled by broker configuration",
            }
        try:
            return self.insights.health_response()
        except Exception as exc:
            return {
                "available": False,
                "reason": "early_warning_unavailable",
                "detail": (
                    "early-warning evaluation failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }

    def dtc_response(self) -> dict[str, object]:
        """Return the saved DTC cache; this method cannot initiate a scan."""
        if self.insights is None:
            return {
                "available": False,
                "reason": "dtc_cache_disabled",
                "detail": "saved DTC cache access is disabled by broker configuration",
            }
        try:
            return self.insights.dtc_response()
        except Exception as exc:
            return {
                "available": False,
                "reason": "dtc_cache_unavailable",
                "detail": f"saved DTC cache read failed: {type(exc).__name__}: {exc}",
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

    def _plausibility_error(
        self,
        metric: str,
        result: AcquisitionResult,
    ) -> str | None:
        """Reject physically impossible time derivatives before cache/history."""

        if metric != TRANSMISSION_OIL_TEMPERATURE_METRIC:
            return None
        if (
            result.source != "ccan.broadcast.0x1f7"
            or result.unit != "°F"
            or result.quality != "observed_alfa_scale"
            or result.observed_monotonic is None
            or not isinstance(result.value, (int, float))
            or isinstance(result.value, bool)
        ):
            return None
        with self._lock:
            previous = self._cache.get(metric)
        if (
            previous is None
            or not previous.available
            or previous.source != result.source
            or previous.unit != result.unit
            or previous.quality != result.quality
            or previous.observed_monotonic is None
            or not isinstance(previous.value, (int, float))
            or isinstance(previous.value, bool)
        ):
            return None
        elapsed = result.observed_monotonic - previous.observed_monotonic
        delta_f = abs(float(result.value) - float(previous.value))
        if elapsed < 0:
            return (
                "transmission-oil temperature arrived with an older "
                "monotonic observation timestamp"
            )
        if elapsed == 0:
            return (
                "transmission-oil temperature changed without a newer "
                "monotonic observation timestamp"
                if delta_f > 0
                else None
            )
        # OEM P0711 plausibility evidence is a delta criterion, not a general
        # rate limit: more than 10 °C within *less than* one second.  A large
        # legitimate warm-up/cool-down delta over a longer sampling gap is not
        # rejected here.
        if elapsed >= TRANSMISSION_TEMPERATURE_DELTA_WINDOW_SECONDS:
            return None
        if delta_f <= MAX_TRANSMISSION_TEMPERATURE_DELTA_F:
            return None
        delta_c = delta_f * 5.0 / 9.0
        return (
            f"transmission-oil temperature changed {delta_f:.3f} °F "
            f"({delta_c:.3f} °C) in {elapsed:.3f}s, exceeding the OEM-context "
            "10 °C delta within less than one second"
        )

    @staticmethod
    def _data_quality_key(metric: str, source: str, reason: str) -> str:
        return f"{metric}|{source}|{reason}"

    def _record_data_quality_rejection(
        self,
        *,
        metric: str,
        source: str,
        reason: str,
        detail: str,
        interface_mode: str,
        rejection_count: int = 1,
        evidence: dict[str, float] | None = None,
    ) -> None:
        """Latch and deduplicate acquisition-quality evidence in memory.

        The last good metric stays cached.  This path never changes interface
        state, creates an inhibit, or feeds the advisory notification outbox.
        """

        definition = self.definitions.get(metric)
        if definition is None:
            raise ValueError("data-quality event metric is not registered")
        source_definition = self._source_for(definition, source)
        if source_definition is None:
            raise ValueError("data-quality event source is not registered")
        if reason != "implausible_transition":
            raise ValueError("data-quality event reason is not allowlisted")
        if not isinstance(detail, str) or not detail or len(detail) > 4000:
            raise ValueError("data-quality event detail must be bounded text")
        if (
            not isinstance(rejection_count, int)
            or isinstance(rejection_count, bool)
            or not 1 <= rejection_count <= 10000
        ):
            raise ValueError("data-quality rejection count is out of bounds")
        if interface_mode not in ("listen_only", "armed_diagnostic"):
            raise ValueError("data-quality event interface mode is invalid")

        now = datetime.now(timezone.utc).isoformat()
        key = self._data_quality_key(metric, source, reason)
        with self._lock:
            incident = self._active_data_quality.get(key)
            if incident is None:
                self._data_quality_sequence += 1
                incident = {
                    "incident_id": (
                        f"{self._started_at}:{self._data_quality_sequence}"
                    ),
                    "producer_instance": self._started_at,
                    "metric": metric,
                    "source": source,
                    "bus": source_definition.bus,
                    "quality": source_definition.quality,
                    "reason": reason,
                    "status": "active",
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "resolved_at": None,
                    "resolution_reason": None,
                    "rejection_count": rejection_count,
                    "detail": detail,
                    "interface_mode": interface_mode,
                    "evidence": dict(evidence or {}),
                    "notification_eligible": False,
                }
                self._active_data_quality[key] = incident
                self._recent_data_quality.append(incident)
                del self._recent_data_quality[:-MAX_RECENT_DATA_QUALITY_EVENTS]
            else:
                incident["last_seen_at"] = now
                incident["rejection_count"] = int(
                    incident.get("rejection_count", 0)
                ) + rejection_count
                incident["detail"] = detail
                incident["interface_mode"] = interface_mode
                incident["evidence"] = dict(evidence or {})
            self._last_error[metric] = failure(
                metric=metric,
                unit=definition.unit,
                reason=reason,
                detail=detail,
                bus=source_definition.bus,
                acquisition=source_definition.acquisition_class,
                interface_mode=interface_mode,
            )

    def _resolve_data_quality(self, metric: str, source: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            keys = [
                key
                for key, incident in self._active_data_quality.items()
                if incident.get("metric") == metric
                and incident.get("source") == source
            ]
            for key in keys:
                incident = self._active_data_quality.pop(key)
                incident["status"] = "resolved"
                incident["resolved_at"] = now
                incident["resolution_reason"] = "authoritative_good_sample"
            last_error = self._last_error.get(metric)
            if (
                keys
                and last_error is not None
                and last_error.reason == "implausible_transition"
            ):
                self._last_error.pop(metric, None)

    def _handle_data_quality_event(self, event: dict[str, object]) -> None:
        if event.get("interface_mode") != "armed_diagnostic":
            raise ValueError(
                "active data-quality event must report armed_diagnostic mode"
            )
        if event.get("metric") != TRANSMISSION_OIL_TEMPERATURE_METRIC:
            raise ValueError("active data-quality metric is not allowlisted")
        if event.get("source") != "ccan.broadcast.0x1f7":
            raise ValueError("active data-quality source is not allowlisted")
        if event.get("bus") != "c-can":
            raise ValueError("active data-quality bus is not allowlisted")
        if event.get("quality") != "observed_alfa_scale":
            raise ValueError("active data-quality evidence tier is invalid")
        reason = event.get("reason")
        detail = event.get("detail")
        rejection_count = event.get("rejection_count")
        if reason != "implausible_transition":
            raise ValueError("active data-quality reason is not allowlisted")
        if not isinstance(detail, str) or not detail:
            raise ValueError("active data-quality detail must be nonempty text")
        if (
            not isinstance(rejection_count, int)
            or isinstance(rejection_count, bool)
        ):
            raise ValueError("active data-quality rejection count must be an integer")
        numbers: dict[str, float] = {}
        for field_name in (
            "previous_value_c",
            "rejected_value_c",
            "delta_c",
            "elapsed_seconds",
        ):
            value = event.get(field_name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
            ):
                raise ValueError(
                    f"active data-quality {field_name} must be finite"
                )
            numbers[field_name] = float(value)
        if numbers["delta_c"] <= 10.0:
            raise ValueError(
                "active temperature rejection must exceed the 10 C criterion"
            )
        if not 0.0 < numbers["elapsed_seconds"] < 1.0:
            raise ValueError(
                "active temperature rejection must occur within less than one second"
            )
        calculated_delta = abs(
            numbers["rejected_value_c"] - numbers["previous_value_c"]
        )
        if not math.isclose(
            calculated_delta,
            numbers["delta_c"],
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            raise ValueError("active temperature rejection delta is inconsistent")
        self._record_data_quality_rejection(
            metric=TRANSMISSION_OIL_TEMPERATURE_METRIC,
            source="ccan.broadcast.0x1f7",
            reason=reason,
            detail=detail,
            interface_mode="armed_diagnostic",
            rejection_count=rejection_count,
            evidence=numbers,
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
            plausibility_error = self._plausibility_error(metric, result)
            if plausibility_error is not None:
                rejected = failure(
                    metric=metric,
                    unit=definition.unit,
                    reason="implausible_transition",
                    detail=plausibility_error,
                    bus=source_definition.bus,
                    acquisition=source_definition.acquisition_class,
                    interface_mode=result.interface_mode,
                )
                self._record_data_quality_rejection(
                    metric=metric,
                    source=source_definition.name,
                    reason="implausible_transition",
                    detail=plausibility_error,
                    interface_mode=result.interface_mode or "listen_only",
                )
            else:
                self._cache[metric] = result
                # Publisher success does not prove that a prior in-process CAN
                # acquisition failure (for example can_busy or restoration_failed)
                # has recovered. Keep that diagnostic until the acquirer itself
                # completes successfully.
                if (
                    self._last_error.get(metric) is not None
                    and self._last_error[metric].reason
                    == "implausible_transition"
                ):
                    # This rejection is produced by this same publisher admission
                    # path, so a later plausible sample does prove its recovery.
                    self._last_error.pop(metric, None)
                if metric == TRANSMISSION_OIL_TEMPERATURE_METRIC:
                    self._resolve_data_quality(metric, source_definition.name)
        if plausibility_error is not None:
            return rejected
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
            plausibility_error = self._plausibility_error(metric, result)
            if plausibility_error is not None:
                rejected = failure(
                    metric=metric,
                    unit=definition.unit,
                    reason="implausible_transition",
                    detail=plausibility_error,
                    bus=source.bus,
                    acquisition=source.acquisition_class,
                    interface_mode="armed_diagnostic",
                )
                self._record_data_quality_rejection(
                    metric=metric,
                    source=source.name,
                    reason="implausible_transition",
                    detail=plausibility_error,
                    interface_mode="armed_diagnostic",
                )
            else:
                self._cache[metric] = result
                self._last_error.pop(metric, None)
                if metric == TRANSMISSION_OIL_TEMPERATURE_METRIC:
                    self._resolve_data_quality(metric, source.name)
        if plausibility_error is not None:
            return
        self._update_vehicle_state(result)
        if metric == "engine.crankshaft_torque":
            self._refresh_derived_power()

    def _refresh_derived_power(self) -> None:
        """Derive ECU-estimated crankshaft horsepower from exact fresh inputs."""

        definition = self.definitions.get(DERIVED_POWER_METRIC)
        if definition is None:
            return
        source = self._source_for(definition, DERIVED_POWER_SOURCE)
        if source is None:
            raise ValueError("derived crankshaft-power source is not registered")
        with self._lock:
            rpm = self._cache.get("engine.rpm")
            torque = self._cache.get("engine.crankshaft_torque")
            had_power = DERIVED_POWER_METRIC in self._cache

        reason = None
        detail = None
        if rpm is None or torque is None:
            reason = "source_unavailable"
            detail = "fresh RPM and current crankshaft torque are both required"
        elif not rpm.available or not torque.available:
            reason = "source_unavailable"
            detail = "one or more derived-power inputs are unavailable"
        elif (
            rpm.source != "ccan.broadcast.0x0fc"
            or rpm.unit != "rpm"
            or rpm.quality != "observed_alfa_scale"
            or torque.source != "pcm.did.06da"
            or torque.unit != "lb-ft"
            or torque.quality != "observed_alfa_scale"
        ):
            reason = "invalid_source_result"
            detail = "derived power inputs do not match the exact approved sources"
        elif (
            rpm.interface_mode != "armed_diagnostic"
            or torque.interface_mode != "armed_diagnostic"
        ):
            reason = "source_unavailable"
            detail = "derived power requires one coordinated armed observation interval"
        elif rpm.observed_monotonic is None or torque.observed_monotonic is None:
            reason = "source_unavailable"
            detail = "derived power inputs lack monotonic observation timestamps"
        elif not (
            isinstance(rpm.value, (int, float))
            and not isinstance(rpm.value, bool)
            and isinstance(torque.value, (int, float))
            and not isinstance(torque.value, bool)
        ):
            reason = "invalid_source_result"
            detail = "derived power inputs must be finite numeric observations"
        else:
            now = self.monotonic()
            rpm_age = max(0.0, now - rpm.observed_monotonic)
            torque_age = max(0.0, now - torque.observed_monotonic)
            skew = abs(rpm.observed_monotonic - torque.observed_monotonic)
            torque_stale_after = self.definitions[
                "engine.crankshaft_torque"
            ].stale_after_seconds
            if (
                rpm_age > self.definitions["engine.rpm"].stale_after_seconds
                or torque_age > torque_stale_after
            ):
                reason = "source_unavailable"
                detail = "derived power input is older than its registered freshness limit"
            elif skew > DERIVED_POWER_INPUT_MAX_SKEW_SECONDS:
                reason = "source_unavailable"
                detail = (
                    f"RPM/torque observation skew {skew:.3f}s exceeds "
                    f"{DERIVED_POWER_INPUT_MAX_SKEW_SECONDS:.3f}s"
                )
            else:
                value = (
                    float(torque.value)
                    * float(rpm.value)
                    / LBFT_RPM_PER_HORSEPOWER
                )
                value_error = self._value_error(definition, value)
                if value_error is not None:
                    reason = "invalid_source_result"
                    detail = value_error
                else:
                    # Derived freshness is constrained by the oldest input.
                    # Select wall and monotonic timestamps from that same input
                    # so an NTP adjustment cannot create mismatched provenance.
                    oldest_input = (
                        rpm
                        if rpm.observed_monotonic
                        <= torque.observed_monotonic
                        else torque
                    )
                    result = success(
                        metric=definition.name,
                        unit=definition.unit,
                        value=value,
                        source=source.name,
                        bus=source.bus,
                        acquisition=source.acquisition_class,
                        quality=source.quality,
                        observed_monotonic=oldest_input.observed_monotonic,
                        observed_at=oldest_input.observed_at,
                        detail=(
                            "ECU-estimated crankshaft power from current PCM "
                            f"torque and passive RPM; input skew {skew:.3f}s; "
                            "not measured wheel horsepower"
                        ),
                        interface_mode="armed_diagnostic",
                    )
                    with self._lock:
                        self._cache[definition.name] = result
                        self._last_error.pop(definition.name, None)
                    return

        # A torque-triggered attempt with both inputs present must expose a
        # concrete pairing/freshness/source error even if this is the first
        # power result of the epoch.  Stay silent only while an input has not
        # arrived at all.
        if not had_power and (rpm is None or torque is None):
            return
        self._invalidate_derived_power(
            reason or "source_unavailable",
            detail or "derived power inputs are unavailable",
        )

    def _invalidate_derived_power(self, reason: str, detail: str) -> None:
        definition = self.definitions.get(DERIVED_POWER_METRIC)
        if definition is None:
            return
        source = self._source_for(definition, DERIVED_POWER_SOURCE)
        if source is None:
            return
        result = failure(
            metric=definition.name,
            unit=definition.unit,
            reason=reason,
            detail=detail,
            bus=source.bus,
            acquisition=source.acquisition_class,
            interface_mode="armed_diagnostic",
        )
        with self._lock:
            self._cache.pop(definition.name, None)
            self._last_error[definition.name] = result

    def _record_active_failure(
        self,
        reason: str,
        detail: str,
        *,
        interface_mode: str = "listen_only",
    ) -> None:
        if reason not in ACTIVE_DRIVE_FAILURE_REASONS:
            reason = "helper_failed"
        with self._lock:
            # Engine-running PCM metrics must become inactive immediately when
            # their diagnostic owner stops. Do not leave a still-fresh pre-stop
            # sample looking live for the remainder of its TTL.
            for metric in (
                "generator.field_duty",
                "engine.crankshaft_torque",
                DERIVED_POWER_METRIC,
            ):
                definition = self.definitions.get(metric)
                if definition is None:
                    continue
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
                self._cache.pop(definition.name, None)
                self._last_error[definition.name] = result

    def _record_active_metric_failure(
        self,
        event: dict[str, object],
    ) -> None:
        if event.get("interface_mode") != "armed_diagnostic":
            raise ValueError(
                "active metric failure must report armed_diagnostic mode"
            )
        metric = event.get("metric")
        source_name = event.get("source")
        if metric not in ACTIVE_DRIVE_OPTIONAL_METRICS:
            raise ValueError(
                "active metric failure is outside the optional metric allowlist"
            )
        if source_name not in ACTIVE_DRIVE_SOURCES:
            raise ValueError(
                "active metric failure source is outside the helper allowlist"
            )
        definition = self.definitions.get(metric)
        if definition is None:
            raise ValueError("active metric failure metric is not registered")
        source = self._source_for(definition, source_name)
        if source is None:
            raise ValueError(
                "active metric failure source is not registered for metric"
            )
        if event.get("unit") != definition.unit:
            raise ValueError(
                "active metric failure unit does not match registry"
            )
        if (
            event.get("bus") != source.bus
            or event.get("quality") != source.quality
        ):
            raise ValueError(
                "active metric failure source metadata does not match registry"
            )
        reason = event.get("reason")
        detail = event.get("detail")
        if reason not in ACTIVE_DRIVE_FAILURE_REASONS:
            raise ValueError(
                "active metric failure reason is not allowlisted"
            )
        if not isinstance(detail, str):
            raise ValueError("active metric failure detail must be a string")
        result = failure(
            metric=definition.name,
            unit=definition.unit,
            reason=reason,
            detail=detail,
            bus=source.bus,
            acquisition=source.acquisition_class,
            interface_mode="armed_diagnostic",
        )
        with self._lock:
            self._cache.pop(definition.name, None)
            self._last_error[definition.name] = result
        if metric == "engine.crankshaft_torque":
            self._invalidate_derived_power(reason, detail)

    def handle_active_drive_event(self, event: dict[str, object]) -> None:
        """Validate one trusted helper-pipe event; never expose this as an API."""
        event_type = event.get("type")
        if event_type == "observation":
            if event.get("interface_mode") != "armed_diagnostic":
                raise ValueError("active observation must report armed_diagnostic mode")
            self._store_active_observation(event)
            return
        if event_type == "metric_failure":
            self._record_active_metric_failure(event)
            return
        if event_type == "quality_event":
            self._handle_data_quality_event(event)
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
                    channel="*",
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
                "channel": getattr(
                    self.acquirer, "channel", "c-can-unresolved"
                ),
                "adapter_present": None,
                "up": None,
                "bitrate": None,
                "fd_enabled": None,
                "one_shot": None,
                "listen_only": None,
                "controller_state": None,
                "topology": {
                    "bus": "unknown",
                    "usable": False,
                    "reason": f"status snapshot failed: {exc}",
                },
                "active_inhibits": ["status-unavailable"],
            }
        if self.usb_can_monitor is not None:
            try:
                self.usb_can_monitor.reconcile(snapshot)
            except Exception:
                # USB incident evidence must never interfere with the normal
                # role-status path.  The monitor reports its own degraded
                # state and the next read-only role snapshot retries recovery.
                pass
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
            history_recorder = json.loads(
                json.dumps(self._history_recorder)
            )
            active_drive = json.loads(json.dumps(self._active_drive))
            accepted_temperature = self._cache.get(
                TRANSMISSION_OIL_TEMPERATURE_METRIC
            )
            temperature_quality_active = any(
                incident.get("metric") == TRANSMISSION_OIL_TEMPERATURE_METRIC
                and incident.get("source") == "ccan.broadcast.0x1f7"
                for incident in self._active_data_quality.values()
            )
            authoritative_good = []
            if (
                accepted_temperature is not None
                and accepted_temperature.available
                and accepted_temperature.source == "ccan.broadcast.0x1f7"
                and not temperature_quality_active
            ):
                authoritative_good.append(
                    {
                        "metric": TRANSMISSION_OIL_TEMPERATURE_METRIC,
                        "source": "ccan.broadcast.0x1f7",
                        "observed_at": (
                            accepted_temperature.observed_at.isoformat()
                            if accepted_temperature.observed_at is not None
                            else None
                        ),
                    }
                )
            data_quality = {
                "producer_instance": self._started_at,
                "active_count": len(self._active_data_quality),
                "active": json.loads(
                    json.dumps(list(self._active_data_quality.values()))
                ),
                "recent": json.loads(
                    json.dumps(self._recent_data_quality)
                ),
                "authoritative_good": authoritative_good,
                "notification_delivery": "disabled_by_design",
                "detail": (
                    "Raw sample rejections are data-quality evidence, not "
                    "vehicle-health advisories, and are never notified."
                ),
            }
            interface_reconcile = json.loads(
                json.dumps(self._interface_reconcile)
            )
            vehicle_state = json.loads(json.dumps(self._vehicle_state))
            vehicle_observed = self._vehicle_state_observed_monotonic
        if self.usb_can_monitor is None:
            usb_can_monitor = {
                "enabled": False,
                "state": "disabled",
                "receive_only": True,
                "hardware_actions": False,
                "detail": "kernel USB/CAN incident monitoring is disabled",
            }
        else:
            try:
                usb_can_monitor = self.usb_can_monitor.status_snapshot()
            except Exception as exc:
                usb_can_monitor = {
                    "enabled": True,
                    "state": "degraded",
                    "receive_only": True,
                    "hardware_actions": False,
                    "last_error": f"monitor status failed: {type(exc).__name__}: {exc}",
                }
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
            role_snapshot = interface.get("role_interfaces")
            role_snapshot = (
                role_snapshot if isinstance(role_snapshot, dict) else None
            )
            role_payloads = (
                role_snapshot.get("roles")
                if isinstance(role_snapshot, dict)
                else None
            )
            ccan_role = (
                role_payloads.get("c-can")
                if isinstance(role_payloads, dict)
                else None
            )
            if (
                isinstance(ccan_role, dict)
                and ccan_role.get("channel") == interface.get("channel")
            ):
                actual = ccan_role.get("actual")
                if isinstance(actual, dict):
                    actual["listen_only"] = False
                    actual["mode"] = "armed_diagnostic"
                ccan_role["passive_ready"] = False
                ccan_role["operating_mode"] = "armed_diagnostic"
                ccan_role["topology_usable"] = bool(topology.get("usable"))
                ccan_role["reason"] = (
                    "broker active-drive owner has the resolved C-CAN channel "
                    "armed for reviewed diagnostics"
                )
                role_snapshot["ready"] = False
        else:
            interface["mode"] = (
                "listen_only"
                if interface.get("listen_only") is True
                else "armed_or_unknown"
            )
        active_permitted = bool(
            interface.get("adapter_present")
            and interface.get("up")
            and interface.get("fd_enabled") is False
            and interface.get("one_shot") is False
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
            "history_recorder": history_recorder,
            "usb_can_monitor": usb_can_monitor,
            "data_quality": data_quality,
            "active_drive": active_drive,
            "interface_reconcile": interface_reconcile,
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
        if (
            result.available
            and result.metric == "vehicle.ignition_on"
            and result.value is True
        ):
            with self._lock:
                current_basis = self._vehicle_state.get("basis")
                current_observed = self._vehicle_state_observed_monotonic
            rpm_definition = self.definitions.get("engine.rpm")
            if (
                current_basis == "qualified_ccan_0x0fc_engine_speed"
                and current_observed is not None
                and rpm_definition is not None
                and self.monotonic() - current_observed
                <= rpm_definition.stale_after_seconds
            ):
                # 0x2EF proves ignition presence, but fresh qualified RPM
                # evidence is stronger and has already distinguished running
                # from ignition-on/engine-off.
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
            state = {
                "state": "awake",
                "running": None,
                "confidence": "observed",
                "basis": "passive_bus_activity",
                "detail": (
                    f"{result.bus or 'vehicle bus'} traffic is present; "
                    "running versus ignition-on versus a temporary wake is "
                    "not yet distinguished"
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

    def _wake_state_conflicts(self, *, require_fresh: bool) -> tuple[str, ...]:
        now = self.monotonic()
        with self._lock:
            active_drive = dict(self._active_drive)
            vehicle_state = dict(self._vehicle_state)
            restoration_latched = self._active_drive_restoration_latched
            collector_state = self._collector_state
            vehicle_observed = self._vehicle_state_observed_monotonic
        conflicts: list[str] = []
        if collector_state != "running":
            conflicts.append("passive collector is not running")
        freshness_limit = max(3.0, self.collector_interval_seconds * 3.0)
        if require_fresh and (
            vehicle_observed is None or now - vehicle_observed > freshness_limit
        ):
            conflicts.append("passive vehicle-state evidence is missing or stale")
        if restoration_latched or active_drive.get("restoration_failed"):
            conflicts.append("active-drive passive restoration is latched failed")
        active_state = active_drive.get("state")
        active_mode = active_drive.get("interface_mode")
        if active_state not in ("idle", "disabled"):
            conflicts.append("broker active-drive state is not idle or disabled")
        if active_mode != "listen_only":
            conflicts.append("broker active-drive interface mode is not listen-only")
        if active_drive.get("helper_pid") is not None:
            conflicts.append("broker active-drive helper is still present")
        if (
            vehicle_state.get("running") is True
            or vehicle_state.get("state") in ("running", "ignition_on")
        ):
            conflicts.append(
                "broker has verified running or ignition-on vehicle evidence"
            )
        elif vehicle_state.get("state") not in ("asleep", "awake", "parked"):
            conflicts.append("broker vehicle state is not safe for a parked wake")
        return tuple(conflicts)

    def _begin_wake_authorization(self) -> tuple[str, ...]:
        conflicts = self._wake_state_conflicts(require_fresh=True)
        if conflicts:
            return conflicts
        with self._lock:
            # The passive-first fixed transaction is bounded below twelve
            # seconds (two passive probes, one final role probe, 1.5 s burst,
            # signature/0x46C validation, and exact restoration).  The
            # deadline is established once and is never extended by callbacks.
            self._wake_authorization_owner = threading.get_ident()
            self._wake_authorization_deadline = self.monotonic() + 12.0
        return ()

    def _end_wake_authorization(self) -> None:
        with self._lock:
            if self._wake_authorization_owner == threading.get_ident():
                self._wake_authorization_owner = None
                self._wake_authorization_deadline = None

    def wake_prearm_conflicts(self) -> tuple[str, ...]:
        """Return broker state which forbids a fixed parked wake.

        This callback is re-run by ``lib.can_wake`` while it owns the exact
        role/channel, immediately before link mutation and again before TX.
        Cross-process observers and active owners are enforced separately by
        the exclusive role/channel locks.
        """

        with self._lock:
            owner = self._wake_authorization_owner
            deadline = self._wake_authorization_deadline
        if owner != threading.get_ident() or deadline is None:
            return ("bounded broker wake authorization is not active",)
        if self.monotonic() > deadline:
            return ("bounded broker wake authorization expired",)
        return self._wake_state_conflicts(require_fresh=False)

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
                minimum = (
                    definition.wake_min_interval_seconds
                    if mode == "wake_if_asleep"
                    else definition.passive_min_interval_seconds
                )
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
                    wake_authorized = False
                    if mode == "wake_if_asleep":
                        conflicts = self._begin_wake_authorization()
                        if conflicts:
                            result = failure(
                                metric=metric,
                                unit=definition.unit,
                                reason="can_busy",
                                detail="; ".join(conflicts),
                                bus="b-can",
                                acquisition=mode,
                            )
                        else:
                            wake_authorized = True
                    try:
                        if result is None:
                            result = self.acquirer.acquire(mode)
                    finally:
                        if wake_authorized:
                            self._end_wake_authorization()
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
        self.start_usb_monitor()
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
                # daemon thread could be discarded while C-CAN is still armed.
                daemon=False,
            )
            self._collector_thread = thread
            thread.start()
            if self.insights is not None and self._history_thread is None:
                self._history_stop.clear()
                history_thread = threading.Thread(
                    target=self._history_loop,
                    name="van-telemetry-history",
                    daemon=False,
                )
                self._history_thread = history_thread
                self._history_recorder["state"] = "starting"
                history_thread.start()

    def start_usb_monitor(self) -> None:
        """Start only the receive-only kernel observer, when configured."""

        if self.usb_can_monitor is None:
            return
        try:
            self.usb_can_monitor.start()
        except Exception:
            # The broker remains useful when kernel event observation is not
            # available.  Monitor status carries the degraded reason.
            pass

    def _history_loop(self) -> None:
        """Record broker cache independently of a long active-drive owner."""
        with self._lock:
            self._history_recorder["state"] = "running"
        try:
            while not self._history_stop.is_set():
                captured_at = datetime.now(timezone.utc)
                with self._lock:
                    self._history_sequence += 1
                    sequence = self._history_sequence
                monitor_batch = None
                try:
                    history_snapshot = self.snapshot_response()
                    if self.usb_can_monitor is not None:
                        monitor_batch = self.usb_can_monitor.persistence_batch()
                        history_snapshot["_usb_can_monitor"] = monitor_batch
                    result = self.insights.ingest_snapshot(
                        history_snapshot,
                        captured_at=captured_at,
                        ingest_key=f"broker:{self._started_at}:{sequence}",
                    )
                except Exception as exc:
                    with self._lock:
                        self._history_recorder["state"] = "degraded"
                        self._history_recorder["last_error"] = (
                            f"{type(exc).__name__}: {exc}"
                        )
                else:
                    checkpoint_complete = (
                        getattr(result, "advisory_checkpoint_complete", None)
                        is True
                    )
                    if (
                        checkpoint_complete
                        and self.usb_can_monitor is not None
                        and isinstance(monitor_batch, dict)
                    ):
                        consumed_removals = set(
                            getattr(
                                result,
                                "advisory_consumed_event_ids",
                                (),
                            )
                        )
                        event_ids = [
                            event.get("event_id")
                            for event in monitor_batch.get("events", [])
                            if isinstance(event, dict)
                            and isinstance(event.get("event_id"), str)
                            and (
                                event.get("kind")
                                in USB_NONREMOVAL_EVENT_KINDS
                                or (
                                    event.get("kind")
                                    in USB_REMOVAL_EVENT_KINDS
                                    and event.get("event_id")
                                    in consumed_removals
                                )
                            )
                        ]
                        if event_ids:
                            self.usb_can_monitor.acknowledge_events(event_ids)
                    with self._lock:
                        self._history_recorder["state"] = (
                            "running" if checkpoint_complete else "degraded"
                        )
                        self._history_recorder["last_error"] = (
                            None
                            if checkpoint_complete
                            else getattr(
                                result,
                                "advisory_checkpoint_error",
                                "advisory checkpoint incomplete",
                            )
                        )
                        if not getattr(result, "duplicate", False):
                            self._history_recorder["snapshots_stored"] = int(
                                self._history_recorder["snapshots_stored"]
                            ) + 1
                        self._history_recorder["last_stored_at"] = getattr(
                            result, "captured_at", captured_at.isoformat()
                        )
                self._history_stop.wait(self.history_interval_seconds)
        finally:
            with self._lock:
                self._history_recorder["state"] = "stopped"

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
        drainer = getattr(
            self.passive_powertrain_reader,
            "drain_quality_events",
            None,
        )
        quality_events = drainer() if callable(drainer) else ()
        for event in quality_events:
            self._record_data_quality_rejection(
                metric=event.metric,
                source=event.source,
                reason=event.reason,
                detail=event.detail,
                interface_mode="listen_only",
                rejection_count=event.rejection_count,
                evidence={
                    "previous_value_c": event.previous_value_c,
                    "rejected_value_c": event.rejected_value_c,
                    "delta_c": event.delta_c,
                    "elapsed_seconds": event.elapsed_seconds,
                },
            )
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
                if self.interface_reconciler is not None:
                    try:
                        reconcile = self.interface_reconciler.reconcile_if_needed()
                    except Exception as exc:
                        reconcile = {
                            "enabled": True,
                            "ready": False,
                            "detail": (
                                "serial-aware passive reconciliation failed "
                                f"closed: {type(exc).__name__}: {exc}"
                            ),
                        }
                    with self._lock:
                        self._interface_reconcile = reconcile
                result = self.acquire("battery.voltage", "passive")
                self._collect_passive_powertrain(result)
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
        self._history_stop.set()
        if self.active_drive_supervisor is not None:
            self.active_drive_supervisor.stop()
        with self._lock:
            thread = self._collector_thread
            history_thread = self._history_thread
        # Active-drive restoration has priority, and the two joins together
        # must stay inside the caller's one shutdown budget.
        deadline = time.monotonic() + max(0.0, float(timeout))
        if thread is not None:
            thread.join(max(0.0, deadline - time.monotonic()))
        if history_thread is not None:
            history_thread.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            if history_thread is None or not history_thread.is_alive():
                self._history_thread = None
            if thread is not None and thread.is_alive():
                # Retain the non-daemon handle so a caller can inspect or retry
                # shutdown; losing it would make an armed child cleanup
                # impossible to supervise.
                self._collector_state = "stop_timeout"
            else:
                self._collector_thread = None
                if self._collector_state != "failed":
                    self._collector_state = "stopped"

    def close(self) -> None:
        """Release offline storage after all background threads stop."""
        self.stop_collector()
        if self.usb_can_monitor is not None:
            self.usb_can_monitor.close()
        with self._lock:
            history_alive = bool(
                self._history_thread is not None
                and self._history_thread.is_alive()
            )
            should_close = bool(
                self.insights is not None
                and not self._insights_closed
                and not history_alive
            )
            if should_close:
                self._insights_closed = True
        if should_close:
            self.insights.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve allowlisted cached vehicle telemetry over a Unix socket."
    )
    parser.add_argument("command", choices=("serve",))
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--socket-mode", default="0660")
    parser.add_argument(
        "--can-interface-mode",
        choices=("dual-usbcanfd",),
        required=True,
        help=(
            "required safety gate: select independent serial/dev_id role "
            "routing for the two installed dual-channel adapters; each healthy "
            "bus remains usable when another role is unavailable, and "
            "fixed-channel mode is retired"
        ),
    )
    parser.add_argument("--collector-interval", type=float, default=1.0)
    parser.add_argument(
        "--history-db",
        default=str(DEFAULT_HISTORY_DB),
        help="SQLite telemetry history (offline/cache-only)",
    )
    parser.add_argument(
        "--history-interval",
        type=float,
        default=5.0,
        help="seconds between cache snapshots stored in SQLite (default: 5)",
    )
    parser.add_argument(
        "--dtc-cache",
        default=str(DEFAULT_DTC_CACHE),
        help="atomic saved-DTC JSON cache exposed read-only to the dashboard",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="disable SQLite history and early-warning evaluation",
    )
    parser.add_argument(
        "--enable-advisory-notifications",
        action="store_true",
        help=(
            "deliver persisted warning episodes through the queue-aware "
            "host ntfy helper (requires --advisory-ntfy-topic)"
        ),
    )
    parser.add_argument(
        "--advisory-ntfy-topic",
        help="fixed ntfy topic used only when advisory delivery is enabled",
    )
    parser.add_argument("--probe-seconds", type=float, default=0.75)
    parser.add_argument("--read-timeout", type=float, default=2.0)
    parser.add_argument("--no-collector", action="store_true")
    parser.add_argument(
        "--no-active-drive",
        action="store_true",
        help="disable the coordinated engine-running diagnostic collector",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if (
        args.collector_interval <= 0
        or args.probe_seconds <= 0
        or args.history_interval <= 0
    ):
        raise SystemExit("collector/probe/history intervals must be positive")
    if args.read_timeout <= 0:
        raise SystemExit("read timeout must be positive")
    if args.enable_advisory_notifications and not args.advisory_ntfy_topic:
        raise SystemExit(
            "--enable-advisory-notifications requires --advisory-ntfy-topic"
        )
    if args.advisory_ntfy_topic and not args.enable_advisory_notifications:
        raise SystemExit(
            "--advisory-ntfy-topic requires --enable-advisory-notifications"
        )
    if args.no_history and args.enable_advisory_notifications:
        raise SystemExit("advisory notifications require history to be enabled")
    try:
        socket_mode = int(args.socket_mode, 8)
    except ValueError:
        raise SystemExit("--socket-mode must be an octal value") from None
    if socket_mode & 0o007:
        raise SystemExit("refusing a world-accessible broker Unix socket")

    from projects.vehicle_data.api import serve_unix
    from projects.vehicle_data.can_interfaces import PassiveInterfaceManager
    from projects.vehicle_data.can_runtime import (
        PassiveRoleReconciler,
        RoleAwareActiveDriveSupervisor,
        RoleAwareCcanPowertrainReader,
        RoleAwareVoltageAcquirer,
    )
    from projects.vehicle_data.early_warning import EarlyWarningEvaluator
    from projects.vehicle_data.historian import TelemetryHistorian
    from projects.vehicle_data.insights import TelemetryInsights
    from projects.vehicle_data.notifications import NtfyAdvisoryNotificationSink
    from projects.vehicle_data.usb_can_monitor import UsbCanIncidentMonitor

    broker_holder: dict[str, TelemetryBroker] = {}
    interface_manager = PassiveInterfaceManager()
    interface_reconciler = PassiveRoleReconciler(interface_manager)
    acquirer = RoleAwareVoltageAcquirer(
        interface_manager,
        probe_seconds=args.probe_seconds,
        read_timeout=args.read_timeout,
        wake_prearm_check=lambda: broker_holder[
            "broker"
        ].wake_prearm_conflicts(),
    )
    passive_powertrain_reader = RoleAwareCcanPowertrainReader(
        interface_manager,
        probe_seconds=min(args.probe_seconds, 0.25),
        read_timeout=min(args.read_timeout, 0.5),
    )
    usb_can_monitor = UsbCanIncidentMonitor()
    active_drive_enabled = not args.no_active_drive
    active_supervisor = None
    if active_drive_enabled:
        active_supervisor = RoleAwareActiveDriveSupervisor(
            interface_manager,
            event_handler=lambda event: broker_holder[
                "broker"
            ].handle_active_drive_event(event),
            supervisor_factory=ActiveDriveSupervisor,
        )

    insights = None
    if not args.no_history:
        historian = TelemetryHistorian(args.history_db)
        notification_sink = (
            NtfyAdvisoryNotificationSink(args.advisory_ntfy_topic)
            if args.enable_advisory_notifications
            else None
        )
        insights = TelemetryInsights(
            historian,
            warning_evaluator=EarlyWarningEvaluator(historian),
            notification_sink=notification_sink,
            enable_notification_delivery=args.enable_advisory_notifications,
            dtc_cache_path=args.dtc_cache,
        )
    broker = TelemetryBroker(
        acquirer=acquirer,
        collector_interval_seconds=args.collector_interval,
        passive_powertrain_reader=passive_powertrain_reader,
        active_drive_supervisor=active_supervisor,
        active_drive_enabled=active_drive_enabled,
        interface_reconciler=interface_reconciler,
        insights=insights,
        history_interval_seconds=args.history_interval,
        usb_can_monitor=usb_can_monitor,
    )
    broker_holder["broker"] = broker
    broker.start_usb_monitor()
    if not args.no_collector:
        broker.start_collector()
    with diagnostic_safety.interrupt_on_termination() as termination:
        try:
            serve_unix(broker, args.socket, mode=socket_mode)
        finally:
            termination.begin_cleanup()
            broker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
