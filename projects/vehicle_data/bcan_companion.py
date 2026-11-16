"""Frames received beside the broker's B-CAN auxiliary owner (compass, fuel level)."""

from projects.vehicle_data import compass, fuel_level


FRAME_DLCS = {**compass.FRAME_DLCS, **fuel_level.FRAME_DLCS}
METRICS = (compass.METRIC, fuel_level.METRIC)
# Frame-specific rejection ends only that frame's evidence; transport loss ends all of METRICS.
METRICS_BY_FRAME = {
    next(iter(compass.FRAME_DLCS)): (compass.METRIC,),
    next(iter(fuel_level.FRAME_DLCS)): (fuel_level.METRIC,),
}
FUEL_REPUBLISH_SECONDS = 2.0


class FuelThrottle:
    """Admit a fuel frame when its value changes, else at most once per 2 s.

    The 4 Hz frame would otherwise run the forced route/interface check each time. Freshness
    is 10 s, so a 2 s refresh keeps it live. Kernel receipt time drives it, not the wall clock.
    """

    def __init__(self, interval=FUEL_REPUBLISH_SECONDS):
        self.interval = interval
        self._value = None
        self._at = None

    def reset(self):
        self._value = self._at = None

    def admit(self, observations, observed_monotonic):
        if not observations or observations[0].metric != fuel_level.METRIC:
            return True
        value = observations[0].value
        if (value == self._value and self._at is not None
                and 0 <= observed_monotonic - self._at < self.interval):
            return False
        self._value, self._at = value, observed_monotonic
        return True


def decode_bcan_companion(can_id: int, data: bytes):
    if can_id in fuel_level.FRAME_DLCS:
        return fuel_level.decode_fuel_level_observations(can_id, data)
    return compass.decode_compass_observations(can_id, data)
