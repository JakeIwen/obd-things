"""Continuous bounded foreign-tester watch, receive-only and fail closed.

The step drains every cycle and immediately before a DTC send. Local origin
means this host, not this process; channel leases exclude cooperating senders,
not manual local commands that happen to exactly match a helper request.
"""
from __future__ import annotations

import re
import socket
import struct
import time

from lib.modules import MODULES
from projects.vehicle_data import drive_dtc_contract as contract
from projects.vehicle_data import pcm_electrical as pcm, radar_alignment, radar_service_transport

SO_RXQ_OVFL = getattr(socket, 'SO_RXQ_OVFL', 40)
MAX_DRAIN_FRAMES = contract.LIMITS['watch_drain_frames']
FLAGS_MASK = pcm.CAN_EFF_FLAG | pcm.CAN_RTR_FLAG
# Linux routes filters whose mask includes ERR to its error-only receiver.
# Keep ERR out so ordinary frames arrive; _observe rejects it in userspace.
# Standard IDs must not match the low bits of an extended tester request.
FILTERS = ((pcm.CAN_EFF_FLAG | 0x18DA00F0, FLAGS_MASK | 0x1FFF00F0),
           (pcm.CAN_EFF_FLAG | 0x18DB00F0, FLAGS_MASK | 0x1FFF00F0),
           (0x7DF, FLAGS_MASK | 0x7FF), (0x7E0, FLAGS_MASK | 0x7F8))


def helper_request_signatures(rf_hub_profiles) -> frozenset:
    """Known helper (id-with-flags, exact-data) pairs, not sendable CAN frames.

    Pass active_drive.TPMS_PROFILES from the composition owner. The watch must
    not import that runtime (it will itself own the watch); all other request
    data come from their existing fixed transport registries.
    """
    signatures = {(pcm.CAN_EFF_FLAG | profile.request_id, profile.request_data)
                  for profile in pcm.PCM_ELECTRICAL_PROFILES.values()}
    radar_id = pcm.CAN_EFF_FLAG | radar_alignment.MODULE.txid
    signatures.update((radar_id, data) for data in
                      (radar_alignment.REQUEST_DATA, radar_alignment.FLOW_CONTROL_DATA))
    signatures.update((radar_id, spec.request) for spec in radar_service_transport.EXCHANGES)
    rf_id = pcm.CAN_EFF_FLAG | MODULES['rf_hub'].txid
    signatures.update((rf_id, bytes((3, 0x22, did >> 8, did & 0xFF, 0, 0, 0, 0)))
                      for did, _metric in rf_hub_profiles)
    signatures.update((pcm.CAN_EFF_FLAG | endpoint.txid, contract.REQUEST_DATA)
                      for endpoint in contract.ENDPOINTS.values())
    return frozenset(signatures)


class ForeignTesterWatch:
    def __init__(self, channel, *, rf_hub_profiles, socket_factory=socket.socket,
                 monotonic=time.monotonic):
        if not isinstance(channel, str) or not re.fullmatch(r'can[0-9]+', channel):
            raise ValueError('tester watch requires a resolved CAN channel')
        self.channel, self.socket_factory, self.monotonic = channel, socket_factory, monotonic
        self.signatures = helper_request_signatures(rf_hub_profiles)
        self.sock = None
        self.dirty_until = 0.0
        self.overflow_count = 0

    def open(self):
        """Open once at step construction; caller disables the epoch on failure."""
        if self.sock is not None:
            raise RuntimeError('tester watch already open')
        sock = self.socket_factory(pcm.AF_CAN, socket.SOCK_RAW, pcm.CAN_RAW)
        try:
            sock.setsockopt(pcm.SOL_CAN_RAW, pcm.CAN_RAW_FILTER,
                            b''.join(struct.pack('=II', *item) for item in FILTERS))
            sock.setsockopt(socket.SOL_SOCKET, SO_RXQ_OVFL, 1)
            sock.bind((self.channel,))
            sock.settimeout(0)
        except BaseException:
            sock.close()
            raise
        self.sock = sock
        return self

    def close(self):
        sock, self.sock = self.sock, None
        if sock is not None:
            sock.close()

    @property
    def clean(self) -> bool:
        return self.sock is not None and self.monotonic() >= self.dirty_until

    def _dirty(self):
        self.dirty_until = self.monotonic() + contract.FOREIGN_QUIET_SECONDS

    def _observe(self, frame, ancillary, flags):
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            self._dirty()
        for level, kind, value in ancillary:
            if level == socket.SOL_SOCKET and kind == SO_RXQ_OVFL:
                if len(value) != 4:
                    self._dirty()
                    continue
                count = struct.unpack('=I', value)[0]
                if count != self.overflow_count:
                    self._dirty()
                    self.overflow_count = count
        if len(frame) != pcm.CAN_FRAME_SIZE:
            self._dirty()
            return
        can_id, dlc, data = struct.unpack(pcm.CAN_FRAME_FORMAT, frame)
        if (not flags & socket.MSG_DONTROUTE or dlc != 8
                or (can_id, data) not in self.signatures):
            self._dirty()

    def drain(self) -> bool:
        """Drain at most 64 queued frames, even while already dirty; return clean.

        Reaching the limit dirties conservatively even when the queue happened
        to contain exactly 64 frames. A continuing flood cannot block a cycle
        or age into a clean state while unread traffic remains.
        """
        if self.sock is None:
            return False
        for _ in range(MAX_DRAIN_FRAMES):
            try:
                frame, ancillary, flags, _address = self.sock.recvmsg(
                    pcm.CAN_FRAME_SIZE, socket.CMSG_SPACE(4), socket.MSG_DONTWAIT)
            except (BlockingIOError, TimeoutError):
                return self.clean
            except (OSError, ValueError):
                self._dirty()
                return False
            self._observe(frame, ancillary, flags)
        self._dirty()
        return False
