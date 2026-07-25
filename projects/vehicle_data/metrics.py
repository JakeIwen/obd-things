"""Public telemetry metric registry.

The registry is deliberately a metric allowlist, not a generic DID or CAN API.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SourceDefinition:
    name: str
    bus: str
    bitrate: int
    acquisition_class: str
    quality: str
    provenance: str
    side_effects: str


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    unit: str
    value_type: str
    stale_after_seconds: float
    passive_min_interval_seconds: float
    wake_min_interval_seconds: float
    sources: tuple[SourceDefinition, ...]

    def public_dict(self):
        return {
            "name": self.name,
            "unit": self.unit,
            "value_type": self.value_type,
            "stale_after_seconds": self.stale_after_seconds,
            "sources": [asdict(source) for source in self.sources],
            "allowed_acquisition_modes": ["passive", "wake_if_asleep"],
        }


BATTERY_VOLTAGE = MetricDefinition(
    name="battery.voltage",
    unit="V",
    value_type="number",
    stale_after_seconds=30.0,
    passive_min_interval_seconds=1.0,
    wake_min_interval_seconds=900.0,
    sources=(
        SourceDefinition(
            name="bcan.broadcast.0x46c",
            bus="b-can",
            bitrate=125000,
            acquisition_class="passive_broadcast",
            quality="verified",
            provenance="docs/bus-map.md B-CAN 0x46C low-13-bit /400 decode",
            side_effects="none while already awake",
        ),
        SourceDefinition(
            name="ccan.broadcast.0x2ef",
            bus="c-can",
            bitrate=500000,
            acquisition_class="passive_broadcast",
            quality="approximate",
            provenance="docs/bus-map.md C-CAN fine-voltage field; divisor not independently pinned",
            side_effects="none while already awake",
        ),
        SourceDefinition(
            name="ccan.broadcast.0x41a",
            bus="c-can",
            bitrate=500000,
            acquisition_class="passive_or_wake_assisted_broadcast",
            quality="approximate",
            provenance="docs/bus-map.md parked-wake coarse-voltage field",
            side_effects="C-CAN RFH wake powers BCM accessory rails briefly",
        ),
    ),
)


METRICS = {BATTERY_VOLTAGE.name: BATTERY_VOLTAGE}
