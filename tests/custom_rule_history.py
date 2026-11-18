"""A real historian, ingested as the broker does, for owner custom-rule tests.

``CustomRuleHistory.record`` stores one snapshot through ``TelemetryInsights.ingest_snapshot`` with
an explicit capture time and an independent recording-clock time (so wall-clock steps can be
staged), per-metric observation lag and source, and unavailable entries; the evaluation of that
stored snapshot is the custom-rule assessment it returns.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile

from projects.vehicle_data.early_warning import EarlyWarningEvaluator
from projects.vehicle_data.historian import OutOfOrderSnapshotError, TelemetryHistorian
from projects.vehicle_data.insights import TelemetryInsights
from projects.vehicle_data.metrics import METRICS

START = datetime(2026, 11, 17, 18, 0, tzinfo=timezone.utc)
RPM_SOURCE = "ccan.broadcast.0x0fc"


class RecordingEvaluator(EarlyWarningEvaluator):
    """The production evaluator; keeps the last result so a test can read what a recording saw."""

    def evaluate(self, *, at=None, recording=None):
        self.last = super().evaluate(at=at, recording=recording)
        return self.last


def source_of(metric, source=None):
    definition = METRICS[metric]
    chosen = source or definition.sources[0].name
    match = next(s for s in definition.sources if s.name == chosen)
    return match


def reading(metric, value, *, lag=0.0, source=None, stale=False, unit=None):
    """A broker metric entry observed ``lag`` seconds before capture (filled in by ``record``)."""
    return {"metric": metric, "value": value, "lag": lag, "source": source, "stale": stale, "unit": unit}


def unavailable(metric):
    return {"metric": metric, "unavailable": True}


class CustomRuleHistory:
    def __init__(self, rules=(), *, evaluator_rules=(), episodes=True):
        self.tmp = tempfile.TemporaryDirectory(prefix="custom-rule-history-")
        root = Path(self.tmp.name)
        self.rules_path = root / "early-warnings.json"
        self.historian = TelemetryHistorian(root / "history.sqlite3")
        # evaluator_rules None runs the built-in rules too; the default () runs custom rules only.
        built_ins = {} if evaluator_rules is None else {"rules": evaluator_rules}
        self.evaluator = RecordingEvaluator(self.historian, custom_rules_path=self.rules_path, **built_ins)
        self.insights = TelemetryInsights(self.historian, warning_evaluator=self.evaluator,
                                          dtc_cache_path=root / "dtcs.json",
                                          maintenance_check_interval_seconds=10**9)
        # episodes=False skips advisory persistence: the historian stores the snapshot and the
        # evaluator assesses it with the recording exactly as insights passes them (long timing runs).
        self.episodes = episodes
        self.sequence = 0
        self.rows = []
        for rule in rules:
            self.add(rule)

    def close(self):
        self.historian.close()
        self.tmp.cleanup()

    def add(self, rule, identifier=None):
        identifier = identifier or format(len(self.rows) + 1, "064x")
        self.rows.append({"id": identifier, "rule": dict(rule)})
        self.save()
        return identifier

    def save(self, payload=None):
        self.rules_path.write_text(json.dumps(payload if payload is not None else {"version": 1, "rules": self.rows}))

    def snapshot(self, captured, entries, *, running=True):
        names = sorted({entry["metric"] for entry in entries})
        metrics = {}
        for entry in entries:
            name = entry["metric"]
            if entry.get("unavailable"):
                metrics[name] = {"metric": name, "available": False, "unit": METRICS[name].unit,
                                 "reason": "source_unavailable", "detail": "test gap"}
                continue
            source = source_of(name, entry.get("source"))
            observed = captured - timedelta(seconds=entry["lag"])
            metrics[name] = {"metric": name, "available": True, "unit": entry.get("unit") or METRICS[name].unit,
                             "value": entry["value"], "source": source.name, "bus": source.bus,
                             "acquisition": "passive", "interface_mode": "listen_only",
                             "quality": source.quality, "observed_at": observed.isoformat(),
                             "age_ms": int(round(entry["lag"] * 1000)), "stale": entry.get("stale", False)}
        status = {"vehicle_state": {"state": "running" if running else "unknown",
                                    "running": True if running else None,
                                    "confidence": "verified" if running else "unknown",
                                    "basis": "test_rpm" if running else "no_evidence",
                                    "observed_at": captured.isoformat() if running else None,
                                    "age_ms": 0 if running else None}}
        return {"status": status, "catalog": [METRICS[name].public_dict() for name in names], "metrics": metrics}

    def record(self, seconds, entries, *, clock=None, running=True, rule_id=None):
        """Store a snapshot captured ``seconds`` after START (recording clock ``clock``, default the same).

        Returns the custom assessment of ``rule_id`` (default the first rule) from that recording's
        evaluation, or ``None`` when the historian refused the capture or did not evaluate it.
        """
        captured = START + timedelta(seconds=seconds)
        self.sequence += 1
        self.evaluator.last = None
        payload = self.snapshot(captured, entries, running=running)
        clock = seconds if clock is None else clock
        try:
            if self.episodes:
                self.insights.ingest_snapshot(payload, captured_at=captured, ingest_key=f"test:{self.sequence}",
                                              recording_clock=clock)
            else:
                stored = self.historian.ingest_snapshot(payload, captured_at=captured,
                                                        ingest_key=f"test:{self.sequence}")
                self.evaluator.evaluate(at=captured, recording=(stored.snapshot_id, clock))
        except OutOfOrderSnapshotError:
            return None
        return self.assessment(self.evaluator.last, rule_id)

    def assessment(self, result, rule_id=None):
        if result is None:
            return None
        if rule_id is None and not self.rows:
            return None
        key = "custom_" + (rule_id or self.rows[0]["id"])
        return next((item for item in result["assessments"] if item["rule"] == key), None)

    def health(self, seconds, rule_id=None):
        """An evaluation that is not a recording (as ``/v1/health``) at START + ``seconds``."""
        result = self.evaluator.evaluate(at=START + timedelta(seconds=seconds))
        return self.assessment(result, rule_id), result


def rpm(value=800, *, lag=0.0):
    return reading("engine.rpm", value, lag=lag, source=RPM_SOURCE)


# Owner rules and readings shared by the custom-rule tests.
CCAN = "ccan.broadcast.0x41a"
BCAN = "bcan.broadcast.0x46c"
RULE = {"title": "Low supply voltage", "metric": "battery.voltage", "operator": "below",
        "threshold": 11.8, "persistence_observations": 3, "window_seconds": 60,
        "max_age_seconds": 10, "engine_running": False}
BATTERY = {"title": "Low supply voltage", "metric": "battery.voltage", "operator": "below",
           "threshold": 12.0, "persistence_observations": 2, "window_seconds": 30,
           "max_age_seconds": 15, "engine_running": False}
COOLANT = {"title": "Coolant above 230 °F", "metric": "engine.coolant_temperature", "operator": "above",
           "threshold": 230, "persistence_observations": 2, "window_seconds": 15,
           "max_age_seconds": 15, "engine_running": True}
FIELD_DUTY = {"title": "Field duty high", "metric": "generator.field_duty", "operator": "above",
              "threshold": 95, "persistence_observations": 2, "window_seconds": 30,
              "max_age_seconds": 15, "engine_running": False}


def battery_rule(**changes):
    """BATTERY with ``changes``; the gap follows the form's min(15, hold) unless given."""
    rule = dict(BATTERY, **changes)
    if "max_age_seconds" not in changes:
        rule["max_age_seconds"] = min(15, rule["window_seconds"])
    return rule


def battery(value=11.0, *, lag=0.1, source=CCAN, **extra):
    return reading("battery.voltage", value, lag=lag, source=source, **extra)


def coolant(value=240, *, lag=0.1, **extra):
    return reading("engine.coolant_temperature", value, lag=lag, **extra)
