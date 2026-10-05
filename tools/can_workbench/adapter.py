"""Explicit contracts around the repository's saved-evidence helpers."""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path

from lib.candump_io import parse_candump_line, zstd_stream
from lib.signal_fields import SignalField
from tools.can_capture_summary import parse_frame, summarize_lines
from tools.can_capture_compare import compare_summaries

MAX_JSON = 16 * 1024 * 1024
MAX_LINE = 65536
MAX_STREAMS = 4096
TIERS = {"exploratory_candidate", "operational_proxy", "verified_decode"}


def sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def identity(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"not a regular input file: {path}")
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256(path)}


def load_json(path):
    with Path(path).open("rb") as source:
        raw = source.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise ValueError(f"JSON input exceeds {MAX_JSON} bytes: {path}")
    return json.loads(raw, parse_constant=lambda s: reject(f"non-finite JSON: {s}"))


def reject(message):
    raise ValueError(message)


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        reject(f"{label} must be a finite number")
    return value


def stamp(value):
    # Strings are required so JSON float conversion cannot destroy source precision.
    if not isinstance(value, str) or len(value) > 40:
        reject("timestamps must be decimal strings (at most 40 characters)")
    try:
        result = Decimal(value)
    except InvalidOperation:
        reject(f"invalid timestamp: {value!r}")
    if not result.is_finite() or abs(result) > Decimal("1e12") or result.as_tuple().exponent < -12:
        reject("timestamp outside supported range / picosecond precision")
    return result


def geometry(field):
    return SignalField(**{k: field[k] for k in
                         ("dbc_start_bit", "length_bits", "byte_order", "signed")})


def validate_field(field):
    required = {"name", "bus", "channel", "can_id", "id_bits", "dlc", "field",
                "scale", "offset", "unit", "evidence_tier", "provenance"}
    if not isinstance(field, dict) or not required <= field.keys():
        reject(f"field requires {sorted(required)}")
    for key in ("name", "bus", "channel", "unit", "provenance"):
        if not isinstance(field[key], str) or not field[key]:
            reject(f"field {key} must be nonempty text")
    if field["evidence_tier"] not in TIERS:
        reject("unknown evidence tier")
    if type(field["id_bits"]) is not int or field["id_bits"] not in (11, 29):
        reject("id_bits must be 11 or 29")
    if not isinstance(field["can_id"], str):
        reject("can_id must be a hexadecimal string")
    can_id = int(field["can_id"], 16)
    if not 0 <= can_id < 2 ** field["id_bits"]:
        reject("can_id outside identifier namespace")
    if type(field["dlc"]) is not int or not 0 <= field["dlc"] <= 64:
        reject("dlc must be an integer from 0 to 64")
    if geometry(field["field"]).required_payload_bytes > field["dlc"]:
        reject("field exceeds selected DLC")
    number(field["scale"], "scale")
    number(field["offset"], "offset")
    return field


@contextmanager
def lines(path):
    def bounded(source):
        while raw := source.readline(MAX_LINE + 1):
            if len(raw) > MAX_LINE:
                reject(f"capture line exceeds {MAX_LINE} bytes: {path}")
            yield raw.decode("utf-8", errors="replace")
    if Path(path).suffix.lower() == ".zst":
        with zstd_stream(Path(path), error_type=ValueError) as process:
            yield bounded(process.stdout)
    else:
        with Path(path).open("rb") as source:
            yield bounded(source)


class Series:
    """Bounded deterministic decimation; segment IDs prevent bridging hidden gaps.

    Every observation updates counts/extrema. Only plotting points are decimated.
    Gaps and isolated segments may lose points, but can never acquire a false line.
    """
    def __init__(self, limit, gap):
        self.limit, self.gap = limit, Decimal(str(gap))
        self.points = []
        self.count = self.segment = self.gaps = 0
        self.stride = 1
        self.previous = self.first = self.last_point = None
        self.minimum = self.maximum = None

    def add(self, timestamp, value):
        number(value, "plot value")
        if self.previous is not None:
            if timestamp < self.previous:
                reject("timestamps decrease; split or explicitly sort the source before reporting")
            if timestamp - self.previous > self.gap:
                self.segment += 1
                self.gaps += 1
        if self.first is None:
            self.first = timestamp
        self.previous = timestamp
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        point = [self.count, str(timestamp), value, self.segment]
        self.last_point = point
        if self.count % self.stride == 0:
            self.points.append(point)
        self.count += 1
        if len(self.points) > self.limit - 1:
            self.stride *= 2
            self.points = [p for p in self.points if p[0] % self.stride == 0]

    def report(self, origin):
        points = list(self.points)
        if self.last_point is not None and (not points or points[-1] != self.last_point):
            points.append(self.last_point)
        return {
            "count": self.count, "minimum": self.minimum, "maximum": self.maximum,
            "status": "missing" if not self.count else "insufficient" if self.count < 2 else
                      "constant" if self.minimum == self.maximum else "varying",
            "first_timestamp": str(self.first) if self.first is not None else None,
            "last_timestamp": str(self.previous) if self.previous is not None else None,
            "gap_count": self.gaps, "gap_threshold_s": str(self.gap),
            "decimation_stride": self.stride,
            "downsampling": "every stride-th observation plus last; extrema are full-range; no gap bridging",
            "points": [{"timestamp": p[1], "seconds": float(Decimal(p[1]) - origin),
                        "value": p[2], "segment": p[3]} for p in points],
        }


def capture_report(path, spec, fields, *, window, max_points, gap):
    selected = [f for f in fields if spec["channels"].get(f["channel"]) == f["bus"]]
    series = {f["name"]: Series(max_points, gap) for f in selected}
    decoders = [(f, geometry(f["field"])) for f in selected]
    first = last = None
    streams = set()
    scanned = malformed = blanks = outside = unterminated = 0

    def observed(source):
        nonlocal first, last, scanned, malformed, blanks, outside, unterminated
        for line in source:
            scanned += 1
            if not line.endswith("\n"):
                unterminated += 1
            frame = parse_frame(line)
            if frame is None:
                if line.strip():
                    malformed += 1
                else:
                    blanks += 1
                # Match the existing summary's malformed/blank accounting.
                yield line
                continue
            timestamp = stamp(parse_candump_line(line).timestamp)
            if last is not None and timestamp < last:
                reject(f"{path}:{scanned}: timestamps decrease")
            if first is None:
                first = timestamp
            last = timestamp
            if frame.interface not in spec["channels"]:
                reject(f"{path}: no explicit bus mapping for channel {frame.interface!r}")
            if window and not window[0] <= timestamp - first <= window[1]:
                outside += 1
                continue
            streams.add((frame.interface, frame.id_bits, frame.can_id))
            if len(streams) > MAX_STREAMS:
                reject(f"capture exceeds {MAX_STREAMS} distinct streams")
            for field, decoder in decoders:
                if (frame.interface, frame.id_bits, frame.can_id, frame.dlc) == (
                    field["channel"], field["id_bits"], int(field["can_id"], 16), field["dlc"]
                ):
                    series[field["name"]].add(timestamp, decoder.extract(frame.payload) *
                                               field["scale"] + field["offset"])
            yield line

    with lines(path) as source:
        summary = summarize_lines(observed(source), source=str(path))
    return {
        **spec, "summary": summary,
        "coverage": {"first_timestamp": str(first) if first is not None else None,
                     "last_timestamp": str(last) if last is not None else None,
                     "scanned_lines": scanned, "malformed_lines": malformed,
                     "blank_lines": blanks, "unterminated_lines": unterminated,
                     "frames_outside_window": outside},
        "series": {name: data.report(first or Decimal(0)) for name, data in series.items()},
    }


def compare_captures(baseline, current):
    # Existing comparator uses interface identity. Compare only channels explicitly
    # mapped to the same bus in both captures; never infer bus from a CAN ID.
    shared = {c for c, b in baseline["channels"].items() if current["channels"].get(c) == b}
    def scoped(capture):
        summary = dict(capture["summary"])
        summary["ids"] = [r for r in summary["ids"] if r["interface"] in shared]
        summary["interfaces"] = {k: v for k, v in summary["interfaces"].items() if k in shared}
        return summary
    return {"baseline": baseline["name"], "current": current["name"],
            "matched_channels": sorted(shared),
            "excluded_channels": sorted((baseline["channels"].keys() | current["channels"].keys()) - shared),
            "result": compare_summaries(scoped(baseline), scoped(current)) if shared else None}


def import_analysis(path):
    """Pin correlation schema 1; retain the complete report without promoting it."""
    report = load_json(path)
    if not isinstance(report, dict) or type(report.get("schema_version")) is not int or report["schema_version"] != 1:
        reject("unsupported correlation report schema (expected can_timeseries_correlate v1)")
    if report.get("classification") != "candidate_only" or report.get("offline_only") is not True:
        reject("expected offline can_timeseries_correlate candidate report")
    for key in ("analysis", "reference", "capture", "ranking"):
        if not isinstance(report.get(key), dict):
            reject(f"correlation report lacks {key}")
    rows = report["ranking"].get("candidates")
    if not isinstance(rows, list) or len(rows) > 1000:
        reject("correlation candidates must be a list of at most 1000 rows")
    for row in rows:
        if not isinstance(row, dict) or not {"channel", "can_id", "id_bits", "dlc", "field"} <= row.keys():
            reject("candidate lacks exact stream/field identity")
        if not isinstance(row["channel"], str) or not row["channel"]:
            reject("candidate channel must be nonempty text")
        if type(row["id_bits"]) is not int or row["id_bits"] not in (11, 29):
            reject("candidate id_bits must be 11 or 29")
        if type(row["can_id"]) is not int or not 0 <= row["can_id"] < 2 ** row["id_bits"]:
            reject("candidate can_id outside identifier namespace")
        if type(row["dlc"]) is not int or not 0 <= row["dlc"] <= 64:
            reject("candidate DLC outside 0..64")
        if not isinstance(row["field"], dict) or not isinstance(row["field"].get("kind"), str):
            reject("candidate requires a field definition")
        if "dbc_start_bit" in row["field"] and geometry(row["field"]).required_payload_bytes > row["dlc"]:
            reject("candidate geometry exceeds DLC")
        if row.get("evidence_tier", "exploratory_candidate") != "exploratory_candidate":
            reject("correlation report cannot promote evidence tiers")
    return {"format": "can_timeseries_correlate", "schema_version": 1,
            "evidence_tier": "exploratory_candidate", "telemetry_promotion_allowed": False,
            "source": identity(path), "report": report}
