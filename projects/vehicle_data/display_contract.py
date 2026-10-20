"""Fixed display-frame metadata shared by the broker and receive thread.

This pure module avoids importing the receiver's recorder/ownership machinery
back into the broker merely to share its source allowlist.
"""

from projects.vehicle_data import ccan_powertrain as cp, lighting, compass
from projects.vehicle_data.models import success


DISPLAY_SOURCES = frozenset((cp.SPEED_LIMIT_SOURCE, cp.ACC_DISPLAY_SOURCE, lighting.SOURCE))
DISPLAY_FRAME_DLCS = {cp.SPEED_LIMIT_ID: 4, cp.ACC_DISPLAY_ID: 8, lighting.FRAME_ID: lighting.DLC}
DISPLAY_FRAME_IDS = tuple(DISPLAY_FRAME_DLCS)
CONTINUOUS_SOURCES = DISPLAY_SOURCES | {compass.SOURCE}
DISPLAY_METRICS = (*lighting.METRICS, compass.DEFINITION)


def display_results(observations, definitions, observed_at, observed_monotonic, mode, validate):
    """Validate one complete receive callback before any cache mutation."""
    if mode not in {'listen_only', 'armed_diagnostic'}:
        raise ValueError('invalid display receiver interface mode')
    results = []
    for observation in observations:
        if observation.source not in CONTINUOUS_SOURCES:
            raise ValueError('display source is not allowlisted')
        definition = definitions[observation.metric]
        source = next((s for s in definition.sources if s.name == observation.source), None)
        if source is None:
            raise ValueError('display source is not registered for metric')
        result = success(
            metric=observation.metric, unit=observation.unit, value=observation.value,
            source=source.name, bus=source.bus, acquisition='passive',
            quality=observation.quality, observed_at=observed_at,
            observed_monotonic=observed_monotonic, interface_mode=mode,
            detail=('continuous cluster frame, kernel receipt timestamp'
                    if source.name in DISPLAY_SOURCES else
                    'compass announcement, kernel receipt timestamp'),
        )
        checked = validate(observation.metric, definition, result)
        if not checked.available:
            raise ValueError(checked.detail)
        results.append(result)
    return results
