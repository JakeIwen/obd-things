"""Shared HTTP wire details; listener-specific payload preparation stays local."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from projects.vehicle_data.api_schema import ErrorResponse


MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 1024 * 1024
OBSERVATION_DEADLINE_HEADER = "X-Van-Telemetry-Deadline-Monotonic"
MAX_OBSERVATION_QUEUE_SECONDS = 1.0


RouteTable = tuple[tuple[str, str], ...]


def route_name(path: str, routes: RouteTable) -> str | None:
    """First matching route's method name, preserving subclass dispatch."""

    for pattern, name in routes:
        if re.fullmatch(pattern, path):
            return name
    return None


def web_flags(server, *, include_bind: bool = True, include_build: bool = False) -> dict[str, Any]:
    """Preserve the three existing flag shapes and their SSE insertion order.

    The full v1 stream deliberately omits bind. Dashboard snapshot/lite-stream
    flags add build (even when None) and use sorted keys, matching HTTP JSON.
    """

    flags = {
        "warning_chat_enabled": server.warning_chat_socket is not None,
        "active_acquisition_enabled": server.allow_acquisitions,
        "dtc_jobs_enabled": server.dtc_controller is not None,
    }
    if include_bind:
        flags["bind"] = f"{server.server_address[0]}:{server.server_address[1]}"
    if include_build:
        flags["build"] = server.build_id()
        return dict(sorted(flags.items()))
    return flags


def broker_unavailable(exc: Exception) -> ErrorResponse:
    return {"available": False, "reason": "broker_unavailable", "detail": str(exc)}


def stream_snapshots(
    handler,
    prepare_event: Callable[[int, dict[str, Any]], dict[str, Any]],
) -> None:
    """Serve the common SSE window without changing headers or id framing.

    Preparation owns flag/delivery injection and the full versus lite shape.
    It runs inside the original exception boundary, before serialization.
    """

    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Connection", "keep-alive")
        handler.send_header("X-Accel-Buffering", "no")
        handler._common_headers()
        handler.end_headers()
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
        return
    deadline = time.monotonic() + handler.server.stream_max_seconds
    while time.monotonic() < deadline:
        try:
            status_code, payload = handler.client.request("GET", "/v1/snapshot")
            event = prepare_event(status_code, payload)
            delivery = event["web_delivery"]
            body = json.dumps(event, separators=(",", ":"))
            event_id = f"{delivery['instance_id']}:{delivery['sequence']}"
            handler.wfile.write(
                f"id: {event_id}\nevent: snapshot\ndata: {body}\n\n".encode()
            )
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except (OSError, RuntimeError, json.JSONDecodeError) as exc:
            # SSE errors have no `available` field and no event id.
            body = json.dumps(
                {"reason": "broker_unavailable", "detail": str(exc)},
                separators=(",", ":"),
            )
            try:
                handler.wfile.write(f"event: error\ndata: {body}\n\n".encode())
                handler.wfile.flush()
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return
        time.sleep(handler.server.stream_interval_seconds)
