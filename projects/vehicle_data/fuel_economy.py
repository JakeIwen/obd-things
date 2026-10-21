"""Estimated fuel rate and fuel economy from the passive C-CAN ``0x2ED`` field.

The decode, and the cluster-matching factor it applies, live in
``lib.broadcast_signals.FUEL_RATE``. This module owns the public metric
definition and the fuel-economy figures added to the history response.

Economy is mean road speed over mean fuel rate across engine-running
snapshots, which equals distance over fuel because both metrics come from the
same evenly spaced samples. Sampling every few seconds costs a few percent on
a short trip (checked against full-rate captures of four drives).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Mapping

from lib.broadcast_signals import FUEL_RATE
from projects.vehicle_data.metric_types import MetricDefinition, SourceDefinition

METRIC = FUEL_RATE.metric
SPEED_METRIC = "vehicle.speed"
METRIC_UNITS = {SPEED_METRIC: "mph", METRIC: FUEL_RATE.units}
# About a minute of running at the five-second history cadence.
MIN_RUNNING_SAMPLES = 12
# Trips that began before fuel was recorded have speed without fuel for part
# of their samples; a ratio over different subsets would be wrong.
MIN_FUEL_COVERAGE = 0.9
WINDOW_DAYS = (7, 30)

DEFINITION = MetricDefinition(
    name=METRIC,
    unit=FUEL_RATE.units,
    value_type="number",
    stale_after_seconds=5.0,
    passive_min_interval_seconds=0.0,
    minimum=0.0,
    # The 16-bit field's ceiling is 38.2 gal/h after scaling.
    maximum=40.0,
    sources=(
        SourceDefinition(
            name=FUEL_RATE.source,
            bus=FUEL_RATE.bus,
            bitrate=500000,
            acquisition_class="passive_broadcast",
            quality=FUEL_RATE.quality,
            provenance=(
                "projects/ecu_mapping/findings/promaster_2022/"
                f"2026-10-14_fuel_rate_cluster_video.md; {FUEL_RATE.provenance}"
            ),
            side_effects="none; observation is receive-only",
            publisher_allowed=True,
        ),
    ),
)


def economy(totals: Mapping[str, tuple[float, int]]) -> dict[str, object]:
    """Fuel economy from running-sample totals of speed and fuel rate."""
    speed_sum, speed_count = totals.get(SPEED_METRIC, (0.0, 0))
    fuel_sum, fuel_count = totals.get(METRIC, (0.0, 0))
    result: dict[str, object] = {"available": False, "samples": fuel_count}
    if fuel_count < MIN_RUNNING_SAMPLES or speed_count < MIN_RUNNING_SAMPLES:
        result["reason"] = "not_enough_running_samples"
        return result
    coverage = min(fuel_count, speed_count) / max(fuel_count, speed_count)
    if coverage < MIN_FUEL_COVERAGE:
        result["reason"] = "partial_fuel_coverage"
        return result
    mean_speed = speed_sum / speed_count
    mean_fuel = fuel_sum / fuel_count
    if mean_fuel <= 0:
        result["reason"] = "no_fuel_recorded"
        return result
    result.update(
        available=True,
        mpg=mean_speed / mean_fuel,
        mean_speed_mph=mean_speed,
        mean_fuel_rate_gph=mean_fuel,
    )
    return result


def _window_economy(historian, *, start: datetime, end: datetime, first_fuel: str | None) -> dict:
    """Economy over ``[start, end)``, beginning no earlier than fuel was first recorded.

    Speed from drives before the fuel rate existed would otherwise fail the
    coverage check for the window's whole length. ``since`` marks a window
    that starts late for that reason.
    """
    late = first_fuel is not None and datetime.fromisoformat(first_fuel) > start
    totals = historian.running_totals_for_window(
        METRIC_UNITS, start=first_fuel if late else start, end=end
    )
    result = economy(totals)
    if late:
        result["since"] = first_fuel
    return result


def attach_fuel_economy(history: dict, historian, *, now: datetime | None = None) -> dict:
    """Add estimated economy to each listed trip and a 7/30-day summary.

    A failure here leaves the rest of the history response intact and says so
    in its own ``fuel_economy`` section.
    """
    if not history.get("available", True):
        return history
    summary: dict[str, object] = {
        "metric": METRIC,
        "quality": FUEL_RATE.quality,
        "detail": (
            "Estimated: passive 0x2ED fuel rate scaled to match the cluster's "
            "fuel figure; not yet checked against a fill-up."
        ),
    }
    trips = [
        trip
        for trip in (history.get("current_trip"), *history.get("recent_trips", []))
        if isinstance(trip, dict) and type(trip.get("id")) is int
    ]
    end = now or datetime.now(timezone.utc)
    try:
        per_trip = [
            economy(historian.running_totals_for_trip(trip["id"], METRIC_UNITS))
            for trip in trips
        ]
        first_fuel = historian.first_running_bucket(METRIC, METRIC_UNITS[METRIC])
        windows = {
            f"days_{days}": _window_economy(
                historian, start=end - timedelta(days=days), end=end, first_fuel=first_fuel
            )
            for days in WINDOW_DAYS
        }
    except Exception as exc:
        history["fuel_economy"] = {
            **summary,
            "available": False,
            "reason": "fuel_economy_unavailable",
            "detail": f"fuel economy query failed: {type(exc).__name__}: {exc}",
        }
        return history
    for trip, result in zip(trips, per_trip):
        trip["fuel_economy"] = result
    history["fuel_economy"] = {**summary, "available": True, **windows}
    return history
