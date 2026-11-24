"""Optional low-cadence PCM reads inside a guarded active-drive interval."""

from __future__ import annotations

from projects.vehicle_data import barometric_pressure as baro, transmit_permit
from projects.vehicle_data.active_session_gates import SessionOutcome


class PcmDriveStep:
    """Give each optional read its own cadence and failed-epoch isolation.

    The session retains ownership and cycle timing.
    """

    def __init__(self, backend, sink, lock_handle, initial, *, poll, purpose,
                 interval_seconds, next_due, active_gate, pcm_outcome, defer=None):
        self.backend = backend
        self.sink = sink
        self.poll = poll
        self.purpose = purpose
        self.interval_seconds = interval_seconds
        self.active_gate = active_gate
        self.pcm_outcome = pcm_outcome
        self.defer = defer
        self.lock_handle = lock_handle
        self.initial = initial
        self.enabled = True
        self.next_due = next_due

    def run(self, snapshot) -> SessionOutcome | None:
        if not self.enabled or self.backend.monotonic() < self.next_due:
            return None
        if self.defer is not None and self.defer():
            return None
        gate_failure = self.active_gate(self.backend, self.initial)
        if gate_failure is not None:
            return gate_failure
        try:
            permit = transmit_permit.issue(
                self.lock_handle, snapshot, purpose=self.purpose,
                channel=self.backend.channel, monotonic=self.backend.monotonic,
            )
        except transmit_permit.StaleTransmitEvidenceError:
            return None
        # Command intake may have changed during the gate and permit checks.
        if self.defer is not None and self.defer():
            return None
        result = self.poll(permit)
        if result.reason == "transmit_permit_expired":
            return None
        self.next_due = self.backend.monotonic() + self.interval_seconds
        failed = self.pcm_outcome(result)
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


def optional_steps(backend, sink, poller, lock_handle, initial, *, next_due,
                   enable_oil_life, oil_life_interval, enable_barometric_pressure,
                   active_gate, pcm_outcome, defer_barometric_pressure):
    options = dict(next_due=next_due, active_gate=active_gate, pcm_outcome=pcm_outcome)
    oil_life = pressure = None
    if enable_oil_life:
        oil_life = PcmDriveStep(
            backend, sink, lock_handle, initial,
            poll=poller.poll_oil_life, purpose=transmit_permit.PCM_OIL_LIFE,
            interval_seconds=oil_life_interval, **options,
        ).run
    if enable_barometric_pressure:
        pressure = PcmDriveStep(
            backend, sink, lock_handle, initial,
            poll=poller.poll_barometric_pressure, purpose=transmit_permit.PCM_BAROMETRIC_PRESSURE,
            interval_seconds=baro.INTERVAL_SECONDS, defer=defer_barometric_pressure, **options,
        ).run
    return oil_life, pressure
