"""Unix-domain HTTP client for the vehicle telemetry broker."""

from __future__ import annotations

import http.client
import json
import socket
import time
from typing import Any

from projects.vehicle_data.http_common import (
    MAX_RESPONSE_BYTES,
    MAX_OBSERVATION_QUEUE_SECONDS,
    OBSERVATION_DEADLINE_HEADER,
)


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
