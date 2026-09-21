"""Owner-approved threshold rules. Data configuration only; never code or CAN."""

from datetime import datetime, timezone
import json
import math
from pathlib import Path

from projects.vehicle_data.metrics import METRICS

DEFAULT_RULES_PATH = Path("/var/lib/van-telemetry-advisor/early-warnings.json")
MAX_RULES = 32
FIELDS = {"title", "metric", "operator", "threshold", "persistence_observations",
          "window_seconds", "max_age_seconds", "engine_running"}


def catalog():
    return {name: definition.public_dict() for name, definition in METRICS.items()
            if definition.value_type == "number" and name != "generator.field_duty"
            and any(s.quality in ("verified", "observed_alfa_scale") for s in definition.sources)}


def validate_rule(rule):
    if not isinstance(rule, dict) or set(rule) != FIELDS:
        raise ValueError("A warning must contain exactly the supported threshold-rule fields")
    if not isinstance(rule["title"], str) or not 1 <= len(rule["title"].strip()) <= 120:
        raise ValueError("Use a warning title between 1 and 120 characters")
    if not isinstance(rule["metric"], str) or rule["metric"] not in catalog():
        raise ValueError("Choose an established numeric telemetry metric")
    if rule["operator"] not in ("above", "below", "absolute_above"):
        raise ValueError("Choose above, below or absolute_above")
    for key, low, high in (("threshold", -1000000, 1000000), ("window_seconds", 5, 600),
                           ("max_age_seconds", 1, min(60, METRICS[rule["metric"]].stale_after_seconds)),
                           ("persistence_observations", 2, 60)):
        value = rule[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
    if type(rule["persistence_observations"]) is not int or type(rule["engine_running"]) is not bool:
        raise ValueError("Persistence must be an integer and engine_running a boolean")
    if rule["operator"] == "absolute_above" and rule["threshold"] < 0:
        raise ValueError("An absolute-angle threshold cannot be negative")
    if rule["max_age_seconds"] > rule["window_seconds"]:
        raise ValueError("Maximum sample age cannot exceed the persistence window")
    return dict(rule, title=rule["title"].strip())


def load_rules(path):
    path = Path(path)
    if not path.exists():
        return []
    if path.is_symlink() or path.stat().st_size > 128 * 1024:
        raise ValueError("Invalid warning configuration file")
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("rules"), list):
        raise ValueError("Invalid warning configuration")
    rows = payload["rules"]
    if len(rows) > MAX_RULES or len({r["id"] for r in rows}) != len(rows):
        raise ValueError("Too many or duplicate custom warnings")
    for row in rows:
        if not isinstance(row["id"], str) or len(row["id"]) != 64 or any(c not in "0123456789abcdef" for c in row["id"]):
            raise ValueError("Invalid custom warning ID")
        validate_rule(row["rule"])
    return rows


def _age(sample, at):
    try:
        observed = datetime.fromisoformat(sample["observed_at"].replace("Z", "+00:00"))
        return (at - observed).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None


def _qualified(sample, metric, at, max_age):
    if not sample or type(sample.get("value")) not in (float, int) or not math.isfinite(sample["value"]):
        return False
    age = _age(sample, at)
    definition = METRICS[metric]
    return (sample.get("freshness") == "fresh" and sample.get("unit") == definition.unit
            and age is not None and 0 <= age <= max_age
            and any(s.name == sample.get("source") and s.quality == sample.get("quality")
                    and s.quality in ("verified", "observed_alfa_scale") for s in definition.sources))


def evaluate_rule(historian, row, at):
    rule = validate_rule(row["rule"])
    metric = rule["metric"]
    result = {"rule": "custom_" + row["id"], "title": rule["title"], "metric": metric,
              "category": "vehicle_health", "severity": "warning", "advisory": True,
              "direction": "low" if rule["operator"] == "below" else "high",
              "state": "unavailable", "reason": "No qualified fresh reading",
              "notification_eligible": False, "notification_rate_limit_seconds": 1800,
              "interpretation": "Owner-approved monitoring reference, not an OEM limit or diagnosis",
              "custom_rule": rule, "current": None, "baseline": None, "deviation": None,
              "persistence": {"required": rule["persistence_observations"], "observed": 0,
                              "window_seconds": rule["window_seconds"], "satisfied": False}}
    sample = historian.latest_sample(metric, at=at, fresh_only=False)
    if not _qualified(sample, metric, at, rule["max_age_seconds"]):
        return result
    result["current"] = dict(sample, effective_age_seconds=_age(sample, at))
    result["regime"] = sample.get("regime")
    def running(point_at):
        rpm = historian.latest_sample("engine.rpm", at=point_at, fresh_only=False)
        return _qualified(rpm, "engine.rpm", point_at, 5) and rpm["value"] > 400
    if rule["engine_running"] and not running(at):
        result["reason"] = "Fresh engine-running evidence is absent"
        return result
    def matches(value):
        return (value < rule["threshold"] if rule["operator"] == "below" else
                (abs(value) if rule["operator"] == "absolute_above" else value) > rule["threshold"])
    result.update(state="normal", reason="Inside the owner-approved monitoring reference")
    if not matches(sample["value"]):
        return result
    points = historian.recent_numeric_samples(metric, regime=sample["regime"], trip_id=sample.get("trip_id"),
        at=at, limit=rule["persistence_observations"], quality=sample["quality"],
        source=sample["source"], provenance=sample["provenance"])
    seen = set()
    previous_age = _age(sample, at)
    for point in points:
        age = _age(point, at)
        if (not _qualified(point, metric, at, rule["window_seconds"]) or age < previous_age
                or age - previous_age > rule["max_age_seconds"] or not matches(point["value"])):
            break
        if rule["engine_running"] and not running(datetime.fromisoformat(point["observed_at"].replace("Z", "+00:00"))):
            break
        seen.add(point["observed_at"])
        previous_age = age
    persistent = len(seen) >= rule["persistence_observations"]
    result["persistence"].update(observed=len(seen), satisfied=persistent)
    result.update(state="warning" if persistent else "watch", notification_eligible=persistent,
                  reason="Outside the owner-approved reference" + (" persistently" if persistent else "; awaiting persistence"))
    return result
