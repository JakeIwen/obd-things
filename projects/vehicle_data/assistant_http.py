"""JSON replies (and the request body reader) shared by the assistant's two Unix-socket servers: the
advisor (``warning_chat``) and the research worker (``dtc_research_worker``).

A caller that gives up (a timed-out ``curl``, the advisor's five-second worker client) can leave at
any point of a reply. The reply is then dropped with one log line, never a traceback: an install
check reads tracebacks in the journal as a failure. ``QuietDisconnects`` does the same for a caller
that leaves while its request is being read, which the handler cannot catch itself.
"""

import json
import sys

from projects.vehicle_data.assistant_common import ChatError

# TimeoutError: a handler's socket timeout fired on a caller that stopped reading or sending.
CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)


def log_client_gone(server, stage, exc, path=None):
    fields = {"event": "assistant_http_client_gone", "server": server, "stage": stage,
              "error": type(exc).__name__}
    if path:
        fields["path"] = path[:200]
    print(json.dumps(fields), file=sys.stderr, flush=True)


def send_json(handler, status, payload, headers=()):
    """Write one JSON reply; ``False`` when the caller had gone (logged once, nothing raised)."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(body)
    except CLIENT_GONE as exc:
        handler.close_connection = True
        log_client_gone(getattr(handler.server, "log_name", "assistant"), "reply", exc, getattr(handler, "path", None))
        return False
    return True


def read_json_body(handler, limit):
    """The request body parsed as JSON; ``ChatError(400)`` unless its length is 1..``limit`` and it is JSON."""
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if not 0 < length <= limit:
            raise ValueError()
        return json.loads(handler.rfile.read(length))
    except (ValueError, UnicodeDecodeError):
        raise ChatError(400, "Invalid request body") from None


class QuietDisconnects:
    """``socketserver`` mixin: a caller gone while its request is read is one log line; any other
    handler error keeps the standard traceback."""

    log_name = "assistant"

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, CLIENT_GONE):
            log_client_gone(self.log_name, "request", exc)
            return
        super().handle_error(request, client_address)
