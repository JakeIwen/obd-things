"""Saved-history warning evaluation; no CAN or notification side effects.

The evaluator owns mutable state and tick lifetime. Internal mixins separate
rule families without overriding one another or changing method bodies.
``projects.vehicle_data.early_warning`` retains the historical import surface.
"""
from .evaluator import EarlyWarningEvaluator
from .infrastructure import InfrastructureHealthEvaluator
from .rules import (
    AbsoluteOilPressureRule,
    AbsoluteRule,
    CorroborationRule,
    DEFAULT_ABSOLUTE_WARNING_RULES,
    DEFAULT_EVALUATION_RULES,
    DEFAULT_WARNING_RULES,
    EvaluationRule,
    TireGroupRule,
    WARNING_SCHEMA_VERSION,
    WarningRule,
    default_rule_catalog,
    derive_group,
    validate_action,
)
