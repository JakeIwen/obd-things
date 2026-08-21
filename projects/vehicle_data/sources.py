"""Guarded acquisition sources for allowlisted vehicle telemetry metrics."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from lib import canbus, diagnostic_safety
from projects.battery import bcan_voltage, ccan_voltage
from projects.vehicle_data.models import AcquisitionResult, failure, success


METRIC = "battery.voltage"
UNIT = "V"
BITRATE_BY_BUS = {"c-can": 500000, "b-can": 125000, "can-ch": 500000}


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


class LockProvider(Protocol):
    def observer(self, channel: str): ...


class SystemLockProvider:
    def observer(self, channel: str):
        return diagnostic_safety.channel_observer_lock(channel)


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


class VoltageAcquirer:
    """Read one runtime-resolved SocketCAN channel without changing CAN state."""

    def __init__(
        self,
        *,
        channel: str,
        backend: VoltageBackend | None = None,
        locks: LockProvider | None = None,
        probe_seconds: float = 0.75,
        read_timeout: float = 2.0,
        monotonic=time.monotonic,
        wall_clock=lambda: datetime.now(timezone.utc),
    ):
        if (
            not isinstance(channel, str)
            or not re.fullmatch(r"can[0-9]+", channel)
        ):
            raise ValueError(
                "voltage acquisition requires a runtime-resolved SocketCAN canN"
            )
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
        if state.fd_enabled is not False or state.restart_ms != 0:
            return None, self._fail(
                "source_unavailable",
                f"{self.channel} is not fixed classical CAN with restart-ms zero",
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
            return self._fail(
                "wrong_bus",
                "CAN-CH/grey is connected; no approved voltage source is mapped",
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
            return self._fail(
                "source_unavailable", f"passive CAN read failed: {exc}"
            )

    def acquire(self, mode: str) -> AcquisitionResult:
        if mode == "passive":
            return self.acquire_passive()
        return self._fail(
            "unsupported_mode", f"unsupported acquisition mode {mode!r}"
        )
