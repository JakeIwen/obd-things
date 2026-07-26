"""Guarded acquisition sources for allowlisted vehicle telemetry metrics."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from lib import can_operation_state, canbus, diagnostic_safety
from projects.battery import bcan_voltage, ccan_voltage
from projects.vehicle_data.models import AcquisitionResult, failure, success


METRIC = "battery.voltage"
UNIT = "V"
CHANNEL = "can0"
BITRATE_BY_BUS = {"c-can": 500000, "b-can": 125000, "can-ch": 500000}
PAIR_BY_BUS = {"c-can": "6/14", "b-can": "3/11", "can-ch": "12/13"}


@dataclass(frozen=True)
class DecodedVoltage:
    value: float
    source: str
    quality: str
    detail: str


class VoltageBackend(Protocol):
    def interface_state(self, channel: str) -> canbus.InterfaceState: ...
    def identify_bus(self, channel: str, probe: float) -> str: ...
    def read_voltage(
        self, bus: str, channel: str, timeout: float
    ) -> DecodedVoltage | None: ...
    def load_topology(self, channel: str): ...
    def active_inhibits(self, channel: str): ...
    def record_topology(self, channel: str, bus: str) -> None: ...
    def wake(
        self, bus: str, channel: str, lock_handle, restore_state
    ) -> bool: ...


class LockProvider(Protocol):
    def observer(self, channel: str): ...
    def exclusive(self, channel: str): ...


class SystemLockProvider:
    def observer(self, channel: str):
        return diagnostic_safety.channel_observer_lock(channel)

    def exclusive(self, channel: str):
        return diagnostic_safety.channel_lock(channel)


class SystemVoltageBackend:
    def interface_state(self, channel: str) -> canbus.InterfaceState:
        return canbus.interface_state(channel)

    def identify_bus(self, channel: str, probe: float) -> str:
        return canbus.identify_bus(channel, probe=probe)

    def read_voltage(
        self, bus: str, channel: str, timeout: float
    ) -> DecodedVoltage | None:
        if bus == "b-can":
            value, detail = bcan_voltage.read_voltage(channel, timeout=timeout)
            if value is None:
                return None
            return DecodedVoltage(
                value=value,
                source="bcan.broadcast.0x46c",
                quality="verified",
                detail=detail,
            )
        if bus == "c-can":
            value, detail = ccan_voltage.read_voltage(channel, timeout=timeout)
            if value is None:
                return None
            return DecodedVoltage(
                value=value,
                source="ccan.broadcast.0x41a",
                quality="verified",
                detail=detail,
            )
        return None

    def load_topology(self, channel: str):
        return can_operation_state.load_topology(channel)

    def active_inhibits(self, channel: str):
        return can_operation_state.active_inhibits(channel)

    def record_topology(self, channel: str, bus: str) -> None:
        try:
            can_operation_state.set_topology(
                channel,
                bus,
                pair=PAIR_BY_BUS[bus],
                source="vehicle_telemetry_passive_signature",
                note="passively identified by allowlisted telemetry acquisition",
            )
        except (KeyError, OSError, RuntimeError, ValueError):
            # A failed cache write must not turn a passive observation into CAN activity.
            return

    def wake(self, bus: str, channel: str, lock_handle, restore_state) -> bool:
        if bus == "c-can":
            return canbus.poke_wake(
                channel,
                BITRATE_BY_BUS[bus],
                lock_handle=lock_handle,
                restore_state=restore_state,
            )
        if bus == "b-can":
            return canbus.tx_wake_burst(
                channel,
                BITRATE_BY_BUS[bus],
                lock_handle=lock_handle,
                restore_state=restore_state,
            )
        return False


class VoltageAcquirer:
    """One safety contract shared by the broker and the legacy voltage monitor."""

    def __init__(
        self,
        *,
        channel: str = CHANNEL,
        backend: VoltageBackend | None = None,
        locks: LockProvider | None = None,
        probe_seconds: float = 0.75,
        read_timeout: float = 2.0,
        monotonic=time.monotonic,
        wall_clock=lambda: datetime.now(timezone.utc),
    ):
        self.channel = channel
        self.backend = backend or SystemVoltageBackend()
        self.locks = locks or SystemLockProvider()
        self.probe_seconds = probe_seconds
        self.read_timeout = read_timeout
        self.monotonic = monotonic
        self.wall_clock = wall_clock

    def _fail(
        self,
        reason: str,
        detail: str,
        *,
        bus: str | None = None,
        acquisition: str | None = None,
    ) -> AcquisitionResult:
        return failure(
            metric=METRIC,
            unit=UNIT,
            reason=reason,
            detail=detail,
            bus=bus,
            acquisition=acquisition,
        )

    def _interface_gate(self):
        state = self.backend.interface_state(self.channel)
        if not state.present:
            return None, self._fail(
                "adapter_absent", f"{self.channel} is not present"
            )
        if not state.up or state.bitrate is None:
            return None, self._fail(
                "source_unavailable",
                f"{self.channel} is down or has no readable bitrate; left unchanged",
            )
        if not state.listen_only:
            return None, self._fail(
                "can_busy",
                f"{self.channel} is armed; refusing to touch another CAN operation",
            )
        if state.controller_state != "ERROR-ACTIVE":
            return None, self._fail(
                "source_unavailable",
                f"{self.channel} controller is "
                f"{state.controller_state or 'unavailable'}; left unchanged",
            )
        if state.bitrate not in (125000, 500000):
            return None, self._fail(
                "wrong_bus",
                f"{self.channel} is at unsupported bitrate {state.bitrate}",
            )
        return state, None

    def _read_identified(
        self,
        bus: str,
        state: canbus.InterfaceState,
        *,
        acquisition: str,
    ) -> AcquisitionResult:
        expected = BITRATE_BY_BUS.get(bus)
        if bus == "can-ch":
            self.backend.record_topology(self.channel, bus)
            return self._fail(
                "wrong_bus",
                "CAN-CH/grey is connected; battery acquisition never wakes this branch",
                bus=bus,
                acquisition=acquisition,
            )
        if bus not in ("c-can", "b-can"):
            reason = {
                "silent": "bus_asleep",
                "wrong-rate": "wrong_bus",
                "unknown": "unrecognized_bus",
            }.get(bus, "unrecognized_bus")
            return self._fail(
                reason,
                f"passive bus identification returned {bus}",
                bus=bus,
                acquisition=acquisition,
            )
        if state.bitrate != expected:
            return self._fail(
                "wrong_bus",
                f"{bus} requires {expected} bit/s but {self.channel} is "
                f"{state.bitrate}",
                bus=bus,
                acquisition=acquisition,
            )
        self.backend.record_topology(self.channel, bus)
        decoded = self.backend.read_voltage(
            bus, self.channel, timeout=self.read_timeout
        )
        if decoded is None:
            return self._fail(
                "source_unavailable",
                f"{bus} was identified but no approved voltage frame arrived",
                bus=bus,
                acquisition=acquisition,
            )
        return success(
            metric=METRIC,
            unit=UNIT,
            value=decoded.value,
            source=decoded.source,
            bus=bus,
            acquisition=acquisition,
            quality=decoded.quality,
            observed_monotonic=self.monotonic(),
            observed_at=self.wall_clock(),
            detail=decoded.detail,
        )

    def acquire_passive(self) -> AcquisitionResult:
        try:
            with self.locks.observer(self.channel):
                state, blocked = self._interface_gate()
                if blocked is not None:
                    return blocked
                bus = self.backend.identify_bus(
                    self.channel, probe=self.probe_seconds
                )
                return self._read_identified(
                    bus, state, acquisition="passive"
                )
        except diagnostic_safety.ChannelLockError:
            return self._fail(
                "can_busy",
                f"another participating CAN operation owns {self.channel}",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return self._fail("source_unavailable", f"passive CAN read failed: {exc}")

    def acquire(self, mode: str) -> AcquisitionResult:
        if mode == "passive":
            return self.acquire_passive()
        if mode != "wake_if_asleep":
            return self._fail(
                "unsupported_mode", f"unsupported acquisition mode {mode!r}"
            )

        passive = self.acquire_passive()
        if passive.available or passive.reason != "bus_asleep":
            return passive
        return self._acquire_wake_assisted()

    def _acquire_wake_assisted(self) -> AcquisitionResult:
        try:
            with self.locks.exclusive(self.channel) as lock_handle:
                state, blocked = self._interface_gate()
                if blocked is not None:
                    return blocked
                bus = self.backend.identify_bus(
                    self.channel, probe=self.probe_seconds
                )
                if bus in ("c-can", "b-can", "can-ch"):
                    return self._read_identified(
                        bus, state, acquisition="passive"
                    )
                if bus != "silent":
                    return self._read_identified(
                        bus, state, acquisition="wake_assisted"
                    )

                topology = self.backend.load_topology(self.channel)
                if topology.bus == "can-ch":
                    return self._fail(
                        "wrong_bus",
                        "same-boot topology says CAN-CH/grey; wake is forbidden",
                        bus="can-ch",
                        acquisition="wake_assisted",
                    )
                inhibits = self.backend.active_inhibits(self.channel)
                if inhibits:
                    names = ",".join(
                        str(item.get("name", "invalid")) for item in inhibits
                    )
                    return self._fail(
                        "can_busy",
                        f"active acquisition inhibited by {names}",
                        acquisition="wake_assisted",
                    )
                if (
                    not topology.usable
                    or topology.bus not in ("c-can", "b-can")
                ):
                    return self._fail(
                        "unrecognized_bus",
                        f"silent-bus wake denied: {topology.reason}",
                        acquisition="wake_assisted",
                    )
                expected_bus = topology.bus
                expected_bitrate = BITRATE_BY_BUS[expected_bus]
                if state.bitrate != expected_bitrate:
                    return self._fail(
                        "wrong_bus",
                        f"topology {expected_bus} requires {expected_bitrate} "
                        f"bit/s but {self.channel} is {state.bitrate}",
                        bus=expected_bus,
                        acquisition="wake_assisted",
                    )

                checked, blocked = self._interface_gate()
                if blocked is not None:
                    return blocked
                if not state.same_configuration(checked):
                    return self._fail(
                        "can_busy",
                        "SocketCAN configuration changed during wake preflight",
                        acquisition="wake_assisted",
                    )
                rechecked_bus = self.backend.identify_bus(
                    self.channel, probe=self.probe_seconds
                )
                if rechecked_bus in ("c-can", "b-can", "can-ch"):
                    return self._read_identified(
                        rechecked_bus, checked, acquisition="passive"
                    )
                if rechecked_bus != "silent":
                    return self._fail(
                        "unrecognized_bus",
                        f"wake recheck returned {rechecked_bus}; refusing TX",
                        acquisition="wake_assisted",
                    )

                result = None
                mutation_attempted = True
                try:
                    woke = self.backend.wake(
                        expected_bus, self.channel, lock_handle, state
                    )
                    if not woke:
                        result = self._fail(
                            "acquisition_timeout",
                            f"{expected_bus} wake produced no validated response",
                            bus=expected_bus,
                            acquisition="wake_assisted",
                        )
                    else:
                        verified = self.backend.identify_bus(
                            self.channel, probe=self.probe_seconds
                        )
                        if verified != expected_bus:
                            result = self._fail(
                                "wrong_bus",
                                f"post-wake topology mismatch: expected "
                                f"{expected_bus}, got {verified}",
                                bus=verified,
                                acquisition="wake_assisted",
                            )
                        else:
                            result = self._read_identified(
                                verified,
                                state,
                                acquisition="wake_assisted",
                            )
                except canbus.PassiveRestoreError as exc:
                    result = self._fail(
                        "restoration_failed",
                        str(exc),
                        bus=expected_bus,
                        acquisition="wake_assisted",
                    )
                finally:
                    if mutation_attempted:
                        restored = self.backend.interface_state(self.channel)
                        if not state.same_configuration(restored):
                            result = self._fail(
                                "restoration_failed",
                                "could not verify exact SocketCAN state "
                                "restoration after wake-assisted acquisition",
                                bus=expected_bus,
                                acquisition="wake_assisted",
                            )
                return result or self._fail(
                    "acquisition_timeout",
                    "wake-assisted acquisition ended without a result",
                    bus=expected_bus,
                    acquisition="wake_assisted",
                )
        except diagnostic_safety.ChannelLockError:
            return self._fail(
                "can_busy",
                f"another participating CAN operation owns {self.channel}",
                acquisition="wake_assisted",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return self._fail(
                "source_unavailable",
                f"wake-assisted CAN operation failed: {exc}",
                acquisition="wake_assisted",
            )

    def status_snapshot(self) -> dict[str, object]:
        state = self.backend.interface_state(self.channel)
        try:
            topology = self.backend.load_topology(self.channel)
            topology_payload = {
                "bus": topology.bus,
                "pair": topology.pair,
                "source": topology.source,
                "usable": topology.usable,
                "reason": topology.reason,
            }
        except (OSError, RuntimeError, ValueError):
            topology_payload = {
                "bus": "unknown",
                "usable": False,
                "reason": "topology state unavailable",
            }
        try:
            inhibits = [
                str(item.get("name", "invalid"))
                for item in self.backend.active_inhibits(self.channel)
            ]
        except (OSError, RuntimeError, ValueError):
            inhibits = ["inhibit-state-unavailable"]
        return {
            "channel": self.channel,
            "adapter_present": state.present,
            "up": state.up,
            "bitrate": state.bitrate,
            "listen_only": state.listen_only,
            "controller_state": state.controller_state,
            "restart_ms": state.restart_ms,
            "topology": topology_payload,
            "active_inhibits": inhibits,
        }
