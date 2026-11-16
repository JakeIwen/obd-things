"""Serialized, crash-durable reservations for the drive-DTC elapsed cooldown.

Only the writer thread touches files or its current record. Caller timeouts do
not cancel filesystem operations: an abandoned write may finish, but its null
session end deliberately forces the next reservation to wait a whole hour.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import threading
import time
import uuid

from lib.dtc_batch import atomic_json
from lib.timeutil import finite_number
from projects.vehicle_data.van_scan_cache import VAN_SCAN_CACHE_NAME
from projects.vehicle_data.drive_dtc_contract import (
    INTERVAL_SECONDS, MINIMUM_SESSION_SECONDS, WINDOW_SECONDS, session_id_valid,
    window_start,
)

RESERVATION_TIMEOUT_SECONDS = 2.0
QUEUE_SIZE = 2
MAX_RECORD_BYTES = 1024
BUDGET_FILENAME = 'drive-dtc-budget.json'
HARVEST_FILENAME = VAN_SCAN_CACHE_NAME
# This cache is external evidence, never an unbounded read on the launch path.
MAX_HARVEST_BYTES = 1024 * 1024
BOOT_ID_PATH = Path('/proc/sys/kernel/random/boot_id')


def json_size(payload: dict) -> int:
    """Match atomic_json's encoding, including indentation and final newline."""
    return len((json.dumps(payload, indent=2, sort_keys=True) + '\n').encode('utf-8'))


def read_bounded_json(path: Path, limit: int) -> dict:
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('drive-DTC JSON exceeds its bound')
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError('drive-DTC JSON must be an object')
    return result


def clear_temporary_files(path: Path) -> None:
    """Remove only this writer's interrupted atomic_json files before another copy.

    One writer owns each fixed destination. Failed cleanup prevents publication,
    so repeated crashes cannot accumulate unbounded orphan copies.
    """
    for temporary in path.parent.glob(f'.{path.name}.*.tmp'):
        temporary.unlink()


def durable_json(path: Path, payload: dict) -> None:
    """Reuse the batch writer's temp/fsync/rename, then persist the directory."""
    clear_temporary_files(path)
    atomic_json(path, payload)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    boot_id: str
    origin_monotonic: float
    first_due: float
    interval: int = INTERVAL_SECONDS
    window: int = WINDOW_SECONDS
    session_end_monotonic: float | None = None
    last_pass_bound_monotonic: float | None = None
    schema: int = 1

    def launch_arguments(self) -> list[str]:
        return ['--enable-drive-dtc', '--drive-dtc-reservation', self.reservation_id,
                '--drive-dtc-origin', str(self.origin_monotonic),
                '--drive-dtc-first-due', str(self.first_due)]


def parse_record(payload: dict) -> Reservation:
    if set(payload) != set(Reservation.__dataclass_fields__):
        raise ValueError('invalid drive-DTC budget fields')
    record = Reservation(**payload)
    if (type(record.schema) is not int or record.schema != 1
            or not session_id_valid(record.reservation_id)
            or not isinstance(record.boot_id, str)
            or str(uuid.UUID(record.boot_id)) != record.boot_id
            or type(record.interval) is not int or record.interval != INTERVAL_SECONDS
            or type(record.window) is not int or record.window != WINDOW_SECONDS):
        raise ValueError('invalid drive-DTC budget identity')
    window_start(record.origin_monotonic, record.first_due, 0)
    end, bound = record.session_end_monotonic, record.last_pass_bound_monotonic
    if end is not None and (not finite_number(end) or end < record.origin_monotonic):
        raise ValueError('invalid drive-DTC session end')
    if bound is not None and (not finite_number(bound) or bound < 0
                              or (end is not None and bound > end)):
        raise ValueError('invalid drive-DTC pass bound')
    return record


def first_due(previous: Reservation | None, *, boot_id: str, now: float,
              harvest_completed: datetime | None, wall_now: datetime) -> float:
    """Conservative same-boot cooldown, with launch-only van harvest evidence."""
    uncertain = (previous is None or previous.boot_id != boot_id
                 or previous.session_end_monotonic is None
                 or previous.session_end_monotonic > now)
    due = float(INTERVAL_SECONDS) if uncertain else 0.0
    if not uncertain and previous.last_pass_bound_monotonic is not None:
        elapsed = now - previous.last_pass_bound_monotonic
        due = min(INTERVAL_SECONDS, max(0.0, INTERVAL_SECONDS - elapsed))
    if harvest_completed is not None:
        age = (wall_now - harvest_completed).total_seconds()
        due = max(due, min(INTERVAL_SECONDS, max(0.0, INTERVAL_SECONDS - age)))
    return max(float(MINIMUM_SESSION_SECONDS), due)


def ended_record(record: Reservation, ended: float) -> Reservation:
    if not finite_number(ended) or ended < record.origin_monotonic:
        raise ValueError('invalid drive-DTC session end')
    first = window_start(record.origin_monotonic, record.first_due, 0)
    bound = record.last_pass_bound_monotonic
    if ended >= first:
        index = int((ended - first) // INTERVAL_SECONDS)
        latest = min(ended, window_start(record.origin_monotonic, record.first_due, index)
                     + WINDOW_SECONDS)
        bound = latest if bound is None else max(bound, latest)
    return replace(record, session_end_monotonic=ended, last_pass_bound_monotonic=bound)


@dataclass
class _Operation:
    reservation_id: str | None = None
    ended: float | None = None
    done: threading.Event = field(default_factory=threading.Event)
    abandoned: threading.Event = field(default_factory=threading.Event)
    result: Reservation | None = None


class DriveDtcBudget:
    """One writer, two queued operations, at most two seconds to reserve.

    ``end`` is asynchronous and identity checked. ``close`` is bounded even if
    storage is stuck; it never waits for the writer while holding a broker lock.
    """
    def __init__(self, directory: Path, *, monotonic=time.monotonic,
                 wall_now=lambda: datetime.now(timezone.utc), boot_id=None,
                 write=durable_json):
        self.path = Path(directory) / BUDGET_FILENAME
        self.harvest_path = Path(directory) / HARVEST_FILENAME
        self._monotonic, self._wall_now = monotonic, wall_now
        self._boot_id = boot_id or (lambda: BOOT_ID_PATH.read_text().strip())
        self._write = write
        self._queue: queue.Queue[_Operation] = queue.Queue(maxsize=QUEUE_SIZE)
        self._closing = threading.Event()
        self._record: Reservation | None = None
        self._thread = threading.Thread(target=self._run, name='drive-dtc-budget', daemon=True)
        self._thread.start()

    def reserve(self) -> Reservation | None:
        deadline = time.monotonic() + RESERVATION_TIMEOUT_SECONDS
        operation = _Operation()
        if not self._submit(operation):
            return None
        completed = operation.done.wait(max(0.0, deadline - time.monotonic()))
        if not completed or time.monotonic() > deadline:
            operation.abandoned.set()
            return None
        return operation.result

    def end(self, reservation_id: str, ended: float) -> bool:
        return self._submit(_Operation(reservation_id=reservation_id, ended=ended))

    def close(self, timeout: float = 0.0) -> None:
        self._closing.set()
        self._thread.join(max(0.0, timeout))

    def _submit(self, operation: _Operation) -> bool:
        if self._closing.is_set():
            return False
        try:
            self._queue.put_nowait(operation)
        except queue.Full:
            return False
        return True

    def _load(self) -> None:
        try:
            clear_temporary_files(self.path)
            self._record = parse_record(read_bounded_json(self.path, MAX_RECORD_BYTES))
        except (OSError, ValueError, TypeError, OverflowError):
            self._record = None

    def _harvest_completed(self) -> datetime | None:
        try:
            payload = read_bounded_json(self.harvest_path, MAX_HARVEST_BYTES)
            value = payload['last_scan']['completed_at']
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return stamp if stamp.tzinfo is not None else None
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            return None

    def _reserve(self) -> Reservation:
        boot, now = self._boot_id(), self._monotonic()
        previous = self._record
        due = first_due(previous, boot_id=boot, now=now,
                        harvest_completed=self._harvest_completed(), wall_now=self._wall_now())
        bound = previous.last_pass_bound_monotonic if previous and previous.boot_id == boot else None
        if bound is not None and bound > now:
            bound = None
        return Reservation(uuid.uuid4().hex, boot, now, due, last_pass_bound_monotonic=bound)

    def _apply(self, operation: _Operation) -> None:
        if operation.abandoned.is_set():
            return
        if operation.reservation_id is None:
            record = self._reserve()
        elif self._record is not None and operation.reservation_id == self._record.reservation_id:
            record = ended_record(self._record, operation.ended)
        else:
            return  # A delayed final from a previous helper cannot modify a new reservation.
        payload = asdict(record)
        parse_record(payload)
        if json_size(payload) > MAX_RECORD_BYTES:
            raise ValueError('drive-DTC budget exceeds one KiB')
        # Keep the conservative pending record even if rename or directory fsync fails.
        self._record = replace(record, session_end_monotonic=None)
        self._write(self.path, payload)
        self._record = record
        if not operation.abandoned.is_set():
            operation.result = record

    def _run(self) -> None:
        self._load()
        while not self._closing.is_set() or not self._queue.empty():
            try:
                operation = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                self._apply(operation)
            except Exception:
                # Any uncertainty is a launch without capability, never a helper failure.
                operation.result = None
            finally:
                operation.done.set()
                self._queue.task_done()
