"""Frames received beside the broker's B-CAN auxiliary owner (compass, fuel level, range)."""

from projects.vehicle_data import compass, fuel_level, fuel_range


FRAME_DLCS = {**compass.FRAME_DLCS, **fuel_level.FRAME_DLCS, **fuel_range.FRAME_DLCS}
METRICS = (compass.METRIC, fuel_level.METRIC, fuel_range.METRIC)
# Frame-specific rejection ends only that frame's evidence; transport loss ends all of METRICS.
METRICS_BY_FRAME = {
    next(iter(compass.FRAME_DLCS)): (compass.METRIC,),
    next(iter(fuel_level.FRAME_DLCS)): (fuel_level.METRIC,),
    next(iter(fuel_range.FRAME_DLCS)): (fuel_range.METRIC,),
}
REPUBLISH_SECONDS = 2.0
# Periodic readings; the compass announces changes only and always publishes.
THROTTLED_METRICS = frozenset((fuel_level.METRIC, fuel_range.METRIC))


class PublishThrottle:
    """Admit a periodic reading when its value changes, else at most once per 2 s.

    Each admitted frame runs the forced route/interface check, so the 4 Hz fuel frame
    (and the 1 Hz range) would repeat it needlessly. Freshness is 10 s, so a 2 s refresh
    keeps it live. Kernel receipt time drives it, not the wall clock. State is per metric.
    """

    def __init__(self, interval=REPUBLISH_SECONDS):
        self.interval = interval
        self._last = {}

    def reset(self):
        self._last.clear()

    def admit(self, observations, observed_monotonic):
        if not observations or observations[0].metric not in THROTTLED_METRICS:
            return True
        metric, value = observations[0].metric, observations[0].value
        last = self._last.get(metric)
        if (last is not None and last[0] == value
                and 0 <= observed_monotonic - last[1] < self.interval):
            return False
        self._last[metric] = (value, observed_monotonic)
        return True


def decode_bcan_companion(can_id: int, data: bytes):
    if can_id in fuel_level.FRAME_DLCS:
        return fuel_level.decode_fuel_level_observations(can_id, data)
    if can_id in fuel_range.FRAME_DLCS:
        return fuel_range.decode_fuel_range_observations(can_id, data)
    return compass.decode_compass_observations(can_id, data)
