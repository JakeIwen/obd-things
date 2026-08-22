"""Receive-only kernel uevent monitor for the installed USB CAN branch.

The normal role snapshot is intentionally periodic, so a hub reset which
removes and recreates both adapters in under a second can fall entirely
between two samples.  This module listens to the kernel's kobject-uevent
multicast group and retains those edges until the historian acknowledges
them.

The monitor has no hardware-control API.  It never sends a netlink message,
opens a CAN socket, writes sysfs, resets a device, rebinds a driver, or changes
an interface.  Stable serial/dev_id role resolution remains the authority for
declaring a recovered incident healthy.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import threading
import time
from typing import Callable
import uuid

from lib.vehicle_can_roles import CAN_ROLE_SPECS


NETLINK_KOBJECT_UEVENT = 15
CAN_USB_VID = "1d50"
CAN_USB_PID = "606f"
MAX_UEVENT_BYTES = 64 * 1024
EVENT_SCHEMA_VERSION = 1
MONITOR_SOURCE = "kernel_kobject_uevent"
RECOVERY_SOURCE = "serial_role_reconciliation"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _bounded_text(value: object, *, maximum: int = 1024) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:maximum]


def _stable_id(namespace: str, payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"{namespace}:{digest}"


def _product_identity(product: str | None) -> tuple[str | None, str | None]:
    if not isinstance(product, str):
        return None, None
    fields = product.lower().split("/")
    if len(fields) < 2:
        return None, None
    try:
        vid = f"{int(fields[0], 16):04x}"
        pid = f"{int(fields[1], 16):04x}"
    except ValueError:
        return None, None
    return vid, pid


@dataclass(frozen=True)
class KernelUevent:
    """Bounded subset of one NUL-delimited kobject uevent datagram."""

    action: str
    devpath: str
    subsystem: str
    devtype: str | None
    product: str | None
    seqnum: str | None
    serial: str | None

    @classmethod
    def parse(cls, payload: bytes) -> "KernelUevent":
        if not isinstance(payload, bytes):
            raise TypeError("uevent payload must be bytes")
        if not payload or len(payload) > MAX_UEVENT_BYTES:
            raise ValueError("uevent payload is empty or oversized")
        fields = payload.rstrip(b"\0").split(b"\0")
        if not fields:
            raise ValueError("uevent has no fields")
        first = fields[0].decode("utf-8", "replace")
        values: dict[str, str] = {}
        for raw in fields[1:]:
            if b"=" not in raw:
                continue
            key_raw, value_raw = raw.split(b"=", 1)
            key = key_raw.decode("ascii", "ignore")[:64]
            if not key:
                continue
            values[key] = value_raw.decode("utf-8", "replace")[:4096]
        first_action, separator, first_path = first.partition("@")
        action = values.get("ACTION") or (first_action if separator else "")
        devpath = values.get("DEVPATH") or (first_path if separator else "")
        subsystem = values.get("SUBSYSTEM", "")
        if not action or not devpath.startswith("/devices/") or not subsystem:
            raise ValueError("uevent lacks action, subsystem, or absolute devpath")
        return cls(
            action=action[:32],
            devpath=devpath[:4096],
            subsystem=subsystem[:64],
            devtype=_bounded_text(values.get("DEVTYPE"), maximum=64),
            product=_bounded_text(values.get("PRODUCT"), maximum=128),
            seqnum=_bounded_text(values.get("SEQNUM"), maximum=64),
            # Kernel USB uevents do not normally carry this field.  Accepting
            # it is useful for deterministic fixture injection; live identity
            # still comes from read-only sysfs or the pre-remove cache.
            serial=_bounded_text(values.get("SERIAL"), maximum=256),
        )


class UsbCanIncidentMonitor:
    """Observe and latch transient USB/CAN detach/recovery evidence."""

    def __init__(
        self,
        *,
        sysfs_root: str | Path = "/sys",
        expected_roles_by_serial: Mapping[str, Sequence[str]] | None = None,
        queue_limit: int = 256,
        recent_limit: int = 64,
        seen_limit: int = 2048,
        wall_clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        boot_id: str | None = None,
        boot_id_path: str | Path = "/proc/sys/kernel/random/boot_id",
        producer_instance: str | None = None,
        socket_factory=None,
    ) -> None:
        if queue_limit < 1 or recent_limit < 1 or seen_limit < queue_limit:
            raise ValueError("USB monitor queue limits are invalid")
        if expected_roles_by_serial is None:
            roles: dict[str, list[str]] = {}
            for spec in CAN_ROLE_SPECS:
                roles.setdefault(spec.usb_serial, []).append(spec.role)
            expected_roles_by_serial = roles
        normalized: dict[str, tuple[str, ...]] = {}
        for serial, role_names in expected_roles_by_serial.items():
            if not isinstance(serial, str) or not serial:
                raise ValueError("expected USB serials must be nonempty strings")
            role_tuple = tuple(dict.fromkeys(role_names))
            if not role_tuple or any(
                not isinstance(role, str) or not role for role in role_tuple
            ):
                raise ValueError("each expected USB serial requires role names")
            normalized[serial] = role_tuple
        if not normalized:
            raise ValueError("at least one installed USB CAN serial is required")

        self.sysfs_root = Path(sysfs_root)
        self.expected_roles_by_serial = normalized
        self.queue_limit = queue_limit
        self.recent_limit = recent_limit
        self.seen_limit = seen_limit
        self.wall_clock = wall_clock
        self.monotonic = monotonic
        self.boot_id_path = Path(boot_id_path)
        self.boot_id = boot_id or self._read_boot_id()
        self.producer_instance = producer_instance or f"usb-monitor:{uuid.uuid4().hex}"
        if (
            not isinstance(self.producer_instance, str)
            or not self.producer_instance
            or len(self.producer_instance) > 128
        ):
            raise ValueError("USB monitor producer identity is invalid")
        self.socket_factory = socket_factory or socket.socket

        self._lock = threading.RLock()
        self._pending: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._recent_events: deque[dict[str, object]] = deque(maxlen=recent_limit)
        self._active_incidents: dict[str, dict[str, object]] = {}
        self._recent_incidents: deque[dict[str, object]] = deque(maxlen=recent_limit)
        self._board_path_by_serial: dict[str, str] = {}
        self._serial_by_board_path: dict[str, str] = {}
        self._relevant_hubs: set[str] = set()
        self._hub_serials: dict[str, set[str]] = {}
        self._fallback_sequence = 0
        self._dropped_event_count = 0
        self._ignored_event_count = 0
        self._state = "stopped"
        self._last_error: str | None = None
        self._started_at: str | None = None
        self._last_event_at: str | None = None
        self._socket = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _read_boot_id(self) -> str:
        try:
            value = self.boot_id_path.read_text(encoding="ascii").strip()
        except OSError:
            value = "boot-id-unavailable"
        return value[:128] or "boot-id-unavailable"

    @staticmethod
    def _read_attribute(path: Path, name: str) -> str | None:
        try:
            value = (path / name).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    def _devpath_for(self, path: Path) -> str | None:
        try:
            relative = path.resolve().relative_to(self.sysfs_root.resolve())
        except (OSError, ValueError):
            return None
        return "/" + relative.as_posix()

    def _learn_board_path(self, serial: str, device_path: Path) -> None:
        devpath = self._devpath_for(device_path)
        if devpath is None:
            return
        old_path = self._board_path_by_serial.get(serial)
        if old_path is not None and old_path != devpath:
            self._serial_by_board_path.pop(old_path, None)
            for hub_path, serials in tuple(self._hub_serials.items()):
                serials.discard(serial)
                if not serials:
                    self._hub_serials.pop(hub_path, None)
                    self._relevant_hubs.discard(hub_path)
        self._board_path_by_serial[serial] = devpath
        self._serial_by_board_path[devpath] = serial
        parent = device_path.resolve().parent
        root = self.sysfs_root.resolve()
        while parent != root and root in parent.parents:
            device_class = self._read_attribute(parent, "bDeviceClass")
            if device_class is not None:
                try:
                    is_hub = int(device_class, 16) == 9
                except ValueError:
                    is_hub = False
                if is_hub:
                    hub_path = self._devpath_for(parent)
                    if hub_path is not None:
                        self._relevant_hubs.add(hub_path)
                        self._hub_serials.setdefault(hub_path, set()).add(serial)
            parent = parent.parent

    def refresh_inventory(self) -> dict[str, object]:
        """Learn exact installed board and ancestor-hub paths from sysfs."""

        devices = self.sysfs_root / "bus" / "usb" / "devices"
        found: set[str] = set()
        try:
            entries = tuple(devices.iterdir())
        except OSError as exc:
            with self._lock:
                self._last_error = f"cannot inspect USB sysfs: {type(exc).__name__}: {exc}"
            return {"found_serials": [], "error": self._last_error}
        with self._lock:
            for entry in entries:
                path = entry.resolve()
                vid = self._read_attribute(path, "idVendor")
                pid = self._read_attribute(path, "idProduct")
                if (vid or "").lower() != CAN_USB_VID or (pid or "").lower() != CAN_USB_PID:
                    continue
                serial = self._read_attribute(path, "serial")
                if serial not in self.expected_roles_by_serial:
                    continue
                found.add(serial)
                self._learn_board_path(serial, path)
        return {"found_serials": sorted(found), "error": None}

    def _sysfs_device(self, devpath: str) -> Path:
        return self.sysfs_root / devpath.lstrip("/")

    def _serial_for_event(self, event: KernelUevent) -> str | None:
        cached = self._serial_by_board_path.get(event.devpath)
        if cached is not None:
            return cached
        if event.serial in self.expected_roles_by_serial:
            return event.serial
        if event.action in ("add", "bind"):
            serial = self._read_attribute(self._sysfs_device(event.devpath), "serial")
            if serial in self.expected_roles_by_serial:
                self._learn_board_path(serial, self._sysfs_device(event.devpath))
                return serial
        return None

    def _board_for_descendant(self, devpath: str) -> str | None:
        matches = [
            (path, serial)
            for path, serial in self._serial_by_board_path.items()
            if (
                devpath == path
                or devpath.startswith(path + "/")
                or devpath.startswith(path + ":")
            )
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: len(item[0]))[1]

    def _next_fallback_seq(self) -> str:
        self._fallback_sequence += 1
        return f"fallback-{self._fallback_sequence}"

    def _event_payload(
        self,
        *,
        kind: str,
        action: str,
        devpath: str,
        seqnum: str | None,
        serial: str | None,
        affected_serials: Sequence[str],
        usb_vid: str | None,
        usb_pid: str | None,
        source: str = MONITOR_SOURCE,
        identity_extra: str | None = None,
    ) -> dict[str, object]:
        occurred = self.wall_clock().astimezone(timezone.utc)
        identity_seq = seqnum or (
            f"synthetic:{identity_extra}"
            if identity_extra is not None
            else self._next_fallback_seq()
        )
        identity = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "boot_id": self.boot_id,
            "kernel_seqnum": identity_seq,
            "action": action,
            "devpath": devpath,
            "kind": kind,
            "serial": serial,
            "identity_extra": identity_extra,
        }
        event_id = _stable_id("usb-can-event-v1", identity)
        return {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_id": event_id,
            "boot_id": self.boot_id,
            "kernel_seqnum": seqnum,
            "kind": kind,
            "action": action,
            "scope": (
                f"board:{serial}"
                if serial is not None
                else f"path:{hashlib.sha256(devpath.encode()).hexdigest()[:16]}"
            ),
            "devpath": devpath,
            "usb_vid": usb_vid,
            "usb_pid": usb_pid,
            "usb_serial": serial,
            "affected_serials": sorted(set(affected_serials)),
            "occurred_at": occurred.isoformat(),
            "observed_monotonic": self.monotonic(),
            "source": source,
            "receive_only": True,
            "hardware_action": False,
        }

    def _remember_seen(self, event_id: str) -> bool:
        if event_id in self._seen:
            self._seen.move_to_end(event_id)
            return False
        self._seen[event_id] = None
        while len(self._seen) > self.seen_limit:
            self._seen.popitem(last=False)
        return True

    def _queue_event(self, event: dict[str, object]) -> bool:
        event_id = str(event["event_id"])
        if not self._remember_seen(event_id):
            return False
        if len(self._pending) >= self.queue_limit:
            self._pending.popitem(last=False)
            self._dropped_event_count += 1
        self._pending[event_id] = event
        self._recent_events.append(event)
        self._last_event_at = str(event["occurred_at"])
        self._apply_lifecycle(event)
        return True

    def _incident_for_serial(self, serial: str) -> dict[str, object] | None:
        candidates = [
            incident
            for incident in self._active_incidents.values()
            if serial in incident.get("affected_serials", [])
        ]
        if not candidates:
            return None
        # Prefer a branch-level incident so child remove events do not create
        # duplicate notifications for one parent-hub reset.
        candidates.sort(
            key=lambda incident: (
                incident.get("kind") != "usb_parent_hub_removed",
                str(incident.get("opened_at", "")),
            )
        )
        return candidates[0]

    def _open_incident(
        self,
        event: Mapping[str, object],
        *,
        scope: str,
        affected_serials: Sequence[str],
    ) -> dict[str, object]:
        existing = self._active_incidents.get(scope)
        if existing is not None:
            return existing
        incident_id = _stable_id(
            "usb-can-incident-v1",
            {
                "opened_event_id": event["event_id"],
                "scope": scope,
            },
        )
        incident = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "incident_id": incident_id,
            "state": "active",
            "kind": event["kind"],
            "scope": scope,
            "affected_serials": sorted(set(affected_serials)),
            "opened_event_id": event["event_id"],
            "opened_at": event["occurred_at"],
            "last_event_id": event["event_id"],
            "last_seen_at": event["occurred_at"],
            "event_count": 0,
            "reappearance_count": 0,
            "reappeared_at": None,
            "resolved_event_id": None,
            "resolved_at": None,
            "resolution": None,
            "source": MONITOR_SOURCE,
            "producer_instance": self.producer_instance,
            "notification_eligible": True,
        }
        self._active_incidents[scope] = incident
        return incident

    @staticmethod
    def _touch_incident(
        incident: dict[str, object], event: Mapping[str, object]
    ) -> None:
        incident["last_event_id"] = event["event_id"]
        incident["last_seen_at"] = event["occurred_at"]
        incident["event_count"] = int(incident.get("event_count", 0)) + 1

    def _apply_lifecycle(self, event: dict[str, object]) -> None:
        kind = str(event["kind"])
        affected = [str(value) for value in event.get("affected_serials", [])]
        serial = event.get("usb_serial")
        serial = serial if isinstance(serial, str) else None
        incident: dict[str, object] | None = None
        if kind == "usb_parent_hub_removed":
            scope = f"hub:{hashlib.sha256(str(event['devpath']).encode()).hexdigest()[:16]}"
            incident = self._open_incident(event, scope=scope, affected_serials=affected)
        elif kind in ("usb_can_adapter_removed", "usb_can_netdev_removed"):
            if serial is not None:
                incident = self._incident_for_serial(serial)
            if incident is None:
                scope = f"board:{serial}" if serial else str(event["scope"])
                incident = self._open_incident(
                    event,
                    scope=scope,
                    affected_serials=affected or ([serial] if serial else []),
                )
        elif kind == "usb_parent_hub_added":
            scope = f"hub:{hashlib.sha256(str(event['devpath']).encode()).hexdigest()[:16]}"
            incident = self._active_incidents.get(scope)
        elif kind in ("usb_can_adapter_added", "usb_can_netdev_added") and serial:
            incident = self._incident_for_serial(serial)
        if incident is None:
            return
        self._touch_incident(incident, event)
        if kind.endswith("_added"):
            incident["reappearance_count"] = int(
                incident.get("reappearance_count", 0)
            ) + 1
            incident["reappeared_at"] = event["occurred_at"]

    def process_datagram(self, payload: bytes) -> dict[str, object] | None:
        """Parse one injected/live datagram and return a retained event."""

        try:
            kernel_event = KernelUevent.parse(payload)
        except (TypeError, ValueError):
            with self._lock:
                self._ignored_event_count += 1
            return None
        if kernel_event.action not in ("add", "remove"):
            with self._lock:
                self._ignored_event_count += 1
            return None
        # Real kobject uevents carry a boot-scoped monotonically increasing
        # SEQNUM.  Without it, a restart-stable dedupe identity cannot be
        # proven, so fail closed instead of inventing a durable kernel edge.
        if kernel_event.seqnum is None:
            with self._lock:
                self._ignored_event_count += 1
            return None
        with self._lock:
            vid, pid = _product_identity(kernel_event.product)
            exact_adapter = vid == CAN_USB_VID and pid == CAN_USB_PID
            cached_serial = self._serial_by_board_path.get(kernel_event.devpath)
            serial = (
                self._serial_for_event(kernel_event)
                if exact_adapter
                else cached_serial
            )
            exact_installed_adapter = (
                serial in self.expected_roles_by_serial
                and (exact_adapter or cached_serial is not None)
            )
            known_hub = kernel_event.devpath in self._relevant_hubs
            descendant_serial = self._board_for_descendant(kernel_event.devpath)
            kind: str | None = None
            affected: list[str] = []
            if (
                kernel_event.subsystem == "usb"
                and kernel_event.devtype == "usb_device"
                and exact_installed_adapter
            ):
                kind = f"usb_can_adapter_{'added' if kernel_event.action == 'add' else 'removed'}"
                if serial is not None:
                    affected = [serial]
            elif (
                kernel_event.subsystem == "usb"
                and kernel_event.devtype == "usb_device"
                and known_hub
            ):
                kind = f"usb_parent_hub_{'added' if kernel_event.action == 'add' else 'removed'}"
                affected = sorted(self._hub_serials.get(kernel_event.devpath, set()))
            elif kernel_event.subsystem == "net" and descendant_serial is not None:
                serial = descendant_serial
                kind = f"usb_can_netdev_{'added' if kernel_event.action == 'add' else 'removed'}"
                affected = [serial]
            if kind is None:
                self._ignored_event_count += 1
                return None
            event = self._event_payload(
                kind=kind,
                action=kernel_event.action,
                devpath=kernel_event.devpath,
                seqnum=kernel_event.seqnum,
                serial=serial,
                affected_serials=affected,
                usb_vid=vid,
                usb_pid=pid,
            )
            if not self._queue_event(event):
                return None
            return json.loads(json.dumps(event))

    @staticmethod
    def _role_payloads(role_status: Mapping[str, object]) -> Mapping[str, object]:
        direct = role_status.get("roles")
        if isinstance(direct, Mapping):
            return direct
        nested = role_status.get("role_interfaces")
        if isinstance(nested, Mapping):
            roles = nested.get("roles")
            if isinstance(roles, Mapping):
                return roles
        return {}

    def _serial_is_healthy(
        self,
        serial: str,
        roles: Mapping[str, object],
    ) -> bool:
        for role in self.expected_roles_by_serial.get(serial, ()):
            payload = roles.get(role)
            if not isinstance(payload, Mapping) or payload.get("resolution") != "resolved":
                return False
            expected = payload.get("expected")
            if not isinstance(expected, Mapping) or expected.get("usb_serial") != serial:
                return False
            actual = payload.get("actual")
            if isinstance(actual, Mapping) and actual.get("present") is False:
                return False
            if "safe" in payload and payload.get("safe") is not True:
                return False
        return True

    def reconcile(self, role_status: Mapping[str, object]) -> tuple[str, ...]:
        """Resolve active incidents only after exact role health is re-proven."""

        if not isinstance(role_status, Mapping):
            return ()
        roles = self._role_payloads(role_status)
        if not roles:
            return ()
        generation = role_status.get("generation")
        if not isinstance(generation, str):
            nested = role_status.get("role_interfaces")
            generation = nested.get("generation") if isinstance(nested, Mapping) else None
        generation = generation if isinstance(generation, str) else "generation-unavailable"
        resolved: list[str] = []
        with self._lock:
            for scope, incident in tuple(self._active_incidents.items()):
                affected = [
                    serial
                    for serial in incident.get("affected_serials", [])
                    if isinstance(serial, str)
                ]
                if not affected or not all(
                    self._serial_is_healthy(serial, roles) for serial in affected
                ):
                    continue
                event = self._event_payload(
                    kind="usb_can_recovered",
                    action="reconcile",
                    devpath=str(incident.get("scope", scope)),
                    seqnum=None,
                    serial=None,
                    affected_serials=affected,
                    usb_vid=CAN_USB_VID,
                    usb_pid=CAN_USB_PID,
                    source=RECOVERY_SOURCE,
                    identity_extra=f"{incident['incident_id']}|{generation}",
                )
                self._queue_event(event)
                incident["state"] = "resolved"
                incident["resolved_event_id"] = event["event_id"]
                incident["resolved_at"] = event["occurred_at"]
                incident["resolution"] = "exact_serial_role_health_reestablished"
                incident["notification_eligible"] = False
                resolved.append(str(incident["incident_id"]))
                self._recent_incidents.append(json.loads(json.dumps(incident)))
                self._active_incidents.pop(scope, None)
        return tuple(resolved)

    def persistence_batch(self) -> dict[str, object]:
        """Return pending immutable events plus current incident revisions."""

        with self._lock:
            incidents = [
                *self._active_incidents.values(),
                *self._recent_incidents,
            ]
            return {
                "schema_version": EVENT_SCHEMA_VERSION,
                "source": MONITOR_SOURCE,
                "producer_instance": self.producer_instance,
                "boot_id": self.boot_id,
                "events": json.loads(json.dumps(list(self._pending.values()))),
                "incidents": json.loads(json.dumps(incidents)),
                "dropped_event_count": self._dropped_event_count,
            }

    def acknowledge_events(self, event_ids: Sequence[str]) -> int:
        """Forget only events durably committed by the historian transaction."""

        removed = 0
        with self._lock:
            for event_id in event_ids:
                if isinstance(event_id, str) and self._pending.pop(event_id, None) is not None:
                    removed += 1
        return removed

    def status_snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "schema_version": EVENT_SCHEMA_VERSION,
                "enabled": True,
                "state": self._state,
                "source": MONITOR_SOURCE,
                "producer_instance": self.producer_instance,
                "boot_id": self.boot_id,
                "receive_only": True,
                "hardware_actions": False,
                "filter": {
                    "usb_vid": CAN_USB_VID,
                    "usb_pid": CAN_USB_PID,
                    "expected_serial_count": len(self.expected_roles_by_serial),
                    "relevant_hub_count": len(self._relevant_hubs),
                },
                "started_at": self._started_at,
                "last_event_at": self._last_event_at,
                "last_error": self._last_error,
                "pending_event_count": len(self._pending),
                "dropped_event_count": self._dropped_event_count,
                "ignored_event_count": self._ignored_event_count,
                "active_count": len(self._active_incidents),
                "active": json.loads(json.dumps(list(self._active_incidents.values()))),
                "recent_incidents": json.loads(json.dumps(list(self._recent_incidents))),
                "recent_events": json.loads(json.dumps(list(self._recent_events))),
            }

    def _open_socket(self):
        sock = self.socket_factory(
            socket.AF_NETLINK,
            socket.SOCK_DGRAM,
            NETLINK_KOBJECT_UEVENT,
        )
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
        sock.bind((0, 1))
        sock.settimeout(0.5)
        return sock

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            inventory = self.refresh_inventory()
            inventory_error = inventory.get("error")
            self._stop.clear()
            self._started_at = self.wall_clock().astimezone(timezone.utc).isoformat()
            try:
                self._socket = self._open_socket()
            except OSError as exc:
                self._state = "degraded"
                self._last_error = f"cannot open receive-only uevent socket: {type(exc).__name__}: {exc}"
                return
            self._state = "degraded" if inventory_error else "running"
            if not inventory_error:
                self._last_error = None
            thread = threading.Thread(
                target=self._run,
                name="van-telemetry-usb-uevent",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self._socket.recv(MAX_UEVENT_BYTES)
            except socket.timeout:
                continue
            except OSError as exc:
                if self._stop.is_set():
                    break
                with self._lock:
                    self._state = "degraded"
                    self._last_error = f"uevent receive failed: {type(exc).__name__}: {exc}"
                break
            self.process_datagram(payload)

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        with self._lock:
            sock = self._socket
            thread = self._thread
            self._socket = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if thread is not None:
            thread.join(max(0.0, timeout))
        with self._lock:
            self._thread = None
            self._state = "stopped"


__all__ = (
    "CAN_USB_PID",
    "CAN_USB_VID",
    "KernelUevent",
    "UsbCanIncidentMonitor",
)
