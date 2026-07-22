#!/usr/bin/env python3
"""Inspect AlfaOBD ``Data/*.dat`` plot-series files without touching ADB or CAN.

Observed AlfaOBD 2.4.4.0 files alternate a decimal series identifier line with one
semicolon-delimited value line.  The format carries neither timestamps nor labels, so this tool
deliberately calls them *series IDs*, not DIDs or parameter IDs.  Its main purpose is to compare a
post-campaign file with a baseline and distinguish new samples from unchanged or mechanically
duplicated cache data.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Iterable, TextIO


_SERIES_ID = re.compile(r"[0-9]+")


class DatFormatError(ValueError):
    """The input does not match the observed alternating-line AlfaOBD format."""


@dataclass(frozen=True)
class DatSeries:
    series_id: int
    occurrence: int
    id_line: int
    values_line: int
    values: tuple[str, ...]

    @property
    def key(self) -> tuple[int, int]:
        return self.series_id, self.occurrence

    def as_dict(self) -> dict[str, object]:
        numeric_values: list[float] = []
        missing_count = 0
        nonnumeric_count = 0
        for value in self.values:
            if value == "":
                missing_count += 1
                continue
            try:
                numeric = float(value)
            except ValueError:
                nonnumeric_count += 1
                continue
            if not math.isfinite(numeric):
                nonnumeric_count += 1
                continue
            numeric_values.append(numeric)

        digest = hashlib.sha256()
        for value in self.values:
            encoded = value.encode("utf-8", errors="surrogatepass")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)

        return {
            "series_id": self.series_id,
            "occurrence": self.occurrence,
            "id_line": self.id_line,
            "values_line": self.values_line,
            "sample_count": len(self.values),
            "numeric_count": len(numeric_values),
            "missing_count": missing_count,
            "nonnumeric_count": nonnumeric_count,
            "minimum": min(numeric_values) if numeric_values else None,
            "maximum": max(numeric_values) if numeric_values else None,
            "values_sha256": digest.hexdigest(),
        }


def _strip_line_ending(line: str) -> str:
    return line.rstrip("\r\n")


def parse_lines(lines: Iterable[str], source: str = "<stream>") -> list[DatSeries]:
    """Parse alternating series-ID/value lines while retaining exact value tokens."""
    iterator = iter(enumerate(lines, 1))
    occurrences: Counter[int] = Counter()
    series: list[DatSeries] = []

    while True:
        try:
            id_line_number, raw_id = next(iterator)
        except StopIteration:
            break

        id_text = _strip_line_ending(raw_id)
        if not _SERIES_ID.fullmatch(id_text):
            raise DatFormatError(
                f"{source}:{id_line_number}: expected a decimal series identifier, "
                f"got {id_text!r}"
            )
        series_id = int(id_text, 10)

        try:
            values_line_number, raw_values = next(iterator)
        except StopIteration as exc:
            raise DatFormatError(
                f"{source}:{id_line_number}: series {series_id} has no value line"
            ) from exc

        value_text = _strip_line_ending(raw_values)
        values = value_text.split(";")
        if value_text.endswith(";"):
            values.pop()
        if value_text == "":
            values = []

        occurrences[series_id] += 1
        series.append(
            DatSeries(
                series_id=series_id,
                occurrence=occurrences[series_id],
                id_line=id_line_number,
                values_line=values_line_number,
                values=tuple(values),
            )
        )

    return series


def _read_file(path: Path) -> tuple[list[DatSeries], int, str]:
    with path.open("rb") as raw_input:
        digest = hashlib.file_digest(raw_input, "sha256").hexdigest()
    with path.open("r", encoding="utf-8", errors="strict", newline="") as input_file:
        series = parse_lines(input_file, source=str(path))
    return series, path.stat().st_size, digest


def classify_values(
    current: tuple[str, ...], baseline: tuple[str, ...]
) -> dict[str, object]:
    """Classify an exact token comparison without treating decimal spellings as equivalent."""
    if current == baseline:
        return {"status": "unchanged", "sample_delta": 0}

    if baseline and len(current) > len(baseline) and len(current) % len(baseline) == 0:
        factor = len(current) // len(baseline)
        if current == baseline * factor:
            return {
                "status": "exact_repetition",
                "repeat_factor": factor,
                "sample_delta": len(current) - len(baseline),
            }

    if len(current) > len(baseline) and current[: len(baseline)] == baseline:
        return {
            "status": "baseline_prefix_with_append",
            "appended_samples": len(current) - len(baseline),
            "sample_delta": len(current) - len(baseline),
        }

    if len(current) < len(baseline) and baseline[: len(current)] == current:
        return {
            "status": "truncated_baseline_prefix",
            "removed_samples": len(baseline) - len(current),
            "sample_delta": len(current) - len(baseline),
        }

    return {
        "status": "changed",
        "sample_delta": len(current) - len(baseline),
    }


def inventory_file(path: Path, baseline_path: Path | None = None) -> dict[str, object]:
    current, size, digest = _read_file(path)
    report: dict[str, object] = {
        "schema_version": 1,
        "source": str(path),
        "source_size_bytes": size,
        "source_sha256": digest,
        "interpretation_warning": (
            "Series IDs are opaque AlfaOBD cache identifiers, not verified DIDs or labels. "
            "The format has no timestamps; only exact baseline comparison can establish whether "
            "tokens are new, unchanged, appended, or mechanically repeated."
        ),
        "series_count": len(current),
        "total_samples": sum(len(row.values) for row in current),
        "series": [row.as_dict() for row in current],
    }

    if baseline_path is None:
        return report

    baseline, baseline_size, baseline_digest = _read_file(baseline_path)
    baseline_by_key = {row.key: row for row in baseline}
    current_keys = {row.key for row in current}
    status_counts: Counter[str] = Counter()

    for series_row, output_row in zip(current, report["series"]):
        assert isinstance(output_row, dict)
        baseline_row = baseline_by_key.get(series_row.key)
        if baseline_row is None:
            comparison = {
                "status": "new_series",
                "sample_delta": len(series_row.values),
            }
        else:
            comparison = classify_values(series_row.values, baseline_row.values)
            comparison["baseline_sample_count"] = len(baseline_row.values)
        output_row["comparison"] = comparison
        status_counts[str(comparison["status"])] += 1

    missing_current = [
        {"series_id": row.series_id, "occurrence": row.occurrence}
        for row in baseline
        if row.key not in current_keys
    ]
    if missing_current:
        status_counts["missing_current_series"] += len(missing_current)

    report["baseline"] = {
        "source": str(baseline_path),
        "source_size_bytes": baseline_size,
        "source_sha256": baseline_digest,
        "series_count": len(baseline),
        "total_samples": sum(len(row.values) for row in baseline),
    }
    report["comparison"] = {
        "status_counts": dict(sorted(status_counts.items())),
        "missing_current_series": missing_current,
    }
    return report


def print_human(report: dict[str, object], output: TextIO | None = None) -> None:
    if output is None:
        output = sys.stdout
    print(f"AlfaOBD DAT: {report['source']}", file=output)
    print(
        f"Series: {report['series_count']}; samples: {report['total_samples']}; "
        f"bytes: {report['source_size_bytes']}",
        file=output,
    )
    baseline = report.get("baseline")
    if isinstance(baseline, dict):
        print(
            f"Baseline: {baseline['source']} ({baseline['series_count']} series; "
            f"{baseline['total_samples']} samples)",
            file=output,
        )

    print("\nID   occ   samples   numeric   min          max          comparison", file=output)
    for row in report["series"]:
        comparison = row.get("comparison")
        comparison_text = "not compared"
        if isinstance(comparison, dict):
            comparison_text = str(comparison["status"])
            if comparison_text == "exact_repetition":
                comparison_text += f" x{comparison['repeat_factor']}"
            elif comparison_text == "baseline_prefix_with_append":
                comparison_text += f" +{comparison['appended_samples']}"
        minimum = "n/a" if row["minimum"] is None else f"{row['minimum']:.8g}"
        maximum = "n/a" if row["maximum"] is None else f"{row['maximum']:.8g}"
        print(
            f"{row['series_id']:>3} {row['occurrence']:>5} {row['sample_count']:>9} "
            f"{row['numeric_count']:>9} {minimum:>12} {maximum:>12}  {comparison_text}",
            file=output,
        )

    comparison = report.get("comparison")
    if isinstance(comparison, dict):
        status_text = ", ".join(
            f"{status}={count}"
            for status, count in comparison["status_counts"].items()
        ) or "none"
        print(f"\nComparison: {status_text}", file=output)
    print(f"Warning: {report['interpretation_warning']}", file=output)


def write_json(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        description=(
            "Inspect an AlfaOBD Data/*.dat plot cache and optionally compare it with a baseline. "
            "This is offline-only and never opens ADB, CAN, or system services."
        ),
        epilog=(
            "Series IDs have no proven DID/label meaning and the file has no timestamps. JSON is "
            "written only when an explicit --json path is supplied."
        ),
    )
    argument_parser.add_argument("current", type=Path, help="post-campaign .dat file")
    argument_parser.add_argument(
        "--baseline", type=Path, help="optional pre-campaign .dat file"
    )
    argument_parser.add_argument(
        "--json", type=Path, metavar="PATH", help="also write the report to this path"
    )
    return argument_parser


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    protected = {args.current.resolve()}
    if args.baseline is not None:
        protected.add(args.baseline.resolve())
    if args.json is not None and args.json.resolve() in protected:
        print("error: --json must not overwrite an input .dat file", file=sys.stderr)
        return 2

    try:
        report = inventory_file(args.current, args.baseline)
        print_human(report)
        if args.json is not None:
            write_json(args.json, report)
            print(f"\nJSON: {args.json}")
    except (OSError, UnicodeError, DatFormatError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
