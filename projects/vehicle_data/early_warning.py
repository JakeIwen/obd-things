"""Transparent, history-relative early-warning evaluation.

The evaluator consumes only saved historian rows.  It cannot read or transmit
CAN traffic.  Every assessment exposes its current evidence, exact operating
regime, robust median/MAD baseline, persistence count, and named corroborators.
It deliberately emits no composite health score and makes no claim to diagnose
a component failure.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

from lib.vehicle_can_roles import CAN_BUS_ROLES, CAN_ROLE_SPECS
from projects.vehicle_data.historian import (
    REGIME_DIMENSIONS,
    BaselineStats,
    TelemetryHistorian,
    project_regime,
)


WARNING_SCHEMA_VERSION = 1
DIRECTIONS = frozenset(("high", "low", "either"))
GENERATOR_DUTY_METRIC = "generator.field_duty"
ROLE_USB_SERIALS = {
    spec.role: spec.usb_serial
    for spec in CAN_ROLE_SPECS
    if spec.role in CAN_BUS_ROLES
}


@dataclass(frozen=True)
class CorroborationRule:
    """An independently measured history-relative companion deviation."""

    metric: str
    direction: str
    mad_multiplier: float = 4.5
    minimum_effect: float = 0.0
    max_age_seconds: float = 10.0
    regime_dimensions: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        _validate_direction(self.direction)
        _validate_positive(self.mad_multiplier, "mad_multiplier")
        _validate_nonnegative(self.minimum_effect, "minimum_effect")
        _validate_positive(self.max_age_seconds, "max_age_seconds")
        if self.regime_dimensions is not None:
            project_regime("engine:motion:rpm:thermal", self.regime_dimensions)


@dataclass(frozen=True)
class WarningRule:
    """One explainable relative-deviation rule.

    ``minimum_effect`` is a minimum change from the learned median, not an
    absolute safe/unsafe limit.  It prevents tiny quantization noise from being
    labeled anomalous when the historical MAD is zero or very small.
    """

    key: str
    title: str
    metric: str
    direction: str
    mad_multiplier: float = 4.5
    minimum_effect: float = 0.0
    minimum_baseline_buckets: int = 30
    minimum_baseline_trips: int = 3
    lookback_days: int = 30
    persistence_observations: int = 5
    persistence_window_seconds: float = 120.0
    max_age_seconds: float = 10.0
    regime_dimensions: tuple[str, ...] = REGIME_DIMENSIONS
    corroborators: tuple[CorroborationRule, ...] = ()
    required_corroborators: int = 0
    maximum_delta_c: float | None = None
    maximum_delta_window_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.key or not self.title or not self.metric:
            raise ValueError("warning key, title, and metric must be nonempty")
        if self.metric == GENERATOR_DUTY_METRIC:
            raise ValueError(
                "generator field duty may corroborate voltage evidence but "
                "cannot be a primary warning signal"
            )
        _validate_direction(self.direction)
        _validate_positive(self.mad_multiplier, "mad_multiplier")
        _validate_nonnegative(self.minimum_effect, "minimum_effect")
        for name, value in (
            ("minimum_baseline_buckets", self.minimum_baseline_buckets),
            ("minimum_baseline_trips", self.minimum_baseline_trips),
            ("lookback_days", self.lookback_days),
            ("persistence_observations", self.persistence_observations),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        _validate_positive(self.persistence_window_seconds, "persistence_window_seconds")
        _validate_positive(self.max_age_seconds, "max_age_seconds")
        project_regime("engine:motion:rpm:thermal", self.regime_dimensions)
        if (
            not isinstance(self.required_corroborators, int)
            or isinstance(self.required_corroborators, bool)
            or not 0 <= self.required_corroborators <= len(self.corroborators)
        ):
            raise ValueError("required_corroborators is outside the corroborator list")
        if (self.maximum_delta_c is None) != (
            self.maximum_delta_window_seconds is None
        ):
            raise ValueError(
                "maximum_delta_c and maximum_delta_window_seconds must be set together"
            )
        if self.maximum_delta_c is not None:
            _validate_positive(self.maximum_delta_c, "maximum_delta_c")
            _validate_positive(
                self.maximum_delta_window_seconds,
                "maximum_delta_window_seconds",
            )


@dataclass(frozen=True)
class AbsoluteOilPressureRule:
    """OEM-context critical oil-pressure advisory with positive RPM gating."""

    key: str = "engine_oil_pressure_absolute_critical"
    title: str = "Engine oil pressure below the operating minimum"
    metric: str = "engine.oil_pressure"
    running_metric: str = "engine.rpm"
    pressure_source: str = "ccan.broadcast.0x41d"
    pressure_quality: str = "observed_alfa_scale"
    pressure_unit: str = "psi"
    running_source: str = "ccan.broadcast.0x0fc"
    running_quality: str = "observed_alfa_scale"
    running_unit: str = "rpm"
    minimum_pressure_psi: float = 12.0
    running_rpm_threshold: float = 400.0
    startup_grace_seconds: float = 10.0
    running_max_gap_seconds: float = 10.0
    max_age_seconds: float = 5.0
    persistence_observations: int = 2
    persistence_window_seconds: float = 10.0
    notification_rate_limit_seconds: float = 5 * 60
    reference_provenance: str = (
        "exact-vehicle OEM P06DD theory: approximately 12 psi minimum "
        "while the engine is operating; qualified passive 0x41D pressure "
        "and 0x0FC RPM decodes"
    )

    def __post_init__(self) -> None:
        if not self.key or not self.title:
            raise ValueError("absolute warning key and title must be nonempty")
        if self.metric != "engine.oil_pressure" or self.running_metric != "engine.rpm":
            raise ValueError("absolute oil rule is fixed to qualified oil pressure and RPM")
        if (
            self.pressure_source != "ccan.broadcast.0x41d"
            or self.pressure_quality != "observed_alfa_scale"
            or self.pressure_unit != "psi"
            or self.running_source != "ccan.broadcast.0x0fc"
            or self.running_quality != "observed_alfa_scale"
            or self.running_unit != "rpm"
        ):
            raise ValueError(
                "absolute oil rule sources, qualities, and units are fixed to "
                "qualified 0x41D pressure and 0x0FC RPM"
            )
        for name, value in (
            ("minimum_pressure_psi", self.minimum_pressure_psi),
            ("running_rpm_threshold", self.running_rpm_threshold),
            ("startup_grace_seconds", self.startup_grace_seconds),
            ("running_max_gap_seconds", self.running_max_gap_seconds),
            ("max_age_seconds", self.max_age_seconds),
            ("persistence_window_seconds", self.persistence_window_seconds),
            ("notification_rate_limit_seconds", self.notification_rate_limit_seconds),
        ):
            _validate_positive(value, name)
        if (
            not isinstance(self.persistence_observations, int)
            or isinstance(self.persistence_observations, bool)
            or self.persistence_observations < 1
        ):
            raise ValueError("persistence_observations must be a positive integer")
        if not self.reference_provenance:
            raise ValueError("absolute oil rule requires reference provenance")


def _validate_direction(direction: str) -> None:
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {', '.join(sorted(DIRECTIONS))}")


def _validate_positive(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError(f"{name} must be finite and positive")


def _validate_nonnegative(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{name} must be finite and nonnegative")


# These are conservative relative-change detectors for signals already in the
# public metric allowlist.  None is an OEM limit.  The exact regime includes
# running state, speed band, RPM band, and coolant band, so the oil-pressure
# comparison is explicitly RPM/coolant conditioned rather than a global value.
DEFAULT_WARNING_RULES: tuple[WarningRule, ...] = (
    WarningRule(
        key="engine_oil_pressure_relative_low",
        title="Oil pressure below its comparable-history band",
        metric="engine.oil_pressure",
        direction="low",
        minimum_effect=3.0,
        persistence_observations=5,
        persistence_window_seconds=30.0,
        regime_dimensions=("engine", "motion", "rpm", "thermal"),
    ),
    WarningRule(
        key="engine_coolant_temperature_relative_high",
        title="Coolant temperature above its comparable-history band",
        metric="engine.coolant_temperature",
        direction="high",
        minimum_effect=5.0,
        persistence_observations=10,
        persistence_window_seconds=60.0,
        regime_dimensions=("engine", "motion", "rpm"),
    ),
    WarningRule(
        key="transmission_oil_temperature_relative_high",
        title="Transmission oil temperature above its comparable-history band",
        metric="transmission.oil_temperature",
        direction="high",
        minimum_effect=5.0,
        persistence_observations=10,
        persistence_window_seconds=60.0,
        regime_dimensions=("engine", "motion", "rpm", "thermal"),
        # Reject a discontinuity before it can enter persistence.  The stored
        # display metric is Fahrenheit; evaluation converts the delta back to
        # Celsius and applies the OEM P0711 >10 °C in <1 second criterion.
        maximum_delta_c=10.0,
        maximum_delta_window_seconds=1.0,
    ),
    WarningRule(
        key="battery_voltage_relative_low",
        title="Battery voltage below its comparable-history band",
        metric="battery.voltage",
        direction="low",
        minimum_effect=0.2,
        persistence_observations=10,
        persistence_window_seconds=60.0,
        max_age_seconds=35.0,
        regime_dimensions=("engine", "motion", "rpm"),
        corroborators=(
            CorroborationRule(
                metric="generator.field_duty",
                direction="high",
                minimum_effect=5.0,
            ),
        ),
        # Generator duty is useful independent evidence when available, but a
        # temporarily unavailable diagnostic read must not erase the primary
        # persistent voltage deviation.  Its status is always exposed.
        required_corroborators=0,
    ),
    WarningRule(
        key="tire_pressure_fl_relative_low",
        title="Front-left tire pressure drifting below comparable history",
        metric="tire.pressure.fl",
        direction="low",
        minimum_effect=1.5,
        persistence_observations=3,
        persistence_window_seconds=180.0,
        max_age_seconds=35.0,
        regime_dimensions=("motion",),
    ),
    WarningRule(
        key="tire_pressure_fr_relative_low",
        title="Front-right tire pressure drifting below comparable history",
        metric="tire.pressure.fr",
        direction="low",
        minimum_effect=1.5,
        persistence_observations=3,
        persistence_window_seconds=180.0,
        max_age_seconds=35.0,
        regime_dimensions=("motion",),
    ),
    WarningRule(
        key="tire_pressure_rl_relative_low",
        title="Rear-left tire pressure drifting below comparable history",
        metric="tire.pressure.rl",
        direction="low",
        minimum_effect=1.5,
        persistence_observations=3,
        persistence_window_seconds=180.0,
        max_age_seconds=35.0,
        regime_dimensions=("motion",),
    ),
    WarningRule(
        key="tire_pressure_rr_relative_low",
        title="Rear-right tire pressure drifting below comparable history",
        metric="tire.pressure.rr",
        direction="low",
        minimum_effect=1.5,
        persistence_observations=3,
        persistence_window_seconds=180.0,
        max_age_seconds=35.0,
        regime_dimensions=("motion",),
    ),
)

DEFAULT_ABSOLUTE_WARNING_RULES: tuple[AbsoluteOilPressureRule, ...] = (
    AbsoluteOilPressureRule(),
)
DEFAULT_EVALUATION_RULES: tuple[WarningRule | AbsoluteOilPressureRule, ...] = (
    *DEFAULT_ABSOLUTE_WARNING_RULES,
    *DEFAULT_WARNING_RULES,
)


def _utc(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        value = datetime.fromisoformat(text)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("evaluation time must be timezone-aware")
    return value.astimezone(timezone.utc)


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
) -> float:
    return max(minimum_effect, mad_multiplier * baseline.robust_sigma)


class EarlyWarningEvaluator:
    """Evaluate explainable warnings against prior, like-for-like history."""

    def __init__(
        self,
        historian: TelemetryHistorian,
        rules: Sequence[WarningRule | AbsoluteOilPressureRule] = (
            DEFAULT_EVALUATION_RULES
        ),
    ):
        self.historian = historian
        self.rules = tuple(rules)
        keys = [rule.key for rule in self.rules]
        if len(keys) != len(set(keys)):
            raise ValueError("warning rule keys must be unique")

    @staticmethod
    def _current_payload(
        sample: dict[str, object], age_seconds: float
    ) -> dict[str, object]:
        return {
            "value": sample["value"],
            "unit": sample["unit"],
            "captured_at": sample["captured_at"],
            "observed_at": sample["observed_at"],
            "effective_age_seconds": age_seconds,
            "freshness": sample["freshness"],
            "source": sample["source"],
            "bus": sample["bus"],
            "quality": sample["quality"],
            "provenance": sample["provenance"],
            "trip_id": sample["trip_id"],
        }

    @staticmethod
    def _base(rule: WarningRule) -> dict[str, object]:
        return {
            "rule": rule.key,
            "title": rule.title,
            "metric": rule.metric,
            "direction": rule.direction,
            "category": "vehicle_health",
            "severity": "warning",
            "advisory": True,
            "notification_eligible": False,
            "notification_rate_limit_seconds": 30 * 60,
            "interpretation": (
                "history-relative persistent deviation; not an OEM limit, "
                "component diagnosis, or substitute for a warning lamp"
            ),
        }

    @staticmethod
    def _absolute_base(rule: AbsoluteOilPressureRule) -> dict[str, object]:
        return {
            "rule": rule.key,
            "title": rule.title,
            "metric": rule.metric,
            "direction": "low",
            "category": "vehicle_health",
            "severity": "critical",
            "advisory": True,
            "notification_eligible": False,
            "notification_rate_limit_seconds": (
                rule.notification_rate_limit_seconds
            ),
            "interpretation": (
                "OEM-context critical advisory; it does not diagnose the "
                "cause or replace the factory oil-pressure warning"
            ),
            "absolute_threshold": {
                "operator": "below",
                "value": rule.minimum_pressure_psi,
                "unit": "psi",
            },
            "reference_provenance": rule.reference_provenance,
        }

    def _plausibility_rejection(
        self,
        rule: WarningRule,
        *,
        sample: dict[str, object],
        at: datetime,
    ) -> dict[str, object] | None:
        delta_limit = rule.maximum_delta_c
        window = rule.maximum_delta_window_seconds
        if delta_limit is None or window is None:
            return None
        dimensions = tuple(
            name for name in rule.regime_dimensions if name != "thermal"
        )
        recent = self.historian.recent_numeric_samples(
            rule.metric,
            regime=str(sample["regime"]),
            trip_id=(
                int(sample["trip_id"])
                if sample.get("trip_id") is not None
                else None
            ),
            at=at,
            limit=2,
            quality=str(sample["quality"]),
            source=str(sample["source"]),
            provenance=str(sample["provenance"]),
            regime_dimensions=dimensions,
        )
        if len(recent) < 2:
            return None
        newest_at = recent[0].get("observed_at")
        previous_at = recent[1].get("observed_at")
        if not isinstance(newest_at, str) or not isinstance(previous_at, str):
            return None
        elapsed = (_utc(newest_at) - _utc(previous_at)).total_seconds()
        if elapsed <= 0:
            return {
                "rejected": True,
                "reason": "temperature observations are not in increasing time order",
                "elapsed_seconds": elapsed,
                "maximum_delta_c": delta_limit,
                "maximum_delta_window_seconds": window,
            }
        delta = float(recent[0]["value"]) - float(recent[1]["value"])
        unit = str(sample["unit"])
        if unit == "°F":
            delta_c = delta * 5.0 / 9.0
        elif unit == "°C":
            delta_c = delta
        else:
            return {
                "rejected": True,
                "reason": "temperature-delta plausibility requires °F or °C units",
                "elapsed_seconds": elapsed,
                "delta": delta,
                "unit": unit,
                "maximum_delta_c": delta_limit,
                "maximum_delta_window_seconds": window,
            }
        rejected = elapsed < window and abs(delta_c) > delta_limit
        return {
            "rejected": rejected,
            "reason": (
                "temperature delta exceeded the OEM-context plausibility criterion"
                if rejected
                else "temperature delta is outside the reject criterion"
            ),
            "previous_value": recent[1]["value"],
            "current_value": recent[0]["value"],
            "unit": unit,
            "elapsed_seconds": elapsed,
            "delta_c": delta_c,
            "maximum_delta_c": delta_limit,
            "maximum_delta_window_seconds": window,
        }

    def _evaluate_absolute_oil_rule(
        self,
        rule: AbsoluteOilPressureRule,
        *,
        at: datetime,
    ) -> dict[str, object]:
        base = self._absolute_base(rule)
        rpm = self.historian.latest_sample(
            rule.running_metric,
            at=at,
            fresh_only=True,
        )
        rpm_age = (
            _sample_age_seconds(rpm, at)
            if isinstance(rpm, dict)
            and isinstance(rpm.get("value"), (int, float))
            else None
        )
        if rpm_age is None or rpm_age > rule.max_age_seconds:
            return {
                **base,
                "state": "unavailable",
                "reason": "fresh positive RPM evidence is unavailable",
                "current": None,
                "running_evidence": None,
                "persistence": {
                    "required": rule.persistence_observations,
                    "observed": 0,
                    "window_seconds": rule.persistence_window_seconds,
                },
            }
        assert rpm is not None
        rpm_payload = self._current_payload(rpm, rpm_age)
        if (
            rpm.get("source") != rule.running_source
            or rpm.get("quality") != rule.running_quality
            or rpm.get("unit") != rule.running_unit
        ):
            return {
                **base,
                "state": "unavailable",
                "reason": (
                    "RPM evidence does not match the exact qualified 0x0FC "
                    "source, quality, and unit"
                ),
                "current": None,
                "running_evidence": {
                    "qualified": False,
                    "rpm": rpm_payload,
                    "expected": {
                        "source": rule.running_source,
                        "quality": rule.running_quality,
                        "unit": rule.running_unit,
                    },
                },
                "persistence": {
                    "required": rule.persistence_observations,
                    "observed": 0,
                    "window_seconds": rule.persistence_window_seconds,
                },
            }
        if float(rpm["value"]) < rule.running_rpm_threshold:
            return {
                **base,
                "state": "suppressed",
                "reason": "engine-running RPM gate is not satisfied",
                "current": None,
                "running_evidence": {
                    "qualified": False,
                    "rpm": rpm_payload,
                    "minimum_rpm": rule.running_rpm_threshold,
                },
                "persistence": {
                    "required": rule.persistence_observations,
                    "observed": 0,
                    "window_seconds": rule.persistence_window_seconds,
                },
            }
        running = self.historian.continuous_numeric_condition(
            rule.running_metric,
            at=at,
            minimum=rule.running_rpm_threshold,
            max_gap_seconds=rule.running_max_gap_seconds,
            source=str(rpm["source"]),
            quality=str(rpm["quality"]),
            provenance=str(rpm["provenance"]),
            trip_id=(int(rpm["trip_id"]) if rpm.get("trip_id") is not None else None),
        )
        if running is None or float(running["duration_seconds"]) < rule.startup_grace_seconds:
            return {
                **base,
                "state": "suppressed",
                "reason": "engine is inside the defined startup/cranking grace period",
                "current": None,
                "running_evidence": {
                    "qualified": False,
                    "rpm": rpm_payload,
                    "continuous_interval": running,
                    "startup_grace_seconds": rule.startup_grace_seconds,
                },
                "persistence": {
                    "required": rule.persistence_observations,
                    "observed": 0,
                    "window_seconds": rule.persistence_window_seconds,
                },
            }
        oil = self.historian.latest_sample(rule.metric, at=at, fresh_only=True)
        oil_age = (
            _sample_age_seconds(oil, at)
            if isinstance(oil, dict)
            and isinstance(oil.get("value"), (int, float))
            else None
        )
        running_evidence = {
            "qualified": True,
            "rpm": rpm_payload,
            "continuous_interval": running,
            "startup_grace_seconds": rule.startup_grace_seconds,
        }
        if oil_age is None or oil_age > rule.max_age_seconds or oil is None:
            return {
                **base,
                "state": "unavailable",
                "reason": "fresh oil-pressure evidence is unavailable",
                "current": None,
                "running_evidence": running_evidence,
                "persistence": {
                    "required": rule.persistence_observations,
                    "observed": 0,
                    "window_seconds": rule.persistence_window_seconds,
                },
            }
        current = self._current_payload(oil, oil_age)
        if (
            oil.get("source") != rule.pressure_source
            or oil.get("quality") != rule.pressure_quality
            or oil.get("unit") != rule.pressure_unit
        ):
            return {
                **base,
                "state": "unavailable",
                "reason": (
                    "oil-pressure evidence does not match the exact qualified "
                    "0x41D source, quality, and psi unit"
                ),
                "current": current,
                "running_evidence": running_evidence,
                "persistence": {
                    "required": rule.persistence_observations,
                    "observed": 0,
                    "window_seconds": rule.persistence_window_seconds,
                },
            }
        below = float(oil["value"]) < rule.minimum_pressure_psi
        recent = self.historian.recent_numeric_samples(
            rule.metric,
            regime=str(oil["regime"]),
            trip_id=(int(oil["trip_id"]) if oil.get("trip_id") is not None else None),
            at=at,
            limit=rule.persistence_observations,
            quality=str(oil["quality"]),
            source=str(oil["source"]),
            provenance=str(oil["provenance"]),
            regime_dimensions=("engine",),
        )
        observed = 0
        newest_at = _utc(str(oil["observed_at"]))
        running_start = _utc(str(running["started_at"]))
        if below:
            for point in recent:
                observed_text = point.get("observed_at")
                if not isinstance(observed_text, str):
                    continue
                point_at = _utc(observed_text)
                if (
                    point_at - running_start
                ).total_seconds() < rule.startup_grace_seconds:
                    break
                elapsed = (newest_at - point_at).total_seconds()
                if elapsed < 0:
                    continue
                if elapsed > rule.persistence_window_seconds:
                    break
                if float(point["value"]) >= rule.minimum_pressure_psi:
                    break
                observed += 1
        persistent = observed >= rule.persistence_observations
        if not below:
            state = "normal"
            reason = "oil pressure is not below the OEM-context operating minimum"
        elif not persistent:
            state = "watch"
            reason = "critical deviation awaits a second independent observation"
        else:
            state = "warning"
            reason = (
                "persistent oil pressure below the approximately 12 psi "
                "OEM operating minimum"
            )
        return {
            **base,
            "state": state,
            "reason": reason,
            "notification_eligible": state == "warning",
            "current": current,
            "regime": oil["regime"],
            "running_evidence": running_evidence,
            "persistence": {
                "required": rule.persistence_observations,
                "observed": observed,
                "window_seconds": rule.persistence_window_seconds,
                "satisfied": persistent,
            },
        }

    def _baseline_for(
        self,
        *,
        metric: str,
        sample: dict[str, object],
        at: datetime,
        lookback_days: int,
        regime_dimensions: Sequence[str],
    ) -> BaselineStats | None:
        return self.historian.robust_baseline(
            metric,
            str(sample["regime"]),
            before=at,
            lookback_days=lookback_days,
            exclude_trip_id=(
                int(sample["trip_id"]) if sample.get("trip_id") is not None else None
            ),
            unit=str(sample["unit"]),
            quality=str(sample["quality"]),
            source=str(sample["source"]),
            provenance=str(sample["provenance"]),
            regime_dimensions=regime_dimensions,
        )

    @staticmethod
    def _baseline_shortfall(
        baseline: BaselineStats | None,
        *,
        buckets: int,
        trips: int,
    ) -> str | None:
        if baseline is None:
            return "no completed comparable rollup buckets"
        reasons = []
        if baseline.bucket_count < buckets:
            reasons.append(f"{baseline.bucket_count}/{buckets} comparable buckets")
        if baseline.trip_count < trips:
            reasons.append(f"{baseline.trip_count}/{trips} prior trips")
        return "; ".join(reasons) if reasons else None

    def _corroborate(
        self,
        definition: CorroborationRule,
        *,
        primary_regime: str,
        at: datetime,
        lookback_days: int,
        minimum_baseline_buckets: int,
        minimum_baseline_trips: int,
        primary_regime_dimensions: Sequence[str],
    ) -> dict[str, object]:
        sample = self.historian.latest_sample(definition.metric, at=at, fresh_only=True)
        if sample is None or not isinstance(sample.get("value"), (int, float)):
            return {
                "metric": definition.metric,
                "state": "unavailable",
                "reason": "no fresh numeric observation",
            }
        age = _sample_age_seconds(sample, at)
        if age is None or age > definition.max_age_seconds:
            return {
                "metric": definition.metric,
                "state": "unavailable",
                "reason": "latest observation is undated or too old",
                "effective_age_seconds": age,
            }
        dimensions = definition.regime_dimensions or tuple(primary_regime_dimensions)
        if project_regime(str(sample.get("regime")), dimensions) != project_regime(
            primary_regime, dimensions
        ):
            return {
                "metric": definition.metric,
                "state": "unavailable",
                "reason": "latest observation is from a different operating regime",
                "regime": sample.get("regime"),
            }
        baseline = self._baseline_for(
            metric=definition.metric,
            sample=sample,
            at=at,
            lookback_days=lookback_days,
            regime_dimensions=dimensions,
        )
        shortfall = self._baseline_shortfall(
            baseline,
            buckets=minimum_baseline_buckets,
            trips=minimum_baseline_trips,
        )
        if shortfall is not None:
            return {
                "metric": definition.metric,
                "state": "insufficient_history",
                "reason": shortfall,
                "baseline": baseline.as_dict() if baseline is not None else None,
            }
        assert baseline is not None
        effect, signed = _effect(
            float(sample["value"]), baseline.median, definition.direction
        )
        threshold = _threshold(
            baseline,
            mad_multiplier=definition.mad_multiplier,
            minimum_effect=definition.minimum_effect,
        )
        anomalous = effect >= threshold
        return {
            "metric": definition.metric,
            "state": "corroborating" if anomalous else "not_corroborating",
            "direction": definition.direction,
            "current": self._current_payload(sample, age),
            "baseline": baseline.as_dict(),
            "deviation": {
                "signed_from_median": signed,
                "effect_in_rule_direction": effect,
                "threshold": threshold,
                "mad_multiplier": definition.mad_multiplier,
                "minimum_effect": definition.minimum_effect,
            },
        }

    def evaluate_rule(
        self,
        rule: WarningRule | AbsoluteOilPressureRule,
        *,
        at: datetime | str | None = None,
        _refresh_rollups: bool = True,
    ) -> dict[str, object]:
        evaluated = _utc(at)
        if isinstance(rule, AbsoluteOilPressureRule):
            return self._evaluate_absolute_oil_rule(rule, at=evaluated)
        base = self._base(rule)
        sample = self.historian.latest_sample(rule.metric, at=evaluated, fresh_only=True)
        if sample is None or not isinstance(sample.get("value"), (int, float)):
            return {
                **base,
                "state": "unavailable",
                "reason": "no fresh numeric observation is stored",
                "current": None,
                "regime": None,
                "baseline_regime": None,
                "regime_dimensions": list(rule.regime_dimensions),
                "baseline": None,
                "deviation": None,
                "persistence": {
                    "required": rule.persistence_observations,
                    "observed": 0,
                    "window_seconds": rule.persistence_window_seconds,
                },
                "corroboration": {
                    "required": rule.required_corroborators,
                    "observed": 0,
                    "satisfied": False,
                },
                "corroborators": [],
            }
        age = _sample_age_seconds(sample, evaluated)
        if age is None or age > rule.max_age_seconds:
            return {
                **base,
                "state": "unavailable",
                "reason": "latest observation is undated or older than the rule permits",
                "current": (
                    None if age is None else self._current_payload(sample, age)
                ),
                "regime": sample.get("regime"),
                "baseline_regime": None,
                "regime_dimensions": list(rule.regime_dimensions),
                "baseline": None,
                "deviation": None,
                "persistence": {
                    "required": rule.persistence_observations,
                    "observed": 0,
                    "window_seconds": rule.persistence_window_seconds,
                },
                "corroboration": {
                    "required": rule.required_corroborators,
                    "observed": 0,
                    "satisfied": False,
                },
                "corroborators": [],
            }

        plausibility = self._plausibility_rejection(
            rule,
            sample=sample,
            at=evaluated,
        )
        if plausibility is not None and plausibility["rejected"]:
            return {
                **base,
                "state": "rejected",
                "reason": str(plausibility["reason"]),
                "current": self._current_payload(sample, age),
                "regime": sample.get("regime"),
                "baseline_regime": None,
                "regime_dimensions": list(rule.regime_dimensions),
                "baseline": None,
                "deviation": None,
                "plausibility": plausibility,
                "persistence": {
                    "required": rule.persistence_observations,
                    "observed": 0,
                    "window_seconds": rule.persistence_window_seconds,
                },
                "corroboration": {
                    "required": rule.required_corroborators,
                    "observed": 0,
                    "satisfied": False,
                },
                "corroborators": [],
            }

        # Only completed buckets before the current time can train a baseline.
        # The current trip is filtered again by robust_baseline.
        if _refresh_rollups:
            self.historian.refresh_rollups(through=evaluated)
        baseline = self._baseline_for(
            metric=rule.metric,
            sample=sample,
            at=evaluated,
            lookback_days=rule.lookback_days,
            regime_dimensions=rule.regime_dimensions,
        )
        shortfall = self._baseline_shortfall(
            baseline,
            buckets=rule.minimum_baseline_buckets,
            trips=rule.minimum_baseline_trips,
        )
        if shortfall is not None:
            return {
                **base,
                "state": "insufficient_history",
                "reason": shortfall,
                "current": self._current_payload(sample, age),
                "regime": sample["regime"],
                "baseline_regime": baseline.regime if baseline is not None else None,
                "regime_dimensions": list(rule.regime_dimensions),
                "baseline": baseline.as_dict() if baseline is not None else None,
                "deviation": None,
                "persistence": {
                    "required": rule.persistence_observations,
                    "observed": 0,
                    "window_seconds": rule.persistence_window_seconds,
                },
                "corroboration": {
                    "required": rule.required_corroborators,
                    "observed": 0,
                    "satisfied": False,
                },
                "corroborators": [],
            }
        assert baseline is not None
        threshold = _threshold(
            baseline,
            mad_multiplier=rule.mad_multiplier,
            minimum_effect=rule.minimum_effect,
        )
        current_effect, current_signed = _effect(
            float(sample["value"]), baseline.median, rule.direction
        )
        primary_anomalous = current_effect >= threshold

        recent = self.historian.recent_numeric_samples(
            rule.metric,
            regime=str(sample["regime"]),
            trip_id=(int(sample["trip_id"]) if sample.get("trip_id") is not None else None),
            at=evaluated,
            limit=rule.persistence_observations,
            quality=str(sample["quality"]),
            source=str(sample["source"]),
            provenance=str(sample["provenance"]),
            regime_dimensions=rule.regime_dimensions,
        )
        observed = 0
        newest_observed = sample.get("observed_at")
        newest_at = (
            _utc(newest_observed) if isinstance(newest_observed, str) else None
        )
        if primary_anomalous and newest_at is not None:
            for point in recent:
                point_observed = point.get("observed_at")
                if not isinstance(point_observed, str):
                    continue
                point_at = _utc(point_observed)
                elapsed = (newest_at - point_at).total_seconds()
                if elapsed < 0:
                    continue
                if elapsed > rule.persistence_window_seconds:
                    break
                effect, _signed = _effect(
                    float(point["value"]), baseline.median, rule.direction
                )
                if effect < threshold:
                    break
                observed += 1
        persistent = observed >= rule.persistence_observations

        corroborators = [
            self._corroborate(
                definition,
                primary_regime=str(sample["regime"]),
                at=evaluated,
                lookback_days=rule.lookback_days,
                minimum_baseline_buckets=rule.minimum_baseline_buckets,
                minimum_baseline_trips=rule.minimum_baseline_trips,
                primary_regime_dimensions=rule.regime_dimensions,
            )
            for definition in rule.corroborators
        ]
        corroborating_count = sum(
            item["state"] == "corroborating" for item in corroborators
        )
        corroboration_met = corroborating_count >= rule.required_corroborators
        if not primary_anomalous:
            state = "normal"
            reason = "current value is inside the learned relative-deviation band"
        elif not persistent:
            state = "watch"
            reason = "deviation is present but has not met the persistence requirement"
        elif not corroboration_met:
            state = "watch"
            reason = "persistent deviation lacks the rule's required corroboration"
        else:
            state = "warning"
            reason = "persistent history-relative deviation"
            if corroborating_count:
                reason += f" with {corroborating_count} corroborating metric(s)"
        return {
            **base,
            "state": state,
            "reason": reason,
            "notification_eligible": state == "warning",
            "current": self._current_payload(sample, age),
            "regime": sample["regime"],
            "baseline_regime": baseline.regime,
            "regime_dimensions": list(rule.regime_dimensions),
            "baseline": baseline.as_dict(),
            "deviation": {
                "signed_from_median": current_signed,
                "effect_in_rule_direction": current_effect,
                "threshold": threshold,
                "mad_multiplier": rule.mad_multiplier,
                "minimum_effect": rule.minimum_effect,
            },
            "persistence": {
                "required": rule.persistence_observations,
                "observed": observed,
                "window_seconds": rule.persistence_window_seconds,
                "satisfied": persistent,
            },
            "corroboration": {
                "required": rule.required_corroborators,
                "observed": corroborating_count,
                "satisfied": corroboration_met,
            },
            "corroborators": corroborators,
            "plausibility": plausibility,
        }

    def evaluate(
        self,
        *,
        at: datetime | str | None = None,
    ) -> dict[str, object]:
        """Return a compact assessment list and active watch/warning subset."""

        evaluated = _utc(at)
        self.historian.refresh_rollups(through=evaluated)
        assessments = [
            self.evaluate_rule(rule, at=evaluated, _refresh_rollups=False)
            for rule in self.rules
        ]
        active = [
            assessment
            for assessment in assessments
            if assessment["state"] in ("watch", "warning")
        ]
        return {
            "schema_version": WARNING_SCHEMA_VERSION,
            "generated_at": evaluated.isoformat(),
            "method": {
                "center": "median of completed comparable bucket medians",
                "spread": "1.4826 × median absolute deviation",
                "conditioning": (
                    "exact engine/speed/RPM/coolant regime plus exact unit, source, "
                    "quality, and provenance"
                ),
                "opaque_health_score": False,
            },
            "active": active,
            "assessments": assessments,
        }


class InfrastructureHealthEvaluator:
    """Translate persisted USB/interface facts into advisory assessments.

    This evaluator never resets, rebinds, or reconfigures hardware.  A missing
    or unresolved role must persist for two historian samples before becoming
    notification-eligible; an exact kernel USB removal edge, ambiguity, an
    unhealthy controller, a topology generation change, or a restoration
    inhibit is immediately eligible.
    """

    def __init__(self, historian: TelemetryHistorian):
        self.historian = historian

    @staticmethod
    def _base(
        *,
        rule: str,
        title: str,
        severity: str = "warning",
        rate_limit_seconds: float = 30 * 60,
    ) -> dict[str, object]:
        return {
            "rule": rule,
            "title": title,
            "metric": None,
            "category": "can_infrastructure",
            "severity": severity,
            "advisory": True,
            "notification_eligible": False,
            "notification_rate_limit_seconds": rate_limit_seconds,
            "interpretation": (
                "host/interface evidence only; no hardware reset, USB power "
                "cycle, CAN reconfiguration, or component diagnosis is implied"
            ),
        }

    @staticmethod
    def _inhibit_name(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            for key in ("name", "reason", "kind"):
                candidate = value.get(key)
                if isinstance(candidate, str):
                    return candidate
        return ""

    def evaluate(
        self,
        snapshot_id: int,
        *,
        at: datetime | str | None = None,
    ) -> dict[str, object]:
        evaluated = _utc(at)
        context = self.historian.system_health_context(snapshot_id)
        roles = context.get("roles")
        roles = roles if isinstance(roles, Mapping) else {}
        gaps = context.get("active_interface_gaps")
        gaps = gaps if isinstance(gaps, Mapping) else {}
        global_gap = gaps.get("interface-status")
        global_gap = global_gap if isinstance(global_gap, Mapping) else {}
        try:
            usb_context = self.historian.usb_can_health_context(snapshot_id)
        except (AttributeError, KeyError, RuntimeError, ValueError):
            usb_context = {"available": False}
        removals = usb_context.get("new_removal_events")
        removals = removals if isinstance(removals, list) else []
        active_usb = usb_context.get("active_incidents")
        active_usb = active_usb if isinstance(active_usb, list) else []
        usb_history_available = usb_context.get("available") is True
        transient_detected = bool(removals or active_usb)
        affected_serials = sorted(
            {
                serial
                for item in [*removals, *active_usb]
                if isinstance(item, Mapping)
                for serial in item.get("affected_serials", [])
                if isinstance(serial, str)
            }
        )
        if removals:
            usb_reason = (
                f"{len(removals)} new receive-only kernel removal edge(s) "
                "were durably recorded"
            )
        elif active_usb:
            usb_reason = (
                f"{len(active_usb)} USB CAN incident(s) remain active pending "
                "healthy exact-role re-resolution"
            )
        elif usb_history_available:
            usb_reason = "no new or unresolved USB CAN removal incident is present"
        else:
            usb_reason = "kernel USB CAN incident history is not available for this snapshot"
        if transient_detected:
            usb_state = "warning"
        elif usb_history_available:
            usb_state = "normal"
        else:
            # Lost incident history is not affirmative recovery evidence.  The
            # advisory lifecycle treats unavailable as inconclusive and keeps
            # any already-open episode intact without notification eligibility.
            usb_state = "unavailable"
        assessments: list[dict[str, object]] = []
        for role in CAN_BUS_ROLES:
            normalized_rule = role.replace("-", "_")
            base = self._base(
                rule=f"can_interface_role_{normalized_rule}",
                title=f"{role} interface role is unhealthy",
            )
            current = roles.get(role)
            current = current if isinstance(current, Mapping) else None
            role_gap = (
                current.get("active_gap")
                if isinstance(current, Mapping)
                else gaps.get(role)
            )
            role_gap = role_gap if isinstance(role_gap, Mapping) else global_gap
            count = role_gap.get("observation_count", 0)
            count = count if isinstance(count, int) and not isinstance(count, bool) else 0
            if current is None:
                state = "warning" if count >= 2 else "watch"
                reason = "logical role is absent from current interface status"
                current_payload: dict[str, object] = {
                    "role": role,
                    "health": "missing",
                    "reason": role_gap.get("reason", "interface_role_absent"),
                    "gap_observation_count": count,
                }
            else:
                current_payload = dict(current)
                health = current.get("health")
                resolution = current.get("resolution")
                cause = current.get("reason")
                if health == "healthy":
                    state = "normal"
                    reason = "role is resolved and its controller is healthy"
                elif health == "unknown":
                    state = "watch"
                    reason = "role health evidence is incomplete"
                else:
                    immediate = (
                        resolution == "ambiguous"
                        or cause == "controller_unhealthy"
                    )
                    state = "warning" if immediate or count >= 2 else "watch"
                    if resolution in ("missing", "ambiguous"):
                        reason = f"logical USB role resolution is {resolution}"
                    elif cause == "controller_unhealthy":
                        reason = "SocketCAN controller is not ERROR-ACTIVE"
                    else:
                        reason = f"interface health check failed: {cause}"
                current_payload["gap_observation_count"] = count
            role_serial = (
                current.get("usb_serial")
                if isinstance(current, Mapping)
                and isinstance(current.get("usb_serial"), str)
                else ROLE_USB_SERIALS.get(role)
            )
            usb_covers_role = bool(
                transient_detected
                and role_serial in affected_serials
            )
            assessments.append(
                {
                    **base,
                    "state": state,
                    "reason": reason,
                    "notification_eligible": (
                        state == "warning" and not usb_covers_role
                    ),
                    "notification_suppressed_by": (
                        "usb_can_transient_disconnect"
                        if state == "warning" and usb_covers_role
                        else None
                    ),
                    "current": current_payload,
                    "persistence": {
                        "required": 1 if (
                            current is not None
                            and (
                                current.get("resolution") == "ambiguous"
                                or current.get("reason") == "controller_unhealthy"
                            )
                        ) else 2,
                        "observed": count,
                    },
                }
            )

        topology_changed = context.get("topology_changed") is True
        assessments.append(
            {
                **self._base(
                    rule="usb_can_topology_generation_changed",
                    title="USB CAN topology generation changed",
                    rate_limit_seconds=10 * 60,
                ),
                "state": "warning" if topology_changed else "normal",
                "reason": (
                    "serial/dev_id to netdev topology changed since the prior sample"
                    if topology_changed
                    else "USB CAN topology generation is stable or establishing its baseline"
                ),
                "notification_eligible": (
                    topology_changed and not transient_detected
                ),
                "notification_suppressed_by": (
                    "usb_can_transient_disconnect"
                    if topology_changed and transient_detected
                    else None
                ),
                "current": {
                    "topology_generation": context.get("topology_generation"),
                    "previous_topology_generation": context.get(
                        "previous_topology_generation"
                    ),
                    "issues": context.get("issues"),
                },
            }
        )

        assessments.append(
            {
                **self._base(
                    rule="usb_can_transient_disconnect",
                    title="USB CAN branch transiently disconnected",
                    rate_limit_seconds=10 * 60,
                ),
                "state": usb_state,
                "reason": usb_reason,
                "notification_eligible": usb_state == "warning",
                "current": {
                    "new_removal_event_count": len(removals),
                    "active_incident_count": len(active_usb),
                    "affected_serials": affected_serials,
                    "event_ids": [
                        item.get("event_id")
                        for item in removals
                        if isinstance(item, Mapping)
                        and isinstance(item.get("event_id"), str)
                    ],
                    "incident_ids": [
                        item.get("incident_id")
                        for item in active_usb
                        if isinstance(item, Mapping)
                        and isinstance(item.get("incident_id"), str)
                    ],
                    "dropped_event_count": usb_context.get(
                        "dropped_event_count", 0
                    ),
                    "removal_event_count_24h": usb_context.get(
                        "removal_event_count_24h", 0
                    ),
                    "source": usb_context.get("source"),
                },
                "persistence": {
                    "required": 1,
                    "observed": len(removals) or len(active_usb),
                },
            }
        )

        inhibits = context.get("active_inhibits")
        inhibits = inhibits if isinstance(inhibits, list) else []
        restoration_inhibits = [
            value
            for value in inhibits
            if "restoration" in self._inhibit_name(value).lower()
        ]
        restoration_failed = context.get("restoration_failed") is True
        inhibited = restoration_failed or bool(restoration_inhibits)
        assessments.append(
            {
                **self._base(
                    rule="can_restoration_inhibit",
                    title="CAN restoration inhibit is active",
                    severity="critical",
                    rate_limit_seconds=10 * 60,
                ),
                "state": "warning" if inhibited else "normal",
                "reason": (
                    "active-drive restoration failed or a restoration inhibit is latched"
                    if inhibited
                    else "no restoration failure or restoration inhibit is present"
                ),
                "notification_eligible": inhibited,
                "current": {
                    "restoration_failed": restoration_failed,
                    "active_inhibits": restoration_inhibits,
                },
            }
        )
        return {
            "schema_version": WARNING_SCHEMA_VERSION,
            "generated_at": evaluated.isoformat(),
            "method": {
                "source": "persisted serial-role/interface snapshots",
                "automatic_hardware_reset": False,
                "opaque_health_score": False,
            },
            "active": [
                item
                for item in assessments
                if item["state"] in ("watch", "warning")
            ],
            "assessments": assessments,
        }


def default_rule_catalog() -> list[dict[str, object]]:
    """Serializable rule metadata for API/UI discovery."""

    return [asdict(rule) for rule in DEFAULT_EVALUATION_RULES]
