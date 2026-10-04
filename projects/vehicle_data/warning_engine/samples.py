"""Shared saved-sample helpers and the per-evaluation tick cache."""
from __future__ import annotations

import functools
import math
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

from projects.vehicle_data.historian import BaselineStats, project_regime

from .rules import (
    AbsoluteOilPressureRule,
    AbsoluteRule,
    PAIR_COMPANION_MAX_AGE_SECONDS,
    PAIR_ROLLING_TRUST_SECONDS,
    PAIR_SPEED_MAX_AGE_SECONDS,
    RPM_MAX_AGE_SECONDS,
    TireGroupRule,
    WHEEL_LABELS,
    WarningRule,
)


@functools.lru_cache(maxsize=4096)
def _project(regime: str, dimensions: tuple[str, ...]) -> str:
    """Memoized ``project_regime`` (pure; stored regimes repeat constantly)."""

    return project_regime(regime, dimensions)


def _utc(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        value = datetime.fromisoformat(text)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("evaluation time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _to_us(value: datetime) -> int:
    return int(round(value.timestamp() * 1_000_000))


def _observed_us(sample: Mapping[str, object]) -> int | None:
    value = sample.get("observed_us")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = sample.get("observed_at")
    if not isinstance(text, str):
        return None
    return _to_us(_utc(text))


def _captured_us(sample: Mapping[str, object]) -> int | None:
    value = sample.get("captured_us")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = sample.get("captured_at")
    if not isinstance(text, str):
        return None
    return _to_us(_utc(text))


def _engine(sample: Mapping[str, object]) -> str:
    return str(sample.get("regime") or "").split(":", 1)[0]


def _motion(sample: Mapping[str, object]) -> str:
    parts = str(sample.get("regime") or "").split(":")
    return parts[1] if len(parts) > 1 else ""


def _sample_age_seconds(sample: dict[str, object], at: datetime) -> float | None:
    captured_text = sample.get("captured_at")
    source_age = sample.get("age_ms")
    if not isinstance(captured_text, str):
        return None
    if (
        not isinstance(source_age, (int, float))
        or isinstance(source_age, bool)
        or not math.isfinite(float(source_age))
        or source_age < 0
    ):
        return None
    captured = _utc(captured_text)
    elapsed = max(0.0, (at - captured).total_seconds())
    return float(source_age) / 1000 + elapsed


def _effect(value: float, center: float, direction: str) -> tuple[float, float]:
    signed = value - center
    if direction == "high":
        return signed, signed
    if direction == "low":
        return -signed, signed
    return abs(signed), signed


def _threshold(
    baseline: BaselineStats,
    *,
    mad_multiplier: float,
    minimum_effect: float,
    sigma_floor: float = 0.0,
) -> float:
    return max(minimum_effect, mad_multiplier * max(baseline.robust_sigma, sigma_floor))


def _past(value: float, threshold: float, direction: str) -> bool:
    return value < threshold if direction == "low" else value >= threshold


def _cleared(value: float, threshold: float, margin: float, direction: str) -> bool:
    return value >= threshold + margin if direction == "low" else value <= threshold - margin


def _slope(points: Sequence[tuple[float, float]]) -> float | None:
    """Least-squares slope of (seconds, value) points."""

    if len(points) < 2:
        return None
    mean_t = sum(t for t, _ in points) / len(points)
    mean_v = sum(v for _, v in points) / len(points)
    var = sum((t - mean_t) ** 2 for t, _ in points)
    if var <= 0:
        return None
    return sum((t - mean_t) * (v - mean_v) for t, v in points) / var


def _labels(wheels: Sequence[str]) -> str:
    names = [WHEEL_LABELS.get(wheel, wheel.upper()) for wheel in wheels]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _companion_at(
    values: Sequence[tuple[int, float]],
    at_us: int,
    max_age_seconds: float,
) -> float | None:
    """Newest companion observation at or before ``at_us`` within its max age."""

    for observed_us, value in values:
        if observed_us <= at_us:
            if at_us - observed_us <= max_age_seconds * 1_000_000:
                return value
            return None
    return None


def _band_floor(rpm: float, bands: Sequence[tuple]) -> float | None:
    for low, high, floor in bands:
        if rpm >= float(low) and (high is None or rpm <= float(high)):
            return float(floor)
    return None


def _relative_runs(
    points: Sequence[Mapping[str, object]],
    *,
    newest_us: int,
    center: float,
    direction: str,
    threshold: float,
    fraction: float,
    window_seconds: float,
    minimum_value: float | None = None,
) -> dict[str, object]:
    """Escalation run (inside the window) and de-escalation run, newest first.

    With ``minimum_value`` a point also has to be at or above that value to
    count toward escalation (the coolant slow-motion floor); the clear run is
    unaffected.
    """

    escalate = 0
    clear = 0
    counted: list[Mapping[str, object]] = []
    escalating = True
    clearing = True
    clear_broken = False
    window_us = window_seconds * 1_000_000
    for point in points:
        point_us = _observed_us(point)
        if point_us is None:
            continue
        elapsed = newest_us - point_us
        if elapsed < 0:
            continue
        effect, _signed = _effect(float(point["value"]), center, direction)
        if escalating:
            if (
                elapsed <= window_us
                and effect >= threshold
                and (minimum_value is None or float(point["value"]) >= minimum_value)
            ):
                escalate += 1
                counted.append(point)
            else:
                escalating = False
        if clearing:
            if effect <= fraction * threshold:
                clear += 1
            else:
                clearing = False
                clear_broken = True
        if not escalating and not clearing:
            break
    return {
        "escalate": escalate,
        "clear": clear,
        "clear_broken": clear_broken,
        "counted": counted,
    }


def _absolute_runs(
    points: Sequence[Mapping[str, object]],
    *,
    newest_us: int,
    accept: Callable[[Mapping[str, object]], tuple],
    direction: str,
    window_seconds: float,
    clear_margin: float,
) -> dict[str, object]:
    """Consecutive qualifying, critical and clear samples, newest first.

    ``accept(sample)`` returns ``(status, warning, critical, value)`` where
    status is ``ok`` (gate met), ``fail`` (gate not met), ``gap`` (a required
    companion observation is missing) or ``skip`` (sample outside every
    band; neither counts nor breaks a run).  An optional fifth element is
    the threshold the clear run compares against (default: ``warning``).
    """

    escalate = 0
    critical = 0
    clear = 0
    gaps = 0
    counted: list[Mapping[str, object]] = []
    escalating = True
    critical_running = True
    clearing = True
    clear_broken = False
    clear_contradicted = False
    window_us = window_seconds * 1_000_000
    for point in points:
        point_us = _observed_us(point)
        if point_us is None:
            continue
        elapsed = newest_us - point_us
        if elapsed < 0:
            continue
        in_window = elapsed <= window_us
        if not (escalating or critical_running or clearing) and not in_window:
            break
        outcome = accept(point)
        status, warning, critical_limit, value = outcome[:4]
        # An optional fifth element is a separate clear threshold.
        clear_limit = outcome[4] if len(outcome) > 4 else warning
        if status == "skip":
            continue
        if in_window and status == "gap":
            gaps += 1
        ok = status == "ok"
        if escalating:
            if in_window and ok and warning is not None and _past(value, warning, direction):
                escalate += 1
                counted.append(point)
            else:
                escalating = False
        if critical_running:
            if (
                in_window
                and ok
                and critical_limit is not None
                and _past(value, critical_limit, direction)
            ):
                critical += 1
            else:
                critical_running = False
        if clearing:
            if (
                status not in ("fail", "gap")
                and clear_limit is not None
                and _cleared(value, clear_limit, clear_margin, direction)
            ):
                clear += 1
            else:
                clearing = False
                clear_broken = True
                # A gate-unmet sample (or a missing companion reading, whose
                # placeholder value says nothing) ends the evaluable run; only
                # an evaluable reading that is not clear contradicts recovery.
                clear_contradicted = status not in ("fail", "gap")
    return {
        "escalate": escalate,
        "critical": critical,
        "clear": clear,
        "clear_broken": clear_broken,
        "clear_contradicted": clear_contradicted,
        "companion_gaps": gaps,
        "counted": counted,
    }


def _observation_rows(points: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [
        {
            key: point.get(key)
            for key in (
                "sample_id",
                "observed_at",
                "captured_at",
                "value",
                "source",
                "quality",
                "regime",
                "trip_id",
            )
        }
        for point in points
    ]


class _Tick:
    """Per-``evaluate()`` shared reads; thread-local, never shared."""

    __slots__ = (
        "at",
        "at_us",
        "latest",
        "series",
        "conditions",
        "open_states",
        "phase_index",
        "last_trip",
    )

    def __init__(self, at: datetime) -> None:
        self.at = at
        self.at_us = _to_us(at)
        self.latest: dict[str, dict[str, object] | None] = {}
        self.series: dict[tuple, tuple[float, list[dict[str, object]]]] = {}
        self.conditions: dict[tuple, object] = {}
        self.open_states: dict[str, dict[str, object]] | None = None
        self.phase_index: dict[int, dict[str, object]] | None = None
        self.last_trip: object = ...


def _rule_lookbacks(rule: object) -> dict[str, float]:
    """Seconds of raw history each metric needs for one rule's evaluation."""

    needs: dict[str, float] = {}

    def add(metric: str, seconds: float) -> None:
        needs[metric] = max(needs.get(metric, 0.0), float(seconds))

    def span(window: float, count: int) -> float:
        return max(float(window), 6.0 * count) + 30.0

    if isinstance(rule, TireGroupRule):
        for wheel in rule.wheels:
            add(f"{rule.metric}.{wheel}", span(rule.persistence_window_seconds, rule.persistence_observations))
    elif isinstance(rule, AbsoluteOilPressureRule):
        add(rule.metric, span(rule.persistence_window_seconds, rule.persistence_observations))
    elif isinstance(rule, AbsoluteRule):
        base = span(rule.persistence_window_seconds, rule.persistence_observations)
        if rule.gate == "tire_cold_phase":
            for wheel, _warning, _critical in rule.wheel_thresholds:
                add(f"{rule.metric}.{wheel}", base)
        elif rule.gate == "tire_pair":
            # The oldest reading in the run needs its moving segment's start
            # (up to PAIR_ROLLING_TRUST_SECONDS earlier) and each wheel's value
            # at that start.
            for pair in rule.gate_params:
                for wheel in pair:
                    add(
                        f"{rule.metric}.{wheel}",
                        base + PAIR_COMPANION_MAX_AGE_SECONDS + PAIR_ROLLING_TRUST_SECONDS,
                    )
            add("vehicle.speed", base + PAIR_ROLLING_TRUST_SECONDS + PAIR_SPEED_MAX_AGE_SECONDS)
        else:
            add(rule.metric, base)
        if rule.bands or rule.gate == "running_rpm_min":
            add("engine.rpm", base + RPM_MAX_AGE_SECONDS)
        if rule.companion is not None:
            add(str(rule.companion[0]), base + float(rule.companion[2]))
    elif isinstance(rule, WarningRule):
        base = span(rule.persistence_window_seconds, rule.persistence_observations)
        if rule.rate_of_rise is not None:
            base = max(base, float(rule.rate_of_rise[2]) + 30.0)
        add(rule.metric, base)
    return needs
