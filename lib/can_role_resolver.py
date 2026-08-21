"""Resolve stable physical CAN roles to ephemeral SocketCAN netdev names.

Linux assigns ``canN`` names in enumeration order.  That order is not a
physical identity and can change after a USB reset.  This module performs only
read-only sysfs inspection and maps caller-supplied role specifications by the
tuple ``(driver, USB VID:PID, USB serial, dev_id)``.

There is deliberately no interface configuration or CAN socket access here.
Callers decide which resolved roles are required and what safety gates apply to
their use.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Iterable


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_USB_ID = re.compile(r"^[0-9a-f]{4}$")


class CanRoleResolutionError(RuntimeError):
    """A physical CAN role does not resolve to exactly one netdev."""

    def __init__(self, role: str, state: str, detail: str):
        super().__init__(detail)
        self.role = role
        self.state = state
        self.detail = detail


@dataclass(frozen=True)
class CanRoleSpec:
    """Immutable identity and expected passive configuration for one role."""

    role: str
    usb_serial: str
    dev_id: int
    bitrate: int | None
    pair: str | None
    board: str
    connector: str
    driver: str = "gs_usb"
    usb_vid: str = "1d50"
    usb_pid: str = "606f"
    passive_required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not _SAFE_NAME.fullmatch(self.role):
            raise ValueError(f"invalid CAN role name: {self.role!r}")
        if not isinstance(self.usb_serial, str) or not self.usb_serial.strip():
            raise ValueError("usb_serial must be a non-empty string")
        object.__setattr__(self, "usb_serial", self.usb_serial.strip())
        if (
            not isinstance(self.dev_id, int)
            or isinstance(self.dev_id, bool)
            or self.dev_id < 0
        ):
            raise ValueError("dev_id must be a nonnegative integer")
        if self.bitrate is not None and (
            not isinstance(self.bitrate, int)
            or isinstance(self.bitrate, bool)
            or self.bitrate <= 0
        ):
            raise ValueError("bitrate must be a positive integer or None")
        if not isinstance(self.driver, str) or not _SAFE_NAME.fullmatch(self.driver):
            raise ValueError(f"invalid driver name: {self.driver!r}")
        for field_name in ("usb_vid", "usb_pid"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not _USB_ID.fullmatch(value.lower()):
                raise ValueError(f"{field_name} must be four hexadecimal digits")
            object.__setattr__(self, field_name, value.lower())

    @property
    def identity(self) -> tuple[str, str, str, str, int]:
        return (
            self.driver,
            self.usb_vid,
            self.usb_pid,
            self.usb_serial,
            self.dev_id,
        )

    def matches(self, device: "NetdevIdentity") -> bool:
        return (
            device.driver == self.driver
            and device.usb_vid == self.usb_vid
            and device.usb_pid == self.usb_pid
            and device.usb_serial == self.usb_serial
            and device.dev_id == self.dev_id
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "board": self.board,
            "connector": self.connector,
            "driver": self.driver,
            "usb_vid": self.usb_vid,
            "usb_pid": self.usb_pid,
            "usb_serial": self.usb_serial,
            "dev_id": self.dev_id,
            "bitrate": self.bitrate,
            "pair": self.pair,
            "passive_required": self.passive_required,
        }


@dataclass(frozen=True)
class NetdevIdentity:
    """One SocketCAN netdev and the immutable USB identity above it."""

    channel: str
    driver: str
    usb_vid: str
    usb_pid: str
    usb_serial: str
    dev_id: int
    sysfs_path: str

    @property
    def identity(self) -> tuple[str, str, str, str, int]:
        """Immutable hardware identity, excluding ephemeral netdev/path names."""

        return (
            self.driver,
            self.usb_vid,
            self.usb_pid,
            self.usb_serial,
            self.dev_id,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "channel": self.channel,
            "driver": self.driver,
            "usb_vid": self.usb_vid,
            "usb_pid": self.usb_pid,
            "usb_serial": self.usb_serial,
            "dev_id": self.dev_id,
            "sysfs_path": self.sysfs_path,
        }


@dataclass(frozen=True)
class DiscoveryIssue:
    """A netdev-shaped sysfs entry that could not be identified safely."""

    channel: str
    reason: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "channel": self.channel,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RoleResolution:
    """Resolution outcome for exactly one requested role."""

    spec: CanRoleSpec
    state: str
    matches: tuple[NetdevIdentity, ...]
    detail: str

    def __post_init__(self) -> None:
        if self.state not in ("resolved", "missing", "ambiguous"):
            raise ValueError(f"invalid role resolution state: {self.state!r}")
        if self.state == "resolved" and len(self.matches) != 1:
            raise ValueError("resolved role must contain exactly one match")
        if self.state == "missing" and self.matches:
            raise ValueError("missing role cannot contain matches")
        if self.state == "ambiguous" and len(self.matches) < 2:
            raise ValueError("ambiguous role must contain at least two matches")

    @property
    def channel(self) -> str | None:
        return self.matches[0].channel if self.state == "resolved" else None

    @property
    def device(self) -> NetdevIdentity | None:
        return self.matches[0] if self.state == "resolved" else None

    def require_channel(self) -> str:
        if self.channel is None:
            raise CanRoleResolutionError(self.spec.role, self.state, self.detail)
        return self.channel

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "channel": self.channel,
            "detail": self.detail,
            "matches": [item.as_dict() for item in self.matches],
        }


@dataclass(frozen=True)
class RoleTopology:
    """One atomic-enough read-only sysfs inventory and role resolution pass."""

    resolutions: tuple[RoleResolution, ...]
    inventory: tuple[NetdevIdentity, ...]
    issues: tuple[DiscoveryIssue, ...]
    fingerprint: str

    def resolution(self, role: str) -> RoleResolution:
        for item in self.resolutions:
            if item.spec.role == role:
                return item
        raise KeyError(f"unknown CAN role {role!r}")

    def channel_for(self, role: str) -> str:
        return self.resolution(role).require_channel()

    def channel_map(
        self, required: Iterable[str] | None = None
    ) -> dict[str, str]:
        roles = (
            tuple(required)
            if required is not None
            else tuple(item.spec.role for item in self.resolutions)
        )
        return {role: self.channel_for(role) for role in roles}

    def all_resolved(self, required: Iterable[str] | None = None) -> bool:
        roles = (
            tuple(required)
            if required is not None
            else tuple(item.spec.role for item in self.resolutions)
        )
        try:
            return all(self.resolution(role).state == "resolved" for role in roles)
        except KeyError:
            return False

    def as_dict(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "roles": {
                item.spec.role: item.as_dict() for item in self.resolutions
            },
            "inventory": [item.as_dict() for item in self.inventory],
            "issues": [item.as_dict() for item in self.issues],
        }


class SysfsCanRoleResolver:
    """Read sysfs and resolve caller-provided physical role specifications."""

    def __init__(self, sys_class_net_root: str | Path = "/sys/class/net"):
        self.sys_class_net_root = Path(sys_class_net_root)

    @staticmethod
    def _read_text(path: Path) -> str:
        return path.read_text(encoding="utf-8").strip()

    @staticmethod
    def _usb_ancestor(device_path: Path) -> Path | None:
        current = device_path.resolve(strict=True)
        while True:
            if all(
                (current / name).is_file()
                for name in ("idVendor", "idProduct", "serial")
            ):
                return current
            if current == current.parent:
                return None
            current = current.parent

    def inventory(
        self, *, drivers: Iterable[str] = ("gs_usb",)
    ) -> tuple[tuple[NetdevIdentity, ...], tuple[DiscoveryIssue, ...]]:
        """Return identified netdevs and fail-closed discovery issues.

        Entries without ``dev_id`` are not SocketCAN candidates for this
        resolver and are ignored.  A candidate with an expected driver but
        incomplete USB ancestry is reported as an issue, never guessed.
        """
        allowed_drivers = frozenset(drivers)
        devices: list[NetdevIdentity] = []
        issues: list[DiscoveryIssue] = []
        try:
            entries = sorted(self.sys_class_net_root.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            return (), (
                DiscoveryIssue(
                    channel="*",
                    reason="sysfs_unavailable",
                    detail=f"cannot enumerate {self.sys_class_net_root}: {exc}",
                ),
            )

        for net_path in entries:
            if not (net_path / "dev_id").is_file():
                continue
            # Linux exposes dev_id on several virtual/non-CAN netdevs too.
            # ARPHRD_CAN is 280; ignore an explicitly different link type
            # before attempting USB/driver identity discovery.
            try:
                link_type = int(self._read_text(net_path / "type"), 0)
            except (OSError, RuntimeError, ValueError):
                link_type = None
            if link_type is not None and link_type != 280:
                continue
            channel = net_path.name
            if not _SAFE_NAME.fullmatch(channel):
                issues.append(
                    DiscoveryIssue(
                        channel=channel,
                        reason="unsafe_channel_name",
                        detail="netdev name is unsafe for SocketCAN tooling",
                    )
                )
                continue
            try:
                driver = (net_path / "device" / "driver").resolve(strict=True).name
            except (OSError, RuntimeError) as exc:
                issues.append(
                    DiscoveryIssue(
                        channel=channel,
                        reason="driver_unavailable",
                        detail=f"cannot read netdev driver: {exc}",
                    )
                )
                continue
            if driver not in allowed_drivers:
                continue
            try:
                dev_id = int(self._read_text(net_path / "dev_id"), 0)
                if dev_id < 0:
                    raise ValueError("negative dev_id")
            except (OSError, RuntimeError, ValueError) as exc:
                issues.append(
                    DiscoveryIssue(
                        channel=channel,
                        reason="invalid_dev_id",
                        detail=f"cannot parse dev_id: {exc}",
                    )
                )
                continue
            try:
                usb_path = self._usb_ancestor(net_path / "device")
                if usb_path is None:
                    raise FileNotFoundError("no USB identity ancestor")
                usb_vid = self._read_text(usb_path / "idVendor").lower()
                usb_pid = self._read_text(usb_path / "idProduct").lower()
                usb_serial = self._read_text(usb_path / "serial")
                if not _USB_ID.fullmatch(usb_vid) or not _USB_ID.fullmatch(usb_pid):
                    raise ValueError("invalid USB VID:PID")
                if not usb_serial:
                    raise ValueError("empty USB serial")
            except (OSError, RuntimeError, ValueError) as exc:
                issues.append(
                    DiscoveryIssue(
                        channel=channel,
                        reason="usb_identity_unavailable",
                        detail=f"cannot read immutable USB identity: {exc}",
                    )
                )
                continue
            try:
                sysfs_path = str(net_path.resolve(strict=True))
            except (OSError, RuntimeError) as exc:
                issues.append(
                    DiscoveryIssue(
                        channel=channel,
                        reason="sysfs_identity_changed",
                        detail=f"netdev disappeared during inventory: {exc}",
                    )
                )
                continue
            devices.append(
                NetdevIdentity(
                    channel=channel,
                    driver=driver,
                    usb_vid=usb_vid,
                    usb_pid=usb_pid,
                    usb_serial=usb_serial,
                    dev_id=dev_id,
                    sysfs_path=sysfs_path,
                )
            )
        return tuple(devices), tuple(issues)

    @staticmethod
    def _validate_specs(specs: tuple[CanRoleSpec, ...]) -> None:
        roles = [item.role for item in specs]
        if len(roles) != len(set(roles)):
            raise ValueError("CAN role names must be unique")
        identities = [
            (
                item.driver,
                item.usb_vid,
                item.usb_pid,
                item.usb_serial,
                item.dev_id,
            )
            for item in specs
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("CAN role USB serial/dev_id identities must be unique")

    def resolve(self, specs: Iterable[CanRoleSpec]) -> RoleTopology:
        requested = tuple(specs)
        self._validate_specs(requested)
        drivers = {item.driver for item in requested}
        inventory, issues = self.inventory(drivers=drivers)
        resolutions: list[RoleResolution] = []
        for spec in requested:
            matches = tuple(
                sorted(
                    (item for item in inventory if spec.matches(item)),
                    key=lambda item: item.channel,
                )
            )
            if len(matches) == 1:
                state = "resolved"
                detail = (
                    f"{spec.role} resolved by USB serial/dev_id to "
                    f"{matches[0].channel}"
                )
            elif not matches:
                state = "missing"
                detail = (
                    f"no exact {spec.driver} {spec.usb_vid}:{spec.usb_pid} "
                    f"serial {spec.usb_serial} dev_id 0x{spec.dev_id:x} match"
                )
            else:
                state = "ambiguous"
                detail = (
                    f"{len(matches)} exact matches for {spec.role}: "
                    + ", ".join(item.channel for item in matches)
                )
            resolutions.append(
                RoleResolution(
                    spec=spec,
                    state=state,
                    matches=matches,
                    detail=detail,
                )
            )

        fingerprint_text = "|".join(
            f"{item.spec.role}:{item.state}:"
            + ",".join(
                f"{match.channel}/{match.usb_serial}/{match.dev_id}"
                for match in item.matches
            )
            for item in resolutions
        )
        fingerprint = hashlib.sha256(fingerprint_text.encode("utf-8")).hexdigest()[:16]
        return RoleTopology(
            resolutions=tuple(resolutions),
            inventory=inventory,
            issues=issues,
            fingerprint=fingerprint,
        )


__all__ = (
    "CanRoleResolutionError",
    "CanRoleSpec",
    "DiscoveryIssue",
    "NetdevIdentity",
    "RoleResolution",
    "RoleTopology",
    "SysfsCanRoleResolver",
)
