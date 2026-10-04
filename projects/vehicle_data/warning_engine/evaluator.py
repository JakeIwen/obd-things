"""Warning evaluation orchestration, shared reads and candidate memory."""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable, Mapping, Sequence

from lib.timeutil import finite_number as _numeric
from projects.vehicle_data import warning_context
from projects.vehicle_data.historian import BaselineStats, TelemetryHistorian, project_regime

from .absolute import AbsoluteMixin
from .gates import GatesMixin
from .payloads import PayloadsMixin
from .relative import RelativeMixin
from .rules import (
    AbsoluteOilPressureRule,
    AbsoluteRule,
    CANDIDATE_MEMORY_WINDOWS,
    DEFAULT_EVALUATION_RULES,
    EvaluationRule,
    PHASE_INDEX_CACHE_SECONDS,
    RUNNING_MAX_GAP_SECONDS,
    TireGroupRule,
    WARNING_SCHEMA_VERSION,
)
from .samples import (
    _Tick,
    _observed_us,
    _rule_lookbacks,
    _to_us,
    _utc,
)
from .tire_pairs import TirePairsMixin
from .tires import TiresMixin


class EarlyWarningEvaluator(
    PayloadsMixin, RelativeMixin, GatesMixin, AbsoluteMixin, TiresMixin, TirePairsMixin
):
    """Evaluate tier-0 absolute limits and tier-1 like-for-like deviations."""

    def __init__(
        self,
        historian: TelemetryHistorian,
        rules: Sequence[EvaluationRule] = DEFAULT_EVALUATION_RULES,
        custom_rules_path=None,
    ):
        self.historian = historian
        self.custom_rules_path = custom_rules_path
        self.rules = tuple(rules)
        keys = [rule.key for rule in self.rules]
        if len(keys) != len(set(keys)):
            raise ValueError("warning rule keys must be unique")
        # Offline replay sets a dict here to memoize every baseline; production
        # leaves it None so each tick reads current history.
        self.baseline_memo: dict | None = None
        # Counters for the offline memo (replay reporting); unused in production.
        self.baseline_memo_stats = {"hits": 0, "misses": 0}
        self._candidates: dict[str, dict[str, object]] = {}
        self._phase_cache: tuple[int, dict[int, dict[str, object]]] | None = None
        self._lock = threading.RLock()
        self._local = threading.local()
        self._lookbacks: dict[str, float] = {}
        for rule in self.rules:
            for metric, seconds in _rule_lookbacks(rule).items():
                self._lookbacks[metric] = max(self._lookbacks.get(metric, 0.0), seconds)

    # ------------------------------------------------------------------
    # Shared per-tick reads

    def _tick(self, at: datetime) -> tuple[_Tick, bool]:
        tick = getattr(self._local, "tick", None)
        if isinstance(tick, _Tick) and tick.at == at:
            return tick, False
        tick = _Tick(at)
        self._local.tick = tick
        return tick, True

    def _latest(self, tick: _Tick, metric: str) -> dict[str, object] | None:
        if metric not in tick.latest:
            tick.latest[metric] = self.historian.latest_sample(
                metric, at=tick.at, fresh_only=True
            )
        return tick.latest[metric]

    def _series(
        self,
        tick: _Tick,
        metric: str,
        sample: Mapping[str, object],
        *,
        lookback: float = 0.0,
    ) -> list[dict[str, object]]:
        trip_id = sample.get("trip_id")
        key = (
            metric,
            None if trip_id is None else int(trip_id),
            str(sample["source"]),
            str(sample["quality"]),
            str(sample["provenance"]),
        )
        wanted = max(float(lookback), self._lookbacks.get(metric, 0.0), 60.0)
        cached = tick.series.get(key)
        if cached is not None and cached[0] >= wanted:
            return cached[1]
        rows = warning_context.recent_series(
            self.historian,
            metric,
            trip_id=key[1],
            at=tick.at,
            lookback_seconds=wanted,
            source=key[2],
            quality=key[3],
            provenance=key[4],
        )
        tick.series[key] = (wanted, rows)
        return rows

    def _companion_values(
        self,
        tick: _Tick,
        metric: str,
        primary: Mapping[str, object],
        *,
        lookback: float,
    ) -> list[tuple[int, float]]:
        latest = self._latest(tick, metric)
        if latest is None or not _numeric(latest.get("value")):
            return []
        probe = {
            "trip_id": primary.get("trip_id"),
            "source": latest["source"],
            "quality": latest["quality"],
            "provenance": latest["provenance"],
        }
        values = []
        for point in self._series(tick, metric, probe, lookback=lookback):
            observed = _observed_us(point)
            if observed is not None and _numeric(point.get("value")):
                values.append((observed, float(point["value"])))
        return values

    def _continuous(
        self,
        tick: _Tick,
        rpm: Mapping[str, object],
        *,
        minimum: float,
        lookback: float,
        max_gap: float = RUNNING_MAX_GAP_SECONDS,
    ) -> dict[str, object] | None:
        trip_id = rpm.get("trip_id")
        key = (
            "continuous",
            str(rpm["source"]),
            str(rpm["quality"]),
            str(rpm["provenance"]),
            trip_id,
            float(minimum),
            float(lookback),
            float(max_gap),
        )
        if key not in tick.conditions:
            tick.conditions[key] = self.historian.continuous_numeric_condition(
                "engine.rpm",
                at=tick.at,
                minimum=minimum,
                max_gap_seconds=float(max_gap),
                source=str(rpm["source"]),
                quality=str(rpm["quality"]),
                provenance=str(rpm["provenance"]),
                trip_id=trip_id,
                max_lookback_seconds=lookback,
            )
        return tick.conditions[key]  # type: ignore[return-value]

    def _open_info(self, tick: _Tick, key: str) -> dict[str, object]:
        """Open-episode state for a rule, read once per evaluation.

        ``state`` is ``advisory_episodes.current_state``, which stays
        ``warning`` while the lifecycle holds a recovering episode.
        """

        if tick.open_states is None:
            states: dict[str, dict[str, object]] = {}
            try:
                episodes = self.historian.list_advisory_episodes(
                    active_only=True, limit=200
                )
                for episode in episodes:
                    rule = episode.get("rule")
                    if not isinstance(rule, str):
                        continue
                    latest = episode.get("latest_assessment")
                    latest = latest if isinstance(latest, Mapping) else {}
                    held = latest.get("held_keys")
                    first = episode.get("first_assessment")
                    first = first if isinstance(first, Mapping) else {}
                    pair = first.get("pair")
                    pair = pair if isinstance(pair, Mapping) else {}
                    states[rule] = {
                        "opened": {
                            "wheel": first.get("wheel"),
                            "metric": first.get("metric"),
                            "axle": pair.get("axle"),
                            "lower": pair.get("lower_wheel"),
                            "left": pair.get("left"),
                            "right": pair.get("right"),
                        },
                        "state": episode.get("state"),
                        "severity": latest.get("severity"),
                        "held": tuple(
                            item for item in held if isinstance(item, str)
                        ) if isinstance(held, list) else (),
                    }
            except (AttributeError, TypeError, ValueError):
                states = {}
            tick.open_states = states
        return tick.open_states.get(key, {})

    def _phase_index(
        self,
        tick: _Tick,
        *,
        trip_id: object = None,
    ) -> dict[int, dict[str, object]]:
        wanted = None if trip_id is None or isinstance(trip_id, bool) else int(trip_id)
        if tick.phase_index is not None and (wanted is None or wanted in tick.phase_index):
            return tick.phase_index
        with self._lock:
            cached = self._phase_cache
            fresh = (
                cached is not None
                and 0 <= tick.at_us - cached[0] < PHASE_INDEX_CACHE_SECONDS * 1_000_000
                and (wanted is None or wanted in cached[1])
            )
            if fresh:
                index = cached[1]
                warning_context.refine_open_trips(self.historian, index, at=tick.at_us)
            else:
                index = warning_context.trip_phase_index(self.historian, at=tick.at)
                self._phase_cache = (tick.at_us, index)
        tick.phase_index = index
        return index

    def _last_trip(self, tick: _Tick) -> dict[str, object] | None:
        if tick.last_trip is ...:
            try:
                trips = self.historian.list_trips(limit=1)
            except (AttributeError, TypeError, ValueError):
                trips = []
            tick.last_trip = trips[0] if trips else None
        return tick.last_trip  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Baselines and candidate memory

    def _baseline(self, key: tuple, compute: Callable[[], BaselineStats | None]):
        memo = self.baseline_memo
        if not isinstance(memo, dict):
            return compute()
        try:
            if key in memo:
                self.baseline_memo_stats["hits"] += 1
                return memo[key]
        except TypeError:
            return compute()
        self.baseline_memo_stats["misses"] += 1
        value = compute()
        memo[key] = value
        return value

    @staticmethod
    def _memo_key(
        metric: str,
        sample: Mapping[str, object],
        at: datetime,
        *,
        dimensions: Sequence[str],
        lookback_days: int,
        minimum_trip_age_seconds: float = 0,
        median_range: tuple | None = None,
        companion: tuple | None = None,
        phase: str | None = None,
    ) -> tuple:
        trip_id = sample.get("trip_id")
        return (
            metric,
            project_regime(str(sample["regime"]), dimensions),
            str(sample["unit"]),
            str(sample["quality"]),
            str(sample["source"]),
            str(sample["provenance"]),
            tuple(dimensions),
            int(lookback_days),
            float(minimum_trip_age_seconds),
            None if median_range is None else tuple(median_range),
            None if companion is None else tuple(companion),
            phase,
            int(trip_id) if trip_id is not None else f"hour:{_to_us(at) // 3_600_000_000}",
        )

    @staticmethod
    def memo_key_lookback_days(key: tuple) -> int:
        """Read the lookback from the unchanged baseline-memo key contract."""

        return int(key[7])

    def _baseline_for(
        self,
        *,
        metric: str,
        sample: dict[str, object],
        at: datetime,
        lookback_days: int,
        regime_dimensions: Sequence[str],
        minimum_trip_age_seconds: float = 0,
    ) -> BaselineStats | None:
        key = self._memo_key(
            metric,
            sample,
            at,
            dimensions=regime_dimensions,
            lookback_days=lookback_days,
            minimum_trip_age_seconds=minimum_trip_age_seconds,
        )
        return self._baseline(
            key,
            lambda: self.historian.robust_baseline(
                metric,
                str(sample["regime"]),
                before=at,
                lookback_days=lookback_days,
                exclude_trip_id=(
                    int(sample["trip_id"]) if sample.get("trip_id") is not None else None
                ),
                unit=str(sample["unit"]),
                quality=str(sample["quality"]),
                source=str(sample["source"]),
                provenance=str(sample["provenance"]),
                regime_dimensions=regime_dimensions,
                minimum_trip_age_seconds=minimum_trip_age_seconds,
            ),
        )

    def _rollup_baseline(
        self,
        *,
        metric: str,
        sample: dict[str, object],
        at: datetime,
        lookback_days: int,
        regime_dimensions: Sequence[str],
        minimum_trip_age_seconds: float = 0,
        median_range: tuple | None = None,
        companion: tuple | None = None,
        phase: str | None = None,
        phase_index: Mapping[int, Mapping[str, object]] | None = None,
        label: str | None = None,
    ) -> BaselineStats | None:
        key = self._memo_key(
            metric,
            sample,
            at,
            dimensions=regime_dimensions,
            lookback_days=lookback_days,
            minimum_trip_age_seconds=minimum_trip_age_seconds,
            median_range=median_range,
            companion=companion,
            phase=phase,
        )
        return self._baseline(
            key,
            lambda: warning_context.rollup_baseline(
                self.historian,
                metric,
                regime=str(sample["regime"]),
                regime_dimensions=regime_dimensions,
                before=at,
                lookback_days=lookback_days,
                exclude_trip_id=(
                    int(sample["trip_id"]) if sample.get("trip_id") is not None else None
                ),
                unit=str(sample["unit"]),
                quality=str(sample["quality"]),
                source=str(sample["source"]),
                provenance=str(sample["provenance"]),
                median_range=median_range,
                phase=None if phase is None else (phase, phase_index or {}),
                companion=companion,
                minimum_trip_age_seconds=minimum_trip_age_seconds,
                label=label,
            ),
        )

    def _candidate(
        self,
        key: str,
        *,
        at_us: int,
        first_seen_at: str | None,
        observed: int,
        required: int,
        window_seconds: float,
    ) -> dict[str, object]:
        """Remember an unconfirmed deviation; it never opens an episode."""

        with self._lock:
            entry = self._candidates.get(key)
            if entry is not None and (
                at_us - int(entry["last_seen_us"])
                > CANDIDATE_MEMORY_WINDOWS * float(entry["window_seconds"]) * 1_000_000
            ):
                entry = None
            if entry is None:
                entry = {
                    "first_seen_at": first_seen_at,
                    "last_seen_us": at_us,
                    "window_seconds": float(window_seconds),
                }
                self._candidates[key] = entry
            else:
                entry["last_seen_us"] = max(int(entry["last_seen_us"]), at_us)
                entry["window_seconds"] = float(window_seconds)
            return {
                "observed": int(observed),
                "required": int(required),
                "window_seconds": float(window_seconds),
                "first_seen_at": entry["first_seen_at"],
                "confidence": "low",
            }

    def _drop_candidate(self, key: str) -> None:
        with self._lock:
            self._candidates.pop(key, None)

    def _prune_candidates(self, at_us: int) -> None:
        with self._lock:
            for key, entry in list(self._candidates.items()):
                limit = CANDIDATE_MEMORY_WINDOWS * float(entry["window_seconds"]) * 1_000_000
                if at_us - int(entry["last_seen_us"]) > limit:
                    self._candidates.pop(key, None)

    # ------------------------------------------------------------------
    # Public entry points

    def evaluate_rule(
        self,
        rule: EvaluationRule,
        *,
        at: datetime | str | None = None,
        _refresh_rollups: bool = True,
    ) -> dict[str, object]:
        evaluated = _utc(at)
        tick, owned = self._tick(evaluated)
        try:
            if owned:
                self._prune_candidates(tick.at_us)
            if isinstance(rule, AbsoluteOilPressureRule):
                return self._evaluate_absolute_oil_rule(rule, at=evaluated, tick=tick)
            if isinstance(rule, AbsoluteRule):
                return self._evaluate_absolute_rule(rule, tick, refresh=_refresh_rollups)
            if isinstance(rule, TireGroupRule):
                return self._evaluate_tire_group(rule, tick, refresh=_refresh_rollups)
            return self._evaluate_relative_rule(rule, tick, refresh=_refresh_rollups)
        finally:
            if owned:
                self._local.tick = None

    def evaluate(
        self,
        *,
        at: datetime | str | None = None,
    ) -> dict[str, object]:
        """Return the assessment list, the active warning subset and candidates."""

        evaluated = _utc(at)
        self.historian.refresh_rollups(through=evaluated)
        tick = _Tick(evaluated)
        previous = getattr(self._local, "tick", None)
        self._local.tick = tick
        try:
            self._prune_candidates(tick.at_us)
            assessments = [
                self.evaluate_rule(rule, at=evaluated, _refresh_rollups=False)
                for rule in self.rules
            ]
        finally:
            self._local.tick = previous
        custom_status = {"enabled": self.custom_rules_path is not None, "count": 0, "error": None}
        if self.custom_rules_path is not None:
            from projects.vehicle_data.custom_warnings import load_rules, evaluate_rule
            try:
                rows = load_rules(self.custom_rules_path)
                custom = [evaluate_rule(self.historian, row, evaluated) for row in rows]
                assessments.extend(custom)
                custom_status["count"] = len(rows)
                custom_status["rule_ids"] = [row["id"] for row in rows]
            except (OSError, ValueError, KeyError, TypeError):
                # Malformed owner configuration must not suppress built-in warnings.
                custom_status["error"] = "Custom warning configuration could not be evaluated"
        from projects.vehicle_data.monitoring_coverage import coverage_assessments
        assessments.extend(coverage_assessments(self.historian, assessments, evaluated))
        active = [
            assessment
            for assessment in assessments
            if assessment["state"] in ("watch", "warning")
        ]
        candidates = [
            assessment
            for assessment in assessments
            if assessment["state"] == "normal"
            and isinstance(assessment.get("candidate"), Mapping)
        ]
        return {
            "schema_version": WARNING_SCHEMA_VERSION,
            "generated_at": evaluated.isoformat(),
            "custom_rules": custom_status,
            "method": {
                "center": "median of completed comparable bucket medians",
                "spread": "1.4826 × median absolute deviation",
                "conditioning": (
                    "exact engine/speed/RPM/coolant regime plus exact unit, source, "
                    "quality, and provenance"
                ),
                "tiers": {
                    "0": (
                        "absolute limits behind context gates; a confirmed warning "
                        "holds until the reading is back past the limit by a clear margin"
                    ),
                    "1": (
                        "sustained same-conditions deviation: threshold max(minimum "
                        "effect, multiplier × max(robust sigma, sigma floor)), escalation "
                        "after N consecutive samples, de-escalation after N samples at "
                        "or below 0.7 × threshold; unconfirmed deviations are in-memory "
                        "candidates that never open an episode"
                    ),
                    "3": "system and telemetry-coverage items; System notes only, never notify",
                },
                "opaque_health_score": False,
            },
            "active": active,
            "candidates": candidates,
            "assessments": assessments,
        }
