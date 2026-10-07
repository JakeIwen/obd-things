"""Pure activity and regime decisions for historian ingestion."""

from __future__ import annotations

from typing import Mapping

from .models import _MetricSample


def activity_basis(
    samples: Mapping[str, _MetricSample],
    vehicle: Mapping[str, object],
    *,
    running_rpm_threshold: float,
    moving_speed_threshold_mph: float,
    vehicle_state_max_age_seconds: float,
) -> tuple[bool, str]:
    """Return activity from fresh RPM, speed or vehicle evidence."""

    rpm = samples.get("engine.rpm")
    if (
        rpm is not None
        and rpm.freshness == "fresh"
        and rpm.value_num is not None
        and rpm.value_num >= running_rpm_threshold
    ):
        return True, "fresh_engine_rpm"
    speed = samples.get("vehicle.speed")
    if (
        speed is not None
        and speed.freshness == "fresh"
        and speed.value_num is not None
        and speed.value_num > moving_speed_threshold_mph
    ):
        return True, "fresh_vehicle_speed"
    if (
        vehicle.get("running") == 1
        and isinstance(vehicle.get("age_ms"), int)
        and vehicle["age_ms"] <= vehicle_state_max_age_seconds * 1000
        and vehicle.get("confidence") not in (None, "unknown", "stale")
    ):
        return True, "fresh_vehicle_running_state"
    return False, "no_fresh_running_or_moving_evidence"


def classify_regime(
    samples: Mapping[str, _MetricSample],
    *,
    running_rpm_threshold: float,
    moving_speed_threshold_mph: float,
) -> str:
    """Classify the engine, road-speed and temperature regime of fresh samples."""

    def numeric(name: str) -> float | None:
        sample = samples.get(name)
        if sample is None or sample.freshness != "fresh":
            return None
        return sample.value_num

    rpm = numeric("engine.rpm")
    speed = numeric("vehicle.speed")
    coolant = numeric("engine.coolant_temperature")
    if rpm is None:
        engine, rpm_band = "engine_unknown", "rpm_unknown"
    elif rpm < running_rpm_threshold:
        engine, rpm_band = "engine_off", "rpm_off"
    elif rpm < 1_000:
        engine, rpm_band = "engine_running", "rpm_idle"
    elif rpm < 2_200:
        engine, rpm_band = "engine_running", "rpm_low"
    elif rpm < 3_500:
        engine, rpm_band = "engine_running", "rpm_mid"
    else:
        engine, rpm_band = "engine_running", "rpm_high"
    if speed is None:
        motion = "speed_unknown"
    elif speed <= moving_speed_threshold_mph:
        motion = "stationary"
    elif speed < 35:
        motion = "urban"
    elif speed < 65:
        motion = "road"
    else:
        motion = "highway"
    if coolant is None:
        thermal = "thermal_unknown"
    elif coolant < 160:
        thermal = "cold"
    elif coolant <= 220:
        thermal = "warm"
    else:
        thermal = "hot"
    return ":".join((engine, motion, rpm_band, thermal))

