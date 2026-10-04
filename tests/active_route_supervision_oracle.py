#!/usr/bin/env python3
"""Trace parent role ownership around the real active-drive session offline.

Usage: python THIS.py REPO OUTPUT.json. The unchanged base oracle's OS fakes
are reused. Only the supervisor's subprocess boundary is replaced: its child
runs the target tree's actual session synchronously. The real parent role-lock
scope, resolution, helper arguments and release order remain observable. This
supplements, rather than replaces, the subprocess/heartbeat tests.
"""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

import active_route_effect_oracle as base


class SupervisedWorld(base.World):
    def __init__(self, root, scenario):
        super().__init__(root, scenario)
        self.parent_resolved = False
        self.held_names = set()

    def inventory(self, **kwargs):
        # Count the parent's initial resolution separately from child arm and
        # revalidation faults; every underlying sysfs read is still recorded.
        if not self.parent_resolved:
            scenario = self.scenario
            self.scenario = "normal" if scenario != "identity_initial" else scenario
            try:
                result = super().inventory(**kwargs)
            finally:
                self.scenario = scenario
            self.inventory_count = 0
            self.parent_resolved = True
            return result
        return super().inventory(**kwargs)

    def acquire(self, name):
        if name in self.held_names:
            self.log("lock.acquire", name)
            raise base.diagnostic_safety.ChannelLockError("fixture duplicate exclusive ownership")
        if self.scenario == "channel_contention" and name == "can7":
            self.log("lock.acquire", name)
            raise base.diagnostic_safety.ChannelLockError("fixture channel contention")
        handle = super().acquire(name)
        self.held_names.add(name)
        return handle

    def release(self, handle):
        self.held_names.discard(self.lock_names.get(id(handle)))
        super().release(handle)


def run_case(scenario):
    with tempfile.TemporaryDirectory(prefix="supervised-effects-") as directory:
        world = SupervisedWorld(Path(directory), scenario)
        spec = base.resolver.CanRoleSpec("c-can", "serial-a", 0, 500000, "6/14", "A", "CAN1")

        def topology():
            inventory, issues = world.inventory()
            matches = tuple(item for item in inventory if spec.matches(item))
            state = "resolved" if len(matches) == 1 else "missing" if not matches else "ambiguous"
            return base.resolver.RoleTopology(
                (base.resolver.RoleResolution(spec, state, matches, "fixture"),),
                inventory, issues, "fixture",
            )

        def helper_factory(**kwargs):
            world.log("helper.create", **{k: v for k, v in kwargs.items() if k != "event_handler"})
            def run(stop_event):
                world.log("helper.run", stop_event.is_set())
                # The parent's current API passes exactly these identities to the
                # subprocess. Fail the harness rather than silently ignoring drift.
                assert kwargs["channel"] == "can7"
                assert kwargs["expected_usb_serial"] == "serial-a"
                assert kwargs["expected_dev_id"] == 0
                return base.active_case(world)
            return SimpleNamespace(run=run)

        supervisor = base.runtime.RoleAwareActiveDriveSupervisor(
            SimpleNamespace(topology=topology),
            event_handler=lambda event: world.log("parent.event", event),
            supervisor_factory=helper_factory,
        )
        with world.patches():
            outcome = supervisor.run(SimpleNamespace(is_set=lambda: False))
            world.log("return", outcome)
        assert not world.held_names, scenario
        return json.loads(json.dumps(world.effects).replace(directory, "<sysfs>"))


def main():
    sys.addaudithook(base.forbid_hardware)
    scenarios = (
        "normal", "already_passive_cleanup", "identity_initial", "identity_arm",
        "identity_revalidate", "renumber", "wrong_bitrate", "fd_enabled", "fd_unknown",
        "listen_only_stuck", "one_shot", "one_shot_unknown", "restart_nonzero", "restart_unknown",
        "error_passive", "bus_off", "restore_command_failure", "restore_verify_failure",
        "keyboard_interrupt", "sigterm", "lock_contention", "channel_contention", "existing_inhibit",
        "arm_wrong_bitrate", "arm_fd_enabled", "arm_one_shot", "arm_restart_nonzero",
        "arm_error_passive", "arm_bus_off",
    )
    result = {scenario: run_case(scenario) for scenario in scenarios}
    for scenario, rows in result.items():
        assert rows[0][:2] == ["lock.acquire", ["can-role-c-can"]], scenario
        if scenario != "lock_contention":
            assert rows[-2][:2] == ["lock.release", ["can-role-c-can"]], scenario
        assert "unfaked external effect" not in json.dumps(rows), scenario
    normal = result["normal"]
    assert normal[-1][1][0]["restored"] is True
    assert len([row for row in normal if row[0] == "socket.send"]) >= 5
    assert result["channel_contention"][-1][1][0]["reason"] == "can_busy"
    for scenario in ("restore_command_failure", "restore_verify_failure"):
        assert any(row[0] == "inhibit.begin" for row in result[scenario])
        assert result[scenario][-1][1][0]["restored"] is False
    for scenario in ("keyboard_interrupt", "sigterm"):
        assert any(row[0] == "interrupt" for row in result[scenario])
        assert result[scenario][-1][1][0]["restored"] is True
    Path(sys.argv[2]).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(f"recorded {len(result)} parent/child ordered-effect scenarios")


if __name__ == "__main__":
    main()
