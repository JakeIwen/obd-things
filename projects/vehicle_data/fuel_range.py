"""Fixed B-CAN 0x760 distance to empty (miles, candidate); no bus I/O.

The same frame carries the ICS-local odometer (``u17be@8``); that field is neither
decoded nor published here.
"""

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
    name=METRIC, unit='mi', value_type='integer',
    stale_after_seconds=CONTRACT['stale_after_seconds'],
    passive_min_interval_seconds=0.0,
    minimum=CONTRACT['minimum'], maximum=CONTRACT['maximum'],
    sources=(SourceDefinition(
        name=SOURCE, bus='b-can', bitrate=125000,
        # One owner-read cluster value (260 mi) is the only direct check, so no higher.
        acquisition_class='passive_broadcast', quality='candidate',
        provenance=PROVENANCE, side_effects='none for receive-only acquisition',
    ),),
)


def decode_fuel_range_observations(can_id: int, data: bytes):
    """Bytes 4-5 hold an unsigned 11-bit range in miles: ``(b4 << 3) | (b5 >> 5)``.

    Raw 0x7FF means no value (seen at wake and key-off): returns ``None`` so the
    receiver keeps the previous reading, as for the fuel level's raw 0. Any frame of
    the wrong ID or length returns ``()`` and ends live evidence.
    """
    if can_id not in FRAME_DLCS or len(data) != FRAME_DLCS[can_id]:
        return ()
    raw = (data[4] << 3) | (data[5] >> 5)
    if raw == CONTRACT['no_value_raw']:
        return None
    return (PassiveObservation(
        metric=METRIC, value=raw, unit=DEFINITION.unit,
        source=SOURCE, quality=DEFINITION.sources[0].quality, detail=PROVENANCE,
    ),)
