"""Tests for the recovery admission table, using only synthetic evidence."""

from dataclasses import replace

import pytest

from lib.netdev_instance import NetdevInstance
from projects.vehicle_data import usb_recovery_contract as contract
from projects.vehicle_data import usb_recovery_model as model
from projects.vehicle_data.usb_recovery_contract import Refusal as R

C, B = contract.BOARD_A_ROLES
ACTIVE, AUX = contract.HELPERS
BOOT = "test-boot"
NOW = 100.0


def _instance(dev_id, index, devnum):
    channel = f"can{dev_id}"
    return NetdevInstance(channel, index, f"/sys/usb/board/{channel}", "1-1.4", 1, devnum,
                          contract.BOARD_A_SERIAL, dev_id)


@pytest.fixture
def evidence():
    incident = model.Incident("a" * 32, BOOT, 1, "b" * 32, "opening", NOW, "opening")
    board = {}
    inhibits = {}
    for dev_id, helper in enumerate(contract.HELPERS):
        role = contract.HELPER_ROLES[helper]
        armed = _instance(dev_id, 10 + dev_id, 2)
        replacement = _instance(dev_id, 30 + dev_id, 3)
        name = contract.OWNED_INHIBITS[helper]
        inhibit = {"name": name, "channel": "*", "reason": "failed", "boot_id": BOOT,
                   "updated_at": "opening"}
        slot = model.HelperRecord(
            role, model.LaunchRecord(armed.channel, armed.usb_serial, dev_id, armed, 30.0),
            model.GrantRecord(True, "granted", 40.0), model.ArmedRecord(armed, 50.0),
            radar_at_claim=(1, False) if helper == ACTIVE else None,
            launch_seq=dev_id + 1, claimed_monotonic=20.0,
        )
        slot.note_latch(model.LatchRecord("opening", NOW, "restoration_failed", False), inhibit)
        incident.helpers[helper] = slot
        board[role] = model.RoleNow("resolved", replacement,
                                    model.LinkSeen(replacement.ifindex, True, False, False))
        inhibits[name] = inhibit
    observation = model.Observation(
        BOOT, False, model.BrokerGates(latched=frozenset(contract.HELPERS)), (1, False),
        model.ProcessCheck(True), tuple(inhibits.values()), inhibits, {30, 31}, board, True, ignition_flag=True,
    )
    return incident, observation


def _gates(observation, **fields):
    return replace(observation, gates=replace(observation.gates, **fields))


def _role(observation, role=C, **fields):
    return replace(observation, board_a={
        **observation.board_a, role: replace(observation.board_a[role], **fields),
    })


def _link(observation, **fields):
    return _role(observation, link=replace(observation.board_a[C].link, **fields))


@pytest.mark.parametrize(("modify", "result"), [
    (lambda o: replace(o, link_helper_ok=False), model.GiveUp(R.LINK_HELPER_UNVERIFIED)),
    (lambda o: replace(o, boot_id=""), model.GiveUp(R.BOOT_ID_UNAVAILABLE)),
    (lambda o: replace(o, boot_id="different"), model.GiveUp(R.BOOT_ID_UNAVAILABLE)),
    (lambda o: replace(o, reservation_exists=True), model.GiveUp(R.BOOT_BUDGET_SPENT)),
    (lambda o: replace(o, radar_marker=(2, False)), model.GiveUp(R.RADAR_RUN_ATTEMPTED)),
    (lambda o: replace(o, radar_marker=None), model.GiveUp(R.RADAR_RUN_UNCERTAIN)),
    (lambda o: _gates(o, latched=frozenset()), model.GiveUp(R.LATCH_STATE_MISMATCH)),
    (lambda o: replace(o, processes=model.ProcessCheck(False, unverifiable=True)),
     model.GiveUp(R.SERVICE_CGROUP_UNVERIFIABLE)),
    (lambda o: _gates(o, claimed=frozenset({AUX})), model.Wait(R.HELPER_CLAIMED)),
    (lambda o: _gates(o, auxiliary_thread_alive=True), model.Wait(R.HELPER_THREAD_ALIVE)),
    (lambda o: replace(o, processes=model.ProcessCheck(False)), model.Wait(R.PROCESSES_NOT_QUIET)),
    (lambda o: replace(o, processes=model.ProcessCheck(False, unstable=True)),
     model.Wait(R.PROCESSES_NOT_QUIET)),
    (lambda o: _gates(o, hold_active=True), model.Wait(R.HELPERS_HELD)),
    (lambda o: _gates(o, startup_faults_unrecorded=True), model.Wait(R.STARTUP_FAULT_UNRECORDED)),
    (lambda o: _gates(o, stopping=True), model.Wait(R.BROKER_STOPPING)),
    (lambda o: replace(o, ignition_flag=False), model.Wait(R.IGNITION_NOT_ON)),
    (lambda o: replace(o, on_disk={}), model.GiveUp(R.INHIBIT_ALREADY_ENDED)),
    (lambda o: replace(o, on_disk={name: dict(record, reason="new")
                                 for name, record in o.on_disk.items()}),
     model.GiveUp(R.INHIBIT_RECORD_CHANGED)),
    (lambda o: replace(o, inhibits=(*o.inhibits, {"name": "invalid"})), model.Wait(R.FOREIGN_INHIBIT)),
    (lambda o: replace(o, inhibits=(*o.inhibits, o.inhibits[0])), model.Wait(R.FOREIGN_INHIBIT)),
    (lambda o: replace(o, present_ifindexes={10, 30, 31}), model.Wait(R.OLD_INSTANCE_PRESENT)),
    (lambda o: replace(o, present_ifindexes=None), model.Wait(R.OLD_INSTANCE_PRESENT)),
    (lambda o: _role(o, resolution_state="ambiguous"), model.Wait(R.ROLE_UNRESOLVED)),
    (lambda o: _role(o, instance=None), model.Wait(R.ROLE_UNRESOLVED)),
    (lambda o: _role(_role(o, instance=_instance(0, 30, 2)), B, instance=_instance(1, 31, 2)),
     model.Wait(R.USB_INSTANCE_UNCHANGED)),
    (lambda o: _role(o, B, instance=_instance(1, 31, 4)), model.Wait(R.USB_INSTANCE_UNCHANGED)),
    (lambda o: _link(o, ifindex=99), model.Wait(R.TOPOLOGY_CHANGED)),
    (lambda o: _link(o, present=False), model.Wait(R.LINK_NOT_DOWN)),
    (lambda o: _link(o, up=True), model.Wait(R.LINK_NOT_DOWN)),
    (lambda o: _link(o, up=None), model.Wait(R.LINK_NOT_DOWN)),
    (lambda o: replace(o, topology_stable=False), model.Wait(R.TOPOLOGY_CHANGED)),
])
def test_assess_observation_refusals(evidence, modify, result):
    incident, observation = evidence
    assert model.assess(incident, modify(observation), NOW) == result


def test_all_rules_pass_and_owned_records_are_in_commit_order(evidence):
    incident, observation = evidence
    incident.helpers = dict(reversed(list(incident.helpers.items())))
    assert tuple(model.owned_inhibits(incident)) == tuple(contract.OWNED_INHIBITS.values())
    assert model.assess(incident, observation, NOW) == model.Proceed()


def test_first_refusal_short_circuits_and_wait_expires(evidence):
    incident, observation = evidence
    observation = _gates(observation, hold_active=True)
    expiry = NOW + contract.RECOVERY_WINDOW_SECONDS
    assert model.assess(incident, observation, expiry) == model.Wait(R.HELPERS_HELD)
    assert model.assess(incident, observation, expiry + 0.01) == model.GiveUp(R.WINDOW_EXPIRED)
    # Invalid later inputs would raise if evaluated.
    invalid = replace(observation, link_helper_ok=False, boot_id=None, gates=None, processes=None)
    assert model.assess(incident, invalid, expiry + 0.01) == model.GiveUp(R.LINK_HELPER_UNVERIFIED)


def test_bcan_only_is_terminal_even_without_a_ccan_claim(evidence):
    incident, observation = evidence
    del incident.helpers[ACTIVE]
    assert model.assess(incident, observation, NOW) == model.GiveUp(R.BCAN_ONLY_LATCH)


def test_ccan_only_requires_both_replacement_links(evidence):
    incident, observation = evidence
    del incident.helpers[AUX]
    owned = model.owned_inhibits(incident)
    observation = replace(_gates(observation, latched={ACTIVE}),
                          on_disk=owned, inhibits=tuple(owned.values()))
    assert model.assess(incident, observation, NOW) == model.Proceed()
    assert model.assess(incident, _role(observation, B, instance=None), NOW) == model.Wait(R.ROLE_UNRESOLVED)


@pytest.mark.parametrize("bad_evidence", ["grant", "order", "armed", "index", "dev_id", "ambiguous"])
def test_unproven_armed_instance_is_terminal(evidence, bad_evidence):
    incident, observation = evidence
    helper = incident.helpers[ACTIVE]
    if bad_evidence == "grant":
        helper.grant = replace(helper.grant, attempted=False)
    elif bad_evidence == "order":
        helper.grant = replace(helper.grant, granted_monotonic=51.0)
    elif bad_evidence == "armed":
        helper.armed = None
    elif bad_evidence == "index":
        helper.armed = replace(helper.armed, instance=_instance(0, 99, 2))
    elif bad_evidence == "dev_id":
        helper.armed = replace(helper.armed, instance=replace(helper.armed.instance, dev_id=1))
    else:
        helper.ambiguous = True
    assert model.assess(incident, observation, NOW) == model.GiveUp(R.ARMED_INSTANCE_UNKNOWN)


@pytest.mark.parametrize("change", [{"usb_devnum": 7}, {"ifindex": 10}, {"usb_serial": "other"}])
def test_armed_records_must_name_same_board_and_distinct_devices(evidence, change):
    incident, observation = evidence
    helper = incident.helpers[AUX]
    armed = replace(helper.armed.instance, **change)
    helper.launch = replace(helper.launch, instance=armed)
    helper.armed = replace(helper.armed, instance=armed)
    assert model.assess(incident, observation, NOW) == model.GiveUp(R.INSTANCE_RECORDS_DISAGREE)


def test_missing_and_fallback_owned_records_are_never_accepted(evidence):
    incident, observation = evidence
    helper = incident.helpers[ACTIVE]
    owned = helper.owned_inhibit
    assert owned is not None
    helper.owned_inhibit = None
    assert model.assess(incident, observation, NOW) == model.GiveUp(R.OWNED_INHIBIT_MISSING)
    fallback = {**owned, "name": "nolock.example", "stands_for": owned["name"]}
    helper.owned_inhibit = fallback
    observation = replace(observation, on_disk={**observation.on_disk, owned["name"]: fallback})
    assert model.assess(incident, observation, NOW) == model.GiveUp(R.INHIBIT_RECORD_CHANGED)


@pytest.mark.parametrize(("claim", "marker", "expected"), [
    ((1, False), (1, False), contract.RadarCondition.NONE),
    ((1, True), (1, False), contract.RadarCondition.ATTEMPTED),
    ((1, False), (1, True), contract.RadarCondition.ATTEMPTED),
    ((1, False), (2, False), contract.RadarCondition.ATTEMPTED),
    (None, (1, False), contract.RadarCondition.ATTEMPTED),
    (contract.RadarCondition.UNCERTAIN, (1, False), contract.RadarCondition.UNCERTAIN),
    ((1, False), None, contract.RadarCondition.UNCERTAIN),
])
def test_radar_claim_precedes_helper_publication(evidence, claim, marker, expected):
    incident, _ = evidence
    incident.helpers[ACTIVE].radar_at_claim = claim
    # A request made between the claim and helper publication can settle inactive; the counter
    # still changes, so the equal-active-flags/different-counter case must refuse.
    assert model.radar_condition(incident, marker) == expected


@pytest.mark.parametrize(("modify", "reason"), [
    (lambda o: o, None),
    (lambda o: replace(o, link_helper_ok=False), R.LINK_HELPER_UNVERIFIED),
    (lambda o: replace(o, radar_marker=(2, False)), R.RADAR_RUN_ATTEMPTED),
    (lambda o: replace(o, radar_marker=None), R.RADAR_RUN_UNCERTAIN),
    (lambda o: _gates(o, latched=set()), R.LATCH_STATE_MISMATCH),
    (lambda o: _gates(o, claimed={AUX}), R.HELPER_CLAIMED),
    (lambda o: _gates(o, auxiliary_thread_alive=True), R.HELPER_THREAD_ALIVE),
    (lambda o: _gates(o, hold_active=True), R.HELPERS_HELD),
    (lambda o: _gates(o, startup_faults_unrecorded=True), R.STARTUP_FAULT_UNRECORDED),
    (lambda o: _gates(o, stopping=True), R.BROKER_STOPPING),
    (lambda o: replace(o, processes=model.ProcessCheck(False, unverifiable=True)),
     R.SERVICE_CGROUP_UNVERIFIABLE),
    (lambda o: replace(o, processes=model.ProcessCheck(False, unstable=True)), R.PROCESSES_UNSTABLE),
    (lambda o: replace(o, processes=model.ProcessCheck(False, offenders=((91, "sudo", "sudo"),))),
     R.PROCESSES_NOT_QUIET),
    (lambda o: replace(o, on_disk={}), R.INHIBIT_ALREADY_ENDED),
    (lambda o: replace(o, on_disk={name: dict(record, updated_at="later")
                                 for name, record in o.on_disk.items()}), R.INHIBIT_RECORD_CHANGED),
    (lambda o: replace(o, inhibits=()), R.FOREIGN_INHIBIT),
    (lambda o: _role(o, B, instance=_instance(1, 31, 4)), R.INSTANCE_CHANGED),
    (lambda o: _link(o, ifindex=88), R.INSTANCE_CHANGED),
    (lambda o: _link(o, passive_ok=False), R.VERIFICATION_FAILED),
    (lambda o: _link(o, up=None), R.VERIFICATION_FAILED),
])
def test_commit_check(evidence, modify, reason):
    incident, observation = evidence
    configured = {role: value.instance for role, value in observation.board_a.items()}
    board = {role: replace(value, link=replace(value.link, up=True, passive_ok=True))
             for role, value in observation.board_a.items()}
    observation = replace(observation, reservation_exists=True, board_a=board)
    assert model.commit_check(incident, configured, modify(observation)) == reason
