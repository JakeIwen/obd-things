"""Pure incident records and admission/commit rules for USB drop recovery."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from math import isfinite
from typing import NotRequired, TypedDict

from lib.netdev_instance import NetdevInstance
from projects.vehicle_data.usb_recovery_contract import (
    ACTIVE_DRIVE, AUXILIARY_DRIVE, BOARD_A_DEV_IDS, BOARD_A_ROLES, BOARD_A_SERIAL, HELPERS,
    HELPER_ROLES, MAX_LATCH_DETAIL_CHARS, MAX_LATCHES_PER_HELPER, OWNED_INHIBITS,
    RECOVERY_WINDOW_SECONDS, SCHEMA, IncidentState, MutationOutcome, RadarCondition, Refusal,
)


RadarMarker = tuple[int, bool]


class InhibitRecord(TypedDict):
    name: str
    channel: str
    reason: str
    boot_id: str
    updated_at: str
    stands_for: NotRequired[str]


@dataclass(frozen=True)
class LaunchRecord:
    channel: str
    usb_serial: str
    dev_id: int
    instance: NetdevInstance | None
    captured_monotonic: float


@dataclass(frozen=True)
class GrantRecord:
    attempted: bool
    granted_at: str | None
    granted_monotonic: float | None


@dataclass(frozen=True)
class ArmedRecord:
    instance: NetdevInstance | None
    seen_monotonic: float | None


@dataclass(frozen=True)
class LatchRecord:
    at: str
    monotonic: float
    reason: str | None
    restored: bool | None
    detail: str = ""


@dataclass
class HelperRecord:
    role: str
    launch: LaunchRecord | None = None
    grant: GrantRecord | None = None
    armed: ArmedRecord | None = None
    helper_pid: int | None = None
    latches: list[LatchRecord] = field(default_factory=list)
    owned_inhibit: InhibitRecord | None = None
    latches_dropped: int = 0
    radar_at_claim: RadarMarker | RadarCondition | None = None
    ambiguous: bool = False
    launch_seq: int | None = None
    claimed_monotonic: float | None = None

    def note_latch(self, latch: LatchRecord, owned_inhibit: InhibitRecord | None) -> None:
        if len(self.latches) < MAX_LATCHES_PER_HELPER:
            self.latches.append(replace(latch, detail=latch.detail[:MAX_LATCH_DETAIL_CHARS]))
        else:
            self.latches_dropped += 1
        self.owned_inhibit = dict(owned_inhibit) if owned_inhibit is not None else None


@dataclass
class MutationRecord:
    role: str
    ifindex: int
    submitted_at: str
    outcome: MutationOutcome | None = None


@dataclass(frozen=True)
class StepRecord:
    step: str
    at: str
    ok: bool
    detail: str = ""


@dataclass
class TransactionRecord:
    reserved_at: str
    board_a: dict[str, NetdevInstance]
    mutations: list[MutationRecord] = field(default_factory=list)
    engine_seen_monotonic: float | None = None
    steps: list[StepRecord] = field(default_factory=list)
    ended_inhibits: list[str] = field(default_factory=list)
    usb_monitor_incident_ids: list[str] = field(default_factory=list)
    durable: bool | None = None


@dataclass
class Incident:
    incident_id: str
    boot_id: str
    broker_pid: int
    broker_instance: str
    opened_at: str
    opened_monotonic: float
    updated_at: str
    state: IncidentState = IncidentState.OPEN
    reason: Refusal | None = None
    detail: str = ""
    radar_condition: RadarCondition = RadarCondition.NONE
    helpers: dict[str, HelperRecord] = field(default_factory=dict)
    transaction: TransactionRecord | None = None


def incident_to_dict(incident: Incident) -> dict[str, object]:
    payload = asdict(incident)
    for helper in payload["helpers"].values():
        # Claim evidence is process-local; on-disk incidents are never resumed.
        del helper["ambiguous"], helper["radar_at_claim"]
    return {"schema": SCHEMA, **payload}


def _record_fields(value: object, record: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"expected JSON object for {record}")
    return value.copy()


def _array(value: object, field_name: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"expected JSON array for {field_name}")
    return value


def _instance_from_dict(value: object) -> NetdevInstance:
    instance = NetdevInstance(**_record_fields(value, "instance"))
    if not all(isinstance(s, str) and s for s in (
        instance.channel, instance.netdev_sysfs_path, instance.usb_devpath, instance.usb_serial,
    )) or not all(type(n) is int for n in (
        instance.ifindex, instance.usb_busnum, instance.usb_devnum, instance.dev_id,
    )):
        raise ValueError("invalid USB recovery instance")
    return instance


def _helper_from_dict(value: object) -> HelperRecord:
    fields = _record_fields(value, "helper")
    if fields.keys() & {"radar_at_claim", "ambiguous"}:
        raise ValueError("incident records must not contain live claim evidence")
    for key, cls in (("launch", LaunchRecord), ("armed", ArmedRecord)):
        if (entry := fields.get(key)) is not None:
            evidence = _record_fields(entry, key)
            if (instance := evidence["instance"]) is not None:
                evidence["instance"] = _instance_from_dict(instance)
            fields[key] = cls(**evidence)
    if (grant := fields.get("grant")) is not None:
        fields["grant"] = GrantRecord(**_record_fields(grant, "grant"))
    latches = _array(fields["latches"], "latches")
    if len(latches) > MAX_LATCHES_PER_HELPER:
        raise ValueError("too many latches in USB recovery incident")
    fields["latches"] = [LatchRecord(**_record_fields(entry, "latch")) for entry in latches]
    if (inhibit := fields.get("owned_inhibit")) is not None:
        fields["owned_inhibit"] = _record_fields(inhibit, "owned_inhibit")
    return HelperRecord(**fields)


def _transaction_from_dict(value: object) -> TransactionRecord:
    fields = _record_fields(value, "transaction")
    board = _record_fields(fields["board_a"], "board_a")
    if board.keys() != set(BOARD_A_ROLES):
        raise ValueError("transaction must record both Board A roles")
    fields["board_a"] = {role: _instance_from_dict(entry) for role, entry in board.items()}
    mutations = []
    for entry in _array(fields["mutations"], "mutations"):
        data = _record_fields(entry, "mutation")
        if (outcome := data.get("outcome")) is not None:
            data["outcome"] = MutationOutcome(outcome)
        mutations.append(MutationRecord(**data))
    fields["mutations"] = mutations
    fields["steps"] = [
        StepRecord(**_record_fields(entry, "step")) for entry in _array(fields["steps"], "steps")
    ]
    for key in ("ended_inhibits", "usb_monitor_incident_ids"):
        if not all(isinstance(entry, str) for entry in _array(fields[key], key)):
            raise ValueError(f"expected strings in {key}")
    return TransactionRecord(**fields)


def incident_from_dict(payload: object) -> Incident:
    """Decode a schema-1 audit record, not launch evidence for another broker."""
    fields = _record_fields(payload, "incident")
    schema = fields.pop("schema", None)
    if type(schema) is not int or schema != SCHEMA:
        raise ValueError("unsupported USB recovery incident schema")
    try:
        fields["state"] = IncidentState(fields["state"])
        if (reason := fields.get("reason")) is not None:
            fields["reason"] = Refusal(reason)
        fields["radar_condition"] = RadarCondition(fields["radar_condition"])
        fields["helpers"] = {
            helper: _helper_from_dict(entry)
            for helper, entry in _record_fields(fields["helpers"], "helpers").items()
        }
        if (transaction := fields.get("transaction")) is not None:
            fields["transaction"] = _transaction_from_dict(transaction)
        return Incident(**fields)
    except (TypeError, KeyError) as exc:
        raise ValueError("invalid USB recovery incident") from exc


@dataclass(frozen=True)
class BrokerGates:
    claimed: frozenset[str] = frozenset()
    hold_active: bool = False
    startup_faults_unrecorded: bool = False
    latched: frozenset[str] = frozenset()
    auxiliary_thread_alive: bool = False
    stopping: bool = False


@dataclass(frozen=True)
class LinkSeen:
    ifindex: int | None
    present: bool
    up: bool | None
    passive_ok: bool


@dataclass(frozen=True)
class RoleNow:
    resolution_state: str
    instance: NetdevInstance | None
    link: LinkSeen | None


@dataclass(frozen=True)
class ProcessCheck:
    quiet: bool
    unverifiable: bool = False
    unstable: bool = False
    offenders: tuple[tuple[int, str, str], ...] = ()


@dataclass(frozen=True)
class Observation:
    boot_id: str | None
    reservation_exists: bool
    gates: BrokerGates
    radar_marker: RadarMarker | None
    processes: ProcessCheck
    inhibits: Sequence[Mapping[str, object]]
    on_disk: Mapping[str, Mapping[str, object] | None]
    present_ifindexes: frozenset[int] | set[int] | None
    board_a: Mapping[str, RoleNow]
    link_helper_ok: bool
    ignition_flag: bool
    topology_stable: bool = True


@dataclass(frozen=True)
class Proceed:
    pass


@dataclass(frozen=True)
class Wait:
    reason: Refusal


@dataclass(frozen=True)
class GiveUp:
    reason: Refusal


Decision = Proceed | Wait | GiveUp


def radar_condition(incident: Incident, marker: RadarMarker | None) -> RadarCondition:
    helper = incident.helpers.get(ACTIVE_DRIVE)
    if marker is None or (helper and helper.radar_at_claim is RadarCondition.UNCERTAIN):
        return RadarCondition.UNCERTAIN
    claim = helper.radar_at_claim if helper else None
    if not isinstance(claim, tuple) or claim[1] or marker[1] or claim[0] != marker[0]:
        return RadarCondition.ATTEMPTED
    return RadarCondition.NONE


def _radar_refusal(incident: Incident, marker: RadarMarker | None) -> Refusal | None:
    condition = radar_condition(incident, marker)
    if condition is RadarCondition.ATTEMPTED:
        return Refusal.RADAR_RUN_ATTEMPTED
    if condition is RadarCondition.UNCERTAIN:
        return Refusal.RADAR_RUN_UNCERTAIN
    return None


def latched_helpers(incident: Incident) -> frozenset[str]:
    return frozenset(helper for helper, record in incident.helpers.items() if record.latches)


def owned_inhibits(incident: Incident) -> dict[str, InhibitRecord]:
    latched = latched_helpers(incident)
    result = {}
    for helper in HELPERS:
        if helper in latched and (owned := incident.helpers[helper].owned_inhibit) is not None:
            result[OWNED_INHIBITS[helper]] = owned
    return result


def _instance_key(instance: NetdevInstance) -> tuple[int, str, int, int]:
    return instance.ifindex, instance.netdev_sysfs_path, instance.usb_busnum, instance.usb_devnum


def _usb_key(instance: NetdevInstance) -> tuple[str, int, int]:
    return instance.usb_serial, instance.usb_busnum, instance.usb_devnum


def _proven_armed(record: HelperRecord, role: str) -> tuple[NetdevInstance, str] | None:
    if record.ambiguous or record.role != role or not (record.launch and record.grant and record.armed):
        return None
    launch, grant, armed = record.launch, record.grant, record.armed
    if not grant.attempted or launch.instance is None or armed.instance is None:
        return None
    launched, granted, seen = launch.captured_monotonic, grant.granted_monotonic, armed.seen_monotonic
    if granted is None or seen is None or not all(isfinite(t) for t in (launched, granted, seen)):
        return None
    claimed = record.claimed_monotonic if record.claimed_monotonic is not None else launched
    if not 0 <= claimed <= launched <= granted <= seen:
        return None
    if launch.dev_id != BOARD_A_DEV_IDS[role] or launch.instance.dev_id != launch.dev_id:
        return None
    if launch.instance.channel != launch.channel or armed.instance.dev_id != launch.dev_id:
        return None
    if (
        armed.instance.channel != launch.channel
        or armed.instance.usb_serial != launch.instance.usb_serial
        or _instance_key(launch.instance) != _instance_key(armed.instance)
    ):
        return None
    return armed.instance, launch.usb_serial


def _armed_records_disagree(armed: Sequence[tuple[NetdevInstance, str]]) -> bool:
    first_usb = _usb_key(armed[0][0])
    return (
        len({instance.ifindex for instance, _ in armed}) != len(armed)
        or any(
            serial != BOARD_A_SERIAL or instance.usb_serial != serial or _usb_key(instance) != first_usb
            for instance, serial in armed
        )
    )


def _gate_refusal(gates: BrokerGates, processes: ProcessCheck, *, committing: bool) -> Refusal | None:
    if gates.claimed:
        return Refusal.HELPER_CLAIMED
    if gates.auxiliary_thread_alive:
        return Refusal.HELPER_THREAD_ALIVE
    if not processes.quiet or processes.unstable or processes.offenders:
        if committing and processes.unstable and not processes.offenders:
            return Refusal.PROCESSES_UNSTABLE
        return Refusal.PROCESSES_NOT_QUIET
    if gates.hold_active:
        return Refusal.HELPERS_HELD
    if gates.startup_faults_unrecorded:
        return Refusal.STARTUP_FAULT_UNRECORDED
    if gates.stopping:
        return Refusal.BROKER_STOPPING
    return None


def _owned_refusal(
    owned: Mapping[str, InhibitRecord], observation: Observation,
) -> Refusal | None:
    for name, payload in owned.items():
        stored = observation.on_disk.get(name)
        if stored is None:
            return Refusal.INHIBIT_ALREADY_ENDED
        # A fail-closed writer may return a fallback payload. It is never owned here.
        if stored != payload or payload.get("name") != name or payload.get("channel") != "*":
            return Refusal.INHIBIT_RECORD_CHANGED
    seen = set()
    for payload in observation.inhibits:
        name = payload.get("name")
        if not isinstance(name, str) or name in seen or payload != owned.get(name):
            return Refusal.FOREIGN_INHIBIT
        seen.add(name)
    return None if seen == owned.keys() else Refusal.FOREIGN_INHIBIT


def _replacement_instances(observation: Observation) -> list[NetdevInstance] | None:
    instances = []
    for role in BOARD_A_ROLES:
        current = observation.board_a.get(role)
        if current is None or current.resolution_state != "resolved" or current.instance is None:
            return None
        if current.instance.dev_id != BOARD_A_DEV_IDS[role]:
            return None
        instances.append(current.instance)
    return instances


def _replacement_refusal(
    armed: Sequence[NetdevInstance], observation: Observation,
) -> Refusal | None:
    indexes = observation.present_ifindexes
    old_indexes = {instance.ifindex for instance in armed}
    if indexes is None or old_indexes & indexes:
        return Refusal.OLD_INSTANCE_PRESENT
    replacements = _replacement_instances(observation)
    if replacements is None:
        return Refusal.ROLE_UNRESOLVED
    ccan, bcan = replacements
    if (
        ccan.usb_serial != BOARD_A_SERIAL or _usb_key(ccan) != _usb_key(bcan)
        or _usb_key(ccan) == _usb_key(armed[0])
    ):
        return Refusal.USB_INSTANCE_UNCHANGED
    if ccan.ifindex == bcan.ifindex or ccan.channel == bcan.channel:
        return Refusal.TOPOLOGY_CHANGED
    links = []
    for role, instance in zip(BOARD_A_ROLES, replacements, strict=True):
        link = observation.board_a[role].link
        if link is None or link.ifindex != instance.ifindex or instance.ifindex in old_indexes:
            return Refusal.TOPOLOGY_CHANGED
        links.append(link)
    if any(not link.present or link.up is not False for link in links):
        return Refusal.LINK_NOT_DOWN
    return None if observation.topology_stable else Refusal.TOPOLOGY_CHANGED


def assess(incident: Incident, observation: Observation, now_monotonic: float) -> Decision:
    if not observation.link_helper_ok:
        return GiveUp(Refusal.LINK_HELPER_UNVERIFIED)
    if not observation.boot_id or incident.boot_id != observation.boot_id:
        return GiveUp(Refusal.BOOT_ID_UNAVAILABLE)
    if observation.reservation_exists:
        return GiveUp(Refusal.BOOT_BUDGET_SPENT)
    age = now_monotonic - incident.opened_monotonic
    if not 0 <= age <= RECOVERY_WINDOW_SECONDS:
        return GiveUp(Refusal.WINDOW_EXPIRED)
    latched = latched_helpers(incident)
    # A B-CAN-only latch has no C-CAN launch to evaluate; classify it under R4a.
    if latched != {AUXILIARY_DRIVE} and (reason := _radar_refusal(incident, observation.radar_marker)):
        return GiveUp(reason)
    if latched not in ({ACTIVE_DRIVE}, set(HELPERS)):
        return GiveUp(Refusal.BCAN_ONLY_LATCH)
    armed = []
    for helper in HELPERS:
        if helper not in latched:
            continue
        proven = _proven_armed(incident.helpers[helper], HELPER_ROLES[helper])
        if proven is None:
            return GiveUp(Refusal.ARMED_INSTANCE_UNKNOWN)
        armed.append(proven)
    if _armed_records_disagree(armed):
        return GiveUp(Refusal.INSTANCE_RECORDS_DISAGREE)
    owned = owned_inhibits(incident)
    if len(owned) != len(latched) or not all(owned.values()):
        return GiveUp(Refusal.OWNED_INHIBIT_MISSING)
    if observation.gates.latched != latched:
        return GiveUp(Refusal.LATCH_STATE_MISMATCH)
    if observation.processes.unverifiable:
        return GiveUp(Refusal.SERVICE_CGROUP_UNVERIFIABLE)
    if reason := _gate_refusal(observation.gates, observation.processes, committing=False):
        return Wait(reason)
    if not observation.ignition_flag:
        return Wait(Refusal.IGNITION_NOT_ON)
    if reason := _owned_refusal(owned, observation):
        return Wait(reason) if reason is Refusal.FOREIGN_INHIBIT else GiveUp(reason)
    if reason := _replacement_refusal([instance for instance, _ in armed], observation):
        return Wait(reason)
    return Proceed()


def commit_check(
    incident: Incident, configured: Mapping[str, NetdevInstance], observation: Observation,
) -> Refusal | None:
    if not observation.link_helper_ok:
        return Refusal.LINK_HELPER_UNVERIFIED
    if reason := _radar_refusal(incident, observation.radar_marker):
        return reason
    latched = latched_helpers(incident)
    if observation.gates.latched != latched or not latched:
        return Refusal.LATCH_STATE_MISMATCH
    if observation.processes.unverifiable:
        return Refusal.SERVICE_CGROUP_UNVERIFIABLE
    if reason := _gate_refusal(observation.gates, observation.processes, committing=True):
        return reason
    owned = owned_inhibits(incident)
    if len(owned) != len(latched) or not all(owned.values()):
        return Refusal.OWNED_INHIBIT_MISSING
    if reason := _owned_refusal(owned, observation):
        return reason
    for role in BOARD_A_ROLES:
        expected = configured.get(role)
        current = observation.board_a.get(role)
        if (
            expected is None or current is None or current.resolution_state != "resolved"
            or current.instance != expected or current.link is None
            or current.link.ifindex != expected.ifindex
        ):
            return Refusal.INSTANCE_CHANGED
        if not current.link.present or current.link.up is not True or not current.link.passive_ok:
            return Refusal.VERIFICATION_FAILED
    return None
