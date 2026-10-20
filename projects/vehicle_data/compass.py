"""Fixed, owner-route-referenced B-CAN compass announcements; no bus I/O."""

import json
from pathlib import Path

from projects.vehicle_data.ccan_powertrain import PassiveObservation
from projects.vehicle_data.metric_types import MetricDefinition, SourceDefinition


CONTRACT = json.loads(Path(__file__).with_suffix('.json').read_text())
METRIC = CONTRACT['metric']
SOURCE = CONTRACT['source']
FRAME_DLCS = {CONTRACT['frame_id']: CONTRACT['dlc']}
PROVENANCE = 'projects/ecu_mapping/findings/promaster_2022/2026-10-14_compass_direction_video.md'
DEFINITION = MetricDefinition(
    name=METRIC, unit='direction', value_type='integer',
    stale_after_seconds=CONTRACT['stale_after_seconds'],
    passive_min_interval_seconds=0.0, minimum=0, maximum=len(CONTRACT['directions']) - 1,
    sources=(SourceDefinition(
        name=SOURCE, bus='b-can', bitrate=125000,
        acquisition_class='passive_broadcast', quality='verified',
        provenance=PROVENANCE, side_effects='none for receive-only acquisition',
    ),),
)


def decode_compass_observations(can_id: int, data: bytes) -> tuple[PassiveObservation, ...]:
    """Accept only the exact standard-ID/DLC/payload family in the owner reference.

    Unknown surrounding bits have no established validity meaning. Reject them
    instead of assuming that any three-bit value establishes a usable heading.
    """
    if (can_id not in FRAME_DLCS or len(data) != FRAME_DLCS[can_id]
            or data[:3] != b'\x00\x00\x03' or data[4] != 0 or data[3] & 0xC7 != 0xC0):
        return ()
    return (PassiveObservation(
        metric=METRIC, value=(data[3] >> 3) & 7, unit=DEFINITION.unit,
        source=SOURCE, quality=DEFINITION.sources[0].quality, detail=PROVENANCE,
    ),)
