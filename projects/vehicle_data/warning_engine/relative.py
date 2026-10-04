"""Tier-1 history-relative warning evaluation."""
from __future__ import annotations

from datetime import datetime
from typing import Mapping, Sequence

from lib.timeutil import finite_number as _numeric
from projects.vehicle_data import warning_context
from projects.vehicle_data.historian import BaselineStats, project_regime

from .payloads import _baseline_dict
from .rules import (
    COOLANT_RATE_ACTION,
    CorroborationRule,
    GENERATOR_DUTY_METRIC,
    SLOW_MOTIONS,
    WarningRule,
)
from .samples import (
    _Tick,
    _effect,
    _engine,
    _motion,
    _observation_rows,
    _observed_us,
    _project,
    _relative_runs,
    _rule_lookbacks,
    _sample_age_seconds,
    _slope,
    _threshold,
    _utc,
)


class RelativeMixin:
    """Like-for-like relative rules using the owning evaluator's tick and baselines."""

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
