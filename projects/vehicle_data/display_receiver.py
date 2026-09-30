"""Continuous, receive-only cluster display feed for the telemetry broker.

The passive branch holds shared role/channel leases and yields to the existing
wake handoff gate. The armed branch borrows only the recorder's proven broker
owner, never its exclusive locks. Neither branch configures or sends CAN.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import socket
import struct
import threading
import time

from lib import can_handoff, can_operation_state
from projects.vehicle_data import ccan_powertrain as cp
from projects.vehicle_data.can_interfaces import C_CAN_ROLE
from projects.vehicle_data.drive_recorder import broker_c_can_route


CONTROL_SECONDS = 0.25
RECHECK_SECONDS = 1.0
# Linux SO_TIMESTAMPNS_OLD; vanpi is 64-bit (two native 64-bit longs).
SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)
TIMESPEC = "@ll"
DISPLAY_SOURCES = frozenset((cp.SPEED_LIMIT_SOURCE, cp.ACC_DISPLAY_SOURCE))


def receive_display_frames(channel, *, valid, stopped, publish,
                           socket_factory=socket.socket, monotonic=time.monotonic,
                           wall_time=time.time):
    """Keep one filtered socket open, publishing each received frame immediately.

    Kernel receipt timestamps prevent a host stall or a queued frame from
    acquiring a new age. No snapshot schedule, aggregation, or sample holding
    participates in this path. ``valid`` also cooperatively yields ownership.
    """
    sock = socket_factory(cp.AF_CAN, socket.SOCK_RAW, cp.CAN_RAW)
    try:
        sock.setsockopt(cp.SOL_CAN_RAW, cp.CAN_RAW_FILTER, b"".join(
            struct.pack("=II", can_id, cp.FILTER_MASK) for can_id in cp.DISPLAY_FRAME_IDS
        ))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
        sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
        sock.bind((channel,))
        sock.settimeout(CONTROL_SECONDS)
        while not stopped():
            if not valid():
                return
            try:
                frame, ancillary, flags, _address = sock.recvmsg(
                    16, socket.CMSG_SPACE(struct.calcsize(TIMESPEC))
                )
            except socket.timeout:
                continue
            if stopped() or not valid():
                return
            if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(frame) != 16:
                continue
            can_id, dlc, data = struct.unpack("=IB3x8s", frame)
            if can_id not in cp.DISPLAY_FRAME_IDS or can_id & cp.FRAME_TYPE_FLAGS:
                continue
            if dlc != (8 if can_id == cp.ACC_DISPLAY_ID else 4):
                continue
            received = None
            for level, kind, value in ancillary:
                if level == socket.SOL_SOCKET and kind == SO_TIMESTAMPNS:
                    if len(value) >= struct.calcsize(TIMESPEC):
                        sec, ns = struct.unpack(TIMESPEC, value[:struct.calcsize(TIMESPEC)])
                        if sec > 0 and 0 <= ns < 1_000_000_000:
                            received = sec + ns / 1_000_000_000
            if received is None:
                continue  # No receipt evidence: never manufacture freshness.
            age = wall_time() - received
            if age < -0.05 or age > 1.0:
                continue  # Discard a stalled queue; wait for a current frame.
            observations = cp.decode_frame_observations(can_id, data[:dlc])
            if observations:
                publish(observations, datetime.fromtimestamp(received, timezone.utc),
                        monotonic() - max(0.0, age))
    finally:
        sock.close()


class DisplayReceiver:
    """One broker-owned thread; only the two display identifiers reach Python."""

    def __init__(self, manager, *, status_reader, publish,
                 receiver=receive_display_frames, monotonic=time.monotonic):
        self.manager = manager
        self.status_reader = status_reader
        self.publish = publish
        self.receiver = receiver
        self.monotonic = monotonic
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._passive_released = threading.Event()
        self._passive_released.set()
        self._lock = threading.Lock()
        self._thread = None
        self._status = {"state": "stopped", "frames": 0, "last_received_at": None,
                        "reason": None, "channel": None, "interface_mode": None}

    def status(self):
        with self._lock:
            return dict(self._status)

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="van-display-receiver", daemon=True)
        self._thread.start()

    def stop(self, timeout=2.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if not self._thread.is_alive():
                self._thread = None

    def pause_passive(self, timeout=2.0):
        # Serialize the pause with admission, including time spent acquiring a
        # lease. The caller starts the helper only after every lease is released.
        with self._lock:
            self._pause.set()
        return self._passive_released.wait(timeout)

    def resume_passive(self):
        self._pause.clear()

    def _check_route(self, channel, serial, dev_id, *, passive):
        resolution = self.manager.topology().resolution(C_CAN_ROLE)
        if (resolution.require_channel(), resolution.spec.usb_serial,
                resolution.spec.dev_id) != (channel, serial, dev_id):
            raise RuntimeError("C-CAN USB role changed")
        state = self.manager.interface_state_reader(channel)
        if not (state.channel == channel and state.present and state.up
                and state.bitrate == 500000 and state.fd_enabled is False
                and state.one_shot is False and state.restart_ms == 0
                and state.controller_state == "ERROR-ACTIVE"
                and state.listen_only is passive):
            raise RuntimeError("C-CAN display route is no longer healthy")
        if can_operation_state.active_inhibits(channel):
            raise RuntimeError("C-CAN operation inhibit is active")
        if not passive and broker_c_can_route(self.status_reader()) != (channel, serial, dev_id):
            raise RuntimeError("broker active-drive owner changed")

    @contextmanager
    def _route(self, passive):
        if passive:
            with can_handoff.passive_turn(C_CAN_ROLE):
                with self.manager.observe(C_CAN_ROLE) as lease:
                    yield lease.channel, lease.usb_serial, lease.dev_id
        else:
            # Same narrow exception as drive_recorder: the verified helper
            # retains exclusive ownership. This companion only receives.
            yield broker_c_can_route(self.status_reader())

    def _listen(self, passive, route):
        channel, serial, dev_id = route
        mode = "listen_only" if passive else "armed_diagnostic"
        next_check = float("-inf")
        next_control = float("-inf")

        def valid():
            nonlocal next_check, next_control
            if self._stop.is_set() or passive == self._pause.is_set():
                return False
            now = self.monotonic()
            if now >= next_control:
                if passive:
                    # A wake waiter holds this gate exclusively. Let our
                    # existing shared turn drain within one receive timeout.
                    if can_handoff.passive_yield_requested(C_CAN_ROLE):
                        return False
                next_control = now + CONTROL_SECONDS
            if now >= next_check:
                self._check_route(channel, serial, dev_id, passive=passive)
                next_check = now + RECHECK_SECONDS
            return True

        def publish(observations, observed_at, observed_monotonic):
            self.publish(observations, observed_at, observed_monotonic, mode)
            with self._lock:
                self._status["frames"] += 1
                self._status["last_received_at"] = observed_at.isoformat()

        if not valid():
            return
        with self._lock:
            self._status.update(state="receiving", reason=None, channel=channel, interface_mode=mode)
        self.receiver(channel, valid=valid, stopped=self._stop.is_set, publish=publish)

    def run(self):
        try:
            while not self._stop.is_set():
                with self._lock:
                    passive = not self._pause.is_set()
                    if passive:
                        self._passive_released.clear()
                try:
                    with self._route(passive) as route:
                        self._listen(passive, route)
                except (OSError, RuntimeError, ValueError, KeyError) as exc:
                    with self._lock:
                        self._status.update(state="waiting", reason=str(exc), channel=None,
                                            interface_mode=None)
                finally:
                    self._passive_released.set()
                self._stop.wait(CONTROL_SECONDS)
        finally:
            with self._lock:
                self._status.update(state="stopped", channel=None, interface_mode=None)
