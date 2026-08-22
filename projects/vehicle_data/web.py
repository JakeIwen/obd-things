#!/usr/bin/env python3
"""Explicitly gated cached telemetry web proxy and dashboard."""

from __future__ import annotations

import argparse
import http.server
import ipaddress
import json
import os
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
from projects.vehicle_data.broker import DEFAULT_SOCKET
from lib.dtc_batch import FINAL_JOB_STATES, JobStore, atomic_json
from lib.dtc_web import (
    ArmTokenStore,
    DEFAULT_ARM_PATH,
    DEFAULT_CANCEL_DIR,
    DEFAULT_CURRENT_PATH,
    DEFAULT_JOB_ROOT,
    DEFAULT_REQUEST_PATH,
    DtcWebAuthorizationError,
    DtcWebRequestError,
    build_request,
    cancel_path_for_job,
    queue_cancel_request,
    queue_request,
    read_cancel_request,
)


STATIC = pathlib.Path(__file__).with_name("static")
MAX_STREAM_SECONDS = 300.0
DEFAULT_STREAM_INTERVAL_SECONDS = 1.0
LOOPBACK_BINDS = frozenset(("127.0.0.1", "::1", "localhost"))
TAILSCALE_IPV4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_IPV6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
CURRENT_JOB_ID_RE = re.compile(r"dtc-web-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\Z")


class DtcWebController:
    """Queue and observe one fixed batch without holding active CAN privileges."""

    def __init__(
        self,
        *,
        arm_path: str | pathlib.Path = DEFAULT_ARM_PATH,
        request_path: str | pathlib.Path = DEFAULT_REQUEST_PATH,
        current_path: str | pathlib.Path = DEFAULT_CURRENT_PATH,
        cancel_dir: str | pathlib.Path = DEFAULT_CANCEL_DIR,
        job_root: str | pathlib.Path = DEFAULT_JOB_ROOT,
    ) -> None:
        self.arm_store = ArmTokenStore(arm_path)
        self.request_path = pathlib.Path(request_path)
        self.current_path = pathlib.Path(current_path)
        self.cancel_dir = pathlib.Path(cancel_dir)
        self.cancel_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
        cancel_dir_info = self.cancel_dir.stat()
        if cancel_dir_info.st_uid != os.geteuid() or cancel_dir_info.st_mode & 0o007:
            raise DtcWebRequestError(
                "DTC cancel directory must be owned by the web user and private"
            )
        self.job_root = pathlib.Path(job_root)
        self._lock = threading.Lock()

    @staticmethod
    def _public_job(record: dict[str, Any]) -> dict[str, Any]:
        modules = []
        for row in record.get("modules", []):
            if not isinstance(row, dict):
                continue
            modules.append(
                {
                    key: row.get(key)
                    for key in (
                        "module_key",
                        "logical_bus",
                        "state",
                        "reason",
                        "outcome",
                        "dtc_count",
                    )
                }
            )
        return {
            key: record.get(key)
            for key in (
                "schema_version",
                "job_id",
                "state",
                "created_at",
                "updated_at",
                "started_at",
                "completed_at",
                "current_bus",
                "current_module",
                "cancel_requested",
                "failure",
                "restoration_failure",
                "progress",
            )
        } | {"modules": modules}

    def _pointer(self) -> dict[str, Any] | None:
        try:
            raw = self.current_path.read_bytes()
        except FileNotFoundError:
            return None
        if len(raw) > 4096:
            raise DtcWebRequestError("current DTC job pointer is oversized")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DtcWebRequestError("current DTC job pointer is malformed") from exc
        if not isinstance(payload, dict):
            raise DtcWebRequestError("current DTC job pointer is malformed")
        job_id = payload.get("job_id")
        if not isinstance(job_id, str) or CURRENT_JOB_ID_RE.fullmatch(job_id) is None:
            raise DtcWebRequestError("current DTC job id is invalid")
        return payload

    def _cancel_path(self, job_id: str) -> pathlib.Path:
        return cancel_path_for_job(self.cancel_dir, job_id)

    def status(self) -> dict[str, Any]:
        with self._lock:
            pointer = self._pointer()
            if pointer is None:
                return {"available": True, "enabled": True, "state": "idle", "job": None}
            store = JobStore(self.job_root, str(pointer["job_id"]))
            try:
                record = store.read()
            except FileNotFoundError:
                record = pointer
            if record.get("state") not in FINAL_JOB_STATES:
                try:
                    read_cancel_request(
                        self._cancel_path(str(pointer["job_id"])),
                        expected_job_id=str(pointer["job_id"]),
                    )
                except FileNotFoundError:
                    pass
                else:
                    record = {**record, "cancel_requested": True}
            return {
                "available": True,
                "enabled": True,
                "state": record.get("state", "starting"),
                "job": self._public_job(record),
            }

    def start(self, token: str) -> dict[str, Any]:
        with self._lock:
            current = self._pointer()
            if current is not None:
                try:
                    state = JobStore(
                        self.job_root, str(current["job_id"])
                    ).read().get("state")
                except FileNotFoundError:
                    state = current.get("state")
                if state == "restoration_failed":
                    raise DtcWebRequestError(
                        "the previous job has an unverified restoration; inspect all "
                        "roles and clear the same-boot inhibit locally before manually "
                        "retiring the current-job pointer"
                    )
                if state not in FINAL_JOB_STATES:
                    raise DtcWebRequestError("a DTC batch is already queued or running")
            if self.request_path.exists():
                raise DtcWebRequestError("a DTC batch request is already queued")
            # Consume first: a later queue/storage failure requires a fresh
            # local arm rather than leaving a reusable network credential.
            self.arm_store.consume(token)
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            job_id = f"dtc-web-{stamp}-{uuid.uuid4().hex[:8]}"
            request = build_request(job_id)
            pointer = {
                "schema_version": 1,
                "job_id": job_id,
                "state": "queued",
                "created_at_epoch": request["created_at_epoch"],
            }
            atomic_json(self.current_path, pointer)
            try:
                queue_request(self.request_path, request)
            except BaseException as exc:
                atomic_json(
                    self.current_path,
                    {
                        **pointer,
                        "state": "failed",
                        "failure": f"request queue failed: {type(exc).__name__}: {exc}",
                    },
                )
                raise
            return {
                "available": True,
                "enabled": True,
                "state": "queued",
                "job": self._public_job(pointer),
            }

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            current = self._pointer()
            if current is None:
                raise DtcWebRequestError("there is no current DTC batch")
            # If systemd has not claimed the request yet, atomically move it
            # out of the watched name. A concurrently claimed request simply
            # falls through to the worker's cooperative cancel flag.
            if self.request_path.exists():
                cancelled_path = self.request_path.with_name(
                    f"{self.request_path.name}.cancelled-{current['job_id']}"
                )
                try:
                    os.replace(self.request_path, cancelled_path)
                except FileNotFoundError:
                    pass
                else:
                    cancelled = {
                        **current,
                        "state": "cancelled",
                        "cancel_requested": True,
                        "failure": "cancelled before the worker claimed the request",
                    }
                    atomic_json(self.current_path, cancelled)
                    return {
                        "available": True,
                        "enabled": True,
                        "state": "cancelled",
                        "job": self._public_job(cancelled),
                    }
            store = JobStore(self.job_root, str(current["job_id"]))
            try:
                record = store.read()
            except FileNotFoundError:
                queue_cancel_request(
                    self._cancel_path(str(current["job_id"])),
                    str(current["job_id"]),
                )
                return {
                    "available": True,
                    "enabled": True,
                    "state": current.get("state", "starting"),
                    "job": self._public_job(
                        {**current, "cancel_requested": True}
                    ),
                }
            if record.get("state") in FINAL_JOB_STATES:
                raise DtcWebRequestError(f"DTC batch is already {record.get('state')}")
            try:
                queue_cancel_request(
                    self._cancel_path(str(current["job_id"])),
                    str(current["job_id"]),
                )
            except DtcWebRequestError:
                # An existing request for this same job is idempotent; any
                # malformed/stale record remains a hard failure.
                read_cancel_request(
                    self._cancel_path(str(current["job_id"])),
                    expected_job_id=str(current["job_id"]),
                )
            return {
                "available": True,
                "enabled": True,
                "state": record.get("state"),
                "job": self._public_job(
                    {**store.read(), "cancel_requested": True}
                ),
            }


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
            "dtc_jobs_enabled": self.server.dtc_controller is not None,
            "dtc_jobs_require_local_one_use_arm": (
                self.server.dtc_controller is not None
            ),
            "bind": f"{self.server.server_address[0]}:"
            f"{self.server.server_address[1]}",
        }
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
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._common_headers()
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
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
        if path in (
            "/v1/status",
            "/v1/snapshot",
            "/v1/history",
            "/v1/health",
            "/v1/diagnostics/dtcs",
        ):
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
        if path == "/v1/diagnostics/dtc-jobs/current":
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
        return self._json(
            404,
            {"available": False, "reason": "not_found", "detail": path},
        )

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path in (
            "/v1/diagnostics/dtc-jobs",
            "/v1/diagnostics/dtc-jobs/current/cancel",
        ):
            return self._dtc_job_post(path)
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
                    "token",
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
                    or not isinstance(payload.get("token"), str)
                ):
                    raise DtcWebRequestError("DTC start request schema is not exact")
                result = controller.start(payload["token"])
        except DtcWebAuthorizationError as exc:
            return self._json(
                403,
                {"available": False, "reason": "local_arm_rejected", "detail": str(exc)},
            )
        except (DtcWebRequestError, OSError, RuntimeError, ValueError) as exc:
            return self._json(
                409,
                {"available": False, "reason": "dtc_job_rejected", "detail": str(exc)},
            )
        return self._json(202, result)

    def _stream(self) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self._common_headers()
            self.end_headers()
        except (
            BrokenPipeError,
            ConnectionAbortedError,
            ConnectionResetError,
        ):
            return
        deadline = time.monotonic() + self.server.stream_max_seconds
        while time.monotonic() < deadline:
            try:
                status_code, payload = self.client.request(
                    "GET", "/v1/snapshot"
                )
                web_status = {
                    "active_acquisition_enabled":
                    self.server.allow_acquisitions,
                    "dtc_jobs_enabled": self.server.dtc_controller is not None,
                    "dtc_jobs_require_local_one_use_arm": (
                        self.server.dtc_controller is not None
                    ),
                }
                payload["status_code"] = status_code
                payload["web"] = web_status
                delivery = self.server.next_snapshot_delivery()
                payload["web_delivery"] = delivery
                if isinstance(payload.get("status"), dict):
                    payload["status"]["web"] = web_status
                body = json.dumps(payload, separators=(",", ":"))
                event_id = (
                    f"{delivery['instance_id']}:{delivery['sequence']}"
                )
                self.wfile.write(
                    (
                        f"id: {event_id}\n"
                        f"event: snapshot\ndata: {body}\n\n"
                    ).encode()
                )
                self.wfile.flush()
            except (
                BrokenPipeError,
                ConnectionAbortedError,
                ConnectionResetError,
            ):
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
                except (
                    BrokenPipeError,
                    ConnectionAbortedError,
                    ConnectionResetError,
                ):
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
        dtc_controller: DtcWebController | None = None,
        dtc_trusted_origin: str | None = None,
    ):
        super().__init__(address, TelemetryWebHandler)
        self.telemetry_client = TelemetryClient(socket_path)
        self.allow_acquisitions = allow_acquisitions
        self.dtc_controller = dtc_controller
        self.dtc_trusted_origin = dtc_trusted_origin
        self.stream_interval_seconds = stream_interval_seconds
        self.stream_max_seconds = stream_max_seconds
        self.snapshot_instance_id = uuid.uuid4().hex
        self._snapshot_sequence = 0
        self._snapshot_sequence_lock = threading.Lock()

    def next_snapshot_delivery(self) -> dict[str, int | str]:
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
    parser.add_argument(
        "--allow-acquisitions",
        action="store_true",
        help="allow the dashboard to request an allowlisted passive acquisition",
    )
    parser.add_argument(
        "--enable-dtc-jobs",
        action="store_true",
        help=(
            "enable one-use-locally-armed fixed DTC batch requests on this "
            "listener; never enable this on the unauthenticated LAN listener"
        ),
    )
    parser.add_argument("--dtc-trusted-origin")
    parser.add_argument("--dtc-arm-file", default=str(DEFAULT_ARM_PATH))
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
            arm_path=args.dtc_arm_file,
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
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
