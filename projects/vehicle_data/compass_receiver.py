"""Receive compass events, the fuel tank level and the range, receive-only, on the B-CAN.

Each receive session takes one of two routes. While the broker proves the
auxiliary drive helper's armed B-CAN, the companion borrows that owner and takes
no lease, so it can never hold up the helper's arming or restoration. Otherwise
(ignition on and engine off, or an engine epoch with the helper disabled or
blocked) it holds the same shared passive role and channel leases the C-CAN
display receiver holds, behind the cooperative passive turn, re-proves the route
about once a second and yields within one receive timeout to a cooperative
exclusive user. The broker pauses the passive route before it claims the
auxiliary helper and resumes it when that helper's run is over. Neither route
configures an interface, transmits or launches a helper. Outside a session the
last direction and fuel readings remain dated retained readings.
"""

from collections.abc import Callable
from dataclasses import dataclass
import threading
import time

from lib import can_handoff
from projects.vehicle_data import bcan_companion, fuel_level
from projects.vehicle_data.can_interfaces import B_CAN_ROLE
from projects.vehicle_data.api_schema import StatusResponse
from projects.vehicle_data.display_receiver import CONTROL_SECONDS, RECHECK_SECONDS, receive_display_frames
from projects.vehicle_data.display_route import check_display_route, passive_display_route
from projects.vehicle_data.drive_recorder import admit_secondary_route, broker_secondary_route


ARMED_COMPANION = 'broker_auxiliary_drive_companion'
BCAN_BITRATE = 125000


@dataclass(frozen=True)
class LocalBrokerStatusClient:
    """Expose cached status through the recorder's client contract, with no HTTP I/O."""

    read_status: Callable[[], StatusResponse]

    def request(self, method: str, path: str) -> tuple[int, StatusResponse]:
        if (method, path) != ('GET', '/v1/status'):
            raise ValueError('local status client permits only GET /v1/status')
        return 200, self.read_status()


def armed_compass_route(status):
    route = broker_secondary_route(status, 'b-can')
    if route.ownership != ARMED_COMPANION:
        raise RuntimeError('compass is waiting for the B-CAN drive owner')
    return route


class CompassReceiver:
    def __init__(self, manager, *, status_reader, publish, invalidate, invalidate_frame=None,
                 tank_tap=None, receiver=receive_display_frames, admit=admit_secondary_route,
                 monotonic=time.monotonic):
        self.manager = manager
        self.status_reader = status_reader
        self.status_client = LocalBrokerStatusClient(status_reader)
        self.publish = publish
        self.invalidate = invalidate
        self.invalidate_frame = invalidate_frame
        # Sees every fresh 0x356 reading, 0 included, which the published metric never carries.
        self.tank_tap = tank_tap
        self.receiver = receiver
        self.admit = admit
        self.monotonic = monotonic
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        # Pause/admission use DisplayReceiver's handshake: pause is set under the lock, a
        # passive session clears the released event under that lock, so a pause either
        # prevents admission or waits for the lease to be released.
        self._pause = threading.Event()
        self._passive_released = threading.Event()
        self._passive_released.set()
        self._resumes = 0
        self._status = {'state': 'stopped', 'frames': 0, 'last_received_at': None,
                        'channel': None, 'reason': None, 'interface_mode': None}

    def status(self):
        with self._lock:
            return dict(self._status)

    def start(self):
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self.run, name='van-compass-receiver', daemon=True)
            self._thread.start()

    def stop(self, timeout=2.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if not self._thread.is_alive():
                self._thread = None

    def pause_passive(self, timeout=2.0):
        """Release the shared passive leases and admit no new passive session; True once released."""
        with self._lock:
            self._pause.set()
        return self._passive_released.wait(timeout)

    def resume_passive(self):
        with self._lock:
            self._pause.clear()
            self._resumes += 1  # an armed session of the paused interval ends with it

    def _receive(self, channel, mode, valid):
        throttle = bcan_companion.PublishThrottle()

        def publish(observations, observed_at, observed_monotonic):
            # Throttle only the publication; the route check runs in full on each admit.
            if not throttle.admit(observations, observed_monotonic):
                return
            if not valid(force=True):
                return
            self.publish(observations, observed_at, observed_monotonic, mode)
            with self._lock:
                self._status['frames'] += 1
                self._status['last_received_at'] = observed_at.isoformat()

        # After any loss the cache entry is gone, so the next fuel frame must publish at once.
        def lost():
            throttle.reset()
            self.invalidate()

        def frame_lost(can_id):
            throttle.reset()
            self.invalidate_frame(can_id)

        tank_next = float('-inf')

        def observe(can_id, data, _observed_at, observed_monotonic):
            # The gauge needs the 0 the metric never carries, with the receipt time the metric
            # uses, and only from a frame the route check still admits. At most one a second.
            nonlocal tank_next
            if self.tank_tap is None or observed_monotonic < tank_next:
                return
            level = fuel_level.raw_reading(can_id, data)
            if level is None or not valid(force=True):
                return
            tank_next = observed_monotonic + 1.0
            self.tank_tap(level, observed_monotonic)

        with self._lock:
            self._status.update(state='receiving', channel=channel, reason=None,
                                interface_mode=mode)
        self.receiver(channel, valid=valid, stopped=self._stop.is_set, publish=publish,
                      frame_dlcs=bcan_companion.FRAME_DLCS, decode=bcan_companion.decode_bcan_companion,
                      observe=observe,
                      lost=lost, frame_lost=frame_lost if self.invalidate_frame else None)

    def _listen_armed(self, route, check, owner_pid):
        next_check = float('-inf')
        with self._lock:
            resumes = self._resumes

        def valid(force=False):
            nonlocal next_check
            if self._stop.is_set():
                return False
            now = self.monotonic()
            if force or now >= next_check:
                status = self.status_reader()
                if (armed_compass_route(status) != route
                        or status['auxiliary_drive']['helper_pid'] != owner_pid):
                    return False
                check()
                next_check = now + 1.0
            with self._lock:
                return not self._stop.is_set() and self._resumes == resumes

        self._receive(route.channel, 'armed_diagnostic', valid)

    def _listen_passive(self, route):
        next_check = next_control = float('-inf')

        def valid(force=False):
            nonlocal next_check, next_control
            if self._stop.is_set() or self._pause.is_set():
                return False
            now = self.monotonic()
            if force or now >= next_control:
                # A cooperative exclusive user holds this gate; drain our shared turn.
                if can_handoff.passive_yield_requested(B_CAN_ROLE):
                    return False
                next_control = now + CONTROL_SECONDS
            if force or now >= next_check:
                check_display_route(self.manager, B_CAN_ROLE, 'B-CAN', BCAN_BITRATE, route,
                                    listen_only=True)
                next_check = now + RECHECK_SECONDS
            return True

        if valid(force=True):
            self._receive(route[0], 'listen_only', valid)

    def _run_armed(self, status, expected):
        (route, check, stack), _interface = self.admit(
            'b-can', status, self.status_client, self.manager, expected_route=expected,
        )
        try:
            self._listen_armed(route, check, status['auxiliary_drive']['helper_pid'])
        finally:
            stack.close()

    def _run_passive(self):
        with self._lock:
            if self._pause.is_set():
                self._status.update(state='waiting', channel=None, interface_mode=None,
                                    reason='paused for the B-CAN drive helper')
                return
            self._passive_released.clear()
        try:
            with passive_display_route(self.manager, B_CAN_ROLE) as route:
                self._listen_passive(route)
        finally:
            self._passive_released.set()

    @staticmethod
    def _armed_route(status):
        try:
            return armed_compass_route(status)
        except RuntimeError:
            return None  # not armed: the passive route applies

    def run(self):
        try:
            while not self._stop.is_set():
                try:
                    status = self.status_reader()
                    expected = self._armed_route(status)
                    try:
                        if expected is not None:
                            self._run_armed(status, expected)
                        else:
                            self._run_passive()
                    finally:
                        self.invalidate()
                        with self._lock:
                            self._status.update(state='waiting', channel=None, interface_mode=None)
                except (OSError, RuntimeError, ValueError, KeyError) as exc:
                    with self._lock:
                        self._status.update(state='waiting', channel=None, interface_mode=None,
                                            reason=str(exc))
                self._stop.wait(CONTROL_SECONDS)
        finally:
            self.invalidate()
            self._passive_released.set()
            with self._lock:
                self._status.update(state='stopped', channel=None, interface_mode=None)
