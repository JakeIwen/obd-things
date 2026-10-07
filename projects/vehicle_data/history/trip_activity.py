"""Pure activity and trip-gap decisions for historian ingestion."""

from __future__ import annotations

from typing import Mapping

from projects.vehicle_data.can_availability import (
    CAN_UNAVAILABLE_ENGINE, vehicle_hardware_unavailable,
)

from .models import _MetricSample


def activity_basis(
    samples: Mapping[str, _MetricSample],
    vehicle: Mapping[str, object],
    *,
    running_rpm_threshold: float,
    moving_speed_threshold_mph: float,
    vehicle_state_max_age_seconds: float,
) -> tuple[bool, str]:
    """Return activity only when current vehicle evidence is available."""

    if vehicle_hardware_unavailable(vehicle):
        basis = vehicle.get("basis")
        return False, basis if isinstance(basis, str) else "vehicle_hardware_unavailable"
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
    vehicle: Mapping[str, object],
    *,
    running_rpm_threshold: float,
    moving_speed_threshold_mph: float,
) -> str:
    """Classify current samples without promoting cached motion through an outage."""

    def numeric(name: str) -> float | None:
        sample = samples.get(name)
        if sample is None or sample.freshness != "fresh":
            return None
        return sample.value_num

    hardware_unavailable = vehicle_hardware_unavailable(vehicle)
    rpm = None if hardware_unavailable else numeric("engine.rpm")
    speed = None if hardware_unavailable else numeric("vehicle.speed")
    coolant = numeric("engine.coolant_temperature")
    if hardware_unavailable:
        engine, rpm_band = CAN_UNAVAILABLE_ENGINE, "rpm_unknown"
    elif rpm is None:
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


def suspend_trip_timeout(
    *,
    active: bool,
    vehicle: Mapping[str, object],
    previous_vehicle_basis: object,
) -> bool:
    """Hold an open trip across unresolved hardware and its active return."""

    if vehicle_hardware_unavailable(vehicle):
        return True
    if not vehicle_hardware_unavailable({"basis": previous_vehicle_basis}):
        return False
    stopped = (
        vehicle.get("running") == 0
        and vehicle.get("state") in ("asleep", "parked", "ignition_on")
        and vehicle.get("confidence") in ("inferred", "verified")
    )
    return active or not stopped
