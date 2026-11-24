"""Public session fixtures, also runnable against an untouched source snapshot.

python3 -m tests.test_active_drive_oil_life_step --source OLD.py --output OLD.json
python3 -m tests.test_active_drive_oil_life_step --output NEW.json
cmp OLD.json NEW.json
"""

import argparse
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

from projects.vehicle_data import active_drive, pcm_electrical
from tests.test_active_drive import FakeBackend, rpm_observation, snapshot


CASES = ("disabled", "cadence", "failure", "stale", "expired", "gate", "fatal", "poll_error")


def scripted_backend(actions, *, running_snapshots=125):
    """Script a running epoch followed by shutdown."""
    backend = FakeBackend(
        snapshots=[snapshot((750, 751, 752), rpm_observation()) for _ in range(running_snapshots)]
        + [snapshot((0, 0, 0), rpm_observation(0))], events=actions,
    )
    clock = [100.0]
    backend.monotonic = lambda: clock[0]

    def sleep(seconds):
        actions.append(["sleep", seconds])
        clock[0] += seconds

    backend.sleep = sleep
    read = backend.broadcast_snapshot
    backend.broadcast_snapshot = lambda timeout: replace(
        read(timeout), completed_monotonic=clock[0],
    )
    return backend, clock


def capture_session(module, case):
    """Capture events, collaborator order, permits, time and verified restoration."""
    actions = []
    backend, clock = scripted_backend(actions)
    if case == "failure":
        backend.pcm.oil_life_result = pcm_electrical.PcmElectricalResult(
            available=False, metric="engine.oil_life_remaining", unit="%",
            source="pcm.did.2185", bus="c-can", quality="observed_alfa_scale",
            reason="session_required", detail="NRC 7E; no session change",
        )
    issue = module.transmit_permit.issue
    gate = module._active_gate
    oil_poll = backend.pcm.poll_oil_life
    oil_attempts = [0]
    oil_times = []

    def issue_permit(*args, **kwargs):
        purpose = kwargs["purpose"]
        actions.append(["permit", purpose, clock[0]])
        if purpose == module.transmit_permit.PCM_OIL_LIFE:
            oil_attempts[0] += 1
            if case == "stale" and oil_attempts[0] == 1:
                raise module.transmit_permit.StaleTransmitEvidenceError("oil evidence stale")
            if case == "fatal":
                raise module.transmit_permit.TransmitPermitError("oil lock lost")
        return issue(*args, **kwargs)

    def active_gate(backend, initial):
        actions.append("active_gate")
        if case == "gate" and backend.pcm.temperature_poll_count:
            return module.SessionOutcome("adapter_unhealthy", "oil boundary gate failed")
        return gate(backend, initial)

    def poll_oil(permit):
        oil_times.append(clock[0])
        result = oil_poll(permit)
        if case == "poll_error":
            raise OSError("oil read failed unexpectedly")
        if case == "expired" and len(oil_times) == 1:
            return replace(result, available=False, reason="transmit_permit_expired")
        return result

    backend.pcm.poll_oil_life = poll_oil

    class RadarSteps:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, snapshot):
            actions.append(["radar_cycle", clock[0]])

    stream, stdout, stderr = io.StringIO(), io.StringIO(), io.StringIO()
    with (
        mock.patch.object(module.diagnostic_safety, "acquire_channel_lock",
                          side_effect=lambda channel: actions.append("lock") or object()),
        mock.patch.object(module.diagnostic_safety, "release_channel_lock",
                          side_effect=lambda handle: actions.append("unlock")),
        mock.patch.object(module.diagnostic_safety, "validate_channel_lock",
                          side_effect=lambda handle, channel: handle),
        mock.patch.object(module.can_operation_state, "begin_inhibit") as inhibit,
        mock.patch.object(module.transmit_permit, "issue", side_effect=issue_permit),
        mock.patch.object(module, "_active_gate", side_effect=active_gate),
        mock.patch.object(module.radar_drive_steps, "RadarDriveSteps", RadarSteps),
        mock.patch.object(module.os, "getpid", return_value=12345),
        redirect_stdout(stdout), redirect_stderr(stderr),
    ):
        outcome = module.run_active_session(
            backend, module.JsonEventSink(stream), enable_oil_life=case != "disabled",
            termination_guard=SimpleNamespace(begin_cleanup=lambda: actions.append("cleanup_guard")),
        )
    return dict(
        outcome=asdict(outcome), event_bytes=stream.getvalue(), actions=actions,
        oil_times=oil_times, oil_attempts=oil_attempts[0], final_clock=clock[0],
        restoration_count=backend.restore_count, inhibits=inhibit.call_count,
        stdout=stdout.getvalue(), stderr=stderr.getvalue(),
    )


@pytest.mark.parametrize("case", CASES)
def test_oil_life_through_public_session(case):
    captured = capture_session(active_drive, case)
    assert captured["outcome"]["restored"] is True
    assert captured["restoration_count"] == 1
    assert captured["inhibits"] == 0
    assert captured["stdout"] == captured["stderr"] == ""
    assert captured["actions"][-1] == "unlock"
    expected = {
        "disabled": [], "cadence": [100.0, 160.0, 220.0], "failure": [100.0],
        "stale": [101.0, 161.0, 221.0], "expired": [100.0, 101.0, 161.0, 221.0],
        "gate": [], "fatal": [], "poll_error": [100.0],
    }
    assert captured["oil_times"] == expected[case]
    expected_reason = ("adapter_unhealthy" if case == "gate" else
                       "helper_failed" if case in ("fatal", "poll_error") else "engine_not_running")
    assert captured["outcome"]["reason"] == expected_reason
    oil_events = [event for event in map(json.loads, captured["event_bytes"].splitlines())
                  if event.get("metric") == "engine.oil_life_remaining"]
    if case == "failure":
        assert [event["type"] for event in oil_events] == ["metric_failure"]
    elif case in ("disabled", "gate", "fatal", "poll_error"):
        assert not oil_events
    else:
        assert len(oil_events) == 3
        assert all(event["type"] == "observation" and event["value"] == 82 for event in oil_events)
    if case in ("stale", "expired"):
        assert ["radar_cycle", 100.0] in captured["actions"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    module = active_drive
    if args.source:
        name = "projects.vehicle_data._oil_life_baseline"
        spec = importlib.util.spec_from_file_location(name, args.source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    result = {case: capture_session(module, case) for case in CASES}
    assert all(case["outcome"]["restored"] is True for case in result.values())
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(f"Captured {len(result)} scripted sessions; all restored, no live I/O")


if __name__ == "__main__":
    main()
