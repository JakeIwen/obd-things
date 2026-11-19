"""Fixed B-CAN 0x356 fuel tank level (percent, candidate); no bus I/O."""

import json
from pathlib import Path

from projects.vehicle_data.ccan_powertrain import PassiveObservation
from projects.vehicle_data.metric_types import MetricDefinition, SourceDefinition


CONTRACT = json.loads(Path(__file__).with_suffix('.json').read_text())
METRIC = CONTRACT['metric']
SOURCE = CONTRACT['source']
FRAME_DLCS = {CONTRACT['frame_id']: CONTRACT['dlc']}
PROVENANCE = 'projects/ecu_mapping/findings/promaster_2022/2026-11-15_fuel_level_0x356.md'
DEFINITION = MetricDefinition(
    name=METRIC, unit='%', value_type='integer',
    stale_after_seconds=CONTRACT['stale_after_seconds'],
    passive_min_interval_seconds=0.0,
    minimum=CONTRACT['minimum'], maximum=CONTRACT['maximum'],
    sources=(SourceDefinition(
        name=SOURCE, bus='b-can', bitrate=125000,
        acquisition_class='passive_broadcast', quality='candidate',
        provenance=PROVENANCE, side_effects='none for receive-only acquisition',
    ),),
)


def raw_reading(can_id: int, data: bytes):
    """The tank percent of a well-formed 0x356 frame, 0 included, else ``None``.

    The decoder above drops raw 0 so the metric never shows it; the below-empty gauge
    (``fuel_floor``) needs that 0 to know the van's gauge has bottomed out.
    """
    if can_id not in FRAME_DLCS or len(data) != FRAME_DLCS[can_id] or data[0] > CONTRACT['maximum']:
        return None
    return data[0]


def decode_fuel_level_observations(can_id: int, data: bytes):
    """Byte 0 of 0x356 is the tank level in percent.

    Returns ``None`` for raw 0, which the bus shows briefly at key-off and wake:
    the frame is understood but carries no value, so the receiver keeps the
    previous reading instead of treating it as lost evidence. Any other
    unusable frame returns ``()`` and ends live evidence like a malformed frame.
    """
    if can_id not in FRAME_DLCS or len(data) != FRAME_DLCS[can_id]:
        return ()
    raw = data[0]
    if raw == 0:
        return None
    if raw > CONTRACT['maximum']:
        return ()
    return (PassiveObservation(
        metric=METRIC, value=raw, unit=DEFINITION.unit,
        source=SOURCE, quality=DEFINITION.sources[0].quality, detail=PROVENANCE,
    ),)
