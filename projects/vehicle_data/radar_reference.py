"""Owner-chosen reference points for the radar alignment readings.

Display bookkeeping only: nothing here reaches the vehicle or changes what the
radar reports. A reference point is a dated copy of the 0845 angles, and the
dashboard shows later readings as the change since the selected point.

Points are never edited or replaced. Zeroing adds one and selects it, so every
earlier choice stays selectable as a rollback point, and each completed Service
Drive Alignment adds the first full reading taken after it. The points live in
``radar-references.json`` beside the reading history's own file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
from typing import NamedTuple
import uuid

from lib.timeutil import finite_number
from projects.vehicle_data.engine_off_voltage import _atomic_json
from projects.vehicle_data.http_common import exact_json
from projects.vehicle_data.radar_alignment import AXES


VOCABULARY = json.loads(Path(__file__).with_suffix(".json").read_text())
API_PATH = VOCABULARY["path"]
MANUAL, CALIBRATION = (VOCABULARY["sources"][name] for name in ("manual", "calibration"))
ZERO_REQUEST = VOCABULARY["zero_request"]
CLEAR_REQUEST = VOCABULARY["clear_request"]
SELECT_ACTION = VOCABULARY["select_action"]
FILE_NAME = "radar-references.json"
HEX_ID = re.compile(r"[0-9a-f]{32}")
MAX_POINTS = 200
MAX_FILE_BYTES = 256 * 1024
# The reading history accepts no angle beyond this either.
MAX_ANGLE_DEGREES = 20.0


def _stamp(text: object) -> datetime:
    """A saved timestamp parsed; it must carry its timezone."""
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        raise ValueError("saved radar reference timestamp is not timezone-aware")
    return stamp


def _hex_id(value: object) -> bool:
    return isinstance(value, str) and HEX_ID.fullmatch(value) is not None


class _Reading(NamedTuple):
    """The last full reading: every axis's angle, and when the newest and oldest were read."""

    angles: dict[str, float]
    newest: datetime
    oldest: datetime


@dataclass(frozen=True)
class ReferencePoint:
    """One saved pair of angles; ``asdict`` is both its file and its API form."""

    id: str
    created_at: str
    source: str
    observed_at: str
    angles: dict[str, float]
    # The Service Drive Alignment run a calibration point was read after.
    request_id: str | None = None

    @classmethod
    def from_saved(cls, row: object) -> "ReferencePoint":
        point = cls(**row)
        for text in (point.created_at, point.observed_at):
            _stamp(text)
        angles, linked = point.angles, point.request_id is not None
        if (not _hex_id(point.id) or point.source not in (MANUAL, CALIBRATION)
                or linked != (point.source == CALIBRATION)
                or (linked and not _hex_id(point.request_id))
                or not isinstance(angles, dict) or set(angles) != set(AXES)
                or not all(finite_number(value) and abs(value) <= MAX_ANGLE_DEGREES
                           for value in angles.values())):
            raise ValueError("saved radar reference point is invalid")
        return point


@dataclass(frozen=True)
class _State:
    """Everything saved; replaced as a whole so a failed write changes nothing."""

    points: tuple[ReferencePoint, ...] = ()
    selected: str | None = None
    selected_at: str | None = None
    # A finished calibration whose point is the next full reading.
    pending: dict[str, str] | None = None

    def saved(self) -> dict[str, object]:
        return {"version": 1, "selected": self.selected, "selected_at": self.selected_at,
                "pending": self.pending, "points": [asdict(point) for point in self.points]}

    @classmethod
    def load(cls, path: Path) -> "_State":
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("radar reference file exceeds size limit")
        saved = json.loads(path.read_text())
        if (not isinstance(saved, dict) or saved.get("version") != 1
                or not isinstance(saved.get("points"), list)):
            raise ValueError("radar reference file schema is invalid")
        points = tuple(ReferencePoint.from_saved(row) for row in saved["points"])
        ids = {point.id for point in points}
        selected, selected_at, pending = (saved[name] for name in ("selected", "selected_at", "pending"))
        if len(ids) != len(points) or (selected is not None and selected not in ids):
            raise ValueError("radar reference selection is invalid")
        if selected_at is not None:
            _stamp(selected_at)
        if pending is not None:
            if set(pending) != {"request_id", "completed_at"} or not _hex_id(pending["request_id"]):
                raise ValueError("saved pending calibration is invalid")
            _stamp(pending["completed_at"])
        return cls(points, selected, selected_at, pending)


class RadarReferences:
    """The saved points, the selected one, and their local API.

    ``history`` is the broker's ``AlignmentHistory``: the angles a point copies
    are its last readings, and it reports each new reading so a finished
    calibration gets its point. Without one (tests) nothing can be zeroed.
    """

    def __init__(self, history=None, *, path=None, now=lambda: datetime.now(timezone.utc)):
        self._history = history
        if path is None and history is not None and history.path is not None:
            path = history.path.with_name(FILE_NAME)
        self.path = Path(path) if path is not None else None
        self._now = now
        self._lock = threading.Lock()
        self._state = _State()
        self.error = None
        self.write_error = None
        if self.path is not None:
            try:
                self._state = _State.load(self.path)
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError, KeyError) as exc:
                # Never overwrite unreadable owner records with an empty list.
                self.error = f"Radar reference storage could not be read: {exc}"
        if history is not None:
            history.on_reading = self.reading_observed

    # -- readings --------------------------------------------------------

    def _current(self) -> _Reading | None:
        """The last full reading, or None until every axis has been read.

        Called before taking the lock, because it takes the history's. It never
        raises: the history calls back into here while the broker stores a reading.
        """
        rows = self._history.latest() if self._history is not None else None
        if rows is None:
            return None
        try:
            stamps = sorted(_stamp(observed_at) for _, observed_at in rows.values())
        except (TypeError, ValueError):
            return None
        angles = {axis: float(value) for axis, (value, _) in rows.items()}
        return _Reading(angles, newest=stamps[-1], oldest=stamps[0])

    def _point(self, source: str, reading: _Reading, request_id=None) -> ReferencePoint:
        return ReferencePoint(uuid.uuid4().hex, self._now().isoformat(), source,
                              reading.newest.isoformat(), dict(reading.angles), request_id)

    @staticmethod
    def _trimmed(points, keep) -> tuple[ReferencePoint, ...]:
        """At most MAX_POINTS: the oldest go first, zeroed ones before calibrations.

        The newest point and the ids in ``keep`` (the selection, and the one it
        replaces) are never dropped.
        """
        points = list(points)
        while len(points) > MAX_POINTS:
            droppable = [point for point in points[:-1] if point.id not in keep]
            if not droppable:
                break
            points.remove(next((point for point in droppable if point.source == MANUAL), droppable[0]))
        return tuple(points)

    def _commit(self, state: _State) -> None:
        """Save, then adopt. A failed write raises and leaves the old state in force."""
        if self.path is not None:
            _atomic_json(self.path, state.saved())
        self._state = state
        self.write_error = None

    # -- calibration points ----------------------------------------------

    def calibration_completed(self, request_id: str) -> None:
        """A Service Drive Alignment finished: its point is the next full reading."""
        with self._lock:
            state = self._state
            waiting = state.pending is not None and state.pending["request_id"] == request_id
            if self.error or waiting or any(point.request_id == request_id for point in state.points):
                return
            pending = {"request_id": request_id, "completed_at": self._now().isoformat()}
            self._commit_unattended(replace(state, pending=pending))

    def reading_observed(self) -> None:
        """Called by the history after each stored reading."""
        if self._state.pending is None:
            return
        current = self._current()
        with self._lock:
            state = self._state
            if state.pending is None or current is None:
                return
            # Both axes must have been read after the routine finished.
            if current.oldest <= datetime.fromisoformat(state.pending["completed_at"]):
                return
            point = self._point(CALIBRATION, current, state.pending["request_id"])
            self._commit_unattended(replace(
                state, points=self._trimmed((*state.points, point), {state.selected}), pending=None))

    def _commit_unattended(self, state: _State) -> None:
        """Nobody is waiting for an answer: a failed write is reported in the status."""
        try:
            self._commit(state)
        except OSError as exc:
            self.write_error = f"Radar reference was not saved: {exc}"

    # -- local API -------------------------------------------------------

    def _payload(self, current, **refusal: object) -> dict[str, object]:
        state = self._state
        return {
            "available": self.error is None and not refusal,
            "persistent": self.path is not None,
            "storage_error": self.error or self.write_error,
            "current": None if current is None else {
                "angles": current.angles, "observed_at": current.newest.isoformat()},
            "selected": state.selected,
            "selected_at": state.selected_at,
            "points": [asdict(point) for point in reversed(state.points)],
            "pending_calibration": state.pending,
            **refusal,
        }

    def status_response(self) -> tuple[int, dict[str, object]]:
        current = self._current()
        with self._lock:
            return 200, self._payload(current)

    def request(self, payload: object) -> tuple[int, dict[str, object]]:
        current = self._current()
        with self._lock:
            if self.error:
                return 503, self._payload(current, reason="storage_unavailable", detail=self.error)
            try:
                if exact_json(payload, ZERO_REQUEST):
                    return self._zero(current)
                if exact_json(payload, CLEAR_REQUEST):
                    return self._select(None, current)
                if (isinstance(payload, dict) and set(payload) == {"action", "id"}
                        and payload["action"] == SELECT_ACTION and _hex_id(payload["id"])):
                    return self._select(payload["id"], current)
            except OSError as exc:
                return 503, self._payload(current, reason="storage_unavailable",
                                          detail=f"the reference was not saved: {exc}")
            return 400, self._payload(current, reason="invalid_request",
                                      detail="body must be a zero, select or clear request")

    def _zero(self, current) -> tuple[int, dict[str, object]]:
        if current is None:
            return 409, self._payload(current, reason="no_reading",
                                      detail="the radar angles have not been read yet")
        state, read_at = self._state, current.newest.isoformat()
        # Zeroing twice on one reading selects the point it already made.
        existing = next((point for point in state.points
                         if point.observed_at == read_at and point.angles == current.angles), None)
        point = existing or self._point(MANUAL, current)
        points = state.points if existing else self._trimmed((*state.points, point), {point.id, state.selected})
        self._commit(replace(state, points=points, selected=point.id,
                             selected_at=self._now().isoformat()))
        return (200 if existing else 201), self._payload(current)

    def _select(self, point_id: str | None, current) -> tuple[int, dict[str, object]]:
        state = self._state
        if point_id is not None and all(point.id != point_id for point in state.points):
            return 404, self._payload(current, reason="unknown_reference",
                                      detail="no saved reference has that id")
        self._commit(replace(state, selected=point_id, selected_at=self._now().isoformat()))
        return 200, self._payload(current)
