"""Durable per-boot incident and reservation files for USB drop recovery."""

from collections import defaultdict
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re

from lib.reports import atomic_json
from projects.vehicle_data.usb_recovery_contract import (
    KEEP_BOOTS, MAX_INCIDENTS_PER_BOOT, MAX_RECORD_BYTES, RECORD_DIR,
)
from projects.vehicle_data.usb_recovery_model import Incident, incident_from_dict, incident_to_dict

_BOOT = r"[a-zA-Z0-9_-]{1,64}"
_INCIDENT = r"[0-9a-f]{32}"
_INCIDENT_FILENAME = re.compile(rf"incident-({_BOOT})-({_INCIDENT})\.json")
_RESERVATION_FILENAME = re.compile(rf"reservation-({_BOOT})\.json")
_PENDING_FILENAME = re.compile(rf"\.pending-incident-({_BOOT})\.json")
_JSON_OPTS = {"indent": 2, "ensure_ascii": False}


def _reservation_path(directory: Path, boot_id: str) -> Path:
    if not re.fullmatch(_BOOT, boot_id):
        raise ValueError("invalid boot ID")
    return directory / f"reservation-{boot_id}.json"


def _incident_path(directory: Path, boot_id: str, incident_id: str) -> Path:
    if not re.fullmatch(_BOOT, boot_id) or not re.fullmatch(_INCIDENT, incident_id):
        raise ValueError("invalid incident ID or boot ID")
    return directory / f"incident-{boot_id}-{incident_id}.json"


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_directory(directory: Path) -> None:
    try:
        directory.mkdir(mode=0o750)
    except FileExistsError:
        return
    except FileNotFoundError:
        _ensure_directory(directory.parent)
        directory.mkdir(mode=0o750, exist_ok=True)
    _sync_directory(directory.parent)


@contextmanager
def _write_lock(directory: Path):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # Serialize the count and write without leaving a second lock file to maintain.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def reservation_exists(boot_id: str, *, directory: Path = RECORD_DIR) -> bool:
    path = _reservation_path(directory, boot_id)
    try:
        # An empty/truncated reservation after a crash still spends the budget.
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def reserve(
    boot_id: str, incident_id: str, *, broker_pid: int, reserved_at: str,
    directory: Path = RECORD_DIR,
) -> None:
    path = _reservation_path(directory, boot_id)
    _incident_path(directory, boot_id, incident_id)
    payload = {
        "boot_id": boot_id, "incident_id": incident_id, "broker_pid": broker_pid,
        "reserved_at": reserved_at,
    }
    data = _encode(payload)
    _ensure_directory(directory)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(fd, "wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    # A failed write/sync never gives this boot another try.
    _sync_directory(directory)
    _sync_directory(directory.parent)


def _encode(payload: object) -> bytes:
    data = (json.dumps(payload, allow_nan=False, **_JSON_OPTS) + "\n").encode("utf-8")
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError("USB recovery record exceeds its byte limit")
    return data


def _owned_paths(directory: Path):
    for path in directory.iterdir():
        if match := _INCIDENT_FILENAME.fullmatch(path.name):
            yield path, match[1], match[2]
        elif match := _RESERVATION_FILENAME.fullmatch(path.name) or _PENDING_FILENAME.fullmatch(path.name):
            yield path, match[1], None


def write_incident(incident: Incident, *, directory: Path = RECORD_DIR) -> None:
    path = _incident_path(directory, incident.boot_id, incident.incident_id)
    payload = incident_to_dict(incident)
    _encode(payload)
    _ensure_directory(directory)
    with _write_lock(directory):
        _prune(incident.boot_id, directory)
        ids = {
            incident_id for _, boot_id, incident_id in _owned_paths(directory)
            if boot_id == incident.boot_id and incident_id is not None
        }
        if incident.incident_id not in ids and len(ids) >= MAX_INCIDENTS_PER_BOOT:
            raise OSError("USB recovery incident limit reached for this boot")
        # Reuse one pending pathname per boot so repeated crashes cannot accumulate temp files.
        pending = directory / f".pending-incident-{incident.boot_id}.json"
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        atomic_json(path, payload, temporary=pending, fsync=True, fsync_dir=True, **_JSON_OPTS)


def read_incident(path: Path) -> Incident:
    with path.open("rb") as file:
        data = file.read(MAX_RECORD_BYTES + 1)
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError("USB recovery record exceeds its byte limit")
    return incident_from_dict(json.loads(data))


def _prune(boot_id: str, directory: Path) -> None:
    paths_by_boot = defaultdict(list)
    for path, boot, _ in _owned_paths(directory):
        if boot == boot_id:
            continue
        try:
            mtime = path.lstat().st_mtime_ns
        except FileNotFoundError:
            continue
        paths_by_boot[boot].append((mtime, path))
    oldest = sorted(paths_by_boot, key=lambda b: (max(t for t, _ in paths_by_boot[b]), b))
    for boot in oldest[:max(0, len(oldest) - (KEEP_BOOTS - 1))]:
        for _, path in paths_by_boot[boot]:
            path.unlink(missing_ok=True)


def prune(boot_id: str, *, directory: Path = RECORD_DIR) -> None:
    _reservation_path(directory, boot_id)
    try:
        with _write_lock(directory):
            _prune(boot_id, directory)
            _sync_directory(directory)
    except FileNotFoundError:
        if directory.exists():
            raise
