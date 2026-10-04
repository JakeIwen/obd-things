"""Same-axle tire-pressure asymmetry evaluation."""
from __future__ import annotations

import bisect
from datetime import timedelta
from typing import Callable, Mapping, Sequence

from lib.timeutil import finite_number as _numeric

from .payloads import _baseline_dict, _pair_summary
from .rules import (
    AXLE_NAMES,
    AbsoluteRule,
    PAIR_CLEAR_OBSERVATIONS,
    PAIR_COMPANION_MAX_AGE_SECONDS,
    PAIR_MOVING_SPEED_MPH,
    PAIR_OFFSET_DIMENSIONS,
    PAIR_OFFSET_LOOKBACK_DAYS,
    PAIR_OFFSET_MINIMUM_BUCKETS,
    PAIR_OFFSET_MINIMUM_TRIPS,
    PAIR_ROLLING_TRUST_SECONDS,
    PAIR_SPEED_MAX_AGE_SECONDS,
    WHEEL_LABELS,
    _EPOCH,
)
from .samples import (
    _Tick,
    _absolute_runs,
    _companion_at,
    _observation_rows,
    _observed_us,
    _rule_lookbacks,
    _sample_age_seconds,
)


class TirePairsMixin:
    """Same-axle tire comparison and motion evidence using shared evaluator state."""

    def _pair_motion(
        self,
        tick: _Tick,
        anchor: Mapping[str, object],
    ) -> Callable[[int], dict[str, object] | None]:
        """Moving-segment lookup for the pair rule (see PAIR_* constants).

        Returns ``at(us)``.  It gives None when no fresh vehicle.speed of at
        least PAIR_MOVING_SPEED_MPH is at most PAIR_SPEED_MAX_AGE_SECONDS old
        at that moment.  That covers a stopped van and a stale speed alike.
        Otherwise it gives the speed and the start of the unbroken moving
        segment.  A slower reading, or a speed gap longer than the max age,
        ends a segment.
        """

        key = ("pair_motion", anchor.get("trip_id"))
        cached = tick.conditions.get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        # Oldest first: (observed_us, mph).
        speeds = list(reversed(self._companion_values(
            tick, "vehicle.speed", anchor,
            lookback=self._lookbacks.get("vehicle.speed", 0.0),
        )))
        max_age_us = PAIR_SPEED_MAX_AGE_SECONDS * 1_000_000
        starts: list[int] = []  # segment start for each moving speed sample
        segment_start: int | None = None
        previous_us: int | None = None
        for observed_us, mph in speeds:
            if mph < PAIR_MOVING_SPEED_MPH:
                segment_start = None
            elif (
                segment_start is None
                or previous_us is None
                or observed_us - previous_us > max_age_us
            ):
                segment_start = observed_us
            starts.append(segment_start)  # type: ignore[arg-type]
            previous_us = observed_us
        stamps = [observed_us for observed_us, _mph in speeds]

        def at(point_us: int) -> dict[str, object] | None:
            index = bisect.bisect_right(stamps, point_us) - 1
            if index < 0 or point_us - stamps[index] > max_age_us:
                return None
            if starts[index] is None:
                return None
            return {"speed": speeds[index][1], "segment_start_us": starts[index]}

        tick.conditions[key] = at
        return at

    @staticmethod
    def _wheel_fresh(
        values: Sequence[tuple[int, float]],
        point_us: int,
        segment_start_us: int,
    ) -> bool:
        """Whether a wheel's reading at ``point_us`` was sent in this segment.

        ``values`` are newest first.  A value that changed after the segment
        began proves a new transmission.  So does moving without a stop for
        PAIR_ROLLING_TRUST_SECONDS.  With no reading before the segment, the
        first reading after its start stands in as the starting value.
        """

        if point_us - segment_start_us >= PAIR_ROLLING_TRUST_SECONDS * 1_000_000:
            return True
        inside = [
            value for observed_us, value in values
            if segment_start_us < observed_us <= point_us
        ]
        before = next(
            (value for observed_us, value in values if observed_us <= segment_start_us),
            None,
        )
        reference = before if before is not None else (inside[-1] if inside else None)
        return reference is not None and any(
            abs(value - reference) >= 0.05 for value in inside
        )

    def _pair_axle(
        self,
        rule: AbsoluteRule,
        tick: _Tick,
        left: str,
        right: str,
    ) -> dict[str, object]:
        left_metric = f"{rule.metric}.{left}"
        right_metric = f"{rule.metric}.{right}"
        samples = {}
        for wheel, metric in ((left, left_metric), (right, right_metric)):
            sample = self._latest(tick, metric)
            if sample is None or not _numeric(sample.get("value")):
                return {"state": "unavailable", "reason": f"no fresh {WHEEL_LABELS[wheel]} reading"}
            age = _sample_age_seconds(sample, tick.at)
            if age is None or age > rule.max_age_seconds:
                return {"state": "unavailable",
                        "reason": f"the {WHEEL_LABELS[wheel]} reading is too old"}
            if sample.get("unit") != rule.unit or sample.get("quality") not in rule.qualities:
                return {"state": "unavailable",
                        "reason": f"the {WHEEL_LABELS[wheel]} reading is not from a qualified decode"}
            samples[wheel] = (sample, age)
        left_sample, left_age = samples[left]
        right_sample, right_age = samples[right]
        # Rationale: docs/history/early-warning-rationale.md#tire-pair-cached-readings
        stale = {
            wheel: self._wheel_stale(
                tick,
                metric,
                samples[wheel][0],
                self._series(
                    tick, metric, samples[wheel][0],
                    lookback=_rule_lookbacks(rule).get(metric, 0.0),
                ),
            )
            for wheel, metric in ((left, left_metric), (right, right_metric))
        }
        cached = [wheel for wheel in (left, right) if stale[wheel]["stale"]]
        if cached:
            return {
                "state": "not_applicable",
                "reason": (
                    f"the {WHEEL_LABELS[cached[0]]} tire reading has not updated "
                    "since the last drive"
                ),
                "left": left,
                "right": right,
                "axle": AXLE_NAMES.get((left, right), f"{left}-{right}"),
                "sample": left_sample,
                "current": self._current_payload(left_sample, left_age),
                "samples": samples,
                "stale": stale,
            }
        # Stationary and fresh-transmission gates (see the PAIR_* constants).
        right_values = self._companion_values(
            tick, right_metric, left_sample,
            lookback=_rule_lookbacks(rule).get(right_metric, 400.0),
        )
        left_series = self._series(
            tick, left_metric, left_sample,
            lookback=_rule_lookbacks(rule).get(left_metric, 0.0),
        )
        left_values = [
            (stamp, float(point["value"])) for point in left_series
            for stamp in (_observed_us(point),)
            if stamp is not None and _numeric(point.get("value"))
        ]
        motion_at = self._pair_motion(tick, left_sample)

        def fresh_at(point_us: int, motion: Mapping[str, object]) -> dict[str, bool]:
            start = int(motion["segment_start_us"])  # type: ignore[arg-type]
            return {
                left: self._wheel_fresh(left_values, point_us, start),
                right: self._wheel_fresh(right_values, point_us, start),
            }

        stamps = [
            stamp for stamp in (_observed_us(left_sample), _observed_us(right_sample))
            if stamp is not None
        ]
        latest_us = max(stamps) if stamps else tick.at_us
        motion = motion_at(latest_us)
        fresh = fresh_at(latest_us, motion) if motion is not None else {left: False, right: False}
        motion_info = {
            "moving": motion is not None,
            "speed_mph": None if motion is None else motion["speed"],
            "segment_started_at": (
                None if motion is None
                else (_EPOCH + timedelta(microseconds=int(motion["segment_start_us"]))).isoformat()  # type: ignore[arg-type]
            ),
            "fresh": fresh,
        }
        waiting = [wheel for wheel in (left, right) if not fresh[wheel]]
        if motion is None or waiting:
            return {
                "state": "not_applicable",
                "reason": (
                    "the van is stopped or its speed is not current; tire sensors "
                    "report rarely while it stands"
                    if motion is None
                    else f"the {WHEEL_LABELS[waiting[0]]} tire has not reported since "
                    "the van started moving"
                ),
                "left": left,
                "right": right,
                "axle": AXLE_NAMES.get((left, right), f"{left}-{right}"),
                "sample": left_sample,
                "current": self._current_payload(left_sample, left_age),
                "samples": samples,
                "stale": stale,
                "motion": motion_info,
            }
        # Both offsets are conditioned on the same (engine, motion) regime.
        right_probe = {**right_sample, "regime": left_sample["regime"],
                       "trip_id": left_sample.get("trip_id")}
        baselines = {}
        for wheel, metric, probe in (
            (left, left_metric, left_sample),
            (right, right_metric, right_probe),
        ):
            baseline = self._rollup_baseline(
                metric=metric,
                sample=probe,
                at=tick.at,
                lookback_days=PAIR_OFFSET_LOOKBACK_DAYS,
                regime_dimensions=PAIR_OFFSET_DIMENSIONS,
            )
            shortfall = self._baseline_shortfall(
                baseline,
                buckets=PAIR_OFFSET_MINIMUM_BUCKETS,
                trips=PAIR_OFFSET_MINIMUM_TRIPS,
            )
            if shortfall is not None:
                return {
                    "state": "insufficient_history",
                    "reason": f"{WHEEL_LABELS[wheel]}: {shortfall}",
                    "sample": left_sample,
                    "current": self._current_payload(left_sample, left_age),
                    "baseline": baseline,
                    "samples": samples,
                    "stale": stale,
                }
            baselines[wheel] = baseline
        offset = baselines[left].median - baselines[right].median
        warning = float(rule.warning_threshold)  # type: ignore[arg-type]

        def accept(point: Mapping[str, object]) -> tuple:
            point_us = _observed_us(point)
            partner = (
                None if point_us is None
                else _companion_at(right_values, point_us, PAIR_COMPANION_MAX_AGE_SECONDS)
            )
            if partner is None:
                return ("gap", warning, None, 0.0)
            # A stationary reading, or one a wheel has not re-sent since the
            # van started moving, is gate-unmet ("fail").  It ends both runs
            # but does not contradict a recovery (see _absolute_runs).
            point_motion = motion_at(point_us)  # type: ignore[arg-type]
            if point_motion is None or not all(fresh_at(point_us, point_motion).values()):  # type: ignore[arg-type]
                return ("fail", warning, None, 0.0)
            return ("ok", warning, None, abs(float(point["value"]) - partner - offset))

        newest_us = _observed_us(left_sample)
        runs = _absolute_runs(
            left_series,
            newest_us=newest_us if newest_us is not None else tick.at_us,
            accept=accept,
            direction="high",
            window_seconds=rule.persistence_window_seconds,
            clear_margin=rule.clear_margin,
        )
        asymmetry = float(left_sample["value"]) - float(right_sample["value"]) - offset
        if abs(asymmetry) < warning:
            # Rationale: docs/history/early-warning-rationale.md#tire-pair-latest-reading
            runs = {**runs, "escalate": 0, "counted": []}
        lower = left if asymmetry < 0 else right
        return {
            "state": "evaluated",
            "left": left,
            "right": right,
            "axle": AXLE_NAMES.get((left, right), f"{left}-{right}"),
            "sample": samples[lower][0],
            "current": self._current_payload(*samples[lower]),
            "lower": lower,
            "offset": offset,
            "asymmetry": asymmetry,
            "value": abs(asymmetry),
            "past": abs(asymmetry) >= warning,
            "baselines": baselines,
            "left_value": float(left_sample["value"]),
            "right_value": float(right_sample["value"]),
            "samples": samples,
            "stale": stale,
            "motion": motion_info,
            **runs,
            "runs": runs,
        }

    def _evaluate_tire_pair(
        self,
        rule: AbsoluteRule,
        tick: _Tick,
        *,
        refresh: bool,
    ) -> dict[str, object]:
        if refresh:
            self.historian.refresh_rollups(through=tick.at)
        base = self._absolute_rule_base(rule)
        required = rule.persistence_observations
        results = {
            f"{left}{right}": self._pair_axle(rule, tick, left, right)
            for left, right in rule.gate_params
        }
        grouped = self._group_absolute(
            rule,
            tick,
            results,
            severity_key=lambda result: float(result["value"]),
            clear_required=PAIR_CLEAR_OBSERVATIONS,
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
            confidence = "high"
            reason = "the two tires on one axle stayed further apart than usual"
        elif state == "normal" and past:
            core = grouped["evaluated"][past[0]]
            counted = core.get("counted") or []
            first = counted[-1].get("observed_at") if counted else None
            candidate = self._candidate(
                rule.key,
                at_us=tick.at_us,
                first_seen_at=first if isinstance(first, str) else None,
                observed=max(int(grouped["evaluated"][name]["escalate"]) for name in past),
                required=required,
                window_seconds=rule.persistence_window_seconds,
            )
            confidence = "low"
            reason = "the two tires on one axle are further apart than usual; waiting to confirm"
        elif state == "normal" and grouped["sticky"]:
            reason = "the axle's tires are back as close as usual long enough to clear"
        elif state == "normal":
            reason = "each axle's tires are as close as usual"
        elif state == "recovering":
            reason = "the axle's tires are back as close as usual; confirming over more readings"
        else:
            reason = (
                str(result.get("reason")) if isinstance(result, Mapping) and result.get("reason")
                else "no axle could be compared"
            )
        evaluated = result if isinstance(result, Mapping) and result.get("state") == "evaluated" else None
        action_axles = named or (past if state == "normal" else [])
        metric = f"{rule.metric}.{evaluated['lower']}" if evaluated else rule.metric
        current = result.get("current") if isinstance(result, Mapping) else None
        opened_metric = grouped["opened"].get("metric")
        if (
            grouped["sticky"]
            and state != "warning"
            and isinstance(opened_metric, str)
            and isinstance(result, Mapping)
        ):
            # Describe the wheel that was lower when the event opened, even
            # if the other wheel now reads lower, so the timed recovery gate
            # compares the same sensor.
            opened_wheel = opened_metric.rsplit(".", 1)[-1]
            held_sample = (result.get("samples") or {}).get(opened_wheel)
            if held_sample is not None:
                metric = opened_metric
                current = self._current_payload(*held_sample)
        payload: dict[str, object] = {
            **base,
            "metric": metric,
            "state": state,
            "reason": reason,
            "notification_eligible": state == "warning",
            "severity": grouped["severity"],
            "confidence": confidence,
            "current": current,
            "regime": (
                result["sample"].get("regime")  # type: ignore[union-attr]
                if isinstance(result, Mapping) and isinstance(result.get("sample"), Mapping)
                else None
            ),
            "baseline": None,
            "persistence": {
                "required": required,
                "observed": int(runs["escalate"]) if runs else 0,
                "window_seconds": rule.persistence_window_seconds,
                "satisfied": bool(runs) and int(runs["escalate"]) >= required,
                "companion_gaps": int(runs["companion_gaps"]) if runs else 0,
                "observations": _observation_rows(runs["counted"]) if runs else [],
            },
            "hysteresis": {
                "clear_margin": rule.clear_margin,
                "clear_threshold": float(rule.warning_threshold) - rule.clear_margin,  # type: ignore[arg-type]
                "clear_observed": int(runs["clear"]) if runs else 0,
                "required": PAIR_CLEAR_OBSERVATIONS,
            },
            "pair": None if not evaluated else {
                "axle": evaluated["axle"],
                "left": evaluated["left"],
                "right": evaluated["right"],
                "left_value": evaluated["left_value"],
                "right_value": evaluated["right_value"],
                "usual_offset": evaluated["offset"],
                "asymmetry": evaluated["asymmetry"],
                "lower_wheel": evaluated["lower"],
                "summary": _pair_summary(evaluated),
            },
            # The per-wheel baselines are the second source; listing them as
            # paired companions lets the event history archive their inputs.
            "corroborators": [] if not evaluated else [
                {
                    "metric": f"{rule.metric}.{wheel}",
                    "state": "paired",
                    "baseline": _baseline_dict(evaluated["baselines"][wheel]),
                }
                for wheel in (evaluated["left"], evaluated["right"])
            ],
            "axle_assessments": {
                name: {
                    "state": (
                        "warning" if name in named
                        else "normal" if item.get("state") == "evaluated"
                        else item.get("state")
                    ),
                    "reason": item.get("reason"),
                    "axle": item.get("axle"),
                    "asymmetry": item.get("asymmetry"),
                    "usual_offset": item.get("offset"),
                    "observed": item.get("escalate", 0),
                    "clear_observed": item.get("clear", 0),
                    "companion_gaps": item.get("companion_gaps", 0),
                    **({"stale": item["stale"]} if item.get("stale") is not None else {}),
                    **({"motion": item["motion"]} if item.get("motion") is not None else {}),
                }
                for name, item in results.items()
            },
            "axles_past_threshold": [results[name]["axle"] for name in past],
            "held_keys": grouped["held"],
            "action_wheels": [
                wheel for name in action_axles
                for wheel in (results[name]["left"], results[name]["right"])
            ],
        }
        if action_axles:
            axle = results[action_axles[0]]["axle"]
            payload["action"] = f"Check both {axle} tires at the next stop."
            payload["title"] = f"{str(axle).capitalize()} tires uneven"
        if candidate is not None:
            payload["candidate"] = candidate
        return payload
