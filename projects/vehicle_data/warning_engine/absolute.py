"""Tier-0 absolute warning evaluation."""
from __future__ import annotations

from datetime import datetime
from typing import Mapping

from lib.timeutil import finite_number as _numeric

from .rules import AbsoluteOilPressureRule, AbsoluteRule, RUNNING_LOOKBACK_SECONDS
from .samples import (
    _Tick,
    _absolute_runs,
    _engine,
    _observation_rows,
    _observed_us,
    _rule_lookbacks,
    _sample_age_seconds,
    _to_us,
    _utc,
)


class AbsoluteMixin:
    """Absolute rules, retaining the independent qualified oil-pressure path."""

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

    @staticmethod
    def _absolute_oil_persistence(
        rule: AbsoluteOilPressureRule,
    ) -> dict[str, object]:
        return {
            "required": rule.persistence_observations,
            "observed": 0,
            "window_seconds": rule.persistence_window_seconds,
        }

    def _absolute_oil_rpm_context(
        self,
        rule: AbsoluteOilPressureRule,
        tick: _Tick,
        at: datetime,
        base: Mapping[str, object],
    ) -> dict[str, object]:
        rpm = self._latest(tick, rule.running_metric)
        rpm_age = (
            _sample_age_seconds(rpm, at)
            if isinstance(rpm, dict)
            and isinstance(rpm.get("value"), (int, float))
            else None
        )
        if rpm_age is None or rpm_age > rule.max_age_seconds:
            return {"terminal": {
                **base,
                "state": "unavailable",
                "reason": "fresh positive RPM evidence is unavailable",
                "current": None,
                "running_evidence": None,
                "persistence": self._absolute_oil_persistence(rule),
            }}
        assert rpm is not None
        rpm_payload = self._current_payload(rpm, rpm_age)
        if (
            rpm.get("source") != rule.running_source
            or rpm.get("quality") != rule.running_quality
            or rpm.get("unit") != rule.running_unit
        ):
            return {"terminal": {
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
                "persistence": self._absolute_oil_persistence(rule),
            }}
        if float(rpm["value"]) < rule.running_rpm_threshold:
            return {"terminal": {
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
                "persistence": self._absolute_oil_persistence(rule),
            }}
        return {"rpm": rpm, "rpm_payload": rpm_payload}

    def _absolute_oil_running_context(
        self,
        rule: AbsoluteOilPressureRule,
        tick: _Tick,
        base: Mapping[str, object],
        rpm: Mapping[str, object],
        rpm_payload: Mapping[str, object],
    ) -> dict[str, object]:
        running = self._continuous(
            tick,
            rpm,
            minimum=rule.running_rpm_threshold,
            lookback=RUNNING_LOOKBACK_SECONDS,
            max_gap=rule.running_max_gap_seconds,
        )
        if running is None or float(running["duration_seconds"]) < rule.startup_grace_seconds:
            return {"terminal": {
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
                "persistence": self._absolute_oil_persistence(rule),
            }}
        return {"running": running}

    def _absolute_oil_sample_context(
        self,
        rule: AbsoluteOilPressureRule,
        tick: _Tick,
        at: datetime,
        base: Mapping[str, object],
        rpm_payload: Mapping[str, object],
        running: Mapping[str, object],
    ) -> dict[str, object]:
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
            return {"terminal": {
                **base,
                "state": "unavailable",
                "reason": "fresh oil-pressure evidence is unavailable",
                "current": None,
                "running_evidence": running_evidence,
                "persistence": self._absolute_oil_persistence(rule),
            }}
        current = self._current_payload(oil, oil_age)
        if (
            oil.get("source") != rule.pressure_source
            or oil.get("quality") != rule.pressure_quality
            or oil.get("unit") != rule.pressure_unit
        ):
            return {"terminal": {
                **base,
                "state": "unavailable",
                "reason": (
                    "oil-pressure evidence does not match the exact qualified "
                    "0x41D source, quality, and psi unit"
                ),
                "current": current,
                "running_evidence": running_evidence,
                "persistence": self._absolute_oil_persistence(rule),
            }}
        return {
            "oil": oil,
            "current": current,
            "running_evidence": running_evidence,
        }

    def _absolute_oil_runs(
        self,
        rule: AbsoluteOilPressureRule,
        tick: _Tick,
        oil: Mapping[str, object],
        running: Mapping[str, object],
    ) -> dict[str, object]:
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
        return _absolute_runs(
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

    def _absolute_oil_payload(
        self,
        rule: AbsoluteOilPressureRule,
        tick: _Tick,
        base: Mapping[str, object],
        oil: Mapping[str, object],
        current: Mapping[str, object],
        running_evidence: Mapping[str, object],
        runs: Mapping[str, object],
    ) -> dict[str, object]:
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
        rpm_context = self._absolute_oil_rpm_context(rule, tick, at, base)
        if "terminal" in rpm_context:
            return rpm_context["terminal"]
        running_context = self._absolute_oil_running_context(
            rule, tick, base, rpm_context["rpm"], rpm_context["rpm_payload"]
        )
        if "terminal" in running_context:
            return running_context["terminal"]
        sample_context = self._absolute_oil_sample_context(
            rule,
            tick,
            at,
            base,
            rpm_context["rpm_payload"],
            running_context["running"],
        )
        if "terminal" in sample_context:
            return sample_context["terminal"]
        runs = self._absolute_oil_runs(
            rule, tick, sample_context["oil"], running_context["running"]
        )
        return self._absolute_oil_payload(
            rule,
            tick,
            base,
            sample_context["oil"],
            sample_context["current"],
            sample_context["running_evidence"],
            runs,
        )
