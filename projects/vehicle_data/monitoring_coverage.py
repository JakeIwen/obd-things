"""Saved-data coverage checks for expected running broadcasts; never acquire CAN."""
from datetime import datetime

METRICS = {
    "engine.coolant_temperature": "Coolant temperature",
    "engine.oil_pressure": "Oil pressure",
    "transmission.oil_temperature": "Transmission temperature",
}
GAP_SECONDS = 60


def age(sample, at):
    try:
        elapsed = (at - datetime.fromisoformat(sample["observed_at"])).total_seconds()
        return elapsed if elapsed >= 0 else None
    except (TypeError, ValueError, KeyError):
        return None


def coverage_assessments(historian, assessments, at):
    """A gap only becomes an advisory with independent sustained running proof.

    Silence without that proof creates no new episode. A fresh zero-RPM witness
    ends running-only monitoring; merely missing RPM cannot resolve a prior gap.
    """
    monitored = {a.get("metric") for a in assessments} & METRICS.keys()
    if not monitored:
        return []
    rpm = historian.latest_sample("engine.rpm", at=at, fresh_only=True)
    rpm_age = age(rpm, at) if rpm else None
    rpm_fresh = (rpm_age is not None and rpm_age <= 5 and rpm.get("freshness") == "fresh"
                 and isinstance(rpm.get("value"), (int, float)) and not isinstance(rpm.get("value"), bool))
    running = None
    if rpm_fresh and rpm["value"] > 400:
        running = historian.continuous_numeric_condition("engine.rpm", at=at, minimum=400.01,
            source=rpm["source"], quality=rpm["quality"], provenance=rpm["provenance"],
            trip_id=rpm.get("trip_id"), max_gap_seconds=10, max_lookback_seconds=90)
    result = []
    for metric in sorted(monitored):
        target = historian.latest_sample(metric, at=at, fresh_only=True)
        target_age = age(target, at) if target else None
        a = {"rule": "telemetry_gap_"+metric.replace(".","_"),
             "title": METRICS[metric]+" readings missing while running",
             "metric":metric, "category":"telemetry_quality", "severity":"info", "advisory":True,
             "notification_eligible":False, "state":"unavailable",
             "reason":"No sustained independent running evidence to judge an unexpected data gap",
             "current":dict(target, effective_age_seconds=target_age) if target else None,
             "coverage_policy":{"minimum_gap_seconds":GAP_SECONDS,"minimum_running_seconds":GAP_SECONDS,
                                "rpm_max_age_seconds":5,"rpm_max_gap_seconds":10},
             "interpretation":"A telemetry coverage issue, not a mechanical temperature or pressure warning",
             "running_evidence":running}
        if rpm_fresh and rpm["value"] == 0:
            a.update(state="suppressed", reason="Fresh zero RPM ended running-only monitoring; silence is expected when the vehicle stops transmitting")
        elif target_age is not None and target_age <= 10:
            a.update(state="normal", reason="Fresh readings are available again")
        elif (target_age is not None and target_age >= GAP_SECONDS and running
                and running["duration_seconds"] >= GAP_SECONDS and running["observation_count"] >= 2):
            a.update(state="warning", reason=f"No fresh {METRICS[metric].lower()} reading for {target_age:.0f} seconds while independent RPM observations confirm sustained running; cause not established")
        result.append(a)
    return result
