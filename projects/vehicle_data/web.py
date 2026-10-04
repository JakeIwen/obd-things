#!/usr/bin/env python3
"""Explicitly gated cached telemetry web proxy (the ``/v1`` API).

This module serves only JSON routes: the cached broker proxy, the server-sent
event stream, the gated passive voltage read, maintenance and event POSTs, DTC
batch jobs and the warning-advisor proxy. It serves no dashboard page. The
dashboard listener is ``web_v2.py``, which subclasses these classes and adds
the built frontend from ``dashboard/dist`` plus the ``/v2`` routes; the systemd
units run that file, not this one.
"""

from __future__ import annotations

import argparse
import http.server
import ipaddress
import json
import pathlib
import re
import sys
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from projects.vehicle_data.api import MAX_REQUEST_BYTES, TelemetryClient
from projects.vehicle_data.api_schema import SnapshotDelivery
from projects.vehicle_data.http_common import broker_unavailable, route_name, stream_snapshots, web_flags
from projects.vehicle_data.warning_chat import (
    DEFAULT_SOCKET as DEFAULT_WARNING_CHAT_SOCKET, MAX_BODY as MAX_WARNING_CHAT_BYTES,
)
from projects.vehicle_data.broker import DEFAULT_SOCKET
from lib.dtc_web import (
    DtcWebController,
    DEFAULT_CANCEL_DIR,
    DEFAULT_CURRENT_PATH,
    DEFAULT_JOB_ROOT,
    DEFAULT_REQUEST_PATH,
    DtcWebRequestError,
)


MAX_STREAM_SECONDS = 300.0
DEFAULT_STREAM_INTERVAL_SECONDS = 1.0
LOOPBACK_BINDS = frozenset(("127.0.0.1", "::1", "localhost"))
TAILSCALE_IPV4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_IPV6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


class TelemetryWebHandler(http.server.BaseHTTPRequestHandler):
    server_version = "VanTelemetryWeb/1"

    def log_message(self, format_string, *args):
        print(
            f"{self.client_address[0]} "
            f"{format_string % args}",
            flush=True,
        )

    @property
    def client(self):
        return self.server.telemetry_client

    def _common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._common_headers()
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def _broker_request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        try:
            status, response = self.client.request(method, path, payload)
        except (OSError, RuntimeError, json.JSONDecodeError) as exc:
            return self._json(503, broker_unavailable(exc))
        web_status = web_flags(self.server)
        if path == "/v1/status":
            response["web"] = web_status
        elif path == "/v1/snapshot":
            response["web"] = web_status
            response["web_delivery"] = (
                self.server.next_snapshot_delivery()
            )
            if isinstance(response.get("status"), dict):
                response["status"]["web"] = web_status
        return self._json(status, response)

    GET_ROUTES = (
        (r"/v1/assistant/.*", "_assistant_get"),
        (r"/v1/events(?:/.*)?", "_event_get"),
        (r"/v1/(?:status|snapshot|history|health|diagnostics/dtcs|maintenance)", "_proxy_get"),
        (r"/v1/metrics(?:/[^/]+)?", "_proxy_get"),
        (r"/v1/stream", "_stream_get"),
        (r"/v1/diagnostics/dtc-jobs/current", "_dtc_current"),
    )
    POST_ROUTES = (
        (r"/v1/assistant/.*", "_assistant_post"),
        (r"/v1/events/.*", "_event_post"),
        (r"/v1/maintenance/oil-changes", "_maintenance_post"),
        (r"/v1/diagnostics/dtc-jobs(?:/current/cancel)?", "_dtc_job_post"),
        (r"/v1/acquisitions/battery\.voltage", "_acquisition_post"),
    )

    def _not_found(self, path: str) -> None:
        return self._json(
            404,
            {"available": False, "reason": "not_found", "detail": path},
        )

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        name = route_name(path, self.GET_ROUTES)
        if name is None:
            return self._not_found(path)
        return getattr(self, name)(path)

    def _proxy_get(self, path: str) -> None:
        return self._broker_request("GET", path)

    def _event_get(self, path: str) -> None:
        return self._broker_request("GET", self.path)

    def _assistant_get(self, path: str) -> None:
        return self._assistant_request("GET", path)

    def _stream_get(self, path: str) -> None:
        return self._stream()

    def _dtc_current(self, path: str) -> None:
        if self.server.dtc_controller is None:
            return self._json(
                403,
                {
                    "available": False,
                    "reason": "dtc_jobs_disabled",
                    "detail": "this listener is cache-only",
                },
            )
        try:
            return self._json(200, self.server.dtc_controller.status())
        except (OSError, RuntimeError, ValueError) as exc:
            return self._json(
                503,
                {
                    "available": False,
                    "reason": "dtc_job_status_unavailable",
                    "detail": str(exc),
                },
            )

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        name = route_name(path, self.POST_ROUTES)
        if name is None:
            return self._not_found(path)
        return getattr(self, name)(path)

    def _assistant_post(self, path: str) -> None:
        return self._assistant_request("POST", path)

    def _event_post(self, path: str) -> None:
        return self._maintenance_post(self.path)

    def _acquisition_post(self, path: str) -> None:
        if not self.server.allow_acquisitions:
            return self._json(
                403,
                {
                    "available": False,
                    "reason": "web_acquisition_disabled",
                    "detail": "the web service was started cache-only",
                },
            )
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
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
            payload = None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"mode"}
            or payload["mode"] != "passive"
        ):
            return self._json(
                400,
                {
                    "available": False,
                    "reason": "invalid_request",
                    "detail": "only the approved acquisition modes are accepted",
                },
            )
        return self._broker_request("POST", path, payload)

    def _assistant_request(self, method: str, path: str) -> None:
        if self.server.warning_chat_socket is None:
            return self._json(503, {"available": False, "detail": "Warning chat is disabled on this listener"})
        allowed = (method == "GET" and (path == "/v1/assistant/status" or
                   re.fullmatch(r"/v1/assistant/chats/[0-9a-f]{32}", path))) or (
                   method == "POST" and (path == "/v1/assistant/chats" or
                   re.fullmatch(r"/v1/assistant/chats/[0-9a-f]{32}/(messages|cancel|apply-warning|remove-warning)", path) or
                   re.fullmatch(r"/v1/assistant/warnings/[0-9a-f]{64}/remove", path)))
        if not allowed:
            return self._json(404, {"available": False, "detail": "Unknown assistant request"})
        host = self.headers.get("Host", "")
        origin = f"http://{host}"
        if (origin not in self.server.warning_chat_origins or
                self.headers.get("Sec-Fetch-Site") not in (None, "same-origin") or
                (method == "POST" and self.headers.get("Origin") != origin) or
                (self.headers.get("Origin") not in (None, origin))):
            return self._json(403, {"available": False, "detail": "Warning chat requires a trusted same-origin request"})
        headers = {name: self.headers.get(name, "") for name in ("X-Van-Assistant-Key", "X-Van-Chat-Client")}
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value) for value in headers.values()):
            return self._json(401, {"available": False, "detail": "Connect this browser using the local assistant access code"})
        payload = None
        if method == "POST":
            if self.headers.get_content_type() != "application/json":
                return self._json(415, {"available": False, "detail": "JSON is required"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_WARNING_CHAT_BYTES:
                    raise ValueError()
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError()
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"available": False, "detail": "Invalid assistant request body"})
        try:
            client = TelemetryClient(self.server.warning_chat_socket, timeout=12)
            status, response = client.request(method, path, payload, headers=headers)
            return self._json(status, response)
        except (OSError, RuntimeError, ValueError):
            return self._json(503, {"available": False, "detail": "Local Codex assistant is not running or is unavailable on the Pi"})

    def _maintenance_post(self, path: str) -> None:
        # Human notes only. Require a same-origin JSON request; plain forms
        # and cross-origin browser requests cannot write the service journal.
        expected_origin = f"http://{self.headers.get('Host', '')}"
        if (self.headers.get("Origin") != expected_origin
                or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin")):
            return self._json(403, {"available": False, "detail": "Service records require a same-origin request"})
        if self.headers.get_content_type() != "application/json":
            return self._json(415, {"available": False, "detail": "Service records require JSON"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise ValueError("Invalid service record size")
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError) as exc:
            return self._json(400, {"available": False, "detail": str(exc)})
        return self._broker_request("POST", path, payload)

    def _dtc_job_post(self, path: str) -> None:
        controller = self.server.dtc_controller
        if controller is None:
            return self._json(
                403,
                {
                    "available": False,
                    "reason": "dtc_jobs_disabled",
                    "detail": "this listener is cache-only",
                },
            )
        if self.headers.get("Origin") != self.server.dtc_trusted_origin:
            return self._json(
                403,
                {
                    "available": False,
                    "reason": "origin_rejected",
                    "detail": "the DTC action origin is not the configured listener",
                },
            )
        if self.headers.get_content_type() != "application/json":
            return self._json(
                415,
                {
                    "available": False,
                    "reason": "invalid_content_type",
                    "detail": "DTC job actions require application/json",
                },
            )
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
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
            payload = None
        try:
            if path.endswith("/cancel"):
                if not isinstance(payload, dict) or payload != {"action": "cancel"}:
                    raise DtcWebRequestError("cancel request schema is not exact")
                result = controller.cancel()
            else:
                expected = {
                    "confirm_parked",
                    "confirm_park_gear",
                    "confirm_ignition_on_engine_off",
                }
                if (
                    not isinstance(payload, dict)
                    or set(payload) != expected
                    or payload.get("confirm_parked") is not True
                    or payload.get("confirm_park_gear") is not True
                    or payload.get("confirm_ignition_on_engine_off") is not True
                ):
                    raise DtcWebRequestError("DTC start request schema is not exact")
                result = controller.start()
        except (DtcWebRequestError, OSError, RuntimeError, ValueError) as exc:
            return self._json(
                409,
                {"available": False, "reason": "dtc_job_rejected", "detail": str(exc)},
            )
        return self._json(202, result)

    def _stream(self) -> None:
        return stream_snapshots(self, self._stream_event)

    def _stream_event(self, status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
        web_status = web_flags(self.server, include_bind=False)
        payload["status_code"] = status_code
        payload["web"] = web_status
        delivery = self.server.next_snapshot_delivery()
        payload["web_delivery"] = delivery
        if isinstance(payload.get("status"), dict):
            payload["status"]["web"] = web_status
        return payload


class TelemetryWebServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        *,
        socket_path: str,
        allow_acquisitions: bool,
        stream_interval_seconds: float,
        stream_max_seconds: float,
        dtc_controller: DtcWebController | None = None,
        dtc_trusted_origin: str | None = None,
        warning_chat_socket: str | None = DEFAULT_WARNING_CHAT_SOCKET,
        warning_chat_origins: list[str] | None = None,
    ):
        super().__init__(address, TelemetryWebHandler)
        self.telemetry_client = TelemetryClient(socket_path)
        self.allow_acquisitions = allow_acquisitions
        self.dtc_controller = dtc_controller
        self.dtc_trusted_origin = dtc_trusted_origin
        self.warning_chat_socket = warning_chat_socket
        bind, port = self.server_address[:2]
        host = f"[{bind}]" if ":" in str(bind) else str(bind)
        self.warning_chat_origins = {f"http://{host}:{port}"}
        if bind in LOOPBACK_BINDS:
            self.warning_chat_origins.add(f"http://localhost:{port}")
        self.warning_chat_origins.update(warning_chat_origins or [])
        self.stream_interval_seconds = stream_interval_seconds
        self.stream_max_seconds = stream_max_seconds
        self.snapshot_instance_id = uuid.uuid4().hex
        self._snapshot_sequence = 0
        self._snapshot_sequence_lock = threading.Lock()

    def next_snapshot_delivery(self) -> SnapshotDelivery:
        """Return process-scoped ordering and generation metadata."""

        with self._snapshot_sequence_lock:
            self._snapshot_sequence += 1
            generated_at_ms = time.time_ns() // 1_000_000
            generated_monotonic_ms = time.monotonic_ns() // 1_000_000
            return {
                "instance_id": self.snapshot_instance_id,
                "sequence": self._snapshot_sequence,
                "generated_at_ms": generated_at_ms,
                "generated_monotonic_ms": generated_monotonic_ms,
            }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--warning-chat-socket", default=DEFAULT_WARNING_CHAT_SOCKET)
    parser.add_argument("--no-warning-chat", action="store_true")
    parser.add_argument("--warning-chat-origin", action="append", default=[],
                        help="additional exact trusted HTTP origin for warning chat (for example http://vanpi.lan:8765)")
    parser.add_argument(
        "--allow-remote-bind",
        action="store_true",
        help=(
            "permit an explicitly selected non-loopback bind; this does not "
            "provide authentication"
        ),
    )
    parser.add_argument(
        "--stream-interval",
        type=float,
        default=DEFAULT_STREAM_INTERVAL_SECONDS,
    )
    parser.add_argument("--stream-max-seconds", type=float, default=MAX_STREAM_SECONDS)
    acquisition = parser.add_mutually_exclusive_group()
    acquisition.add_argument(
        "--allow-acquisitions",
        action="store_true",
        default=True,
        help="allow the dashboard to request an allowlisted passive voltage read (default)",
    )
    acquisition.add_argument(
        "--cache-only",
        dest="allow_acquisitions",
        action="store_false",
        help="disable browser-requested voltage reads; GETs and streams remain cache-only in either mode",
    )
    parser.add_argument(
        "--enable-dtc-jobs",
        action="store_true",
        help=(
            "enable confirmed, guarded fixed DTC batch requests on this "
            "listener; never enable this on the unauthenticated LAN listener"
        ),
    )
    parser.add_argument("--dtc-trusted-origin")
    parser.add_argument("--dtc-request-file", default=str(DEFAULT_REQUEST_PATH))
    parser.add_argument("--dtc-current-file", default=str(DEFAULT_CURRENT_PATH))
    parser.add_argument("--dtc-cancel-dir", default=str(DEFAULT_CANCEL_DIR))
    parser.add_argument("--dtc-job-root", default=str(DEFAULT_JOB_ROOT))
    return parser


def validate_bind(bind: str, *, allow_remote_bind: bool) -> None:
    if bind not in LOOPBACK_BINDS and not allow_remote_bind:
        raise SystemExit(
            "refusing a non-loopback bind without --allow-remote-bind; "
            "prefer an authenticated external proxy"
        )


def validate_dtc_origin(origin: str | None, *, bind: str, port: int) -> str:
    if not isinstance(origin, str) or not origin:
        raise SystemExit("--enable-dtc-jobs requires --dtc-trusted-origin")
    parsed = urlsplit(origin)
    try:
        parsed_port = parsed.port
    except ValueError:
        parsed_port = None
    if (
        parsed.scheme not in ("http", "https")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or parsed.hostname != bind
        or parsed_port != port
    ):
        raise SystemExit(
            "--dtc-trusted-origin must exactly match scheme://bind:port"
        )
    return origin.rstrip("/")


def validate_dtc_job_bind(bind: str) -> None:
    if bind in LOOPBACK_BINDS:
        return
    try:
        address = ipaddress.ip_address(bind)
    except ValueError:
        raise SystemExit(
            "DTC jobs require a literal loopback or Tailscale bind address"
        ) from None
    if address not in TAILSCALE_IPV4 and address not in TAILSCALE_IPV6:
        raise SystemExit(
            "DTC jobs may be enabled only on loopback or a Tailscale address"
        )


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    validate_bind(args.bind, allow_remote_bind=args.allow_remote_bind)
    if not 0 < args.port < 65536:
        raise SystemExit("--port must be between 1 and 65535")
    if args.stream_interval <= 0 or args.stream_max_seconds <= 0:
        raise SystemExit("stream intervals must be positive")
    dtc_controller = None
    dtc_origin = None
    if args.enable_dtc_jobs:
        validate_dtc_job_bind(args.bind)
        dtc_origin = validate_dtc_origin(
            args.dtc_trusted_origin,
            bind=args.bind,
            port=args.port,
        )
        dtc_controller = DtcWebController(
            request_path=args.dtc_request_file,
            current_path=args.dtc_current_file,
            cancel_dir=args.dtc_cancel_dir,
            job_root=args.dtc_job_root,
        )
    elif args.dtc_trusted_origin:
        raise SystemExit("--dtc-trusted-origin requires --enable-dtc-jobs")
    server = TelemetryWebServer(
        (args.bind, args.port),
        socket_path=args.socket,
        allow_acquisitions=args.allow_acquisitions,
        stream_interval_seconds=args.stream_interval,
        stream_max_seconds=args.stream_max_seconds,
        dtc_controller=dtc_controller,
        dtc_trusted_origin=dtc_origin,
        warning_chat_socket=None if args.no_warning_chat else args.warning_chat_socket,
        warning_chat_origins=args.warning_chat_origin,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
