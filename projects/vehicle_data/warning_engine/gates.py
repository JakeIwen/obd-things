"""Applicability gates for absolute warning rules."""
from __future__ import annotations

from typing import Callable, Mapping

from lib.timeutil import finite_number as _numeric

from .rules import (
    AbsoluteRule,
    PARKED_ENGINE_STATES,
    PARKED_SETTLE_SECONDS,
    QUALIFIED_QUALITIES,
    RPM_MAX_AGE_SECONDS,
    RUNNING_GRACE_SECONDS,
    RUNNING_LOOKBACK_SECONDS,
    RUNNING_RPM,
    WARM_COOLANT_F,
)
from .samples import (
    _Tick,
    _band_floor,
    _captured_us,
    _companion_at,
    _engine,
    _observed_us,
    _rule_lookbacks,
    _sample_age_seconds,
    _to_us,
    _utc,
)


class GatesMixin:
    """Running and absolute-rule context gates on the owning evaluator's tick."""

    # ------------------------------------------------------------------
    # Tier 0: absolute rules

    def _running_gate(self, tick: _Tick) -> dict[str, object]:
        """Fresh qualified RPM >= 400 continuously for at least 10 s."""

        key = ("running_gate",)
        if key in tick.conditions:
            return tick.conditions[key]  # type: ignore[return-value]
        rpm = self._latest(tick, "engine.rpm")
        rpm_age = (
            _sample_age_seconds(rpm, tick.at)
            if isinstance(rpm, dict) and _numeric(rpm.get("value"))
            else None
        )
        result: dict[str, object]
        if rpm is None or rpm_age is None or rpm_age > RPM_MAX_AGE_SECONDS:
            result = {
                "ok": False,
                "state": "not_applicable",
                "reason": "engine running is not established by a fresh RPM reading",
                "evidence": {"qualified": False, "rpm": None},
            }
        else:
            rpm_payload = self._current_payload(rpm, rpm_age)
            if rpm.get("quality") not in QUALIFIED_QUALITIES:
                result = {
                    "ok": False,
                    "state": "not_applicable",
                    "reason": "RPM reading is not from a qualified decode",
                    "evidence": {"qualified": False, "rpm": rpm_payload},
                }
            elif float(rpm["value"]) < RUNNING_RPM:
                result = {
                    "ok": False,
                    "state": "not_applicable",
                    "reason": "engine is not running",
                    "evidence": {
                        "qualified": False,
                        "rpm": rpm_payload,
                        "minimum_rpm": RUNNING_RPM,
                    },
                }
            else:
                interval = self._continuous(
                    tick, rpm, minimum=RUNNING_RPM, lookback=RUNNING_LOOKBACK_SECONDS
                )
                if (
                    interval is None
                    or float(interval["duration_seconds"]) < RUNNING_GRACE_SECONDS
                ):
                    result = {
                        "ok": False,
                        "state": "not_applicable",
                        "reason": "engine is inside the startup grace period",
                        "evidence": {
                            "qualified": False,
                            "rpm": rpm_payload,
                            "continuous_interval": interval,
                            "startup_grace_seconds": RUNNING_GRACE_SECONDS,
                        },
                    }
                else:
                    result = {
                        "ok": True,
                        "rpm": rpm,
                        "start_us": _to_us(_utc(str(interval["started_at"]))),
                        "evidence": {
                            "qualified": True,
                            "rpm": rpm_payload,
                            "continuous_interval": interval,
                            "startup_grace_seconds": RUNNING_GRACE_SECONDS,
                        },
                    }
        tick.conditions[key] = result
        return result

    def _absolute_parked_gate(
        self,
        rule: AbsoluteRule,
        tick: _Tick,
        sample: dict[str, object],
        warning: float | None,
        critical: float | None,
    ) -> dict[str, object]:
        rpm = self._latest(tick, "engine.rpm")
        rpm_age = (
            _sample_age_seconds(rpm, tick.at)
            if isinstance(rpm, dict) and _numeric(rpm.get("value"))
            else None
        )
        trip = self._last_trip(tick)
        last_active_us = None
        if isinstance(trip, Mapping) and isinstance(trip.get("last_active_at"), str):
            last_active_us = _to_us(_utc(str(trip["last_active_at"])))
        evidence = {
            "rpm": None if rpm is None or rpm_age is None else self._current_payload(rpm, rpm_age),
            "last_trip_id": trip.get("id") if isinstance(trip, Mapping) else None,
            "last_active_at": trip.get("last_active_at") if isinstance(trip, Mapping) else None,
            "settle_seconds": PARKED_SETTLE_SECONDS,
            "engine_states": sorted(PARKED_ENGINE_STATES),
        }
        if (
            rpm_age is not None
            and rpm_age <= PARKED_SETTLE_SECONDS
            and float(rpm["value"]) >= RUNNING_RPM  # type: ignore[index]
        ):
            return {"ok": False, "state": "not_applicable",
                    "reason": "engine is running", "evidence": evidence}
        if _engine(sample) not in PARKED_ENGINE_STATES:
            return {"ok": False, "state": "not_applicable",
                    "reason": "reading was not taken with the engine off", "evidence": evidence}
        captured = _captured_us(sample)
        if (
            last_active_us is not None
            and captured is not None
            and captured - last_active_us < PARKED_SETTLE_SECONDS * 1_000_000
        ):
            return {"ok": False, "state": "not_applicable",
                    "reason": "battery reading is still settling after the last drive",
                    "evidence": evidence}

        def accept_parked(point: Mapping[str, object]) -> tuple:
            point_captured = _captured_us(point)
            ok = (
                _engine(point) in PARKED_ENGINE_STATES
                and point_captured is not None
                and (
                    last_active_us is None
                    or point_captured - last_active_us >= PARKED_SETTLE_SECONDS * 1_000_000
                )
            )
            return ("ok" if ok else "fail", warning, critical, float(point["value"]))

        return {"ok": True, "accept": accept_parked, "evidence": evidence}

    @staticmethod
    def _absolute_running_accept(
        rule: AbsoluteRule,
        *,
        start_us: int,
        warning: float | None,
        critical: float | None,
        rpm_values: list[tuple[int, float]],
        minimum_rpm: float | None,
        companion_values: list[tuple[int, float]],
    ) -> Callable[[Mapping[str, object]], tuple]:
        def accept_running(point: Mapping[str, object]) -> tuple:
            point_us = _observed_us(point)
            value = float(point["value"])
            if point_us is None:
                return ("fail", warning, critical, value)
            status = "ok" if point_us - start_us >= RUNNING_GRACE_SECONDS * 1_000_000 else "fail"
            threshold = warning
            if rule.bands or minimum_rpm is not None:
                rpm = _companion_at(rpm_values, point_us, RPM_MAX_AGE_SECONDS)
                if rpm is None:
                    return ("gap", None if rule.bands else threshold, critical, value)
                if rule.bands:
                    threshold = _band_floor(rpm, rule.bands)
                    if threshold is None:
                        return ("skip", None, None, value)
                if minimum_rpm is not None and rpm < minimum_rpm:
                    status = "fail"
            if rule.companion is not None and status != "fail":
                companion = _companion_at(
                    companion_values, point_us, float(rule.companion[2])
                )
                if companion is None:
                    status = "gap"
                elif companion < float(rule.companion[1]):
                    status = "fail"
            return (status, threshold, critical, value)

        return accept_running

    def _absolute_running_gate(
        self,
        rule: AbsoluteRule,
        tick: _Tick,
        sample: dict[str, object],
        warning: float | None,
        critical: float | None,
        lookback: float,
    ) -> dict[str, object]:
        running = self._running_gate(tick)
        if not running["ok"]:
            return {
                "ok": False,
                "state": running["state"],
                "reason": running["reason"],
                "evidence": running["evidence"],
                "running_evidence": running["evidence"],
            }
        start_us = int(running["start_us"])
        rpm_now = float(running["rpm"]["value"])  # type: ignore[index]
        evidence: dict[str, object] = {"running": running["evidence"]}
        if rule.gate == "warm_running":
            coolant = self._latest(tick, "engine.coolant_temperature")
            coolant_age = (
                _sample_age_seconds(coolant, tick.at)
                if isinstance(coolant, dict) and _numeric(coolant.get("value"))
                else None
            )
            evidence["coolant"] = (
                None if coolant is None or coolant_age is None
                else self._current_payload(coolant, coolant_age)
            )
            evidence["minimum_coolant"] = WARM_COOLANT_F
            if (
                coolant_age is None
                or coolant_age > RPM_MAX_AGE_SECONDS
                or float(coolant["value"]) < WARM_COOLANT_F  # type: ignore[index]
            ):
                return {"ok": False, "state": "not_applicable",
                        "reason": "limits apply once the engine is warm",
                        "evidence": evidence, "running_evidence": running["evidence"]}
        rpm_values: list[tuple[int, float]] = []
        if rule.bands or rule.gate == "running_rpm_min":
            rpm_values = self._companion_values(
                tick, "engine.rpm", sample, lookback=lookback + RPM_MAX_AGE_SECONDS
            )
        if rule.bands and _band_floor(rpm_now, rule.bands) is None:
            evidence["bands"] = [list(band) for band in rule.bands]
            return {"ok": False, "state": "not_applicable",
                    "reason": "engine speed is outside every pressure band",
                    "evidence": evidence, "running_evidence": running["evidence"]}
        minimum_rpm = float(rule.gate_params[0]) if rule.gate == "running_rpm_min" else None
        if minimum_rpm is not None:
            evidence["minimum_rpm"] = minimum_rpm
            if rpm_now < minimum_rpm:
                return {"ok": False, "state": "not_applicable",
                        "reason": "charging check applies above its minimum engine speed",
                        "evidence": evidence, "running_evidence": running["evidence"]}
        companion_values: list[tuple[int, float]] = []
        if rule.companion is not None:
            companion_metric, companion_minimum, companion_age = rule.companion
            companion_values = self._companion_values(
                tick,
                str(companion_metric),
                sample,
                lookback=lookback + float(companion_age),
            )
            evidence["companion"] = {
                "metric": companion_metric,
                "minimum": companion_minimum,
                "max_age_seconds": companion_age,
                "current": companion_values[0][1] if companion_values else None,
            }
        accept_running = self._absolute_running_accept(
            rule,
            start_us=start_us,
            warning=warning,
            critical=critical,
            rpm_values=rpm_values,
            minimum_rpm=minimum_rpm,
            companion_values=companion_values,
        )
        return {
            "ok": True,
            "accept": accept_running,
            "evidence": evidence,
            "running_evidence": running["evidence"],
        }

    def _absolute_gate(
        self,
        rule: AbsoluteRule,
        tick: _Tick,
        sample: dict[str, object],
    ) -> dict[str, object]:
        """Gate for the newest sample plus a per-sample ``accept`` function."""

        warning = rule.warning_threshold
        critical = rule.critical_threshold
        if warning is None and critical is not None:
            warning = critical
        lookback = _rule_lookbacks(rule).get(rule.metric, 120.0)
        if rule.gate == "parked":
            return self._absolute_parked_gate(
                rule, tick, sample, warning, critical
            )
        return self._absolute_running_gate(
            rule, tick, sample, warning, critical, lookback
        )
