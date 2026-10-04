"""Frozen bc94da0 broker methods: independent differential-test reference.

Do not modernize these methods along with production code. They intentionally
retain the original validation order, error text, locks and side effects.
"""

from datetime import datetime, timezone

from lib import can_operation_state
from projects.vehicle_data.broker import (
    ACTIVE_DRIVE_FAILURE_REASONS, ACTIVE_DRIVE_RESTORATION_INHIBIT,
    AUXILIARY_DRIVE_RESTORATION_INHIBIT, AUXILIARY_ODOMETER_METRIC,
)
from projects.vehicle_data.models import AcquisitionResult


class ReferenceMethods:
    def handle_active_drive_event(self, event: dict[str, object]) -> None:
        """Validate one trusted helper-pipe event; never expose this as an API."""
        event_type = event.get("type")
        if event_type == "observation":
            if event.get("interface_mode") != "armed_diagnostic":
                raise ValueError("active observation must report armed_diagnostic mode")
            self._store_active_observation(event)
            return
        if event_type == "metric_failure":
            self._record_active_metric_failure(event)
            return
        if event_type == "quality_event":
            self._handle_data_quality_event(event)
            return
        if event_type not in ("status", "failure", "final"):
            raise ValueError("unsupported active-drive event type")
        reason = event.get("reason")
        detail = event.get("detail")
        if not isinstance(reason, str) or not isinstance(detail, str):
            raise ValueError("active-drive status reason/detail must be strings")
        interface_mode = event.get("interface_mode", "listen_only")
        if interface_mode not in (
            "listen_only",
            "armed_diagnostic",
            "unknown",
        ):
            raise ValueError("invalid active-drive interface mode")
        state = event.get("state")
        if state is None:
            state = (
                "restoration_failed"
                if reason == "restoration_failed"
                else ("idle" if event_type == "final" else event_type)
            )
        if not isinstance(state, str):
            raise ValueError("active-drive state must be a string")
        if event_type == "status":
            if (
                state != "armed_diagnostic"
                or reason != "running_gate_satisfied"
                or interface_mode != "armed_diagnostic"
            ):
                raise ValueError("active-drive armed status is inconsistent")
        elif event_type == "failure":
            if reason not in ACTIVE_DRIVE_FAILURE_REASONS:
                raise ValueError("active-drive failure reason is not allowlisted")
        else:
            restored = event.get("restored")
            if restored is not None and type(restored) is not bool:
                raise ValueError("active-drive final restored must be boolean or null")
            if reason not in ACTIVE_DRIVE_FAILURE_REASONS:
                raise ValueError("active-drive final reason is not allowlisted")
            if state not in ("idle", "restoration_failed"):
                raise ValueError("active-drive final state is invalid")
            if restored is True and interface_mode != "listen_only":
                raise ValueError("restored final must report listen_only mode")
            if restored is False and (
                reason != "restoration_failed"
                or state != "restoration_failed"
                or interface_mode != "armed_diagnostic"
            ):
                raise ValueError("failed restoration final is inconsistent")
            if reason == "restoration_failed" and restored is not False:
                raise ValueError("restoration failure must carry restored=false")
            if restored is None and interface_mode == "armed_diagnostic":
                raise ValueError("unverified final cannot claim armed ownership")
        with self._lock:
            self._active_drive.update(
                {
                    "state": state,
                    "reason": reason,
                    "detail": detail,
                    "interface_mode": interface_mode,
                    "last_event_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            if isinstance(event.get("pid"), int) and not isinstance(
                event.get("pid"), bool
            ):
                self._active_drive["helper_pid"] = event["pid"]
            if event_type == "final":
                self._active_drive["helper_pid"] = None
        if event_type in ("failure", "final") and reason in ACTIVE_DRIVE_FAILURE_REASONS:
            self._record_active_failure(
                reason,
                detail,
                interface_mode=interface_mode,
            )
        if reason == "restoration_failed" or event.get("restored") is False:
            with self._lock:
                self._active_drive_restoration_latched = True
                self._active_drive["restoration_failed"] = True
            try:
                can_operation_state.begin_inhibit(
                    ACTIVE_DRIVE_RESTORATION_INHIBIT,
                    channel="*",
                    reason=(
                        "broker could not verify active-drive listen-only "
                        f"restoration: {detail}"
                    ),
                )
            except Exception as exc:
                with self._lock:
                    self._active_drive["detail"] = (
                        f"{detail}; persistent restoration inhibit could not "
                        f"be recorded: {type(exc).__name__}: {exc}"
                    )

    def handle_auxiliary_drive_event(self, event: dict[str, object]) -> None:
        """Validate one event from the fixed B-CAN helper."""
        event_type = event.get("type")
        if event_type == "observation":
            if (
                event.get("metric") != AUXILIARY_ODOMETER_METRIC
                or event.get("source") != "ics.did.2001"
                or event.get("bus") != "b-can"
                or event.get("quality") != "candidate"
                or event.get("interface_mode") != "armed_diagnostic"
            ):
                raise ValueError("B-CAN auxiliary observation is outside its fixed profile")
            self._store_active_observation(event)
            return
        if event_type not in ("status", "failure", "final"):
            raise ValueError("unsupported B-CAN auxiliary event type")
        reason = event.get("reason")
        detail = event.get("detail")
        if not isinstance(reason, str) or not isinstance(detail, str):
            raise ValueError("B-CAN auxiliary reason/detail is invalid")
        interface_mode = event.get("interface_mode", "listen_only")
        if interface_mode not in ("listen_only", "armed_diagnostic", "unknown"):
            raise ValueError("invalid B-CAN auxiliary interface mode")
        state = event.get("state")
        if not isinstance(state, str):
            raise ValueError("B-CAN auxiliary state must be a string")
        if event_type == "status" and (
            state != "armed_diagnostic"
            or reason != "running_gate_satisfied"
            or interface_mode != "armed_diagnostic"
        ):
            raise ValueError("B-CAN auxiliary armed status is inconsistent")
        if event_type in ("failure", "final") and reason not in ACTIVE_DRIVE_FAILURE_REASONS:
            raise ValueError("B-CAN auxiliary failure reason is not allowlisted")
        if event_type == "final":
            restored = event.get("restored")
            if restored is not None and type(restored) is not bool:
                raise ValueError("B-CAN auxiliary restored must be boolean or null")
            if state not in ("idle", "restoration_failed"):
                raise ValueError("B-CAN auxiliary final state is invalid")
            if restored is True and interface_mode != "listen_only":
                raise ValueError("restored B-CAN final must report listen_only")
            if restored is False and (
                state != "restoration_failed"
                or reason != "restoration_failed"
                or interface_mode != "armed_diagnostic"
            ):
                raise ValueError("failed B-CAN restoration final is inconsistent")
        owner_route = None
        if event_type == "status":
            supervisor = self.auxiliary_drive_supervisor
            delegate = vars(supervisor).get("_delegate") if supervisor is not None else None
            attrs = vars(delegate or supervisor) if (delegate or supervisor) is not None else {}
            channel, serial, dev_id = (attrs.get("channel"), attrs.get("expected_usb_serial"), attrs.get("expected_dev_id"))
            if isinstance(channel,str) and isinstance(serial,str) and type(dev_id) is int:
                owner_route = {"channel":channel, "usb_serial":serial, "dev_id":dev_id}
        with self._lock:
            self._auxiliary_drive.update(
                {
                    "state": state,
                    "reason": reason,
                    "detail": detail,
                    "interface_mode": interface_mode,
                    "last_event_at": datetime.now(timezone.utc).isoformat(),
                    "owner_route": owner_route,
                }
            )
            pid = event.get("pid")
            if isinstance(pid, int) and not isinstance(pid, bool):
                self._auxiliary_drive["helper_pid"] = pid
            if event_type == "final":
                self._auxiliary_drive["helper_pid"] = None
        if event_type in ("failure", "final"):
            self._record_auxiliary_failure(reason, detail, interface_mode=interface_mode)
        if reason == "restoration_failed" or event.get("restored") is False:
            with self._lock:
                self._auxiliary_drive_restoration_latched = True
                self._auxiliary_drive["restoration_failed"] = True
            try:
                can_operation_state.begin_inhibit(
                    AUXILIARY_DRIVE_RESTORATION_INHIBIT,
                    channel="*",
                    reason=f"broker could not verify B-CAN restoration: {detail}",
                )
            except Exception as exc:
                with self._lock:
                    self._auxiliary_drive["detail"] = (
                        f"{detail}; persistent B-CAN restoration inhibit could "
                        f"not be recorded: {type(exc).__name__}: {exc}"
                    )

    def _update_vehicle_state(self, result: AcquisitionResult) -> None:
        """Record only state conclusions supported by passive acquisition.

        Awake traffic does not distinguish an idling engine from ignition-on,
        a fob wake, or a charger-powered module wake. In particular, battery
        voltage is never used as an engine-running heuristic.
        """
        if result.metric not in (
            "battery.voltage",
            "engine.rpm",
            "vehicle.ignition_on",
        ):
            return
        if (
            result.metric == "engine.rpm"
            and result.available
            and result.source == "ccan.broadcast.0x0fc"
            and isinstance(result.value, (int, float))
            and not isinstance(result.value, bool)
        ):
            running = float(result.value) >= 400.0
            state = {
                "state": "running" if running else "ignition_on",
                "running": running,
                "confidence": "verified",
                "basis": "qualified_ccan_0x0fc_engine_speed",
                "detail": (
                    f"qualified passive 0x0FC engine speed is "
                    f"{float(result.value):.0f} rpm"
                ),
                "observed_at": (
                    result.observed_at.isoformat()
                    if result.observed_at is not None
                    else datetime.now(timezone.utc).isoformat()
                ),
            }
            with self._lock:
                self._vehicle_state = state
                self._vehicle_state_observed_monotonic = (
                    result.observed_monotonic
                    if result.observed_monotonic is not None
                    else self.monotonic()
                )
            return
        if (
            result.metric == "battery.voltage"
            and result.acquisition == "physical_read_data_by_identifier"
        ):
            # A solicited cluster response proves neither passive bus activity
            # nor current ignition state. The separate verified 0x2EF
            # observation is the authority during a cluster logger run.
            return
        if result.metric == "battery.voltage":
            with self._lock:
                current_basis = self._vehicle_state.get("basis")
                current_observed = self._vehicle_state_observed_monotonic
            authoritative_metric = {
                "qualified_ccan_0x0fc_engine_speed": "engine.rpm",
                "ccan_0x2ef_ignition_gate": "vehicle.ignition_on",
            }.get(str(current_basis))
            authoritative_definition = self.definitions.get(
                authoritative_metric or ""
            )
            if (
                authoritative_metric is not None
                and current_observed is not None
                and authoritative_definition is not None
                and self.monotonic() - current_observed
                <= authoritative_definition.stale_after_seconds
            ):
                # Generic voltage/bus activity cannot downgrade a fresher
                # verified RPM or ignition conclusion. Once that stronger
                # evidence expires, this observation may establish Awake.
                return
        if (
            result.available
            and result.metric == "vehicle.ignition_on"
            and result.value is True
        ):
            with self._lock:
                current_basis = self._vehicle_state.get("basis")
                current_observed = self._vehicle_state_observed_monotonic
            rpm_definition = self.definitions.get("engine.rpm")
            if (
                current_basis == "qualified_ccan_0x0fc_engine_speed"
                and current_observed is not None
                and rpm_definition is not None
                and self.monotonic() - current_observed
                <= rpm_definition.stale_after_seconds
            ):
                # 0x2EF proves ignition presence, but fresh qualified RPM
                # evidence is stronger and has already distinguished running
                # from ignition-on/engine-off.
                return
        state = None
        if result.available and result.metric == "vehicle.ignition_on":
            ignition_on = result.value is True
            state = {
                "state": "ignition_on" if ignition_on else "parked",
                "running": None if ignition_on else False,
                "confidence": "verified",
                "basis": "ccan_0x2ef_ignition_gate",
                "detail": (
                    "verified C-CAN ignition-on gate is present"
                    if ignition_on
                    else "verified C-CAN ignition-on gate is absent"
                ),
            }
        elif result.available:
            state = {
                "state": "awake",
                "running": None,
                "confidence": "observed",
                "basis": "passive_bus_activity",
                "detail": (
                    f"{result.bus or 'vehicle bus'} traffic is present; "
                    "running versus ignition-on versus a temporary wake is "
                    "not yet distinguished"
                ),
            }
        elif result.reason == "bus_asleep" and "c-can" in self._receive_silent_roles_snapshot():
            # The C-CAN leg is silent but CAN-CH has been busy for minutes: the
            # van is awake and the C-CAN adapter is deaf (2026-09-22 incident).
            state = {
                "state": "awake",
                "running": None,
                "confidence": "inferred",
                "basis": "passive_can_ch_activity_c_can_silent",
                "detail": (
                    "CAN-CH traffic is present but the C-CAN adapter has "
                    "received nothing for minutes; replug its USB cable or "
                    "reboot the Pi"
                ),
            }
        elif result.reason == "bus_asleep":
            state = {
                "state": "asleep",
                "running": False,
                "confidence": "inferred",
                "basis": "passive_bus_silence",
                "detail": (
                    "no frames arrived at the approved bitrate; this is "
                    "consistent with a sleeping vehicle, but an unplugged "
                    "physical leg is not distinguishable from silence"
                ),
            }
        elif result.bus == "can-ch":
            state = {
                "state": "awake",
                "running": None,
                "confidence": "observed",
                "basis": "passive_can_ch_activity",
                "detail": (
                    "CAN-CH traffic is present; no verified running-state "
                    "metric is available on this branch"
                ),
            }
        elif result.bus == "wrong-rate":
            state = {
                "state": "awake",
                "running": None,
                "confidence": "inferred",
                "basis": "wrong_rate_rx_activity",
                "detail": (
                    "RX errors show traffic at another bitrate; vehicle "
                    "running state cannot be determined"
                ),
            }
        if state is None:
            return
        state["observed_at"] = (
            result.observed_at.isoformat()
            if result.observed_at is not None
            else datetime.now(timezone.utc).isoformat()
        )
        with self._lock:
            self._vehicle_state = state
            self._vehicle_state_observed_monotonic = (
                result.observed_monotonic
                if result.observed_monotonic is not None
                else self.monotonic()
            )
