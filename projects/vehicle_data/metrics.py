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
    publisher_allowed: bool = False
    publisher_values: tuple[object, ...] | None = None


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    unit: str
    value_type: str
    stale_after_seconds: float
    passive_min_interval_seconds: float
    wake_min_interval_seconds: float
    sources: tuple[SourceDefinition, ...]
    allowed_acquisition_modes: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None

    def public_dict(self):
        public_sources = []
        for source in self.sources:
            source_payload = asdict(source)
            # This is an ingest-validation constraint, not a claim about the
            # complete physical value domain of the underlying signal.
            source_payload.pop("publisher_values", None)
            public_sources.append(source_payload)
        payload = {
            "name": self.name,
            "unit": self.unit,
            "value_type": self.value_type,
            "stale_after_seconds": self.stale_after_seconds,
            "sources": public_sources,
            "allowed_acquisition_modes": list(self.allowed_acquisition_modes),
        }
        if self.minimum is not None:
            payload["minimum"] = self.minimum
        if self.maximum is not None:
            payload["maximum"] = self.maximum
        return payload


BATTERY_VOLTAGE = MetricDefinition(
    name="battery.voltage",
    unit="V",
    value_type="number",
    stale_after_seconds=30.0,
    passive_min_interval_seconds=1.0,
    wake_min_interval_seconds=900.0,
    allowed_acquisition_modes=("passive", "wake_if_asleep"),
    minimum=0.0,
    maximum=32.0,
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
            name="ccan.broadcast.0x41a",
            bus="c-can",
            bitrate=500000,
            acquisition_class="passive_or_wake_assisted_broadcast",
            quality="verified",
            provenance="docs/bus-map.md C-CAN 0x41A byte0 x0.05 V +4.0 V decode",
            side_effects="C-CAN RFH wake powers BCM accessory rails briefly",
        ),
        SourceDefinition(
            name="cluster.did.1004",
            bus="c-can",
            bitrate=500000,
            acquisition_class="physical_read_data_by_identifier",
            quality="observed_alfa_scale",
            provenance=(
                "projects/ecu_mapping/findings/promaster_2022/"
                "2026-07-24_cluster_singleton_correlation.md; "
                "raw u8 x 0.1 V"
            ),
            side_effects=(
                "physical UDS 22 read; may refresh the cluster diagnostic "
                "session timer"
            ),
            publisher_allowed=True,
        ),
    ),
)


IGNITION_ON = MetricDefinition(
    name="vehicle.ignition_on",
    unit="boolean",
    value_type="boolean",
    stale_after_seconds=5.0,
    passive_min_interval_seconds=0.0,
    wake_min_interval_seconds=0.0,
    sources=(
        SourceDefinition(
            name="ccan.broadcast.0x2ef",
            bus="c-can",
            bitrate=500000,
            acquisition_class="passive_broadcast",
            quality="verified",
            provenance=(
                "docs/bus-map.md C-CAN 0x2EF ignition-on presence gate"
            ),
            side_effects="none; observation is receive-only",
            publisher_allowed=True,
            # 0x2EF is a presence gate. A received frame establishes True;
            # silence becomes unknown through staleness and must never be
            # published as a synthetic False observation.
            publisher_values=(True,),
        ),
    ),
)


ENGINE_OIL_PRESSURE = MetricDefinition(
    name="engine.oil_pressure",
    unit="psi",
    value_type="number",
    stale_after_seconds=5.0,
    passive_min_interval_seconds=0.0,
    wake_min_interval_seconds=0.0,
    minimum=0.0,
    maximum=150.0,
    sources=(
        SourceDefinition(
            name="ccan.broadcast.0x41d",
            bus="c-can",
            bitrate=500000,
            acquisition_class="passive_broadcast",
            quality="observed_alfa_scale",
            provenance=(
                "projects/ecu_mapping/findings/promaster_2022/"
                "2026-07-26_pcm_plots_idle_mapping.md; "
                "0x41D byte 2 mirrors PCM DID 022A raw, native x 4 kPa; "
                "telemetry converts kPa to psi"
            ),
            side_effects="none; observation is receive-only",
            publisher_allowed=True,
        ),
    ),
)


ENGINE_COOLANT_TEMPERATURE = MetricDefinition(
    name="engine.coolant_temperature",
    unit="°F",
    value_type="number",
    stale_after_seconds=5.0,
    passive_min_interval_seconds=0.0,
    wake_min_interval_seconds=0.0,
    minimum=-40.0,
    maximum=419.0,
    sources=(
        SourceDefinition(
            name="ccan.broadcast.0x2ed",
            bus="c-can",
            bitrate=500000,
            acquisition_class="passive_broadcast",
            quality="observed_alfa_scale",
            provenance=(
                "projects/ecu_mapping/findings/promaster_2022/"
                "2026-07-26_pcm_plots_idle_mapping.md; "
                "0x2ED byte 0 - 40 °C, exactly linked to PCM DID 011D; "
                "telemetry converts °C to °F"
            ),
            side_effects="none; observation is receive-only",
            publisher_allowed=True,
        ),
    ),
)

ENGINE_RPM = MetricDefinition(
    name="engine.rpm",
    unit="rpm",
    value_type="number",
    stale_after_seconds=5.0,
    passive_min_interval_seconds=0.0,
    wake_min_interval_seconds=0.0,
    minimum=0.0,
    maximum=9000.0,
    sources=(
        SourceDefinition(
            name="ccan.broadcast.0x0fc",
            bus="c-can",
            bitrate=500000,
            acquisition_class="passive_broadcast",
            quality="observed_alfa_scale",
            provenance=(
                "projects/ecu_mapping/findings/promaster_2022/"
                "2026-07-27_pcm_plots_loaded_drive_mapping.md; "
                "0x0FC bytes 0-1 u16be with low 2 bits masked, / 4, "
                "exactly tracks PCM DID 01D5 raw rpm across a loaded drive"
            ),
            side_effects="none; observation is receive-only",
            publisher_allowed=True,
        ),
    ),
)

VEHICLE_SPEED = MetricDefinition(
    name="vehicle.speed",
    unit="mph",
    value_type="number",
    stale_after_seconds=5.0,
    passive_min_interval_seconds=0.0,
    wake_min_interval_seconds=0.0,
    minimum=0.0,
    maximum=160.0,
    sources=(
        SourceDefinition(
            name="ccan.broadcast.0x101",
            bus="c-can",
            bitrate=500000,
            acquisition_class="passive_broadcast",
            quality="observed_alfa_scale",
            provenance=(
                "projects/ecu_mapping/findings/promaster_2022/"
                "2026-07-27_tcm_plots_loaded_drive_mapping.md; "
                "packed 0x101 speed / 16 km/h exactly linked to TCM DID "
                "F40D over a loaded drive; telemetry converts km/h to mph"
            ),
            side_effects="none; observation is receive-only",
            publisher_allowed=True,
        ),
    ),
)

TARGET_CRANKSHAFT_TORQUE = MetricDefinition(
    name="engine.target_crankshaft_torque",
    unit="lb-ft",
    value_type="number",
    stale_after_seconds=5.0,
    passive_min_interval_seconds=0.0,
    wake_min_interval_seconds=0.0,
    minimum=-400.0,
    maximum=1200.0,
    sources=(
        SourceDefinition(
            name="ccan.broadcast.0x100",
            bus="c-can",
            bitrate=500000,
            acquisition_class="passive_broadcast",
            quality="observed_alfa_scale",
            provenance=(
                "projects/ecu_mapping/findings/promaster_2022/"
                "2026-07-27_tcm_plots_loaded_drive_mapping.md; "
                "0x100 bytes 3-4 >> 5 minus 500 Nm exactly linked to TCM "
                "DID 101B target crankshaft torque; converted to lb-ft"
            ),
            side_effects="none; observation is receive-only",
            publisher_allowed=True,
        ),
    ),
)


def _transmission_speed_metric(name: str, field: str) -> MetricDefinition:
    return MetricDefinition(
        name=name,
        unit="rpm",
        value_type="number",
        stale_after_seconds=5.0,
        passive_min_interval_seconds=0.0,
        wake_min_interval_seconds=0.0,
        minimum=0.0,
        maximum=20000.0,
        sources=(
            SourceDefinition(
                name="ccan.broadcast.0x1f7",
                bus="c-can",
                bitrate=500000,
                acquisition_class="passive_broadcast",
                quality="observed_alfa_scale",
                provenance=(
                    "projects/ecu_mapping/findings/promaster_2022/"
                    "2026-07-27_tcm_plots_loaded_drive_mapping.md; "
                    f"0x1F7 {field} exactly linked to its labeled TCM DID "
                    "over a loaded drive"
                ),
                side_effects="none; observation is receive-only",
                publisher_allowed=True,
            ),
        ),
    )


TRANSMISSION_OUTPUT_SPEED = _transmission_speed_metric(
    "transmission.output_speed",
    "packed 17-bit field (byte0 bit0, then bytes 1-2) / 32 rpm",
)
TRANSMISSION_TURBINE_SPEED = _transmission_speed_metric(
    "transmission.turbine_speed",
    "bytes 4-5 / 2 rpm",
)


def _raw_cluster_metric(did: str, unit: str, maximum: int) -> MetricDefinition:
    return MetricDefinition(
        name=f"diagnostics.cluster.did.{did}.raw",
        unit=unit,
        value_type="integer",
        stale_after_seconds=5.0,
        passive_min_interval_seconds=0.0,
        wake_min_interval_seconds=0.0,
        minimum=0,
        maximum=maximum,
        sources=(
            SourceDefinition(
                name=f"cluster.did.{did}",
                bus="c-can",
                bitrate=500000,
                acquisition_class="physical_read_data_by_identifier",
                quality="candidate",
                provenance=(
                    "projects/ecu_mapping/findings/promaster_2022/"
                    "2026-07-24_cluster_singleton_correlation.md; "
                    "label association only, decoded scale not yet qualified"
                ),
                side_effects=(
                    "physical UDS 22 read; may refresh the cluster diagnostic "
                    "session timer"
                ),
                publisher_allowed=True,
            ),
        ),
    )


CLUSTER_DID_1000_RAW = _raw_cluster_metric("1000", "raw_u16_be", 0xFFFF)
CLUSTER_DID_1002_RAW = _raw_cluster_metric("1002", "raw_u8", 0xFF)
CLUSTER_DID_0107_RAW = _raw_cluster_metric("0107", "raw_u8", 0xFF)
CLUSTER_DID_1005_RAW = _raw_cluster_metric("1005", "raw_u8", 0xFF)


METRICS = {
    definition.name: definition
    for definition in (
        BATTERY_VOLTAGE,
        IGNITION_ON,
        ENGINE_OIL_PRESSURE,
        ENGINE_COOLANT_TEMPERATURE,
        ENGINE_RPM,
        VEHICLE_SPEED,
        TARGET_CRANKSHAFT_TORQUE,
        TRANSMISSION_OUTPUT_SPEED,
        TRANSMISSION_TURBINE_SPEED,
        CLUSTER_DID_1000_RAW,
        CLUSTER_DID_1002_RAW,
        CLUSTER_DID_0107_RAW,
        CLUSTER_DID_1005_RAW,
    )
}
