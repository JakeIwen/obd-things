"""Metric and activation contract for the optional PCM barometric reading."""

from lib.modules import MODULES
from projects.vehicle_data.metric_types import MetricDefinition, SourceDefinition

DID = 0x0109
SCALE = 0.199773
INTERVAL_SECONDS = 10.0
BROKER_FLAG = "--enable-barometric-pressure"
HELPER_ENV = "VAN_TELEMETRY_BAROMETRIC_PRESSURE"
HELPER_ENV_ON = "1"

_PCM = MODULES["pcm"]
SOURCE = SourceDefinition(
    name=f"pcm.did.{DID:04x}",
    bus=_PCM.bus,
    bitrate=_PCM.bitrate,
    acquisition_class="physical_read_data_by_identifier",
    quality="observed_alfa_scale",
    provenance=(
        "projects/ecu_mapping/findings/promaster_2022/"
        "2026-11-18_pcm_plots_reference_drive.md; AlfaOBD u16be × 0.199773 kPa, "
        "observed over raw 404–411"
    ),
    side_effects=(
        "optional engine-running physical PCM 22 0109 at most once per 10 seconds "
        "in the armed C-CAN interval; no session change"
    ),
)
DEFINITION = MetricDefinition(
    name="engine.barometric_pressure",
    unit="kPa",
    value_type="number",
    stale_after_seconds=30.0,
    passive_min_interval_seconds=0.0,
    minimum=50.0,
    maximum=110.0,
    sources=(SOURCE,),
)
