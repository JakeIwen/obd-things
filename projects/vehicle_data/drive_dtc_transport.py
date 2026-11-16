"""Only the two fixed stage-1a 19 02 8F requests; never a FlowControl.

The caller owns fresh authority, watch/service checks and per-pass scheduling;
its ``admit`` check runs again after preparation, just before the permit is
consumed. The radar borrows the 0845 link; the BCM has an exact-filtered
temporary socket. Every actual send consumes a fresh one-use permit before its
in-memory marker.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import socket
import struct
import time

from projects.vehicle_data import drive_dtc_contract as contract, transmit_permit
from projects.vehicle_data.pcm_electrical import (
    AF_CAN, CAN_RAW, SOL_CAN_RAW, CAN_RAW_FILTER, CAN_EFF_FLAG, CAN_EFF_MASK,
    CAN_RTR_FLAG, CAN_FRAME_FORMAT, CAN_FRAME_SIZE,
)


@dataclass(frozen=True)
class ReadResult:
    outcome: str
    reason: str
    attempted: bool = False
    nrc: str | None = None
    pending_count: int = 0
    frame_hex: str | None = None
    count: int | None = None

    def event_fields(self) -> dict:
        """Payload fields for dtc_read; the step adds identity, evidence and dates."""
        return {key: getattr(self, key) for key in
                ('outcome', 'reason', 'nrc', 'pending_count', 'frame_hex', 'count')}


class AttemptMarkerError(RuntimeError):
    """An in-memory send marker failed; fail closed, not as an ECU outcome."""


class _BcmLink:
    """Private exact BCM link; no method accepts an arbitrary endpoint."""

    def __init__(self, channel, socket_factory, monotonic):
        self.channel, self.monotonic = channel, monotonic
        self.module = contract.ENDPOINTS['bcm_ccan']
        self.sock = socket_factory(AF_CAN, socket.SOCK_RAW, CAN_RAW)
        try:
            # Linux treats ERR in a receive mask specially and suppresses
            # ordinary frames. Filter EFF/RTR here; receive rejects ERR too.
            mask = CAN_EFF_MASK | CAN_EFF_FLAG | CAN_RTR_FLAG
            self.sock.setsockopt(SOL_CAN_RAW, CAN_RAW_FILTER,
                                 struct.pack('=II', CAN_EFF_FLAG | self.module.rxid, mask))
            self.sock.bind((channel,))
        except BaseException:
            self.sock.close()
            raise

    def close(self):
        self.sock.close()

    def drain(self):
        self.sock.settimeout(0)
        for _ in range(contract.LIMITS['reply_drain_frames']):
            try:
                self.sock.recv(CAN_FRAME_SIZE)
            except (BlockingIOError, TimeoutError):
                return
        raise ValueError('BCM response queue did not drain')

    def send_fixed(self, data, permit, purpose, *, before_send):
        frame = struct.pack(CAN_FRAME_FORMAT, CAN_EFF_FLAG | self.module.txid, 8, data)
        transmit_permit.consume(permit, purpose=purpose, channel=self.channel)
        # Same boundary as the radar link: expiry is unsent; a send error may
        # already have transmitted, so mark in memory before entering send.
        before_send()
        if self.sock.send(frame) != CAN_FRAME_SIZE:
            raise OSError('BCM DTC send was incomplete')

    def receive(self, deadline):
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise TimeoutError('BCM DTC response deadline expired')
        self.sock.settimeout(remaining)
        frame = self.sock.recv(CAN_FRAME_SIZE)
        if len(frame) != CAN_FRAME_SIZE:
            raise ValueError('BCM CAN frame length mismatch')
        can_id, dlc, data = struct.unpack(CAN_FRAME_FORMAT, frame)
        if can_id != CAN_EFF_FLAG | self.module.rxid or not 1 <= dlc <= 8:
            raise ValueError('BCM CAN identifier, flags or DLC mismatch')
        return data[:dlc]


def _receive(link, sent_at, monotonic) -> ReadResult:
    deadline = sent_at + contract.FIRST_REPLY_SECONDS
    pending = 0
    while True:
        frame_hex = None
        try:
            if monotonic() >= deadline:
                raise TimeoutError('DTC receive deadline elapsed')
            frame = link.receive(deadline)
            if isinstance(frame, bytes) and 1 <= len(frame) <= 8:
                frame_hex = frame.hex(' ').upper()
            # A late response cannot turn a timed-out request into a success.
            if monotonic() > deadline:
                raise TimeoutError('DTC response arrived after deadline')
            parsed = contract.parse_frame(frame)
        except TimeoutError:
            return ReadResult(contract.NO_RESPONSE, 'timeout', True, pending_count=pending)
        except OSError:
            return ReadResult(contract.NO_RESPONSE, 'receive_error', True, pending_count=pending)
        except ValueError:
            return ReadResult(contract.MALFORMED, 'invalid_frame', True,
                              pending_count=pending, frame_hex=frame_hex)
        outcome = parsed['outcome']
        if outcome != 'pending':
            reason = {contract.COMPLETE: 'single_frame', contract.TRUNCATED: 'first_frame',
                      contract.NEGATIVE: 'nrc'}[outcome]
            if outcome == contract.NEGATIVE:
                reason = contract.negative_reason(int(parsed['nrc'], 16))
            return ReadResult(outcome, reason, True, parsed['nrc'], pending,
                              frame_hex, parsed['count'])
        pending += 1
        # Pending extends only to send + 2.5s, never from the latest response.
        deadline = sent_at + contract.PENDING_REPLY_SECONDS


class DriveDtcTransport:
    def __init__(self, channel, *, radar_link=None, socket_factory=socket.socket,
                 monotonic=time.monotonic):
        if not isinstance(channel, str) or not re.fullmatch(r'can[0-9]+', channel):
            raise ValueError('DTC reads require a resolved CAN channel')
        if radar_link is not None and radar_link.channel != channel:
            raise ValueError('radar link must use the resolved DTC channel')
        self.channel, self.radar_link = channel, radar_link
        self.socket_factory, self.monotonic = socket_factory, monotonic

    def read(self, module: str, permit: object, *, admit, before_send) -> ReadResult:
        """One bounded exchange. ``before_send()`` records only attempted state.

        ``admit()`` returns None, or a not-sent reason that ends the exchange
        unsent and unattempted. It runs after the BCM socket is created and the
        reply queue drained, immediately before the permit is consumed, so
        preparation time cannot carry a send past a check that has since failed.
        The marker runs after permit consumption and before send; it must not
        perform I/O or block. OSError/ValueError from the socket become results;
        marker errors and non-expiry permit errors remain fatal to the helper.
        No result authorizes a retry: the step must remember its attempted set.
        """
        if module not in contract.MODULES:
            raise ValueError('DTC module is not allowlisted')
        link = self.radar_link
        owned = module == 'bcm_ccan'
        try:
            if owned:
                try:
                    link = _BcmLink(self.channel, self.socket_factory, self.monotonic)
                except (OSError, ValueError):
                    return ReadResult(contract.NOT_SENT, 'drain_failed')
            if link is None:
                return ReadResult(contract.NOT_SENT, 'drain_failed')
            return self._exchange(link, module, permit, admit, before_send)
        finally:
            if owned and link is not None and link is not self.radar_link:
                # Closing a failed exchange cannot create another transmission.
                try:
                    link.close()
                except OSError:
                    pass

    def _exchange(self, link, module, permit, admit, before_send):
        try:
            link.drain()
        except (OSError, ValueError):
            return ReadResult(contract.NOT_SENT, 'drain_failed')
        refused = admit()
        if refused is not None:
            return ReadResult(contract.NOT_SENT, refused)
        sent_at = None

        def mark():
            nonlocal sent_at
            try:
                sent_at = self.monotonic()
                before_send()
            except Exception as exc:
                raise AttemptMarkerError('DTC attempt marker failed; no send') from exc

        try:
            link.send_fixed(contract.REQUEST_DATA, permit, contract.PURPOSES[module],
                            before_send=mark)
        except transmit_permit.ExpiredTransmitPermitError:
            return ReadResult(contract.NOT_SENT, 'permit_expired')
        except transmit_permit.TransmitPermitError:
            raise
        except (OSError, ValueError):
            return ReadResult(contract.NO_RESPONSE, 'send_error', attempted=sent_at is not None)
        return _receive(link, sent_at, self.monotonic)
