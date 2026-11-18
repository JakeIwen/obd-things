"""Fixed ACC 0845 read and bounded alignment summaries; no calibration actions.

The signed microdegree interpretation is inferred in the radar project's DID
map. The 1 degree reference is a monitoring aid, not an ECU trip guarantee.
Each axis publishes 1-minute, 5-minute and 30-minute windows ending at its
latest read (see WINDOW_SECONDS for how gaps are treated), each with its mean,
peak and signed change (newest read minus oldest), and the signed change
since the first reading of the current drive (see DRIVE_GAP_SECONDS), plus the
change across the park before it (see parked_change).
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
# Rolling windows published per axis, keyed by their length in seconds. The
# short windows average one contiguous run of reads: a gap over
# STALE_AFTER_SECONDS (an engine stop, a disabled poller) starts them afresh.
# The 30-minute window is a plain time window ending at the latest read; it keeps
# averaging across such gaps so a fuel stop does not erase the trend. (A 2-hour
# window was removed 2026-11-13 at the owner's request; saved files drop it.)
WINDOW_SECONDS = (60, 300, 1800)
RUN_WINDOW_SECONDS = frozenset((60, 300))
# Older saved summaries predate the 30-minute window; they load without it.
LEGACY_WINDOW_SECONDS = (60, 300)
MAX_WINDOW_SECONDS = max(WINDOW_SECONDS)
# 1,800 s at the 10 s cadence is 180 reads per axis; the bound leaves headroom
# and also caps the history query that rebuilds a drive after a restart.
MAX_SAMPLES = 1024
# A drive is a run of radar readings with no gap over this. It borrows the
# historian's nominal trip idle timeout (history/models.py
# trip_idle_timeout_seconds) but counts radar reads, not vehicle activity, so a
# radar outage mid-drive can split it where a History trip continues. Kept as a
# copy so this module does not import the history package.
DRIVE_GAP_SECONDS = 300.0
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
    """``poll`` sends only 22 0845 and one FC for its exact 11-byte reply.

    No ISO-TP kernel socket: validate the FirstFrame before authorizing FC.
    Both sends consume independent, short-lived engine-running permits.
    ``drain``, ``send_fixed`` and ``receive`` are the socket primitives; the
    service-alignment transport uses them too, so the radar has one tester
    socket and no reply is left queued in front of the next read.
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

    def send_fixed(self, data, permit, purpose, *, before_send=None):
        frame = struct.pack(FRAME, EFF | MODULE.txid, 8, data)
        transmit_permit.consume(permit, purpose=purpose, channel=self.channel)
        # Consume, mark, send: expiry is not an attempt, but an ambiguous
        # send failure is. The optional marker must only update memory. A
        # marker failure propagates without sending; existing callers omit it.
        if before_send is not None:
            before_send()
        if self.sock.send(frame) != 16:
            raise OSError("radar fixed send was incomplete")

    def receive(self, deadline):
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

    def receive_reply(self, deadline, max_stale=4):
        """Discard bounded late DTC answers without extending another exchange.

        Stage 1a sends no flow control, so a DTC answer can only be a
        SingleFrame or FirstFrame. Neither service echo can answer 0845 or
        service alignment; a fifth stale frame keeps the malformed behavior.
        """
        for index in range(max_stale + 1):
            frame = self.receive(deadline)
            single = (len(frame) >= 3 and 2 <= frame[0] <= 7
                      and len(frame) > frame[0])
            late = (single and (frame[1:3] == b"\x59\x02"
                                or (frame[0] == 3 and frame[1:3] == b"\x7f\x19")))
            late |= (len(frame) == 8 and frame[0] >> 4 == 1
                     and frame[2:4] == b"\x59\x02")
            if not late or index == max_stale:
                return frame

    def drain(self):
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

    def poll(self, request_permit, flow_permit):
        sent = False
        try:
            self.drain()
            self.send_fixed(REQUEST_DATA, request_permit, transmit_permit.RADAR_ALIGNMENT)
            sent = True
            deadline = self.monotonic() + self.timeout
            first = self.receive_reply(deadline)
            if len(first) >= 4 and first[:3] == b"\x03\x7f\x22":
                return RadarResult(False, reason=(
                    "session_required" if first[3] in (0x7E, 0x7F) else "response_rejected"
                ), detail=f"Radar 0845 returned NRC {first[3]:02X}; no session change attempted")
            if len(first) != 8 or first[:5] != b"\x10\x0b\x62\x08\x45":
                raise ValueError("radar FirstFrame length/DID mismatch; no FlowControl sent")
            self.send_fixed(FLOW_CONTROL_DATA, flow_permit, transmit_permit.RADAR_ALIGNMENT_FLOW)
            consecutive = self.receive(deadline)
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


def _contiguous_run_start(samples):
    """Monotonic time of the first sample in the run ending at the newest one."""
    start = samples[-1][0]
    for index in range(len(samples) - 1, 0, -1):
        if samples[index][0] - samples[index - 1][0] > STALE_AFTER_SECONDS:
            break
        start = samples[index - 1][0]
    return start


def parked_change(previous, value, observed_at):
    """Change across the park that ended where a new drive starts, or None.

    ``previous`` is the metric's last stored row (its latest reading before the
    gap, in this process or retained from a saved file); ``value`` and
    ``observed_at`` are the new drive's first reading. None when there is no
    previous reading or it is not older than the new one. Additive: rows
    without the figure stay valid.
    """
    if not previous or "latest_value" not in previous or "observed_at" not in previous:
        return None
    if datetime.fromisoformat(previous["observed_at"]) >= datetime.fromisoformat(observed_at):
        return None
    return {"change": value - previous["latest_value"],
            "previous_value": previous["latest_value"],
            "previous_observed_at": previous["observed_at"],
            "observed_at": observed_at}


class AlignmentHistory:
    """Windows ending at the latest observation, retained through parked time.

    Retained data never populates the live metric cache or grants CAN authority.
    The small durable snapshot contains summaries, not old monotonic timestamps.
    """

    def __init__(self, path=None):
        self.samples = {metric: deque(maxlen=MAX_SAMPLES) for metric in METRICS}
        self.last = {metric: {} for metric in METRICS}
        self.path = Path(path) if path is not None else None
        self.lock = threading.RLock()
        self.dirty = False
        self.storage_error = None
        # Per-metric current drive, kept apart from the 30-minute samples deque
        # so a drive longer than the deque still remembers its first reading.
        self._drives = {}
        # Set by the reference points (radar_reference.py); called after each stored reading.
        self.on_reading = None
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
                self.last = {metric: self._drop_retired_windows(saved["last"][metric])
                             for metric in METRICS}
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                self.storage_error = str(exc)

    @staticmethod
    def _drop_retired_windows(row):
        # Files saved before a window was removed (7200) must not republish it.
        return {key: item for key, item in row.items()
                if not key.isdigit() or int(key) in WINDOW_SECONDS}

    @staticmethod
    def _validate_drive(drive):
        def number(name, low, high):
            value = drive[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("invalid retained radar drive")
        number("change", -40, 40)
        number("start_value", -20, 20)
        number("span_seconds", 0, math.inf)
        count = drive["count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("invalid retained radar drive count")
        if datetime.fromisoformat(drive["started_at"]).tzinfo is None:
            raise ValueError("saved radar drive start is not timezone-aware")

    @staticmethod
    def _validate_parked(parked):
        for name, low, high in (("change", -40, 40), ("previous_value", -20, 20)):
            value = parked[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("invalid retained radar parked change")
        for name in ("previous_observed_at", "observed_at"):
            if datetime.fromisoformat(parked[name]).tzinfo is None:
                raise ValueError("saved radar parked timestamp is not timezone-aware")

    @classmethod
    def _validate_retained(cls, row):
        stamp = datetime.fromisoformat(row["observed_at"])
        if stamp.tzinfo is None:
            raise ValueError("saved radar timestamp is not timezone-aware")
        value = row["latest_value"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 20:
            raise ValueError("invalid retained radar angle")
        for seconds in WINDOW_SECONDS:
            window = row.get(str(seconds))
            if window is None:
                if seconds in LEGACY_WINDOW_SECONDS:
                    raise ValueError("saved radar summary lacks a required window")
                continue
            count = window["count"]
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_SAMPLES:
                raise ValueError("invalid retained radar sample count")
            for name, low, high in (("mean", -20, 20), ("peak_abs", 0, 20), ("span_seconds", 0, seconds),
                                    ("change", -40, 40)):
                if name == "change" and name not in window:
                    continue  # saved before windows carried a change; restore rebuilds it
                value = window[name]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                    raise ValueError("invalid retained radar window")
        if "drive" in row:
            cls._validate_drive(row["drive"])
        if "parked" in row:
            cls._validate_parked(row["parked"])

    def add(self, metric, value, observed_monotonic, observed_at=None):
        if metric not in self.samples or not math.isfinite(value) or abs(value) > 20:
            return
        with self.lock:
            samples = self.samples[metric]
            if samples and observed_monotonic <= samples[-1][0]:
                return
            samples.append((observed_monotonic, value))
            while samples and observed_monotonic - samples[0][0] > MAX_WINDOW_SECONDS:
                samples.popleft()
            run_start = _contiguous_run_start(samples)
            row = {"latest_value": value,
                   "observed_at": observed_at or datetime.now(timezone.utc).isoformat()}
            row["drive"] = self._advance_drive(metric, value, observed_monotonic, row["observed_at"])
            parked = self._drives[metric]["parked"]
            if parked is not None:
                row["parked"] = parked
            for seconds in WINDOW_SECONDS:
                earliest = observed_monotonic - seconds
                if seconds in RUN_WINDOW_SECONDS:
                    earliest = max(earliest, run_start)
                window = [(t, v) for t, v in samples if earliest <= t <= observed_monotonic]
                values = [v for _, v in window]
                row[str(seconds)] = {
                    "mean": sum(values) / len(values),
                    "peak_abs": max(map(abs, values)),
                    "count": len(values),
                    "span_seconds": window[-1][0] - window[0][0],
                    # Signed change across the window: its newest read minus its oldest.
                    "change": values[-1] - values[0],
                }
            self.last[metric] = row
            self.dirty = True

    def _advance_drive(self, metric, value, mono, observed_at):
        """Extend the metric's current drive with this reading, or start a new one.

        Inside one process the gap is measured on the monotonic clock. With no
        in-memory drive (a restart), the retained row's drive continues only if
        this reading's wall time follows its last reading by at most
        DRIVE_GAP_SECONDS.
        """
        drive = self._drives.get(metric)
        if drive is not None and mono - drive["last_mono"] > DRIVE_GAP_SECONDS:
            drive = None
        if drive is None:
            drive = self._resume_retained_drive(metric, mono, observed_at)
        if drive is None:
            drive = {"start_value": value, "started_at": observed_at, "start_mono": mono, "count": 0,
                     "parked": parked_change(self.last[metric], value, observed_at)}
        drive["count"] += 1
        drive["last_mono"] = mono
        self._drives[metric] = drive
        return {"change": value - drive["start_value"], "start_value": drive["start_value"],
                "started_at": drive["started_at"], "count": drive["count"],
                "span_seconds": mono - drive["start_mono"]}

    def _resume_retained_drive(self, metric, mono, observed_at):
        if metric in self._drives:
            return None
        saved = self.last[metric]
        retained = saved.get("drive") if saved else None
        if not retained:
            return None
        wall_gap = (datetime.fromisoformat(observed_at)
                    - datetime.fromisoformat(saved["observed_at"])).total_seconds()
        if not 0 < wall_gap <= DRIVE_GAP_SECONDS:
            return None
        return {"start_value": retained["start_value"], "started_at": retained["started_at"],
                "start_mono": mono - retained["span_seconds"] - wall_gap, "count": retained["count"],
                "parked": saved.get("parked")}

    def observe(self, result):
        """Count one stored broker observation; other metrics are ignored."""
        if result.metric in self.samples:
            self.add(result.metric, result.value, result.observed_monotonic,
                     result.observed_at.isoformat())
            if self.on_reading is not None:
                self.on_reading()

    def latest(self):
        """The last reading per axis as ``(value, observed_at)``, or None until every axis has one."""
        with self.lock:
            rows = [self.last[metric] for metric in METRICS]
            if not all(rows):
                return None
            return {axis: (row["latest_value"], row["observed_at"]) for axis, row in zip(AXES, rows)}

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

    def _backfill_window_changes(self, metric, rebuilt):
        saved = self.last[metric]
        if not rebuilt or datetime.fromisoformat(rebuilt["observed_at"]) \
                != datetime.fromisoformat(saved["observed_at"]):
            return
        for seconds in WINDOW_SECONDS:
            window = saved[str(seconds)]
            if "change" not in window:
                window["change"] = rebuilt[str(seconds)]["change"]
                self.dirty = True

    def restore_from_historian(self, historian):
        """Recover the final observed windows once at startup using bounded reads.

        A saved summary that is as new as the historian but predates a window or
        the drive change (a legacy file) is rebuilt too. One that lacks only the
        window changes keeps everything it has and gains the changes when the
        historian's newest reading is its own (otherwise the next live read adds
        them). A saved drive is extended with the
        newer readings (same gap rule), keeping its start. With no saved drive
        the query reaches back at most MAX_SAMPLES * POLL_INTERVAL_SECONDS
        (about 2.8 h; repeated cached rows count toward the limit), so a rebuilt
        drive longer than that starts at the oldest reading found.
        """
        for metric in METRICS:
            latest = historian.latest_sample(metric)
            if not isinstance(latest, dict) or latest.get("source") != SOURCE or not latest.get("observed_at"):
                continue
            stamp = datetime.fromisoformat(latest["observed_at"])
            saved = self.last[metric]
            current = bool(saved) and datetime.fromisoformat(saved["observed_at"]) >= stamp and \
                "drive" in saved and all(str(seconds) in saved for seconds in WINDOW_SECONDS)
            if current and all("change" in saved[str(seconds)] for seconds in WINDOW_SECONDS):
                continue
            rows = historian.query_samples(
                metric, start=stamp - timedelta(seconds=MAX_SAMPLES * POLL_INTERVAL_SECONDS), end=latest["captured_at"],
                fresh_only=True, newest_first=True, limit=MAX_SAMPLES,
            )
            restored = AlignmentHistory()
            # A saved drive knows its true start, which may lie beyond the bounded
            # query; extend it with the readings it missed rather than rebuild it.
            extended = None
            if saved and "drive" in saved:
                extended = AlignmentHistory()
                extended.last[metric] = saved
                saved_at = datetime.fromisoformat(saved["observed_at"])
            for row in reversed(rows):
                if row.get("source") != SOURCE or row.get("unit") != "deg" or not row.get("observed_at"):
                    continue
                when = datetime.fromisoformat(row["observed_at"])
                if when <= stamp:
                    restored.add(metric, row["value"], when.timestamp(), row["observed_at"])
                    if extended is not None and when > saved_at:
                        extended.add(metric, row["value"], when.timestamp(), row["observed_at"])
            if current:
                # A current summary saved before windows carried a change keeps every
                # field it has (its drive may start beyond the bounded query); only
                # the window changes are added, and only when the rebuilt windows end
                # at the same reading as the saved ones.
                self._backfill_window_changes(metric, restored.last[metric])
                continue
            if restored.last[metric]:
                if extended is not None and extended.last[metric] is not saved:
                    restored.last[metric]["drive"] = extended.last[metric]["drive"]
                    restored.last[metric].pop("parked", None)
                    if "parked" in extended.last[metric]:
                        restored.last[metric]["parked"] = extended.last[metric]["parked"]
                self.last[metric] = restored.last[metric]
                self.dirty = True
        # Recovered wall timestamps never enter the new process's live windows.
        self.flush()
