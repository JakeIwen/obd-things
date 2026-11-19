"""Registry rows for the two C-CAN readings that ride the active-drive snapshot.

Both come from broadcast frames that exist only while the engine runs
(0x417 and 0x41B, 10 Hz), so they are optional in the snapshot and gated by the
broker's ``INTAKE_AC_ENV`` handshake (see ``ccan_powertrain``).
"""

from __future__ import annotations

from lib.broadcast_signals import AC_HIGH_SIDE_PRESSURE, INTAKE_AIR_TEMPERATURE
from projects.vehicle_data.metric_types import MetricDefinition, SourceDefinition

FINDINGS = "projects/ecu_mapping/findings/promaster_2022/"


def _metric(signal, finding: str, minimum: float, maximum: float) -> MetricDefinition:
    return MetricDefinition(
        name=signal.metric,
        unit=signal.units,
        value_type="number",
        stale_after_seconds=5.0,
        passive_min_interval_seconds=0.0,
        minimum=minimum,
        maximum=maximum,
        sources=(
            SourceDefinition(
                name=signal.source,
                bus=signal.bus,
                bitrate=500000,
                acquisition_class="passive_broadcast",
                quality=signal.quality,
                provenance=f"{FINDINGS}{finding}; {signal.provenance}",
                side_effects="none; observation is receive-only",
                publisher_allowed=True,
            ),
        ),
    )


# -40 to 150 °C, in °F.
INTAKE_AIR_TEMPERATURE_DEFINITION = _metric(
    INTAKE_AIR_TEMPERATURE, "2026-11-18_pcm_plots_reference_drive.md", -40.0, 302.0
)
# The 9-bit field's ceiling: 511 x 9.9 + 6 kPa = 5065 kPa = 734.6 psi.
AC_HIGH_SIDE_PRESSURE_DEFINITION = _metric(
    AC_HIGH_SIDE_PRESSURE, "2026-11-18_pcm_plots_reference_drive.md", 0.0, 735.0
)
DEFINITIONS = (INTAKE_AIR_TEMPERATURE_DEFINITION, AC_HIGH_SIDE_PRESSURE_DEFINITION)
_BOUNDS = {d.name: (d.minimum, d.maximum) for d in DEFINITIONS}


def in_registry_range(metric: str, value: object) -> bool:
    """Whether a decoded reading of one of these optional metrics is one the broker accepts.

    The IAT field's top raw values decode above the registry's maximum, and the broker
    treats an out-of-range helper observation as a protocol violation that ends the helper.
    The reader therefore drops such a frame's reading (it is implausible, and optional)
    before it can reach a snapshot, leaving the other readings of that snapshot intact.
    """
    low, high = _BOUNDS[metric]
    return isinstance(value, (int, float)) and low <= value <= high
