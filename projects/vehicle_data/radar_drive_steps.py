"""The radar's part of one active-drive cycle: the 0845 read and service alignment.

``active_drive.run_active_session`` owns the lock, the adapter and the cycle
timing; it calls :meth:`RadarDriveSteps.run` once per cycle and ends the session
when that returns an outcome. Every radar transmission first takes fresh
running evidence and repeats the active gates, because the PCM and RF Hub
replies earlier in the cycle have aged the cycle's own snapshot. When that
evidence or a gate says the session must end, the step in progress is abandoned
by :class:`SessionEnding` and ``run`` hands the outcome back.
"""

from __future__ import annotations

from projects.vehicle_data import active_session_gates as gates
from projects.vehicle_data import radar_alignment, transmit_permit
from projects.vehicle_data.radar_service_alignment import ServiceAlignment


class SessionEnding(Exception):
    """The helper session must end now; ``outcome`` says why."""

    def __init__(self, outcome):
        super().__init__(outcome.detail)
        self.outcome = outcome


class FreshAuthority:
    """Fresh running evidence, the active gates and one-use permits for a send."""

    def __init__(self, backend, lock_handle, initial, *, snapshot_seconds):
        self.backend = backend
        self.lock_handle = lock_handle
        self.initial = initial
        self.snapshot_seconds = snapshot_seconds

    def acquire(self, purposes, *, absent_detail):
        """One permit per purpose, or None when the evidence aged out and
        nothing may be sent this cycle. Raises :class:`SessionEnding` when the
        engine is no longer running or an active gate fails."""
        snapshot = self.backend.broadcast_snapshot(self.snapshot_seconds)
        if not gates.running_snapshot(snapshot):
            raise SessionEnding(gates.SessionOutcome("engine_not_running", absent_detail))
        gate_failure = gates.active_gate(self.backend, self.initial)
        if gate_failure is not None:
            raise SessionEnding(gate_failure)
        try:
            return tuple(
                transmit_permit.issue(
                    self.lock_handle, snapshot, purpose=purpose,
                    channel=self.backend.channel, monotonic=self.backend.monotonic,
                )
                for purpose in purposes
            )
        except transmit_permit.StaleTransmitEvidenceError:
            return None


class RadarDriveSteps:
    """Poll 0845 on its cadence, then advance an owner-started service alignment.

    ``service_commands`` is the broker's command reader; without it (an older
    broker, or the feature handshake absent) no service request can be built.
    """

    def __init__(self, backend, sink, poller, authority, *, next_due, service_commands=None):
        self.backend = backend
        self.sink = sink
        self.poller = poller
        self.authority = authority
        self.alignment_enabled = poller is not None
        self.next_alignment = next_due
        self.service = None if service_commands is None else ServiceAlignment(
            poller, sink, authority, service_commands, monotonic=backend.monotonic,
        )

    def _poll_alignment(self) -> None:
        if not self.alignment_enabled or self.backend.monotonic() < self.next_alignment:
            return
        # Radar replies are segmented, so the request and its FlowControl are
        # two independently permitted sends issued from the same new evidence.
        permits = self.authority.acquire(
            (transmit_permit.RADAR_ALIGNMENT, transmit_permit.RADAR_ALIGNMENT_FLOW),
            absent_detail="radar read skipped: fresh running evidence absent",
        )
        if permits is None:
            return
        radar = self.poller.poll(*permits)
        self.next_alignment = self.backend.monotonic() + radar_alignment.POLL_INTERVAL_SECONDS
        if radar.reason != "transmit_permit_expired":
            for index, metric in enumerate(radar_alignment.METRICS):
                common = dict(metric=metric, unit="deg", source=radar_alignment.SOURCE,
                              bus="c-can", quality="candidate", detail=radar.detail,
                              interface_mode="armed_diagnostic")
                if radar.available:
                    self.sink.emit("observation", value=radar.angles[index], **common)
                else:
                    self.sink.emit("metric_failure", reason=radar.reason, **common)
            if not radar.available:
                self.alignment_enabled = False

    def run(self, snapshot):
        """Outcome that ends the helper session, or None to continue."""
        try:
            self._poll_alignment()
            if self.service is not None:
                self.service.run_cycle(snapshot)
        except SessionEnding as ending:
            if self.service is not None:
                self.service.session_ending(ending.outcome.detail)
            return ending.outcome
        return None
