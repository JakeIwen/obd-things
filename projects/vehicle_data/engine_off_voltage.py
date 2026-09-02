#!/usr/bin/env python3
"""Persist one settled, passive voltage sample after a verified engine stop."""

from __future__ import annotations

import json
import math
import os
import pathlib
import stat
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

from projects.vehicle_data.models import AcquisitionResult


VERSION = 1
SERVICE = "van-telemetry"
ROLE = "engine_off_voltage"
DEFAULT_SETTLE_SECONDS = 30.0
MAX_STATUS_BYTES = 16 * 1024
ALLOWED_PASSIVE_SOURCES = {
    ("c-can", "ccan.broadcast.0x41a"),
    ("b-can", "bcan.broadcast.0x46c"),
}


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _valid_voltage(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 32.0
    )


def _atomic_json(path: pathlib.Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _validated_payload(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("version") != VERSION
        or payload.get("service") != SERVICE
        or payload.get("role") != ROLE
        or payload.get("sample_type") != "engine_off"
        or payload.get("unit") != "V"
        or payload.get("quality") != "verified"
        or payload.get("acquisition") not in ("passive", "passive_broadcast")
        or not _valid_voltage(payload.get("value"))
    ):
        return None
    bus = payload.get("bus")
    source = payload.get("source")
    if (bus, source) not in ALLOWED_PASSIVE_SOURCES:
        return None
    try:
        observed_at = _utc(
            datetime.fromisoformat(str(payload.get("observed_at"))),
            "observed_at",
        )
        engine_stopped_at = _utc(
            datetime.fromisoformat(str(payload.get("engine_stopped_at"))),
            "engine_stopped_at",
        )
        saved_at = _utc(
            datetime.fromisoformat(str(payload.get("saved_at"))),
            "saved_at",
        )
    except (TypeError, ValueError):
        return None
    if observed_at < engine_stopped_at or saved_at < observed_at:
        return None
    return {
        "version": VERSION,
        "service": SERVICE,
        "role": ROLE,
        "sample_type": "engine_off",
        "value": round(float(payload["value"]), 3),
        "unit": "V",
        "source": source,
        "bus": bus,
        "acquisition": payload["acquisition"],
        "quality": "verified",
        "observed_at": observed_at.isoformat(),
        "engine_stopped_at": engine_stopped_at.isoformat(),
        "saved_at": saved_at.isoformat(),
    }


def read_engine_off_voltage(
    path: pathlib.Path | str,
    *,
    maximum_bytes: int = MAX_STATUS_BYTES,
) -> dict[str, object] | None:
    """Read one bounded regular-file sample; reject links and malformed state."""

    target = pathlib.Path(path)
    try:
        metadata = target.lstat()
    except OSError:
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > maximum_bytes
    ):
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size <= 0
            or opened.st_size > maximum_bytes
        ):
            os.close(descriptor)
            return None
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            text = handle.read(maximum_bytes + 1)
        if len(text.encode("utf-8")) > maximum_bytes:
            return None
        payload = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return _validated_payload(payload)


class EngineOffVoltageCapture:
    """Collect the newest passive voltage during a bounded post-stop window."""

    def __init__(
        self,
        path: pathlib.Path | str,
        *,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        monotonic=time.monotonic,
        wall_clock=lambda: datetime.now(timezone.utc),
        writer=_atomic_json,
    ) -> None:
        if settle_seconds <= 0:
            raise ValueError("engine-off voltage settle interval must be positive")
        self.path = pathlib.Path(path)
        self.settle_seconds = float(settle_seconds)
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.writer = writer
        self._lock = threading.RLock()
        self._deadline: float | None = None
        self._engine_stopped_at: datetime | None = None
        self._candidate: dict[str, object] | None = None
        saved = read_engine_off_voltage(self.path)
        self._status: dict[str, object] = {
            "enabled": True,
            "state": "idle",
            "settle_seconds": self.settle_seconds,
            "armed_at": None,
            "settles_at": None,
            "last_error": None,
            "last_sample": saved,
        }

    def arm(self, *, engine_stopped_at: datetime | None = None) -> None:
        stopped_at = _utc(
            engine_stopped_at or self.wall_clock(), "engine_stopped_at"
        )
        now = self.monotonic()
        with self._lock:
            self._deadline = now + self.settle_seconds
            self._engine_stopped_at = stopped_at
            self._candidate = None
            self._status.update(
                {
                    "state": "collecting",
                    "armed_at": stopped_at.isoformat(),
                    "settles_at": (
                        stopped_at + timedelta(seconds=self.settle_seconds)
                    ).isoformat(),
                    "last_error": None,
                }
            )

    def cancel(self, detail: str) -> bool:
        with self._lock:
            if self._status["state"] != "collecting":
                return False
            self._deadline = None
            self._engine_stopped_at = None
            self._candidate = None
            self._status.update(
                {
                    "state": "cancelled",
                    "settles_at": None,
                    "last_error": detail,
                }
            )
            return True

    def _candidate_from(self, result: AcquisitionResult) -> dict[str, object] | None:
        stopped_at = self._engine_stopped_at
        if (
            stopped_at is None
            or not result.available
            or result.metric != "battery.voltage"
            or result.unit != "V"
            or result.quality != "verified"
            or result.acquisition not in ("passive", "passive_broadcast")
            or result.interface_mode not in (None, "listen_only")
            or (result.bus, result.source) not in ALLOWED_PASSIVE_SOURCES
            or not _valid_voltage(result.value)
            or result.observed_at is None
        ):
            return None
        try:
            observed_at = _utc(result.observed_at, "observed_at")
        except ValueError:
            return None
        if observed_at < stopped_at:
            return None
        return {
            "version": VERSION,
            "service": SERVICE,
            "role": ROLE,
            "sample_type": "engine_off",
            "value": round(float(result.value), 3),
            "unit": "V",
            "source": result.source,
            "bus": result.bus,
            "acquisition": result.acquisition,
            "quality": "verified",
            "observed_at": observed_at.isoformat(),
            "engine_stopped_at": stopped_at.isoformat(),
        }

    def observe(self, result: AcquisitionResult) -> bool:
        """Consider one collector result and finalize after the settle deadline."""

        payload = None
        with self._lock:
            if self._status["state"] != "collecting" or self._deadline is None:
                return False
            candidate = self._candidate_from(result)
            if candidate is not None:
                previous = self._candidate
                if previous is None or str(candidate["observed_at"]) >= str(
                    previous["observed_at"]
                ):
                    self._candidate = candidate
            if self.monotonic() < self._deadline:
                return False
            payload = dict(self._candidate) if self._candidate is not None else None
            self._deadline = None
            self._engine_stopped_at = None
            self._candidate = None

        if payload is None:
            with self._lock:
                self._status.update(
                    {
                        "state": "no_sample",
                        "settles_at": None,
                        "last_error": (
                            "no fresh passive voltage was observed during the "
                            "engine-off settling window"
                        ),
                    }
                )
            return True

        saved_at = _utc(self.wall_clock(), "saved_at")
        payload["saved_at"] = saved_at.isoformat()
        try:
            self.writer(self.path, payload)
        except OSError as exc:
            with self._lock:
                self._status.update(
                    {
                        "state": "error",
                        "settles_at": None,
                        "last_error": f"could not save engine-off voltage: {exc}",
                    }
                )
            return True
        with self._lock:
            self._status.update(
                {
                    "state": "complete",
                    "settles_at": None,
                    "last_error": None,
                    "last_sample": dict(payload),
                }
            )
        return True

    def status_snapshot(self) -> dict[str, object]:
        with self._lock:
            return json.loads(json.dumps(self._status))
