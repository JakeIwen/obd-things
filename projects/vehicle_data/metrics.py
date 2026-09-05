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
    sources: tuple[SourceDefinition, ...]
    allowed_acquisition_modes: tuple[str, ...] = ()
    wake_min_interval_seconds: float = 900.0
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
            side_effects=(
                "none while already awake; wake_if_asleep may send the fixed "
                "bounded B-CAN network-wake burst before reading 0x46C"
            ),
        ),
        SourceDefinition(
            name="ccan.broadcast.0x41a",
            bus="c-can",
            bitrate=500000,
            acquisition_class="passive_broadcast",
            quality="verified",
            provenance="docs/bus-map.md C-CAN 0x41A byte0 x0.05 V +4.0 V decode",
            side_effects="none for receive-only acquisition",
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

CRANKSHAFT_TORQUE = MetricDefinition(
    name="engine.crankshaft_torque",
    unit="lb-ft",
    value_type="number",
    stale_after_seconds=4.0,
    passive_min_interval_seconds=0.0,
    minimum=-1000.0,
    maximum=1000.0,
    sources=(
        SourceDefinition(
            name="pcm.did.06da",
            bus="c-can",
            bitrate=500000,
            acquisition_class="physical_read_data_by_identifier",
            quality="observed_alfa_scale",
            provenance=(
                "projects/ecu_mapping/findings/promaster_2022/"
                "2026-07-27_pcm_plots_loaded_drive_mapping.md; "
                "physical padded 22 06DA, exact signed i16be x 0.04 Nm "
                "across positive load and negative overrun; telemetry "
                "converts Nm to lb-ft"
            ),
            side_effects=(
                "engine-running-only physical UDS 22 read during a coordinated "
                "armed C-CAN interval; ECU-reported current engine torque"
            ),
        ),
    ),
)


VVT_OIL_TEMPERATURE = MetricDefinition(
    name="engine.vvt_oil_temperature",
    unit="°F",
    value_type="number",
    stale_after_seconds=15.0,
    passive_min_interval_seconds=0.0,
    minimum=-83.2,
    maximum=375.8,
    sources=(
        SourceDefinition(
            name="pcm.did.069f",
            bus="c-can",
            bitrate=500000,
            acquisition_class="physical_read_data_by_identifier",
            quality="observed_alfa_scale",
            provenance=(
                "projects/ecu_mapping/findings/promaster_2022/"
                "2026-07-27_pcm_plots_loaded_drive_mapping.md; "
                "Alfa-labeled VVT Oil Temperature, u8 - 64 °C, converted to °F; "
                "sensor/model relationship to sump oil remains unresolved"
            ),
            side_effects=(
                "engine-running-only fixed physical 22 069F in the coordinated "
                "C-CAN interval; five-second cadence, no session change"
            ),
        ),
    ),
)


ESTIMATED_CRANKSHAFT_POWER = MetricDefinition(
    name="engine.crankshaft_power",
    unit="hp",
    value_type="number",
    stale_after_seconds=4.0,
    passive_min_interval_seconds=0.0,
    minimum=-1000.0,
    maximum=1000.0,
    sources=(
        SourceDefinition(
            name="derived.pcm_06da_x_ccan_0x0fc",
            bus="c-can",
            bitrate=500000,
            acquisition_class="derived_time_aligned",
            quality="observed_alfa_scale",
            provenance=(
                "projects/ecu_mapping/findings/promaster_2022/"
                "2026-07-27_pcm_plots_loaded_drive_mapping.md; derived only "
                "from fresh PCM DID 06DA current crankshaft torque and "
                "qualified passive 0x0FC engine RPM using hp=lb-ft*rpm/5252.113"
            ),
            side_effects=(
                "none beyond the already-required guarded 06DA observation; "
                "derived ECU-estimated crankshaft power, not measured wheel power"
            ),
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

TRANSMISSION_OIL_TEMPERATURE = MetricDefinition(
    name="transmission.oil_temperature",
    unit="°F",
    value_type="number",
    stale_after_seconds=5.0,
    passive_min_interval_seconds=0.0,
    minimum=40.0,
    maximum=250.0,
    sources=(
        SourceDefinition(
            name="ccan.broadcast.0x1f7",
            bus="c-can",
            bitrate=500000,
            acquisition_class="passive_broadcast",
            quality="observed_alfa_scale",
            provenance=(
                "projects/ecu_mapping/findings/promaster_2022/"
                "2026-07-29_tcm_oil_temperature_candidate.md; "
                "0x1F7 byte 3 signed x 0.375 + 57 °C passed fixed-formula "
                "cold-start and hot-soak oil/chip discrimination gates; "
                "telemetry converts °C to °F"
            ),
            side_effects="none; observation is receive-only",
            publisher_allowed=True,
        ),
    ),
)

TRANSMISSION_TURBINE_SPEED = _transmission_speed_metric(
    "transmission.turbine_speed",
    "bytes 4-5 / 2 rpm",
)


GENERATOR_FIELD_DUTY = MetricDefinition(
    name="generator.field_duty",
    unit="%",
    value_type="number",
    stale_after_seconds=4.0,
    passive_min_interval_seconds=0.0,
    minimum=0.0,
    # The current van produced approximately 100.008%. Preserve that measured
    # overshoot rather than clamping to a presentation-driven 100% ceiling.
    maximum=101.0,
    sources=(
        SourceDefinition(
            name="pcm.did.01a1",
            bus="c-can",
            bitrate=500000,
            acquisition_class="physical_read_data_by_identifier",
            quality="observed_alfa_scale",
            provenance=(
                "projects/ecu_mapping/findings/promaster_2022/"
                "2026-07-26_pcm_plots_idle_mapping.md; "
                "2026-07-27_pcm_plots_loaded_drive_mapping.md; "
                "2026-07-30_legacy_pcm_cda_overlap.md; "
                "2026-07-30_pcm_generator_duty_direct_read.md; "
                "physical padded 22 01A1, exact u16be x 100/32768 %"
            ),
            side_effects=(
                "engine-running-only physical UDS 22 read during a coordinated "
                "armed C-CAN interval; generator field command, not current or "
                "temperature"
            ),
        ),
    ),
)


VEHICLE_ODOMETER = MetricDefinition(
    name="vehicle.odometer",
    unit="mi",
    value_type="number",
    stale_after_seconds=15.0,
    passive_min_interval_seconds=0.0,
    minimum=0.0,
    maximum=2_000_000.0,
    sources=(
        SourceDefinition(
            name="ics.did.2001",
            bus="b-can",
            bitrate=125000,
            acquisition_class="physical_read_data_by_identifier",
            quality="candidate",
            provenance=(
                "projects/ecu_mapping/findings/promaster_2022/"
                "2026-08-27_three_bus_telemetry_readiness.md; ignition-on "
                "no-session-change physical 22 2001 returned a u24be value "
                "consistent with x0.1 km, but decoded 11.140 mi below the "
                "simultaneous cluster display"
            ),
            side_effects=(
                "engine-running-only physical UDS 22 read during an "
                "independently owned B-CAN interval; no visible parked "
                "side effect was observed"
            ),
        ),
    ),
)


def _tire_pressure_metric(position: str, did: str) -> MetricDefinition:
    return MetricDefinition(
        name=f"tire.pressure.{position}",
        unit="psi",
        value_type="number",
        stale_after_seconds=30.0,
        passive_min_interval_seconds=0.0,
        minimum=0.0,
        maximum=150.0,
        sources=(
            SourceDefinition(
                name=f"rf_hub.did.{did}",
                bus="c-can",
                bitrate=500000,
                acquisition_class="physical_read_data_by_identifier",
                quality="verified",
                provenance=(
                    "projects/tpms/README.md verified 2026-07-07 "
                    "deflate/reinflate wheel map and pressure scale; "
                    f"RF Hub DID {did.upper()} raw u16 x 0.1 kPa, converted "
                    "to psi; FFFF is invalid/no sensor data"
                ),
                side_effects=(
                    "physical UDS 22 read; RF Hub polling can hold C-CAN "
                    "network management awake"
                ),
                publisher_allowed=True,
            ),
        ),
    )


TIRE_PRESSURE_FL = _tire_pressure_metric("fl", "31d0")
TIRE_PRESSURE_FR = _tire_pressure_metric("fr", "31d1")
# The verified RF Hub slot order is RR then RL for slots 3 and 4.
TIRE_PRESSURE_RR = _tire_pressure_metric("rr", "31d2")
TIRE_PRESSURE_RL = _tire_pressure_metric("rl", "31d3")


def _raw_cluster_metric(did: str, unit: str, maximum: int) -> MetricDefinition:
    return MetricDefinition(
        name=f"diagnostics.cluster.did.{did}.raw",
        unit=unit,
        value_type="integer",
        stale_after_seconds=5.0,
        passive_min_interval_seconds=0.0,
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
        CRANKSHAFT_TORQUE,
        VVT_OIL_TEMPERATURE,
        ESTIMATED_CRANKSHAFT_POWER,
        TRANSMISSION_OUTPUT_SPEED,
        TRANSMISSION_OIL_TEMPERATURE,
        TRANSMISSION_TURBINE_SPEED,
        GENERATOR_FIELD_DUTY,
        VEHICLE_ODOMETER,
        TIRE_PRESSURE_FL,
        TIRE_PRESSURE_FR,
        TIRE_PRESSURE_RR,
        TIRE_PRESSURE_RL,
        CLUSTER_DID_1000_RAW,
        CLUSTER_DID_1002_RAW,
        CLUSTER_DID_0107_RAW,
        CLUSTER_DID_1005_RAW,
    )
}
