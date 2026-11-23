"""File durability and bounds for recovery records on a temporary directory."""

from dataclasses import replace
import json
import os

import pytest

from lib.reports import atomic_json
from projects.vehicle_data import usb_recovery_contract as contract
from projects.vehicle_data import usb_recovery_model as model
from projects.vehicle_data import usb_recovery_record as record
from tests.test_usb_recovery_model import ACTIVE, BOOT, evidence  # pytest fixture


def _write(incident, path, **fields):
    record.write_incident(replace(incident, **fields), directory=path)


def _reserve(incident, path):
    record.reserve(incident.boot_id, incident.incident_id, directory=path,
                   broker_pid=incident.broker_pid, reserved_at=incident.opened_at)


def test_reservation_is_exclusive_even_if_empty(evidence, tmp_path):
    incident, _ = evidence
    assert not record.reservation_exists(BOOT, directory=tmp_path)
    _reserve(incident, tmp_path)
    path = tmp_path / f"reservation-{BOOT}.json"
    original = path.read_bytes()
    assert json.loads(original) == {
        "boot_id": BOOT, "incident_id": incident.incident_id, "broker_pid": incident.broker_pid,
        "reserved_at": incident.opened_at,
    }
    with pytest.raises(FileExistsError):
        _reserve(incident, tmp_path)
    assert path.read_bytes() == original
    path.write_bytes(b"")
    assert record.reservation_exists(BOOT, directory=tmp_path)
    with pytest.raises(FileExistsError):
        _reserve(incident, tmp_path)


def test_prune_keeps_current_boot_even_when_every_other_boot_has_future_mtime(evidence, tmp_path):
    incident, _ = evidence
    _reserve(incident, tmp_path)
    _write(incident, tmp_path)
    now = (tmp_path / f"reservation-{BOOT}.json").stat().st_mtime_ns
    for n in range(contract.KEEP_BOOTS):
        other = f"earlier-{n}"
        _reserve(replace(incident, boot_id=other), tmp_path)
        for path in (
            tmp_path / f"reservation-{other}.json",
            tmp_path / f"incident-{other}-{incident.incident_id}.json",
        ):
            if not path.exists():
                path.write_text("{}")
            os.utime(path, ns=(now + n + 1, now + n + 1))
    unrelated = tmp_path / "incident-not-ours.json"
    unrelated.write_text("retain this")
    _write(incident, tmp_path, detail="update")
    assert not (tmp_path / "reservation-earlier-0.json").exists()
    assert not (tmp_path / f"incident-earlier-0-{incident.incident_id}.json").exists()
    assert len(list(tmp_path.glob("reservation-*.json"))) == contract.KEEP_BOOTS
    assert unrelated.read_text() == "retain this"
    assert record.reservation_exists(BOOT, directory=tmp_path)
    with pytest.raises(FileExistsError):
        _reserve(replace(incident, incident_id="d" * 32), tmp_path)


def test_incident_limit_allows_updates_but_refuses_ninth_file(evidence, tmp_path):
    incident, _ = evidence
    for n in range(contract.MAX_INCIDENTS_PER_BOOT):
        _write(incident, tmp_path, incident_id=f"{n:032x}")
    with pytest.raises(OSError, match="incident limit"):
        _write(incident, tmp_path, incident_id="f" * 32)
    _write(incident, tmp_path, incident_id="0" * 32, detail="updated")
    path = tmp_path / f"incident-{BOOT}-{'0' * 32}.json"
    assert record.read_incident(path).detail == "updated"
    assert len(list(tmp_path.iterdir())) == contract.MAX_INCIDENTS_PER_BOOT


def test_restart_reuses_the_pending_path_instead_of_accumulating_files(evidence, tmp_path):
    incident, _ = evidence
    pending = tmp_path / f".pending-incident-{BOOT}.json"
    pending.write_bytes(b"partial bytes left by crash")
    _write(incident, tmp_path)
    assert not pending.exists()
    assert len(list(tmp_path.iterdir())) == 1


def test_latch_cap_still_preserves_newest_owned_record(evidence, tmp_path):
    incident, _ = evidence
    helper = incident.helpers[ACTIVE]
    first = helper.latches[0]
    owned = helper.owned_inhibit
    for index in range(contract.MAX_LATCHES_PER_HELPER):
        helper.note_latch(replace(first, detail="x" * 300), dict(owned, updated_at=str(index)))
    _write(incident, tmp_path)
    loaded = record.read_incident(next(tmp_path.glob("incident-*.json")))
    persisted = loaded.helpers[ACTIVE]
    assert len(persisted.latches) == contract.MAX_LATCHES_PER_HELPER
    assert persisted.latches_dropped == 1
    assert len(persisted.latches[-1].detail) == contract.MAX_LATCH_DETAIL_CHARS
    assert persisted.owned_inhibit["updated_at"] == str(contract.MAX_LATCHES_PER_HELPER - 1)


def test_byte_cap_checks_utf8_and_does_not_replace_previous_record(evidence, tmp_path):
    incident, _ = evidence
    _write(incident, tmp_path)
    path = next(tmp_path.glob("incident-*.json"))
    original = path.read_bytes()
    with pytest.raises(ValueError, match="byte limit"):
        _write(incident, tmp_path, detail="€" * (contract.MAX_RECORD_BYTES // 2))
    assert path.read_bytes() == original
    path.write_bytes(b" " * (contract.MAX_RECORD_BYTES + 1))
    with pytest.raises(ValueError, match="byte limit"):
        record.read_incident(path)


def test_incident_publishes_with_both_syncs_and_decodes_nested_evidence(evidence, tmp_path, monkeypatch):
    incident, observation = evidence
    role = contract.BOARD_A_ROLES[0]
    incident.state = contract.IncidentState.COMMITTED
    incident.transaction = model.TransactionRecord(
        "reserved", {role: current.instance for role, current in observation.board_a.items()},
        mutations=[model.MutationRecord(role, 30, "submitted", contract.MutationOutcome.UP_VERIFIED)],
        steps=[model.StepRecord("configure", "submitted", True)], durable=False,
    )
    calls = []

    def observe_atomic_json(*args, **kwargs):
        calls.append((kwargs["fsync"], kwargs["fsync_dir"]))
        return atomic_json(*args, **kwargs)

    monkeypatch.setattr(record, "atomic_json", observe_atomic_json)
    _write(incident, tmp_path / "nested" / "recovery")
    path = next(tmp_path.rglob("incident-*.json"))
    assert calls == [(True, True)]
    assert model.incident_to_dict(record.read_incident(path)) == model.incident_to_dict(incident)
    loaded = record.read_incident(path)
    assert loaded.state is contract.IncidentState.COMMITTED
    assert loaded.transaction.mutations[0].outcome is contract.MutationOutcome.UP_VERIFIED
    assert loaded.helpers[ACTIVE].radar_at_claim is None
    assert loaded.helpers[ACTIVE].launch_seq == 1


def test_unknown_record_schema_and_unsafe_path_are_rejected(evidence, tmp_path):
    incident, _ = evidence
    with pytest.raises(ValueError, match="boot ID"):
        record.reservation_exists("../elsewhere", directory=tmp_path)
    with pytest.raises(ValueError, match="schema"):
        model.incident_from_dict({**model.incident_to_dict(incident), "schema": True})
