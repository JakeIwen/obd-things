"""Owner-chosen list of diagnostic codes the dashboard hides from its Codes card.

Display bookkeeping only: ignoring a code never clears it, never changes what is
read from the van and never touches the broker's DTC cache. A code is identified
by ``(module_key, fca_display)``. The list lives in ``dtc-ignores.json`` beside
the history database, together with an audit log of the last ``log_limit``
actions (kept in the file, not served). Both are bounded.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading

from lib.modules import MODULES
from projects.vehicle_data.engine_off_voltage import _atomic_json


VOCABULARY = json.loads(Path(__file__).with_suffix(".json").read_text())
API_PATH = VOCABULARY["path"]
SUMMARY_KEY = VOCABULARY["summary_key"]
IGNORE, RESTORE = (VOCABULARY["actions"][name] for name in ("ignore", "restore"))
NOTE_MAX_CHARS = VOCABULARY["note_max_chars"]
MAX_IGNORED = VOCABULARY["max_ignored"]
LOG_LIMIT = VOCABULARY["log_limit"]
FILE_NAME = "dtc-ignores.json"
MAX_FILE_BYTES = 1024 * 1024
# The DTC cache validators accept any string for ``fca_display``; real values look
# like ``B1632-15``. This bound keeps a hostile body from becoming a list key.
CODE_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,32}")
# Same shape the oil-change record accepts (maintenance.py checks it inline).
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{12,80}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ENTRY_KEYS = {"module_key", "fca_display", "note", "ignored_at"}
_LOG_KEYS = {"action", "module_key", "fca_display", "note", "request_id", "at"}


class LimitReached(ValueError):
    """The list is full."""


def _validate_identity(payload: dict) -> None:
    if not isinstance(payload["module_key"], str) or payload["module_key"] not in MODULES:
        raise ValueError("Unknown module")
    code = payload["fca_display"]
    if not isinstance(code, str) or CODE_PATTERN.fullmatch(code) is None:
        raise ValueError("Invalid diagnostic code")


def _validate_note(note: object) -> None:
    if not isinstance(note, str) or len(note) > NOTE_MAX_CHARS or _CONTROL.search(note):
        raise ValueError(f"Note must be text of at most {NOTE_MAX_CHARS} characters")


def validate_request(payload: object) -> dict:
    """The request as a plain dict, or ValueError. Exact keys per action."""
    if not isinstance(payload, dict):
        raise ValueError("Body must be one JSON object")
    action = payload.get("action")
    keys = {"action", "module_key", "fca_display", "request_id"}
    if action == IGNORE:
        keys.add("note")
    elif action != RESTORE:
        raise ValueError("Unknown action")
    if set(payload) != keys:
        raise ValueError("Body has the wrong fields for this action")
    _validate_identity(payload)
    if action == IGNORE:
        _validate_note(payload["note"])
    request_id = payload["request_id"]
    if not isinstance(request_id, str) or REQUEST_ID_PATTERN.fullmatch(request_id) is None:
        raise ValueError("Invalid request ID")
    return dict(payload)


def _stamp(text: object) -> None:
    if datetime.fromisoformat(text).tzinfo is None:
        raise ValueError("saved timestamp is not timezone-aware")


def _check_saved(loaded: object) -> dict:
    if (not isinstance(loaded, dict) or set(loaded) != {"version", "ignored", "log"}
            or loaded["version"] != 1 or not isinstance(loaded["ignored"], list)
            or not isinstance(loaded["log"], list)):
        raise ValueError("file schema is invalid")
    if len(loaded["ignored"]) > MAX_IGNORED or len(loaded["log"]) > LOG_LIMIT:
        raise ValueError("file exceeds its bounds")
    seen = set()
    for row in loaded["ignored"]:
        if not isinstance(row, dict) or set(row) != _ENTRY_KEYS:
            raise ValueError("ignored entry is invalid")
        _validate_identity(row)
        _validate_note(row["note"])
        _stamp(row["ignored_at"])
        key = (row["module_key"], row["fca_display"])
        if key in seen:
            raise ValueError("ignored entry is repeated")
        seen.add(key)
    for row in loaded["log"]:
        if not isinstance(row, dict) or set(row) != _LOG_KEYS or row["action"] not in (IGNORE, RESTORE):
            raise ValueError("log entry is invalid")
        _validate_identity(row)
        _validate_note(row["note"])
        _stamp(row["at"])
        if not isinstance(row["request_id"], str) or REQUEST_ID_PATTERN.fullmatch(row["request_id"]) is None:
            raise ValueError("log request ID is invalid")
    return loaded


class DtcIgnoreStore:
    """The ignored-code list: loaded once, committed to disk before each acknowledgement."""

    def __init__(self, path=None, *, now=lambda: datetime.now(timezone.utc)):
        self.path = Path(path) if path is not None else None
        self._now = now
        self._lock = threading.RLock()
        self.data = {"version": 1, "ignored": [], "log": []}
        self.error = None
        if self.path is None:
            return
        try:
            if self.path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("file exceeds size limit")
            self.data = _check_saved(json.loads(self.path.read_text()))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # Never overwrite an unreadable owner record with an empty one.
            self.error = f"Ignored-code storage could not be read: {exc}"

    def snapshot(self) -> dict:
        """The list, newest first, in the shape both routes and the summary serve."""
        with self._lock:
            return {
                "available": self.error is None,
                "persistent": self.path is not None,
                "storage_error": self.error,
                "limit": MAX_IGNORED,
                "ignored": [dict(row) for row in reversed(self.data["ignored"])],
            }

    def request(self, payload: object) -> tuple[int, dict]:
        """Apply one ignore or restore; the reply is the list after the change."""
        try:
            body = validate_request(payload)
        except ValueError as exc:
            return 400, {**self.snapshot(), "reason": "invalid_request", "detail": str(exc)}
        with self._lock:
            if self.error:
                return 503, {**self.snapshot(), "reason": "storage_unavailable", "detail": self.error}
            try:
                self._apply(body)
            except LimitReached as exc:
                return 409, {**self.snapshot(), "reason": "limit_reached", "detail": str(exc)}
            except ValueError as exc:
                return 400, {**self.snapshot(), "reason": "invalid_request", "detail": str(exc)}
            except OSError as exc:
                return 503, {**self.snapshot(), "reason": "storage_unavailable",
                             "detail": f"The change was not saved: {exc}"}
            return 200, self.snapshot()

    def _apply(self, body: dict) -> None:
        note = body.get("note", "")
        for row in self.data["log"]:
            if row["request_id"] == body["request_id"]:
                if (row["action"], row["module_key"], row["fca_display"], row["note"]) != (
                        body["action"], body["module_key"], body["fca_display"], note):
                    raise ValueError("Request ID already used for a different change")
                return
        key = (body["module_key"], body["fca_display"])
        ignored = self.data["ignored"]
        present = any((row["module_key"], row["fca_display"]) == key for row in ignored)
        stamp = self._now().isoformat()
        if body["action"] == IGNORE:
            if present:
                return
            if len(ignored) >= MAX_IGNORED:
                raise LimitReached(f"At most {MAX_IGNORED} codes can be ignored; restore some first")
            ignored = [*ignored, {"module_key": key[0], "fca_display": key[1],
                                  "note": note, "ignored_at": stamp}]
        else:
            if not present:
                return
            ignored = [row for row in ignored if (row["module_key"], row["fca_display"]) != key]
        entry = {"action": body["action"], "module_key": key[0], "fca_display": key[1],
                 "note": note, "request_id": body["request_id"], "at": stamp}
        updated = {**self.data, "ignored": ignored, "log": [*self.data["log"], entry][-LOG_LIMIT:]}
        # Commit to disk before acknowledging; a failed write leaves memory unchanged.
        if self.path is not None:
            _atomic_json(self.path, updated)
        self.data = updated
