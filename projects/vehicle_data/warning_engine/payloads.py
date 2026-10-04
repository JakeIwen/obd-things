"""Explainable assessment payloads and tire-pair display text."""
from __future__ import annotations

from typing import Mapping

from lib.timeutil import finite_number as _numeric
from projects.vehicle_data.historian import BaselineStats

from .rules import (
    AbsoluteOilPressureRule,
    AbsoluteRule,
    RELATIVE_RATE_LIMIT_SECONDS,
    TireGroupRule,
    WarningRule,
    _rule_snapshot,
)


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


class PayloadsMixin:
    """Assessment payload builders; no evaluator state of their own."""

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
