"""The broker's owner record of the below-empty fuel gauge: its file, throttled writes and local API.

``fuel-floor.json`` sits beside ``maintenance.json`` and ``radar-references.json`` and has one
writer, the broker. Nothing here may stop the broker: a missing file is floor 0 with no episode,
an unreadable one is moved aside and replaced by the same, and a failed write is reported in
``storage_error`` and retried, never raised.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading

from projects.vehicle_data import fuel_floor_contract as C
from projects.vehicle_data.engine_off_voltage import _atomic_json
from projects.vehicle_data.fuel_floor_model import FuelFloorModel

MAX_FILE_BYTES = 256 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FuelFloorStore:
    """The model plus its file. Callers pass the broker's monotonic time; the store never reads it."""

    def __init__(self, path=None, *, now=_utc_now):
        self.path = Path(path) if path is not None else None
        self._now = now
        self._lock = threading.Lock()
        self.read_error: str | None = None
        self.write_error: str | None = None
        self._last_write: float | None = None
        self.model = self._load()

    def _load(self) -> FuelFloorModel:
        if self.path is None:
            return FuelFloorModel()
        try:
            if self.path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("file exceeds size limit")
            return FuelFloorModel(json.loads(self.path.read_text()))
        except FileNotFoundError:
            return FuelFloorModel()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # Keep the bytes for inspection, then run as if new: the broker must still start.
            self.read_error = f"fuel floor file could not be read ({exc}); moved aside, floor is 0"
            try:
                self.path.replace(self.path.with_name(self.path.name + ".unreadable"))
            except OSError:
                pass
            return FuelFloorModel()

    @property
    def storage_error(self) -> str | None:
        return self.write_error or self.read_error

    # -- persistence ---------------------------------------------------------

    def _write(self, mono: float) -> bool:
        self._last_write = mono  # a failing disk is retried at the interval, not on every sample
        if self.path is None:
            self.model.dirty = False
            return True
        try:
            _atomic_json(self.path, self.model.to_record())
        except OSError as exc:
            self.write_error = f"fuel floor was not saved: {exc}"
            return False
        self.write_error = None
        self.model.dirty = False
        return True

    def _persist(self, mono: float, *, force: bool) -> None:
        if not self.model.dirty:
            return
        if (not force and self._last_write is not None
                and mono - self._last_write < C.WRITE_INTERVAL_SECONDS):
            return
        self._write(mono)

    def flush(self, mono: float) -> None:
        """Save anything pending now (broker shutdown)."""
        with self._lock:
            try:
                self._persist(mono, force=True)
            except Exception as exc:  # shutdown must finish
                self.write_error = f"fuel floor was not saved: {type(exc).__name__}: {exc}"

    # -- observations (broker threads) ---------------------------------------

    def observe_van_level(self, value: object, mono: float) -> None:
        if type(value) is not int:
            return
        with self._lock:
            try:
                must_write = self.model.observe_van_level(value, mono, self._now())
                self._persist(mono, force=must_write)
            except Exception as exc:  # this feature must never disturb the receive thread
                self.write_error = f"fuel floor tracking failed: {type(exc).__name__}: {exc}"

    def observe_fuel_rate(self, rate: object, sample_mono: float, engine_running: bool, mono: float) -> None:
        with self._lock:
            try:
                self.model.observe_fuel_rate(rate, sample_mono, engine_running)
                self._persist(mono, force=False)
            except Exception as exc:
                self.write_error = f"fuel floor tracking failed: {type(exc).__name__}: {exc}"

    # -- exposure ------------------------------------------------------------

    def status_block(self) -> dict:
        with self._lock:
            return {**self.model.status_block(), "storage_error": self.storage_error}

    def _payload(self, **refusal: object) -> dict:
        return {"available": not refusal, "persistent": self.path is not None,
                "storage_error": self.storage_error, **self.model.listing(), **refusal}

    def response(self) -> dict:
        with self._lock:
            return self._payload()

    def request(self, payload: object, mono: float) -> tuple[int, dict]:
        """``set`` or ``restore``; the new state is adopted only once it is saved."""
        with self._lock:
            before = (dict(self.model.floor), [dict(row) for row in self.model.earlier_floors])
            was_dirty = self.model.dirty
            try:
                if (isinstance(payload, dict) and set(payload) == {"action", "points"}
                        and payload["action"] == C.SET_ACTION):
                    self.model.set_floor(payload["points"], self._now())
                elif (isinstance(payload, dict) and set(payload) == {"action", "index"}
                        and payload["action"] == C.RESTORE_ACTION):
                    self.model.restore_floor(payload["index"], self._now())
                else:
                    return 400, self._payload(
                        reason="invalid_request", detail="body must be a set or restore request")
            except ValueError as exc:
                return 400, self._payload(reason="invalid_request", detail=str(exc))
            if not self._write(mono):
                self.model.floor, self.model.earlier_floors = before
                self.model.dirty = was_dirty
                return 503, self._payload(reason="storage_unavailable", detail=self.write_error)
            return 200, self._payload()
