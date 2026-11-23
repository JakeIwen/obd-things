"""Shared vocabulary and limits for one-boot USB drop recovery."""

from enum import StrEnum
from pathlib import Path

from lib.vehicle_can_roles import BOARD_A_SERIAL as BOARD_A_SERIAL
from lib.vehicle_can_roles import B_CAN_ROLE, CAN_ROLE_SPECS, C_CAN_ROLE

FLAG = "--enable-usb-drop-recovery"
IGNITION_FLAG_PATH = "/home/pi/hooks/ignition_is_on"
FLAG_HELP = ("opt in to one automatic restoration-latch recovery per boot after a "
             "verified Board A USB re-enumeration while the engine runs")
ACTIVE_DRIVE = "active_drive"
AUXILIARY_DRIVE = "auxiliary_drive"
HELPERS = (ACTIVE_DRIVE, AUXILIARY_DRIVE)
BOARD_A_ROLES = (C_CAN_ROLE, B_CAN_ROLE)
BOARD_A_DEV_IDS = {spec.role: spec.dev_id for spec in CAN_ROLE_SPECS if spec.role in BOARD_A_ROLES}
HELPER_ROLES = dict(zip(HELPERS, BOARD_A_ROLES, strict=True))
OWNED_INHIBITS = {
    ACTIVE_DRIVE: "vehicle-data-restoration-failed",
    AUXILIARY_DRIVE: "vehicle-data-bcan-restoration-failed",
}
HELPER_MODULES = ("projects.vehicle_data.active_drive", "projects.vehicle_data.bcan_auxiliary")
SERVICE_CGROUP_SUFFIX = "/van-telemetry.service"
LINK_HELPER_PATH = "/usr/local/sbin/vanpi-can-link-by-index"
LINK_HELPER_SHA256 = "c8e72961b9f56d730219f2ca2415df4597c811f5c8114fb6d9d3f0fdad703101"

RECOVERY_WINDOW_SECONDS = 900
RUNNING_CHECK_ATTEMPTS = 3
IP_TIMEOUT_SECONDS = 5
ENGINE_EVIDENCE_MAX_AGE_SECONDS = 2.0
PROCESS_CHECK_ROUNDS = 8
COMMIT_PROCESS_RETRIES = 3
SUBMITTER_WAIT_SECONDS = 10

RECORD_DIR = Path(__file__).parents[2] / "tmp" / "vehicle_data" / "usb-recovery"
SCHEMA = 1
KEEP_BOOTS = 8
MAX_INCIDENTS_PER_BOOT = 8
MAX_LATCHES_PER_HELPER = 8
MAX_LATCH_DETAIL_CHARS = 240
MAX_RECORD_BYTES = 16384


class IncidentState(StrEnum):
    OPEN = "open"
    WAITING = "waiting"
    INELIGIBLE = "ineligible"
    RESERVED = "reserved"
    CONFIGURING = "configuring"
    VERIFYING = "verifying"
    COMMITTING = "committing"
    COMMITTED = "committed"
    COMMITTED_LATCHES_KEPT = "committed_latches_kept"
    COMMIT_OUTCOME_UNKNOWN = "commit_outcome_unknown"
    FAILED = "failed"
    PARTIAL_COMMIT = "partial_commit"


TERMINAL_STATES = frozenset({
    IncidentState.INELIGIBLE, IncidentState.COMMITTED, IncidentState.COMMITTED_LATCHES_KEPT,
    IncidentState.COMMIT_OUTCOME_UNKNOWN, IncidentState.FAILED, IncidentState.PARTIAL_COMMIT,
})


class RadarCondition(StrEnum):
    NONE = "none"
    ATTEMPTED = "attempted"
    UNCERTAIN = "uncertain"


class MutationOutcome(StrEnum):
    UP_VERIFIED = "up_verified"
    CONFIGURE_FAILED = "configure_failed"
    VERIFICATION_FAILED = "verification_failed"
    INSTANCE_CHANGED = "instance_changed"
    DOWN_VERIFIED = "down_verified"
    INSTANCE_GONE = "instance_gone"
    ROLLBACK_UNVERIFIED = "rollback_unverified"


class Refusal(StrEnum):
    BOOT_ID_UNAVAILABLE = "boot_id_unavailable"
    BOOT_BUDGET_SPENT = "boot_budget_spent"
    WINDOW_EXPIRED = "window_expired"
    LINK_HELPER_UNVERIFIED = "link_helper_unverified"
    RADAR_RUN_ATTEMPTED = "radar_run_attempted"
    RADAR_RUN_UNCERTAIN = "radar_run_uncertain"
    BCAN_ONLY_LATCH = "bcan_only_latch"
    ARMED_INSTANCE_UNKNOWN = "armed_instance_unknown"
    INSTANCE_RECORDS_DISAGREE = "instance_records_disagree"
    OWNED_INHIBIT_MISSING = "owned_inhibit_missing"
    LATCH_STATE_MISMATCH = "latch_state_mismatch"
    SERVICE_CGROUP_UNVERIFIABLE = "service_cgroup_unverifiable"
    INHIBIT_ALREADY_ENDED = "inhibit_already_ended"
    INHIBIT_RECORD_CHANGED = "inhibit_record_changed"
    INCIDENT_RECORD_UNWRITTEN = "incident_record_unwritten"
    RESERVATION_FAILED = "reservation_failed"
    HELPER_CLAIMED = "helper_claimed"
    HELPER_THREAD_ALIVE = "helper_thread_alive"
    BROKER_STOPPING = "broker_stopping"
    PROCESSES_NOT_QUIET = "processes_not_quiet"
    HELPERS_HELD = "helpers_held"
    STARTUP_FAULT_UNRECORDED = "startup_fault_unrecorded"
    IGNITION_NOT_ON = "ignition_not_on"
    FOREIGN_INHIBIT = "foreign_inhibit"
    OLD_INSTANCE_PRESENT = "old_instance_present"
    ROLE_UNRESOLVED = "role_unresolved"
    USB_INSTANCE_UNCHANGED = "usb_instance_unchanged"
    LINK_NOT_DOWN = "link_not_down"
    DISPLAY_BUSY = "display_busy"
    LOCKS_BUSY = "locks_busy"
    TOPOLOGY_CHANGED = "topology_changed"
    INSTANCE_CHANGED = "instance_changed"
    CONFIGURE_FAILED = "configure_failed"
    VERIFICATION_FAILED = "verification_failed"
    WRONG_BUS = "wrong_bus"
    ENGINE_NOT_RUNNING = "engine_not_running"
    ENGINE_EVIDENCE_STALE = "engine_evidence_stale"
    PROCESSES_UNSTABLE = "processes_unstable"
    TOPOLOGY_RECORD_FAILED = "topology_record_failed"
    COMMIT_REFUSED = "commit_refused"
    UNEXPECTED_ERROR = "unexpected_error"
