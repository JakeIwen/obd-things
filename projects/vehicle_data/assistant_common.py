"""Request primitives shared by the advisor's assistant features (warning chat, DTC research).

The advisor answers every assistant route with ``ChatError`` for an owner-facing refusal; these
helpers are the one copy of that vocabulary and of the id formats its routes accept.
"""

from datetime import datetime, timezone
import re

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
ID_RE = re.compile(r"[0-9a-f]{32}\Z")


def now():
    return datetime.now(timezone.utc).isoformat()


class ChatError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def require_keys(payload, required, optional=()):
    if not isinstance(payload, dict) or not set(required) <= set(payload) or set(payload) - set(required) - set(optional):
        raise ChatError(400, "Unexpected or missing request fields")
