"""Offline adapter loss, recovery and status/authorization contract tests."""

import json
from types import SimpleNamespace
from unittest import mock

import pytest

from lib.vehicle_can_roles import CAN_BUS_ROLES
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.can_availability import (
    CAN_ADAPTER_INITIALIZING, CAN_ADAPTER_MISSING, CAN_ADAPTER_RECOVERING,
    CanAvailability, interface_presence, vehicle_hardware_unavailable,
)
from projects.vehicle_data.can_runtime import RoleAwareVoltageAcquirer
from projects.vehicle_data.models import failure
from tests.broker_pure_oracle import delivery_rows, fixed_time, status_rows
from tests.can_missing_cases import HardwareAcquirer, hardware_status
from tests.test_vehicle_data import FakeClock


@pytest.fixture
def rig():
    clock = FakeClock()
    acquirer = HardwareAcquirer()
    broker = TelemetryBroker(acquirer=acquirer, monotonic=clock)
    clock.value = 132
    broker.acquire("battery.voltage", "passive")
    return broker, acquirer, clock


def sample(rig, missing=(), *, hardware=None, now=None):
    broker, acquirer, clock = rig
    clock.value = clock.value + 2 if now is None else now
    acquirer.hardware = hardware_status(missing) if hardware is None else hardware
    broker.acquire("battery.voltage", "passive")
    return broker.status_response()


def publish_rpm(broker, rpm=751):
    return broker.publish_observation(
        "engine.rpm", value=rpm, unit="rpm", source="ccan.broadcast.0x0fc",
        bus="c-can", quality="observed_alfa_scale",
    )


@pytest.mark.parametrize("roles,label", [
    (CAN_BUS_ROLES, "CAN adapters missing"),
    (("c-can", "b-can"), "C-CAN / B-CAN adapters missing"),
    (("can-ch", "spare"), "CAN-CH adapter missing"),
    (("c-can",), "C-CAN adapter missing"),
    (("b-can",), "B-CAN adapter missing"),
    (("can-ch",), "CAN-CH adapter missing"),
])
def test_absence_beats_silence_and_disables_permission(rig, roles, label):
    payload = sample(rig, roles)
    vehicle = payload["vehicle_state"]
    assert vehicle["state"] == "unknown"
    assert vehicle["running"] is None
    assert vehicle["confidence"] == "unavailable"
    assert vehicle["basis"] == CAN_ADAPTER_MISSING
    assert vehicle["detail"].startswith(label + "; ")
    assert payload["active_acquisition_permitted"] is False


def test_absence_beats_fresh_rpm_and_surviving_bus_publisher(rig):
    broker, _, _ = rig
    assert publish_rpm(broker).available
    assert broker.status_response()["vehicle_state"]["state"] == "running"
    sample(rig, ("can-ch",))
    assert publish_rpm(broker).available
    assert broker.metric_response("engine.rpm")["available"] is True
    assert broker.status_response()["vehicle_state"]["basis"] == CAN_ADAPTER_MISSING


@pytest.mark.parametrize("elapsed,basis", [(0, CAN_ADAPTER_INITIALIZING),
    (29.999, CAN_ADAPTER_INITIALIZING), (30, CAN_ADAPTER_MISSING),
    (30.001, CAN_ADAPTER_MISSING)])
def test_startup_grace_is_not_permission_grace(elapsed, basis):
    clock = FakeClock()
    acquirer = HardwareAcquirer(CAN_BUS_ROLES)
    broker = TelemetryBroker(acquirer=acquirer, monotonic=clock)
    clock.value += elapsed
    broker.acquire("battery.voltage", "passive")
    status = broker.status_response()
    assert status["vehicle_state"]["basis"] == basis
    assert status["vehicle_state"]["running"] is None
    assert status["active_acquisition_permitted"] is False
    if elapsed < 30:
        assert "missing" not in status["vehicle_state"]["detail"]


def test_get_does_not_refresh_interface_observation_time(rig):
    broker, _, clock = rig
    first = sample(rig, CAN_BUS_ROLES)["vehicle_state"]
    clock.value += 7
    later = broker.status_response()["vehicle_state"]
    assert later["observed_at"] == first["observed_at"]
    assert later["age_ms"] == 7000


def test_return_does_not_resurrect_retained_asleep(rig):
    sample(rig, CAN_BUS_ROLES)
    returning = sample(rig)["vehicle_state"]
    assert returning["basis"] == CAN_ADAPTER_RECOVERING
    assert returning["running"] is None
    resumed = sample(rig)["vehicle_state"]
    assert resumed["state"] == "asleep"
    assert resumed["basis"] == "passive_bus_silence"


def test_brief_return_requires_subsequent_rpm(rig):
    broker, _, clock = rig
    publish_rpm(broker)
    sample(rig, ("b-can",), now=133.1)
    assert broker.status_response()["vehicle_state"]["basis"] == CAN_ADAPTER_MISSING
    assert sample(rig, now=134.2)["vehicle_state"]["basis"] == CAN_ADAPTER_RECOVERING
    clock.value = 134.3
    assert publish_rpm(broker).available
    assert broker.status_response()["vehicle_state"]["state"] == "running"


@pytest.mark.parametrize("recovery_kind", ["failed", "incomplete", "no_roles", "ambiguous", "partial"])
def test_unproven_return_cannot_clear_loss(rig, recovery_kind):
    broker, acquirer, _ = rig
    sample(rig, CAN_BUS_ROLES)
    hardware = hardware_status()
    if recovery_kind == "failed":
        acquirer.probe_error = True
    elif recovery_kind == "incomplete":
        del hardware["role_interfaces"]["roles"]["b-can"]
    elif recovery_kind == "no_roles":
        del hardware["role_interfaces"]
    elif recovery_kind == "ambiguous":
        hardware["role_interfaces"]["roles"]["b-can"]["resolution"] = "ambiguous"
    else:
        hardware = hardware_status(("b-can",))
    state = sample(rig, hardware=hardware)["vehicle_state"]
    assert vehicle_hardware_unavailable(state)
    assert publish_rpm(broker).available
    assert vehicle_hardware_unavailable(broker.status_response()["vehicle_state"])


@pytest.mark.parametrize("reason", ["sysfs_unavailable", "usb_identity_unavailable", "driver_unavailable"])
def test_discovery_failure_is_not_a_physical_missing_claim(rig, reason):
    hardware = hardware_status(CAN_BUS_ROLES, issues=({"reason": reason},))
    state = sample(rig, hardware=hardware)["vehicle_state"]
    assert state["basis"] == CAN_ADAPTER_RECOVERING
    assert "status unavailable" in state["detail"]
    assert "missing" not in state["detail"]


def test_spare_and_controller_or_armed_conditions_are_not_missing(rig):
    assert sample(rig, ("spare",))["vehicle_state"]["state"] == "asleep"
    for reason in ("interface_armed", "wrong_bitrate", "controller_not_error_active"):
        hardware = hardware_status()
        for role in hardware["role_interfaces"]["roles"].values():
            role.update(reason=reason, passive_ready=False)
        hardware["role_interfaces"].update(ready=False, passive_ready=False)
        assert not interface_presence(hardware).missing_roles
        assert sample(rig, hardware=hardware)["vehicle_state"]["state"] == "asleep"


def test_netdev_disappeared_after_resolution_is_missing(rig):
    hardware = hardware_status()
    hardware["role_interfaces"]["roles"]["b-can"]["actual"]["present"] = False
    state = sample(rig, hardware=hardware)["vehicle_state"]
    assert state["basis"] == CAN_ADAPTER_MISSING
    assert state["detail"].startswith("B-CAN adapter missing")


def test_absence_and_return_work_without_role_provider():
    tracker = CanAvailability()
    tracker.observe({"adapter_present": False}, probe_ok=True, now=31, observed_at="lost")
    assert tracker.vehicle_state(now=31, elapsed_seconds=31)["basis"] == CAN_ADAPTER_MISSING
    tracker.observe({"adapter_present": True}, probe_ok=True, now=32, observed_at="back")
    assert not tracker.admit_observation(31.5)
    assert tracker.admit_observation(33)
    assert tracker.vehicle_state(now=33, elapsed_seconds=33) is None


def test_renumbering_is_not_recovery_until_roles_are_exact_and_evidence_is_new():
    tracker = CanAvailability()
    tracker.observe(hardware_status(CAN_BUS_ROLES), probe_ok=True, now=31, observed_at="lost")
    returned = hardware_status()
    for index, role in enumerate(returned["role_interfaces"]["roles"].values()):
        role["channel"] = f"can{index + 8}"
    tracker.observe(returned, probe_ok=True, now=32, observed_at="back")
    assert not tracker.admit_observation(31.9)
    assert tracker.admit_observation(32.1)


@pytest.mark.parametrize("value", [None, {}, [], {"basis": []}, {"basis": {}}])
def test_unavailable_predicate_handles_untrusted_shapes(value):
    assert vehicle_hardware_unavailable(value) is False


def test_production_wake_admission_rejects_missing_secondary(rig):
    broker, _, _ = rig
    sample(rig, ("can-ch",))
    # Only collector lifecycle is simulated; the public acquisition and actual
    # authorization callback run, with all passive hardware boundaries replaced.
    broker._collector_state = "running"
    wake = mock.Mock(side_effect=AssertionError("must not reach wake hardware"))
    runtime = RoleAwareVoltageAcquirer(
        SimpleNamespace(channel_for_bus=lambda _: "can0"),
        wake_authorization_begin=broker._begin_wake_authorization,
        wake_authorization_end=broker._end_wake_authorization,
        wake_prearm_check=broker.wake_prearm_conflicts, wake_once=wake,
    )
    silence = failure(metric="battery.voltage", unit="V", reason="bus_asleep", detail="silent")
    with mock.patch.object(runtime, "_passive", return_value=silence):
        result = runtime.acquire("wake_if_asleep")
    assert not result.available
    assert "not safe for a parked wake" in result.detail
    wake.assert_not_called()


def test_initial_and_prearm_gate_share_adapter_precedence(rig):
    broker, _, _ = rig
    broker._collector_state = "running"
    assert broker._begin_wake_authorization() == ()
    assert broker.wake_prearm_conflicts() == ()
    sample(rig, ("b-can",))
    assert "broker vehicle state is not safe for a parked wake" in broker.wake_prearm_conflicts()
    broker._end_wake_authorization()


def test_new_cases_cross_all_http_and_sse_shapes_unchanged():
    with fixed_time():
        expected = {row["case"]: json.loads(row["bytes"])["vehicle_state"]
                    for row in status_rows() if row["case"].startswith("adapter/")}
        delivered = [row for row in delivery_rows() if row[0] in expected]
    assert len(expected) == 8
    assert len(delivered) == 40
    for name, _kind, code, raw in delivered:
        assert code == 200
        payload = json.loads(raw)
        state = (payload.get("status") or payload)["vehicle_state"]
        assert state == expected[name]
