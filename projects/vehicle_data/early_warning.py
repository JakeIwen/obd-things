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

# Compatibility surface. Implementations live in warning_engine; these are
# direct re-exports, including the historically imported private helpers.

from .warning_engine.evaluator import (
    EarlyWarningEvaluator,
)

from .warning_engine.infrastructure import (
    InfrastructureHealthEvaluator,
)

from .warning_engine.payloads import (
    _baseline_dict,
    _pair_summary,
)

from .warning_engine.rules import (
    ABSOLUTE_GATES,
    ABSOLUTE_RATE_LIMIT_SECONDS,
    AXLE_NAMES,
    AbsoluteOilPressureRule,
    AbsoluteRule,
    CANDIDATE_MEMORY_WINDOWS,
    COOLANT_RATE_ACTION,
    CorroborationRule,
    DEFAULT_ABSOLUTE_WARNING_RULES,
    DEFAULT_ACTION,
    DEFAULT_EVALUATION_RULES,
    DEFAULT_WARNING_RULES,
    DIRECTIONS,
    EvaluationRule,
    FORBIDDEN_ACTION_WORDS,
    GENERATOR_DUTY_METRIC,
    GROUPS,
    INCONCLUSIVE_PRIORITY,
    MAX_ACTION_LENGTH,
    OBSERVATION_SPACING_ALLOWANCE_SECONDS,
    PAIR_CLEAR_OBSERVATIONS,
    PAIR_COMPANION_MAX_AGE_SECONDS,
    PAIR_MOVING_SPEED_MPH,
    PAIR_OFFSET_DIMENSIONS,
    PAIR_OFFSET_LOOKBACK_DAYS,
    PAIR_OFFSET_MINIMUM_BUCKETS,
    PAIR_OFFSET_MINIMUM_TRIPS,
    PAIR_ROLLING_TRUST_SECONDS,
    PAIR_SPEED_MAX_AGE_SECONDS,
    PARKED_ENGINE_STATES,
    PARKED_SETTLE_SECONDS,
    PHASE_INDEX_CACHE_SECONDS,
    QUALIFIED_QUALITIES,
    RELATIVE_RATE_LIMIT_SECONDS,
    ROLE_USB_SERIALS,
    RPM_MAX_AGE_SECONDS,
    RUNNING_GRACE_SECONDS,
    RUNNING_LOOKBACK_SECONDS,
    RUNNING_MAX_GAP_SECONDS,
    RUNNING_RPM,
    SEVERITIES,
    SLOW_MOTIONS,
    TIERS,
    TireGroupRule,
    WARM_COOLANT_F,
    WARNING_SCHEMA_VERSION,
    WHEEL_LABELS,
    WarningRule,
    _EPOCH,
    _cached_snapshot,
    _rule_snapshot,
    _validate_count,
    _validate_direction,
    _validate_finite,
    _validate_group,
    _validate_nonnegative,
    _validate_positive,
    _validate_tier,
    _wheel_rule,
    default_rule_catalog,
    derive_group,
    validate_action,
)

from .warning_engine.samples import (
    _Tick,
    _absolute_runs,
    _band_floor,
    _captured_us,
    _cleared,
    _companion_at,
    _effect,
    _engine,
    _labels,
    _motion,
    _observation_rows,
    _observed_us,
    _past,
    _project,
    _relative_runs,
    _rule_lookbacks,
    _sample_age_seconds,
    _slope,
    _threshold,
    _to_us,
    _utc,
)
