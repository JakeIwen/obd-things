"""Compatibility imports for the telemetry broker HTTP server and client.

New callers can import the client without loading the server implementation.
Existing callers (including the broker's function-local import) keep this path.
This module has no command-line interface.
"""

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from projects.vehicle_data.api_client import TelemetryClient, UnixHTTPConnection
from projects.vehicle_data.api_server import (
    TelemetryApiHandler,
    UnixHTTPServer,
    _prepare_socket_path,
    serve_unix,
)
from projects.vehicle_data.http_common import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    OBSERVATION_DEADLINE_HEADER,
    MAX_OBSERVATION_QUEUE_SECONDS,
)

__all__ = [
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "OBSERVATION_DEADLINE_HEADER",
    "MAX_OBSERVATION_QUEUE_SECONDS",
    "TelemetryApiHandler",
    "UnixHTTPServer",
    "_prepare_socket_path",
    "serve_unix",
    "UnixHTTPConnection",
    "TelemetryClient",
]
