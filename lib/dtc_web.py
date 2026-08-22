"""One-use local arming and fixed request records for web-supervised DTC jobs.

This module performs no CAN or network I/O.  A local operator creates a
short-lived secret whose SHA-256 digest is stored under ``/run``.  The
Tailscale-only web listener can consume that secret once and atomically place
one closed-schema request for the separately sandboxed batch worker.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import stat
import tempfile
import time
from typing import Any, Iterator, Mapping

from lib.dtc_batch import JOB_ID_RE


ARM_SCHEMA_VERSION = 1
REQUEST_SCHEMA_VERSION = 1
DEFAULT_ARM_PATH = Path("/run/van-telemetry/dtc-web-arm.json")
DEFAULT_REQUEST_PATH = Path("/run/van-telemetry/dtc-batch.request.json")
DEFAULT_CURRENT_PATH = Path("/run/van-telemetry/dtc-batch-current.json")
DEFAULT_CANCEL_DIR = Path("/run/van-telemetry/dtc-batch-cancel")
DEFAULT_JOB_ROOT = (
    Path(__file__).resolve().parents[1]
    / "tmp"
    / "inventories"
    / "dtc-batch"
)
DEFAULT_ARM_TTL_SECONDS = 5 * 60
MAX_RECORD_BYTES = 4096


class DtcWebAuthorizationError(RuntimeError):
    """A one-use local authorization is absent, expired, or invalid."""


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


def _atomic_replace_json(path: Path, payload: Mapping[str, object]) -> None:
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
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


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


class ArmTokenStore:
    def __init__(self, path: str | Path = DEFAULT_ARM_PATH) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        _require_runtime_parent(self.path)
        descriptor = os.open(
            self.lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def issue(
        self,
        *,
        ttl_seconds: int = DEFAULT_ARM_TTL_SECONDS,
        now: float | None = None,
    ) -> dict[str, object]:
        if (
            not isinstance(ttl_seconds, int)
            or isinstance(ttl_seconds, bool)
            or not 30 <= ttl_seconds <= 15 * 60
        ):
            raise ValueError("DTC web arm TTL must be 30..900 seconds")
        issued_at = time.time() if now is None else float(now)
        if not math.isfinite(issued_at):
            raise ValueError("DTC web arm time must be finite")
        token = secrets.token_urlsafe(32)
        record = {
            "schema_version": ARM_SCHEMA_VERSION,
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "issued_at_epoch": issued_at,
            "expires_at_epoch": issued_at + ttl_seconds,
            "purpose": "scan_registered_dtcs_once",
        }
        with self._locked():
            _atomic_replace_json(self.path, record)
        return {
            "token": token,
            "expires_at_epoch": record["expires_at_epoch"],
            "ttl_seconds": ttl_seconds,
        }

    def consume(self, token: str, *, now: float | None = None) -> None:
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise DtcWebAuthorizationError("DTC arm token is invalid")
        checked_at = time.time() if now is None else float(now)
        if not math.isfinite(checked_at):
            raise DtcWebAuthorizationError("DTC arm check time is invalid")
        with self._locked():
            try:
                record = _read_private_json(self.path)
            except FileNotFoundError as exc:
                raise DtcWebAuthorizationError(
                    "no local DTC authorization is armed"
                ) from exc
            try:
                valid_shape = (
                    record.get("schema_version") == ARM_SCHEMA_VERSION
                    and record.get("purpose") == "scan_registered_dtcs_once"
                    and isinstance(record.get("token_sha256"), str)
                    and isinstance(record.get("expires_at_epoch"), (int, float))
                    and not isinstance(record.get("expires_at_epoch"), bool)
                )
            except Exception:
                valid_shape = False
            if not valid_shape:
                raise DtcWebAuthorizationError("DTC arm record is malformed")
            if (
                not math.isfinite(float(record["expires_at_epoch"]))
                or checked_at >= float(record["expires_at_epoch"])
            ):
                self.path.unlink(missing_ok=True)
                raise DtcWebAuthorizationError("DTC arm token has expired")
            presented = hashlib.sha256(token.encode()).hexdigest()
            if not hmac.compare_digest(presented, str(record["token_sha256"])):
                raise DtcWebAuthorizationError("DTC arm token is invalid")
            # Consume before queuing. A queue failure cannot leave a reusable
            # network authorization behind; the operator must arm again.
            self.path.unlink()


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


__all__ = [
    "ArmTokenStore",
    "DEFAULT_ARM_PATH",
    "DEFAULT_CANCEL_DIR",
    "DEFAULT_CURRENT_PATH",
    "DEFAULT_JOB_ROOT",
    "DEFAULT_REQUEST_PATH",
    "DtcWebAuthorizationError",
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
