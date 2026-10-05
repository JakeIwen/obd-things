"""Concurrent Unix-domain HTTP server for the vehicle telemetry broker."""

from __future__ import annotations

import errno
import http.server
import json
import math
import os
import pathlib
import queue
import select
import socket
import socketserver
import stat
import threading
import time
import types
from dataclasses import dataclass, field
from typing import Any

from projects.vehicle_data.http_common import (
    MAX_REQUEST_BYTES,
    MAX_OBSERVATION_QUEUE_SECONDS,
    OBSERVATION_DEADLINE_HEADER,
    route_name,
)


@dataclass
class _ConnectionRecord:
    request: socket.socket
    client_address: Any
    phase: str | None
    deadline: float | None
    expired: bool = False
    retired: bool = False


@dataclass
class _PostJob:
    handler: "TelemetryApiHandler"
    done: threading.Event = field(default_factory=threading.Event)
    exception: Exception | None = None
    cancelled: bool = False
    queue_accounted: bool = False


class TelemetryApiHandler(http.server.BaseHTTPRequestHandler):
    server_version = "VanTelemetry/1"

    def setup(self) -> None:
        super().setup()
        attach = getattr(self.server, "_attach_handler", None)
        if attach is not None:
            attach(self)

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        if parsed:
            self._transport_phase("_complete_header_phase")
        return parsed

    def send_error(self, code, message=None, explain=None) -> None:
        self._transport_phase("_begin_response_phase")
        return super().send_error(code, message, explain)

    def _transport_phase(self, name: str) -> None:
        server = getattr(self, "server", None)
        hook = getattr(server, name, None)
        if hook is not None:
            hook(self)

    def log_message(self, _format, *_args):
        return

    @property
    def broker(self):
        return self.server.broker

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        try:
            self._transport_phase("_begin_response_phase")
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
        submit = getattr(self.server, "_submit_post", None)
        if submit is None or not hasattr(self, "_transport_record"):
            return self._do_POST_serialized()
        job = submit(self)
        job.done.wait()
        if job.exception is not None:
            raise job.exception
        if job.cancelled:
            raise TimeoutError("server stopped before POST execution completed")
        return None

    def _do_POST_serialized(self):
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
        self._transport_phase("_begin_body_phase")
        body = self.rfile.read(length)
        self._transport_phase("_complete_body_phase")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json(
                400,
                {
                    "available": False,
                    "reason": "invalid_request",
                    "detail": "body must be one JSON object",
                },
            )
        self._transport_phase("_begin_callback_phase")
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
    """Serve cache reads concurrently while serializing POSTs on the caller."""

    request_queue_size = 64
    request_worker_count = 8
    request_io_timeout = 5.0
    request_poll_interval = 0.05

    def __init__(self, path: str, broker):
        # TCPServer.__init__ can call our server_close override when bind or
        # activation fails, so all lifecycle state must exist first.
        self.broker = broker
        self._condition = threading.Condition()
        self._transport_queue: queue.Queue[_ConnectionRecord] = queue.Queue(
            maxsize=self.request_worker_count
        )
        self._post_jobs: queue.Queue[_PostJob] = queue.Queue(
            maxsize=self.request_worker_count
        )
        self._connections: dict[int, _ConnectionRecord] = {}
        self._worker_local = threading.local()
        self._helper_threads: list[threading.Thread] = []
        self._fatal: tuple[BaseException, types.TracebackType | None] | None = None
        self._active_post: _PostJob | None = None
        self._accept_reservations = 0
        self._serve_state = "new"
        self._serving_thread_ident: int | None = None
        self._stop_admission = False
        self._shutdown_requested = False
        self._finalized = False
        self._server_closed = False
        self._serve_done = threading.Event()
        super().__init__(path, TelemetryApiHandler)

    def _poll_interval(self) -> float:
        return max(0.001, min(float(self.request_poll_interval), 0.05))

    def _attach_handler(self, handler: TelemetryApiHandler) -> None:
        record = getattr(self._worker_local, "record", None)
        if record is not None:
            handler._transport_record = record

    def _handler_record(self, handler: TelemetryApiHandler) -> _ConnectionRecord | None:
        record = getattr(handler, "_transport_record", None)
        if isinstance(record, _ConnectionRecord):
            return record
        return None

    def _set_phase(
        self,
        handler: TelemetryApiHandler,
        phase: str | None,
        *,
        duration: float | None,
    ) -> None:
        record = self._handler_record(handler)
        if record is None:
            return
        expired_socket: socket.socket | None = None
        with self._condition:
            now = time.monotonic()
            if record.retired or record.expired:
                raise TimeoutError("request transport phase expired")
            if (
                self._fatal is not None
                or (
                    record.deadline is not None
                    and now >= record.deadline
                )
            ):
                record.expired = True
                record.phase = None
                record.deadline = None
                expired_socket = record.request
                self._condition.notify_all()
            else:
                record.phase = phase
                record.deadline = None if duration is None else now + duration
                self._condition.notify_all()
        if expired_socket is not None:
            self._shutdown_raw_socket(expired_socket)
            raise TimeoutError("request transport phase expired")

    def _complete_header_phase(self, handler: TelemetryApiHandler) -> None:
        self._set_phase(handler, None, duration=None)

    def _begin_body_phase(self, handler: TelemetryApiHandler) -> None:
        self._set_phase(
            handler,
            "body",
            duration=float(self.request_io_timeout),
        )

    def _complete_body_phase(self, handler: TelemetryApiHandler) -> None:
        self._set_phase(handler, None, duration=None)

    def _begin_callback_phase(self, handler: TelemetryApiHandler) -> None:
        self._set_phase(handler, None, duration=None)

    def _begin_response_phase(self, handler: TelemetryApiHandler) -> None:
        self._set_phase(
            handler,
            "response",
            duration=float(self.request_io_timeout),
        )

    def _submit_post(self, handler: TelemetryApiHandler) -> _PostJob:
        job = _PostJob(handler=handler)
        cancelled: list[_PostJob] = []
        expired: list[socket.socket] = []
        with self._condition:
            record = self._handler_record(handler)
            if (
                record is None
                or record.retired
                or record.expired
                or self._fatal is not None
                or self._stop_admission
                or self._finalized
            ):
                job.cancelled = True
                job.done.set()
                return job
            try:
                self._post_jobs.put_nowait(job)
            except queue.Full as exc:
                failure = RuntimeError("bounded POST queue capacity was exceeded")
                cancelled, expired = self._record_fatal_locked(
                    failure, failure.__traceback__
                )
                job.exception = exc
                job.done.set()
            else:
                self._condition.notify_all()
        self._signal_jobs(cancelled)
        for request in expired:
            self._shutdown_raw_socket(request)
        return job

    def _post_task_done_locked(self, job: _PostJob) -> None:
        if not job.queue_accounted:
            job.queue_accounted = True
            self._post_jobs.task_done()

    def _drain_post_jobs_locked(self) -> list[_PostJob]:
        cancelled: list[_PostJob] = []
        while True:
            try:
                job = self._post_jobs.get_nowait()
            except queue.Empty:
                break
            job.cancelled = True
            self._post_task_done_locked(job)
            cancelled.append(job)
        return cancelled

    @staticmethod
    def _signal_jobs(jobs: list[_PostJob]) -> None:
        for job in jobs:
            job.done.set()

    def _record_fatal_locked(
        self,
        exc: BaseException,
        traceback: types.TracebackType | None,
    ) -> tuple[list[_PostJob], list[socket.socket]]:
        if self._fatal is not None or self._finalized:
            return [], []
        self._fatal = (exc, traceback)
        self._stop_admission = True
        cancelled = self._drain_post_jobs_locked()
        expired = []
        for record in self._connections.values():
            if (
                not record.retired
                and not record.expired
                and record.deadline is not None
            ):
                record.expired = True
                record.phase = None
                record.deadline = None
                expired.append(record.request)
        self._condition.notify_all()
        return cancelled, expired

    def _record_fatal(
        self,
        exc: BaseException,
        traceback: types.TracebackType | None,
    ) -> None:
        with self._condition:
            cancelled, expired = self._record_fatal_locked(exc, traceback)
        self._signal_jobs(cancelled)
        for request in expired:
            self._shutdown_raw_socket(request)

    @staticmethod
    def _shutdown_raw_socket(request: socket.socket) -> None:
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    @classmethod
    def _close_raw_socket(cls, request: socket.socket) -> None:
        cls._shutdown_raw_socket(request)
        try:
            request.close()
        except OSError:
            pass

    def _expire_due_connections(self) -> list[socket.socket]:
        expired: list[socket.socket] = []
        with self._condition:
            now = time.monotonic()
            for record in self._connections.values():
                if (
                    not record.retired
                    and not record.expired
                    and record.deadline is not None
                    and now >= record.deadline
                ):
                    record.expired = True
                    record.phase = None
                    record.deadline = None
                    expired.append(record.request)
            if expired:
                self._condition.notify_all()
        return expired

    def _retire_connection(self, record: _ConnectionRecord) -> None:
        with self._condition:
            record.retired = True
            record.phase = None
            record.deadline = None
            if self._connections.get(id(record)) is record:
                del self._connections[id(record)]
            self._condition.notify_all()

    def _acceptor_entry(self) -> None:
        try:
            self._acceptor_loop()
        except BaseException as exc:
            self._record_fatal(exc, exc.__traceback__)
            return
        with self._condition:
            expected = self._finalized
        if not expected:
            failure = RuntimeError("telemetry API acceptor exited unexpectedly")
            self._record_fatal(failure, failure.__traceback__)

    def _acceptor_loop(self) -> None:
        interval = self._poll_interval()
        while True:
            for request in self._expire_due_connections():
                self._shutdown_raw_socket(request)

            with self._condition:
                if self._finalized:
                    return
                can_accept = (
                    not self._stop_admission
                    and not self._server_closed
                    and len(self._connections) + self._accept_reservations
                    < self.request_worker_count
                )
                if not can_accept:
                    self._condition.wait(timeout=interval)
                    continue
                self._accept_reservations += 1

            reservation = True
            request: socket.socket | None = None
            registered = False
            try:
                try:
                    ready, _, _ = select.select([self.socket], [], [], interval)
                except (OSError, ValueError):
                    with self._condition:
                        if self._finalized or self._server_closed:
                            return
                    raise
                if not ready:
                    continue
                try:
                    request, client_address = self.get_request()
                except OSError:
                    continue
                request.settimeout(float(self.request_io_timeout))
                now = time.monotonic()
                record = _ConnectionRecord(
                    request=request,
                    client_address=client_address,
                    phase="headers",
                    deadline=now + float(self.request_io_timeout),
                )
                close_request = False
                with self._condition:
                    self._accept_reservations -= 1
                    reservation = False
                    if (
                        self._finalized
                        or self._stop_admission
                        or self._server_closed
                    ):
                        close_request = True
                    elif len(self._connections) >= self.request_worker_count:
                        raise RuntimeError(
                            "accepted a telemetry API connection without capacity"
                        )
                    else:
                        self._connections[id(record)] = record
                        registered = True
                        try:
                            self._transport_queue.put_nowait(record)
                        except queue.Full as exc:
                            del self._connections[id(record)]
                            registered = False
                            raise RuntimeError(
                                "bounded transport queue capacity was exceeded"
                            ) from exc
                        self._condition.notify_all()
                if close_request:
                    self._close_raw_socket(request)
                    request = None
            finally:
                with self._condition:
                    if reservation:
                        self._accept_reservations -= 1
                    self._condition.notify_all()
                if request is not None and not registered:
                    self._close_raw_socket(request)

    def _worker_entry(self) -> None:
        try:
            self._worker_loop()
        except BaseException as exc:
            self._record_fatal(exc, exc.__traceback__)
            return
        with self._condition:
            expected = self._finalized
        if not expected:
            failure = RuntimeError("telemetry API transport worker exited unexpectedly")
            self._record_fatal(failure, failure.__traceback__)

    def _worker_loop(self) -> None:
        interval = self._poll_interval()
        while True:
            try:
                record = self._transport_queue.get(timeout=interval)
            except queue.Empty:
                with self._condition:
                    if self._finalized:
                        return
                continue
            try:
                self._serve_connection(record)
            finally:
                self._transport_queue.task_done()

    def _serve_connection(self, record: _ConnectionRecord) -> None:
        self._worker_local.record = record
        try:
            with self._condition:
                skip = record.retired
            if not skip:
                try:
                    self.finish_request(record.request, record.client_address)
                except Exception as exc:
                    with self._condition:
                        quiet = (
                            (record.expired or record.retired or self._finalized)
                            and isinstance(exc, OSError)
                        )
                    if not quiet:
                        self.handle_error(record.request, record.client_address)
        finally:
            try:
                del self._worker_local.record
            except AttributeError:
                pass
            try:
                self.shutdown_request(record.request)
            finally:
                self._retire_connection(record)

    def _start_helpers(self) -> None:
        workers = [
            threading.Thread(
                target=self._worker_entry,
                name=f"van-telemetry-api-worker-{index + 1}",
                daemon=True,
            )
            for index in range(self.request_worker_count)
        ]
        acceptor = threading.Thread(
            target=self._acceptor_entry,
            name="van-telemetry-api-acceptor",
            daemon=True,
        )
        self._helper_threads = [acceptor, *workers]
        for thread in workers:
            with self._condition:
                if self._finalized or self._server_closed:
                    raise RuntimeError("telemetry API server closed during startup")
            thread.start()
        with self._condition:
            if self._finalized or self._server_closed:
                raise RuntimeError("telemetry API server closed during startup")
        acceptor.start()

    def _execute_post(self, job: _PostJob) -> None:
        try:
            job.handler._do_POST_serialized()
        except Exception as exc:
            job.exception = exc
        except BaseException:
            job.cancelled = True
            raise
        finally:
            job.done.set()
            with self._condition:
                self._post_task_done_locked(job)
                if self._active_post is job:
                    self._active_post = None
                self._condition.notify_all()

    def _finish_serving(self) -> None:
        with self._condition:
            self._stop_admission = True
            self._shutdown_requested = True
            self._finalized = True
            cancelled = self._drain_post_jobs_locked()
            active = self._active_post
            if active is not None:
                if not active.done.is_set():
                    active.cancelled = True
                    active.done.set()
                self._post_task_done_locked(active)
                self._active_post = None
            requests = []
            for record in self._connections.values():
                if not record.retired:
                    record.retired = True
                    record.phase = None
                    record.deadline = None
                    requests.append(record.request)
            self._serve_state = "stopped"
            self._condition.notify_all()
        self._signal_jobs(cancelled)
        for request in requests:
            self._shutdown_raw_socket(request)
        self._serve_done.set()

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        with self._condition:
            if self._serve_state != "new" or self._server_closed:
                raise RuntimeError("UnixHTTPServer.serve_forever is one-shot")
            self._serve_state = "starting"
            self._serving_thread_ident = threading.get_ident()
            self._stop_admission = self._shutdown_requested
            self._finalized = False
            self._serve_done.clear()
            self._condition.notify_all()
        try:
            # Readiness can race a disconnect; accept must not pin the watchdog.
            self.socket.setblocking(False)
            self._start_helpers()
            with self._condition:
                self._serve_state = "serving"
                self._condition.notify_all()
            while True:
                fatal: tuple[BaseException, types.TracebackType | None] | None = None
                job: _PostJob | None = None
                with self._condition:
                    if self._fatal is not None:
                        fatal = self._fatal
                    elif self._shutdown_requested:
                        break
                    else:
                        try:
                            job = self._post_jobs.get_nowait()
                        except queue.Empty:
                            self._condition.wait(timeout=poll_interval)
                        else:
                            if job.cancelled:
                                self._post_task_done_locked(job)
                                job.done.set()
                                job = None
                            else:
                                self._active_post = job
                if fatal is not None:
                    exc, traceback = fatal
                    raise exc.with_traceback(traceback)
                if job is not None:
                    self._execute_post(job)
        finally:
            self._finish_serving()

    def shutdown(self) -> None:
        with self._condition:
            if self._serve_state in {"stopped", "closed"}:
                return
            self._stop_admission = True
            self._shutdown_requested = True
            cancelled = self._drain_post_jobs_locked()
            self._condition.notify_all()
        self._signal_jobs(cancelled)
        self._serve_done.wait()

    def server_close(self) -> None:
        with self._condition:
            if self._server_closed:
                return
            self._server_closed = True
            self._stop_admission = True
            self._shutdown_requested = True
            self._finalized = True
            cancelled = self._drain_post_jobs_locked()
            records = list(self._connections.values())
            self._connections.clear()
            for record in records:
                record.retired = True
                record.phase = None
                record.deadline = None
            active = self._serve_state in {"starting", "serving"}
            if not active:
                self._serve_state = "closed"
            self._condition.notify_all()
        self._signal_jobs(cancelled)
        for record in records:
            self._close_raw_socket(record.request)
        try:
            super().server_close()
        finally:
            if not active:
                self._serve_done.set()


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
