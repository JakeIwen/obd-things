"""Fixed ACC 0845 read and bounded alignment summaries; no calibration actions.

The signed microdegree interpretation is inferred in the radar project's DID
map. The 1 degree reference is a monitoring aid, not an ECU trip guarantee.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import socket
import struct
import time
import threading

from lib.modules import MODULES
from projects.vehicle_data import transmit_permit
from projects.vehicle_data.engine_off_voltage import _atomic_json


# Parked no-session support verified 2026-09-17 UTC; see radar DID map.
# Full engine-running cadence remains subject to subsequent live validation.
LIVE_POLLING_COMMISSIONED = True
POLL_INTERVAL_SECONDS = 10.0
STALE_AFTER_SECONDS = 30.0
SOURCE = "radar_acc.did.0845"
AXES = ("elevation", "azimuth")
METRICS = tuple(f"radar.alignment.{axis}" for axis in AXES)
REQUEST_DATA = bytes.fromhex("03 22 08 45 00 00 00 00")
FLOW_CONTROL_DATA = bytes.fromhex("30 01 00 00 00 00 00 00")
FRAME = "=IB3x8s"
EFF = 0x80000000
MODULE = MODULES["radar_acc"]
assert MODULE.bus == "c-can"


def decode_alignment(payload: bytes) -> tuple[float, float]:
    if len(payload) != 11 or payload[:3] != b"\x62\x08\x45":
        raise ValueError("radar response requires exact 62 08 45 and eight data bytes")
    raw = struct.unpack(">ii", payload[3:])
    # An entirely all-ones payload is ambiguous (possible unavailable value).
    # A single -1 component is a valid signed microdegree and must not be lost.
    if payload[3:] == b"\xff" * 8:
        raise ValueError("radar alignment contains an ambiguous all-ones payload")
    angles = tuple(value / 1_000_000 for value in raw)
    if any(abs(value) > 20 for value in angles):
        raise ValueError("radar alignment is outside the supported +/-20 degree domain")
    return angles


@dataclass(frozen=True)
class RadarResult:
    available: bool
    angles: tuple[float, float] | None = None
    reason: str | None = None
    detail: str = ""


class RadarAlignmentPoller:
    """Only 22 0845 and one FC for its exact 11-byte reply are constructible.

    No ISO-TP kernel socket: validate the FirstFrame before authorizing FC.
    Both sends consume independent, short-lived engine-running permits.
    """

    def __init__(self, channel, *, socket_factory=socket.socket,
                 monotonic=time.monotonic, timeout=0.2):
        if not isinstance(channel, str) or not re.fullmatch(r"can[0-9]+", channel):
            raise ValueError("radar requires a runtime-resolved CAN channel")
        if isinstance(timeout, bool) or not 0 < timeout <= 0.2:
            raise ValueError("radar timeout must be positive and at most 0.2 seconds")
        self.channel = channel
        self.socket_factory = socket_factory
        self.monotonic = monotonic
        self.timeout = timeout
        self.sock = None

    def open(self):
        if self.sock is not None:
            raise RuntimeError("radar socket is already open")
        sock = self.socket_factory(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        try:
            sock.setsockopt(101, 1, struct.pack("=II", EFF | MODULE.rxid, 0xDFFFFFFF))
            sock.bind((self.channel,))
            sock.settimeout(self.timeout)
        except BaseException:
            sock.close()
            raise
        self.sock = sock
        return self

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def _send(self, data, permit, purpose):
        frame = struct.pack(FRAME, EFF | MODULE.txid, 8, data)
        transmit_permit.consume(permit, purpose=purpose, channel=self.channel)
        if self.sock.send(frame) != 16:
            raise OSError("radar fixed send was incomplete")

    def _receive(self, deadline):
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise TimeoutError("radar response deadline expired")
        self.sock.settimeout(remaining)
        frame = self.sock.recv(16)
        if len(frame) != 16:
            raise ValueError("radar CAN frame length mismatch")
        can_id, dlc, data = struct.unpack(FRAME, frame)
        if can_id != EFF | MODULE.rxid or not 1 <= dlc <= 8:
            raise ValueError("radar CAN identifier, flags, or DLC mismatch")
        return data[:dlc]

    def poll(self, request_permit, flow_permit):
        sent = False
        try:
            # Bounded drain: a flood cannot hold the owner or age permits forever.
            self.sock.settimeout(0)
            for _ in range(32):
                try:
                    self.sock.recv(16)
                except (BlockingIOError, TimeoutError):
                    break
            else:
                raise ValueError("radar response queue did not drain")
            self.sock.settimeout(self.timeout)
            self._send(REQUEST_DATA, request_permit, transmit_permit.RADAR_ALIGNMENT)
            sent = True
            deadline = self.monotonic() + self.timeout
            first = self._receive(deadline)
            if len(first) >= 4 and first[:3] == b"\x03\x7f\x22":
                return RadarResult(False, reason=(
                    "session_required" if first[3] in (0x7E, 0x7F) else "response_rejected"
                ), detail=f"Radar 0845 returned NRC {first[3]:02X}; no session change attempted")
            if len(first) != 8 or first[:5] != b"\x10\x0b\x62\x08\x45":
                raise ValueError("radar FirstFrame length/DID mismatch; no FlowControl sent")
            self._send(FLOW_CONTROL_DATA, flow_permit, transmit_permit.RADAR_ALIGNMENT_FLOW)
            consecutive = self._receive(deadline)
            if len(consecutive) < 6 or consecutive[0] != 0x21:
                raise ValueError("radar ConsecutiveFrame sequence/length mismatch")
            angles = decode_alignment(first[2:] + consecutive[1:6])
            return RadarResult(True, angles, detail="Stored radar alignment estimate; inferred i32 / 1e6 degrees")
        except transmit_permit.ExpiredTransmitPermitError as exc:
            return RadarResult(False, reason=(
                "response_timeout" if sent else "transmit_permit_expired"
            ), detail=str(exc))
        except transmit_permit.TransmitPermitError:
            # Wrong lock/purpose/channel/clock is an owner safety failure.
            raise
        except TimeoutError as exc:
            return RadarResult(False, reason="response_timeout", detail=str(exc))
        except ValueError as exc:
            return RadarResult(False, reason="malformed_response", detail=str(exc))
        except OSError as exc:
            return RadarResult(False, reason="response_rejected", detail=str(exc))


class AlignmentHistory:
    """Windows ending at the latest observation, retained through parked time.

    Retained data never populates the live metric cache or grants CAN authority.
    The small durable snapshot contains summaries, not old monotonic timestamps.
    """

    def __init__(self, path=None):
        self.samples = {metric: deque(maxlen=512) for metric in METRICS}
        self.last = {metric: {} for metric in METRICS}
        self.path = Path(path) if path is not None else None
        self.lock = threading.RLock()
        self.dirty = False
        self.storage_error = None
        if self.path is not None:
            try:
                if self.path.stat().st_size > 16384:
                    raise ValueError("saved radar summary exceeds size limit")
                saved = json.loads(self.path.read_text())
                if saved.get("version") != 1 or saved.get("source") != SOURCE:
                    raise ValueError("invalid saved radar summary schema")
                for metric in METRICS:
                    row = saved["last"][metric]
                    if not row:
                        continue
                    self._validate_retained(row)
                self.last = {metric: saved["last"][metric] for metric in METRICS}
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                self.storage_error = str(exc)

    @staticmethod
    def _validate_retained(row):
        stamp = datetime.fromisoformat(row["observed_at"])
        if stamp.tzinfo is None:
            raise ValueError("saved radar timestamp is not timezone-aware")
        value = row["latest_value"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 20:
            raise ValueError("invalid retained radar angle")
        for seconds in (60, 300):
            window = row[str(seconds)]
            count = window["count"]
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 512:
                raise ValueError("invalid retained radar sample count")
            for name, low, high in (("mean", -20, 20), ("peak_abs", 0, 20), ("span_seconds", 0, seconds)):
                value = window[name]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                    raise ValueError("invalid retained radar window")

    def add(self, metric, value, observed_monotonic, observed_at=None):
        if metric not in self.samples or not math.isfinite(value) or abs(value) > 20:
            return
        with self.lock:
            samples = self.samples[metric]
            if samples and observed_monotonic <= samples[-1][0]:
                return
            if samples and observed_monotonic - samples[-1][0] > STALE_AFTER_SECONDS:
                samples.clear()
            samples.append((observed_monotonic, value))
            while samples and observed_monotonic - samples[0][0] > 300:
                samples.popleft()
            row = {"latest_value": value,
                   "observed_at": observed_at or datetime.now(timezone.utc).isoformat()}
            for seconds in (60, 300):
                window = [(t, v) for t, v in samples if 0 <= observed_monotonic - t <= seconds]
                values = [v for _, v in window]
                row[str(seconds)] = {
                    "mean": sum(values) / len(values),
                    "peak_abs": max(map(abs, values)),
                    "count": len(values),
                    "span_seconds": window[-1][0] - window[0][0],
                }
            self.last[metric] = row
            self.dirty = True

    def summary(self, now):
        # Deliberately do not age away the last measured windows during parking.
        # Current/stale status continues to come from the separate metric cache.
        with self.lock:
            return json.loads(json.dumps(self.last))

    def flush(self):
        with self.lock:
            if self.path is None or not self.dirty:
                return
            try:
                _atomic_json(self.path, {"version": 1, "source": SOURCE, "last": self.last})
            except OSError as exc:
                self.storage_error = str(exc)
            else:
                self.storage_error = None
                self.dirty = False

    def restore_from_historian(self, historian):
        """Recover the final observed windows once at startup using bounded reads."""
        for metric in METRICS:
            latest = historian.latest_sample(metric)
            if not isinstance(latest, dict) or latest.get("source") != SOURCE or not latest.get("observed_at"):
                continue
            stamp = datetime.fromisoformat(latest["observed_at"])
            if self.last[metric] and datetime.fromisoformat(self.last[metric]["observed_at"]) >= stamp:
                continue
            rows = historian.query_samples(
                metric, start=stamp - timedelta(seconds=300), end=latest["captured_at"],
                fresh_only=True, newest_first=True, limit=256,
            )
            restored = AlignmentHistory()
            for row in reversed(rows):
                if row.get("source") != SOURCE or row.get("unit") != "deg" or not row.get("observed_at"):
                    continue
                when = datetime.fromisoformat(row["observed_at"])
                if when <= stamp:
                    restored.add(metric, row["value"], when.timestamp(), row["observed_at"])
            if restored.last[metric]:
                self.last[metric] = restored.last[metric]
                self.dirty = True
        # Recovered wall timestamps never enter the new process's live windows.
        self.flush()
