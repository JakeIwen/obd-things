"""Relative and absolute tire-pressure evaluation."""
from __future__ import annotations

from datetime import timedelta
from typing import Callable, Mapping, Sequence

from lib.timeutil import finite_number as _numeric
from projects.vehicle_data import warning_context
from projects.vehicle_data.historian import BaselineStats

from .rules import (
    AbsoluteRule,
    INCONCLUSIVE_PRIORITY,
    TireGroupRule,
    _EPOCH,
)
from .samples import (
    _Tick,
    _absolute_runs,
    _labels,
    _observation_rows,
    _observed_us,
    _past,
    _rule_lookbacks,
    _sample_age_seconds,
    _to_us,
    _utc,
)


class TiresMixin:
    """Per-wheel and grouped tire rules using shared evaluator state."""

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
