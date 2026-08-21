"""Canonical installed dual-USBCANFD role configuration for this van.

The vehicle has three dedicated classical-CAN taps plus one intentionally
unused controller.  Linux ``canN`` names are ephemeral; the tuples below are
the stable physical identities shared by telemetry and standalone tools.
This module only reads sysfs through :mod:`lib.can_role_resolver` and never
configures an interface or opens a CAN socket.
"""

from __future__ import annotations

from lib.can_role_resolver import CanRoleSpec, RoleTopology, SysfsCanRoleResolver


BOARD_A_SERIAL = "207C3384413250013"
BOARD_B_SERIAL = "207E33A4413250013"

C_CAN_ROLE = "c-can"
B_CAN_ROLE = "b-can"
CAN_CH_ROLE = "can-ch"
SPARE_ROLE = "spare"
CAN_BUS_ROLES = (C_CAN_ROLE, B_CAN_ROLE, CAN_CH_ROLE)
ALL_CAN_ROLES = CAN_BUS_ROLES + (SPARE_ROLE,)

CAN_ROLE_SPECS = (
    CanRoleSpec(
        role=C_CAN_ROLE,
        board="A",
        connector="CAN1",
        usb_serial=BOARD_A_SERIAL,
        dev_id=0x0,
        bitrate=500000,
        pair="6/14",
    ),
    CanRoleSpec(
        role=B_CAN_ROLE,
        board="A",
        connector="CAN2",
        usb_serial=BOARD_A_SERIAL,
        dev_id=0x1,
        bitrate=125000,
        pair="3/11",
    ),
    CanRoleSpec(
        role=CAN_CH_ROLE,
        board="B",
        connector="CAN1",
        usb_serial=BOARD_B_SERIAL,
        dev_id=0x0,
        bitrate=500000,
        pair="12/13",
    ),
    CanRoleSpec(
        role=SPARE_ROLE,
        board="B",
        connector="CAN2",
        usb_serial=BOARD_B_SERIAL,
        dev_id=0x1,
        bitrate=None,
        pair=None,
        passive_required=False,
    ),
)

_ROLE_ALIASES = {
    "c-can": C_CAN_ROLE,
    "ccan": C_CAN_ROLE,
    "c_can": C_CAN_ROLE,
    "b-can": B_CAN_ROLE,
    "bcan": B_CAN_ROLE,
    "b_can": B_CAN_ROLE,
    "can-ch": CAN_CH_ROLE,
    "canch": CAN_CH_ROLE,
    "can_ch": CAN_CH_ROLE,
    "spare": SPARE_ROLE,
}


def normalize_can_role(role: str) -> str:
    """Normalize registry bus names and conservative spelling aliases."""

    if not isinstance(role, str):
        raise ValueError("CAN role must be a string")
    normalized = _ROLE_ALIASES.get(role.strip().lower())
    if normalized is None:
        raise ValueError(f"unknown CAN role {role!r}")
    return normalized


class InstalledCanRoleResolver:
    """Resolve the canonical installed role set from one sysfs snapshot."""

    def __init__(
        self,
        *,
        resolver: SysfsCanRoleResolver | None = None,
        specs: tuple[CanRoleSpec, ...] = CAN_ROLE_SPECS,
    ):
        self.resolver = resolver or SysfsCanRoleResolver()
        self.specs = tuple(specs)
        roles = tuple(item.role for item in self.specs)
        if set(roles) != set(ALL_CAN_ROLES) or len(roles) != len(ALL_CAN_ROLES):
            raise ValueError(
                "installed role resolver requires c-can, b-can, can-ch, and spare"
            )

    def topology(self) -> RoleTopology:
        return self.resolver.resolve(self.specs)

    def channel_for_bus(
        self,
        bus: str,
        *,
        topology: RoleTopology | None = None,
    ) -> str:
        role = normalize_can_role(bus)
        if role not in CAN_BUS_ROLES:
            raise ValueError(f"{bus!r} is not a connected vehicle bus")
        return (topology or self.topology()).channel_for(role)

    def module_channel(self, module, *, topology: RoleTopology | None = None) -> str:
        bus = getattr(module, "bus", None)
        if not isinstance(bus, str):
            raise ValueError("diagnostic module has no physical bus name")
        return self.channel_for_bus(bus, topology=topology)
