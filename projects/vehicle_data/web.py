#!/usr/bin/env python3
"""Explicitly gated cached telemetry web proxy and dashboard."""

from __future__ import annotations

import argparse
import http.server
import json
import pathlib
import sys
import time
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from projects.vehicle_data.api import MAX_REQUEST_BYTES, TelemetryClient
from projects.vehicle_data.broker import DEFAULT_SOCKET


STATIC = pathlib.Path(__file__).with_name("static")
MAX_STREAM_SECONDS = 300.0
LOOPBACK_BINDS = frozenset(("127.0.0.1", "::1", "localhost"))


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
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._common_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
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
            return self._json(
                503,
                {
                    "available": False,
                    "reason": "broker_unavailable",
                    "detail": str(exc),
                },
            )
        web_status = {
            "active_acquisition_enabled": self.server.allow_acquisitions,
            "bind": f"{self.server.server_address[0]}:"
            f"{self.server.server_address[1]}",
        }
        if path == "/v1/status":
            response["web"] = web_status
        elif path == "/v1/snapshot":
            response["web"] = web_status
            if isinstance(response.get("status"), dict):
                response["status"]["web"] = web_status
        return self._json(status, response)

    def _static(self, filename: str, content_type: str) -> None:
        path = STATIC / filename
        try:
            body = path.read_bytes()
        except OSError:
            return self._json(
                500,
                {
                    "available": False,
                    "reason": "static_asset_unavailable",
                    "detail": filename,
                },
            )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._common_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if path == "/app.js":
            return self._static("app.js", "text/javascript; charset=utf-8")
        if path == "/profiles.js":
            return self._static("profiles.js", "text/javascript; charset=utf-8")
        if path == "/style.css":
            return self._static("style.css", "text/css; charset=utf-8")
        if path in ("/v1/status", "/v1/snapshot"):
            return self._broker_request("GET", path)
        metric_prefix = "/v1/metrics/"
        if (
            path == "/v1/metrics"
            or (
                path.startswith(metric_prefix)
                and path[len(metric_prefix):]
                and "/" not in path[len(metric_prefix):]
            )
        ):
            return self._broker_request("GET", path)
        if path == "/v1/stream":
            return self._stream()
        return self._json(
            404,
            {"available": False, "reason": "not_found", "detail": path},
        )

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/v1/acquisitions/battery.voltage":
            return self._json(
                404,
                {"available": False, "reason": "not_found", "detail": path},
            )
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
            or payload["mode"] not in ("passive", "wake_if_asleep")
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

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._common_headers()
        self.end_headers()
        deadline = time.monotonic() + self.server.stream_max_seconds
        while time.monotonic() < deadline:
            try:
                status_code, payload = self.client.request(
                    "GET", "/v1/snapshot"
                )
                web_status = {
                    "active_acquisition_enabled":
                    self.server.allow_acquisitions,
                }
                payload["status_code"] = status_code
                payload["web"] = web_status
                if isinstance(payload.get("status"), dict):
                    payload["status"]["web"] = web_status
                body = json.dumps(payload, separators=(",", ":"))
                self.wfile.write(f"event: snapshot\ndata: {body}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            except (OSError, RuntimeError, json.JSONDecodeError) as exc:
                body = json.dumps(
                    {"reason": "broker_unavailable", "detail": str(exc)},
                    separators=(",", ":"),
                )
                try:
                    self.wfile.write(
                        f"event: error\ndata: {body}\n\n".encode()
                    )
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
            time.sleep(self.server.stream_interval_seconds)


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
    ):
        super().__init__(address, TelemetryWebHandler)
        self.telemetry_client = TelemetryClient(socket_path)
        self.allow_acquisitions = allow_acquisitions
        self.stream_interval_seconds = stream_interval_seconds
        self.stream_max_seconds = stream_max_seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--allow-remote-bind",
        action="store_true",
        help=(
            "permit an explicitly selected non-loopback bind; this does not "
            "provide authentication"
        ),
    )
    parser.add_argument("--stream-interval", type=float, default=2.0)
    parser.add_argument("--stream-max-seconds", type=float, default=MAX_STREAM_SECONDS)
    parser.add_argument(
        "--allow-acquisitions",
        action="store_true",
        help="allow the dashboard to request allowlisted passive/wake acquisitions",
    )
    return parser


def validate_bind(bind: str, *, allow_remote_bind: bool) -> None:
    if bind not in LOOPBACK_BINDS and not allow_remote_bind:
        raise SystemExit(
            "refusing a non-loopback bind without --allow-remote-bind; "
            "prefer an authenticated external proxy"
        )


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    validate_bind(args.bind, allow_remote_bind=args.allow_remote_bind)
    if not 0 < args.port < 65536:
        raise SystemExit("--port must be between 1 and 65535")
    if args.stream_interval <= 0 or args.stream_max_seconds <= 0:
        raise SystemExit("stream intervals must be positive")
    server = TelemetryWebServer(
        (args.bind, args.port),
        socket_path=args.socket,
        allow_acquisitions=args.allow_acquisitions,
        stream_interval_seconds=args.stream_interval,
        stream_max_seconds=args.stream_max_seconds,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
