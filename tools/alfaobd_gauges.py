#!/usr/bin/env python3
"""Inventory a concatenated AlfaOBD ``Gauges_Data`` archive offline.

AlfaOBD appends many independently headed CSV recordings to one file.  A profile marker
(``Recording data for ...``) is not repeated before every recording, so a recording may
inherit the most recent marker.  This tool preserves that distinction and emits three
machine-readable reports:

* ``inventory.json``: source provenance, archive totals, every section, and metric stats;
* ``sections.csv``: one row per recording; and
* ``metrics.csv``: one row per selected-profile/metric namespace.

Gauge labels identify the profile selected in AlfaOBD, not confirmed ECU hardware.  The
archive contains labels and rendered values but no DID identifiers; mapping labels to DIDs
still requires time-alignment with the matching AlfaOBD debug trace or a controlled capture.

This tool only reads a saved file.  It never accesses ADB, SocketCAN, or system services.
Default output stays under ``tmp/inventories/alfaobd_gauges/``.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Iterable, TextIO


REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = REPO / "tmp" / "inventories" / "alfaobd_gauges"
PROFILE_PREFIX = "Recording data for "
DATE_PREFIX = "Date (YY/MM/DD):"
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}(?:\.\d+)?$")
SEPARATOR_RE = re.compile(r"^_+$")
UNKNOWN_PROFILE = "<unknown: no preceding profile marker>"


@dataclass
class MetricStats:
    numeric_count: int = 0
    missing_count: int = 0
    nonnumeric_count: int = 0
    minimum: float | None = None
    maximum: float | None = None

    def add(self, value: str) -> None:
        cleaned = value.strip()
        if not cleaned or cleaned.upper() == "NA":
            self.missing_count += 1
            return
        try:
            number = float(cleaned)
        except ValueError:
            self.nonnumeric_count += 1
            return
        if not math.isfinite(number):
            self.nonnumeric_count += 1
            return
        self.numeric_count += 1
        self.minimum = number if self.minimum is None else min(self.minimum, number)
        self.maximum = number if self.maximum is None else max(self.maximum, number)

    def merge(self, other: "MetricStats") -> None:
        self.numeric_count += other.numeric_count
        self.missing_count += other.missing_count
        self.nonnumeric_count += other.nonnumeric_count
        if other.minimum is not None:
            self.minimum = (
                other.minimum if self.minimum is None else min(self.minimum, other.minimum)
            )
        if other.maximum is not None:
            self.maximum = (
                other.maximum if self.maximum is None else max(self.maximum, other.maximum)
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "numeric_count": self.numeric_count,
            "missing_count": self.missing_count,
            "nonnumeric_count": self.nonnumeric_count,
            "minimum": self.minimum,
            "maximum": self.maximum,
        }


@dataclass
class Section:
    index: int
    profile: str
    profile_source: str
    profile_marker: str
    date_raw: str
    date: str | None
    start_line: int
    header_line: int | None = None
    end_line: int | None = None
    metrics: list[str] = field(default_factory=list)
    metric_stats: dict[str, MetricStats] = field(default_factory=dict)
    sample_rows: int = 0
    valid_rows: int = 0
    short_rows: int = 0
    long_rows: int = 0
    first_time: str | None = None
    last_time: str | None = None

    def add_header(self, fields: list[str], line_number: int) -> None:
        self.header_line = line_number
        self.metrics = fields[1:]
        for metric in self.metrics:
            self.metric_stats.setdefault(metric, MetricStats())

    def add_sample(self, fields: list[str]) -> None:
        self.sample_rows += 1
        timestamp = fields[0]
        if self.first_time is None:
            self.first_time = timestamp
        self.last_time = timestamp

        expected = 1 + len(self.metrics)
        if len(fields) < expected:
            self.short_rows += 1
            return
        if len(fields) > expected:
            self.long_rows += 1
            return

        self.valid_rows += 1
        for metric, value in zip(self.metrics, fields[1:]):
            self.metric_stats[metric].add(value)

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "profile": self.profile,
            "profile_source": self.profile_source,
            "profile_marker": self.profile_marker,
            "date_raw": self.date_raw,
            "date": self.date,
            "start_line": self.start_line,
            "header_line": self.header_line,
            "end_line": self.end_line,
            "first_time": self.first_time,
            "last_time": self.last_time,
            "sample_rows": self.sample_rows,
            "valid_rows": self.valid_rows,
            "short_rows": self.short_rows,
            "long_rows": self.long_rows,
            "metrics": [
                {"name": metric, **self.metric_stats[metric].as_dict()}
                for metric in self.metrics
            ],
        }


def _csv_fields(line: str) -> list[str]:
    fields = next(csv.reader([line]))
    # AlfaOBD commonly terminates every row with a cosmetic comma.  Remove exactly
    # that CSV field; a genuinely absent last metric then remains detectable as short.
    if line.rstrip("\r\n").endswith(",") and fields and fields[-1] == "":
        fields.pop()
    return [field.strip() for field in fields]


def _iso_date(raw: str) -> str | None:
    try:
        return datetime.strptime(raw, "%y/%m/%d").date().isoformat()
    except ValueError:
        return None


def parse_lines(lines: Iterable[str], source: str = "<stream>") -> dict[str, object]:
    """Parse a Gauges_Data stream without retaining sample rows."""
    sections: list[Section] = []
    current: Section | None = None
    current_profile: str | None = None
    marker_pending: str | None = None
    total_lines = 0
    ignored_lines = 0
    unparsed_lines = 0

    def finish(end_line: int) -> None:
        nonlocal current
        if current is not None:
            current.end_line = end_line
            sections.append(current)
            current = None

    for line_number, raw_line in enumerate(lines, 1):
        total_lines = line_number
        line = raw_line.lstrip("\ufeff").strip("\r\n")
        stripped = line.strip()

        if stripped.startswith(PROFILE_PREFIX):
            current_profile = stripped[len(PROFILE_PREFIX) :].strip() or UNKNOWN_PROFILE
            marker_pending = "named"
            continue

        # Some AlfaOBD versions write the marker but omit the unchanged profile name.
        # Carry the last named profile forward, while making that inference visible.
        if stripped == PROFILE_PREFIX.rstrip():
            marker_pending = "blank"
            continue

        if stripped.startswith(DATE_PREFIX):
            finish(line_number - 1)
            date_raw = stripped[len(DATE_PREFIX) :].strip()
            current = Section(
                index=len(sections) + 1,
                profile=current_profile or UNKNOWN_PROFILE,
                profile_source="explicit" if marker_pending == "named" else "inherited",
                profile_marker=marker_pending or "absent",
                date_raw=date_raw,
                date=_iso_date(date_raw),
                start_line=line_number,
            )
            marker_pending = None
            continue

        if not stripped or SEPARATOR_RE.fullmatch(stripped):
            ignored_lines += 1
            continue

        if current is not None and current.header_line is None and stripped.startswith("Time,"):
            fields = _csv_fields(line)
            if len(fields) >= 2 and fields[0] == "Time":
                current.add_header(fields, line_number)
            else:
                unparsed_lines += 1
            continue

        if current is not None and current.header_line is not None:
            fields = _csv_fields(line)
            if fields and TIME_RE.fullmatch(fields[0]):
                current.add_sample(fields)
                continue

        unparsed_lines += 1

    finish(total_lines)
    return _build_inventory(
        source=source,
        sections=sections,
        total_lines=total_lines,
        ignored_lines=ignored_lines,
        unparsed_lines=unparsed_lines,
    )


def _build_inventory(
    *,
    source: str,
    sections: list[Section],
    total_lines: int,
    ignored_lines: int,
    unparsed_lines: int,
) -> dict[str, object]:
    aggregate: dict[tuple[str, str], dict[str, object]] = {}
    profile_sections: dict[str, int] = {}
    profile_explicit: dict[str, int] = {}
    valid_dates = [section.date for section in sections if section.date is not None]

    for section in sections:
        profile_sections[section.profile] = profile_sections.get(section.profile, 0) + 1
        if section.profile_source == "explicit":
            profile_explicit[section.profile] = profile_explicit.get(section.profile, 0) + 1
        for metric in section.metrics:
            key = (section.profile, metric)
            row = aggregate.setdefault(
                key,
                {
                    "profile": section.profile,
                    "metric": metric,
                    "section_count": 0,
                    "first_date": None,
                    "last_date": None,
                    "stats": MetricStats(),
                },
            )
            row["section_count"] = int(row["section_count"]) + 1
            if section.date is not None:
                first_date = row["first_date"]
                last_date = row["last_date"]
                row["first_date"] = section.date if first_date is None else min(first_date, section.date)
                row["last_date"] = section.date if last_date is None else max(last_date, section.date)
            stats = row["stats"]
            assert isinstance(stats, MetricStats)
            stats.merge(section.metric_stats[metric])

    metric_rows = []
    for key in sorted(aggregate, key=lambda item: (item[0].casefold(), item[1].casefold())):
        row = aggregate[key]
        stats = row.pop("stats")
        assert isinstance(stats, MetricStats)
        metric_rows.append({**row, **stats.as_dict()})

    profile_rows = [
        {
            "profile": profile,
            "section_count": profile_sections[profile],
            "explicit_marker_count": profile_explicit.get(profile, 0),
            "metric_count": sum(row["profile"] == profile for row in metric_rows),
        }
        for profile in sorted(profile_sections, key=str.casefold)
    ]

    return {
        "schema_version": 1,
        "source": source,
        "interpretation_warning": (
            "Profile names are AlfaOBD selected-profile labels, not confirmed ECU identity. "
            "A blank profile marker inherits the most recent preceding named profile; this is "
            "an archive-structure inference. Gauge data does not itself contain DID identifiers."
        ),
        "total_lines": total_lines,
        "ignored_lines": ignored_lines,
        "unparsed_lines": unparsed_lines,
        "section_count": len(sections),
        "first_date": min(valid_dates) if valid_dates else None,
        "last_date": max(valid_dates) if valid_dates else None,
        "sample_rows": sum(section.sample_rows for section in sections),
        "valid_rows": sum(section.valid_rows for section in sections),
        "short_rows": sum(section.short_rows for section in sections),
        "long_rows": sum(section.long_rows for section in sections),
        "profiles": profile_rows,
        "metrics": metric_rows,
        "sections": [section.as_dict() for section in sections],
    }


def inventory_file(path: Path) -> dict[str, object]:
    with path.open("rb") as raw_input:
        digest = hashlib.file_digest(raw_input, "sha256")
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as input_file:
        inventory = parse_lines(input_file, source=str(path))
    inventory["source_size_bytes"] = path.stat().st_size
    inventory["source_sha256"] = digest.hexdigest()
    return inventory


def _atomic_text(path: Path, writer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
    )
    temporary = Path(handle.name)
    try:
        with handle:
            writer(handle)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_reports(output_dir: Path, inventory: dict[str, object]) -> list[Path]:
    json_path = output_dir / "inventory.json"
    sections_path = output_dir / "sections.csv"
    metrics_path = output_dir / "metrics.csv"

    def json_writer(output: TextIO) -> None:
        json.dump(inventory, output, indent=2, ensure_ascii=False)
        output.write("\n")

    def sections_writer(output: TextIO) -> None:
        fieldnames = [
            "index", "profile", "profile_source", "profile_marker", "date", "date_raw",
            "start_line", "header_line", "end_line", "first_time", "last_time",
            "sample_rows", "valid_rows", "short_rows", "long_rows", "metric_count", "metrics",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for section in inventory["sections"]:
            metrics = section["metrics"]
            writer.writerow(
                {
                    **{key: section[key] for key in fieldnames if key not in {"metric_count", "metrics"}},
                    "metric_count": len(metrics),
                    "metrics": " | ".join(metric["name"] for metric in metrics),
                }
            )

    def metrics_writer(output: TextIO) -> None:
        fieldnames = [
            "profile", "metric", "section_count", "first_date", "last_date",
            "numeric_count", "missing_count", "nonnumeric_count", "minimum", "maximum",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(inventory["metrics"])

    _atomic_text(json_path, json_writer)
    _atomic_text(sections_path, sections_writer)
    _atomic_text(metrics_path, metrics_writer)
    return [json_path, sections_path, metrics_path]


def print_summary(inventory: dict[str, object], output: TextIO | None = None) -> None:
    if output is None:
        output = sys.stdout
    print(f"Archive: {inventory['source']}", file=output)
    print(
        f"Sections: {inventory['section_count']}; dates: "
        f"{inventory['first_date'] or 'unknown'} to {inventory['last_date'] or 'unknown'}",
        file=output,
    )
    print(
        f"Rows: {inventory['sample_rows']} samples; {inventory['valid_rows']} valid; "
        f"{inventory['short_rows']} short; {inventory['long_rows']} long; "
        f"{inventory['unparsed_lines']} other unparsed lines",
        file=output,
    )
    print(f"Profile namespaces: {len(inventory['profiles'])}", file=output)
    for profile in inventory["profiles"]:
        print(
            f"  {profile['section_count']:>4} sections / {profile['metric_count']:>3} metrics / "
            f"{profile['explicit_marker_count']:>3} explicit markers  {profile['profile']}",
            file=output,
        )


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        description=(
            "Inventory a concatenated AlfaOBD Gauges_Data CSV archive offline; no ADB or CAN I/O."
        )
    )
    argument_parser.add_argument("archive", type=Path, help="saved Gauges_Data.csv/.log archive")
    argument_parser.add_argument(
        "--out-dir",
        type=Path,
        help=(
            "report directory (default: tmp/inventories/alfaobd_gauges/"
            "<source-name>_<sha256-prefix>)"
        ),
    )
    return argument_parser


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        inventory = inventory_file(args.archive)
        output_dir = args.out_dir
        if output_dir is None:
            output_dir = DEFAULT_OUTPUT_ROOT / (
                f"{args.archive.stem}_{inventory['source_sha256'][:12]}"
            )
        paths = write_reports(output_dir, inventory)
    except (OSError, csv.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print_summary(inventory)
    print("Reports:", file=sys.stdout)
    for path in paths:
        print(f"  {path}", file=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
