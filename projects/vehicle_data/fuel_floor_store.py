"""The broker's owner record of the below-empty fuel gauge: its file, throttled writes and local API.

``fuel-floor.json`` sits beside ``maintenance.json`` and ``radar-references.json`` and has one
writer, the broker. Nothing here may stop the broker: a missing file is floor 0 with no episode,
an unreadable one (any parse or validation failure) is moved aside and replaced by the same, and a
failed write is reported in ``storage_error`` and retried, never raised.

Threads. Receive and publication threads only ``enqueue_*`` into a bounded in-memory queue (an
append, no lock shared with any file write). One worker thread, started and stopped by the broker,
drains it, applies the model and writes the file. Readers (status, the local API, the owner CLI)
get an immutable copy swapped in under a lock that is never held across file I/O.
"""

from __future__ import annotations

import collections
from datetime import datetime, timezone
import json
from pathlib import Path
import threading

from projects.vehicle_data import fuel_floor_contract as C
from projects.vehicle_data.engine_off_voltage import _atomic_json
from projects.vehicle_data.fuel_floor_model import FuelFloorModel

MAX_FILE_BYTES = 256 * 1024
WORKER_WAKE_SECONDS = 0.5


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FuelFloorStore:
    """The model plus its file. Callers pass the broker's monotonic time; the store never reads it."""

    def __init__(self, path=None, *, now=_utc_now):
        self.path = Path(path) if path is not None else None
        self._now = now
        self._model_lock = threading.Lock()   # model mutation and record snapshots: no I/O inside
        self._write_lock = threading.Lock()   # serializes file writes: only writers ever take it
        self._view_lock = threading.Lock()    # swaps the reader copy: held for one assignment
        self._queue: collections.deque = collections.deque(maxlen=C.QUEUE_MAX)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.read_error: str | None = None
        self.write_error: str | None = None
        self._last_write: float | None = None
        self.model = self._load()
        self._view: dict = {}
        self._publish_view()

    def _load(self) -> FuelFloorModel:
        if self.path is None:
            return FuelFloorModel()
        try:
            if self.path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("file exceeds size limit")
            return FuelFloorModel(json.loads(self.path.read_text()))
        except FileNotFoundError:
            return FuelFloorModel()
        except Exception as exc:  # RecursionError, OverflowError, bad UTF-8...: the broker must still start
            self.read_error = (f"fuel floor file could not be read ({type(exc).__name__}: {exc}); "
                               "moved aside, floor is 0")
            try:
                self.path.replace(self.path.with_name(self.path.name + ".unreadable"))
            except OSError:
                pass
            return FuelFloorModel()

    @property
    def storage_error(self) -> str | None:
        return self.write_error or self.read_error

    # -- reader copy ---------------------------------------------------------

    def _publish_view(self) -> None:
        """Call with the model lock held (or before sharing): builds fresh copies, swaps them in."""
        view = {"status": self.model.status_block(), "listing": self.model.listing()}
        with self._view_lock:
            self._view = view

    def _current_view(self) -> dict:
        with self._view_lock:
            return self._view

    # -- persistence ---------------------------------------------------------

    def _persist(self, mono: float, *, force: bool) -> None:
        """Write the record when dirty and due; the file I/O happens outside the model lock."""
        with self._write_lock:
            with self._model_lock:
                if not self.model.dirty:
                    return
                if (not force and self._last_write is not None
                        and mono - self._last_write < C.WRITE_INTERVAL_SECONDS):
                    return
                record = self.model.to_record()
                self.model.dirty = False
                self._last_write = mono  # a failing disk is retried at the interval, not on every sample
            ok = self._write_record(record)
            if not ok:
                with self._model_lock:
                    self.model.dirty = True

    def _write_record(self, record: dict) -> bool:
        if self.path is None:
            return True
        try:
            _atomic_json(self.path, record)
        except Exception as exc:  # never raise into a caller
            self.write_error = f"fuel floor was not saved: {type(exc).__name__}: {exc}"
            return False
        self.write_error = None
        return True

    def flush(self, mono: float) -> None:
        """Drain the queue and save anything pending now (broker shutdown)."""
        try:
            self.process(mono)
            self._persist(mono, force=True)
        except Exception as exc:  # shutdown must finish
            self.write_error = f"fuel floor was not saved: {type(exc).__name__}: {exc}"

    # -- enqueue (receive and publication threads: append only) ----------------

    def enqueue_van_level(self, value: object, mono: float) -> None:
        self._queue.append(("van", value, mono))
        self._wake.set()

    def enqueue_engine(self, rpm: object, mono: float) -> None:
        self._queue.append(("engine", rpm, mono))
        self._wake.set()

    def enqueue_fuel_rate(self, rate: object, sample_mono: float, rpm: object, mono: float) -> None:
        self._queue.append(("rate", rate, sample_mono, rpm, mono))
        self._wake.set()

    # -- worker --------------------------------------------------------------

    def process(self, mono: float) -> None:
        """Apply every queued observation in order (worker thread, or a test calling it directly)."""
        while True:
            try:
                item = self._queue.popleft()
            except IndexError:
                return
            kind = item[0]
            if kind == "van":
                self.observe_van_level(item[1], item[2])
            elif kind == "engine":
                self.observe_engine(item[1], item[2])
            else:
                self.observe_fuel_rate(item[1], item[2], item[3], item[4])

    def start(self, clock) -> None:
        """Start the drain thread; ``clock`` is the broker's monotonic function."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(clock,),
                                        name="van-telemetry-fuel-floor", daemon=True)
        self._thread.start()

    def _run(self, clock) -> None:
        while not self._stop.is_set():
            self._wake.wait(WORKER_WAKE_SECONDS)
            self._wake.clear()
            try:
                self.process(clock())
            except Exception as exc:
                self.write_error = f"fuel floor tracking failed: {type(exc).__name__}: {exc}"

    def stop(self, clock, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if not thread.is_alive():
                self._thread = None
        self.flush(clock())

    # -- observations (applied by the worker) ----------------------------------

    def _apply(self, action, mono: float, *, force_if=lambda changed: bool(changed)) -> None:
        try:
            with self._model_lock:
                changed = action()
                self._publish_view()
            self._persist(mono, force=force_if(changed))
        except Exception as exc:  # this feature must never disturb the broker
            self.write_error = f"fuel floor tracking failed: {type(exc).__name__}: {exc}"

    def observe_van_level(self, value: object, mono: float) -> None:
        if type(value) is not int:
            return
        self._apply(lambda: self.model.observe_van_level(value, mono, self._now()), mono)

    def observe_engine(self, rpm: object, mono: float) -> None:
        positive = (isinstance(rpm, (int, float)) and not isinstance(rpm, bool) and rpm > 0)
        self._apply(lambda: self.model.observe_engine(positive, mono), mono, force_if=lambda _c: False)

    def observe_fuel_rate(self, rate: object, sample_mono: float, rpm: object, mono: float) -> None:
        running = (isinstance(rpm, (int, float)) and not isinstance(rpm, bool) and rpm >= C.RUNNING_RPM)
        positive = (isinstance(rpm, (int, float)) and not isinstance(rpm, bool) and rpm > 0)

        def action():
            self.model.observe_engine(positive, mono)
            self.model.observe_fuel_rate(rate, sample_mono, running)

        self._apply(action, mono, force_if=lambda _c: False)

    # -- exposure ------------------------------------------------------------

    def status_block(self) -> dict:
        return {**self._current_view()["status"], "storage_error": self.storage_error}

    def _payload(self, **refusal: object) -> dict:
        return {"available": not refusal, "persistent": self.path is not None,
                "storage_error": self.storage_error, **self._current_view()["listing"], **refusal}

    def response(self) -> dict:
        return self._payload()

    def request(self, payload: object, mono: float) -> tuple[int, dict]:
        """``set`` or ``restore``; the new state is adopted only once it is saved."""
        with self._write_lock:
            with self._model_lock:
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
                record = self.model.to_record()
                self._last_write = mono
                self._publish_view()
            if not self._write_record(record):
                with self._model_lock:
                    self.model.floor, self.model.earlier_floors = before
                    self.model.dirty = was_dirty
                    self._publish_view()
                return 503, self._payload(reason="storage_unavailable", detail=self.write_error)
            with self._model_lock:
                self.model.dirty = was_dirty  # the record just written held the whole state
            return 200, self._payload()
