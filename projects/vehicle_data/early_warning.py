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
from typing import Sequence

from projects.vehicle_data.historian import (
    REGIME_DIMENSIONS,
    BaselineStats,
    TelemetryHistorian,
    project_regime,
)


WARNING_SCHEMA_VERSION = 1
DIRECTIONS = frozenset(("high", "low", "either"))


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

    def __post_init__(self) -> None:
        if not self.key or not self.title or not self.metric:
            raise ValueError("warning key, title, and metric must be nonempty")
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
        rules: Sequence[WarningRule] = DEFAULT_WARNING_RULES,
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
            "advisory": True,
            "interpretation": (
                "history-relative persistent deviation; not an OEM limit, "
                "component diagnosis, or substitute for a warning lamp"
            ),
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
        rule: WarningRule,
        *,
        at: datetime | str | None = None,
        _refresh_rollups: bool = True,
    ) -> dict[str, object]:
        evaluated = _utc(at)
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


def default_rule_catalog() -> list[dict[str, object]]:
    """Serializable rule metadata for API/UI discovery."""

    return [asdict(rule) for rule in DEFAULT_WARNING_RULES]
