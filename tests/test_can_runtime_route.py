import unittest
from types import SimpleNamespace
from unittest import mock

from lib import can_runtime_route as route
from lib import canbus
from lib.can_role_resolver import NetdevIdentity, RoleResolution, RoleTopology
from lib.modules import MODULES
from lib.vehicle_can_roles import CAN_ROLE_SPECS


def interface(
    channel,
    *,
    bitrate=500_000,
    listen_only=True,
    fd_enabled=False,
    restart_ms=0,
    up=True,
    controller_state="ERROR-ACTIVE",
):
    return canbus.InterfaceState(
        channel=channel,
        present=True,
        up=up,
        bitrate=bitrate,
        listen_only=listen_only,
        controller_state=controller_state,
        restart_ms=restart_ms,
        fd_enabled=fd_enabled,
    )


DEFAULT_CHANNELS = {
    "c-can": "can7",
    "b-can": "can2",
    "can-ch": "can9",
    "spare": "can4",
}


def make_device(spec, channel, *, suffix=""):
    return NetdevIdentity(
        channel=channel,
        driver=spec.driver,
        usb_vid=spec.usb_vid,
        usb_pid=spec.usb_pid,
        usb_serial=spec.usb_serial,
        dev_id=spec.dev_id,
        sysfs_path=f"/sys/class/net/{channel}{suffix}",
    )


def make_topology(
    channels=None,
    *,
    missing=(),
    ambiguous=(),
    fingerprint="topology-1",
):
    channels = dict(DEFAULT_CHANNELS if channels is None else channels)
    resolutions = []
    inventory = []
    for spec in CAN_ROLE_SPECS:
        channel = channels.get(spec.role, f"unassigned-{spec.role}")
        if spec.role in missing:
            matches = ()
            state = "missing"
        elif spec.role in ambiguous:
            matches = (
                make_device(spec, channel, suffix="-a"),
                make_device(spec, f"{channel}0", suffix="-b"),
            )
            state = "ambiguous"
            inventory.extend(matches)
        else:
            matches = (make_device(spec, channel),)
            state = "resolved"
            inventory.extend(matches)
        resolutions.append(
            RoleResolution(
                spec=spec,
                state=state,
                matches=matches,
                detail=f"{spec.role} {state}",
            )
        )
    return RoleTopology(
        resolutions=tuple(resolutions),
        inventory=tuple(inventory),
        issues=(),
        fingerprint=fingerprint,
    )


class SequenceManager:
    def __init__(self, *topologies):
        self.topologies = list(topologies)

    def topology(self):
        if len(self.topologies) > 1:
            return self.topologies.pop(0)
        return self.topologies[0]


class RuntimeRouteTests(unittest.TestCase):
    def lock_patches(self, events):
        def acquire(name):
            events.append(("acquire", name))
            return SimpleNamespace(channel=name)

        def release(handle):
            events.append(("release", handle.channel))

        return (
            mock.patch.object(route.diagnostic_safety, "acquire_channel_lock", side_effect=acquire),
            mock.patch.object(route.diagnostic_safety, "release_channel_lock", side_effect=release),
        )

    def normal_active_patches(self, *, channel="can7", bitrate=500_000):
        initial = interface(channel, bitrate=bitrate)
        armed = interface(channel, bitrate=bitrate, listen_only=False)
        return initial, armed

    def test_missing_and_ambiguous_roles_fail_closed(self):
        for topology in (
            make_topology(missing=("c-can",)),
            make_topology(ambiguous=("c-can",)),
        ):
            with self.subTest(state=topology.resolution("c-can").state):
                with self.assertRaises(route.RuntimeRouteError):
                    route.resolve_module_route(
                        MODULES["pcm"], manager=SequenceManager(topology)
                    )

    def test_renumber_during_lock_acquisition_releases_in_reverse_order(self):
        before = make_topology()
        after_channels = dict(DEFAULT_CHANNELS, **{"c-can": "can8"})
        after = make_topology(after_channels, fingerprint="topology-2")
        events = []
        acquire_patch, release_patch = self.lock_patches(events)
        with acquire_patch, release_patch:
            with self.assertRaises(route.RuntimeRouteError):
                route.acquire_active_module_route(
                    MODULES["pcm"], manager=SequenceManager(before, after)
                )
        self.assertEqual(
            events,
            [
                ("acquire", "can-role-c-can"),
                ("acquire", "can7"),
                ("release", "can7"),
                ("release", "can-role-c-can"),
            ],
        )

    def test_role_lock_precedes_channel_lock_and_release_reverses_it(self):
        events = []
        acquire_patch, release_patch = self.lock_patches(events)
        with acquire_patch, release_patch:
            ownership = route.acquire_active_module_route(
                MODULES["pcm"], manager=SequenceManager(make_topology())
            )
            ownership.release()
        self.assertEqual(
            events,
            [
                ("acquire", "can-role-c-can"),
                ("acquire", "can7"),
                ("release", "can7"),
                ("release", "can-role-c-can"),
            ],
        )

    def test_pair_mismatch_rejects_before_interface_access(self):
        events = []
        acquire_patch, release_patch = self.lock_patches(events)
        with (
            acquire_patch,
            release_patch,
            mock.patch.object(route.canbus, "interface_state") as state,
            mock.patch.object(route.canbus, "ip_up") as ip_up,
        ):
            with self.assertRaises(route.RuntimeRouteError):
                route.acquire_armed_module_route(
                    MODULES["pcm"],
                    asserted_pair="3/11",
                    prearm_check=lambda: (),
                    manager=SequenceManager(make_topology()),
                )
        state.assert_not_called()
        ip_up.assert_not_called()

    def test_service_conflict_is_checked_before_any_ip_up(self):
        events = []
        acquire_patch, release_patch = self.lock_patches(events)

        def conflicts():
            events.append(("check", "services"))
            return ("van-telemetry.service is active",)

        with (
            acquire_patch,
            release_patch,
            mock.patch.object(route.can_operation_state, "active_inhibits", return_value=()),
            mock.patch.object(route.canbus, "interface_state") as state,
            mock.patch.object(route.canbus, "ip_up") as ip_up,
        ):
            with self.assertRaises(route.RuntimeRouteError):
                route.acquire_armed_module_route(
                    MODULES["pcm"],
                    asserted_pair="6/14",
                    prearm_check=conflicts,
                    manager=SequenceManager(make_topology()),
                )
        self.assertIn(("check", "services"), events)
        state.assert_not_called()
        ip_up.assert_not_called()

    def test_preexisting_inhibit_rejects_before_service_check_or_ip_up(self):
        callback = mock.Mock(return_value=())
        events = []
        acquire_patch, release_patch = self.lock_patches(events)
        with (
            acquire_patch,
            release_patch,
            mock.patch.object(
                route.can_operation_state,
                "active_inhibits",
                return_value=({"name": "alfaobd"},),
            ),
            mock.patch.object(route.canbus, "ip_up") as ip_up,
        ):
            with self.assertRaises(route.RuntimeRouteError):
                route.acquire_armed_module_route(
                    MODULES["pcm"],
                    asserted_pair="6/14",
                    prearm_check=callback,
                    manager=SequenceManager(make_topology()),
                )
        callback.assert_not_called()
        ip_up.assert_not_called()

    def test_exact_passive_baseline_rejects_fd_restart_and_other_variants(self):
        invalid = {
            "fd": interface("can7", fd_enabled=True),
            "unknown_fd": interface("can7", fd_enabled=None),
            "restart": interface("can7", restart_ms=100),
            "armed": interface("can7", listen_only=False),
            "wrong_rate": interface("can7", bitrate=125_000),
            "bus_off": interface("can7", controller_state="BUS-OFF"),
            "down": interface("can7", up=False),
        }
        for label, state in invalid.items():
            with self.subTest(label=label):
                events = []
                acquire_patch, release_patch = self.lock_patches(events)
                with (
                    acquire_patch,
                    release_patch,
                    mock.patch.object(route.can_operation_state, "active_inhibits", return_value=()),
                    mock.patch.object(route.canbus, "interface_state", return_value=state),
                    mock.patch.object(route.canbus, "ip_up") as ip_up,
                ):
                    with self.assertRaises(route.RuntimeRouteError):
                        route.acquire_armed_module_route(
                            MODULES["pcm"],
                            asserted_pair="6/14",
                            prearm_check=lambda: (),
                            manager=SequenceManager(make_topology()),
                        )
                ip_up.assert_not_called()

    def test_arm_command_and_readback_then_normal_release_exactly_restore(self):
        topology = make_topology()
        manager = SequenceManager(topology)
        initial, armed = self.normal_active_patches()
        events = []
        acquire_patch, release_patch = self.lock_patches(events)

        def restore(state, *, noninteractive=False):
            events.append(("restore", state, noninteractive))
            return True

        with (
            acquire_patch,
            release_patch,
            mock.patch.object(route.can_operation_state, "active_inhibits", side_effect=((), ())),
            mock.patch.object(route.canbus, "interface_state", side_effect=(initial, armed, initial)),
            mock.patch.object(route.canbus, "ip_up", return_value=True) as ip_up,
            mock.patch.object(route.canbus, "restore_interface_state", side_effect=restore),
        ):
            ownership = route.acquire_armed_module_route(
                MODULES["pcm"],
                asserted_pair="6/14",
                prearm_check=lambda: (),
                manager=manager,
            )
            self.assertTrue(ownership.armed)
            self.assertTrue(ownership.release())

        ip_up.assert_called_once_with(
            "can7",
            500_000,
            listen_only=False,
            restart_ms=0,
            noninteractive=True,
        )
        restore_event = next(index for index, item in enumerate(events) if item[0] == "restore")
        channel_release = events.index(("release", "can7"))
        self.assertLess(restore_event, channel_release)
        restored = events[restore_event]
        self.assertIs(restored[1], initial)
        self.assertTrue(restored[2])

    def test_inhibit_appearing_during_arm_restores_before_unlock(self):
        initial, armed = self.normal_active_patches()
        events = []
        acquire_patch, release_patch = self.lock_patches(events)

        def restore(state, *, noninteractive=False):
            events.append(("restore", state.channel))
            return True

        with (
            acquire_patch,
            release_patch,
            mock.patch.object(
                route.can_operation_state,
                "active_inhibits",
                side_effect=((), ({"name": "campaign-started"},)),
            ),
            mock.patch.object(route.canbus, "interface_state", side_effect=(initial, armed, initial)),
            mock.patch.object(route.canbus, "ip_up", return_value=True),
            mock.patch.object(route.canbus, "restore_interface_state", side_effect=restore),
        ):
            with self.assertRaises(route.RuntimeRouteError):
                route.acquire_armed_module_route(
                    MODULES["pcm"],
                    asserted_pair="6/14",
                    prearm_check=lambda: (),
                    manager=SequenceManager(make_topology()),
                )
        self.assertLess(events.index(("restore", "can7")), events.index(("release", "can7")))

    def test_failed_arm_readback_restores_before_unlock(self):
        initial, _armed = self.normal_active_patches()
        invalid_armed = interface("can7", listen_only=True)
        events = []
        acquire_patch, release_patch = self.lock_patches(events)

        def restore(state, *, noninteractive=False):
            events.append(("restore", state.channel))
            return True

        with (
            acquire_patch,
            release_patch,
            mock.patch.object(route.can_operation_state, "active_inhibits", return_value=()),
            mock.patch.object(
                route.canbus,
                "interface_state",
                side_effect=(initial, invalid_armed, initial),
            ),
            mock.patch.object(route.canbus, "ip_up", return_value=True),
            mock.patch.object(route.canbus, "restore_interface_state", side_effect=restore),
        ):
            with self.assertRaises(route.RuntimeRouteError):
                route.acquire_armed_module_route(
                    MODULES["pcm"],
                    asserted_pair="6/14",
                    prearm_check=lambda: (),
                    manager=SequenceManager(make_topology()),
                )
        self.assertLess(events.index(("restore", "can7")), events.index(("release", "can7")))

    def test_restoration_failure_sets_global_inhibit_before_unlock(self):
        initial, armed = self.normal_active_patches()
        events = []
        acquire_patch, release_patch = self.lock_patches(events)

        def restore(_state, *, noninteractive=False):
            events.append(("restore-failed", noninteractive))
            return False

        def inhibit(name, *, channel, reason):
            events.append(("inhibit", name, channel, reason))

        with (
            acquire_patch,
            release_patch,
            mock.patch.object(route.can_operation_state, "active_inhibits", side_effect=((), ())),
            mock.patch.object(route.can_operation_state, "begin_inhibit", side_effect=inhibit),
            mock.patch.object(route.canbus, "interface_state", side_effect=(initial, armed)),
            mock.patch.object(route.canbus, "ip_up", return_value=True),
            mock.patch.object(route.canbus, "restore_interface_state", side_effect=restore),
        ):
            ownership = route.acquire_armed_module_route(
                MODULES["pcm"],
                asserted_pair="6/14",
                prearm_check=lambda: (),
                manager=SequenceManager(make_topology()),
            )
            self.assertFalse(ownership.release())

        inhibit_event = next(item for item in events if item[0] == "inhibit")
        self.assertEqual(inhibit_event[1:3], ("runtime-route-restoration-failed", "*"))
        self.assertLess(events.index(inhibit_event), events.index(("release", "can7")))

    def test_all_three_roles_resolve_independently_and_only_selected_bus_is_armed(self):
        topology = make_topology()
        manager = SequenceManager(topology)
        resolved = {
            bus: route.resolve_bus_route(bus, manager=manager).channel
            for bus in ("c-can", "b-can", "can-ch")
        }
        self.assertEqual(
            resolved,
            {"c-can": "can7", "b-can": "can2", "can-ch": "can9"},
        )

        initial = interface("can2", bitrate=125_000)
        armed = interface("can2", bitrate=125_000, listen_only=False)
        events = []
        acquire_patch, release_patch = self.lock_patches(events)
        with (
            acquire_patch,
            release_patch,
            mock.patch.object(route.can_operation_state, "active_inhibits", side_effect=((), ())),
            mock.patch.object(route.canbus, "interface_state", side_effect=(initial, armed, initial)),
            mock.patch.object(route.canbus, "ip_up", return_value=True) as ip_up,
            mock.patch.object(route.canbus, "restore_interface_state", return_value=True),
        ):
            ownership = route.acquire_armed_module_route(
                MODULES["uconnect_bcan"],
                asserted_pair="3/11",
                prearm_check=lambda: (),
                manager=manager,
            )
            ownership.release()
        self.assertEqual({call.args[0] for call in ip_up.call_args_list}, {"can2"})


if __name__ == "__main__":
    unittest.main()
