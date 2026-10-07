"""Synthetic hardware snapshots shared by availability tests and status oracles."""

from copy import deepcopy

from lib.vehicle_can_roles import ALL_CAN_ROLES, CAN_ROLE_SPECS
from projects.vehicle_data.models import failure
from tests.test_vehicle_data import FakeAcquirer


def hardware_status(missing=(), *, issues=()):
    roles, inventory = {}, []
    for index, spec in enumerate(CAN_ROLE_SPECS):
        absent = spec.role in missing
        roles[spec.role] = {
            "resolution": "missing" if absent else "resolved",
            "reason": "role_missing" if absent else "ready",
            "channel": None if absent else f"can{index}",
            "expected": spec.as_dict(),
            "actual": {
                "present": None if absent else True,
                "up": None if absent else spec.passive_required,
                "bitrate": spec.bitrate, "fd_enabled": False,
                "one_shot": False, "listen_only": True,
                "controller_state": "ERROR-ACTIVE", "restart_ms": 0,
            },
            "passive_ready": not absent and spec.passive_required,
        }
        if not absent:
            inventory.append({"channel": f"can{index}", "usb_serial": spec.usb_serial,
                              "dev_id": spec.dev_id, "usb_vid": "1d50", "usb_pid": "606f"})
    ccan = roles["c-can"]
    return {
        "channel": ccan["channel"] or "c-can-unresolved",
        "adapter_present": "c-can" not in missing,
        **{key: value for key, value in ccan["actual"].items() if key != "present"},
        "topology": {"bus": "c-can", "usable": "c-can" not in missing, "pair": "6/14"},
        "active_inhibits": [],
        "role_interfaces": {"roles": roles, "inventory": inventory, "issues": list(issues)},
    }


class HardwareAcquirer(FakeAcquirer):
    def __init__(self, missing=()):
        super().__init__(result=failure(
            metric="battery.voltage", unit="V", reason="bus_asleep",
            detail="synthetic passive silence", bus="c-can", acquisition="passive",
        ))
        self.hardware = hardware_status(missing)
        self.probe_error = False

    def status_snapshot(self):
        if self.probe_error:
            raise OSError("synthetic discovery failure")
        return deepcopy(self.hardware)


def availability_status_cases():
    cases = (
        ("adapter/missing", ALL_CAN_ROLES, 140),
        ("adapter/board_a_missing", ("c-can", "b-can"), 140),
        ("adapter/board_b_missing", ("can-ch", "spare"), 140),
        ("adapter/bcan_missing", ("b-can",), 140),
        ("adapter/startup_grace", ALL_CAN_ROLES, 129.999),
        ("adapter/grace_boundary", ALL_CAN_ROLES, 130),
    )
    for label, roles, now in cases:
        def configure(broker, roles=roles, now=now):
            broker.acquirer = HardwareAcquirer(roles)
            broker.monotonic.value = now
            broker.acquire("battery.voltage", "passive")
        yield label, configure
    for fresh in (False, True):
        def returning(broker, fresh=fresh):
            broker.acquirer = HardwareAcquirer(ALL_CAN_ROLES)
            broker.monotonic.value = 140
            broker.acquire("battery.voltage", "passive")
            broker.acquirer.hardware = hardware_status()
            broker.monotonic.value = 142
            broker.acquire("battery.voltage", "passive")
            if fresh:
                broker.monotonic.value = 144
                broker.acquire("battery.voltage", "passive")
        yield "adapter/returned_fresh" if fresh else "adapter/returning", returning
