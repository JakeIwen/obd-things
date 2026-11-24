"""Optional low-cadence PCM reads inside a guarded active-drive interval."""

from __future__ import annotations

from projects.vehicle_data import barometric_pressure as baro, transmit_permit
from projects.vehicle_data.active_session_gates import SessionOutcome


class PcmDriveStep:
    """Give each optional read its own cadence and failed-epoch isolation.

    The session retains ownership, gate functions and cycle waiting. Collaborators
    are supplied at the call boundary to preserve its existing patch seams.
    """

    def __init__(self, backend, sink, lock_handle, initial, *, poll, purpose,
                 interval_seconds, next_due):
        self.backend = backend
        self.sink = sink
        self.poll = poll
        self.purpose = purpose
        self.interval_seconds = interval_seconds
        self.lock_handle = lock_handle
        self.initial = initial
        self.enabled = True
        self.next_due = next_due

    def run(self, snapshot, *, active_gate, pcm_outcome, permit_api) -> SessionOutcome | None:
        # Optional data waits for preceding PCM/TPMS replies and requires its
        # own one-use permit; a failed read is never retried in this epoch.
        if self.enabled and self.backend.monotonic() >= self.next_due:
            gate_failure = active_gate(self.backend, self.initial)
            if gate_failure is not None:
                return gate_failure
            try:
                permit = permit_api.issue(
                    self.lock_handle, snapshot,
                    purpose=self.purpose,
                    channel=self.backend.channel, monotonic=self.backend.monotonic,
                )
            except permit_api.StaleTransmitEvidenceError:
                # Keep this read due and let radar obtain its own fresh evidence.
                return None
            result = self.poll(permit)
            if result.reason == "transmit_permit_expired":
                return None
            self.next_due = self.backend.monotonic() + self.interval_seconds
            failed = pcm_outcome(result)
            if failed is not None:
                self.enabled = False
                self.sink.emit(
                    "metric_failure", metric=result.metric,
                    unit=result.unit, source=result.source,
                    bus=result.bus, quality=result.quality,
                    reason=failed.reason, detail=failed.detail,
                    interface_mode="armed_diagnostic",
                )
            else:
                self.sink.emit(
                    "observation", metric=result.metric,
                    value=result.value, unit=result.unit,
                    source=result.source, bus=result.bus,
                    quality=result.quality, detail=result.detail,
                    interface_mode="armed_diagnostic",
                )
        return None


def run_pcm_steps(steps, snapshot, *, active_gate, pcm_outcome, permit_api) -> SessionOutcome | None:
    for step in steps:
        outcome = step.run(
            snapshot, active_gate=active_gate, pcm_outcome=pcm_outcome, permit_api=permit_api,
        )
        if outcome is not None:
            return outcome
    return None


def optional_steps(backend, sink, poller, lock_handle, initial, *, next_due,
                   enable_oil_life, oil_life_interval, enable_barometric_pressure):
    steps = []
    if enable_oil_life:
        steps.append(PcmDriveStep(
            backend, sink, lock_handle, initial, next_due=next_due,
            poll=poller.poll_oil_life, purpose=transmit_permit.PCM_OIL_LIFE,
            interval_seconds=oil_life_interval,
        ))
    if enable_barometric_pressure:
        steps.append(PcmDriveStep(
            backend, sink, lock_handle, initial, next_due=next_due,
            poll=poller.poll_barometric_pressure, purpose=transmit_permit.PCM_BAROMETRIC_PRESSURE,
            interval_seconds=baro.INTERVAL_SECONDS,
        ))
    return steps
