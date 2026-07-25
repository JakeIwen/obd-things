"""Unix-domain HTTP API and client for the vehicle telemetry broker."""

from __future__ import annotations

import errno
import http.client
import http.server
import json
import os
import pathlib
import socket
import socketserver
import stat
from typing import Any


MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 1024 * 1024


class TelemetryApiHandler(http.server.BaseHTTPRequestHandler):
    server_version = "VanTelemetry/1"

    def log_message(self, _format, *_args):
        return

    @property
    def broker(self):
        return self.server.broker

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/v1/status":
            return self._json(200, self.broker.status_response())
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
        prefix = "/v1/acquisitions/"
        if not path.startswith(prefix) or "/" in path[len(prefix):]:
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
        if (
            not isinstance(payload, dict)
            or set(payload) != {"mode"}
            or payload.get("mode") not in ("passive", "wake_if_asleep")
        ):
            return self._json(
                400,
                {
                    "available": False,
                    "reason": "invalid_request",
                    "detail": "body must contain only mode=passive or wake_if_asleep",
                },
            )
        metric = path[len(prefix):]
        result = self.broker.acquire(metric, payload["mode"])
        definition = self.broker.definitions.get(metric)
        stale_after = definition.stale_after_seconds if definition else 0
        response = result.as_dict(
            now_monotonic=self.broker.monotonic(),
            stale_after_seconds=stale_after,
        )
        status = {
            "unknown_metric": 404,
            "unsupported_mode": 400,
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
    ) -> tuple[int, dict[str, Any]]:
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        connection = UnixHTTPConnection(self.socket_path, timeout=self.timeout)
        try:
            connection.request(method, path, body=body, headers=headers)
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
