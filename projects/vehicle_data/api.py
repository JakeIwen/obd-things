"""Unix-domain HTTP API and client for the vehicle telemetry broker."""

from __future__ import annotations

import errno
import http.client
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


MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 1024 * 1024
OBSERVATION_DEADLINE_HEADER = "X-Van-Telemetry-Deadline-Monotonic"
MAX_OBSERVATION_QUEUE_SECONDS = 1.0


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

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/v1/status":
            return self._json(200, self.broker.status_response())
        if path == "/v1/snapshot":
            return self._json(200, self.broker.snapshot_response())
        if path == "/v1/history":
            return self._json(200, self.broker.cached_history_response())
        if path == "/v1/health":
            return self._json(200, self.broker.cached_health_response())
        if path == "/v1/diagnostics/dtcs":
            return self._json(200, self.broker.cached_dtc_response())
        if path == "/v1/metrics":
            return self._json(200, self.broker.list_metrics())
        prefix = "/v1/metrics/"
        if path.startswith(prefix) and "/" not in path[len(prefix):]:
            metric = path[len(prefix):]
            payload = self.broker.metric_response(metric)
            return self._json(
                200 if payload.get("reason") != "unknown_metric" else 404,
                payload,
            )
        return self._json(
            404,
            {"available": False, "reason": "not_found", "detail": path},
        )

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        acquisition_prefix = "/v1/acquisitions/"
        observation_prefix = "/v1/observations/"
        if (
            path.startswith(acquisition_prefix)
            and path[len(acquisition_prefix):]
            and "/" not in path[len(acquisition_prefix):]
        ):
            request_kind = "acquisition"
            metric = path[len(acquisition_prefix):]
        elif (
            path.startswith(observation_prefix)
            and path[len(observation_prefix):]
            and "/" not in path[len(observation_prefix):]
        ):
            request_kind = "observation"
            metric = path[len(observation_prefix):]
        else:
            return self._json(
                404,
                {"available": False, "reason": "not_found", "detail": path},
            )
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
        if request_kind == "acquisition":
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
        else:
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


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 10.0):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class TelemetryClient:
    def __init__(self, socket_path: str, *, timeout: float = 10.0):
        self.socket_path = socket_path
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        body = None
        request_headers = dict(headers or {})
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            request_headers["Content-Type"] = "application/json"
        connection = UnixHTTPConnection(self.socket_path, timeout=self.timeout)
        try:
            connection.request(
                method, path, body=body, headers=request_headers
            )
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError("broker response exceeded size limit")
            decoded = json.loads(raw) if raw else {}
            if not isinstance(decoded, dict):
                raise RuntimeError("broker response was not a JSON object")
            return response.status, decoded
        finally:
            connection.close()

    def publish(
        self,
        metric: str,
        *,
        value: bool | int | float | str,
        unit: str,
        source: str,
        bus: str,
        quality: str,
    ) -> tuple[int, dict[str, Any]]:
        """Publish one allowlisted observation to the local broker."""
        queue_seconds = min(
            MAX_OBSERVATION_QUEUE_SECONDS,
            max(0.05, float(self.timeout)),
        )
        return self.request(
            "POST",
            f"/v1/observations/{metric}",
            {
                "value": value,
                "unit": unit,
                "source": source,
                "bus": bus,
                "quality": quality,
            },
            headers={
                OBSERVATION_DEADLINE_HEADER: (
                    f"{time.monotonic() + queue_seconds:.9f}"
                )
            },
        )
