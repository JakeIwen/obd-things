#!/usr/bin/env python3
"""Passive harvester for the van's own diagnostic sweeps (tester source F1).

An in-vehicle diagnostic client (probably the TBM2 telematics box) occasionally runs a read-only
sweep: identity DIDs and ``19 02 0D`` DTC reads of about fifteen modules on C-CAN and B-CAN.  CAN-CH
modules are reached through the gateway, which copies their replies onto C-CAN re-addressed as
``18DAF2xx``.  The client waits for the Pi's tester address (also F1) to be quiet, so a sweep only
appears while the Pi's active-drive helper is not polling.

This module turns those free sweeps into app data **without any CAN access**.  It reads only the
completed chunk files of the synchronized drive recorder (``drive_recorder.py``): a chunk is used
only after the recorder's own ``manifest.jsonl`` lists it as complete and its size matches the
recorded compressed size.  The ``.partial`` file being written, ``capture.lock`` and the recorder
process are never touched.  Nothing here imports SocketCAN or can transmit.

Pipeline per campaign and bus (one chunk at a time, in order, with a small carried state):

1. ``zstd -dcq chunk | grep -F ' 18DA'`` keeps only 29-bit physical diagnostic frames (C speed).
2. ISO-TP single/first/consecutive frames are reassembled per direction and peer; flow control is
   skipped.  ``7F xx 78`` (response pending) keeps the request open.
3. Requests from F1 are paired with the reply from the same target (``18DAF1xx``, or the gateway's
   ``18DAF2xx`` copy on C-CAN).  The Pi's own fixed helper requests (see ``PI_FIXED_REQUESTS`` and
   ``PI_DTC_TOOL_REQUESTS``; ``tests/test_van_scan_harvest.py`` keeps them in sync with the helper
   modules) are only counted, as activity intervals.
4. Non-Pi exchanges are grouped into windows (gap > ``WINDOW_GAP_SECONDS``).  A closed window that
   contains a ``19 02 0D`` request is an in-vehicle health check: its per-module ``59 02`` (or the
   PCM's KWP ``58``) results, identity DIDs and other reads are written to
   ``tmp/vehicle_data/van_scans/scans/<scan_id>.json`` and imported into separate, clearly
   sourced tables of ``tmp/vehicle_data/dtc-history.sqlite3`` (the Pi's ``module_scans`` history is
   untouched: a ``19 02 0D`` result cannot resolve codes the Pi read with ``19 02 FF``).
5. ``tmp/vehicle_data/van-scan-cache.json`` (the latest successful in-vehicle result per module) is
   rewritten atomically for the broker's ``/v1/diagnostics/dtcs`` response.

VINs are masked in every stored payload: ``F190`` replies are replaced entirely and any 17-character
VIN-shaped run inside another reply (``F1A0`` embeds one) is overwritten with ``*``.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fnmatch
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable, Iterator, Mapping, Sequence


REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib.dtc import (  # noqa: E402
    DtcParseError,
    parse_dtc_list_response,
)
from lib.modules import MODULES  # noqa: E402


DEFAULT_CAPTURE_ROOT = (
    Path("/mnt/EXFAT512") / "obd-things" / "tmp" / "captures" / "three_bus_drive" / "broker-drive"
)
DEFAULT_OUT_DIR = REPO / "tmp" / "vehicle_data" / "van_scans"
DEFAULT_DB = REPO / "tmp" / "vehicle_data" / "dtc-history.sqlite3"
DEFAULT_CACHE = REPO / "tmp" / "vehicle_data" / "van-scan-cache.json"

BUSES = ("c-can", "b-can", "can-ch")
TESTER = 0xF1
GATEWAY_COPY = 0xF2
PROVENANCE = "in-vehicle scan (source F1), passive"
VAN_DTC_REQUEST = bytes.fromhex("19 02 0D")
KWP_DTC_REQUEST = bytes.fromhex("18 00 FF 00")
STATE_VERSION = 1
SCAN_SCHEMA_VERSION = 1
CACHE_SCHEMA_VERSION = 1
DB_SCHEMA_KEY = "in_vehicle_scan_schema_version"
DB_SCHEMA_VERSION = 1

WINDOW_GAP_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 6.0
REASSEMBLY_TIMEOUT_SECONDS = 2.0
PI_INTERVAL_GAP_SECONDS = 5.0
MAX_PAYLOAD_BYTES = 4095

# The Pi's reviewed fixed helper requests (target address -> request payloads).  The Pi also uses
# tester address F1, so these are recognised by content.  Kept in sync with
# pcm_electrical.py, active_drive.py, radar_alignment.py and bcan_auxiliary.py by the tests.
PI_FIXED_REQUESTS: Mapping[int, frozenset[bytes]] = {
    0x10: frozenset(bytes.fromhex(h) for h in ("22 01 A1", "22 06 DA", "22 06 9F")),
    0xC7: frozenset(bytes.fromhex(h) for h in ("22 31 D0", "22 31 D1", "22 31 D2", "22 31 D3")),
    0x2A: frozenset((bytes.fromhex("22 08 45"),)),
    0x85: frozenset((bytes.fromhex("22 20 01"),)),
}
# The Pi's DTC tooling (lib/dtc_batch.py, tools/dtc_inventory.py) may address any module.
PI_DTC_TOOL_REQUESTS = frozenset(
    bytes.fromhex(h) for h in ("19 02 FF", "19 01 FF", "19 03", "19 0A")
)

_LINE = re.compile(r"\((\d+\.\d+)\)\s+\S+\s+([0-9A-Fa-f]{8})#([0-9A-Fa-f]*)")
_VIN_RUN = re.compile(rb"[A-HJ-NPR-Z0-9]{11}[A-HJ-NPR-Z0-9#*]{6}")


class HarvestError(RuntimeError):
    """A capture, state or output problem that stops one campaign."""


# ---------------------------------------------------------------------------
# small helpers


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_from_epoch(ts: float) -> str:
    text = dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="milliseconds")
    return text.replace("+00:00", "Z")


def iso_seconds(ts: float) -> str:
    text = dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")
    return text.replace("+00:00", "Z")


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def module_for_target(target: int):
    for module in MODULES.values():
        if (module.rxid & 0xFF) == target and ((module.rxid >> 8) & 0xFF) == TESTER:
            return module
    return None


def module_key_for_target(target: int) -> str:
    module = module_for_target(target)
    return module.key if module is not None else f"unregistered_0x{target:02X}"


def is_pi_request(target: int, payload: bytes) -> bool:
    if payload in PI_DTC_TOOL_REQUESTS:
        return True
    return payload in PI_FIXED_REQUESTS.get(target, frozenset())


def mask_vin(payload: bytes) -> tuple[bytes, bool]:
    """Return *payload* with any VIN removed; ``F190`` data is replaced entirely."""

    data = bytearray(payload)
    masked = False
    if len(data) > 3 and data[0] == 0x62 and data[1] == 0xF1 and data[2] == 0x90:
        for index in range(3, len(data)):
            data[index] = 0x2A
        masked = True
    vin = os.environ.get("OBD_VIN", "").strip().upper().encode("ascii", "ignore")
    if len(vin) == 17:
        start = bytes(data).find(vin)
        while start >= 0:
            data[start : start + 17] = b"*" * 17
            masked = True
            start = bytes(data).find(vin, start + 17)
    for match in _VIN_RUN.finditer(bytes(data)):
        run = match.group(0)
        if run.count(b"*") == len(run):
            continue
        letters = sum(1 for byte in run if 0x41 <= byte <= 0x5A)
        digits = sum(1 for byte in run if 0x30 <= byte <= 0x39)
        if letters >= 2 and digits >= 2:
            data[match.start() : match.end()] = b"*" * len(run)
            masked = True
    return bytes(data), masked


def printable(payload: bytes) -> str:
    return "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in payload)


def kwp_dtc_name(raw: bytes) -> str:
    first, second = raw[0], raw[1]
    letter = "PCBU"[(first >> 6) & 0x03]
    return f"{letter}{((first & 0x3F) << 8) | second:04X}"


# ---------------------------------------------------------------------------
# frames, ISO-TP and pairing


@dataclasses.dataclass(frozen=True)
class Frame:
    ts: float
    can_id: int
    data: bytes


def parse_candump_line(line: str) -> Frame | None:
    match = _LINE.match(line.strip())
    if match is None:
        return None
    try:
        data = bytes.fromhex(match.group(3))
    except ValueError:
        return None
    return Frame(float(match.group(1)), int(match.group(2), 16), data)


def iter_chunk_frames(path: Path) -> Iterator[Frame]:
    """Stream diagnostic frames from one completed zstd chunk (``zstd`` + ``grep`` do the work)."""

    env = dict(os.environ, LC_ALL="C")
    try:
        zstd = subprocess.Popen(
            ["zstd", "-dcq", "--", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        raise HarvestError(f"cannot start zstd for {path}: {exc}") from exc
    try:
        grep = subprocess.Popen(
            ["grep", "-F", " 18DA"],
            stdin=zstd.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except OSError as exc:
        try:
            zstd.kill()
        except ProcessLookupError:
            pass
        finally:
            zstd.wait()
        if zstd.stdout is not None:
            zstd.stdout.close()
        if zstd.stderr is not None:
            zstd.stderr.close()
        raise HarvestError(f"cannot start grep for {path}: {exc}") from exc
    assert zstd.stdout is not None and grep.stdout is not None
    zstd.stdout.close()
    try:
        for raw in grep.stdout:
            frame = parse_candump_line(raw.decode("ascii", "replace"))
            if frame is not None:
                yield frame
    finally:
        grep.stdout.close()
        grep_code = grep.wait()
        zstd_err = zstd.stderr.read().decode("utf-8", "replace") if zstd.stderr else ""
        zstd_code = zstd.wait()
        if zstd.stderr:
            zstd.stderr.close()
    if zstd_code != 0:
        raise HarvestError(f"zstd failed on {path.name} (exit {zstd_code}): {zstd_err.strip()[:200]}")
    if grep_code not in (0, 1):
        raise HarvestError(f"grep failed on {path.name} (exit {grep_code})")


@dataclasses.dataclass
class Message:
    ts: float
    direction: str  # "request" (F1 -> target) or "response" (target -> F1/F2)
    target: int
    via: int  # F1, or F2 for the gateway's C-CAN copy
    payload: bytes
    complete: bool = True


class IsoTpAssembler:
    """Reassemble ISO-TP messages per (direction, target, via); flow control is skipped."""

    def __init__(self, state: Mapping[str, Any] | None = None) -> None:
        self.buffers: dict[str, dict[str, Any]] = {}
        if state:
            for key, value in state.items():
                self.buffers[key] = dict(value)

    def state(self) -> dict[str, Any]:
        return {key: dict(value) for key, value in self.buffers.items()}

    def feed(self, frame: Frame) -> list[Message]:
        can_id = frame.can_id & 0x1FFFFFFF
        if (can_id >> 16) != 0x18DA:
            return []
        dest = (can_id >> 8) & 0xFF
        src = can_id & 0xFF
        if src == TESTER and dest not in (TESTER, GATEWAY_COPY):
            direction, target, via = "request", dest, TESTER
        elif dest in (TESTER, GATEWAY_COPY) and src not in (TESTER, GATEWAY_COPY):
            direction, target, via = "response", src, dest
        else:
            return []
        data = frame.data
        if not data:
            return []
        key = f"{direction}:{target:02X}:{via:02X}"
        out: list[Message] = []
        pci = data[0] >> 4
        buffer = self.buffers.get(key)
        if buffer is not None and frame.ts - buffer["last_ts"] > REASSEMBLY_TIMEOUT_SECONDS:
            out.append(self._incomplete(key, buffer, direction, target, via))
            buffer = None
        if pci == 0:
            length = data[0] & 0x0F
            if buffer is not None:
                out.append(self._incomplete(key, buffer, direction, target, via))
            if 0 < length <= len(data) - 1:
                out.append(Message(frame.ts, direction, target, via, bytes(data[1 : 1 + length])))
        elif pci == 1:
            if buffer is not None:
                out.append(self._incomplete(key, buffer, direction, target, via))
            if len(data) >= 2:
                length = ((data[0] & 0x0F) << 8) | data[1]
                if 7 < length <= MAX_PAYLOAD_BYTES:
                    self.buffers[key] = {
                        "first_ts": frame.ts,
                        "last_ts": frame.ts,
                        "length": length,
                        "data": bytes(data[2:8]).hex(),
                        "next": 1,
                    }
        elif pci == 2:
            if buffer is None:
                return out
            sequence = data[0] & 0x0F
            if sequence != buffer["next"]:
                out.append(self._incomplete(key, buffer, direction, target, via))
                return out
            collected = bytes.fromhex(buffer["data"]) + bytes(data[1:])
            if len(collected) >= buffer["length"]:
                self.buffers.pop(key, None)
                out.append(
                    Message(buffer["first_ts"], direction, target, via, collected[: buffer["length"]])
                )
            else:
                buffer["data"] = collected.hex()
                buffer["last_ts"] = frame.ts
                buffer["next"] = (sequence + 1) & 0x0F
        # pci == 3: flow control, not part of any message.
        return out

    def _incomplete(self, key, buffer, direction, target, via) -> Message:
        self.buffers.pop(key, None)
        return Message(
            buffer["first_ts"], direction, target, via, bytes.fromhex(buffer["data"]), complete=False
        )

    def expire(self, now_ts: float) -> list[Message]:
        out = []
        for key in list(self.buffers):
            buffer = self.buffers[key]
            if now_ts - buffer["last_ts"] > REASSEMBLY_TIMEOUT_SECONDS:
                direction, target, via = key.split(":")
                out.append(self._incomplete(key, buffer, direction, int(target, 16), int(via, 16)))
        return out


class ExchangePairer:
    """Pair F1 requests with their target's reply on one bus."""

    def __init__(self, bus: str, state: Mapping[str, Any] | None = None) -> None:
        self.bus = bus
        self.pending: dict[str, dict[str, Any]] = {}
        self.pi_intervals: list[list[float]] = []
        self.orphans = 0
        self.pi_count = 0
        if state:
            self.pending = {key: dict(value) for key, value in state.get("pending", {}).items()}
            self.pi_intervals = [list(item) for item in state.get("pi_open_interval", [])]

    def state(self) -> dict[str, Any]:
        return {
            "pending": {key: dict(value) for key, value in self.pending.items()},
            # Only the last interval stays open across a chunk boundary.
            "pi_open_interval": self.pi_intervals[-1:],
        }

    def _note_pi(self, ts: float) -> None:
        self.pi_count += 1
        if self.pi_intervals and ts - self.pi_intervals[-1][1] <= PI_INTERVAL_GAP_SECONDS:
            self.pi_intervals[-1][1] = ts
            self.pi_intervals[-1][2] += 1
        else:
            self.pi_intervals.append([ts, ts, 1])

    def take_closed_pi_intervals(self) -> list[list[float]]:
        """Return all Pi intervals, keeping only the last as open state for the next chunk."""
        closed = [list(item) for item in self.pi_intervals]
        self.pi_intervals = self.pi_intervals[-1:]
        return closed

    def _finish(self, entry: Mapping[str, Any], *, response: Message | None) -> dict[str, Any] | None:
        request = bytes.fromhex(entry["payload"])
        target = int(entry["target"], 16)
        if entry["pi"]:
            return None
        record: dict[str, Any] = {
            "t": entry["ts"],
            "bus": self.bus,
            "target": f"{target:02X}",
            "request_hex": request.hex().upper(),
            "response_pending_78": entry.get("pending_78", 0),
        }
        if response is None:
            record["outcome"] = "no_response"
            record["response_hex"] = None
            record["response_t"] = None
            record["via"] = None
        else:
            payload, masked = mask_vin(response.payload)
            record["response_hex"] = payload.hex().upper()
            record["response_t"] = response.ts
            record["via"] = f"{response.via:02X}"
            if masked:
                record["vin_masked"] = True
            if not response.complete:
                record["outcome"] = "incomplete"
            elif payload[:1] == b"\x7F":
                record["outcome"] = "negative"
                record["nrc"] = f"{payload[2]:02X}" if len(payload) >= 3 else None
            else:
                record["outcome"] = "positive"
        return record

    def feed(self, message: Message) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        key = f"{message.target:02X}"
        out.extend(self.expire(message.ts, only=key))
        if message.direction == "request":
            if not message.complete:
                return out
            previous = self.pending.pop(key, None)
            if previous is not None:
                finished = self._finish(previous, response=None)
                if finished:
                    out.append(finished)
            pi = is_pi_request(message.target, message.payload)
            if pi:
                self._note_pi(message.ts)
            self.pending[key] = {
                "ts": message.ts,
                "target": key,
                "payload": message.payload.hex(),
                "pi": pi,
                "pending_78": 0,
                "last_ts": message.ts,
            }
            return out
        entry = self.pending.get(key)
        if entry is None:
            self.orphans += 1
            return out
        payload = message.payload
        if message.complete and len(payload) >= 3 and payload[0] == 0x7F and payload[2] == 0x78:
            entry["pending_78"] = entry.get("pending_78", 0) + 1
            entry["last_ts"] = message.ts
            return out
        self.pending.pop(key, None)
        finished = self._finish(entry, response=message)
        if finished:
            out.append(finished)
        return out

    def expire(self, now_ts: float, *, only: str | None = None) -> list[dict[str, Any]]:
        out = []
        keys = [only] if only is not None else list(self.pending)
        for key in keys:
            entry = self.pending.get(key)
            if entry is None:
                continue
            if now_ts - entry["last_ts"] > REQUEST_TIMEOUT_SECONDS:
                self.pending.pop(key, None)
                finished = self._finish(entry, response=None)
                if finished:
                    out.append(finished)
        return out


def process_frames(
    bus: str,
    frames: Iterable[Frame],
    carry: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[list[float]], dict[str, Any], dict[str, Any]]:
    """Return (non-Pi exchanges, Pi activity intervals, carry state, stats) for one chunk."""

    carry = carry or {}
    assembler = IsoTpAssembler(carry.get("isotp"))
    pairer = ExchangePairer(bus, carry.get("pairer"))
    exchanges: list[dict[str, Any]] = []
    frames_seen = 0
    first_ts = None
    last_ts = carry.get("last_ts")
    for frame in frames:
        frames_seen += 1
        if first_ts is None:
            first_ts = frame.ts
        last_ts = frame.ts
        for message in assembler.feed(frame):
            exchanges.extend(pairer.feed(message))
    if last_ts is not None:
        for message in assembler.expire(last_ts):
            exchanges.extend(pairer.feed(message))
        exchanges.extend(pairer.expire(last_ts))
    exchanges.sort(key=lambda item: item["t"])
    intervals = pairer.take_closed_pi_intervals()
    state = {"isotp": assembler.state(), "pairer": pairer.state(), "last_ts": last_ts}
    stats = {
        "diagnostic_frames": frames_seen,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "orphan_responses": pairer.orphans,
        "non_pi_exchanges": len(exchanges),
        "pi_requests": pairer.pi_count,
    }
    return exchanges, intervals, state, stats


# ---------------------------------------------------------------------------
# campaign discovery


def completed_chunks(bus_dir: Path) -> list[dict[str, Any]]:
    """Chunks the recorder's manifest lists as complete, whose file size matches the manifest."""

    manifest = bus_dir / "manifest.jsonl"
    chunks = []
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "chunk" or entry.get("complete") is not True:
            continue
        full = (entry.get("streams") or {}).get("full") or {}
        if full.get("complete") is not True or not full.get("path"):
            continue
        path = bus_dir / Path(str(full["path"])).name
        if path.name.endswith(".partial") or not path.name.endswith(".candump.zst"):
            continue
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            continue
        if isinstance(full.get("compressed_bytes"), int) and size != full["compressed_bytes"]:
            continue
        chunks.append(
            {
                "sequence": int(entry.get("sequence", len(chunks))),
                "name": path.name,
                "path": path,
                "size": size,
                "sha256": full.get("sha256"),
                "first_frame_timestamp": entry.get("first_frame_timestamp"),
                "last_frame_timestamp": entry.get("last_frame_timestamp"),
            }
        )
    chunks.sort(key=lambda item: item["sequence"])
    return chunks


def bus_capture_ended(bus_dir: Path) -> bool:
    try:
        lines = (bus_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        # The drive recorder may append a re-admitted secondary segment after a
        # route loss; only the latest start/end marker says whether it ended.
        if isinstance(entry, dict) and entry.get("type") == "capture_start":
            return False
        if isinstance(entry, dict) and entry.get("type") == "capture_end":
            return True
    return False


def discover_campaigns(root: Path, patterns: Sequence[str] | None) -> list[Path]:
    if not root.is_dir():
        raise HarvestError(f"capture root {root} is not a directory (is the drive mounted?)")
    campaigns = sorted(path for path in root.iterdir() if path.is_dir() and path.name.startswith("broker-drive-"))
    if patterns:
        campaigns = [path for path in campaigns if any(fnmatch.fnmatch(path.name, p) for p in patterns)]
    return campaigns


# ---------------------------------------------------------------------------
# campaign state and windows


def _state_path(out_dir: Path, campaign: str) -> Path:
    return out_dir / "state" / f"{campaign}.json"


def load_state(out_dir: Path, campaign: str) -> dict[str, Any]:
    path = _state_path(out_dir, campaign)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = None
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        state = {"version": STATE_VERSION, "campaign": campaign, "buses": {}, "scans": {}, "complete": False}
    return state


def harvest_campaign(
    campaign_dir: Path,
    out_dir: Path,
    *,
    max_chunks: int | None = None,
    frame_reader=iter_chunk_frames,
    log=lambda text: None,
) -> dict[str, Any]:
    """Process new completed chunks of one campaign; return the campaign summary."""

    campaign = campaign_dir.name
    state = load_state(out_dir, campaign)
    exchange_dir = out_dir / "exchanges" / campaign
    processed_now = 0
    ended = {}
    for bus in BUSES:
        bus_dir = campaign_dir / bus
        bus_state = state["buses"].setdefault(bus, {"chunks": {}, "carry": {}, "pi_intervals": []})
        ended[bus] = bus_capture_ended(bus_dir) if bus_dir.is_dir() else True
        if not bus_dir.is_dir():
            continue
        for chunk in completed_chunks(bus_dir):
            if chunk["name"] in bus_state["chunks"]:
                continue
            if max_chunks is not None and processed_now >= max_chunks:
                break
            try:
                exchanges, intervals, carry, stats = process_frames(
                    bus, frame_reader(chunk["path"]), bus_state.get("carry")
                )
                error = None
            except HarvestError as exc:
                exchanges, intervals, carry, stats = [], [], {}, {}
                error = str(exc)
            target = exchange_dir / bus / (chunk["name"].split(".")[0] + ".json")
            if exchanges:
                write_json_atomic(target, {"chunk": chunk["name"], "exchanges": exchanges})
            else:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
            # The last Pi interval stays open in the carry; replace any earlier copy of it.
            merged = bus_state["pi_intervals"]
            for interval in intervals:
                if merged and interval[0] == merged[-1][0]:
                    merged[-1] = interval
                else:
                    merged.append(interval)
            bus_state["carry"] = carry
            bus_state["chunks"][chunk["name"]] = {
                "sequence": chunk["sequence"],
                "size": chunk["size"],
                "sha256": chunk["sha256"],
                "processed_at": utc_now(),
                "error": error,
                **{key: stats.get(key) for key in ("diagnostic_frames", "non_pi_exchanges", "pi_requests", "first_ts", "last_ts")},
            }
            # How far this bus has been read: the manifest's last frame time covers chunks with
            # no diagnostic traffic at all.
            candidates = [
                value
                for value in (stats.get("last_ts"), chunk.get("last_frame_timestamp"), bus_state.get("last_ts"))
                if isinstance(value, (int, float))
            ]
            bus_state["last_ts"] = max(candidates) if candidates else None
            processed_now += 1
            write_json_atomic(_state_path(out_dir, campaign), state)
            log(
                f"{campaign} {bus} {chunk['name']}: {stats.get('diagnostic_frames', 0)} diag frames, "
                f"{len(exchanges)} non-Pi exchanges" + (f", ERROR {error}" if error else "")
            )
    all_ended = all(ended.values())
    all_done = all_ended and all(
        set(state["buses"].get(bus, {}).get("chunks", {}))
        >= {chunk["name"] for chunk in completed_chunks(campaign_dir / bus)}
        for bus in BUSES
        if (campaign_dir / bus).is_dir()
    )
    state["complete"] = all_done
    exchanges = load_exchanges(exchange_dir)
    pi_intervals = {bus: state["buses"].get(bus, {}).get("pi_intervals", []) for bus in BUSES}
    if all_done:
        windows = build_windows(exchanges, closed_before=None)
    else:
        # A window is final only once every bus still recording has been read well past its end.
        horizons = []
        for bus in BUSES:
            bus_dir = campaign_dir / bus
            if not bus_dir.is_dir():
                continue
            bus_state = state["buses"].get(bus, {})
            finished = ended[bus] and set(bus_state.get("chunks", {})) >= {
                chunk["name"] for chunk in completed_chunks(bus_dir)
            }
            if finished:
                continue
            horizons.append(bus_state.get("last_ts"))
        if any(value is None for value in horizons):
            windows = []
        else:
            windows = build_windows(exchanges, closed_before=min(horizons) if horizons else None)
    new_scans = []
    for window in windows:
        report = build_scan_report(campaign, window, pi_intervals)
        scan_id = report["scan_id"]
        if scan_id in state["scans"]:
            continue
        path = out_dir / "scans" / f"{scan_id}.json"
        write_json_atomic(path, report)
        state["scans"][scan_id] = {"classification": report["classification"], "path": str(path)}
        new_scans.append(report)
    write_json_atomic(_state_path(out_dir, campaign), state)
    return {
        "campaign": campaign,
        "chunks_processed": processed_now,
        "complete": all_done,
        "new_scans": new_scans,
    }


def load_exchanges(exchange_dir: Path) -> list[dict[str, Any]]:
    exchanges = []
    if not exchange_dir.is_dir():
        return exchanges
    for path in sorted(exchange_dir.glob("*/*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        exchanges.extend(item for item in payload.get("exchanges", []) if isinstance(item, dict))
    return dedupe_gateway_copies(exchanges)


def _home_bus(target: int, buses_seen: Iterable[str]) -> str:
    module = module_for_target(target)
    if module is not None:
        return module.bus
    seen = set(buses_seen)
    if "can-ch" in seen:
        return "can-ch"
    return sorted(seen)[0] if seen else "c-can"


def dedupe_gateway_copies(exchanges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge the C-CAN copy of a gateway-forwarded CAN-CH exchange into one record."""

    exchanges = sorted(exchanges, key=lambda item: (item["t"], item["bus"]))
    kept: list[dict[str, Any]] = []
    recent: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in exchanges:
        key = (item["target"], item["request_hex"])
        twins = recent.setdefault(key, [])
        twin = next(
            (
                other
                for other in twins
                if other["bus"] != item["bus"] and abs(other["t"] - item["t"]) < 0.1
            ),
            None,
        )
        if twin is None:
            item = dict(item)
            item["buses_seen"] = [item["bus"]]
            twins.append(item)
            del twins[:-4]
            kept.append(item)
            continue
        twin["buses_seen"] = sorted(set(twin["buses_seen"]) | {item["bus"]})
        home = _home_bus(int(item["target"], 16), twin["buses_seen"])
        better = (item["outcome"] != "no_response" and twin["outcome"] == "no_response") or (
            item["bus"] == home and (item["outcome"] != "no_response") == (twin["outcome"] != "no_response")
        )
        if better:
            seen = twin["buses_seen"]
            twin.clear()
            twin.update(item)
            twin["buses_seen"] = seen
    return kept


def build_windows(exchanges: list[dict[str, Any]], *, closed_before: float | None) -> list[list[dict[str, Any]]]:
    """Group exchanges by time gap; drop a window that may still be open."""

    windows: list[list[dict[str, Any]]] = []
    for item in sorted(exchanges, key=lambda x: x["t"]):
        if windows and item["t"] - windows[-1][-1]["t"] <= WINDOW_GAP_SECONDS:
            windows[-1].append(item)
        else:
            windows.append([item])
    if closed_before is not None:
        windows = [w for w in windows if closed_before - w[-1]["t"] > WINDOW_GAP_SECONDS]
    return windows


def _pi_overlap(pi_intervals: Mapping[str, list[list[float]]], start: float, end: float) -> dict[str, Any]:
    overlap_seconds = 0.0
    requests = 0
    for intervals in pi_intervals.values():
        for first, last, count in intervals:
            if last < start or first > end:
                continue
            overlap_seconds += max(0.0, min(last, end) - max(first, start))
            requests += int(count)
    last_before = max(
        (last for intervals in pi_intervals.values() for first, last, _ in intervals if last < start),
        default=None,
    )
    first_after = min(
        (first for intervals in pi_intervals.values() for first, last, _ in intervals if first > end),
        default=None,
    )
    return {
        "pi_quiet": requests == 0,
        "pi_requests_overlapping": requests,
        "pi_overlap_seconds": round(overlap_seconds, 3),
        "pi_last_request_before": iso_from_epoch(last_before) if last_before else None,
        "pi_first_request_after": iso_from_epoch(first_after) if first_after else None,
    }


def _dtc_result(exchange: Mapping[str, Any]) -> dict[str, Any]:
    request = bytes.fromhex(exchange["request_hex"])
    result: dict[str, Any] = {
        "observed_at": iso_from_epoch(exchange.get("response_t") or exchange["t"]),
        "request_hex": request.hex(" ").upper(),
        "response_hex": bytes.fromhex(exchange["response_hex"]).hex(" ").upper()
        if exchange.get("response_hex")
        else None,
        "bus": exchange["bus"],
    }
    outcome = exchange["outcome"]
    if outcome != "positive":
        result.update(outcome={"negative": "negative_response"}.get(outcome, outcome), nrc=exchange.get("nrc"))
        return result
    response = bytes.fromhex(exchange["response_hex"])
    if request[:1] == b"\x19":
        try:
            mask, records = parse_dtc_list_response(response)
        except DtcParseError as exc:
            result.update(outcome="unparsed", detail=str(exc))
            return result
        result.update(
            outcome="success",
            protocol="uds",
            status_availability_mask=f"{mask:02X}",
            dtcs=[{"raw_dtc": r.raw_dtc, "fca_display": r.fca_display, "status": f"{r.status:02X}"} for r in records],
        )
        return result
    # KWP2000 readDiagnosticTroubleCodesByStatus: 58 <count> (<dtc hi> <dtc lo> <status>)*
    if len(response) < 2 or response[0] != 0x58 or len(response) != 2 + 3 * response[1]:
        result.update(outcome="unparsed", detail="KWP 58 response length does not match its count")
        return result
    records = []
    for offset in range(2, len(response), 3):
        raw = response[offset : offset + 2]
        records.append(
            {"raw_dtc": raw.hex().upper(), "fca_display": kwp_dtc_name(raw), "status": f"{response[offset + 2]:02X}"}
        )
    result.update(outcome="success", protocol="kwp2000", status_availability_mask=None, dtcs=records)
    return result


def build_scan_report(
    campaign: str,
    window: list[dict[str, Any]],
    pi_intervals: Mapping[str, list[list[float]]],
) -> dict[str, Any]:
    start = window[0]["t"]
    end = max(item.get("response_t") or item["t"] for item in window)
    dtc_requests = [item for item in window if bytes.fromhex(item["request_hex"]) == VAN_DTC_REQUEST]
    classification = "in_vehicle_health_check" if dtc_requests else "other_f1_traffic"
    modules: dict[str, dict[str, Any]] = {}
    for item in window:
        target = int(item["target"], 16)
        module = module_for_target(target)
        key = module_key_for_target(target)
        entry = modules.setdefault(
            key,
            {
                "module_key": key,
                "module_name": module.name if module is not None else f"Unregistered module 0x{target:02X}",
                "target_address": f"0x{target:02X}",
                "logical_bus": _home_bus(target, item.get("buses_seen", [item["bus"]])),
                "registered": module is not None,
                "buses_seen": set(),
                "requests": 0,
                "answered": False,
                "dtc_reads": [],
                "dids": {},
                "other_reads": {},
            },
        )
        entry["buses_seen"].update(item.get("buses_seen", [item["bus"]]))
        entry["requests"] += 1
        if item["outcome"] in ("positive", "negative"):
            entry["answered"] = True
        request = bytes.fromhex(item["request_hex"])
        if request == VAN_DTC_REQUEST or request == KWP_DTC_REQUEST:
            entry["dtc_reads"].append(_dtc_result(item))
            continue
        response = bytes.fromhex(item["response_hex"]) if item.get("response_hex") else b""
        record = {
            "observed_at": iso_from_epoch(item.get("response_t") or item["t"]),
            "outcome": item["outcome"],
            "nrc": item.get("nrc"),
            "response_hex": response.hex(" ").upper() if response else None,
        }
        if request[:1] == b"\x22" and len(request) == 3:
            did = request[1:3].hex().upper()
            if item["outcome"] == "positive" and response[:3] == b"\x62" + request[1:3]:
                record["data_hex"] = response[3:].hex(" ").upper()
                record["ascii"] = printable(response[3:])
            if item.get("vin_masked"):
                record["vin_masked"] = True
            entry["dids"][did] = record  # the final read of each DID wins
        else:
            entry["other_reads"][request.hex(" ").upper()] = record
    module_rows = []
    for entry in modules.values():
        reads = entry.pop("dtc_reads")
        successes = [read for read in reads if read["outcome"] == "success"]
        chosen = successes[-1] if successes else (reads[-1] if reads else None)
        if chosen is not None:
            chosen = dict(chosen)
            chosen["attempts"] = len(reads)
            chosen["successful_passes"] = len(successes)
            chosen["consistent"] = len({json.dumps(read.get("dtcs"), sort_keys=True) for read in successes}) <= 1
        entry["dtc"] = chosen
        entry["buses_seen"] = sorted(entry["buses_seen"])
        module_rows.append(entry)
    module_rows.sort(key=lambda row: (not row["registered"], row["module_key"]))
    buses = sorted({bus for item in window for bus in item.get("buses_seen", [item["bus"]])})
    scan_id = f"{campaign}--{dt.datetime.fromtimestamp(start, dt.timezone.utc):%Y%m%dT%H%M%SZ}"
    return {
        "schema_version": SCAN_SCHEMA_VERSION,
        "type": "in_vehicle_scan_report",
        "scan_id": scan_id,
        "campaign": campaign,
        "classification": classification,
        "provenance": PROVENANCE,
        "tester_source": "F1",
        "dtc_request": VAN_DTC_REQUEST.hex(" ").upper(),
        "started_at": iso_from_epoch(start),
        "completed_at": iso_from_epoch(end),
        "buses": buses,
        "exchange_count": len(window),
        "modules_queried": len(module_rows),
        "modules_answered": sum(1 for row in module_rows if row["answered"]),
        **_pi_overlap(pi_intervals, start, end),
        "generated_at": utc_now(),
        "tool": "projects/vehicle_data/van_scan_harvest.py",
        "note": (
            "Passive reconstruction from the synchronized drive recorder's completed chunks; "
            "no CAN frame was sent. VINs are masked."
        ),
        "modules": module_rows,
    }


# ---------------------------------------------------------------------------
# DTC-history import (separate tables in the same SQLite file)


from projects.vehicle_data.van_scan_harvest_store import (  # noqa: E402
    InVehicleScanStore,
)


# ---------------------------------------------------------------------------
# CLI compatibility exports


from projects.vehicle_data import van_scan_harvest_cli as _cli  # noqa: E402


def run(
    *,
    capture_root: Path,
    out_dir: Path,
    db_path: Path | None,
    cache_path: Path | None,
    patterns: Sequence[str] | None,
    max_chunks: int | None,
    reimport: bool = False,
    log=print,
) -> dict[str, Any]:
    return _cli.run(
        capture_root=capture_root,
        out_dir=out_dir,
        db_path=db_path,
        cache_path=cache_path,
        patterns=patterns,
        max_chunks=max_chunks,
        reimport=reimport,
        log=log,
        discover_campaigns_fn=discover_campaigns,
        load_state_fn=load_state,
        harvest_campaign_fn=harvest_campaign,
        scan_store_factory=InVehicleScanStore,
        write_json_atomic_fn=write_json_atomic,
    )


def build_parser() -> argparse.ArgumentParser:
    return _cli.build_parser(
        default_capture_root=DEFAULT_CAPTURE_ROOT,
        default_out_dir=DEFAULT_OUT_DIR,
        default_db=DEFAULT_DB,
        default_cache=DEFAULT_CACHE,
    )


def main(argv: Sequence[str] | None = None) -> int:
    return _cli.main(
        argv,
        parser_factory=build_parser,
        run_fn=run,
        harvest_error=HarvestError,
    )


if __name__ == "__main__":
    raise SystemExit(main())
