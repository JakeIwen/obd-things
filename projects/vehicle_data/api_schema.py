"""Documentation-only types for the existing /v1 JSON wire contract.

No serializers, validation, defaults or coercion live here. In particular, an
absent optional key is not the same as a key whose value is null. These types
must never be used to filter/rebuild broker responses: insertion order matters
for SSE, and the proxy passes through product-specific fields unchanged.

Sources of truth:
* metrics.MetricDefinition.public_dict and models.AcquisitionResult.as_dict
* broker.TelemetryBroker.*_response (including retained/error overlays)
* maintenance.MaintenanceStore.snapshot, insights.TelemetryInsights, event_history
* lib.dtc_web.DtcWebController._public_job and warning_chat.WarningChatManager

Nested diagnostic/history/assistant evidence remains JSON: those products own
its versioned schema, not the web transport. Optional product fields describe
both ready and unavailable cache envelopes, not permission to invent defaults.
"""

from typing import Literal, NotRequired, TypedDict, Union


JsonValue = Union[None, bool, int, float, str, list["JsonValue"], dict[str, "JsonValue"]]
JsonObject = dict[str, JsonValue]
ScalarValue = bool | int | float | str


class ErrorResponse(TypedDict):
    available: Literal[False]
    detail: NotRequired[str]
    reason: NotRequired[str]
    metric: NotRequired[str]
    pending: NotRequired[bool]
    status_code: NotRequired[int]


class StreamError(TypedDict):
    """SSE `event: error` has neither available nor a delivery id."""

    reason: Literal["broker_unavailable"]
    detail: str


class WebFlags(TypedDict):
    warning_chat_enabled: bool
    active_acquisition_enabled: bool
    dtc_jobs_enabled: bool
    dtc_jobs_require_local_one_use_arm: Literal[False]
    # Missing on /v1/stream, including the dashboard listener's full stream.
    bind: NotRequired[str]
    # Only dashboard /v1/snapshot and /v2 products; null when no build is known.
    build: NotRequired[str | None]


class SnapshotDelivery(TypedDict):
    instance_id: str
    sequence: int
    generated_at_ms: int
    generated_monotonic_ms: int


class MetricSource(TypedDict):
    name: str
    bus: str
    bitrate: int
    acquisition_class: str
    quality: str
    provenance: str
    side_effects: str
    publisher_allowed: bool


class MetricDefinition(TypedDict):
    name: str
    unit: str
    value_type: str
    stale_after_seconds: float
    sources: list[MetricSource]
    allowed_acquisition_modes: list[str]
    minimum: NotRequired[float]
    maximum: NotRequired[float]


class MetricCatalogResponse(TypedDict):
    metrics: list[MetricDefinition]


class AcquisitionError(TypedDict):
    reason: str | None
    detail: str


class RetainedObservation(TypedDict):
    """Dated evidence, not a live sample or freshness substitute."""

    value: ScalarValue
    unit: str
    source: str
    quality: str
    observed_at: str


class MetricResponse(TypedDict):
    """GET metric and acquisition/observation results, including failures.

    Unknown metrics have no unit. Live fields appear only when available;
    last_recorded is an optional broker overlay when there is no cached sample.
    """

    metric: str
    available: bool
    unit: NotRequired[str]
    value: NotRequired[ScalarValue | None]
    source: NotRequired[str | None]
    bus: NotRequired[str | None]
    acquisition: NotRequired[str | None]
    interface_mode: NotRequired[str | None]
    quality: NotRequired[str | None]
    observed_at: NotRequired[str | None]
    age_ms: NotRequired[int | None]
    stale: NotRequired[bool]
    reason: NotRequired[str]
    detail: NotRequired[str]
    coalesced: NotRequired[Literal[True]]
    last_acquisition_error: NotRequired[AcquisitionError]
    last_recorded: NotRequired[RetainedObservation]


class VehicleState(TypedDict):
    state: str
    running: bool | None
    confidence: str
    basis: str
    detail: str
    observed_at: str | None
    age_ms: int | None


class CollectorStatus(TypedDict):
    state: str
    cycles: int
    last_cycle_at: str | None
    interval_seconds: float
    failure_detail: str | None


class LastReadingsStatus(TypedDict):
    persistent: bool
    storage_error: str | None


class InflightAcquisition(TypedDict):
    metric: str
    mode: str


class StatusResponse(TypedDict):
    """Full broker status; do not confuse it with /v2 status-lite."""

    service: str
    started_at: str
    interface_probe: JsonObject
    interface: JsonObject
    current_owner: JsonObject | None
    last_readings: LastReadingsStatus
    active_acquisition_permitted: bool
    collector: CollectorStatus
    display_receiver: JsonObject
    history_recorder: JsonObject
    supplemental_cache: JsonObject
    usb_can_monitor: JsonObject
    data_quality: JsonObject
    active_drive: JsonObject
    auxiliary_drive: JsonObject
    engine_off_voltage: JsonObject
    interface_reconcile: JsonObject
    vehicle_state: VehicleState
    radar_alignment: JsonObject
    radar_alignment_polling: JsonObject
    inflight: list[InflightAcquisition]
    last_acquisition_errors: dict[str, AcquisitionError]
    cached_metrics: dict[str, MetricResponse]
    web: NotRequired[WebFlags]


class SnapshotResponse(TypedDict):
    status: StatusResponse
    catalog: list[MetricDefinition]
    metrics: dict[str, MetricResponse]
    # Web injects web, then web_delivery, then status.web; the dashboard adds
    # catalog_hash afterward. The broker's snapshot has none of these overlays.
    web: NotRequired[WebFlags]
    web_delivery: NotRequired[SnapshotDelivery]
    catalog_hash: NotRequired[str]
    # /v1/stream only; inserted before web and web_delivery.
    status_code: NotRequired[int]


class CachedProduct(TypedDict):
    available: bool
    reason: NotRequired[str]
    detail: NotRequired[str]
    schema_version: NotRequired[int]
    generated_at: NotRequired[str]
    broker_cache: NotRequired[JsonObject]


class HistoryResponse(CachedProduct, total=False):
    coverage: JsonObject
    current_trip: JsonObject | None
    recent_trips: list[JsonObject]
    trip_comparison: JsonObject
    windows: dict[str, JsonObject]
    metric_trends: dict[str, JsonObject]
    maintenance_hook: JsonObject


class HealthResponse(CachedProduct, total=False):
    assessments: list[JsonObject]
    active: list[JsonObject]
    episodes: JsonObject
    data_quality: JsonObject
    usb_can_incidents: JsonObject
    evaluation_hook: JsonObject
    notification_delivery: JsonObject
    custom_rules: JsonObject
    overview_limits: JsonObject
    event_detail_template: str
    evidence_scope: str
    method: JsonObject
    drift: JsonObject


class DtcResponse(CachedProduct, total=False):
    acquisition: str
    compact: bool
    coverage: JsonObject
    group_counts: dict[str, int]
    group_returned_counts: dict[str, int]
    groups: dict[str, list[JsonObject]]
    groups_truncated: bool
    per_group_limit: int
    modules: list[JsonObject]
    description_catalog: JsonObject
    in_vehicle_scan: JsonObject


class OilChangeRequest(TypedDict):
    date: str
    mileage_mi: float | None
    mileage_source: str
    notes: str
    request_id: str


class OilChangeRecord(OilChangeRequest):
    recorded_at: str


class OilChangeResponse(TypedDict):
    available: Literal[True]
    record: OilChangeRecord


class OilLifeStatus(TypedDict):
    available: bool
    metric: str
    detail: str


class MaintenanceResponse(TypedDict):
    available: bool
    storage_error: str | None
    persistent: bool
    last_oil_change: OilChangeRecord | None
    oil_changes: list[OilChangeRecord]
    record_count: int
    last_known_odometer: RetainedObservation | None
    odometer: MetricResponse
    oil_life: OilLifeStatus


class DtcJobModule(TypedDict):
    module_key: str | None
    logical_bus: str | None
    state: str | None
    reason: str | None
    outcome: str | None
    dtc_count: int | None


class DtcJob(TypedDict):
    """Public projection always includes these keys, even on a queued pointer."""

    schema_version: int | None
    job_id: str | None
    state: str | None
    created_at: str | None
    updated_at: str | None
    started_at: str | None
    completed_at: str | None
    current_bus: str | None
    current_module: str | None
    cancel_requested: bool | None
    failure: str | None
    restoration_failure: str | None
    progress: JsonObject | None
    modules: list[DtcJobModule]


class DtcJobResponse(TypedDict):
    available: Literal[True]
    enabled: Literal[True]
    state: str | None
    job: DtcJob | None


class AcquisitionRequest(TypedDict):
    # Web only accepts passive; battery.voltage may use wake_if_asleep locally.
    mode: Literal["passive", "wake_if_asleep"]


class ObservationRequest(TypedDict):
    value: ScalarValue
    unit: str
    source: str
    bus: str
    quality: str


class DtcStartRequest(TypedDict):
    confirm_parked: Literal[True]
    confirm_park_gear: Literal[True]
    confirm_ignition_on_engine_off: Literal[True]
    token: NotRequired[str]


class DtcCancelRequest(TypedDict):
    action: Literal["cancel"]


class EventResponse(TypedDict):
    """Event list/detail/baseline envelopes; evidence is owned by event_history."""

    available: bool
    schema_version: NotRequired[int]
    pending: NotRequired[bool]
    detail: NotRequired[str]
    events: NotRequired[list[JsonObject]]
    event: NotRequired[JsonObject]
    next_before: NotRequired[int | None]
    system_guide: NotRequired[JsonValue]
    digest: NotRequired[str]
    inputs: NotRequired[list[JsonValue]]
    total: NotRequired[int]
    next_offset: NotRequired[int | None]


class AssistantWarningResponse(TypedDict):
    warnings: list[JsonObject]
    warning_config_error: NotRequired[str]


class AssistantStatusResponse(AssistantWarningResponse):
    available: Literal[True]
    read_only: Literal[False]
    vehicle_read_only: Literal[True]
    explanations_read_only: Literal[True]
    busy: bool
    settings: JsonObject
    custom_warnings_enabled: Literal[True]


class AssistantChatResponse(TypedDict):
    """Chat/action results are bare chat objects, with no available wrapper."""

    id: str
    event: JsonObject
    title: str
    created_at: str
    evidence_at: str
    turns: list[JsonObject]
