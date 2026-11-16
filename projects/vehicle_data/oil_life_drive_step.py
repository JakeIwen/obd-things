"""The optional oil-life read in one guarded active-drive ownership interval."""

from __future__ import annotations

from dataclasses import dataclass

from projects.vehicle_data.active_session_gates import SessionOutcome


@dataclass(frozen=True)
class OilLifeStepResult:
    outcome: SessionOutcome | None = None
    skip_cycle: bool = False


class OilLifeDriveStep:
    """Keep the slow maintenance cadence and failed-epoch isolation together.

    The session retains ownership, gate functions and cycle waiting. Collaborators
    are supplied at the call boundary to preserve its existing patch seams.
    """

    def __init__(self, backend, sink, poller, lock_handle, initial, *, enabled, next_due):
        self.backend = backend
        self.sink = sink
        self.poller = poller
        self.lock_handle = lock_handle
        self.initial = initial
        self.enabled = enabled
        self.next_due = next_due

    def run(self, snapshot, *, active_gate, pcm_outcome, permit_api, interval_seconds):
        # Maintenance data waits for preceding PCM/TPMS replies and requires its
        # own one-use permit; a failed read is never retried in this epoch.
        if self.enabled and self.backend.monotonic() >= self.next_due:
            gate_failure = active_gate(self.backend, self.initial)
            if gate_failure is not None:
                return OilLifeStepResult(outcome=gate_failure)
            try:
                oil_permit = permit_api.issue(
                    self.lock_handle, snapshot,
                    purpose=permit_api.PCM_OIL_LIFE,
                    channel=self.backend.channel, monotonic=self.backend.monotonic,
                )
            except permit_api.StaleTransmitEvidenceError:
                return OilLifeStepResult(skip_cycle=True)
            oil_result = self.poller.poll_oil_life(oil_permit)
            if oil_result.reason == "transmit_permit_expired":
                return OilLifeStepResult(skip_cycle=True)
            self.next_due = self.backend.monotonic() + interval_seconds
            failed = pcm_outcome(oil_result)
            if failed is not None:
                self.enabled = False
                self.sink.emit(
                    "metric_failure", metric=oil_result.metric,
                    unit=oil_result.unit, source=oil_result.source,
                    bus=oil_result.bus, quality=oil_result.quality,
                    reason=failed.reason, detail=failed.detail,
                    interface_mode="armed_diagnostic",
                )
            else:
                self.sink.emit(
                    "observation", metric=oil_result.metric,
                    value=oil_result.value, unit=oil_result.unit,
                    source=oil_result.source, bus=oil_result.bus,
                    quality=oil_result.quality, detail=oil_result.detail,
                    interface_mode="armed_diagnostic",
                )
        return OilLifeStepResult()
