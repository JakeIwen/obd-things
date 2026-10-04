"""Serialized Unix-domain HTTP server for the vehicle telemetry broker."""

from __future__ import annotations

import errno
import http.server
import json
import math
import os
import pathlib
import socket
import socketserver
import stat
import time
from typing import Any

from projects.vehicle_data.http_common import (
    MAX_REQUEST_BYTES,
    MAX_OBSERVATION_QUEUE_SECONDS,
    OBSERVATION_DEADLINE_HEADER,
    route_name,
)


class TelemetryApiHandler(http.server.BaseHTTPRequestHandler):
    server_version = "VanTelemetry/1"

    def log_message(self, _format, *_args):
        return

    @property
    def broker(self):
        return self.server.broker

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    GET_PRODUCTS = {
        "/v1/status": "status_response",
        "/v1/snapshot": "snapshot_response",
        "/v1/history": "cached_history_response",
        "/v1/health": "cached_health_response",
        "/v1/diagnostics/dtcs": "cached_dtc_response",
        "/v1/maintenance": "maintenance_response",
        "/v1/metrics": "list_metrics",
    }
    GET_ROUTES = (
        (r"/v1/events(?:/.*)?", "_event_get"),
        # The broker (unlike the web proxy) accepts an empty metric name.
        (r"/v1/metrics/[^/]*", "_metric_get"),
    )
    POST_ROUTES = (
        (r"/v1/events/.*", "_event_post"),
        (r"/v1/acquisitions/[^/]+", "_acquisition_post"),
        (r"/v1/observations/[^/]+", "_observation_post"),
        (r"/v1/maintenance/oil-changes", "_oil_change_post"),
    )

    def _not_found(self, path: str) -> None:
        return self._json(
            404,
            {"available": False, "reason": "not_found", "detail": path},
        )

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        product = self.GET_PRODUCTS.get(path)
        if product is not None:
            return self._json(200, getattr(self.broker, product)())
        name = route_name(path, self.GET_ROUTES)
        if name is None:
            return self._not_found(path)
        return getattr(self, name)(path)

    def _event_get(self, path: str) -> None:
        return self._json(*self.broker.event_request("GET", self.path))

    def _metric_get(self, path: str) -> None:
        metric = path[len("/v1/metrics/"):]
        payload = self.broker.metric_response(metric)
        return self._json(
            200 if payload.get("reason") != "unknown_metric" else 404,
            payload,
        )

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        name = route_name(path, self.POST_ROUTES)
        if name is None:
            return self._not_found(path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._json(
                400,
                {
                    "available": False,
                    "reason": "invalid_request",
                    "detail": "invalid Content-Length",
                },
            )
        if length <= 0 or length > MAX_REQUEST_BYTES:
            return self._json(
                400,
                {
                    "available": False,
                    "reason": "invalid_request",
                    "detail": "request body size is invalid",
                },
            )
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json(
                400,
                {
                    "available": False,
                    "reason": "invalid_request",
                    "detail": "body must be one JSON object",
                },
            )
        return getattr(self, name)(path, payload)

    def _event_post(self, path: str, payload: Any) -> None:
        return self._json(*self.broker.event_request("POST", self.path, payload))

    def _oil_change_post(self, path: str, payload: Any) -> None:
        try:
            record = self.broker.record_oil_change(payload)
            return self._json(201, {"available": True, "record": record})
        except (TypeError, ValueError) as exc:
            return self._json(400, {"available": False, "detail": str(exc)})
        except OSError as exc:
            return self._json(503, {"available": False, "detail": f"Service record was not saved: {exc}"})

    def _acquisition_post(self, path: str, payload: Any) -> None:
        metric = path[len("/v1/acquisitions/"):]
        allowed_mode = (
            isinstance(payload, dict)
            and set(payload) == {"mode"}
            and (
                payload.get("mode") == "passive"
                or (
                    metric == "battery.voltage"
                    and payload.get("mode") == "wake_if_asleep"
                )
            )
        )
        if (
            not allowed_mode
        ):
            return self._json(
                400,
                {
                    "available": False,
                    "reason": "invalid_request",
                    "detail": (
                        "body must contain one approved local acquisition "
                        "mode; wake_if_asleep is restricted to battery.voltage"
                    ),
                },
            )
        result = self.broker.acquire(metric, payload["mode"])
        return self._acquisition_response(metric, result)

    def _observation_post(self, path: str, payload: Any) -> None:
        metric = path[len("/v1/observations/"):]
        required = {"value", "unit", "source", "bus", "quality"}
        if not isinstance(payload, dict) or set(payload) != required:
            return self._json(
                400,
                {
                    "available": False,
                    "reason": "invalid_request",
                    "detail": (
                        "observation body must contain exactly value, "
                        "unit, source, bus, and quality"
                    ),
                },
            )
        raw_deadline = self.headers.get(OBSERVATION_DEADLINE_HEADER)
        try:
            deadline = float(raw_deadline) if raw_deadline is not None else math.nan
        except ValueError:
            deadline = math.nan
        now = time.monotonic()
        if not math.isfinite(deadline):
            return self._json(
                400,
                {
                    "available": False,
                    "reason": "invalid_request",
                    "detail": (
                        f"{OBSERVATION_DEADLINE_HEADER} must contain one "
                        "finite local monotonic deadline"
                    ),
                },
            )
        if deadline < now:
            return self._json(
                408,
                {
                    "metric": metric,
                    "available": False,
                    "reason": "observation_expired",
                    "detail": (
                        "the local publication waited too long before the "
                        "serialized broker could receive it"
                    ),
                },
            )
        if deadline - now > MAX_OBSERVATION_QUEUE_SECONDS:
            return self._json(
                400,
                {
                    "metric": metric,
                    "available": False,
                    "reason": "invalid_request",
                    "detail": (
                        "observation deadline exceeds the broker's bounded "
                        "local queue allowance"
                    ),
                },
            )
        result = self.broker.publish_observation(metric, **payload)
        return self._acquisition_response(metric, result)

    def _acquisition_response(self, metric: str, result) -> None:
        definition = self.broker.definitions.get(metric)
        stale_after = definition.stale_after_seconds if definition else 0
        response = result.as_dict(
            now_monotonic=self.broker.monotonic(),
            stale_after_seconds=stale_after,
        )
        status = {
            "unknown_metric": 404,
            "unsupported_mode": 400,
            "invalid_observation": 400,
            "source_not_publishable": 403,
            "rate_limited": 429,
            "can_busy": 409,
            "restoration_failed": 500,
        }.get(result.reason, 200 if result.available else 503)
        return self._json(status, response)


class UnixHTTPServer(socketserver.UnixStreamServer):
    """Serialized broker transport.

    Active CAN helpers install termination-safe signal guards and therefore must
    execute on the process main thread. The web proxy remains threaded, but this
    privileged local server deliberately handles one bounded request at a time.
    """

    # The web tier is threaded and several dashboard tabs can refresh their
    # memory-only products together. Keep a bounded accept backlog large enough
    # for those short requests; observation deadlines still reject stale local
    # publications after queueing, and request execution remains serialized.
    request_queue_size = 64

    def __init__(self, path: str, broker):
        self.broker = broker
        super().__init__(path, TelemetryApiHandler)


def _prepare_socket_path(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return
    mode = path.lstat().st_mode
    if not stat.S_ISSOCK(mode):
        raise RuntimeError(f"refusing to replace non-socket path {path}")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        probe.connect(str(path))
    except OSError as exc:
        if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT):
            raise RuntimeError(f"cannot validate existing socket {path}: {exc}") from exc
    else:
        raise RuntimeError(f"another telemetry broker is already serving {path}")
    finally:
        probe.close()
    path.unlink()


def serve_unix(broker, path: str, *, mode: int = 0o660) -> None:
    socket_path = pathlib.Path(path)
    _prepare_socket_path(socket_path)
    server = UnixHTTPServer(str(socket_path), broker)
    os.chmod(socket_path, mode)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            if socket_path.exists() and stat.S_ISSOCK(socket_path.lstat().st_mode):
                socket_path.unlink()
        except FileNotFoundError:
            pass
