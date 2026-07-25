#!/usr/bin/env python3
"""Allowlisted vehicle telemetry broker and passive collection daemon."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from projects.vehicle_data.metrics import METRICS, MetricDefinition
from projects.vehicle_data.models import AcquisitionResult, failure
from projects.vehicle_data.sources import VoltageAcquirer


DEFAULT_SOCKET = "/run/van-telemetry/api.sock"


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
        collector_interval_seconds: float = 5.0,
        wake_min_interval_seconds: float | None = None,
        acquisition_wait_seconds: float = 20.0,
    ):
        self.acquirer = acquirer or VoltageAcquirer()
        self.definitions = definitions or METRICS
        self.monotonic = monotonic
        self.collector_interval_seconds = collector_interval_seconds
        self.wake_min_interval_seconds = wake_min_interval_seconds
        self.acquisition_wait_seconds = acquisition_wait_seconds

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
        self._collector_cycles = 0
        self._collector_last_cycle_at: str | None = None
        self._collector_thread: threading.Thread | None = None
        self._collector_stop = threading.Event()
        self._started_at = datetime.now(timezone.utc).isoformat()

    def list_metrics(self) -> dict[str, object]:
        return {
            "metrics": [
                self.definitions[name].public_dict()
                for name in sorted(self.definitions)
            ]
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
            }
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
        )
        if inhibits:
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
            "inflight": inflight,
            "last_acquisition_errors": last_errors,
            "cached_metrics": cached,
        }

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
        if mode not in ("passive", "wake_if_asleep"):
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
        self._refresh_interface_status()
        with self._lock:
            if result.available:
                self._cache[metric] = result
                self._last_error.pop(metric, None)
            elif result.reason != "rate_limited":
                self._last_error[metric] = result
            entry.result = result
            entry.event.set()
            self._inflight.pop(key, None)
        return result

    def start_collector(self) -> None:
        with self._lock:
            if self._collector_thread is not None:
                return
            self._collector_state = "starting"
            self._collector_stop.clear()
            thread = threading.Thread(
                target=self._collector_loop,
                name="van-telemetry-passive",
                daemon=True,
            )
            self._collector_thread = thread
            thread.start()

    def _collector_loop(self) -> None:
        with self._lock:
            self._collector_state = "running"
        while not self._collector_stop.is_set():
            self.acquire("battery.voltage", "passive")
            with self._lock:
                self._collector_cycles += 1
                self._collector_last_cycle_at = datetime.now(
                    timezone.utc
                ).isoformat()
            self._collector_stop.wait(self.collector_interval_seconds)
        with self._lock:
            self._collector_state = "stopped"

    def stop_collector(self, timeout: float = 10.0) -> None:
        self._collector_stop.set()
        with self._lock:
            thread = self._collector_thread
        if thread is not None:
            thread.join(timeout)
        with self._lock:
            self._collector_thread = None
            if thread is not None and thread.is_alive():
                self._collector_state = "stop_timeout"
            else:
                self._collector_state = "stopped"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve allowlisted cached vehicle telemetry over a Unix socket."
    )
    parser.add_argument("command", choices=("serve",))
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--socket-mode", default="0660")
    parser.add_argument("--channel", default="can0")
    parser.add_argument("--collector-interval", type=float, default=5.0)
    parser.add_argument("--probe-seconds", type=float, default=0.75)
    parser.add_argument("--read-timeout", type=float, default=2.0)
    parser.add_argument("--wake-min-interval", type=float, default=900.0)
    parser.add_argument("--no-collector", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.collector_interval <= 0 or args.probe_seconds <= 0:
        raise SystemExit("collector/probe intervals must be positive")
    if args.read_timeout <= 0 or args.wake_min_interval < 0:
        raise SystemExit("read/wake intervals are invalid")
    try:
        socket_mode = int(args.socket_mode, 8)
    except ValueError:
        raise SystemExit("--socket-mode must be an octal value") from None
    if socket_mode & 0o007:
        raise SystemExit("refusing a world-accessible broker Unix socket")

    from projects.vehicle_data.api import serve_unix

    acquirer = VoltageAcquirer(
        channel=args.channel,
        probe_seconds=args.probe_seconds,
        read_timeout=args.read_timeout,
    )
    broker = TelemetryBroker(
        acquirer=acquirer,
        collector_interval_seconds=args.collector_interval,
        wake_min_interval_seconds=args.wake_min_interval,
    )
    if not args.no_collector:
        broker.start_collector()
    try:
        serve_unix(broker, args.socket, mode=socket_mode)
    finally:
        broker.stop_collector()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
