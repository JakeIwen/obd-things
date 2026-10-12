"""Owner-referenced light telltales; fixed C-CAN decoding with no bus I/O."""

import json
from pathlib import Path

from projects.vehicle_data.ccan_powertrain import PassiveObservation
from projects.vehicle_data.metric_types import MetricDefinition, SourceDefinition


CONTRACT = json.loads(Path(__file__).with_suffix('.json').read_text())
SOURCE = CONTRACT['source']
FRAME_ID = CONTRACT['frame_id']
DLC = CONTRACT['dlc']
PROVENANCE = 'projects/ecu_mapping/findings/promaster_2022/2026-10-12_light_indicators.md'
METRICS = tuple(
    MetricDefinition(
        name=indicator['metric'], unit='boolean', value_type='boolean',
        stale_after_seconds=CONTRACT['stale_after_seconds'],
        passive_min_interval_seconds=0.0,
        sources=(SourceDefinition(
            name=SOURCE, bus='c-can', bitrate=500000,
            acquisition_class='passive_broadcast', quality='verified',
            provenance=PROVENANCE,
            side_effects='none for receive-only acquisition',
        ),),
    )
    for indicator in CONTRACT['indicators']
)


def decode_light_observations(can_id: int, data: bytes) -> tuple[PassiveObservation, ...]:
    """Decode only the exact standard-frame ID and DLC validated by the owner."""
    if can_id != FRAME_ID or len(data) != DLC:
        return ()
    return tuple(
        PassiveObservation(
            metric=indicator['metric'], value=bool(data[0] & indicator['mask']),
            unit='boolean', source=SOURCE, quality='verified', detail=PROVENANCE,
        )
        for indicator in CONTRACT['indicators']
    )
