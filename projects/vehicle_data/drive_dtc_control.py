"""Broker-side drive-DTC opt-in, schedule enforcement and bounded result storage.

The helper event path validates and enqueues only. One results thread parses
frames and publishes in arrival order; it is separate from the launch budget
writer, so neither can hold up the other or a broker status request.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import queue
import threading
import time

from lib.dtc_batch import atomic_json
from projects.vehicle_data import drive_dtc_contract as contract
from projects.vehicle_data.drive_dtc_budget import (
    DriveDtcBudget, Reservation, clear_temporary_files, json_size, read_bounded_json,
)
from projects.vehicle_data.helper_hold_control import StopWhenAny

RESULTS_FILENAME = contract.RESULTS_FILENAME
# One fixed sibling for an unreadable or outdated results file: replacing it
# each time keeps at most one set-aside copy beside the live file.
SET_ASIDE_FILENAME = 'drive-dtc-results.invalid.json'


def drive_dtc_arguments(control) -> list[str]:
    return control.launch_arguments() if control is not None else []


def grant_helper_ready(control, grant, event: dict, cancel_requested) -> None:
    """A missing optional capability cancels, never becomes a protocol fault."""
    if control is not None and not control.accept_ready(event):
        grant.cancel()
    else:
        grant.try_grant(cancel_requested)


def empty_results() -> dict:
    return {'schema': 1, 'coverage': contract.COVERAGE, 'passes': []}


def parsed_read(event: dict) -> dict:
    """Raw evidence that cannot be parsed is a storage value, never a fault."""
    try:
        parsed = contract.parse_frame(event['frame_hex'])
    except (ValueError, TypeError):
        parsed = {'outcome': 'unparsed'}
    return {**event, 'parsed': parsed}


def retain_read(payload: dict, event: dict) -> None:
    identity = (event['session'], event['pass'])
    rows = payload['passes']
    row = next((row for row in rows if (row['session'], row['pass']) == identity), None)
    if row is None:
        row = {'session': identity[0], 'pass': identity[1], 'reads': []}
        rows.append(row)
    # A module has one result in a pass; duplicates never grow the storage bound.
    row['reads'] = [read for read in row['reads'] if read['module'] != event['module']]
    row['reads'].append(parsed_read(event))
    del rows[:-contract.LIMITS['passes']]
    while rows and json_size(payload) > contract.LIMITS['results_bytes']:
        del rows[0]


def load_results(path: Path) -> dict:
    """The saved results, revalidated; raises for anything this writer cannot extend."""
    try:
        saved = read_bounded_json(path, contract.LIMITS['results_bytes'])
    except FileNotFoundError:
        return empty_results()
    if (set(saved) != {'schema', 'coverage', 'passes'} or type(saved['schema']) is not int
            or saved['schema'] != 1 or saved['coverage'] != contract.COVERAGE
            or not isinstance(saved['passes'], list)
            or len(saved['passes']) > contract.LIMITS['passes']):
        raise ValueError('invalid drive-DTC results file')
    payload = empty_results()
    identities = set()
    for row in saved['passes']:
        if (not isinstance(row, dict) or set(row) != {'session', 'pass', 'reads'}
                or not isinstance(row['reads'], list) or not 1 <= len(row['reads']) <= len(contract.MODULES)):
            raise ValueError('invalid drive-DTC stored pass')
        identity = (row['session'], row['pass'])
        if identity in identities:
            raise ValueError('duplicate drive-DTC stored pass')
        identities.add(identity)
        modules = set()
        for read in row['reads']:
            event = {key: value for key, value in read.items() if key != 'parsed'}
            contract.validate_event(event)
            if (event['type'] != contract.READ_EVENT or event['session'] != row['session']
                    or event['pass'] != row['pass'] or event['module'] in modules):
                raise ValueError('invalid drive-DTC stored read')
            modules.add(event['module'])
            retain_read(payload, event)
    return payload


class DriveDtcResults:
    """A queue of 64, newest 200 passes/400 reads, 256 KiB + one temp copy."""
    def __init__(self, directory: Path, *, write=atomic_json):
        self.path = Path(directory) / RESULTS_FILENAME
        self.failed = threading.Event()
        self._write = write
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=contract.LIMITS['result_queue'])
        self._closing = threading.Event()
        self._revoked_lock = threading.Lock()
        # Control disables DTC for its lifetime on the first rejected session,
        # so this can contain at most that one reservation in a broker process.
        self._revoked: set[str] = set()
        self._thread = threading.Thread(target=self._run, name='drive-dtc-results', daemon=True)
        self._thread.start()

    def enqueue(self, event: dict) -> bool:
        if self.failed.is_set() or self._closing.is_set():
            return False
        try:
            self._queue.put_nowait(dict(event))
        except queue.Full:
            self.failed.set()
            return False
        return True

    def discard_session(self, session: str) -> None:
        with self._revoked_lock:
            self._revoked.add(session)

    def close(self, timeout: float = 0.0) -> None:
        self._closing.set()
        self._thread.join(max(0.0, timeout))

    def _purge(self, payload: dict) -> bool:
        with self._revoked_lock:
            revoked = frozenset(self._revoked)
        rows = payload['passes']
        payload['passes'] = [row for row in rows if row['session'] not in revoked]
        return len(rows) != len(payload['passes'])

    def _publish(self, payload: dict) -> None:
        if json_size(payload) > contract.LIMITS['results_bytes']:
            raise ValueError('drive-DTC results exceed 256 KiB')
        clear_temporary_files(self.path)
        self._write(self.path, payload)

    def _load(self) -> dict:
        # Interrupted writes leave only this writer's own temporary files; a
        # failure to remove them is a storage fault, latched by the caller.
        clear_temporary_files(self.path)
        try:
            return load_results(self.path)
        except Exception:
            # A damaged file, or one an older or newer vocabulary rejects, is
            # set aside rather than stopping every later read from being kept.
            try:
                os.replace(self.path, self.path.with_name(SET_ASIDE_FILENAME))
            except OSError:
                pass  # the next publication replaces it in place
            return empty_results()

    def _run(self) -> None:
        try:
            payload = self._load()
        except Exception:
            self.failed.set()
            payload = empty_results()
        while True:
            try:
                event = self._queue.get(timeout=0.05)
            except queue.Empty:
                event = None
            try:
                changed = self._purge(payload)
                if event is not None and not self.failed.is_set():
                    contract.validate_event(event)
                    if event['type'] == contract.READ_EVENT:
                        with self._revoked_lock:
                            revoked = event['session'] in self._revoked
                        if not revoked:
                            retain_read(payload, event)
                            changed = True
                if changed:
                    self._publish(payload)
            except Exception:
                self.failed.set()
            finally:
                if event is not None:
                    self._queue.task_done()
            if self._closing.is_set() and self._queue.empty():
                break


@dataclass
class _Session:
    reservation: Reservation
    stop: threading.Event = field(default_factory=threading.Event)
    last_started: float | None = None
    last_pass: int | None = None
    rejected: bool = False


class DriveDtcControl:
    """An inert flag-off control; enabled instances own both bounded writers.

    ``run`` brackets the actual supervisor call, including exceptions and early
    launch failures. Only a completed reservation supplies arguments. Schedule
    violations stop normally; missing capability cancels before startup grant.
    Neither path changes restoration handling or the broker status schema.
    """
    def __init__(self, *, enabled: bool = False, directory: Path | None = None,
                 monotonic=time.monotonic, budget=None, results=None):
        self.enabled = enabled
        self._monotonic = monotonic
        self._disabled = threading.Event()
        self._session: _Session | None = None
        if enabled and directory is None and (budget is None or results is None):
            raise ValueError('enabled drive-DTC control needs its storage directory')
        self.budget = (budget or DriveDtcBudget(directory, monotonic=monotonic)) if enabled else None
        self.results = (results or DriveDtcResults(directory)) if enabled else None

    @classmethod
    def from_args(cls, args):
        return cls(enabled=args.enable_drive_dtc_reads,
                   directory=Path(args.dtc_cache).parent)

    @property
    def drive_dtc_storage_failed(self) -> bool:
        return bool(self.results is not None and self.results.failed.is_set())

    @property
    def available(self) -> bool:
        return self.enabled and not self._disabled.is_set() and not self.drive_dtc_storage_failed

    def launch_arguments(self) -> list[str]:
        session = self._session
        return session.reservation.launch_arguments() if session is not None else []

    def accept_ready(self, event: dict) -> bool:
        if self._session is None or contract.CAPABILITY in event.get('capabilities', []):
            return True
        self._disabled.set()
        return False

    def run(self, supervisor, stop_event) -> dict:
        reservation = self.budget.reserve() if self.available else None
        if not self.available:
            reservation = None  # A concurrent storage failure leaves the unused budget null-ended.
        session = _Session(reservation) if reservation is not None else None
        self._session = session
        try:
            stop = StopWhenAny(stop_event, session.stop) if session is not None else stop_event
            return supervisor.run(stop)
        finally:
            # Events may be missing, invalid or dropped; the durable budget never
            # uses them to shorten its conservative schedule bound.
            if reservation is not None:
                self.budget.end(reservation.reservation_id, self._monotonic())
            self._session = None

    def _reject_session(self) -> None:
        self._disabled.set()
        if self._session is not None:
            self._session.rejected = True
            self._session.stop.set()
            self.results.discard_session(self._session.reservation.reservation_id)

    def handle_event(self, event: dict) -> bool:
        """Consume only DTC events; no file access, waits, or broker lock needed."""
        kind = event.get('type')
        if not isinstance(kind, str) or kind not in contract.EVENT_TYPES:
            return False
        session = self._session
        try:
            contract.validate_event(event)
            if session is None or event['session'] != session.reservation.reservation_id:
                self._reject_session()
                return True
            if session.rejected:
                return True
            if event['type'] == contract.PASS_EVENT and event['state'] == 'started':
                self._pass_started(event, session)
            if not session.rejected and not self.drive_dtc_storage_failed:
                self.results.enqueue(event)
        except (ValueError, TypeError, KeyError, OverflowError):
            self._reject_session()
        return True

    def _pass_started(self, event: dict, session: _Session) -> None:
        reservation = session.reservation
        started = event['started_monotonic']
        allowed = contract.pass_start_allowed(
            origin=reservation.origin_monotonic, first_due=reservation.first_due,
            pass_index=event['pass'], now=started, previous_started=session.last_started)
        if (not allowed or started > self._monotonic()
                or (session.last_pass is not None and event['pass'] <= session.last_pass)):
            self._reject_session()
            return
        session.last_started, session.last_pass = started, event['pass']

    def close(self, timeout: float = 0.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        for writer in (self.budget, self.results):
            if writer is not None:
                writer.close(max(0.0, deadline - time.monotonic()))
