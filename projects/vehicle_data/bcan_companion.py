"""Frames received beside the broker's B-CAN auxiliary owner (compass, fuel level)."""

from projects.vehicle_data import compass, fuel_level


FRAME_DLCS = {**compass.FRAME_DLCS, **fuel_level.FRAME_DLCS}
# Both metrics lose live evidence together when either feed is lost.
METRICS = (compass.METRIC, fuel_level.METRIC)


def decode_bcan_companion(can_id: int, data: bytes):
    if can_id in fuel_level.FRAME_DLCS:
        return fuel_level.decode_fuel_level_observations(can_id, data)
    return compass.decode_compass_observations(can_id, data)
