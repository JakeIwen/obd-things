"""Structured telemetry acquisition and cache result models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


ScalarValue = bool | int | float | str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AcquisitionResult:
    metric: str
    available: bool
    unit: str
    value: ScalarValue | None = None
    source: str | None = None
    bus: str | None = None
    acquisition: str | None = None
    quality: str | None = None
    observed_at: datetime | None = None
    observed_monotonic: float | None = None
    reason: str | None = None
    detail: str = ""
    coalesced: bool = False

    def with_coalesced(self) -> "AcquisitionResult":
        return AcquisitionResult(
            **{
                **self.__dict__,
                "coalesced": True,
            }
        )

    def as_dict(
        self,
        *,
        now_monotonic: float,
        stale_after_seconds: float,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "metric": self.metric,
            "available": self.available,
            "unit": self.unit,
        }
        if self.available:
            age_seconds = (
                max(0.0, now_monotonic - self.observed_monotonic)
                if self.observed_monotonic is not None
                else None
            )
            payload.update(
                {
                    "value": self.value,
                    "source": self.source,
                    "bus": self.bus,
                    "acquisition": self.acquisition,
                    "quality": self.quality,
                    "observed_at": (
                        self.observed_at.isoformat()
                        if self.observed_at is not None
                        else None
                    ),
                    "age_ms": (
                        round(age_seconds * 1000) if age_seconds is not None else None
                    ),
                    "stale": (
                        age_seconds is None
                        or age_seconds > stale_after_seconds
                    ),
                }
            )
        else:
            payload.update(
                {
                    "reason": self.reason or "source_unavailable",
                    "detail": self.detail,
                    "bus": self.bus,
                }
            )
        if self.coalesced:
            payload["coalesced"] = True
        return payload


def success(
    *,
    metric: str,
    unit: str,
    value: ScalarValue,
    source: str,
    bus: str,
    acquisition: str,
    quality: str,
    observed_monotonic: float,
    observed_at: datetime | None = None,
    detail: str = "",
) -> AcquisitionResult:
    return AcquisitionResult(
        metric=metric,
        available=True,
        unit=unit,
        value=value,
        source=source,
        bus=bus,
        acquisition=acquisition,
        quality=quality,
        observed_at=observed_at or utc_now(),
        observed_monotonic=observed_monotonic,
        detail=detail,
    )


def failure(
    *,
    metric: str,
    unit: str,
    reason: str,
    detail: str,
    bus: str | None = None,
    acquisition: str | None = None,
) -> AcquisitionResult:
    return AcquisitionResult(
        metric=metric,
        available=False,
        unit=unit,
        reason=reason,
        detail=detail,
        bus=bus,
        acquisition=acquisition,
    )
