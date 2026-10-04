"""Frozen warning rules, validation, tuned defaults and revision snapshots."""
from __future__ import annotations

import functools
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from lib.vehicle_can_roles import CAN_BUS_ROLES, CAN_ROLE_SPECS
from projects.vehicle_data.historian import REGIME_DIMENSIONS, project_regime


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
# Rationale: docs/history/early-warning-rationale.md#running-observation-gap
RUNNING_MAX_GAP_SECONDS = 20.0
RUNNING_LOOKBACK_SECONDS = 15 * 60
RPM_MAX_AGE_SECONDS = 10.0
WARM_COOLANT_F = 160.0
PARKED_SETTLE_SECONDS = 30.0
PARKED_ENGINE_STATES = frozenset(("engine_off", "engine_unknown"))
SLOW_MOTIONS = frozenset(("stationary", "urban"))
CANDIDATE_MEMORY_WINDOWS = 2
# Rationale: docs/history/early-warning-rationale.md#persistence-observation-spacing
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
        # Rationale: docs/history/early-warning-rationale.md#coolant-slow-motion-floor
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
        # Rationale: docs/history/early-warning-rationale.md#tire-warm-recovery
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
        # Rationale: docs/history/early-warning-rationale.md#tire-pair-recovery
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
# Rationale: docs/history/early-warning-rationale.md#tire-pair-transmission-gates
PAIR_MOVING_SPEED_MPH = 5.0
PAIR_SPEED_MAX_AGE_SECONDS = 20.0
PAIR_ROLLING_TRUST_SECONDS = 150.0
PAIR_CLEAR_OBSERVATIONS = 8


def default_rule_catalog() -> list[dict[str, object]]:
    """Serializable rule metadata for API/UI discovery."""

    return [asdict(rule) for rule in DEFAULT_EVALUATION_RULES]
