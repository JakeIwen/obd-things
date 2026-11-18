"""Owner-approved threshold rules. Data configuration only; never code or CAN.

A rule is defined in time (contract: ``docs/custom-warnings.md``). It confirms when every one of
the Pi's recordings of the metric for at least ``window_seconds`` (the hold time) was beyond the
limit, never more than ``max_age_seconds`` apart, carrying at least
``persistence_observations`` separate readings; with ``engine_running`` every one of those
recordings also showed the engine running. The hold is timed by ``RuleHolds`` on the broker's
monotonic recording clock, one stored recording at a time, so wall-clock steps and the order in
which cached readings were observed cannot lengthen or shorten it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
import threading

from projects.vehicle_data import custom_warning_contract as contract
from projects.vehicle_data.metrics import METRICS

DEFAULT_RULES_PATH = Path("/var/lib/van-telemetry-advisor/early-warnings.json")
MAX_RULES = contract.MAX_RULES
FIELDS = contract.FIELDS
EVALUATOR_REVISION = contract.EVALUATOR_REVISION
RPM_METRIC = "engine.rpm"


def catalog_from(definitions):
    """Owner-rule metrics among public metric definitions: numeric, with an established source."""
    return {name: entry for name, entry in definitions.items()
            if entry.get("value_type") == "number"
            and any(source.get("quality") in contract.CATALOG_QUALITIES for source in entry.get("sources", ()))}


def catalog():
    return catalog_from({name: definition.public_dict() for name, definition in METRICS.items()})


def _in_catalog(metric):
    """``metric in catalog()`` without building every public definition (evaluation hot path)."""
    definition = METRICS.get(metric) if isinstance(metric, str) else None
    return (definition is not None and definition.value_type == "number"
            and any(source.quality in contract.CATALOG_QUALITIES for source in definition.sources))


def catalog_summary(entries):
    """The few fields the setup dialog needs from a chat's captured catalog (metric key, unit).

    The dashboard names each metric itself (its ``metricLabel``); definitions carry no label.
    """
    if not isinstance(entries, dict):
        return []
    rows = [{"metric": name, "unit": str(entry.get("unit", ""))}
            for name, entry in entries.items() if isinstance(entry, dict)]
    return sorted(rows, key=lambda row: row["metric"])


def validate_rule(rule):
    if not isinstance(rule, dict) or set(rule) != FIELDS:
        raise ValueError("A warning must contain exactly the supported threshold-rule fields")
    low, high = contract.LIMITS["title_chars"]
    if not isinstance(rule["title"], str) or not low <= len(rule["title"].strip()) <= high:
        raise ValueError(f"Use a warning title between {low} and {high} characters")
    if not _in_catalog(rule["metric"]):
        raise ValueError("Choose an established numeric telemetry metric")
    if rule["operator"] not in contract.OPERATORS:
        raise ValueError("Choose above, below or absolute_above")
    for key in ("threshold", "window_seconds", "max_age_seconds", "persistence_observations"):
        low, high = contract.LIMITS[key]
        value = rule[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
    if type(rule["persistence_observations"]) is not int or type(rule["engine_running"]) is not bool:
        raise ValueError("Persistence must be an integer and engine_running a boolean")
    if rule["operator"] == "absolute_above" and rule["threshold"] < 0:
        raise ValueError("An absolute-angle threshold cannot be negative")
    if rule["max_age_seconds"] > rule["window_seconds"]:
        raise ValueError("The longest time between readings cannot exceed the hold time")
    return dict(rule, title=rule["title"].strip())


def load_rules(path):
    """Strict load: every row must validate. The broker reads tolerantly (``custom_warning_file``)."""
    from projects.vehicle_data import custom_warning_file
    rules_file = custom_warning_file.read(path)
    if rules_file.rejected:
        raise ValueError("A saved custom warning is invalid")
    return rules_file.rows


def advice(rule, interval_seconds):
    """Owner advice when the longest gap is under twice the reported recording interval."""
    if (isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds) or interval_seconds <= 0):
        return None
    gap = contract.ADVICE_GAP_INTERVAL_MULTIPLE * interval_seconds
    if rule["max_age_seconds"] >= gap:
        return None
    return (f"The Pi records about every {interval_seconds:g} s right now, more slowly when busy; "
            f"a longest time between readings under {gap:g} s may rarely be met.")


@dataclass
class _Run:
    revision: str
    since: float
    last: float
    last_snapshot_id: int
    trip_id: object
    started_at: str
    readings: list = field(default_factory=list)


@dataclass(frozen=True)
class Hold:
    held_seconds: float = 0.0
    readings: int = 0
    started_at: str | None = None


class RuleHolds:
    """Per-rule runs of beyond-limit recordings, in memory, on the broker's recording clock.

    Written only by the evaluation of a stored recording (the broker's history thread); any
    other evaluation only peeks. A broker restart starts every hold again.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._runs: dict[str, _Run] = {}

    def retain(self, rule_ids):
        """Drop the runs of rules that are no longer loaded (removed or rejected)."""
        with self._lock:
            for rule_id in set(self._runs) - set(rule_ids):
                del self._runs[rule_id]

    def observe(self, rule_id, revision, *, recording, snapshot, reading_key, gap, floor):
        """Advance the rule's run with one stored recording; ``reading_key`` None ends it."""
        snapshot_id, clock = recording
        with self._lock:
            run = self._runs.get(rule_id)
            if reading_key is None:
                self._runs.pop(rule_id, None)
                return Hold()
            if run is not None and run.revision == revision and run.last_snapshot_id == snapshot_id:
                return self._hold(run)  # the same recording evaluated again changes nothing
            continues = (
                run is not None and run.revision == revision
                and snapshot["previous_snapshot_id"] is not None
                and snapshot["previous_snapshot_id"] == run.last_snapshot_id
                and 0 <= clock - run.last <= gap
                and snapshot["trip_id"] == run.trip_id
            )
            if continues:
                run.last = clock
                run.last_snapshot_id = snapshot_id
            else:
                run = _Run(revision, clock, clock, snapshot_id, snapshot["trip_id"], snapshot["captured_at"])
                self._runs[rule_id] = run
            if reading_key not in run.readings and len(run.readings) < floor:
                run.readings.append(reading_key)
            return self._hold(run)

    def peek(self, rule_id, revision, snapshot_id):
        """The hold as of ``snapshot_id`` when it is the run's latest recording, else none."""
        with self._lock:
            run = self._runs.get(rule_id)
            if run is None or run.revision != revision or run.last_snapshot_id != snapshot_id:
                return Hold()
            return self._hold(run)

    @staticmethod
    def _hold(run):
        return Hold(float(run.last - run.since), len(run.readings), run.started_at)


def _effective_age(reading, at_us):
    """Recorded source age plus the time since the recording, as the built-in rules measure it."""
    age_ms = reading.get("source_age_ms")
    if isinstance(age_ms, bool) or not isinstance(age_ms, (int, float)) or not math.isfinite(age_ms) or age_ms < 0:
        return None
    return age_ms / 1000 + max(0, at_us - reading["captured_us"]) / 1_000_000


def _qualified(reading, metric, at_us, max_age):
    """Fresh at capture, finite, catalog unit and source, and no older than ``max_age`` at ``at``."""
    if not reading or type(reading.get("value")) not in (float, int) or not math.isfinite(reading["value"]):
        return None
    definition = METRICS[metric]
    if (reading.get("freshness") != "fresh" or reading.get("unit") != definition.unit
            or not any(s.name == reading.get("source") and s.quality == reading.get("quality")
                       and s.quality in contract.CATALOG_QUALITIES for s in definition.sources)):
        return None
    age = _effective_age(reading, at_us)
    return age if age is not None and age <= max_age else None


def _open_warning(historian, rule_key):
    """Whether the rule has an open episode whose current state is warning."""
    lister = getattr(historian, "list_advisory_episodes", None)
    if lister is None:
        return False
    try:
        episodes = lister(active_only=True, limit=200)
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return False
    return any(isinstance(item, dict) and item.get("rule") == rule_key
               and item.get("state") == "warning" for item in episodes or ())


def _beyond(rule, value):
    if rule["operator"] == "below":
        return value < rule["threshold"]
    return (abs(value) if rule["operator"] == "absolute_above" else value) > rule["threshold"]


def evaluate_rule(historian, row, at, *, holds, snapshot, recording=None):
    """Assess one rule at ``at`` from ``snapshot`` (``recording_readings`` at ``at``).

    ``recording`` is ``(snapshot_id, recording_clock_seconds)`` when ``at`` is the evaluation of
    that newly stored snapshot; only then does the rule's hold change.
    """
    from projects.vehicle_data.event_history import digest
    rule = validate_rule(row["rule"])
    metric = rule["metric"]
    revision = digest(rule)
    rule_key = "custom_" + row["id"]
    result = {"rule_snapshot": rule, "rule_revision": revision, "evaluator_revision": EVALUATOR_REVISION,
              "rule": rule_key, "title": rule["title"], "metric": metric,
              "category": "vehicle_health", "severity": "warning", "advisory": True,
              "direction": "low" if rule["operator"] == "below" else "high",
              "state": "unavailable", "reason": "No current reading",
              "notification_eligible": False, "notification_rate_limit_seconds": 1800,
              "tier": 1, "group": "custom", "action": "Check it at the next stop.", "confidence": None,
              "interpretation": "Owner-approved monitoring reference, not an OEM limit or diagnosis",
              "custom_rule": rule, "current": None, "baseline": None, "deviation": None,
              "persistence": {"required": rule["persistence_observations"], "observed": 0,
                              "window_seconds": rule["window_seconds"], "hold_seconds": rule["window_seconds"],
                              "held_seconds": 0.0, "satisfied": False}}
    at_us = round(at.timestamp() * 1_000_000)
    observing = recording is not None and snapshot is not None and snapshot["snapshot_id"] == recording[0]
    reading = snapshot["readings"].get(metric) if snapshot is not None else None
    age = _qualified(reading, metric, at_us, METRICS[metric].stale_after_seconds)
    beyond_key = None
    if age is not None:
        result["current"] = dict(reading, effective_age_seconds=age)
        result["regime"] = snapshot["regime"]
        rpm = snapshot["rpm"]
        running = (_qualified(rpm, RPM_METRIC, at_us, contract.ENGINE_RUNNING_RPM_MAX_AGE_SECONDS) is not None
                   and rpm["value"] > contract.ENGINE_RUNNING_RPM_ABOVE)
        if rule["engine_running"] and not running:
            result["reason"] = "Fresh engine-running evidence is absent"
        else:
            result.update(state="normal", reason="Inside the owner-approved monitoring reference")
            if _beyond(rule, reading["value"]):
                beyond_key = (reading["source"], reading["observed_us"])
    if observing:
        hold = holds.observe(row["id"], revision, recording=recording, snapshot=snapshot,
                             reading_key=beyond_key, gap=rule["max_age_seconds"],
                             floor=rule["persistence_observations"])
    elif beyond_key is not None:
        hold = holds.peek(row["id"], revision, snapshot["snapshot_id"])
    else:
        hold = Hold()
    if beyond_key is None:
        return result
    persistent = (hold.held_seconds >= rule["window_seconds"]
                  and hold.readings >= rule["persistence_observations"])
    result["persistence"].update(observed=hold.readings, held_seconds=hold.held_seconds, satisfied=persistent)
    if persistent:
        result.update(state="warning", notification_eligible=True, confidence="medium",
                      reason="Outside the owner-approved reference persistently")
        return result
    if _open_warning(historian, rule_key):
        # A confirmed owner warning holds while the reading stays outside the
        # reference; a restarted hold (gap, missed recording, broker restart) is
        # not recovery. "normal" here would close the event and reopen it later.
        result.update(state="warning", notification_eligible=True, confidence="medium",
                      reason="Still outside the owner-approved reference")
        return result
    # An unconfirmed reading never opens an episode: it stays normal and
    # carries a low-confidence candidate until the hold is met.
    result.update(state="normal", notification_eligible=False, confidence="low",
                  reason="Outside the owner-approved reference; waiting to confirm",
                  candidate={"observed": hold.readings, "required": rule["persistence_observations"],
                             "window_seconds": float(rule["window_seconds"]),
                             "held_seconds": hold.held_seconds, "hold_seconds": float(rule["window_seconds"]),
                             "first_seen_at": hold.started_at or snapshot["captured_at"], "confidence": "low"})
    return result


def evaluate_rules(historian, rows, at, *, holds, recording=None, failed=None):
    """Assess every loaded rule with one readings query; a recording advances their holds.

    A rule whose own assessment raises is left out and its id appended to ``failed``, so it
    stops alone (its open episode retires as a rejected rule's does) and the others continue.
    """
    if recording is not None:
        holds.retain(row["id"] for row in rows)
    if not rows:
        return []
    snapshot = historian.recording_readings({row["rule"]["metric"] for row in rows}, at=at)
    assessments = []
    for row in rows:
        try:
            assessments.append(evaluate_rule(historian, row, at, holds=holds, snapshot=snapshot,
                                             recording=recording))
        except (ArithmeticError, KeyError, TypeError, ValueError):
            if failed is not None:
                failed.append(row["id"])
    return assessments
