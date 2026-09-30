"""Transparent, tiered early-warning evaluation.

The evaluator consumes only saved historian rows.  It cannot read or transmit
CAN traffic.  Tier 0 rules compare fresh readings with explicit absolute limits
behind context gates (engine running, warm engine, parked, cold tires) and
hold a confirmed warning until the reading is back past the limit by a clear
margin.  Tier 1 rules compare sustained readings with a robust median/MAD
baseline of like-for-like history (sigma floor, minimum effect, escalation
persistence and 0.7 de-escalation hysteresis); an unconfirmed deviation is a
``normal`` assessment carrying an in-memory ``candidate`` object, never a
visible watch.  Tier 3 infrastructure items come from
:class:`InfrastructureHealthEvaluator`.

Every assessment exposes its current evidence, gates, baseline, persistence,
hysteresis and named corroborators, plus the owner-facing grading fields
``tier``, ``group``, ``action`` and ``confidence``.  It deliberately emits no
composite health score and makes no claim to diagnose a component failure.
"""

from __future__ import annotations

import bisect
import functools
import math
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Sequence

from lib.vehicle_can_roles import CAN_BUS_ROLES, CAN_ROLE_SPECS
from lib.timeutil import finite_number as _numeric
from projects.vehicle_data import warning_context
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
TIERS = (0, 1, 2, 3)
GROUPS = (
    "oil",
    "cooling",
    "transmission",
    "charging",
    "battery",
    "tires",
    "system",
    "custom",
)
SEVERITIES = ("critical", "warning", "notice", "info")
# Owner-facing text must not use evaluator vocabulary (same pattern as the
# dashboard's FORBIDDEN_WORDS in dashboard/src/warnings.js).
FORBIDDEN_ACTION_WORDS = re.compile(
    r"\b(regime|mad|deviation|persisten\w*|episode|advisory|unavailable)\b",
    re.IGNORECASE,
)
MAX_ACTION_LENGTH = 120
DEFAULT_ACTION = "Check it at the next stop."
RELATIVE_RATE_LIMIT_SECONDS = 30 * 60
ABSOLUTE_RATE_LIMIT_SECONDS = 30 * 60
RUNNING_RPM = 400.0
RUNNING_GRACE_SECONDS = 10.0
# 2.5 x the p90 8 s observation spacing.  Fresh RPM observations are 10-12 s
# apart 1-46 times per trip (trips 50-57); a 10 s limit restarted the 10 s
# startup grace on each, held the coolant 300 s gate on 0-11 % of ticks in
# trips 50-55 and made the oil-critical rule flap.  A real stop (257 s gap)
# still breaks the running interval.
RUNNING_MAX_GAP_SECONDS = 20.0
RUNNING_LOOKBACK_SECONDS = 15 * 60
RPM_MAX_AGE_SECONDS = 10.0
WARM_COOLANT_F = 160.0
PARKED_SETTLE_SECONDS = 30.0
PARKED_ENGINE_STATES = frozenset(("engine_off", "engine_unknown"))
SLOW_MOTIONS = frozenset(("stationary", "urban"))
CANDIDATE_MEMORY_WINDOWS = 2
# The live historian stores a fresh observation about every 6 s, not 5 s
# (trips 43-57: p50 6.0 s, p90 8.0 s between distinct observations).  A
# run of N consecutive readings must fit its window, so tier-0 and tier-1
# windows allow N x 8 s; with N x 5 s the transmission-hot and
# charging-failure rules (12 obs / 60 s) could never confirm, coolant-hot
# (6 obs / 30 s) rarely, and no tier-1 run (12/60, 24/120, 60/300) or the
# pair-asymmetry run (60/300) was reachable at all.
OBSERVATION_SPACING_ALLOWANCE_SECONDS = 8.0
PHASE_INDEX_CACHE_SECONDS = 60.0
QUALIFIED_QUALITIES = ("verified", "observed_alfa_scale")
ABSOLUTE_GATES = frozenset(
    (
        "running",
        "warm_running",
        "running_rpm_min",
        "parked",
        "tire_cold_phase",
        "tire_pair",
    )
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
WHEEL_LABELS = {"fl": "FL", "fr": "FR", "rl": "RL", "rr": "RR"}
AXLE_NAMES = {("fl", "fr"): "front", ("rl", "rr"): "rear"}
COOLANT_RATE_ACTION = "Pull over and let it idle to cool; check the fan."
INCONCLUSIVE_PRIORITY = ("insufficient_history", "not_applicable", "rejected", "unavailable")


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


def _validate_finite(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{name} must be finite")


def _validate_count(value: object, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _validate_tier(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value not in TIERS:
        raise ValueError("tier must be one of 0, 1, 2, 3")


def _validate_group(value: object) -> None:
    if value not in GROUPS:
        raise ValueError(f"group must be one of {', '.join(GROUPS)}")


def validate_action(value: object, name: str = "action") -> None:
    """One imperative sentence without evaluator vocabulary."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty sentence")
    if len(value) > MAX_ACTION_LENGTH:
        raise ValueError(f"{name} must be at most {MAX_ACTION_LENGTH} characters")
    if not value.endswith("."):
        raise ValueError(f"{name} must end with a period")
    if FORBIDDEN_ACTION_WORDS.search(value):
        raise ValueError(f"{name} must not use evaluator vocabulary")


def derive_group(metric: str) -> str:
    """Default owner-facing group for a vehicle metric."""

    if metric == "engine.oil_pressure":
        return "oil"
    if metric == "engine.coolant_temperature":
        return "cooling"
    if metric.startswith("transmission."):
        return "transmission"
    if metric == "battery.voltage":
        return "charging"
    if metric.startswith("tire.pressure"):
        return "tires"
    return "custom"


@dataclass(frozen=True)
class CorroborationRule:
    """An independently measured companion signal.

    Without ``absolute_minimum`` the companion must deviate from its own
    history-relative band.  With ``absolute_minimum`` it corroborates when the
    fresh companion reading is at or above that value (no baseline needed).
    """

    metric: str
    direction: str
    mad_multiplier: float = 4.5
    minimum_effect: float = 0.0
    max_age_seconds: float = 10.0
    regime_dimensions: tuple[str, ...] | None = None
    absolute_minimum: float | None = None

    def __post_init__(self) -> None:
        _validate_direction(self.direction)
        _validate_positive(self.mad_multiplier, "mad_multiplier")
        _validate_nonnegative(self.minimum_effect, "minimum_effect")
        _validate_positive(self.max_age_seconds, "max_age_seconds")
        if self.regime_dimensions is not None:
            project_regime("engine:motion:rpm:thermal", self.regime_dimensions)
        if self.absolute_minimum is not None:
            _validate_finite(self.absolute_minimum, "absolute_minimum")


@dataclass(frozen=True)
class WarningRule:
    """One explainable tier-1 relative-deviation rule.

    ``minimum_effect`` is a minimum change from the learned median, not an
    absolute safe/unsafe limit.  ``sigma_floor`` keeps a collapsed MAD (tight
    regulation, quantized sensors) from shrinking the band below a physically
    meaningful spread.  A confirmed warning de-escalates only after the effect
    has been at or below ``deescalate_fraction`` of the threshold for the same
    number of samples.  ``group`` and ``action`` get derived defaults when left
    empty so existing call sites stay valid.
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
    recovery_observations: int = 3
    minimum_running_seconds: float = 0
    maximum_delta_c: float | None = None
    maximum_delta_window_seconds: float | None = None
    tier: int = 1
    group: str = ""
    action: str = ""
    sigma_floor: float = 0.0
    deescalate_fraction: float = 0.7
    recovery_seconds: float = 600
    phase_dimension: str | None = None
    thermal_from_metric: bool = False
    companion_band: tuple[str, float, float] | None = None
    rate_of_rise: tuple[float, float, float] | None = None
    # While the newest reading is stationary or urban, a reading counts toward
    # escalation only at or above this value (None = no floor).
    slow_motion_floor: float | None = None

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
            ("recovery_observations", self.recovery_observations),
        ):
            _validate_count(value, name)
        _validate_positive(self.persistence_window_seconds, "persistence_window_seconds")
        _validate_positive(self.max_age_seconds, "max_age_seconds")
        _validate_nonnegative(self.minimum_running_seconds, "minimum_running_seconds")
        if self.minimum_running_seconds > 900 or self.recovery_observations > 60:
            raise ValueError("running or recovery window exceeds the bounded evaluator policy")
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
        _validate_tier(self.tier)
        if self.group:
            _validate_group(self.group)
        else:
            object.__setattr__(self, "group", derive_group(self.metric))
        if self.action:
            validate_action(self.action)
        else:
            object.__setattr__(self, "action", DEFAULT_ACTION)
        _validate_nonnegative(self.sigma_floor, "sigma_floor")
        fraction = self.deescalate_fraction
        if (
            isinstance(fraction, bool)
            or not isinstance(fraction, (int, float))
            or not math.isfinite(float(fraction))
            or not 0 < float(fraction) <= 1
        ):
            raise ValueError("deescalate_fraction must be in (0, 1]")
        _validate_positive(self.recovery_seconds, "recovery_seconds")
        if self.recovery_seconds > 24 * 60 * 60:
            raise ValueError("recovery_seconds exceeds one day")
        if self.phase_dimension not in (None, "tire_phase"):
            raise ValueError("phase_dimension must be None or 'tire_phase'")
        if not isinstance(self.thermal_from_metric, bool):
            raise ValueError("thermal_from_metric must be a boolean")
        if self.companion_band is not None:
            band = tuple(self.companion_band)
            if (
                len(band) != 3
                or not isinstance(band[0], str)
                or not band[0]
            ):
                raise ValueError("companion_band must be (metric, low, high)")
            _validate_finite(band[1], "companion_band low")
            _validate_finite(band[2], "companion_band high")
            if band[1] > band[2]:
                raise ValueError("companion_band low must not exceed high")
            object.__setattr__(self, "companion_band", band)
        if self.rate_of_rise is not None:
            rate = tuple(self.rate_of_rise)
            if len(rate) != 3:
                raise ValueError("rate_of_rise must be (floor, slope per second, window seconds)")
            _validate_finite(rate[0], "rate_of_rise floor")
            _validate_positive(rate[1], "rate_of_rise slope")
            _validate_positive(rate[2], "rate_of_rise window")
            if rate[2] > 900:
                raise ValueError("rate_of_rise window exceeds the bounded evaluator policy")
            object.__setattr__(self, "rate_of_rise", rate)
        if self.slow_motion_floor is not None:
            _validate_finite(self.slow_motion_floor, "slow_motion_floor")


@dataclass(frozen=True)
class AbsoluteOilPressureRule:
    """OEM-context critical oil-pressure advisory with positive RPM gating."""

    key: str = "engine_oil_pressure_absolute_critical"
    title: str = "Oil pressure critical"
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
    running_max_gap_seconds: float = RUNNING_MAX_GAP_SECONDS
    max_age_seconds: float = 5.0
    persistence_observations: int = 2
    persistence_window_seconds: float = 10.0
    notification_rate_limit_seconds: float = 5 * 60
    reference_provenance: str = (
        "exact-vehicle OEM P06DD theory: approximately 12 psi minimum "
        "while the engine is operating; qualified passive 0x41D pressure "
        "and 0x0FC RPM decodes"
    )
    tier: int = 0
    group: str = "oil"
    action: str = "Stop the engine when safe."
    recovery_seconds: float = 600
    clear_margin: float = 2.0

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


@dataclass(frozen=True, kw_only=True)
class AbsoluteRule:
    """A tier-0 absolute limit with a context gate and clear-margin hysteresis.

    ``gate`` selects the applicability test: ``running`` (fresh RPM >= 400
    continuously for >= 10 s), ``warm_running`` (running and coolant >= 160 °F),
    ``running_rpm_min`` (running and RPM >= ``gate_params[0]`` on every
    sample), ``parked`` (no running RPM, >= 30 s after the last trip activity,
    engine off or unknown), ``tire_cold_phase`` (per wheel, cold-tire limits
    from ``wheel_thresholds``, checked in every phase because a warm tire
    only reads higher) or ``tire_pair`` (same-axle
    asymmetry after removing the learned left-right offset; axles in
    ``gate_params``).  ``bands`` are (rpm low, rpm high or None, floor) rows;
    an RPM outside every band is not applicable.  ``companion`` is
    (metric, minimum, max age seconds) and must hold on every sample.
    """

    key: str
    title: str
    metric: str
    group: str
    action: str
    unit: str
    direction: str
    gate: str
    reference_provenance: str
    action_critical: str | None = None
    warning_threshold: float | None = None
    critical_threshold: float | None = None
    clear_margin: float = 0.0
    persistence_observations: int = 3
    persistence_window_seconds: float = 60.0
    max_age_seconds: float = 10.0
    gate_params: tuple = ()
    bands: tuple = ()
    wheel_thresholds: tuple = ()
    companion: tuple | None = None
    qualities: tuple[str, ...] = QUALIFIED_QUALITIES
    recovery_seconds: float = 600
    # Tire rules only: a reading taken after the tires started warming
    # (phase ``warming``/``warm``) clears only at ``(limit + clear_margin) x
    # (1 + warm_clear_fraction)``.  It still escalates against the cold limit.
    warm_clear_fraction: float = 0.0
    tier: int = 0
    notification_rate_limit_seconds: float = ABSOLUTE_RATE_LIMIT_SECONDS

    def __post_init__(self) -> None:
        if not self.key or not self.title or not self.metric or not self.unit:
            raise ValueError("absolute rule key, title, metric and unit must be nonempty")
        _validate_group(self.group)
        validate_action(self.action)
        if self.action_critical is not None:
            validate_action(self.action_critical, "action_critical")
        if self.direction not in ("low", "high"):
            raise ValueError("absolute rule direction must be low or high")
        if self.gate not in ABSOLUTE_GATES:
            raise ValueError(f"gate must be one of {', '.join(sorted(ABSOLUTE_GATES))}")
        for name, value in (
            ("warning_threshold", self.warning_threshold),
            ("critical_threshold", self.critical_threshold),
        ):
            if value is not None:
                _validate_finite(value, name)
        if (
            self.warning_threshold is not None
            and self.critical_threshold is not None
            and (
                (self.direction == "low" and self.critical_threshold > self.warning_threshold)
                or (self.direction == "high" and self.critical_threshold < self.warning_threshold)
            )
        ):
            raise ValueError("critical threshold must be beyond the warning threshold")
        _validate_nonnegative(self.clear_margin, "clear_margin")
        _validate_nonnegative(self.warm_clear_fraction, "warm_clear_fraction")
        _validate_count(self.persistence_observations, "persistence_observations")
        _validate_positive(self.persistence_window_seconds, "persistence_window_seconds")
        _validate_positive(self.max_age_seconds, "max_age_seconds")
        _validate_positive(self.recovery_seconds, "recovery_seconds")
        _validate_tier(self.tier)
        rate = self.notification_rate_limit_seconds
        _validate_positive(rate, "notification_rate_limit_seconds")
        if not 60 <= float(rate) <= 24 * 60 * 60:
            raise ValueError("notification_rate_limit_seconds must be between 60 and 86400")
        if not self.reference_provenance:
            raise ValueError("absolute rule requires reference provenance")
        for name in ("gate_params", "bands", "wheel_thresholds", "qualities"):
            value = getattr(self, name)
            if not isinstance(value, (tuple, list)):
                raise ValueError(f"{name} must be a tuple")
            object.__setattr__(self, name, tuple(
                tuple(item) if isinstance(item, list) else item for item in value
            ))
        for band in self.bands:
            if len(band) != 3:
                raise ValueError("bands rows must be (rpm low, rpm high or None, floor)")
            _validate_finite(band[0], "band rpm low")
            if band[1] is not None:
                _validate_finite(band[1], "band rpm high")
            _validate_finite(band[2], "band floor")
        for row in self.wheel_thresholds:
            if len(row) != 3 or row[0] not in WHEEL_LABELS:
                raise ValueError("wheel_thresholds rows must be (wheel, warning, critical or None)")
            _validate_finite(row[1], "wheel warning threshold")
            if row[2] is not None:
                _validate_finite(row[2], "wheel critical threshold")
        if self.companion is not None:
            companion = tuple(self.companion)
            if len(companion) != 3 or not isinstance(companion[0], str) or not companion[0]:
                raise ValueError("companion must be (metric, minimum, max age seconds)")
            _validate_finite(companion[1], "companion minimum")
            _validate_positive(companion[2], "companion max age")
            object.__setattr__(self, "companion", companion)
        if self.gate == "running_rpm_min":
            if len(self.gate_params) != 1:
                raise ValueError("running_rpm_min requires one RPM minimum")
            _validate_positive(self.gate_params[0], "gate RPM minimum")
        if self.gate == "tire_cold_phase" and not self.wheel_thresholds:
            raise ValueError("tire_cold_phase requires wheel_thresholds")
        if self.gate == "tire_pair":
            if not self.gate_params or any(
                len(pair) != 2 or any(wheel not in WHEEL_LABELS for wheel in pair)
                for pair in self.gate_params
            ):
                raise ValueError("tire_pair requires (left, right) wheel pairs")
            if self.warning_threshold is None:
                raise ValueError("tire_pair requires an asymmetry warning threshold")
        if (
            self.warning_threshold is None
            and self.critical_threshold is None
            and not self.bands
            and not self.wheel_thresholds
        ):
            raise ValueError("absolute rule requires a threshold, bands or wheel thresholds")

    @property
    def second_source(self) -> bool:
        return self.companion is not None or self.gate == "tire_pair"


@dataclass(frozen=True, kw_only=True)
class TireGroupRule:
    """One combined tier-1 tire rule over every wheel.

    Each wheel is compared with its own history in the same trip phase (cold
    to cold, warming to warming, warm to warm); the assessment reports the
    worst wheel and lists every wheel past its threshold.
    """

    key: str = "tire_pressure_relative_low"
    title: str = "Tire pressure low"
    metric: str = "tire.pressure"
    wheels: tuple[str, ...] = ("fl", "fr", "rl", "rr")
    direction: str = "low"
    tier: int = 1
    group: str = "tires"
    action: str = "Check the tire pressure at the next stop."
    mad_multiplier: float = 4.5
    minimum_effect: float = 3.0
    sigma_floor: float = 0.8
    deescalate_fraction: float = 0.7
    minimum_baseline_buckets: int = 30
    minimum_baseline_trips: int = 3
    lookback_days: int = 30
    persistence_observations: int = 60
    persistence_window_seconds: float = 60 * OBSERVATION_SPACING_ALLOWANCE_SECONDS
    max_age_seconds: float = 35.0
    regime_dimensions: tuple[str, ...] = ("engine",)
    phase_dimension: str = "tire_phase"
    recovery_seconds: float = 600
    notification_rate_limit_seconds: float = RELATIVE_RATE_LIMIT_SECONDS

    def __post_init__(self) -> None:
        if not self.key or not self.title or not self.metric:
            raise ValueError("tire rule key, title and metric must be nonempty")
        if not self.wheels or any(wheel not in WHEEL_LABELS for wheel in self.wheels):
            raise ValueError("wheels must be a nonempty subset of fl, fr, rl, rr")
        if len(set(self.wheels)) != len(self.wheels):
            raise ValueError("wheels must be unique")
        if self.phase_dimension != "tire_phase":
            raise ValueError("the combined tire rule is conditioned on the tire phase")
        # Validate every shared parameter through the per-wheel rule.
        _wheel_rule(self, self.wheels[0])

    def wheel_rule(self, wheel: str) -> WarningRule:
        return _wheel_rule(self, wheel)


@functools.lru_cache(maxsize=64)
def _wheel_rule(rule: TireGroupRule, wheel: str) -> WarningRule:
    return WarningRule(
        key=f"{rule.key}.{wheel}",
        title=f"{WHEEL_LABELS[wheel]} tire low",
        metric=f"{rule.metric}.{wheel}",
        direction=rule.direction,
        mad_multiplier=rule.mad_multiplier,
        minimum_effect=rule.minimum_effect,
        minimum_baseline_buckets=rule.minimum_baseline_buckets,
        minimum_baseline_trips=rule.minimum_baseline_trips,
        lookback_days=rule.lookback_days,
        persistence_observations=rule.persistence_observations,
        persistence_window_seconds=rule.persistence_window_seconds,
        max_age_seconds=rule.max_age_seconds,
        regime_dimensions=rule.regime_dimensions,
        tier=rule.tier,
        group=rule.group,
        action=rule.action,
        sigma_floor=rule.sigma_floor,
        deescalate_fraction=rule.deescalate_fraction,
        recovery_seconds=rule.recovery_seconds,
        phase_dimension=rule.phase_dimension,
    )


EvaluationRule = WarningRule | AbsoluteOilPressureRule | AbsoluteRule | TireGroupRule


@functools.lru_cache(maxsize=256)
def _cached_snapshot(rule: object) -> tuple[dict[str, object], str]:
    from projects.vehicle_data.event_history import digest
    snapshot = asdict(rule)  # type: ignore[call-overload]
    return snapshot, digest(snapshot)


def _rule_snapshot(rule: object) -> tuple[dict[str, object], str]:
    """``asdict`` and revision digest of a frozen rule, computed once per rule."""

    try:
        snapshot, revision = _cached_snapshot(rule)
    except TypeError:  # an unhashable field value (e.g. a list) was supplied
        from projects.vehicle_data.event_history import digest
        snapshot = asdict(rule)  # type: ignore[call-overload]
        return snapshot, digest(snapshot)
    return dict(snapshot), revision


# Tier 1: sustained same-conditions deviation.  None of these is an OEM
# limit.  The regime includes running state, speed band and RPM band (plus the
# coolant band for oil pressure); transmission takes its thermal band from its
# own value, battery compares only against full-output generator history and
# requires the generator corroborator, and tires compare by trip phase.
DEFAULT_WARNING_RULES: tuple[WarningRule | TireGroupRule, ...] = (
    WarningRule(
        key="engine_oil_pressure_relative_low",
        title="Oil pressure lower than usual",
        metric="engine.oil_pressure",
        direction="low",
        minimum_effect=5.0,
        sigma_floor=1.0,
        persistence_observations=12,
        persistence_window_seconds=12 * OBSERVATION_SPACING_ALLOWANCE_SECONDS,
        regime_dimensions=("engine", "motion", "rpm", "thermal"),
        group="oil",
        action="Check the oil level at the next stop.",
    ),
    WarningRule(
        key="engine_coolant_temperature_relative_high",
        minimum_running_seconds=300,
        title="Coolant hotter than usual",
        metric="engine.coolant_temperature",
        direction="high",
        minimum_effect=12.0,
        sigma_floor=3.0,
        persistence_observations=24,
        persistence_window_seconds=24 * OBSERVATION_SPACING_ALLOWANCE_SECONDS,
        regime_dimensions=("engine", "motion", "rpm"),
        rate_of_rise=(215.0, 0.1, 180.0),
        # At stationary/urban idle the fan cycles the coolant between 203 and
        # 221 F.  The deviation bar (median 190.4-192.2 + 13.5-24) sits inside
        # that band and moves with whichever hot idles are in the baseline:
        # with the 09-17 bar of 203.9 trip 52 would have held a 133-reading /
        # 873 s "deviation" through a normal fan cycle.  No rollup bucket in
        # all 57 trips reaches 222 F (observed max 221; spec provenance:
        # thermostat fully open near 220), so slow-motion readings count only
        # from 222 F.  Road and highway keep the plain deviation.
        slow_motion_floor=222.0,
        group="cooling",
        action="Ease off and watch the gauge.",
    ),
    WarningRule(
        key="transmission_oil_temperature_relative_high",
        title="Transmission hotter than usual",
        metric="transmission.oil_temperature",
        direction="high",
        minimum_effect=12.0,
        sigma_floor=3.0,
        persistence_observations=24,
        persistence_window_seconds=24 * OBSERVATION_SPACING_ALLOWANCE_SECONDS,
        regime_dimensions=("engine", "motion", "rpm"),
        thermal_from_metric=True,
        # Reject a discontinuity before it can enter persistence.  The stored
        # display metric is Fahrenheit; evaluation converts the delta back to
        # Celsius and applies the OEM P0711 >10 °C in <1 second criterion.
        maximum_delta_c=10.0,
        maximum_delta_window_seconds=1.0,
        group="transmission",
        action="Ease off and let it cool.",
    ),
    WarningRule(
        key="battery_voltage_relative_low",
        title="Charging lower than usual",
        metric="battery.voltage",
        direction="low",
        minimum_effect=0.8,
        sigma_floor=0.15,
        persistence_observations=24,
        persistence_window_seconds=24 * OBSERVATION_SPACING_ALLOWANCE_SECONDS,
        max_age_seconds=35.0,
        regime_dimensions=("engine", "motion", "rpm"),
        corroborators=(
            CorroborationRule(
                metric=GENERATOR_DUTY_METRIC,
                direction="high",
                absolute_minimum=85.0,
            ),
        ),
        # A PCM charge reduction lowers voltage at low duty; only a generator
        # already at full output while voltage sags is actionable.
        required_corroborators=1,
        companion_band=(GENERATOR_DUTY_METRIC, 85.0, 100.0),
        group="charging",
        action="Limit accessories and check charging.",
    ),
    TireGroupRule(),
)

# Tier 0: absolute limits with context gates.  Provenance strings are copied
# from dashboard/docs/warnings-redesign.md section 3.
DEFAULT_ABSOLUTE_WARNING_RULES: tuple[AbsoluteOilPressureRule | AbsoluteRule, ...] = (
    AbsoluteOilPressureRule(),
    AbsoluteRule(
        key="engine_oil_pressure_below_band",
        title="Oil pressure low",
        metric="engine.oil_pressure",
        group="oil",
        action="Check the oil level at the next stop.",
        unit="psi",
        direction="low",
        gate="warm_running",
        bands=((550.0, 850.0, 15.0), (1000.0, 3000.0, 22.0), (3500.0, None, 55.0)),
        clear_margin=2.0,
        persistence_observations=3,
        persistence_window_seconds=3 * OBSERVATION_SPACING_ALLOWANCE_SECONDS,
        max_age_seconds=10.0,
        reference_provenance=(
            "OEM warm bands 15–34 / 28–35 / 65–80; rollup minima ≥ 25 psi in "
            "2,735 of 2,736 running buckets"
        ),
    ),
    AbsoluteRule(
        key="engine_coolant_temperature_hot",
        title="Coolant hot",
        metric="engine.coolant_temperature",
        group="cooling",
        action="Reduce load and watch the gauge.",
        action_critical="Pull over and let it idle to cool.",
        unit="°F",
        direction="high",
        gate="running",
        warning_threshold=230.0,
        critical_threshold=240.0,
        clear_margin=5.0,
        persistence_observations=6,
        persistence_window_seconds=6 * OBSERVATION_SPACING_ALLOWANCE_SECONDS,
        max_age_seconds=10.0,
        reference_provenance=(
            "thermostat fully open ≈ 220; observed max 221 in 57 trips; owner limit"
        ),
    ),
    AbsoluteRule(
        key="transmission_oil_temperature_hot",
        title="Transmission hot",
        metric="transmission.oil_temperature",
        group="transmission",
        action="Ease off and let it cool.",
        unit="°F",
        direction="high",
        gate="running",
        warning_threshold=230.0,
        clear_margin=5.0,
        persistence_observations=12,
        persistence_window_seconds=12 * OBSERVATION_SPACING_ALLOWANCE_SECONDS,
        max_age_seconds=10.0,
        reference_provenance="top of OEM adaptation window 122–230; observed max 183",
    ),
    AbsoluteRule(
        key="battery_voltage_charging_failure",
        title="Charging problem",
        metric="battery.voltage",
        group="charging",
        action="Limit accessories and check charging.",
        unit="V",
        direction="low",
        # Any running reading counts (400 rpm, the running threshold), so a dead alternator is
        # caught in stop-and-go traffic too (iteration-2 safety review S2). The 10 s running
        # grace still excludes the cranking dip, and saturated duty (>= 90 %) on every sample
        # separates a failing alternator from the PCM's normal idle charge reduction.
        gate="running_rpm_min",
        gate_params=(400.0,),
        companion=(GENERATOR_DUTY_METRIC, 90.0, 10.0),
        warning_threshold=12.0,
        clear_margin=0.5,
        persistence_observations=12,
        persistence_window_seconds=12 * OBSERVATION_SPACING_ALLOWANCE_SECONDS,
        max_age_seconds=35.0,
        reference_provenance=(
            "duty saturation separates a PCM charge reduction (duty 20–50 %) from "
            "an alternator that cannot keep up; 18 past corroborating cases"
        ),
    ),
    AbsoluteRule(
        key="battery_voltage_low_parked",
        title="Battery low",
        metric="battery.voltage",
        group="battery",
        action="Start the engine or charge the battery soon.",
        unit="V",
        direction="low",
        gate="parked",
        warning_threshold=11.8,
        critical_threshold=11.5,
        clear_margin=0.2,
        persistence_observations=3,
        persistence_window_seconds=60.0,
        max_age_seconds=35.0,
        reference_provenance=(
            "parked median 12.45 V; the five archived 11.2–11.8 V watches"
        ),
    ),
    AbsoluteRule(
        key="tire_pressure_low_absolute",
        title="Tire pressure low",
        metric="tire.pressure",
        group="tires",
        action="Inflate the tire before driving far.",
        unit="psi",
        direction="low",
        gate="tire_cold_phase",
        wheel_thresholds=(
            ("fl", 50.0, 45.0),
            ("fr", 50.0, 45.0),
            ("rl", 68.0, 60.0),
            ("rr", 68.0, 60.0),
        ),
        clear_margin=2.0,
        # Tires gain 3.2-4.4 psi (5.9-6.4 %) during a drive (trips 43-57), so
        # a front that warned at 49 psi cold reads 52-53 psi warm.  Clearing
        # against the cold limit sent a false "normal" push about 10 minutes
        # into every drive, and that recovery push ended the group cooldown,
        # so the next cold morning pushed again.  Warm readings clear only at
        # 56.2 / 75.6 psi; a cold reading still clears at 52 / 70.
        warm_clear_fraction=0.08,
        persistence_observations=3,
        persistence_window_seconds=60.0,
        max_age_seconds=35.0,
        reference_provenance=(
            "placard-ish 55/75; stationary minima 51.4–53.0 / 70.5–71.7"
        ),
    ),
    AbsoluteRule(
        key="tire_pressure_pair_asymmetry",
        title="Tire pressures uneven",
        metric="tire.pressure",
        group="tires",
        action="Check both tires on that axle at the next stop.",
        unit="psi",
        direction="high",
        gate="tire_pair",
        gate_params=(("fl", "fr"), ("rl", "rr")),
        warning_threshold=3.0,
        clear_margin=1.0,
        persistence_observations=60,
        persistence_window_seconds=60 * OBSERVATION_SPACING_ALLOWANCE_SECONDS,
        max_age_seconds=35.0,
        # Episode 642 (trip 59) stayed open 17 min while both rear tires read
        # the same: 60 clear readings, then the default 600 s recovery.  Only
        # fresh, moving readings count now (PAIR_* below), so a shorter hold
        # is safe: two rolling TPMS transmission periods (~64 s each).
        # Re-opening still needs the full 60-reading run, so it cannot flicker.
        recovery_seconds=120,
        reference_provenance=(
            "mean offsets FL−FR +1.71, RL−RR +0.78 psi; transient excursions to 6 psi"
        ),
    ),
)
DEFAULT_EVALUATION_RULES: tuple[EvaluationRule, ...] = (
    *DEFAULT_ABSOLUTE_WARNING_RULES,
    *DEFAULT_WARNING_RULES,
)
PAIR_OFFSET_DIMENSIONS = ("engine", "motion")
PAIR_OFFSET_MINIMUM_BUCKETS = 30
PAIR_OFFSET_MINIMUM_TRIPS = 3
PAIR_OFFSET_LOOKBACK_DAYS = 30
PAIR_COMPANION_MAX_AGE_SECONDS = 35.0
# Pair asymmetry counts only readings that both sensors sent recently.  The RF
# hub repeats each wheel's last received value.  A TPMS sensor sends about once
# every 64 s while rolling: across trips 43-62, the gaps between value changes
# peak at 64 s and its multiples.  While the van stands, a sensor sends rarely.
# In trip 59 (episode 642, 2026-09-24) the van idled for about 23 min.  RL
# reported its cooling tire (79.6 -> 76.0 psi) while RR repeated 79.6.  That
# made a false 4.4 psi gap, and the warning opened as the van pulled away.
#   * Stationary gate: a reading counts only when a fresh vehicle.speed of at
#     least PAIR_MOVING_SPEED_MPH is at most PAIR_SPEED_MAX_AGE_SECONDS older
#     than it.  Across trips 55-62 that speed-to-tire lag is p99 10 s and at
#     most 17 s.
#   * Fresh-transmission gate: after the van starts moving, a wheel counts
#     once its value has changed since then, or once the van has moved without
#     a stop for PAIR_ROLLING_TRUST_SECONDS.  By then each sensor has sent at
#     least twice.  A steady warm tire can hold one value for up to 20 min on
#     the highway (trips 43-62), so a strict "changed" test would blind the
#     rule for most of a drive.
#   * Clearing needs PAIR_CLEAR_OBSERVATIONS fresh, moving readings (8 x ~8 s
#     is about one rolling transmission period), not 60.
PAIR_MOVING_SPEED_MPH = 5.0
PAIR_SPEED_MAX_AGE_SECONDS = 20.0
PAIR_ROLLING_TRUST_SECONDS = 150.0
PAIR_CLEAR_OBSERVATIONS = 8


@functools.lru_cache(maxsize=4096)
def _project(regime: str, dimensions: tuple[str, ...]) -> str:
    """Memoized ``project_regime`` (pure; stored regimes repeat constantly)."""

    return project_regime(regime, dimensions)


def _baseline_dict(baseline: BaselineStats) -> dict[str, object]:
    """``BaselineStats.as_dict()`` without the recursive deep copy.

    The input preview holds flat dicts, so shallow copies are equivalent.
    """

    return {
        "metric": baseline.metric,
        "regime": baseline.regime,
        "unit": baseline.unit,
        "quality": baseline.quality,
        "source": baseline.source,
        "provenance": baseline.provenance,
        "bucket_count": baseline.bucket_count,
        "trip_count": baseline.trip_count,
        "sample_count": baseline.sample_count,
        "median": baseline.median,
        "mad": baseline.mad,
        "minimum": baseline.minimum,
        "maximum": baseline.maximum,
        "first_at": baseline.first_at,
        "last_at": baseline.last_at,
        "input_digest": baseline.input_digest,
        "input_buckets": tuple(dict(item) for item in baseline.input_buckets),
        "input_buckets_complete": baseline.input_buckets_complete,
        "robust_sigma": baseline.robust_sigma,
    }


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


class EarlyWarningEvaluator:
    """Evaluate tier-0 absolute limits and tier-1 like-for-like deviations."""

    def __init__(
        self,
        historian: TelemetryHistorian,
        rules: Sequence[EvaluationRule] = DEFAULT_EVALUATION_RULES,
        custom_rules_path=None,
    ):
        self.historian = historian
        self.custom_rules_path = custom_rules_path
        self.rules = tuple(rules)
        keys = [rule.key for rule in self.rules]
        if len(keys) != len(set(keys)):
            raise ValueError("warning rule keys must be unique")
        # Offline replay sets a dict here to memoize every baseline; production
        # leaves it None so each tick reads current history.
        self.baseline_memo: dict | None = None
        # Counters for the offline memo (replay reporting); unused in production.
        self.baseline_memo_stats = {"hits": 0, "misses": 0}
        self._candidates: dict[str, dict[str, object]] = {}
        self._phase_cache: tuple[int, dict[int, dict[str, object]]] | None = None
        self._lock = threading.RLock()
        self._local = threading.local()
        self._lookbacks: dict[str, float] = {}
        for rule in self.rules:
            for metric, seconds in _rule_lookbacks(rule).items():
                self._lookbacks[metric] = max(self._lookbacks.get(metric, 0.0), seconds)

    # ------------------------------------------------------------------
    # Shared per-tick reads

    def _tick(self, at: datetime) -> tuple[_Tick, bool]:
        tick = getattr(self._local, "tick", None)
        if isinstance(tick, _Tick) and tick.at == at:
            return tick, False
        tick = _Tick(at)
        self._local.tick = tick
        return tick, True

    def _latest(self, tick: _Tick, metric: str) -> dict[str, object] | None:
        if metric not in tick.latest:
            tick.latest[metric] = self.historian.latest_sample(
                metric, at=tick.at, fresh_only=True
            )
        return tick.latest[metric]

    def _series(
        self,
        tick: _Tick,
        metric: str,
        sample: Mapping[str, object],
        *,
        lookback: float = 0.0,
    ) -> list[dict[str, object]]:
        trip_id = sample.get("trip_id")
        key = (
            metric,
            None if trip_id is None else int(trip_id),
            str(sample["source"]),
            str(sample["quality"]),
            str(sample["provenance"]),
        )
        wanted = max(float(lookback), self._lookbacks.get(metric, 0.0), 60.0)
        cached = tick.series.get(key)
        if cached is not None and cached[0] >= wanted:
            return cached[1]
        rows = warning_context.recent_series(
            self.historian,
            metric,
            trip_id=key[1],
            at=tick.at,
            lookback_seconds=wanted,
            source=key[2],
            quality=key[3],
            provenance=key[4],
        )
        tick.series[key] = (wanted, rows)
        return rows

    def _companion_values(
        self,
        tick: _Tick,
        metric: str,
        primary: Mapping[str, object],
        *,
        lookback: float,
    ) -> list[tuple[int, float]]:
        latest = self._latest(tick, metric)
        if latest is None or not _numeric(latest.get("value")):
            return []
        probe = {
            "trip_id": primary.get("trip_id"),
            "source": latest["source"],
            "quality": latest["quality"],
            "provenance": latest["provenance"],
        }
        values = []
        for point in self._series(tick, metric, probe, lookback=lookback):
            observed = _observed_us(point)
            if observed is not None and _numeric(point.get("value")):
                values.append((observed, float(point["value"])))
        return values

    def _continuous(
        self,
        tick: _Tick,
        rpm: Mapping[str, object],
        *,
        minimum: float,
        lookback: float,
        max_gap: float = RUNNING_MAX_GAP_SECONDS,
    ) -> dict[str, object] | None:
        trip_id = rpm.get("trip_id")
        key = (
            "continuous",
            str(rpm["source"]),
            str(rpm["quality"]),
            str(rpm["provenance"]),
            trip_id,
            float(minimum),
            float(lookback),
            float(max_gap),
        )
        if key not in tick.conditions:
            tick.conditions[key] = self.historian.continuous_numeric_condition(
                "engine.rpm",
                at=tick.at,
                minimum=minimum,
                max_gap_seconds=float(max_gap),
                source=str(rpm["source"]),
                quality=str(rpm["quality"]),
                provenance=str(rpm["provenance"]),
                trip_id=trip_id,
                max_lookback_seconds=lookback,
            )
        return tick.conditions[key]  # type: ignore[return-value]

    def _open_info(self, tick: _Tick, key: str) -> dict[str, object]:
        """Open-episode state for a rule, read once per evaluation.

        ``state`` is ``advisory_episodes.current_state``, which stays
        ``warning`` while the lifecycle holds a recovering episode.
        """

        if tick.open_states is None:
            states: dict[str, dict[str, object]] = {}
            try:
                episodes = self.historian.list_advisory_episodes(
                    active_only=True, limit=200
                )
                for episode in episodes:
                    rule = episode.get("rule")
                    if not isinstance(rule, str):
                        continue
                    latest = episode.get("latest_assessment")
                    latest = latest if isinstance(latest, Mapping) else {}
                    held = latest.get("held_keys")
                    first = episode.get("first_assessment")
                    first = first if isinstance(first, Mapping) else {}
                    pair = first.get("pair")
                    pair = pair if isinstance(pair, Mapping) else {}
                    states[rule] = {
                        "opened": {
                            "wheel": first.get("wheel"),
                            "metric": first.get("metric"),
                            "axle": pair.get("axle"),
                            "lower": pair.get("lower_wheel"),
                            "left": pair.get("left"),
                            "right": pair.get("right"),
                        },
                        "state": episode.get("state"),
                        "severity": latest.get("severity"),
                        "held": tuple(
                            item for item in held if isinstance(item, str)
                        ) if isinstance(held, list) else (),
                    }
            except (AttributeError, TypeError, ValueError):
                states = {}
            tick.open_states = states
        return tick.open_states.get(key, {})

    def _phase_index(
        self,
        tick: _Tick,
        *,
        trip_id: object = None,
    ) -> dict[int, dict[str, object]]:
        wanted = None if trip_id is None or isinstance(trip_id, bool) else int(trip_id)
        if tick.phase_index is not None and (wanted is None or wanted in tick.phase_index):
            return tick.phase_index
        with self._lock:
            cached = self._phase_cache
            fresh = (
                cached is not None
                and 0 <= tick.at_us - cached[0] < PHASE_INDEX_CACHE_SECONDS * 1_000_000
                and (wanted is None or wanted in cached[1])
            )
            if fresh:
                index = cached[1]
                warning_context.refine_open_trips(self.historian, index, at=tick.at_us)
            else:
                index = warning_context.trip_phase_index(self.historian, at=tick.at)
                self._phase_cache = (tick.at_us, index)
        tick.phase_index = index
        return index

    def _last_trip(self, tick: _Tick) -> dict[str, object] | None:
        if tick.last_trip is ...:
            try:
                trips = self.historian.list_trips(limit=1)
            except (AttributeError, TypeError, ValueError):
                trips = []
            tick.last_trip = trips[0] if trips else None
        return tick.last_trip  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Baselines and candidate memory

    def _baseline(self, key: tuple, compute: Callable[[], BaselineStats | None]):
        memo = self.baseline_memo
        if not isinstance(memo, dict):
            return compute()
        try:
            if key in memo:
                self.baseline_memo_stats["hits"] += 1
                return memo[key]
        except TypeError:
            return compute()
        self.baseline_memo_stats["misses"] += 1
        value = compute()
        memo[key] = value
        return value

    @staticmethod
    def _memo_key(
        metric: str,
        sample: Mapping[str, object],
        at: datetime,
        *,
        dimensions: Sequence[str],
        lookback_days: int,
        minimum_trip_age_seconds: float = 0,
        median_range: tuple | None = None,
        companion: tuple | None = None,
        phase: str | None = None,
    ) -> tuple:
        trip_id = sample.get("trip_id")
        return (
            metric,
            project_regime(str(sample["regime"]), dimensions),
            str(sample["unit"]),
            str(sample["quality"]),
            str(sample["source"]),
            str(sample["provenance"]),
            tuple(dimensions),
            int(lookback_days),
            float(minimum_trip_age_seconds),
            None if median_range is None else tuple(median_range),
            None if companion is None else tuple(companion),
            phase,
            int(trip_id) if trip_id is not None else f"hour:{_to_us(at) // 3_600_000_000}",
        )

    def _baseline_for(
        self,
        *,
        metric: str,
        sample: dict[str, object],
        at: datetime,
        lookback_days: int,
        regime_dimensions: Sequence[str],
        minimum_trip_age_seconds: float = 0,
    ) -> BaselineStats | None:
        key = self._memo_key(
            metric,
            sample,
            at,
            dimensions=regime_dimensions,
            lookback_days=lookback_days,
            minimum_trip_age_seconds=minimum_trip_age_seconds,
        )
        return self._baseline(
            key,
            lambda: self.historian.robust_baseline(
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
                minimum_trip_age_seconds=minimum_trip_age_seconds,
            ),
        )

    def _rollup_baseline(
        self,
        *,
        metric: str,
        sample: dict[str, object],
        at: datetime,
        lookback_days: int,
        regime_dimensions: Sequence[str],
        minimum_trip_age_seconds: float = 0,
        median_range: tuple | None = None,
        companion: tuple | None = None,
        phase: str | None = None,
        phase_index: Mapping[int, Mapping[str, object]] | None = None,
        label: str | None = None,
    ) -> BaselineStats | None:
        key = self._memo_key(
            metric,
            sample,
            at,
            dimensions=regime_dimensions,
            lookback_days=lookback_days,
            minimum_trip_age_seconds=minimum_trip_age_seconds,
            median_range=median_range,
            companion=companion,
            phase=phase,
        )
        return self._baseline(
            key,
            lambda: warning_context.rollup_baseline(
                self.historian,
                metric,
                regime=str(sample["regime"]),
                regime_dimensions=regime_dimensions,
                before=at,
                lookback_days=lookback_days,
                exclude_trip_id=(
                    int(sample["trip_id"]) if sample.get("trip_id") is not None else None
                ),
                unit=str(sample["unit"]),
                quality=str(sample["quality"]),
                source=str(sample["source"]),
                provenance=str(sample["provenance"]),
                median_range=median_range,
                phase=None if phase is None else (phase, phase_index or {}),
                companion=companion,
                minimum_trip_age_seconds=minimum_trip_age_seconds,
                label=label,
            ),
        )

    def _candidate(
        self,
        key: str,
        *,
        at_us: int,
        first_seen_at: str | None,
        observed: int,
        required: int,
        window_seconds: float,
    ) -> dict[str, object]:
        """Remember an unconfirmed deviation; it never opens an episode."""

        with self._lock:
            entry = self._candidates.get(key)
            if entry is not None and (
                at_us - int(entry["last_seen_us"])
                > CANDIDATE_MEMORY_WINDOWS * float(entry["window_seconds"]) * 1_000_000
            ):
                entry = None
            if entry is None:
                entry = {
                    "first_seen_at": first_seen_at,
                    "last_seen_us": at_us,
                    "window_seconds": float(window_seconds),
                }
                self._candidates[key] = entry
            else:
                entry["last_seen_us"] = max(int(entry["last_seen_us"]), at_us)
                entry["window_seconds"] = float(window_seconds)
            return {
                "observed": int(observed),
                "required": int(required),
                "window_seconds": float(window_seconds),
                "first_seen_at": entry["first_seen_at"],
                "confidence": "low",
            }

    def _drop_candidate(self, key: str) -> None:
        with self._lock:
            self._candidates.pop(key, None)

    def _prune_candidates(self, at_us: int) -> None:
        with self._lock:
            for key, entry in list(self._candidates.items()):
                limit = CANDIDATE_MEMORY_WINDOWS * float(entry["window_seconds"]) * 1_000_000
                if at_us - int(entry["last_seen_us"]) > limit:
                    self._candidates.pop(key, None)

    # ------------------------------------------------------------------
    # Assessment bases

    @staticmethod
    def _current_payload(
        sample: dict[str, object], age_seconds: float
    ) -> dict[str, object]:
        return {
            "sample_id": sample.get("sample_id"),
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
        rule_snapshot, revision = _rule_snapshot(rule)
        return {
            "rule_snapshot": rule_snapshot,
            "rule_revision": revision,
            "evaluator_revision": "relative-v3",
            "rule": rule.key,
            "title": rule.title,
            "metric": rule.metric,
            "direction": rule.direction,
            "category": "vehicle_health",
            "severity": "warning",
            "advisory": True,
            "notification_eligible": False,
            "notification_rate_limit_seconds": RELATIVE_RATE_LIMIT_SECONDS,
            "interpretation": (
                "history-relative persistent deviation; not an OEM limit, "
                "component diagnosis, or substitute for a warning lamp"
            ),
            "tier": rule.tier,
            "group": rule.group,
            "action": rule.action,
            "confidence": None,
        }

    @staticmethod
    def _tire_base(rule: TireGroupRule) -> dict[str, object]:
        rule_snapshot, revision = _rule_snapshot(rule)
        return {
            "rule_snapshot": rule_snapshot,
            "rule_revision": revision,
            "evaluator_revision": "tires-v1",
            "rule": rule.key,
            "title": rule.title,
            "metric": rule.metric,
            "direction": rule.direction,
            "category": "vehicle_health",
            "severity": "warning",
            "advisory": True,
            "notification_eligible": False,
            "notification_rate_limit_seconds": rule.notification_rate_limit_seconds,
            "interpretation": (
                "tire pressure compared with the same wheel in the same trip phase; "
                "not a placard limit or a diagnosis"
            ),
            "tier": rule.tier,
            "group": rule.group,
            "action": rule.action,
            "confidence": None,
        }

    @staticmethod
    def _absolute_base(rule: AbsoluteOilPressureRule) -> dict[str, object]:
        rule_snapshot, revision = _rule_snapshot(rule)
        return {
            "rule_snapshot": rule_snapshot, "rule_revision": revision,
            "evaluator_revision": "absolute-oil-v2",
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
            "tier": rule.tier,
            "group": rule.group,
            "action": rule.action,
            "confidence": None,
        }

    @staticmethod
    def _absolute_rule_base(rule: AbsoluteRule) -> dict[str, object]:
        rule_snapshot, revision = _rule_snapshot(rule)
        threshold = (
            rule.warning_threshold
            if rule.warning_threshold is not None
            else rule.critical_threshold
        )
        return {
            "rule_snapshot": rule_snapshot,
            "rule_revision": revision,
            "evaluator_revision": "absolute-v1",
            "rule": rule.key,
            "title": rule.title,
            "metric": rule.metric,
            "direction": rule.direction,
            "category": "vehicle_health",
            "severity": (
                "critical"
                if rule.warning_threshold is None
                and rule.critical_threshold is not None
                and not rule.bands
                and not rule.wheel_thresholds
                else "warning"
            ),
            "advisory": True,
            "notification_eligible": False,
            "notification_rate_limit_seconds": rule.notification_rate_limit_seconds,
            "interpretation": (
                "absolute limit behind a context gate; it does not diagnose the "
                "cause or replace a factory warning lamp"
            ),
            "absolute_threshold": {
                "operator": "below" if rule.direction == "low" else "above",
                "value": threshold,
                "unit": rule.unit,
            },
            "reference_provenance": rule.reference_provenance,
            "tier": rule.tier,
            "group": rule.group,
            "action": rule.action,
            "confidence": None,
        }

    @staticmethod
    def _dimension_names(rule: WarningRule) -> list[str]:
        names = list(rule.regime_dimensions)
        if rule.thermal_from_metric:
            names.append("thermal_band")
        if rule.companion_band is not None:
            names.append("duty_band")
        if rule.phase_dimension:
            names.append(rule.phase_dimension)
        return names

    # ------------------------------------------------------------------
    # Tier 1: relative rules

    def _plausibility(
        self,
        rule: WarningRule,
        *,
        sample: Mapping[str, object],
        series: Sequence[Mapping[str, object]],
    ) -> dict[str, object] | None:
        delta_limit = rule.maximum_delta_c
        window = rule.maximum_delta_window_seconds
        if delta_limit is None or window is None:
            return None
        dimensions = tuple(
            name for name in rule.regime_dimensions if name != "thermal"
        ) or ("engine",)
        target = _project(str(sample["regime"]), dimensions)
        recent = [
            point
            for point in series
            if _project(str(point["regime"]), dimensions) == target
        ][:2]
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

    def _projection(
        self,
        rule: WarningRule,
        sample: Mapping[str, object],
        phase_index: Mapping[int, Mapping[str, object]] | None,
    ) -> tuple:
        parts: list[object] = [_project(str(sample["regime"]), tuple(rule.regime_dimensions))]
        if rule.thermal_from_metric:
            band = warning_context.transmission_band(sample.get("value"))
            parts.append(band[0] if band else None)
        if rule.phase_dimension == "tire_phase":
            parts.append(warning_context.phase_of(sample, phase_index or {}))
        return tuple(parts)

    def _rate_of_rise(
        self,
        rule: WarningRule,
        sample: Mapping[str, object],
        series: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        floor, minimum_slope, window = rule.rate_of_rise  # type: ignore[misc]
        newest_us = _observed_us(sample)
        engine = _engine(sample)
        points: list[tuple[float, float]] = []
        motions: set[str] = set()
        if newest_us is not None:
            for point in series:
                if _engine(point) != engine:
                    continue
                point_us = _observed_us(point)
                if point_us is None:
                    continue
                elapsed = (newest_us - point_us) / 1_000_000
                if elapsed < 0:
                    continue
                if elapsed > window:
                    break
                points.append((-elapsed, float(point["value"])))
                motions.add(_motion(point))
        span = -points[-1][0] if points else 0.0
        slope = _slope(points)
        value = float(sample["value"])
        # Strict: every reading in the window must already be at or above the
        # floor, so a normal warm-up or fan cycle climbing through it never
        # triggers.  With the default (215 F, 0.1 F/s, 180 s) that implies the
        # newest reading is >= 232 F.
        window_min = min(v for _, v in points) if points else None
        triggered = bool(
            value >= floor
            and window_min is not None
            and window_min >= floor
            and len(points) >= 3
            and span >= window - 10.0
            and motions
            and motions <= SLOW_MOTIONS
            and slope is not None
            and slope >= minimum_slope
        )
        return {
            "triggered": triggered,
            "slope_f_per_s": slope,
            "span_seconds": span,
            "samples": len(points),
            "floor": floor,
            "minimum_slope_f_per_s": minimum_slope,
            "window_seconds": window,
            "motions": sorted(motions),
            "window_min": window_min,
        }

    def _relative_baseline(
        self,
        rule: WarningRule,
        sample: dict[str, object],
        tick: _Tick,
        *,
        phase: str | None,
        phase_index: Mapping[int, Mapping[str, object]] | None,
    ) -> tuple[BaselineStats | None, dict[str, object] | None]:
        common = {
            "metric": rule.metric,
            "sample": sample,
            "at": tick.at,
            "lookback_days": rule.lookback_days,
            "regime_dimensions": rule.regime_dimensions,
            "minimum_trip_age_seconds": rule.minimum_running_seconds,
        }
        if rule.phase_dimension == "tire_phase":
            return (
                self._rollup_baseline(**common, phase=phase, phase_index=phase_index),
                {"tire_phase": phase},
            )
        if rule.thermal_from_metric:
            band = warning_context.transmission_band(sample.get("value"))
            if band is None:
                return None, {"thermal_band": None}
            name, low, high = band
            return (
                self._rollup_baseline(
                    **common,
                    median_range=(low, high),
                    label=f"thermal_band={name}",
                ),
                {
                    "thermal_band": name,
                    "thermal_source": rule.metric,
                    "median_range": [low, high],
                },
            )
        if rule.companion_band is not None:
            companion_metric, low, high = rule.companion_band
            return (
                self._rollup_baseline(**common, companion=rule.companion_band),
                {
                    "companion_band": {
                        "metric": companion_metric,
                        "minimum": low,
                        "maximum": high,
                        "band": warning_context.duty_band(low),
                    }
                },
            )
        return self._baseline_for(**common), None

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
        tick: _Tick | None = None,
    ) -> dict[str, object]:
        if tick is None:
            tick = _Tick(at)
        sample = self._latest(tick, definition.metric)
        if sample is None or not _numeric(sample.get("value")):
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
        if definition.absolute_minimum is not None:
            value = float(sample["value"])
            corroborating = value >= definition.absolute_minimum
            return {
                "metric": definition.metric,
                "state": "corroborating" if corroborating else "not_corroborating",
                "direction": definition.direction,
                "current": self._current_payload(sample, age),
                "absolute_minimum": definition.absolute_minimum,
                "band": (
                    warning_context.duty_band(value)
                    if definition.metric == GENERATOR_DUTY_METRIC
                    else None
                ),
                "reason": (
                    "companion reading is at or above its minimum"
                    if corroborating
                    else "companion reading is below its minimum"
                ),
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
                "baseline": _baseline_dict(baseline) if baseline is not None else None,
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
            "baseline": _baseline_dict(baseline),
            "deviation": {
                "signed_from_median": signed,
                "effect_in_rule_direction": effect,
                "threshold": threshold,
                "mad_multiplier": definition.mad_multiplier,
                "minimum_effect": definition.minimum_effect,
            },
        }

    def _relative_core(
        self,
        rule: WarningRule,
        tick: _Tick,
        *,
        refresh: bool,
    ) -> dict[str, object]:
        """Evidence for one relative rule; the caller decides the state."""

        at = tick.at
        base = self._base(rule)
        empty = {
            "baseline_regime": None,
            "regime_dimensions": self._dimension_names(rule),
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
        sample = self._latest(tick, rule.metric)
        if sample is None or not _numeric(sample.get("value")):
            return {"terminal": {
                **base,
                **empty,
                "state": "unavailable",
                "reason": "no fresh numeric observation is stored",
                "current": None,
                "regime": None,
            }}
        age = _sample_age_seconds(sample, at)
        if age is None or age > rule.max_age_seconds:
            return {"terminal": {
                **base,
                **empty,
                "state": "unavailable",
                "reason": "latest observation is undated or older than the rule permits",
                "current": None if age is None else self._current_payload(sample, age),
                "regime": sample.get("regime"),
            }, "sample": sample}
        current = self._current_payload(sample, age)
        if rule.minimum_running_seconds:
            rpm = self._latest(tick, "engine.rpm")
            interval = None
            rpm_age = _sample_age_seconds(rpm, at) if isinstance(rpm, dict) else None
            if rpm_age is not None and rpm_age <= 5 and _numeric(rpm.get("value")):
                interval = self._continuous(
                    tick,
                    rpm,
                    minimum=400.01,
                    lookback=rule.minimum_running_seconds + 30,
                )
            if interval is None or interval["duration_seconds"] < rule.minimum_running_seconds:
                return {"terminal": {
                    **base,
                    "state": "not_applicable",
                    "reason": "comparison awaits continuous running evidence",
                    "current": current,
                    "regime": sample.get("regime"),
                    "regime_dimensions": self._dimension_names(rule),
                    "baseline": None,
                    "deviation": None,
                    "applicability": {
                        "minimum_running_seconds": rule.minimum_running_seconds,
                        "running_interval": interval,
                    },
                    "persistence": {
                        "required": rule.persistence_observations,
                        "observed": 0,
                        "evaluated": False,
                    },
                }, "sample": sample}
        phase = None
        phase_index = None
        if rule.phase_dimension == "tire_phase":
            phase_index = self._phase_index(tick, trip_id=sample.get("trip_id"))
            phase = warning_context.phase_of(sample, phase_index)
            if phase is None:
                return {"terminal": {
                    **base,
                    **empty,
                    "state": "not_applicable",
                    "reason": (
                        "tire phase (cold start, warming or warm) is not established "
                        "for this reading"
                    ),
                    "current": current,
                    "regime": sample.get("regime"),
                    "phase": None,
                }, "sample": sample}
        series = self._series(
            tick, rule.metric, sample,
            lookback=_rule_lookbacks(rule).get(rule.metric, 0.0),
        )
        plausibility = self._plausibility(rule, sample=sample, series=series)
        if plausibility is not None and plausibility["rejected"]:
            return {"terminal": {
                **base,
                **empty,
                "state": "rejected",
                "reason": str(plausibility["reason"]),
                "current": current,
                "regime": sample.get("regime"),
                "plausibility": plausibility,
            }, "sample": sample}
        # Only completed buckets before the current time can train a baseline.
        # The current trip is filtered again by the baseline query.
        if refresh:
            self.historian.refresh_rollups(through=at)
        ror = (
            self._rate_of_rise(rule, sample, series)
            if rule.rate_of_rise is not None
            else None
        )
        baseline, conditioning = self._relative_baseline(
            rule, sample, tick, phase=phase, phase_index=phase_index
        )
        result: dict[str, object] = {
            "sample": sample,
            "current": current,
            "phase": phase,
            "plausibility": plausibility,
            "ror": ror,
            "baseline": baseline,
            "conditioning": conditioning,
        }
        shortfall = self._baseline_shortfall(
            baseline,
            buckets=rule.minimum_baseline_buckets,
            trips=rule.minimum_baseline_trips,
        )
        if shortfall is not None:
            result.update(kind="insufficient", shortfall=shortfall)
            return result
        assert baseline is not None
        threshold = _threshold(
            baseline,
            mad_multiplier=rule.mad_multiplier,
            minimum_effect=rule.minimum_effect,
            sigma_floor=rule.sigma_floor,
        )
        effect, signed = _effect(float(sample["value"]), baseline.median, rule.direction)
        target = self._projection(rule, sample, phase_index)
        points = [
            point
            for point in series
            if self._projection(rule, point, phase_index) == target
        ]
        newest_us = _observed_us(sample)
        floored = rule.slow_motion_floor is not None and _motion(sample) in SLOW_MOTIONS
        minimum_value = rule.slow_motion_floor if floored else None
        runs = _relative_runs(
            points,
            newest_us=newest_us if newest_us is not None else tick.at_us,
            center=baseline.median,
            direction=rule.direction,
            threshold=threshold,
            fraction=rule.deescalate_fraction,
            window_seconds=rule.persistence_window_seconds,
            minimum_value=minimum_value,
        )
        below_floor = minimum_value is not None and float(sample["value"]) < minimum_value
        if effect < threshold or below_floor:
            # The newest reading is inside the band (or below the slow-motion
            # floor): no escalation run.
            runs["escalate"] = 0
            runs["counted"] = []
        result["past"] = effect >= threshold and not below_floor
        if rule.slow_motion_floor is not None:
            result["floor"] = {
                "value": rule.slow_motion_floor,
                "applied": floored,
                "motion": _motion(sample),
            }
        corroborators = [
            self._corroborate(
                definition,
                primary_regime=str(sample["regime"]),
                at=at,
                lookback_days=rule.lookback_days,
                minimum_baseline_buckets=rule.minimum_baseline_buckets,
                minimum_baseline_trips=rule.minimum_baseline_trips,
                primary_regime_dimensions=rule.regime_dimensions,
                tick=tick,
            )
            for definition in rule.corroborators
        ]
        corroborating_count = sum(
            item["state"] == "corroborating" for item in corroborators
        )
        result.update(
            kind="evaluated",
            threshold=threshold,
            effect=effect,
            signed=signed,
            corroborators=corroborators,
            corroborating_count=corroborating_count,
            corroboration_met=corroborating_count >= rule.required_corroborators,
            **runs,
        )
        return result

    def _relative_payload(
        self,
        rule: WarningRule,
        core: Mapping[str, object],
        *,
        state: str,
        reason: str,
        confidence: str | None = None,
        trigger: str | None = None,
        candidate: dict[str, object] | None = None,
        action: str | None = None,
        sticky: bool = False,
    ) -> dict[str, object]:
        base = self._base(rule)
        sample = core["sample"]
        baseline = core.get("baseline")
        threshold = core.get("threshold")
        escalate = int(core.get("escalate") or 0)
        required = rule.persistence_observations
        corroborators = core.get("corroborators") or []
        corroborating = int(core.get("corroborating_count") or 0)
        payload: dict[str, object] = {
            **base,
            "state": state,
            "reason": reason,
            "notification_eligible": state == "warning",
            "confidence": confidence,
            "current": core["current"],
            "regime": sample["regime"],  # type: ignore[index]
            "baseline_regime": baseline.regime if isinstance(baseline, BaselineStats) else None,
            "regime_dimensions": self._dimension_names(rule),
            "baseline": _baseline_dict(baseline) if isinstance(baseline, BaselineStats) else None,
            "deviation": None if threshold is None else {
                "signed_from_median": core["signed"],
                "effect_in_rule_direction": core["effect"],
                "threshold": threshold,
                "mad_multiplier": rule.mad_multiplier,
                "minimum_effect": rule.minimum_effect,
                "sigma_floor": rule.sigma_floor,
            },
            "persistence": {
                "required": required,
                "observed": escalate,
                "window_seconds": rule.persistence_window_seconds,
                "satisfied": escalate >= required,
                "observations": _observation_rows(core.get("counted") or []),
            },
            "corroboration": {
                "required": rule.required_corroborators,
                "observed": corroborating,
                "satisfied": bool(core.get("corroboration_met")),
            },
            "corroborators": corroborators,
            "plausibility": core.get("plausibility"),
        }
        if threshold is not None:
            payload["hysteresis"] = {
                "deescalate_threshold": rule.deescalate_fraction * float(threshold),
                "deescalate_fraction": rule.deescalate_fraction,
                "clear_observed": int(core.get("clear") or 0),
                "required": required,
                "holding": sticky,
            }
        if core.get("conditioning"):
            payload["conditioning"] = core["conditioning"]
        if core.get("ror") is not None:
            payload["rate_of_rise"] = core["ror"]
        if core.get("floor") is not None:
            payload["floor"] = core["floor"]
        if core.get("phase") is not None:
            payload["phase"] = core["phase"]
        if trigger is not None:
            payload["trigger"] = trigger
        if candidate is not None:
            payload["candidate"] = candidate
        if action is not None:
            payload["action"] = action
        return payload

    def _evaluate_relative_rule(
        self,
        rule: WarningRule,
        tick: _Tick,
        *,
        refresh: bool,
    ) -> dict[str, object]:
        core = self._relative_core(rule, tick, refresh=refresh)
        if "terminal" in core:
            return core["terminal"]  # type: ignore[return-value]
        open_info = self._open_info(tick, rule.key)
        sticky = open_info.get("state") == "warning"
        ror = core.get("ror")
        ror_fired = isinstance(ror, Mapping) and bool(ror.get("triggered"))
        required = rule.persistence_observations
        if core["kind"] == "insufficient":
            if not ror_fired:
                payload = self._relative_payload(
                    rule,
                    core,
                    state="insufficient_history",
                    reason=str(core["shortfall"]),
                )
                payload["deviation"] = None
                payload.pop("hysteresis", None)
                return payload
            self._drop_candidate(rule.key)
            return self._relative_payload(
                rule,
                core,
                state="warning",
                reason="rising quickly with no fan cycle while stopped or in slow traffic",
                confidence="medium",
                trigger="rate_of_rise",
                action=COOLANT_RATE_ACTION,
            )
        escalate = int(core["escalate"])
        clear = int(core["clear"])
        past = bool(core["past"])
        trigger = None
        action = None
        if ror_fired:
            state = "warning"
            trigger = "rate_of_rise"
            action = COOLANT_RATE_ACTION
            reason = "rising quickly with no fan cycle while stopped or in slow traffic"
        elif sticky:
            if clear >= required:
                state = "normal"
                reason = "back inside its usual range long enough to clear"
            elif clear >= 1 and not core.get("clear_broken"):
                # Every same-conditions reading available (new trip, new
                # speed/rpm band) is inside the lower bar, only fewer than N.
                # Not a warning at a normal value; the event stays open.
                state = "recovering"
                reason = "back inside its usual range; confirming over more readings"
            else:
                state = "warning"
                trigger = "deviation" if escalate >= required else "hysteresis"
                reason = (
                    "stayed outside its usual range long enough to confirm"
                    if escalate >= required
                    else "has not settled back inside its usual range yet"
                )
        elif escalate >= required and core["corroboration_met"]:
            state = "warning"
            trigger = "deviation"
            reason = "stayed outside its usual range long enough to confirm"
            if core["corroborating_count"]:
                reason += " with independent corroboration"
        else:
            state = "normal"
            if not past:
                reason = "inside its usual range"
            elif escalate >= required:
                reason = "outside its usual range without the required corroboration"
            else:
                reason = "outside its usual range; waiting to confirm"
        candidate = None
        confidence = None
        if state == "warning":
            self._drop_candidate(rule.key)
            confidence = "high" if core["corroborating_count"] else "medium"
        elif past:
            counted = core.get("counted") or []
            first = counted[-1].get("observed_at") if counted else None
            candidate = self._candidate(
                rule.key,
                at_us=tick.at_us,
                first_seen_at=first if isinstance(first, str) else None,
                observed=escalate,
                required=required,
                window_seconds=rule.persistence_window_seconds,
            )
            confidence = "low"
        return self._relative_payload(
            rule,
            core,
            state=state,
            reason=reason,
            confidence=confidence,
            trigger=trigger,
            candidate=candidate,
            action=action,
            sticky=sticky and state == "warning",
        )

    # ------------------------------------------------------------------
    # Tier 1: combined tires

    @staticmethod
    def _ratio(core: Mapping[str, object]) -> float:
        threshold = float(core["threshold"])
        effect = float(core["effect"])
        return effect / threshold if threshold > 0 else effect

    @staticmethod
    def _compact_wheel(core: Mapping[str, object], state: str | None = None) -> dict[str, object]:
        if "terminal" in core:
            terminal = core["terminal"]
            current = terminal.get("current") or None
            return {
                "state": terminal.get("state"),
                "reason": terminal.get("reason"),
                "phase": terminal.get("phase"),
                "current": None if not current else {
                    key: current.get(key)
                    for key in ("sample_id", "value", "unit", "observed_at")
                },
            }
        current = core["current"]
        baseline = core.get("baseline")
        compact: dict[str, object] = {
            "state": state or ("insufficient_history" if core["kind"] == "insufficient" else "normal"),
            "phase": core.get("phase"),
            "current": {
                key: current.get(key)  # type: ignore[union-attr]
                for key in ("sample_id", "value", "unit", "observed_at")
            },
            "baseline": None if not isinstance(baseline, BaselineStats) else {
                "median": baseline.median,
                "mad": baseline.mad,
                "robust_sigma": baseline.robust_sigma,
                "bucket_count": baseline.bucket_count,
                "trip_count": baseline.trip_count,
                "regime": baseline.regime,
                "input_digest": baseline.input_digest,
            },
        }
        if core["kind"] == "insufficient":
            compact["reason"] = core.get("shortfall")
            return compact
        compact.update(
            deviation={
                "signed_from_median": core["signed"],
                "effect_in_rule_direction": core["effect"],
                "threshold": core["threshold"],
            },
            persistence={
                "observed": core["escalate"],
                "satisfied": None,
            },
            hysteresis={"clear_observed": core["clear"]},
        )
        return compact

    @staticmethod
    def _inconclusive_state(results: Mapping[str, Mapping[str, object]]) -> tuple[str, str | None]:
        states: dict[str, str] = {}
        for wheel, core in results.items():
            if "terminal" in core:
                state = str(core["terminal"].get("state"))
            elif core.get("kind") == "insufficient":
                state = "insufficient_history"
            else:
                continue
            states.setdefault(state, wheel)
        for state in INCONCLUSIVE_PRIORITY:
            if state in states:
                return state, states[state]
        return "unavailable", None

    def _evaluate_tire_group(
        self,
        rule: TireGroupRule,
        tick: _Tick,
        *,
        refresh: bool,
    ) -> dict[str, object]:
        if refresh:
            self.historian.refresh_rollups(through=tick.at)
        group_base = self._tire_base(rule)
        open_info = self._open_info(tick, rule.key)
        sticky = open_info.get("state") == "warning"
        prior = set(open_info.get("held") or ())
        required = rule.persistence_observations
        results = {
            wheel: self._relative_core(rule.wheel_rule(wheel), tick, refresh=False)
            for wheel in rule.wheels
        }
        evaluated = {
            wheel: core for wheel, core in results.items()
            if core.get("kind") == "evaluated"
        }
        by_ratio = sorted(evaluated, key=lambda wheel: -self._ratio(evaluated[wheel]))
        persistent = [wheel for wheel in by_ratio if int(evaluated[wheel]["escalate"]) >= required]
        past = [
            wheel for wheel in by_ratio
            if float(evaluated[wheel]["effect"]) >= float(evaluated[wheel]["threshold"])
        ]
        # A wheel holds the warning only while a reading outside the lower bar
        # breaks its clear run.  A held wheel whose readings are all inside the
        # bar, only fewer than N, is recovering, not a warning at a normal value.
        holding = [
            wheel for wheel in by_ratio
            if int(evaluated[wheel]["clear"]) < required
            and (
                bool(evaluated[wheel]["clear_broken"])
                or (wheel in prior and int(evaluated[wheel]["clear"]) == 0)
            )
        ]
        prior_order = [wheel for wheel in rule.wheels if wheel in prior]
        recovering_prior = [
            wheel for wheel in prior_order
            if wheel in evaluated
            and 1 <= int(evaluated[wheel]["clear"]) < required
            and not bool(evaluated[wheel]["clear_broken"])
        ]
        cleared_prior = [
            wheel for wheel in prior_order
            if wheel in evaluated and int(evaluated[wheel]["clear"]) >= required
        ]
        evaluated_prior = [wheel for wheel in prior_order if wheel in evaluated]
        inconclusive_prior = [
            wheel for wheel in rule.wheels
            if wheel in prior and wheel not in evaluated
        ]
        named: list[str] = []
        if sticky and (persistent or holding):
            state = "warning"
            named = [wheel for wheel in by_ratio if wheel in persistent or wheel in holding]
            reason = (
                "stayed below its usual pressure for this trip phase long enough to confirm"
                if persistent
                else "has not settled back inside its usual pressure range yet"
            )
        elif sticky and cleared_prior and len(cleared_prior) == len(evaluated_prior):
            state = "normal"
            reason = "back inside its usual pressure range long enough to clear"
        elif sticky and recovering_prior:
            state = "recovering"
            reason = "back inside its usual pressure range; confirming over more readings"
        elif sticky and inconclusive_prior:
            state, _wheel = self._inconclusive_state(
                {wheel: results[wheel] for wheel in inconclusive_prior}
            )
            reason = "the tire that raised the warning cannot be compared right now"
        elif not sticky and persistent:
            state = "warning"
            named = persistent
            reason = "stayed below its usual pressure for this trip phase long enough to confirm"
        elif evaluated:
            state = "normal"
            reason = (
                "below its usual pressure for this trip phase; waiting to confirm"
                if past
                else "every compared tire is inside its usual range"
            )
        else:
            state, _wheel = self._inconclusive_state(results)
            reason = "no tire could be compared with its usual pressure"

        worst: str | None
        opened = open_info.get("opened") or {}
        opened_wheel = opened.get("wheel") if isinstance(opened, Mapping) else None
        if named:
            worst = named[0]
        elif sticky and opened_wheel in rule.wheels and results[opened_wheel].get("sample") is not None:
            # Keep describing the wheel that opened the event so the timed
            # recovery gate compares the same sensor (per-wheel sources differ).
            worst = opened_wheel
        elif sticky and prior_order:
            worst = prior_order[0]
        elif state in INCONCLUSIVE_PRIORITY:
            _state, worst = self._inconclusive_state(
                {wheel: results[wheel] for wheel in (inconclusive_prior or rule.wheels)}
            )
        elif past:
            worst = past[0]
        elif by_ratio:
            worst = by_ratio[0]
        else:
            worst = None
        if worst is None:
            worst = next(
                (wheel for wheel in rule.wheels if results[wheel].get("sample") is not None),
                None,
            )

        wheel_states = {
            wheel: ("warning" if wheel in named else None) for wheel in rule.wheels
        }
        wheel_assessments = {
            wheel: self._compact_wheel(results[wheel], wheel_states[wheel])
            for wheel in rule.wheels
        }
        for wheel in evaluated:
            wheel_assessments[wheel]["persistence"]["satisfied"] = (  # type: ignore[index]
                int(evaluated[wheel]["escalate"]) >= required
            )
        action_wheels = named or (past if state == "normal" else [])
        candidate = None
        confidence = None
        if state == "warning":
            self._drop_candidate(rule.key)
            confidence = "medium"
        elif state == "normal" and past:
            core = evaluated[past[0]]
            counted = core.get("counted") or []
            first = counted[-1].get("observed_at") if counted else None
            candidate = self._candidate(
                rule.key,
                at_us=tick.at_us,
                first_seen_at=first if isinstance(first, str) else None,
                observed=max(int(evaluated[wheel]["escalate"]) for wheel in past),
                required=required,
                window_seconds=rule.persistence_window_seconds,
            )
            confidence = "low"

        if worst is not None and results[worst].get("kind") in ("evaluated", "insufficient"):
            core = results[worst]
            wheel_rule = rule.wheel_rule(worst)
            payload = self._relative_payload(
                wheel_rule,
                core,
                state=state,
                reason=reason,
                confidence=confidence,
                trigger=None if state != "warning" else (
                    "deviation" if persistent else "hysteresis"
                ),
                candidate=candidate,
                sticky=sticky and state == "warning",
            )
            if core.get("kind") == "insufficient":
                payload["deviation"] = None
                payload.pop("hysteresis", None)
                if state == "insufficient_history":
                    payload["reason"] = str(core["shortfall"])
        elif worst is not None and "terminal" in results[worst]:
            payload = dict(results[worst]["terminal"])  # type: ignore[arg-type]
            payload.update(state=state, reason=reason, notification_eligible=False)
        else:
            payload = {
                "state": state,
                "reason": reason,
                "notification_eligible": False,
                "current": None,
                "regime": None,
                "baseline_regime": None,
                "baseline": None,
                "deviation": None,
                "persistence": {
                    "required": required,
                    "observed": 0,
                    "window_seconds": rule.persistence_window_seconds,
                },
                "corroboration": {"required": 0, "observed": 0, "satisfied": False},
                "corroborators": [],
            }
        payload.update({
            key: value for key, value in group_base.items()
            if key not in ("metric", "confidence", "notification_eligible")
        })
        # Name a wheel only when that wheel has a reading to describe.
        payload["metric"] = (
            f"{rule.metric}.{worst}"
            if worst is not None and results[worst].get("sample") is not None
            else rule.metric
        )
        payload["state"] = state
        payload["notification_eligible"] = state == "warning"
        payload["confidence"] = confidence
        payload["regime_dimensions"] = [*rule.regime_dimensions, rule.phase_dimension]
        payload["wheel"] = worst
        payload["phase"] = (
            results[worst].get("phase")
            if worst is not None
            else None
        )
        payload["wheels_past_threshold"] = past
        payload["action_wheels"] = action_wheels
        payload["held_keys"] = named if state == "warning" else (prior_order if sticky else [])
        payload["wheel_assessments"] = wheel_assessments
        if action_wheels:
            labels = _labels(action_wheels)
            payload["action"] = f"Check the {labels} tire pressure at the next stop."
            payload["title"] = f"{labels} tire{'s' if len(action_wheels) > 1 else ''} low"
        if candidate is not None:
            payload["candidate"] = candidate
        else:
            payload.pop("candidate", None)
        return payload

    # ------------------------------------------------------------------
    # Tier 0: absolute rules

    def _running_gate(self, tick: _Tick) -> dict[str, object]:
        """Fresh qualified RPM >= 400 continuously for at least 10 s."""

        key = ("running_gate",)
        if key in tick.conditions:
            return tick.conditions[key]  # type: ignore[return-value]
        rpm = self._latest(tick, "engine.rpm")
        rpm_age = (
            _sample_age_seconds(rpm, tick.at)
            if isinstance(rpm, dict) and _numeric(rpm.get("value"))
            else None
        )
        result: dict[str, object]
        if rpm is None or rpm_age is None or rpm_age > RPM_MAX_AGE_SECONDS:
            result = {
                "ok": False,
                "state": "not_applicable",
                "reason": "engine running is not established by a fresh RPM reading",
                "evidence": {"qualified": False, "rpm": None},
            }
        else:
            rpm_payload = self._current_payload(rpm, rpm_age)
            if rpm.get("quality") not in QUALIFIED_QUALITIES:
                result = {
                    "ok": False,
                    "state": "not_applicable",
                    "reason": "RPM reading is not from a qualified decode",
                    "evidence": {"qualified": False, "rpm": rpm_payload},
                }
            elif float(rpm["value"]) < RUNNING_RPM:
                result = {
                    "ok": False,
                    "state": "not_applicable",
                    "reason": "engine is not running",
                    "evidence": {
                        "qualified": False,
                        "rpm": rpm_payload,
                        "minimum_rpm": RUNNING_RPM,
                    },
                }
            else:
                interval = self._continuous(
                    tick, rpm, minimum=RUNNING_RPM, lookback=RUNNING_LOOKBACK_SECONDS
                )
                if (
                    interval is None
                    or float(interval["duration_seconds"]) < RUNNING_GRACE_SECONDS
                ):
                    result = {
                        "ok": False,
                        "state": "not_applicable",
                        "reason": "engine is inside the startup grace period",
                        "evidence": {
                            "qualified": False,
                            "rpm": rpm_payload,
                            "continuous_interval": interval,
                            "startup_grace_seconds": RUNNING_GRACE_SECONDS,
                        },
                    }
                else:
                    result = {
                        "ok": True,
                        "rpm": rpm,
                        "start_us": _to_us(_utc(str(interval["started_at"]))),
                        "evidence": {
                            "qualified": True,
                            "rpm": rpm_payload,
                            "continuous_interval": interval,
                            "startup_grace_seconds": RUNNING_GRACE_SECONDS,
                        },
                    }
        tick.conditions[key] = result
        return result

    def _absolute_gate(
        self,
        rule: AbsoluteRule,
        tick: _Tick,
        sample: dict[str, object],
    ) -> dict[str, object]:
        """Gate for the newest sample plus a per-sample ``accept`` function."""

        warning = rule.warning_threshold
        critical = rule.critical_threshold
        if warning is None and critical is not None:
            warning = critical
        lookback = _rule_lookbacks(rule).get(rule.metric, 120.0)
        if rule.gate == "parked":
            rpm = self._latest(tick, "engine.rpm")
            rpm_age = (
                _sample_age_seconds(rpm, tick.at)
                if isinstance(rpm, dict) and _numeric(rpm.get("value"))
                else None
            )
            trip = self._last_trip(tick)
            last_active_us = None
            if isinstance(trip, Mapping) and isinstance(trip.get("last_active_at"), str):
                last_active_us = _to_us(_utc(str(trip["last_active_at"])))
            evidence = {
                "rpm": None if rpm is None or rpm_age is None else self._current_payload(rpm, rpm_age),
                "last_trip_id": trip.get("id") if isinstance(trip, Mapping) else None,
                "last_active_at": trip.get("last_active_at") if isinstance(trip, Mapping) else None,
                "settle_seconds": PARKED_SETTLE_SECONDS,
                "engine_states": sorted(PARKED_ENGINE_STATES),
            }
            if (
                rpm_age is not None
                and rpm_age <= PARKED_SETTLE_SECONDS
                and float(rpm["value"]) >= RUNNING_RPM  # type: ignore[index]
            ):
                return {"ok": False, "state": "not_applicable",
                        "reason": "engine is running", "evidence": evidence}
            if _engine(sample) not in PARKED_ENGINE_STATES:
                return {"ok": False, "state": "not_applicable",
                        "reason": "reading was not taken with the engine off", "evidence": evidence}
            captured = _captured_us(sample)
            if (
                last_active_us is not None
                and captured is not None
                and captured - last_active_us < PARKED_SETTLE_SECONDS * 1_000_000
            ):
                return {"ok": False, "state": "not_applicable",
                        "reason": "battery reading is still settling after the last drive",
                        "evidence": evidence}

            def accept_parked(point: Mapping[str, object]) -> tuple:
                point_captured = _captured_us(point)
                ok = (
                    _engine(point) in PARKED_ENGINE_STATES
                    and point_captured is not None
                    and (
                        last_active_us is None
                        or point_captured - last_active_us >= PARKED_SETTLE_SECONDS * 1_000_000
                    )
                )
                return ("ok" if ok else "fail", warning, critical, float(point["value"]))

            return {"ok": True, "accept": accept_parked, "evidence": evidence}

        running = self._running_gate(tick)
        if not running["ok"]:
            return {
                "ok": False,
                "state": running["state"],
                "reason": running["reason"],
                "evidence": running["evidence"],
                "running_evidence": running["evidence"],
            }
        start_us = int(running["start_us"])
        rpm_now = float(running["rpm"]["value"])  # type: ignore[index]
        evidence: dict[str, object] = {"running": running["evidence"]}
        if rule.gate == "warm_running":
            coolant = self._latest(tick, "engine.coolant_temperature")
            coolant_age = (
                _sample_age_seconds(coolant, tick.at)
                if isinstance(coolant, dict) and _numeric(coolant.get("value"))
                else None
            )
            evidence["coolant"] = (
                None if coolant is None or coolant_age is None
                else self._current_payload(coolant, coolant_age)
            )
            evidence["minimum_coolant"] = WARM_COOLANT_F
            if (
                coolant_age is None
                or coolant_age > RPM_MAX_AGE_SECONDS
                or float(coolant["value"]) < WARM_COOLANT_F  # type: ignore[index]
            ):
                return {"ok": False, "state": "not_applicable",
                        "reason": "limits apply once the engine is warm",
                        "evidence": evidence, "running_evidence": running["evidence"]}
        rpm_values: list[tuple[int, float]] = []
        if rule.bands or rule.gate == "running_rpm_min":
            rpm_values = self._companion_values(
                tick, "engine.rpm", sample, lookback=lookback + RPM_MAX_AGE_SECONDS
            )
        if rule.bands and _band_floor(rpm_now, rule.bands) is None:
            evidence["bands"] = [list(band) for band in rule.bands]
            return {"ok": False, "state": "not_applicable",
                    "reason": "engine speed is outside every pressure band",
                    "evidence": evidence, "running_evidence": running["evidence"]}
        minimum_rpm = float(rule.gate_params[0]) if rule.gate == "running_rpm_min" else None
        if minimum_rpm is not None:
            evidence["minimum_rpm"] = minimum_rpm
            if rpm_now < minimum_rpm:
                return {"ok": False, "state": "not_applicable",
                        "reason": "charging check applies above its minimum engine speed",
                        "evidence": evidence, "running_evidence": running["evidence"]}
        companion_values: list[tuple[int, float]] = []
        if rule.companion is not None:
            companion_metric, companion_minimum, companion_age = rule.companion
            companion_values = self._companion_values(
                tick,
                str(companion_metric),
                sample,
                lookback=lookback + float(companion_age),
            )
            evidence["companion"] = {
                "metric": companion_metric,
                "minimum": companion_minimum,
                "max_age_seconds": companion_age,
                "current": companion_values[0][1] if companion_values else None,
            }

        def accept_running(point: Mapping[str, object]) -> tuple:
            point_us = _observed_us(point)
            value = float(point["value"])
            if point_us is None:
                return ("fail", warning, critical, value)
            status = "ok" if point_us - start_us >= RUNNING_GRACE_SECONDS * 1_000_000 else "fail"
            threshold = warning
            if rule.bands or minimum_rpm is not None:
                rpm = _companion_at(rpm_values, point_us, RPM_MAX_AGE_SECONDS)
                if rpm is None:
                    return ("gap", None if rule.bands else threshold, critical, value)
                if rule.bands:
                    threshold = _band_floor(rpm, rule.bands)
                    if threshold is None:
                        return ("skip", None, None, value)
                if minimum_rpm is not None and rpm < minimum_rpm:
                    status = "fail"
            if rule.companion is not None and status != "fail":
                companion = _companion_at(
                    companion_values, point_us, float(rule.companion[2])
                )
                if companion is None:
                    status = "gap"
                elif companion < float(rule.companion[1]):
                    status = "fail"
            return (status, threshold, critical, value)

        return {
            "ok": True,
            "accept": accept_running,
            "evidence": evidence,
            "running_evidence": running["evidence"],
        }

    def _absolute_state(
        self,
        rule: AbsoluteRule,
        tick: _Tick,
        runs: Mapping[str, object],
        *,
        key: str | None = None,
    ) -> tuple[str, str]:
        """Escalation with clear-margin hysteresis; returns (state, severity)."""

        open_info = self._open_info(tick, key or rule.key)
        required = rule.persistence_observations
        critical_hit = int(runs["critical"]) >= required
        if open_info.get("state") == "warning":
            if int(runs["clear"]) >= required:
                return "normal", "warning"
            if int(runs["clear"]) >= 1 and not runs.get("clear_contradicted", True):
                # Every evaluable reading since the gate was last met (a new
                # trip, a parked wake) is past the clear margin, only fewer
                # than N so far.  Re-asserting "warning" here would show a
                # confirmed warning at a normal value and refresh the event's
                # last-observed time at every wake, so the 24 h parked closure
                # could never run.  "recovering" keeps the event open instead.
                return "recovering", "warning"
            critical = critical_hit or open_info.get("severity") == "critical"
            return "warning", "critical" if critical else "warning"
        if int(runs["escalate"]) >= required:
            critical = critical_hit or (
                rule.warning_threshold is None and rule.critical_threshold is not None
                and not rule.bands and not rule.wheel_thresholds
            )
            return "warning", "critical" if critical else "warning"
        return "normal", "warning"

    def _evaluate_absolute_rule(
        self,
        rule: AbsoluteRule,
        tick: _Tick,
        *,
        refresh: bool,
    ) -> dict[str, object]:
        if rule.gate == "tire_cold_phase":
            return self._evaluate_tire_absolute(rule, tick)
        if rule.gate == "tire_pair":
            return self._evaluate_tire_pair(rule, tick, refresh=refresh)
        return self._carry_open_severity(
            tick,
            rule.key,
            self._evaluate_plain_absolute(rule, tick),
            action_critical=rule.action_critical,
        )

    CARRY_SEVERITY_STATES = frozenset(
        ("unavailable", "not_applicable", "recovering", "insufficient_history", "rejected")
    )

    def _carry_open_severity(
        self,
        tick: _Tick,
        key: str,
        payload: dict[str, object],
        *,
        action_critical: str | None = None,
    ) -> dict[str, object]:
        """Keep an open critical critical across inconclusive/recovering ticks.

        A gap in the running evidence or a stale sample must not quietly
        downgrade the open event's severity; the next confirming tick then
        reports critical immediately because the open info still says so.
        """

        if payload.get("state") not in self.CARRY_SEVERITY_STATES:
            return payload
        open_info = self._open_info(tick, key)
        if open_info.get("state") == "warning" and open_info.get("severity") == "critical":
            payload["severity"] = "critical"
            if action_critical:
                payload["action"] = action_critical
        return payload

    def _evaluate_plain_absolute(self, rule: AbsoluteRule, tick: _Tick) -> dict[str, object]:
        base = self._absolute_rule_base(rule)
        required = rule.persistence_observations
        zero = {
            "required": required,
            "observed": 0,
            "window_seconds": rule.persistence_window_seconds,
        }
        sample = self._latest(tick, rule.metric)
        if sample is None or not _numeric(sample.get("value")):
            return {**base, "state": "unavailable",
                    "reason": "no fresh numeric observation is stored",
                    "current": None, "regime": None, "persistence": zero}
        age = _sample_age_seconds(sample, tick.at)
        if age is None or age > rule.max_age_seconds:
            return {**base, "state": "unavailable",
                    "reason": "latest observation is undated or older than the rule permits",
                    "current": None if age is None else self._current_payload(sample, age),
                    "regime": sample.get("regime"), "persistence": zero}
        current = self._current_payload(sample, age)
        if sample.get("unit") != rule.unit or sample.get("quality") not in rule.qualities:
            return {**base, "state": "unavailable",
                    "reason": "reading is not from a qualified decode in the rule's unit",
                    "current": current, "regime": sample.get("regime"),
                    "persistence": zero}
        gate = self._absolute_gate(rule, tick, sample)
        if not gate["ok"]:
            payload = {**base, "state": gate["state"], "reason": gate["reason"],
                       "current": current, "regime": sample.get("regime"),
                       "gate": gate["evidence"], "persistence": zero}
            if gate.get("running_evidence") is not None:
                payload["running_evidence"] = gate["running_evidence"]
            return payload
        accept = gate["accept"]
        series = self._series(
            tick, rule.metric, sample,
            lookback=_rule_lookbacks(rule).get(rule.metric, 0.0),
        )
        newest_us = _observed_us(sample)
        runs = _absolute_runs(
            series,
            newest_us=newest_us if newest_us is not None else tick.at_us,
            accept=accept,  # type: ignore[arg-type]
            direction=rule.direction,
            window_seconds=rule.persistence_window_seconds,
            clear_margin=rule.clear_margin,
        )
        newest_status, newest_threshold, newest_critical, newest_value = accept(sample)  # type: ignore[operator]
        state, severity = self._absolute_state(rule, tick, runs)
        qualifies = int(runs["escalate"]) >= 1
        candidate = None
        confidence = None
        if state == "warning":
            self._drop_candidate(rule.key)
            confidence = "high" if rule.second_source else "medium"
            reason = (
                "held past its limit long enough to confirm"
                if int(runs["escalate"]) >= required
                else "has not come back past its limit by the clear margin yet"
            )
        elif qualifies:
            counted = runs["counted"]
            first = counted[-1].get("observed_at") if counted else None  # type: ignore[index]
            candidate = self._candidate(
                rule.key,
                at_us=tick.at_us,
                first_seen_at=first if isinstance(first, str) else None,
                observed=int(runs["escalate"]),
                required=required,
                window_seconds=rule.persistence_window_seconds,
            )
            confidence = "low"
            reason = "past its limit; waiting to confirm"
        elif state == "recovering":
            reason = "back past its limit by the clear margin; confirming over more readings"
        else:
            reason = "inside its limit"
        threshold_value = newest_threshold if newest_threshold is not None else base[
            "absolute_threshold"
        ]["value"]  # type: ignore[index]
        payload: dict[str, object] = {
            **base,
            "state": state,
            "reason": reason,
            "notification_eligible": state == "warning",
            "severity": severity,
            "action": (
                rule.action_critical
                if severity == "critical" and rule.action_critical
                else rule.action
            ),
            "confidence": confidence,
            "current": current,
            "regime": sample.get("regime"),
            "gate": gate["evidence"],
            "absolute_threshold": {
                "operator": "below" if rule.direction == "low" else "above",
                "value": threshold_value,
                "unit": rule.unit,
            },
            "persistence": {
                "required": required,
                "observed": int(runs["escalate"]),
                "window_seconds": rule.persistence_window_seconds,
                "satisfied": int(runs["escalate"]) >= required,
                "critical_observed": int(runs["critical"]),
                "companion_gaps": int(runs["companion_gaps"]),
                "observations": _observation_rows(runs["counted"]),  # type: ignore[arg-type]
            },
            "hysteresis": {
                "clear_margin": rule.clear_margin,
                "clear_threshold": (
                    None if threshold_value is None
                    else threshold_value + rule.clear_margin
                    if rule.direction == "low"
                    else threshold_value - rule.clear_margin
                ),
                "clear_observed": int(runs["clear"]),
                "required": required,
            },
        }
        if rule.critical_threshold is not None:
            payload["critical_threshold"] = {
                "operator": "below" if rule.direction == "low" else "above",
                "value": rule.critical_threshold,
                "unit": rule.unit,
            }
        if gate.get("running_evidence") is not None:
            payload["running_evidence"] = gate["running_evidence"]
        if candidate is not None:
            payload["candidate"] = candidate
        return payload

    def _wheel_absolute(
        self,
        rule: AbsoluteRule,
        tick: _Tick,
        wheel: str,
        warning: float,
        critical: float | None,
    ) -> dict[str, object]:
        metric = f"{rule.metric}.{wheel}"
        sample = self._latest(tick, metric)
        if sample is None or not _numeric(sample.get("value")):
            return {"state": "unavailable", "reason": "no fresh numeric observation is stored"}
        age = _sample_age_seconds(sample, tick.at)
        if age is None or age > rule.max_age_seconds:
            return {"state": "unavailable",
                    "reason": "latest observation is undated or older than the rule permits",
                    "sample": sample}
        current = self._current_payload(sample, age)
        if sample.get("unit") != rule.unit or sample.get("quality") not in rule.qualities:
            return {"state": "unavailable",
                    "reason": "reading is not from a qualified decode in the rule's unit",
                    "sample": sample, "current": current}
        phase_index = self._phase_index(tick, trip_id=sample.get("trip_id"))
        phase = warning_context.phase_of(sample, phase_index)
        # The limits are cold-tire values.  A warm tire reads higher than the
        # same tire cold, so a reading under the cold limit in any phase (or
        # with no established phase: a trip that starts < 4 h after the last,
        # or a puncture mid-trip) is at least as low.  Checking every phase
        # adds no false warning over the cold-phase check and keeps a flat
        # tire from going unreported until the next cold start.

        # A warm reading cannot show that a tire low when cold was refilled:
        # it clears only past the warm allowance (warm_clear_fraction).
        warm_clear = (
            (warning + rule.clear_margin) * (1.0 + rule.warm_clear_fraction)
            - rule.clear_margin
        )

        def accept(point: Mapping[str, object]) -> tuple:
            value = float(point["value"])
            if rule.warm_clear_fraction > 0 and warning_context.phase_of(
                point, phase_index
            ) in ("warming", "warm"):
                return ("ok", warning, critical, value, warm_clear)
            return ("ok", warning, critical, value)

        newest_us = _observed_us(sample)
        runs = _absolute_runs(
            self._series(tick, metric, sample, lookback=_rule_lookbacks(rule).get(metric, 0.0)),
            newest_us=newest_us if newest_us is not None else tick.at_us,
            accept=accept,
            direction=rule.direction,
            window_seconds=rule.persistence_window_seconds,
            clear_margin=rule.clear_margin,
        )
        return {
            "state": "evaluated",
            "sample": sample,
            "current": current,
            "phase": phase,
            "warning": warning,
            "critical": critical,
            "value": float(sample["value"]),
            "past": _past(float(sample["value"]), warning, rule.direction),
            **runs,
            "runs": runs,
        }

    def _group_absolute(
        self,
        rule: AbsoluteRule,
        tick: _Tick,
        results: Mapping[str, Mapping[str, object]],
        *,
        severity_key: Callable[[Mapping[str, object]], float],
        clear_required: int | None = None,
    ) -> dict[str, object]:
        """Shared wheel/axle aggregation for the tire tier-0 rules.

        ``clear_required`` is the clear-run length that ends a held warning.
        It defaults to the escalation run length.
        """

        open_info = self._open_info(tick, rule.key)
        sticky = open_info.get("state") == "warning"
        prior = set(open_info.get("held") or ())
        required = rule.persistence_observations
        clear_required = clear_required or required
        evaluated = {
            name: result for name, result in results.items()
            if result.get("state") == "evaluated"
        }
        order = sorted(evaluated, key=lambda name: -severity_key(evaluated[name]))
        persistent = [name for name in order if int(evaluated[name]["escalate"]) >= required]
        critical = [name for name in order if int(evaluated[name]["critical"]) >= required]
        past = [name for name in order if evaluated[name]["past"]]
        # Same ladder as the combined tire rule: a held wheel/axle keeps the
        # warning only while a reading short of the clear margin breaks its
        # clear run; all-clear but fewer than N readings is recovering.
        # ``clear_contradicted``: an evaluable reading short of the clear
        # margin ended the clear run (a gate-unmet or companion-gap reading
        # only interrupts it, as in _absolute_state).
        holding = [
            name for name in order
            if int(evaluated[name]["clear"]) < clear_required
            and (
                bool(evaluated[name]["clear_contradicted"])
                or (name in prior and int(evaluated[name]["clear"]) == 0)
            )
        ]
        prior_order = [name for name in results if name in prior]
        recovering_prior = [
            name for name in prior_order
            if name in evaluated
            and 1 <= int(evaluated[name]["clear"]) < clear_required
            and not bool(evaluated[name]["clear_contradicted"])
        ]
        cleared_prior = [
            name for name in prior_order
            if name in evaluated and int(evaluated[name]["clear"]) >= clear_required
        ]
        evaluated_prior = [name for name in prior_order if name in evaluated]
        inconclusive_prior = [
            name for name in results if name in prior and name not in evaluated
        ]
        named: list[str] = []
        if sticky and (persistent or holding):
            state = "warning"
            named = [name for name in order if name in persistent or name in holding]
        elif sticky and cleared_prior and len(cleared_prior) == len(evaluated_prior):
            state = "normal"
        elif sticky and recovering_prior:
            state = "recovering"
        elif sticky and inconclusive_prior:
            state = str(results[inconclusive_prior[0]].get("state"))
        elif not sticky and persistent:
            state = "warning"
            named = persistent
        elif evaluated:
            state = "normal"
        else:
            states = [str(result.get("state")) for result in results.values()]
            state = next(
                (item for item in INCONCLUSIVE_PRIORITY if item in states),
                "unavailable",
            )
        severity = "warning"
        open_critical = open_info.get("severity") == "critical"
        if state == "warning" and (critical or open_critical):
            severity = "critical"
        elif sticky and open_critical and (
            state in INCONCLUSIVE_PRIORITY or state == "recovering"
        ):
            # An open critical stays critical across inconclusive ticks.
            severity = "critical"
        opened = open_info.get("opened") or {}
        opened = opened if isinstance(opened, Mapping) else {}
        if opened.get("left") and opened.get("right"):
            opened_key = f"{opened['left']}{opened['right']}"
        else:
            opened_key = opened.get("wheel")
        if named:
            worst = named[0]
        elif sticky and opened_key in results and (
            results[opened_key].get("sample") is not None
        ):
            # Keep describing what opened the event so the timed recovery
            # gate compares the same sensor (per-wheel sources differ).
            worst = opened_key
        elif sticky and prior_order:
            worst = prior_order[0]
        elif state in INCONCLUSIVE_PRIORITY:
            pool = inconclusive_prior or list(results)
            worst = next(
                (name for name in pool if results[name].get("state") == state),
                pool[0] if pool else None,
            )
        elif past:
            worst = past[0]
        elif order:
            worst = order[0]
        else:
            worst = next(iter(results), None)
        return {
            "state": state,
            "severity": severity,
            "named": named,
            "held": named if state == "warning" else (prior_order if sticky else []),
            "sticky": sticky,
            "opened": opened,
            "past": past,
            "worst": worst,
            "persistent": persistent,
            "evaluated": evaluated,
        }

    def _evaluate_tire_absolute(self, rule: AbsoluteRule, tick: _Tick) -> dict[str, object]:
        base = self._absolute_rule_base(rule)
        required = rule.persistence_observations
        thresholds = {wheel: (warning, critical) for wheel, warning, critical in rule.wheel_thresholds}
        results = {
            wheel: self._wheel_absolute(rule, tick, wheel, warning, critical)
            for wheel, (warning, critical) in thresholds.items()
        }
        grouped = self._group_absolute(
            rule,
            tick,
            results,
            severity_key=lambda result: (
                float(result["warning"]) - float(result["value"])
            ),
        )
        state = grouped["state"]
        worst = grouped["worst"]
        named = grouped["named"]
        past = grouped["past"]
        result = results.get(worst) if worst is not None else None
        runs = result.get("runs") if isinstance(result, Mapping) else None
        candidate = None
        confidence = None
        if state == "warning":
            self._drop_candidate(rule.key)
            confidence = "medium"
            reason = "held below its cold-tire limit long enough to confirm"
        elif state == "normal" and past:
            core = grouped["evaluated"][past[0]]
            counted = core.get("counted") or []
            first = counted[-1].get("observed_at") if counted else None
            candidate = self._candidate(
                rule.key,
                at_us=tick.at_us,
                first_seen_at=first if isinstance(first, str) else None,
                observed=max(int(grouped["evaluated"][wheel]["escalate"]) for wheel in past),
                required=required,
                window_seconds=rule.persistence_window_seconds,
            )
            confidence = "low"
            reason = "below its cold-tire limit; waiting to confirm"
        elif state == "normal" and grouped["sticky"]:
            reason = "back above its cold-tire limit by the clear margin long enough to clear"
        elif state == "normal":
            reason = "every checked tire is above its cold-tire limit"
        elif state == "recovering":
            reason = "back above its cold-tire limit by the clear margin; confirming over more readings"
        else:
            reason = (
                str(result.get("reason")) if isinstance(result, Mapping) and result.get("reason")
                else "no tire could be checked against its cold-tire limit"
            )
        warning_value, critical_value = thresholds.get(worst, (None, None)) if worst else (None, None)
        action_wheels = named or (past if state == "normal" else [])
        payload: dict[str, object] = {
            **base,
            "metric": (
                f"{rule.metric}.{worst}"
                if worst and isinstance(result, Mapping) and result.get("sample") is not None
                else rule.metric
            ),
            "state": state,
            "reason": reason,
            "notification_eligible": state == "warning",
            "severity": grouped["severity"],
            "confidence": confidence,
            "current": result.get("current") if isinstance(result, Mapping) else None,
            "regime": (
                result["sample"].get("regime")  # type: ignore[union-attr]
                if isinstance(result, Mapping) and isinstance(result.get("sample"), Mapping)
                else None
            ),
            "phase": result.get("phase") if isinstance(result, Mapping) else None,
            "wheel": worst,
            "absolute_threshold": {"operator": "below", "value": warning_value, "unit": rule.unit},
            "critical_threshold": {"operator": "below", "value": critical_value, "unit": rule.unit},
            "persistence": {
                "required": required,
                "observed": int(runs["escalate"]) if runs else 0,
                "window_seconds": rule.persistence_window_seconds,
                "satisfied": bool(runs) and int(runs["escalate"]) >= required,
                "critical_observed": int(runs["critical"]) if runs else 0,
                "observations": _observation_rows(runs["counted"]) if runs else [],
            },
            "hysteresis": {
                "clear_margin": rule.clear_margin,
                "clear_observed": int(runs["clear"]) if runs else 0,
                "required": required,
                "warm_clear_fraction": rule.warm_clear_fraction,
            },
            "wheels_past_threshold": past,
            "action_wheels": action_wheels,
            "held_keys": grouped["held"],
            "wheel_assessments": {
                wheel: {
                    "state": (
                        "warning" if wheel in named
                        else "normal" if item.get("state") == "evaluated"
                        else item.get("state")
                    ),
                    "reason": item.get("reason"),
                    "phase": item.get("phase"),
                    "value": (item.get("current") or {}).get("value") if isinstance(item.get("current"), Mapping) else None,
                    "limit": thresholds[wheel][0],
                    "critical_limit": thresholds[wheel][1],
                    "observed": item.get("escalate", 0),
                    "clear_observed": item.get("clear", 0),
                }
                for wheel, item in results.items()
            },
        }
        if action_wheels:
            labels = _labels(action_wheels)
            plural = len(action_wheels) > 1
            payload["action"] = (
                f"Inflate the {labels} tire{'s' if plural else ''} before driving far."
            )
            payload["title"] = f"{labels} tire{'s' if plural else ''} low"
        if candidate is not None:
            payload["candidate"] = candidate
        return payload

    def _wheel_stale(
        self,
        tick: _Tick,
        metric: str,
        sample: Mapping[str, object],
        series: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        """Whether a wheel still shows the RF hub's cached pre-trip reading.

        Stale when every reading this trip is the same value (within 0.05 psi)
        and equals the wheel's last reading before the trip began, or, when
        that reading is beyond raw retention, the value has been constant for
        at least 300 s and 10 readings.
        """

        trip = sample.get("trip_id")
        if not isinstance(trip, int) or isinstance(trip, bool):
            return {"stale": False, "reason": "not in a trip"}
        started_us = self._phase_index(tick, trip_id=trip).get(trip, {}).get("started_us")
        if not isinstance(started_us, int):
            last = self._last_trip(tick)
            if isinstance(last, Mapping) and last.get("id") == trip and last.get("started_at"):
                try:
                    started_us = _to_us(_utc(str(last["started_at"])))
                except (TypeError, ValueError):
                    started_us = None
        if not isinstance(started_us, int):
            return {"stale": False, "reason": "trip start is unknown"}
        key = ("pre_trip", metric, trip)
        if key not in tick.conditions:
            try:
                tick.conditions[key] = self.historian.latest_sample(
                    metric,
                    # Strictly before the trip's first snapshot.
                    at=_EPOCH + timedelta(microseconds=started_us - 1),
                    fresh_only=True,
                )
            except (AttributeError, TypeError, ValueError):
                tick.conditions[key] = None
        pre = tick.conditions[key]
        pre_value = (
            float(pre["value"])
            if isinstance(pre, Mapping)
            and _numeric(pre.get("value"))
            and pre.get("trip_id") != trip
            else None
        )
        trip_points = [
            point for point in series
            if point.get("trip_id") == trip and _numeric(point.get("value"))
        ]
        values = [float(point["value"]) for point in trip_points]
        stamps = [
            stamp for stamp in (_observed_us(point) for point in trip_points)
            if stamp is not None
        ]
        span = (max(stamps) - min(stamps)) / 1_000_000 if len(stamps) >= 2 else 0.0
        constant = len(values) >= 2 and max(values) - min(values) < 0.05
        # The series is newest first; the oldest reading is the trip's first.
        first_value = values[-1] if values else None
        stale = bool(
            constant
            and (
                (pre_value is not None and abs(float(first_value) - pre_value) < 0.05)
                or (pre_value is None and len(values) >= 10 and span >= 300.0)
            )
        )
        return {
            "stale": stale,
            "pre_trip_value": pre_value,
            "constant_since_start": constant,
            "observations": len(values),
            "span_seconds": span,
        }

    def _pair_motion(
        self,
        tick: _Tick,
        anchor: Mapping[str, object],
    ) -> Callable[[int], dict[str, object] | None]:
        """Moving-segment lookup for the pair rule (see PAIR_* constants).

        Returns ``at(us)``.  It gives None when no fresh vehicle.speed of at
        least PAIR_MOVING_SPEED_MPH is at most PAIR_SPEED_MAX_AGE_SECONDS old
        at that moment.  That covers a stopped van and a stale speed alike.
        Otherwise it gives the speed and the start of the unbroken moving
        segment.  A slower reading, or a speed gap longer than the max age,
        ends a segment.
        """

        key = ("pair_motion", anchor.get("trip_id"))
        cached = tick.conditions.get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        # Oldest first: (observed_us, mph).
        speeds = list(reversed(self._companion_values(
            tick, "vehicle.speed", anchor,
            lookback=self._lookbacks.get("vehicle.speed", 0.0),
        )))
        max_age_us = PAIR_SPEED_MAX_AGE_SECONDS * 1_000_000
        starts: list[int] = []  # segment start for each moving speed sample
        segment_start: int | None = None
        previous_us: int | None = None
        for observed_us, mph in speeds:
            if mph < PAIR_MOVING_SPEED_MPH:
                segment_start = None
            elif (
                segment_start is None
                or previous_us is None
                or observed_us - previous_us > max_age_us
            ):
                segment_start = observed_us
            starts.append(segment_start)  # type: ignore[arg-type]
            previous_us = observed_us
        stamps = [observed_us for observed_us, _mph in speeds]

        def at(point_us: int) -> dict[str, object] | None:
            index = bisect.bisect_right(stamps, point_us) - 1
            if index < 0 or point_us - stamps[index] > max_age_us:
                return None
            if starts[index] is None:
                return None
            return {"speed": speeds[index][1], "segment_start_us": starts[index]}

        tick.conditions[key] = at
        return at

    @staticmethod
    def _wheel_fresh(
        values: Sequence[tuple[int, float]],
        point_us: int,
        segment_start_us: int,
    ) -> bool:
        """Whether a wheel's reading at ``point_us`` was sent in this segment.

        ``values`` are newest first.  A value that changed after the segment
        began proves a new transmission.  So does moving without a stop for
        PAIR_ROLLING_TRUST_SECONDS.  With no reading before the segment, the
        first reading after its start stands in as the starting value.
        """

        if point_us - segment_start_us >= PAIR_ROLLING_TRUST_SECONDS * 1_000_000:
            return True
        inside = [
            value for observed_us, value in values
            if segment_start_us < observed_us <= point_us
        ]
        before = next(
            (value for observed_us, value in values if observed_us <= segment_start_us),
            None,
        )
        reference = before if before is not None else (inside[-1] if inside else None)
        return reference is not None and any(
            abs(value - reference) >= 0.05 for value in inside
        )

    def _pair_axle(
        self,
        rule: AbsoluteRule,
        tick: _Tick,
        left: str,
        right: str,
    ) -> dict[str, object]:
        left_metric = f"{rule.metric}.{left}"
        right_metric = f"{rule.metric}.{right}"
        samples = {}
        for wheel, metric in ((left, left_metric), (right, right_metric)):
            sample = self._latest(tick, metric)
            if sample is None or not _numeric(sample.get("value")):
                return {"state": "unavailable", "reason": f"no fresh {WHEEL_LABELS[wheel]} reading"}
            age = _sample_age_seconds(sample, tick.at)
            if age is None or age > rule.max_age_seconds:
                return {"state": "unavailable",
                        "reason": f"the {WHEEL_LABELS[wheel]} reading is too old"}
            if sample.get("unit") != rule.unit or sample.get("quality") not in rule.qualities:
                return {"state": "unavailable",
                        "reason": f"the {WHEEL_LABELS[wheel]} reading is not from a qualified decode"}
            samples[wheel] = (sample, age)
        left_sample, left_age = samples[left]
        right_sample, right_age = samples[right]
        # The RF hub reports each wheel's last reading from before the trip
        # until that sensor transmits again; a cached wheel compared with a
        # live one fabricates an asymmetry (trips 34/36).
        stale = {
            wheel: self._wheel_stale(
                tick,
                metric,
                samples[wheel][0],
                self._series(
                    tick, metric, samples[wheel][0],
                    lookback=_rule_lookbacks(rule).get(metric, 0.0),
                ),
            )
            for wheel, metric in ((left, left_metric), (right, right_metric))
        }
        cached = [wheel for wheel in (left, right) if stale[wheel]["stale"]]
        if cached:
            return {
                "state": "not_applicable",
                "reason": (
                    f"the {WHEEL_LABELS[cached[0]]} tire reading has not updated "
                    "since the last drive"
                ),
                "left": left,
                "right": right,
                "axle": AXLE_NAMES.get((left, right), f"{left}-{right}"),
                "sample": left_sample,
                "current": self._current_payload(left_sample, left_age),
                "samples": samples,
                "stale": stale,
            }
        # Stationary and fresh-transmission gates (see the PAIR_* constants).
        right_values = self._companion_values(
            tick, right_metric, left_sample,
            lookback=_rule_lookbacks(rule).get(right_metric, 400.0),
        )
        left_series = self._series(
            tick, left_metric, left_sample,
            lookback=_rule_lookbacks(rule).get(left_metric, 0.0),
        )
        left_values = [
            (stamp, float(point["value"])) for point in left_series
            for stamp in (_observed_us(point),)
            if stamp is not None and _numeric(point.get("value"))
        ]
        motion_at = self._pair_motion(tick, left_sample)

        def fresh_at(point_us: int, motion: Mapping[str, object]) -> dict[str, bool]:
            start = int(motion["segment_start_us"])  # type: ignore[arg-type]
            return {
                left: self._wheel_fresh(left_values, point_us, start),
                right: self._wheel_fresh(right_values, point_us, start),
            }

        stamps = [
            stamp for stamp in (_observed_us(left_sample), _observed_us(right_sample))
            if stamp is not None
        ]
        latest_us = max(stamps) if stamps else tick.at_us
        motion = motion_at(latest_us)
        fresh = fresh_at(latest_us, motion) if motion is not None else {left: False, right: False}
        motion_info = {
            "moving": motion is not None,
            "speed_mph": None if motion is None else motion["speed"],
            "segment_started_at": (
                None if motion is None
                else (_EPOCH + timedelta(microseconds=int(motion["segment_start_us"]))).isoformat()  # type: ignore[arg-type]
            ),
            "fresh": fresh,
        }
        waiting = [wheel for wheel in (left, right) if not fresh[wheel]]
        if motion is None or waiting:
            return {
                "state": "not_applicable",
                "reason": (
                    "the van is stopped or its speed is not current; tire sensors "
                    "report rarely while it stands"
                    if motion is None
                    else f"the {WHEEL_LABELS[waiting[0]]} tire has not reported since "
                    "the van started moving"
                ),
                "left": left,
                "right": right,
                "axle": AXLE_NAMES.get((left, right), f"{left}-{right}"),
                "sample": left_sample,
                "current": self._current_payload(left_sample, left_age),
                "samples": samples,
                "stale": stale,
                "motion": motion_info,
            }
        # Both offsets are conditioned on the same (engine, motion) regime.
        right_probe = {**right_sample, "regime": left_sample["regime"],
                       "trip_id": left_sample.get("trip_id")}
        baselines = {}
        for wheel, metric, probe in (
            (left, left_metric, left_sample),
            (right, right_metric, right_probe),
        ):
            baseline = self._rollup_baseline(
                metric=metric,
                sample=probe,
                at=tick.at,
                lookback_days=PAIR_OFFSET_LOOKBACK_DAYS,
                regime_dimensions=PAIR_OFFSET_DIMENSIONS,
            )
            shortfall = self._baseline_shortfall(
                baseline,
                buckets=PAIR_OFFSET_MINIMUM_BUCKETS,
                trips=PAIR_OFFSET_MINIMUM_TRIPS,
            )
            if shortfall is not None:
                return {
                    "state": "insufficient_history",
                    "reason": f"{WHEEL_LABELS[wheel]}: {shortfall}",
                    "sample": left_sample,
                    "current": self._current_payload(left_sample, left_age),
                    "baseline": baseline,
                    "samples": samples,
                    "stale": stale,
                }
            baselines[wheel] = baseline
        offset = baselines[left].median - baselines[right].median
        warning = float(rule.warning_threshold)  # type: ignore[arg-type]

        def accept(point: Mapping[str, object]) -> tuple:
            point_us = _observed_us(point)
            partner = (
                None if point_us is None
                else _companion_at(right_values, point_us, PAIR_COMPANION_MAX_AGE_SECONDS)
            )
            if partner is None:
                return ("gap", warning, None, 0.0)
            # A stationary reading, or one a wheel has not re-sent since the
            # van started moving, is gate-unmet ("fail").  It ends both runs
            # but does not contradict a recovery (see _absolute_runs).
            point_motion = motion_at(point_us)  # type: ignore[arg-type]
            if point_motion is None or not all(fresh_at(point_us, point_motion).values()):  # type: ignore[arg-type]
                return ("fail", warning, None, 0.0)
            return ("ok", warning, None, abs(float(point["value"]) - partner - offset))

        newest_us = _observed_us(left_sample)
        runs = _absolute_runs(
            left_series,
            newest_us=newest_us if newest_us is not None else tick.at_us,
            accept=accept,
            direction="high",
            window_seconds=rule.persistence_window_seconds,
            clear_margin=rule.clear_margin,
        )
        asymmetry = float(left_sample["value"]) - float(right_sample["value"]) - offset
        if abs(asymmetry) < warning:
            # Never open on a reading pair that is itself inside the limit.
            # In trip 59 the run's newest reading paired RL with RR's previous,
            # stale value (4.4 psi), while the two latest readings were 0.4
            # psi apart.
            runs = {**runs, "escalate": 0, "counted": []}
        lower = left if asymmetry < 0 else right
        return {
            "state": "evaluated",
            "left": left,
            "right": right,
            "axle": AXLE_NAMES.get((left, right), f"{left}-{right}"),
            "sample": samples[lower][0],
            "current": self._current_payload(*samples[lower]),
            "lower": lower,
            "offset": offset,
            "asymmetry": asymmetry,
            "value": abs(asymmetry),
            "past": abs(asymmetry) >= warning,
            "baselines": baselines,
            "left_value": float(left_sample["value"]),
            "right_value": float(right_sample["value"]),
            "samples": samples,
            "stale": stale,
            "motion": motion_info,
            **runs,
            "runs": runs,
        }

    def _evaluate_tire_pair(
        self,
        rule: AbsoluteRule,
        tick: _Tick,
        *,
        refresh: bool,
    ) -> dict[str, object]:
        if refresh:
            self.historian.refresh_rollups(through=tick.at)
        base = self._absolute_rule_base(rule)
        required = rule.persistence_observations
        results = {
            f"{left}{right}": self._pair_axle(rule, tick, left, right)
            for left, right in rule.gate_params
        }
        grouped = self._group_absolute(
            rule,
            tick,
            results,
            severity_key=lambda result: float(result["value"]),
            clear_required=PAIR_CLEAR_OBSERVATIONS,
        )
        state = grouped["state"]
        worst = grouped["worst"]
        named = grouped["named"]
        past = grouped["past"]
        result = results.get(worst) if worst is not None else None
        runs = result.get("runs") if isinstance(result, Mapping) else None
        candidate = None
        confidence = None
        if state == "warning":
            self._drop_candidate(rule.key)
            confidence = "high"
            reason = "the two tires on one axle stayed further apart than usual"
        elif state == "normal" and past:
            core = grouped["evaluated"][past[0]]
            counted = core.get("counted") or []
            first = counted[-1].get("observed_at") if counted else None
            candidate = self._candidate(
                rule.key,
                at_us=tick.at_us,
                first_seen_at=first if isinstance(first, str) else None,
                observed=max(int(grouped["evaluated"][name]["escalate"]) for name in past),
                required=required,
                window_seconds=rule.persistence_window_seconds,
            )
            confidence = "low"
            reason = "the two tires on one axle are further apart than usual; waiting to confirm"
        elif state == "normal" and grouped["sticky"]:
            reason = "the axle's tires are back as close as usual long enough to clear"
        elif state == "normal":
            reason = "each axle's tires are as close as usual"
        elif state == "recovering":
            reason = "the axle's tires are back as close as usual; confirming over more readings"
        else:
            reason = (
                str(result.get("reason")) if isinstance(result, Mapping) and result.get("reason")
                else "no axle could be compared"
            )
        evaluated = result if isinstance(result, Mapping) and result.get("state") == "evaluated" else None
        action_axles = named or (past if state == "normal" else [])
        metric = f"{rule.metric}.{evaluated['lower']}" if evaluated else rule.metric
        current = result.get("current") if isinstance(result, Mapping) else None
        opened_metric = grouped["opened"].get("metric")
        if (
            grouped["sticky"]
            and state != "warning"
            and isinstance(opened_metric, str)
            and isinstance(result, Mapping)
        ):
            # Describe the wheel that was lower when the event opened, even
            # if the other wheel now reads lower, so the timed recovery gate
            # compares the same sensor.
            opened_wheel = opened_metric.rsplit(".", 1)[-1]
            held_sample = (result.get("samples") or {}).get(opened_wheel)
            if held_sample is not None:
                metric = opened_metric
                current = self._current_payload(*held_sample)
        payload: dict[str, object] = {
            **base,
            "metric": metric,
            "state": state,
            "reason": reason,
            "notification_eligible": state == "warning",
            "severity": grouped["severity"],
            "confidence": confidence,
            "current": current,
            "regime": (
                result["sample"].get("regime")  # type: ignore[union-attr]
                if isinstance(result, Mapping) and isinstance(result.get("sample"), Mapping)
                else None
            ),
            "baseline": None,
            "persistence": {
                "required": required,
                "observed": int(runs["escalate"]) if runs else 0,
                "window_seconds": rule.persistence_window_seconds,
                "satisfied": bool(runs) and int(runs["escalate"]) >= required,
                "companion_gaps": int(runs["companion_gaps"]) if runs else 0,
                "observations": _observation_rows(runs["counted"]) if runs else [],
            },
            "hysteresis": {
                "clear_margin": rule.clear_margin,
                "clear_threshold": float(rule.warning_threshold) - rule.clear_margin,  # type: ignore[arg-type]
                "clear_observed": int(runs["clear"]) if runs else 0,
                "required": PAIR_CLEAR_OBSERVATIONS,
            },
            "pair": None if not evaluated else {
                "axle": evaluated["axle"],
                "left": evaluated["left"],
                "right": evaluated["right"],
                "left_value": evaluated["left_value"],
                "right_value": evaluated["right_value"],
                "usual_offset": evaluated["offset"],
                "asymmetry": evaluated["asymmetry"],
                "lower_wheel": evaluated["lower"],
                "summary": _pair_summary(evaluated),
            },
            # The per-wheel baselines are the second source; listing them as
            # paired companions lets the event history archive their inputs.
            "corroborators": [] if not evaluated else [
                {
                    "metric": f"{rule.metric}.{wheel}",
                    "state": "paired",
                    "baseline": _baseline_dict(evaluated["baselines"][wheel]),
                }
                for wheel in (evaluated["left"], evaluated["right"])
            ],
            "axle_assessments": {
                name: {
                    "state": (
                        "warning" if name in named
                        else "normal" if item.get("state") == "evaluated"
                        else item.get("state")
                    ),
                    "reason": item.get("reason"),
                    "axle": item.get("axle"),
                    "asymmetry": item.get("asymmetry"),
                    "usual_offset": item.get("offset"),
                    "observed": item.get("escalate", 0),
                    "clear_observed": item.get("clear", 0),
                    "companion_gaps": item.get("companion_gaps", 0),
                    **({"stale": item["stale"]} if item.get("stale") is not None else {}),
                    **({"motion": item["motion"]} if item.get("motion") is not None else {}),
                }
                for name, item in results.items()
            },
            "axles_past_threshold": [results[name]["axle"] for name in past],
            "held_keys": grouped["held"],
            "action_wheels": [
                wheel for name in action_axles
                for wheel in (results[name]["left"], results[name]["right"])
            ],
        }
        if action_axles:
            axle = results[action_axles[0]]["axle"]
            payload["action"] = f"Check both {axle} tires at the next stop."
            payload["title"] = f"{str(axle).capitalize()} tires uneven"
        if candidate is not None:
            payload["candidate"] = candidate
        return payload

    def _evaluate_absolute_oil_rule(
        self,
        rule: AbsoluteOilPressureRule,
        *,
        at: datetime,
        tick: _Tick | None = None,
    ) -> dict[str, object]:
        if tick is None:
            tick = _Tick(at)
        base = self._absolute_base(rule)
        rpm = self._latest(tick, rule.running_metric)
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
                # Not "suppressed": that state resolves an open episode, so
                # stopping the engine (the rule's own advice) would close a
                # critical event and cancel its undelivered push.  Gated
                # absolute rules report not_applicable and hold instead.
                "state": "not_applicable",
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
        running = self._continuous(
            tick,
            rpm,
            minimum=rule.running_rpm_threshold,
            lookback=RUNNING_LOOKBACK_SECONDS,
            max_gap=rule.running_max_gap_seconds,
        )
        if running is None or float(running["duration_seconds"]) < rule.startup_grace_seconds:
            return {
                **base,
                "state": "not_applicable",
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
        oil = self._latest(tick, rule.metric)
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
        running_start = _to_us(_utc(str(running["started_at"])))
        engine = _engine(oil)
        grace_us = rule.startup_grace_seconds * 1_000_000

        def accept(point: Mapping[str, object]) -> tuple:
            point_us = _observed_us(point)
            value = float(point["value"])
            if _engine(point) != engine:
                return ("skip", None, None, value)
            if point_us is None or point_us - running_start < grace_us:
                return ("fail", rule.minimum_pressure_psi, None, value)
            return ("ok", rule.minimum_pressure_psi, None, value)

        newest_us = _observed_us(oil)
        runs = _absolute_runs(
            self._series(
                tick, rule.metric, oil,
                lookback=_rule_lookbacks(rule).get(rule.metric, 0.0),
            ),
            newest_us=newest_us if newest_us is not None else tick.at_us,
            accept=accept,
            direction="low",
            window_seconds=rule.persistence_window_seconds,
            clear_margin=rule.clear_margin,
        )
        below = float(oil["value"]) < rule.minimum_pressure_psi
        observed = int(runs["escalate"]) if below else 0
        persistent = observed >= rule.persistence_observations
        open_info = self._open_info(tick, rule.key)
        candidate = None
        confidence = None
        if open_info.get("state") == "warning" and not persistent:
            if int(runs["clear"]) >= rule.persistence_observations:
                state = "normal"
                reason = "oil pressure is back above the operating minimum by the clear margin"
            elif int(runs["clear"]) >= 1 and not runs.get("clear_contradicted", True):
                # First readings after a restart are all clear: confirming,
                # not a re-asserted critical (which would repeat the push).
                state = "recovering"
                reason = "oil pressure is back above the minimum by the clear margin; confirming"
            else:
                state = "warning"
                reason = "oil pressure has not come back above the minimum by the clear margin yet"
        elif persistent:
            state = "warning"
            reason = (
                "persistent oil pressure below the approximately 12 psi "
                "OEM operating minimum"
            )
        elif below:
            state = "normal"
            reason = "critical reading awaits a second independent observation"
        else:
            state = "normal"
            reason = "oil pressure is not below the OEM-context operating minimum"
        if state == "warning":
            self._drop_candidate(rule.key)
            confidence = "medium"
        elif below:
            counted = runs["counted"]
            first = counted[-1].get("observed_at") if counted else oil.get("observed_at")  # type: ignore[index]
            candidate = self._candidate(
                rule.key,
                at_us=tick.at_us,
                first_seen_at=first if isinstance(first, str) else None,
                observed=observed,
                required=rule.persistence_observations,
                window_seconds=rule.persistence_window_seconds,
            )
            confidence = "low"
        payload = {
            **base,
            "state": state,
            "reason": reason,
            "notification_eligible": state == "warning",
            "confidence": confidence,
            "current": current,
            "regime": oil["regime"],
            "running_evidence": running_evidence,
            "persistence": {
                "required": rule.persistence_observations,
                "observed": observed,
                "window_seconds": rule.persistence_window_seconds,
                "satisfied": persistent,
            },
            "hysteresis": {
                "clear_margin": rule.clear_margin,
                "clear_threshold": rule.minimum_pressure_psi + rule.clear_margin,
                "clear_observed": int(runs["clear"]),
                "required": rule.persistence_observations,
            },
        }
        if candidate is not None:
            payload["candidate"] = candidate
        return payload

    # ------------------------------------------------------------------
    # Public entry points

    def evaluate_rule(
        self,
        rule: EvaluationRule,
        *,
        at: datetime | str | None = None,
        _refresh_rollups: bool = True,
    ) -> dict[str, object]:
        evaluated = _utc(at)
        tick, owned = self._tick(evaluated)
        try:
            if owned:
                self._prune_candidates(tick.at_us)
            if isinstance(rule, AbsoluteOilPressureRule):
                return self._evaluate_absolute_oil_rule(rule, at=evaluated, tick=tick)
            if isinstance(rule, AbsoluteRule):
                return self._evaluate_absolute_rule(rule, tick, refresh=_refresh_rollups)
            if isinstance(rule, TireGroupRule):
                return self._evaluate_tire_group(rule, tick, refresh=_refresh_rollups)
            return self._evaluate_relative_rule(rule, tick, refresh=_refresh_rollups)
        finally:
            if owned:
                self._local.tick = None

    def evaluate(
        self,
        *,
        at: datetime | str | None = None,
    ) -> dict[str, object]:
        """Return the assessment list, the active warning subset and candidates."""

        evaluated = _utc(at)
        self.historian.refresh_rollups(through=evaluated)
        tick = _Tick(evaluated)
        previous = getattr(self._local, "tick", None)
        self._local.tick = tick
        try:
            self._prune_candidates(tick.at_us)
            assessments = [
                self.evaluate_rule(rule, at=evaluated, _refresh_rollups=False)
                for rule in self.rules
            ]
        finally:
            self._local.tick = previous
        custom_status = {"enabled": self.custom_rules_path is not None, "count": 0, "error": None}
        if self.custom_rules_path is not None:
            from projects.vehicle_data.custom_warnings import load_rules, evaluate_rule
            try:
                rows = load_rules(self.custom_rules_path)
                custom = [evaluate_rule(self.historian, row, evaluated) for row in rows]
                assessments.extend(custom)
                custom_status["count"] = len(rows)
                custom_status["rule_ids"] = [row["id"] for row in rows]
            except (OSError, ValueError, KeyError, TypeError):
                # Malformed owner configuration must not suppress built-in warnings.
                custom_status["error"] = "Custom warning configuration could not be evaluated"
        from projects.vehicle_data.monitoring_coverage import coverage_assessments
        assessments.extend(coverage_assessments(self.historian, assessments, evaluated))
        active = [
            assessment
            for assessment in assessments
            if assessment["state"] in ("watch", "warning")
        ]
        candidates = [
            assessment
            for assessment in assessments
            if assessment["state"] == "normal"
            and isinstance(assessment.get("candidate"), Mapping)
        ]
        return {
            "schema_version": WARNING_SCHEMA_VERSION,
            "generated_at": evaluated.isoformat(),
            "custom_rules": custom_status,
            "method": {
                "center": "median of completed comparable bucket medians",
                "spread": "1.4826 × median absolute deviation",
                "conditioning": (
                    "exact engine/speed/RPM/coolant regime plus exact unit, source, "
                    "quality, and provenance"
                ),
                "tiers": {
                    "0": (
                        "absolute limits behind context gates; a confirmed warning "
                        "holds until the reading is back past the limit by a clear margin"
                    ),
                    "1": (
                        "sustained same-conditions deviation: threshold max(minimum "
                        "effect, multiplier × max(robust sigma, sigma floor)), escalation "
                        "after N consecutive samples, de-escalation after N samples at "
                        "or below 0.7 × threshold; unconfirmed deviations are in-memory "
                        "candidates that never open an episode"
                    ),
                    "3": "system and telemetry-coverage items; System notes only, never notify",
                },
                "opaque_health_score": False,
            },
            "active": active,
            "candidates": candidates,
            "assessments": assessments,
        }


def _pair_summary(evaluated: Mapping[str, object]) -> str | None:
    """Card sentence for a same-axle pair: the lower wheel against its mate and
    their usual offset, e.g. ``RL 76.0 psi is 3.6 under RR 79.6 (usually 0.8 over)``.
    """
    left, right = evaluated.get("left"), evaluated.get("right")
    lv, rv = evaluated.get("left_value"), evaluated.get("right_value")
    if not (isinstance(left, str) and isinstance(right, str) and _numeric(lv) and _numeric(rv)):
        return None
    left_low = evaluated.get("lower") == left if evaluated.get("lower") else lv <= rv
    low, high = (left, right) if left_low else (right, left)
    low_v, high_v = (lv, rv) if left_low else (rv, lv)
    text = (
        f"{low.upper()} {float(low_v):.1f} psi is {abs(float(lv) - float(rv)):.1f} under "
        f"{high.upper()} {float(high_v):.1f}"
    )
    offset = evaluated.get("offset")
    if _numeric(offset):
        # How far the lower wheel usually sits under its mate (offset is left - right).
        usual = -float(offset) if left_low else float(offset)
        text += (
            " (usually even)" if abs(usual) < 0.05
            else f" (usually {abs(usual):.1f} {'under' if usual > 0 else 'over'})"
        )
    return text


class InfrastructureHealthEvaluator:
    """Translate persisted USB/interface facts into tier-3 system assessments.

    This evaluator never resets, rebinds, or reconfigures hardware.  Nothing
    here is notification-eligible: adapter, bus and data-quality items are
    shown in the dashboard's System notes only (owner decision 2026-09-24).
    Severity still ranks them there: a controller error or ambiguous identity
    (immediately), a missing adapter or down link after about five minutes
    (60 historian samples), a failed status probe and a latched restoration
    inhibit are the hard faults.  A role missing or failing on its first
    observation reports ``normal`` with ``pending`` counts, so no System event
    opens until the second observation confirms it.  USB topology changes and
    transient disconnects are information unless they persist.
    """

    MISSING_NOTIFY_OBSERVATIONS = 60
    TRANSIENT_WARNING_SECONDS = 5 * 60
    TRANSIENT_WARNING_REMOVALS_24H = 3
    ADAPTER_ACTION = "Check the CAN adapter cable."
    USB_ACTION = "Check the USB hub and adapter."
    INHIBIT_ACTION = "Restart the telemetry service when parked."
    DEAF_ACTION = "Unplug the CAN adapter's USB cable for 5 s, or reboot the Pi."

    def __init__(self, historian: TelemetryHistorian):
        self.historian = historian

    @staticmethod
    def _base(
        *,
        rule: str,
        title: str,
        severity: str = "warning",
        rate_limit_seconds: float = 30 * 60,
        action: str = "Check the USB hub and adapter.",
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
            "tier": 3,
            "group": "system",
            "action": action,
            "confidence": None,
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
        removal_count_24h = usb_context.get("removal_event_count_24h", 0)
        removal_count_24h = (
            removal_count_24h
            if isinstance(removal_count_24h, int) and not isinstance(removal_count_24h, bool)
            else 0
        )
        oldest_active_seconds = None
        for item in active_usb:
            if not isinstance(item, Mapping) or not isinstance(item.get("opened_at"), str):
                continue
            try:
                opened = _utc(str(item["opened_at"]))
            except ValueError:
                continue
            elapsed = (evaluated - opened).total_seconds()
            if oldest_active_seconds is None or elapsed > oldest_active_seconds:
                oldest_active_seconds = elapsed
        long_incident = (
            oldest_active_seconds is not None
            and oldest_active_seconds >= self.TRANSIENT_WARNING_SECONDS
        )
        repeated_removals = removal_count_24h >= self.TRANSIENT_WARNING_REMOVALS_24H
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
        if long_incident or repeated_removals:
            usb_state = "warning"
            usb_reason = (
                "a USB CAN incident has stayed unresolved for more than five minutes"
                if long_incident
                else f"{removal_count_24h} USB CAN removals in the last 24 hours"
            )
        elif transient_detected or usb_history_available:
            # Short, self-resolving hub re-enumerations are information only.
            usb_state = "normal"
            if transient_detected:
                usb_reason += "; short-lived, recorded as information"
        else:
            # Lost incident history is not affirmative recovery evidence.  The
            # advisory lifecycle treats unavailable as inconclusive and keeps
            # any already-open episode intact without notification eligibility.
            usb_state = "unavailable"
        probe = context.get("interface_probe") or {}
        probe_state = probe.get("state")
        waiting_probe = probe_state == "awaiting_first_probe"
        failed_probe = probe_state == "failed"
        elapsed = probe.get("elapsed_seconds")
        overdue = waiting_probe and isinstance(elapsed,(int,float)) and not isinstance(elapsed,bool) and elapsed >= 30
        assessments: list[dict[str, object]] = []
        for role in CAN_BUS_ROLES:
            normalized_rule = role.replace("-", "_")
            base = self._base(
                rule=f"can_interface_role_{normalized_rule}",
                title=f"{role} interface status",
                action=self.ADAPTER_ACTION,
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
            status_unproven = current is None or current.get("health") == "unknown"
            immediate = False
            missing_or_down = False
            if status_unproven and (waiting_probe or failed_probe):
                state = "unavailable"
                reason = ("awaiting the broker's first interface status probe" if waiting_probe else "broker interface status probe failed; device health is unknown")
                current_payload = {"role":role,"health":"unknown","reason":"status_probe_pending" if waiting_probe else "status_probe_failed"}
                base["title"] = f"{role} interface status pending"
            elif current is None:
                base["title"] = f"{role} interface status is missing"
                state = "warning" if count >= 2 else "normal"
                reason = "logical role is absent from current interface status"
                missing_or_down = True
                current_payload: dict[str, object] = {
                    "role": role,
                    "health": "missing",
                    "reason": role_gap.get("reason", "interface_role_absent"),
                    "gap_observation_count": count,
                }
                if state != "warning":
                    reason += "; first observation, confirming"
                    current_payload["pending"] = {"observed": count, "required": 2}
            else:
                current_payload = dict(current)
                health = current.get("health")
                resolution = current.get("resolution")
                cause = current.get("reason")
                if health == "healthy":
                    state = "normal"
                    reason = "role is resolved and its controller is healthy"
                elif health == "unknown":
                    state = "normal"
                    reason = "role health evidence is incomplete"
                    current_payload["pending"] = {"observed": count, "required": 2}
                    base["title"] = f"{role} interface status is incomplete"
                else:
                    causes = set(str(cause).split(","))
                    # receive_silent already needed two minutes of evidence.
                    immediate = (
                        resolution == "ambiguous"
                        or "controller_unhealthy" in causes
                        or "receive_silent" in causes
                    )
                    state = "warning" if immediate or count >= 2 else "normal"
                    if resolution in ("missing", "ambiguous"):
                        reason = f"logical USB role resolution is {resolution}"
                        base["title"] = f"{role} adapter {'identity is ambiguous' if resolution == 'ambiguous' else 'is missing'}"
                        missing_or_down = resolution == "missing"
                    elif "controller_unhealthy" in causes:
                        reason = "SocketCAN controller is not ERROR-ACTIVE"
                        base["title"] = f"{role} controller error"
                    elif "receive_silent" in causes:
                        reason = (
                            "no frames received for two minutes or more while "
                            "CAN-CH was continuously busy; the link reports "
                            "normal, so the adapter itself has stopped receiving"
                        )
                        base["title"] = f"{role} adapter hears nothing while CAN-CH is active"
                        base["action"] = self.DEAF_ACTION
                    else:
                        reason = f"interface health check failed: {cause}"
                        if "interface_down" in causes:
                            base["title"] = f"{role} interface is down"
                            missing_or_down = True
                        elif "adapter_missing" in causes:
                            base["title"] = f"{role} adapter is missing"
                            missing_or_down = True
                        else:
                            base["title"] = f"{role} interface configuration mismatch"
                            if current.get("role_reason"):
                                reason += f" ({current['role_reason']})"
                current_payload["gap_observation_count"] = count
                if health not in ("healthy", "unknown") and state != "warning":
                    reason += "; first observation, confirming"
                    current_payload["pending"] = {"observed": count, "required": 2}
            hard = state == "warning" and (
                immediate
                or (missing_or_down and count >= self.MISSING_NOTIFY_OBSERVATIONS)
            )
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
                    "severity": "warning" if hard else "info",
                    "notification_eligible": False,
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
                                or "controller_unhealthy" in str(current.get("reason")).split(",")
                            )
                        ) else 2,
                        "observed": count,
                        "notification_required": (
                            1 if immediate else self.MISSING_NOTIFY_OBSERVATIONS
                        ),
                    },
                }
            )
        hard_role = any(
            item["severity"] == "warning" and item["state"] == "warning"
            for item in assessments
        )

        if probe:
            discovery_state = "warning" if overdue or failed_probe else "unavailable" if waiting_probe else "normal"
            assessments.append({
                **self._base(rule="can_interface_status_probe", title="Interface status probe failed" if failed_probe else "Interface discovery delayed" if overdue else "Interface status initialization", severity="warning" if failed_probe else "info", action=self.USB_ACTION),
                "state":discovery_state,
                "reason":"interface status probe failed; adapter health cannot be established" if failed_probe else "first interface status probe has not completed within 30 seconds" if overdue else "awaiting initial interface discovery" if waiting_probe else "interface status probe completed",
                "notification_eligible": False,
                "current":dict(probe),
            })

        topology_changed = context.get("topology_changed") is True
        topology_warning = topology_changed and hard_role
        assessments.append(
            {
                **self._base(
                    rule="usb_can_topology_generation_changed",
                    title="USB CAN topology generation changed",
                    severity="warning" if topology_warning else "info",
                    rate_limit_seconds=10 * 60,
                    action=self.USB_ACTION,
                ),
                "state": "warning" if topology_warning else "normal",
                "reason": (
                    "serial/dev_id to netdev topology changed while an adapter "
                    "role is failing"
                    if topology_warning
                    else "serial/dev_id to netdev topology changed since the prior "
                    "sample; recorded as information"
                    if topology_changed
                    else "USB CAN topology generation is stable or establishing its baseline"
                ),
                "notification_eligible": False,
                "notification_suppressed_by": None,
                "current": {
                    "topology_changed": topology_changed,
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
                    severity="warning" if usb_state == "warning" else "info",
                    rate_limit_seconds=10 * 60,
                    action=self.USB_ACTION,
                ),
                "state": usb_state,
                "reason": usb_reason,
                "notification_eligible": False,
                "current": {
                    "new_removal_event_count": len(removals),
                    "active_incident_count": len(active_usb),
                    "oldest_active_incident_seconds": oldest_active_seconds,
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
                    "removal_event_count_24h": removal_count_24h,
                    "source": usb_context.get("source"),
                },
                "persistence": {
                    "required": 1,
                    "observed": len(removals) or len(active_usb),
                    "warning_after_seconds": self.TRANSIENT_WARNING_SECONDS,
                    "warning_after_removals_24h": self.TRANSIENT_WARNING_REMOVALS_24H,
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
                    action=self.INHIBIT_ACTION,
                ),
                "state": "warning" if inhibited else "normal",
                "reason": (
                    "active-drive restoration failed or a restoration inhibit is latched"
                    if inhibited
                    else "no restoration failure or restoration inhibit is present"
                ),
                "notification_eligible": False,
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
