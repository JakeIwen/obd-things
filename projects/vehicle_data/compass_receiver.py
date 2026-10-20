"""Receive compass events beside the broker's existing armed B-CAN owner.

There is no passive lease here: this companion exists only during the proven
auxiliary owner's interval. It cannot block that owner's startup/restoration,
configure an interface, transmit, or launch a helper. Outside the interval the
last direction remains a dated retained reading.
"""

from collections.abc import Callable
from dataclasses import dataclass
import threading
import time

from projects.vehicle_data import compass
from projects.vehicle_data.api_schema import StatusResponse
from projects.vehicle_data.display_receiver import CONTROL_SECONDS, receive_display_frames
from projects.vehicle_data.drive_recorder import admit_secondary_route, broker_secondary_route


ARMED_COMPANION = 'broker_auxiliary_drive_companion'


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
    def __init__(self, manager, *, status_reader, publish, invalidate,
                 receiver=receive_display_frames, admit=admit_secondary_route,
                 monotonic=time.monotonic):
        self.manager = manager
        self.status_reader = status_reader
        self.status_client = LocalBrokerStatusClient(status_reader)
        self.publish = publish
        self.invalidate = invalidate
        self.receiver = receiver
        self.admit = admit
        self.monotonic = monotonic
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._status = {'state': 'stopped', 'frames': 0, 'last_received_at': None,
                        'channel': None, 'reason': None}

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

    def _listen(self, route, check, owner_pid):
        next_check = float('-inf')

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
            return not self._stop.is_set()

        def publish(observations, observed_at, observed_monotonic):
            if not valid(force=True):
                return
            self.publish(observations, observed_at, observed_monotonic, 'armed_diagnostic')
            with self._lock:
                self._status['frames'] += 1
                self._status['last_received_at'] = observed_at.isoformat()

        with self._lock:
            self._status.update(state='receiving', channel=route.channel, reason=None)
        self.receiver(route.channel, valid=valid, stopped=self._stop.is_set, publish=publish,
                      frame_dlcs=compass.FRAME_DLCS, decode=compass.decode_compass_observations,
                      lost=self.invalidate)

    def run(self):
        try:
            while not self._stop.is_set():
                try:
                    status = self.status_reader()
                    expected = armed_compass_route(status)
                    (route, check, stack), _interface = self.admit(
                        'b-can', status, self.status_client, self.manager,
                        expected_route=expected,
                    )
                    try:
                        self._listen(route, check, status['auxiliary_drive']['helper_pid'])
                    finally:
                        stack.close()
                        self.invalidate()
                        with self._lock:
                            self._status.update(state='waiting', channel=None)
                except (OSError, RuntimeError, ValueError, KeyError) as exc:
                    with self._lock:
                        self._status.update(state='waiting', channel=None, reason=str(exc))
                self._stop.wait(CONTROL_SECONDS)
        finally:
            self.invalidate()
            with self._lock:
                self._status.update(state='stopped', channel=None)
