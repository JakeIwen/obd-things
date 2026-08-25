"""Fixed, role-resolved CAN network-wake profiles for this ProMaster.

The public surface accepts only a logical vehicle role.  Physical pairs,
bitrates, restart policy, identifiers, payloads, burst count, and timing are
fixed evidence-backed profile data and cannot be supplied by a consumer.

Every session is scoped to exclusive logical-role and freshly resolved-channel
ownership.  It starts from an exact same-boot passive baseline and restores
that baseline before releasing either lock.  ``can-ch`` deliberately has no
wake profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import socket
import struct
import threading
import time
from typing import Callable, Iterable

from lib import can_operation_state, can_runtime_route, canbus, diagnostic_safety


CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_SFF_MASK = 0x7FF
CAN_EFF_MASK = 0x1FFFFFFF
AF_CAN = getattr(socket, "AF_CAN", 29)
CAN_RAW = getattr(socket, "CAN_RAW", 1)
SOL_CAN_RAW = getattr(socket, "SOL_CAN_RAW", 101)
CAN_RAW_FILTER = getattr(socket, "CAN_RAW_FILTER", 1)

_B_CAN_WAKE_ID = 0x7FF
_B_CAN_WAKE_ATTEMPTS = 75
_B_CAN_WAKE_GAP_SECONDS = 0.02
_B_CAN_VOLTAGE_ID = 0x46C
_B_CAN_VOLTAGE_MASK = 0x1FFF
_B_CAN_VOLTAGE_DIVISOR = 400.0
_B_CAN_SANE_VOLTAGE = (6.0, 18.0)

_C_CAN_RFH_TXID = 0x18DAC7F1
_C_CAN_RFH_RXID = 0x18DAF1C7
_C_CAN_WAKE_DID = 0xFEFF
_C_CAN_WAKE_REQUEST_DATA = bytes.fromhex("03 22 FE FF 00 00 00 00")
_C_CAN_WAKE_RESPONSE_PREFIX = bytes.fromhex("62 FE FF")
_C_CAN_WAKE_RESPONSE_LENGTH = 7
_C_CAN_WAKE_ATTEMPTS = 10
_C_CAN_WAKE_RETRY_SECONDS = 0.02
_C_CAN_ENGINE_SPEED_ID = 0x0FC
_C_CAN_IGNITION_GATE_ID = 0x2EF
_C_CAN_RUNNING_RPM = 400.0


@dataclass(frozen=True)
class _WakeProfile:
    role: str
    pair: str
    bitrate: int
    source: str
    wake_probe_policy: str
    one_shot: bool


_PROFILES = {
    "b-can": _WakeProfile(
        role="b-can",
        pair="3/11",
        bitrate=125000,
        source="bcan.network_wake.0x7ff",
        wake_probe_policy="silent",
        one_shot=False,
    ),
    "c-can": _WakeProfile(
        role="c-can",
        pair="6/14",
        bitrate=500000,
        source="rf_hub.uds.22feff",
        wake_probe_policy="role_or_silent",
        one_shot=True,
    ),
}


@dataclass(frozen=True)
class WakeResult:
    """Validated result of one fixed wake trigger.

    ``voltage`` is populated only by the B-CAN voltage wake profile.  A result
    returned by :func:`wake_once` has already passed exact passive restoration.
    """

    role: str
    source: str
    detail: str
    voltage: float | None = None


class CanWakeError(RuntimeError):
    """A fixed wake profile failed closed before producing a valid result."""

    def __init__(self, reason: str, detail: str, *, role: str | None = None):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.role = role


def _profile(role: str) -> _WakeProfile:
    if not isinstance(role, str) or role not in _PROFILES:
        raise CanWakeError(
            "unsupported_role",
            "CAN wake requires the exact logical role 'b-can' or 'c-can'; "
            "CAN-CH wake is intentionally unmapped",
            role=role if isinstance(role, str) else None,
        )
    return _PROFILES[role]


def _standard_filter(can_id: int) -> bytes:
    # Requiring EFF and RTR flag agreement excludes extended/RTR frames which
    # happen to share the same low eleven identifier bits.
    return struct.pack(
        "=II",
        can_id,
        CAN_SFF_MASK | CAN_EFF_FLAG | CAN_RTR_FLAG,
    )


def _recv_standard_frames(
    channel: str,
    identifiers: tuple[int, ...],
    duration: float,
) -> list[tuple[int, bytes]]:
    sock = socket.socket(AF_CAN, socket.SOCK_RAW, CAN_RAW)
    frames: list[tuple[int, bytes]] = []
    try:
        filters = b"".join(_standard_filter(can_id) for can_id in identifiers)
        sock.setsockopt(SOL_CAN_RAW, CAN_RAW_FILTER, filters)
        sock.bind((channel,))
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            try:
                raw = sock.recv(16)
            except socket.timeout:
                break
            if len(raw) != 16:
                continue
            can_id, dlc, data = struct.unpack("=IB3x8s", raw)
            if can_id & (CAN_EFF_FLAG | CAN_RTR_FLAG):
                continue
            can_id &= CAN_SFF_MASK
            if can_id in identifiers:
                frames.append((can_id, data[: min(dlc, 8)]))
    finally:
        sock.close()
    return frames


def _c_can_safety_conflicts(route) -> tuple[str, ...]:
    """Reject ignition/running witnesses without treating activity as routing."""

    try:
        frames = _recv_standard_frames(
            route.channel,
            (_C_CAN_ENGINE_SPEED_ID, _C_CAN_IGNITION_GATE_ID),
            0.25,
        )
    except OSError as exc:
        return (f"could not verify C-CAN parked state: {exc}",)
    ignition_seen = any(
        can_id == _C_CAN_IGNITION_GATE_ID for can_id, _data in frames
    )
    if ignition_seen:
        return ("verified C-CAN ignition-on gate 0x2EF is present",)
    for can_id, data in frames:
        if can_id != _C_CAN_ENGINE_SPEED_ID or len(data) < 2:
            continue
        rpm = (int.from_bytes(data[:2], "big") & 0xFFFC) / 4.0
        if rpm >= _C_CAN_RUNNING_RPM:
            return (f"verified C-CAN engine speed is {rpm:.0f} rpm",)
    return ()


def _classify_route_error(exc: BaseException, role: str) -> CanWakeError:
    detail = str(exc) or type(exc).__name__
    lowered = detail.lower()
    if isinstance(exc, diagnostic_safety.ChannelLockError):
        reason = "can_busy"
    elif "became awake before wake arming" in lowered:
        reason = "bus_not_silent"
    elif (
        "topology" in lowered
        or "wake probe returned" in lowered
        or "physical pair" in lowered
    ):
        reason = "wrong_bus"
    elif (
        "inhibit" in lowered
        or "another active" in lowered
        or "observer" in lowered
        or "engine speed" in lowered
        or "ignition-on" in lowered
    ):
        reason = "can_busy"
    else:
        # A prearm callback describes its own current-owner/state conflict.
        # Once exact role ownership was requested, such a refusal is busy
        # rather than permission to hunt for another channel.
        reason = "can_busy"
    return CanWakeError(reason, detail, role=role)


class _WakeSession:
    """One armed fixed-profile session holding both identity locks."""

    def __init__(
        self,
        profile: _WakeProfile,
        ownership: can_runtime_route.ActiveBusOwnership,
        prearm_check: Callable[[], Iterable[object]],
    ) -> None:
        self._profile = profile
        self._ownership = ownership
        self._prearm_check = prearm_check
        self._active_one_shot = profile.one_shot
        self._closed = False

    def _active_conflicts(self) -> tuple[str, ...]:
        route = self._ownership.route
        can_runtime_route.revalidate_bus_route(
            route, manager=self._ownership.manager
        )
        state = canbus.interface_state(route.channel)
        if not (
            state.present
            and state.up
            and state.bitrate == self._profile.bitrate
            and state.fd_enabled is False
            and state.one_shot is self._active_one_shot
            and not state.listen_only
            and state.controller_state == "ERROR-ACTIVE"
            and state.restart_ms == 0
        ):
            return (
                f"{route.role}/{route.channel} is no longer exact armed "
                f"classical CAN with restart-ms 0 and one-shot "
                f"{'on' if self._active_one_shot else 'off'}",
            )
        topology = can_operation_state.load_topology(route.channel)
        if (
            not topology.usable
            or topology.bus != route.role
            or topology.pair != route.pair
        ):
            return (
                f"same-boot topology no longer proves {route.role} on pair {route.pair}",
            )
        inhibits = can_operation_state.active_inhibits(route.channel)
        if inhibits:
            names = ",".join(
                str(item.get("name", "invalid")) for item in inhibits
            )
            return (f"active CAN operation is inhibited by {names}",)
        conflicts = tuple(str(item) for item in self._prearm_check())
        if conflicts:
            return conflicts
        if route.role == "c-can":
            conflicts = _c_can_safety_conflicts(route)
            if conflicts:
                return conflicts
        return ()

    def _ensure_active(self) -> None:
        if self._closed:
            raise CanWakeError(
                "source_unavailable",
                "CAN wake session is already closed",
                role=self._profile.role,
            )
        try:
            conflicts = self._active_conflicts()
        except Exception as exc:
            raise _classify_route_error(exc, self._profile.role) from exc
        if conflicts:
            raise CanWakeError(
                "can_busy",
                "; ".join(conflicts),
                role=self._profile.role,
            )

    def _rearm_one_shot(self, enabled: bool) -> None:
        """Switch the held role's retransmission policy without releasing it."""

        if type(enabled) is not bool:
            raise TypeError("enabled must be boolean")
        self._ensure_active()
        route = self._ownership.route
        if not canbus.ip_up(
            route.channel,
            route.bitrate,
            listen_only=False,
            restart_ms=0,
            one_shot=enabled,
            noninteractive=True,
        ):
            raise CanWakeError(
                "wake_failed",
                f"could not re-arm {route.role}/{route.channel} with one-shot "
                f"{'on' if enabled else 'off'}",
                role=route.role,
            )
        self._active_one_shot = enabled
        self._ensure_active()

    def _trigger_b_can(self) -> WakeResult:
        self._ensure_active()
        route = self._ownership.route
        sock = socket.socket(AF_CAN, socket.SOCK_RAW, CAN_RAW)
        sent = 0
        try:
            sock.bind((route.channel,))
            frame = struct.pack("=IB3x8s", _B_CAN_WAKE_ID, 0, b"")
            for _attempt in range(_B_CAN_WAKE_ATTEMPTS):
                try:
                    sock.send(frame)
                    sent += 1
                except OSError:
                    # A still-asleep unACKed branch may reject a send. The
                    # installed gs_usb controller has no automatic bus-off
                    # restart; any unhealthy controller state fails the next
                    # exact active gate and the session restores passively.
                    pass
                time.sleep(_B_CAN_WAKE_GAP_SECONDS)
        except OSError as exc:
            raise CanWakeError(
                "wake_failed",
                f"B-CAN wake burst could not use its resolved socket: {exc}",
                role=route.role,
            ) from exc
        finally:
            sock.close()
        if sent == 0:
            raise CanWakeError(
                "wake_failed",
                "all 75 fixed B-CAN wake-frame attempts failed locally",
                role=route.role,
            )
        self._ensure_active()
        observed = canbus.identify_bus(route.channel, probe=0.75)
        if observed != "b-can":
            raise CanWakeError(
                "wake_failed" if observed == "silent" else "wrong_bus",
                f"post-wake B-CAN signature validation returned {observed}",
                role=route.role,
            )
        try:
            frames = _recv_standard_frames(
                route.channel, (_B_CAN_VOLTAGE_ID,), 2.0
            )
        except OSError as exc:
            raise CanWakeError(
                "wake_failed",
                f"woken B-CAN voltage verification failed: {exc}",
                role=route.role,
            ) from exc
        samples = []
        for _can_id, data in frames:
            if len(data) < 6:
                continue
            value = (
                ((data[4] << 8) | data[5]) & _B_CAN_VOLTAGE_MASK
            ) / _B_CAN_VOLTAGE_DIVISOR
            if _B_CAN_SANE_VOLTAGE[0] <= value <= _B_CAN_SANE_VOLTAGE[1]:
                samples.append(value)
        if not samples:
            raise CanWakeError(
                "wake_failed",
                "B-CAN woke but no sane verified 0x46C voltage arrived",
                role=route.role,
            )
        samples.sort()
        voltage = round(samples[len(samples) // 2], 2)
        self._ensure_active()
        return WakeResult(
            role=route.role,
            source=self._profile.source,
            voltage=voltage,
            detail=(
                f"fixed 75-frame 0x7FF wake validated by B-CAN signatures "
                f"and {len(samples)} sane 0x46C sample(s)"
            ),
        )

    def _trigger_c_can(self) -> WakeResult:
        self._ensure_active()
        route = self._ownership.route
        request_frame = struct.pack(
            "=IB3x8s",
            CAN_EFF_FLAG | _C_CAN_RFH_TXID,
            8,
            _C_CAN_WAKE_REQUEST_DATA,
        )

        def receive_one(sock, timeout: float) -> bool:
            sock.settimeout(timeout)
            try:
                raw_frame = sock.recv(16)
            except (BlockingIOError, TimeoutError, socket.timeout):
                return False
            if len(raw_frame) != 16:
                return False
            can_id, dlc, raw_data = struct.unpack("=IB3x8s", raw_frame)
            if can_id & (CAN_RTR_FLAG | CAN_ERR_FLAG):
                return False
            if not can_id & CAN_EFF_FLAG:
                return False
            if (can_id & CAN_EFF_MASK) != _C_CAN_RFH_RXID or not 1 <= dlc <= 8:
                return False
            data = raw_data[:dlc]
            if data[0] >> 4 != 0:
                return False
            payload_length = data[0] & 0x0F
            if payload_length > 7 or len(data) < payload_length + 1:
                return False
            payload = bytes(data[1 : 1 + payload_length])
            return bool(
                payload_length == _C_CAN_WAKE_RESPONSE_LENGTH
                and payload.startswith(_C_CAN_WAKE_RESPONSE_PREFIX)
            )

        observed = canbus.identify_bus(route.channel, probe=0.25)
        if observed not in ("c-can", "silent"):
            raise CanWakeError(
                "wrong_bus",
                f"pre-wake C-CAN signature validation returned {observed}",
                role=route.role,
            )

        if observed == "silent":
            wake_sock = socket.socket(AF_CAN, socket.SOCK_RAW, CAN_RAW)
            try:
                wake_sock.bind((route.channel,))
                for _attempt in range(_C_CAN_WAKE_ATTEMPTS):
                    # ONE-SHOT prevents a hardware retransmission storm. A
                # rapid fixed application burst of the reviewed compact
                # key-off DID supplies enough addressed activity to wake
                # RFH/network management without allowing the controller to
                # accumulate ERROR-WARNING state.
                    self._ensure_active()
                    sent = wake_sock.send(request_frame)
                    if sent != len(request_frame):
                        raise CanWakeError(
                            "wake_failed",
                            f"C-CAN RF Hub wake send length {sent} was not "
                            f"{len(request_frame)}",
                            role=route.role,
                        )
                    time.sleep(_C_CAN_WAKE_RETRY_SECONDS)
            except CanWakeError:
                raise
            except OSError as exc:
                raise CanWakeError(
                    "wake_failed",
                    f"C-CAN RF Hub one-shot wake burst failed: {exc}",
                    role=route.role,
                ) from exc
            finally:
                try:
                    wake_sock.close()
                except Exception:
                    pass
            observed = canbus.identify_bus(route.channel, probe=0.5)
            if observed != "c-can":
                raise CanWakeError(
                    "wake_failed" if observed == "silent" else "wrong_bus",
                    f"post-wake C-CAN signature validation returned {observed}",
                    role=route.role,
                )

        # Once C-CAN is broadcasting, switch this still-exclusive owner back
        # to normal retransmission. The validation read is now immediately
        # acknowledged and refreshes the same RFH network-management wake.
        self._rearm_one_shot(False)
        validation_sock = socket.socket(AF_CAN, socket.SOCK_RAW, CAN_RAW)
        response_valid = False
        try:
            response_filter = struct.pack(
                "=II",
                CAN_EFF_FLAG | _C_CAN_RFH_RXID,
                CAN_EFF_FLAG | CAN_RTR_FLAG | CAN_EFF_MASK,
            )
            validation_sock.setsockopt(
                SOL_CAN_RAW, CAN_RAW_FILTER, response_filter
            )
            validation_sock.bind((route.channel,))
            validation_sock.settimeout(0.0)
            while True:
                try:
                    if not validation_sock.recv(16):
                        break
                except (BlockingIOError, TimeoutError, socket.timeout):
                    break
            self._ensure_active()
            sent = validation_sock.send(request_frame)
            if sent != len(request_frame):
                raise CanWakeError(
                    "wake_failed",
                    f"C-CAN RF Hub validation send length {sent} was not "
                    f"{len(request_frame)}",
                    role=route.role,
                )
            response_valid = receive_one(validation_sock, 0.75)
        except CanWakeError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise CanWakeError(
                "wake_failed",
                f"C-CAN RF Hub wake exchange failed: {exc}",
                role=route.role,
            ) from exc
        finally:
            try:
                validation_sock.close()
            except Exception:
                pass
        if not response_valid:
            raise CanWakeError(
                "wake_failed",
                f"RF Hub did not return the fixed positive 62 {_C_CAN_WAKE_DID:04X} "
                "single-frame response after C-CAN wake",
                role=route.role,
            )
        observed = canbus.identify_bus(route.channel, probe=0.25)
        if observed != "c-can":
            raise CanWakeError(
                "wake_failed" if observed == "silent" else "wrong_bus",
                f"post-wake C-CAN signature validation returned {observed}",
                role=route.role,
            )
        # Revalidate ownership/inhibits and reject a newly present ignition or
        # running witness before reporting this addressed wake as successful.
        self._ensure_active()
        return WakeResult(
            role=route.role,
            source=self._profile.source,
            detail=(
                f"fixed one-shot RF Hub 22 {_C_CAN_WAKE_DID:04X} burst woke "
                "C-CAN; normal-retry validation returned the exact positive "
                "DID echo and broadcast signatures"
            ),
        )

    def trigger(self) -> WakeResult:
        """Send and validate the fixed trigger for this session's role."""

        if self._profile.role == "b-can":
            return self._trigger_b_can()
        return self._trigger_c_can()

    def engine_running(self) -> bool | None:
        """Return a fixed C-CAN RPM witness without exposing a channel.

        This is an additional safety observation for a held C-CAN session. It
        is not available on B-CAN and it does not treat silence as engine-off.
        """

        if self._profile.role != "c-can":
            raise CanWakeError(
                "unsupported_role",
                "engine-running witness is defined only for c-can",
                role=self._profile.role,
            )
        self._ensure_active()
        route = self._ownership.route
        try:
            frames = _recv_standard_frames(
                route.channel, (_C_CAN_ENGINE_SPEED_ID,), 0.25
            )
        except OSError as exc:
            raise CanWakeError(
                "source_unavailable",
                f"could not read the fixed C-CAN engine-speed witness: {exc}",
                role=route.role,
            ) from exc
        rpms = [
            (int.from_bytes(data[:2], "big") & 0xFFFC) / 4.0
            for _can_id, data in frames
            if len(data) >= 2
        ]
        if not rpms:
            return None
        return max(rpms) >= _C_CAN_RUNNING_RPM

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._ownership.release():
            raise canbus.PassiveRestoreError(
                f"could not verify {self._profile.role} passive restoration"
            )

    def __enter__(self) -> "_WakeSession":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


def _open_wake_session(
    role: str,
    *,
    prearm_check: Callable[[], Iterable[object]],
    manager=None,
) -> _WakeSession:
    """Arm one internal fixed-profile session and hold ownership until close."""

    profile = _profile(role)
    if not callable(prearm_check):
        raise TypeError("prearm_check must be callable")
    passive_check = _c_can_safety_conflicts if role == "c-can" else None
    try:
        ownership = can_runtime_route.acquire_armed_bus_route(
            profile.role,
            asserted_pair=profile.pair,
            prearm_check=prearm_check,
            manager=manager,
            one_shot=profile.one_shot,
            wake_probe_policy=profile.wake_probe_policy,
            passive_prearm_check=passive_check,
        )
    except canbus.PassiveRestoreError:
        raise
    except Exception as exc:
        raise _classify_route_error(exc, profile.role) from exc
    return _WakeSession(profile, ownership, prearm_check)


@contextmanager
def _termination_guard():
    """Use process signal shielding where Python permits handler changes.

    Unix-API acquisitions run in worker threads, where signals are delivered
    to the main thread and ``signal.signal`` is forbidden.  The main-thread
    path installs the repository guard; the worker path still uses the same
    unconditional cleanup block.
    """

    if threading.current_thread() is threading.main_thread():
        with diagnostic_safety.interrupt_on_termination() as guard:
            yield guard
        return

    class _WorkerGuard:
        @staticmethod
        def begin_cleanup() -> None:
            return None

    yield _WorkerGuard()


def wake_once(
    role: str,
    *,
    prearm_check: Callable[[], Iterable[object]],
    manager=None,
) -> WakeResult:
    """Run one fixed logical wake and return only after exact restoration."""

    with _termination_guard() as termination:
        session = None
        try:
            session = _open_wake_session(
                role,
                prearm_check=prearm_check,
                manager=manager,
            )
            return session.trigger()
        finally:
            # Repeated INT/TERM/HUP cannot cut through exact passive restore.
            termination.begin_cleanup()
            if session is not None:
                session.close()


__all__ = (
    "CanWakeError",
    "WakeResult",
    "wake_once",
)
