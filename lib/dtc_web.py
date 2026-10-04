"""Fixed request records for guarded DTC jobs.

This module performs no CAN or network I/O. The origin-restricted web listener
queues one closed-schema request after explicit parked confirmations.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
import secrets
import stat
import tempfile
import threading
import time
import uuid
from typing import Any, Mapping

from lib.dtc_batch import FINAL_JOB_STATES, JOB_ID_RE, JobStore, atomic_json


REQUEST_SCHEMA_VERSION = 1
DEFAULT_REQUEST_PATH = Path("/run/van-telemetry/dtc-batch.request.json")
DEFAULT_CURRENT_PATH = Path("/run/van-telemetry/dtc-batch-current.json")
DEFAULT_CANCEL_DIR = Path("/run/van-telemetry/dtc-batch-cancel")
DEFAULT_JOB_ROOT = (
    Path(__file__).resolve().parents[1]
    / "tmp"
    / "inventories"
    / "dtc-batch"
)
MAX_RECORD_BYTES = 4096
CURRENT_JOB_ID_RE = re.compile(r"dtc-web-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\Z")


class DtcWebRequestError(RuntimeError):
    """A fixed DTC job request cannot be queued or consumed safely."""


def _require_runtime_parent(path: Path) -> None:
    try:
        info = path.parent.stat()
    except FileNotFoundError as exc:
        raise DtcWebRequestError(
            f"runtime directory {path.parent} is unavailable; telemetry must be running"
        ) from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o007
    ):
        raise DtcWebRequestError(
            f"runtime directory {path.parent} must be owned by the worker user "
            "and inaccessible to other users"
        )


def _atomic_create_json(path: Path, payload: Mapping[str, object]) -> None:
    _require_runtime_parent(path)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise DtcWebRequestError("a DTC batch request is already queued") from exc
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_private_json(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise DtcWebRequestError(f"{path} is not a regular file")
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise DtcWebRequestError(
                f"{path} must be owned by the worker user with mode 0600"
            )
        if info.st_size <= 0 or info.st_size > MAX_RECORD_BYTES:
            raise DtcWebRequestError(f"{path} has an invalid size")
        chunks = []
        remaining = MAX_RECORD_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DtcWebRequestError(f"{path} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise DtcWebRequestError(f"{path} must contain a JSON object")
    return payload


def build_request(job_id: str, *, now: float | None = None) -> dict[str, object]:
    if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
        raise DtcWebRequestError("unsafe DTC batch job id")
    created = time.time() if now is None else float(now)
    if not math.isfinite(created):
        raise DtcWebRequestError("DTC request time must be finite")
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "job_id": job_id,
        "created_at_epoch": created,
        "action": "scan_registered_dtcs",
        "confirm_parked": True,
        "confirm_park_gear": True,
        "confirm_ignition_on_engine_off": True,
        "request_hex": "19 02 FF",
        "clear_requested": False,
    }


def cancel_path_for_job(directory: str | Path, job_id: str) -> Path:
    if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
        raise DtcWebRequestError("unsafe DTC batch job id")
    return Path(directory) / f"{job_id}.json"


def build_cancel_request(job_id: str, *, now: float | None = None) -> dict[str, object]:
    if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
        raise DtcWebRequestError("unsafe DTC batch job id")
    created = time.time() if now is None else float(now)
    if not math.isfinite(created):
        raise DtcWebRequestError("DTC cancel time must be finite")
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "job_id": job_id,
        "created_at_epoch": created,
        "action": "cancel_dtc_batch",
    }


def validate_cancel_request(
    payload: Mapping[str, object],
    *,
    expected_job_id: str | None = None,
    now: float | None = None,
    maximum_age_seconds: float = 5 * 60,
) -> dict[str, object]:
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version",
        "job_id",
        "created_at_epoch",
        "action",
    }:
        raise DtcWebRequestError("DTC cancel request schema is not exact")
    job_id = payload.get("job_id")
    created = payload.get("created_at_epoch")
    if (
        payload.get("schema_version") != REQUEST_SCHEMA_VERSION
        or not isinstance(job_id, str)
        or JOB_ID_RE.fullmatch(job_id) is None
        or not isinstance(created, (int, float))
        or isinstance(created, bool)
        or not math.isfinite(float(created))
        or payload.get("action") != "cancel_dtc_batch"
        or (expected_job_id is not None and job_id != expected_job_id)
    ):
        raise DtcWebRequestError("DTC cancel request violates the fixed policy")
    checked = time.time() if now is None else float(now)
    if not math.isfinite(checked):
        raise DtcWebRequestError("DTC cancel check time is invalid")
    age = checked - float(created)
    if age < -5 or age > maximum_age_seconds:
        raise DtcWebRequestError("DTC cancel request is expired or future-dated")
    return dict(payload)


def queue_cancel_request(
    path: str | Path,
    job_id: str,
    *,
    now: float | None = None,
) -> dict[str, object]:
    record = build_cancel_request(job_id, now=now)
    _atomic_create_json(Path(path), record)
    return record


def read_cancel_request(
    path: str | Path,
    *,
    expected_job_id: str | None = None,
    now: float | None = None,
) -> dict[str, object]:
    return validate_cancel_request(
        _read_private_json(Path(path)),
        expected_job_id=expected_job_id,
        now=now,
    )


def queue_request(
    path: str | Path,
    request: Mapping[str, object],
    *,
    now: float | None = None,
) -> None:
    validate_request(request, now=now)
    _atomic_create_json(Path(path), request)


def validate_request(
    payload: Mapping[str, object],
    *,
    now: float | None = None,
    maximum_age_seconds: float = 5 * 60,
) -> dict[str, object]:
    expected = {
        "schema_version",
        "job_id",
        "created_at_epoch",
        "action",
        "confirm_parked",
        "confirm_park_gear",
        "confirm_ignition_on_engine_off",
        "request_hex",
        "clear_requested",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise DtcWebRequestError("DTC worker request schema is not exact")
    job_id = payload.get("job_id")
    created = payload.get("created_at_epoch")
    valid = (
        payload.get("schema_version") == REQUEST_SCHEMA_VERSION
        and isinstance(job_id, str)
        and JOB_ID_RE.fullmatch(job_id) is not None
        and isinstance(created, (int, float))
        and not isinstance(created, bool)
        and math.isfinite(float(created))
        and payload.get("action") == "scan_registered_dtcs"
        and payload.get("confirm_parked") is True
        and payload.get("confirm_park_gear") is True
        and payload.get("confirm_ignition_on_engine_off") is True
        and payload.get("request_hex") == "19 02 FF"
        and payload.get("clear_requested") is False
    )
    if not valid:
        raise DtcWebRequestError("DTC worker request violates the fixed policy")
    checked = time.time() if now is None else float(now)
    if not math.isfinite(checked):
        raise DtcWebRequestError("DTC worker request check time is invalid")
    age = checked - float(created)
    if age < -5 or age > maximum_age_seconds:
        raise DtcWebRequestError("DTC worker request is expired or future-dated")
    return dict(payload)


def claim_request(
    path: str | Path,
    *,
    now: float | None = None,
) -> dict[str, object]:
    request_path = Path(path)
    claimed = request_path.with_name(
        f".{request_path.name}.claiming-{os.getpid()}-{secrets.token_hex(6)}"
    )
    try:
        os.replace(request_path, claimed)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise DtcWebRequestError("could not atomically claim DTC request") from exc
    # Validate only after the watched pathname is gone. Invalid, stale,
    # symlinked, or wrong-mode records stay quarantined at the non-watched
    # claim path, preventing a systemd PathExists restart loop and closing the
    # prior read-then-rename race.
    payload = validate_request(_read_private_json(claimed), now=now)
    payload["claimed_path"] = str(claimed)
    return payload


class DtcWebController:
    """Queue and observe one fixed batch without holding active CAN privileges."""

    def __init__(
        self,
        *,
        request_path: str | Path = DEFAULT_REQUEST_PATH,
        current_path: str | Path = DEFAULT_CURRENT_PATH,
        cancel_dir: str | Path = DEFAULT_CANCEL_DIR,
        job_root: str | Path = DEFAULT_JOB_ROOT,
    ) -> None:
        self.request_path = Path(request_path)
        self.current_path = Path(current_path)
        self.cancel_dir = Path(cancel_dir)
        self.cancel_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
        cancel_dir_info = self.cancel_dir.stat()
        if cancel_dir_info.st_uid != os.geteuid() or cancel_dir_info.st_mode & 0o007:
            raise DtcWebRequestError(
                "DTC cancel directory must be owned by the web user and private"
            )
        self.job_root = Path(job_root)
        self._lock = threading.Lock()

    @staticmethod
    def _public_job(record: dict[str, Any]) -> dict[str, Any]:
        modules = []
        for row in record.get("modules", []):
            if not isinstance(row, dict):
                continue
            modules.append(
                {
                    key: row.get(key)
                    for key in (
                        "module_key",
                        "logical_bus",
                        "state",
                        "reason",
                        "outcome",
                        "dtc_count",
                    )
                }
            )
        return {
            key: record.get(key)
            for key in (
                "schema_version",
                "job_id",
                "state",
                "created_at",
                "updated_at",
                "started_at",
                "completed_at",
                "current_bus",
                "current_module",
                "cancel_requested",
                "failure",
                "restoration_failure",
                "progress",
            )
        } | {"modules": modules}

    def _pointer(self) -> dict[str, Any] | None:
        try:
            raw = self.current_path.read_bytes()
        except FileNotFoundError:
            return None
        if len(raw) > 4096:
            raise DtcWebRequestError("current DTC job pointer is oversized")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DtcWebRequestError("current DTC job pointer is malformed") from exc
        if not isinstance(payload, dict):
            raise DtcWebRequestError("current DTC job pointer is malformed")
        job_id = payload.get("job_id")
        if not isinstance(job_id, str) or CURRENT_JOB_ID_RE.fullmatch(job_id) is None:
            raise DtcWebRequestError("current DTC job id is invalid")
        return payload

    def _cancel_path(self, job_id: str) -> Path:
        return cancel_path_for_job(self.cancel_dir, job_id)

    def status(self) -> dict[str, Any]:
        with self._lock:
            pointer = self._pointer()
            if pointer is None:
                return {"available": True, "enabled": True, "state": "idle", "job": None}
            store = JobStore(self.job_root, str(pointer["job_id"]))
            try:
                record = store.read()
            except FileNotFoundError:
                record = pointer
            if record.get("state") not in FINAL_JOB_STATES:
                try:
                    read_cancel_request(
                        self._cancel_path(str(pointer["job_id"])),
                        expected_job_id=str(pointer["job_id"]),
                    )
                except FileNotFoundError:
                    pass
                else:
                    record = {**record, "cancel_requested": True}
            return {
                "available": True,
                "enabled": True,
                "state": record.get("state", "starting"),
                "job": self._public_job(record),
            }

    def start(self) -> dict[str, Any]:
        with self._lock:
            current = self._pointer()
            if current is not None:
                try:
                    state = JobStore(
                        self.job_root, str(current["job_id"])
                    ).read().get("state")
                except FileNotFoundError:
                    state = current.get("state")
                if state == "restoration_failed":
                    raise DtcWebRequestError(
                        "the previous job has an unverified restoration; inspect all "
                        "roles and clear the same-boot inhibit locally before manually "
                        "retiring the current-job pointer"
                    )
                if state not in FINAL_JOB_STATES:
                    raise DtcWebRequestError("a DTC batch is already queued or running")
            if self.request_path.exists():
                raise DtcWebRequestError("a DTC batch request is already queued")
            # Origin-restricted UI confirmation authorizes the guarded request.
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            job_id = f"dtc-web-{stamp}-{uuid.uuid4().hex[:8]}"
            request = build_request(job_id)
            pointer = {
                "schema_version": 1,
                "job_id": job_id,
                "state": "queued",
                "created_at_epoch": request["created_at_epoch"],
            }
            atomic_json(self.current_path, pointer)
            try:
                queue_request(self.request_path, request)
            except BaseException as exc:
                atomic_json(
                    self.current_path,
                    {
                        **pointer,
                        "state": "failed",
                        "failure": f"request queue failed: {type(exc).__name__}: {exc}",
                    },
                )
                raise
            return {
                "available": True,
                "enabled": True,
                "state": "queued",
                "job": self._public_job(pointer),
            }

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            current = self._pointer()
            if current is None:
                raise DtcWebRequestError("there is no current DTC batch")
            # If systemd has not claimed the request yet, atomically move it
            # out of the watched name. A concurrently claimed request simply
            # falls through to the worker's cooperative cancel flag.
            if self.request_path.exists():
                cancelled_path = self.request_path.with_name(
                    f"{self.request_path.name}.cancelled-{current['job_id']}"
                )
                try:
                    os.replace(self.request_path, cancelled_path)
                except FileNotFoundError:
                    pass
                else:
                    cancelled = {
                        **current,
                        "state": "cancelled",
                        "cancel_requested": True,
                        "failure": "cancelled before the worker claimed the request",
                    }
                    atomic_json(self.current_path, cancelled)
                    return {
                        "available": True,
                        "enabled": True,
                        "state": "cancelled",
                        "job": self._public_job(cancelled),
                    }
            store = JobStore(self.job_root, str(current["job_id"]))
            try:
                record = store.read()
            except FileNotFoundError:
                queue_cancel_request(
                    self._cancel_path(str(current["job_id"])),
                    str(current["job_id"]),
                )
                return {
                    "available": True,
                    "enabled": True,
                    "state": current.get("state", "starting"),
                    "job": self._public_job(
                        {**current, "cancel_requested": True}
                    ),
                }
            if record.get("state") in FINAL_JOB_STATES:
                raise DtcWebRequestError(f"DTC batch is already {record.get('state')}")
            try:
                queue_cancel_request(
                    self._cancel_path(str(current["job_id"])),
                    str(current["job_id"]),
                )
            except DtcWebRequestError:
                # An existing request for this same job is idempotent; any
                # malformed/stale record remains a hard failure.
                read_cancel_request(
                    self._cancel_path(str(current["job_id"])),
                    expected_job_id=str(current["job_id"]),
                )
            return {
                "available": True,
                "enabled": True,
                "state": record.get("state"),
                "job": self._public_job(
                    {**store.read(), "cancel_requested": True}
                ),
            }


__all__ = [
    "DtcWebController",
    "DEFAULT_CANCEL_DIR",
    "DEFAULT_CURRENT_PATH",
    "DEFAULT_JOB_ROOT",
    "DEFAULT_REQUEST_PATH",
    "DtcWebRequestError",
    "build_request",
    "build_cancel_request",
    "claim_request",
    "queue_request",
    "queue_cancel_request",
    "read_cancel_request",
    "validate_cancel_request",
    "cancel_path_for_job",
    "validate_request",
]
