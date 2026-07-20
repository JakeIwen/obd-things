#!/usr/bin/env python3
"""Compare two saved ``can_capture_summary.py`` JSON reports offline.

The comparison distinguishes 11-bit and 29-bit identifiers with the same numeric value.  In
addition to the within-activity rate already stored in each summary, it computes a rate across
the *whole capture* and the fraction of the capture spanned by an ID.  Those metrics keep a
short startup burst from looking like a continuously active periodic frame.

Schema-v2 summaries include per-byte ranges.  The comparator uses them to flag bytes that are
constant in both captures but changed value between conditions; it reports only a mask, not the
underlying payload values.  Schema-v1 reports remain accepted, but cannot provide that check.

No CAN or service modules are imported.  This tool reads JSON files and writes only when an
explicit ``--json`` path is supplied.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from typing import TextIO


def _id_key(row: dict[str, object]) -> tuple[int, int]:
    return int(row["id_bits"]), int(row["can_id"])


def _format_can_id(can_id: int, id_bits: int) -> str:
    return f"{can_id:0{3 if id_bits == 11 else 8}X}"


def _mask_text(mask: int, rows: tuple[dict[str, object], ...]) -> str:
    maximum_dlc = max(
        (int(dlc) for row in rows for dlc in row.get("dlcs", [])),
        default=0,
    )
    return f"0x{mask:0{max(2, (maximum_dlc + 3) // 4)}X}"


def _finite_nonnegative(value: object, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be a finite non-negative number")
    return number


def _index(summary: dict[str, object], label: str) -> dict[tuple[int, int], dict[str, object]]:
    if not isinstance(summary, dict) or not isinstance(summary.get("ids"), list):
        raise ValueError(f"{label} is not a can_capture_summary JSON report")
    if summary.get("schema_version") not in (1, 2):
        raise ValueError(f"{label} has unsupported schema_version {summary.get('schema_version')!r}")
    indexed: dict[tuple[int, int], dict[str, object]] = {}
    for row in summary["ids"]:
        if not isinstance(row, dict):
            raise ValueError(f"{label} contains a non-object ID row")
        key = _id_key(row)
        if key in indexed:
            raise ValueError(
                f"{label} repeats {_format_can_id(key[1], key[0])} ({key[0]}-bit)"
            )
        indexed[key] = row
    return indexed


def _constant_values(row: dict[str, object]) -> dict[int, int]:
    """Return byte values known present and constant in every frame of one ID row.

    Older schema-v1 summaries intentionally return no values: their variability masks cannot
    distinguish a constant value shift between captures.
    """
    minimums = row.get("byte_minimums")
    maximums = row.get("byte_maximums")
    presence_counts = row.get("byte_presence_counts")
    if not all(isinstance(values, list) for values in (minimums, maximums, presence_counts)):
        return {}
    if not (len(minimums) == len(maximums) == len(presence_counts)):
        return {}
    count = int(row["count"])
    constants: dict[int, int] = {}
    for index, (minimum, maximum, presence_count) in enumerate(
        zip(minimums, maximums, presence_counts)
    ):
        if (
            isinstance(minimum, int)
            and not isinstance(minimum, bool)
            and isinstance(maximum, int)
            and not isinstance(maximum, bool)
            and 0 <= minimum <= 0xFF
            and minimum == maximum
            and int(presence_count) == count
        ):
            constants[index] = minimum
    return constants


def _activity(row: dict[str, object], capture_duration: float) -> dict[str, object]:
    row_duration = float(row.get("duration_s") or 0.0)
    first = row.get("first_timestamp")
    last = row.get("last_timestamp")
    count = int(row["count"])
    return {
        "count": count,
        "capture_rate_fps": count / capture_duration if capture_duration > 0 else None,
        "within_activity_rate_fps": row.get("average_rate_fps"),
        "activity_coverage_fraction": (
            min(1.0, row_duration / capture_duration) if capture_duration > 0 else None
        ),
        "first_timestamp": first,
        "last_timestamp": last,
        "dlcs": list(row.get("dlcs", [])),
        "changed_byte_mask": int(row.get("changed_byte_mask", 0)),
    }


def _rate_ratio(baseline_rate: object, current_rate: object) -> float | None:
    if baseline_rate is None or current_rate is None:
        return None
    baseline = float(baseline_rate)
    current = float(current_rate)
    return current / baseline if baseline > 0 else None


def compare_summaries(
    baseline: dict[str, object],
    current: dict[str, object],
    *,
    rate_factor: float = 2.0,
    coverage_delta: float = 0.25,
) -> dict[str, object]:
    """Return a machine-readable inventory/activity comparison."""
    if rate_factor <= 1 or not math.isfinite(rate_factor):
        raise ValueError("rate_factor must be finite and greater than 1")
    if not 0 <= coverage_delta <= 1 or not math.isfinite(coverage_delta):
        raise ValueError("coverage_delta must be between 0 and 1")

    baseline_ids = _index(baseline, "baseline")
    current_ids = _index(current, "current")
    baseline_duration = _finite_nonnegative(baseline.get("duration_s") or 0, "baseline duration")
    current_duration = _finite_nonnegative(current.get("duration_s") or 0, "current duration")

    def standalone(
        key: tuple[int, int], row: dict[str, object], duration: float
    ) -> dict[str, object]:
        bits, can_id = key
        return {
            "can_id": can_id,
            "can_id_hex": _format_can_id(can_id, bits),
            "id_bits": bits,
            "activity": _activity(row, duration),
        }

    new_ids = [
        standalone(key, current_ids[key], current_duration)
        for key in sorted(current_ids.keys() - baseline_ids.keys())
    ]
    missing_ids = [
        standalone(key, baseline_ids[key], baseline_duration)
        for key in sorted(baseline_ids.keys() - current_ids.keys())
    ]

    common = []
    motion_candidates = []
    constant_value_change_candidates = []
    for key in sorted(baseline_ids.keys() & current_ids.keys()):
        bits, can_id = key
        baseline_row = baseline_ids[key]
        current_row = current_ids[key]
        before = _activity(baseline_row, baseline_duration)
        after = _activity(current_row, current_duration)
        capture_rate_ratio = _rate_ratio(
            before["capture_rate_fps"], after["capture_rate_fps"]
        )
        within_rate_ratio = _rate_ratio(
            before["within_activity_rate_fps"], after["within_activity_rate_fps"]
        )
        baseline_mask = int(before["changed_byte_mask"])
        current_mask = int(after["changed_byte_mask"])
        newly_variable_mask = current_mask & ~baseline_mask
        no_longer_variable_mask = baseline_mask & ~current_mask
        baseline_constants = _constant_values(baseline_row)
        current_constants = _constant_values(current_row)
        constant_value_change_mask = 0
        for index in baseline_constants.keys() & current_constants.keys():
            if baseline_constants[index] != current_constants[index]:
                constant_value_change_mask |= 1 << index
        before_coverage = before["activity_coverage_fraction"]
        after_coverage = after["activity_coverage_fraction"]
        coverage_change = (
            float(after_coverage) - float(before_coverage)
            if before_coverage is not None and after_coverage is not None
            else None
        )

        reasons = []
        if capture_rate_ratio is not None and (
            capture_rate_ratio >= rate_factor or capture_rate_ratio <= 1 / rate_factor
        ):
            reasons.append("whole_capture_rate")
        if within_rate_ratio is not None and (
            within_rate_ratio >= rate_factor or within_rate_ratio <= 1 / rate_factor
        ):
            reasons.append("within_activity_rate")
        if coverage_change is not None and abs(coverage_change) >= coverage_delta:
            reasons.append("activity_coverage")
        if newly_variable_mask:
            reasons.append("newly_variable_bytes")
        if no_longer_variable_mask:
            reasons.append("no_longer_variable_bytes")
        if constant_value_change_mask:
            reasons.append("constant_value_change")
        if before["dlcs"] != after["dlcs"]:
            reasons.append("dlc_set")

        row = {
            "can_id": can_id,
            "can_id_hex": _format_can_id(can_id, bits),
            "id_bits": bits,
            "baseline": before,
            "current": after,
            "capture_rate_ratio": capture_rate_ratio,
            "within_activity_rate_ratio": within_rate_ratio,
            "activity_coverage_change": coverage_change,
            "newly_variable_byte_mask": newly_variable_mask,
            "newly_variable_byte_mask_hex": _mask_text(
                newly_variable_mask, (baseline_row, current_row)
            ),
            "no_longer_variable_byte_mask": no_longer_variable_mask,
            "no_longer_variable_byte_mask_hex": _mask_text(
                no_longer_variable_mask, (baseline_row, current_row)
            ),
            "constant_value_change_byte_mask": constant_value_change_mask,
            "constant_value_change_byte_mask_hex": _mask_text(
                constant_value_change_mask, (baseline_row, current_row)
            ),
            "significant_reasons": reasons,
        }
        common.append(row)
        if newly_variable_mask:
            motion_candidates.append(row)
        if constant_value_change_mask:
            constant_value_change_candidates.append(row)

    return {
        "schema_version": 2,
        "baseline": {
            "source": baseline.get("source"),
            "summary_schema_version": baseline.get("schema_version"),
            "duration_s": baseline_duration,
            "unique_ids": len(baseline_ids),
            "snapshot": baseline.get("snapshot"),
        },
        "current": {
            "source": current.get("source"),
            "summary_schema_version": current.get("schema_version"),
            "duration_s": current_duration,
            "unique_ids": len(current_ids),
            "snapshot": current.get("snapshot"),
        },
        "thresholds": {
            "rate_factor": rate_factor,
            "coverage_delta": coverage_delta,
        },
        "new_ids": new_ids,
        "missing_ids": missing_ids,
        "common_ids": common,
        "significantly_changed_ids": [row for row in common if row["significant_reasons"]],
        "newly_variable_candidates": motion_candidates,
        "constant_value_change_candidates": constant_value_change_candidates,
    }


def _display_number(value: object, digits: int = 3) -> str:
    return "n/a" if value is None else f"{float(value):.{digits}f}"


def _print_standalone(title: str, rows: list[dict[str, object]], output: TextIO) -> None:
    print(f"\n{title}: {len(rows)}", file=output)
    if not rows:
        print("  (none)", file=output)
        return
    print("  ID          bits  capture-fps  coverage  DLCs  variable-mask", file=output)
    for row in rows:
        activity = row["activity"]
        dlcs = ",".join(str(value) for value in activity["dlcs"])
        mask = _mask_text(int(activity["changed_byte_mask"]), (activity,))
        print(
            f"  {row['can_id_hex']:<11} {row['id_bits']:>4}  "
            f"{_display_number(activity['capture_rate_fps']):>11}  "
            f"{_display_number(activity['activity_coverage_fraction']):>8}  "
            f"{dlcs:<4}  {mask}",
            file=output,
        )


def print_human(comparison: dict[str, object], output: TextIO | None = None) -> None:
    if output is None:
        output = sys.stdout
    print(f"Baseline: {comparison['baseline']['source']}", file=output)
    print(f"Current:  {comparison['current']['source']}", file=output)
    snapshot_labels = [
        label
        for label in ("baseline", "current")
        if comparison[label].get("snapshot") is not None
    ]
    if snapshot_labels:
        print(
            "WARNING: bounded snapshot input(s): "
            + ", ".join(snapshot_labels)
            + "; missing IDs, coverage, and whole-capture rates are provisional.",
            file=output,
        )
    print(
        f"IDs: {comparison['baseline']['unique_ids']} baseline; "
        f"{comparison['current']['unique_ids']} current; "
        f"{len(comparison['common_ids'])} common",
        file=output,
    )
    _print_standalone("New IDs", comparison["new_ids"], output)
    _print_standalone("Missing IDs", comparison["missing_ids"], output)

    changed = comparison["significantly_changed_ids"]
    print(f"\nSignificantly changed common IDs: {len(changed)}", file=output)
    if not changed:
        print("  (none)", file=output)
    else:
        print(
            "  ID          bits  cap-rate ratio  coverage delta  new-var-mask  "
            "state-shift  reasons",
            file=output,
        )
        for row in changed:
            print(
                f"  {row['can_id_hex']:<11} {row['id_bits']:>4}  "
                f"{_display_number(row['capture_rate_ratio']):>14}  "
                f"{_display_number(row['activity_coverage_change']):>14}  "
                f"{row['newly_variable_byte_mask_hex']:<12}  "
                f"{row['constant_value_change_byte_mask_hex']:<11}  "
                f"{','.join(row['significant_reasons'])}",
                file=output,
            )

    candidates = comparison["newly_variable_candidates"]
    print(
        f"\nNewly variable byte candidates: {len(candidates)} "
        "(constant in baseline, variable in current)",
        file=output,
    )
    if not candidates:
        print("  (none)", file=output)
    else:
        for row in candidates:
            print(
                f"  {row['can_id_hex']} ({row['id_bits']}-bit): "
                f"{row['newly_variable_byte_mask_hex']}",
                file=output,
            )

    state_changes = comparison["constant_value_change_candidates"]
    print(
        f"\nConstant-value changes: {len(state_changes)} "
        "(constant in both captures, different value)",
        file=output,
    )
    if not state_changes:
        print("  (none)", file=output)
    else:
        for row in state_changes:
            print(
                f"  {row['can_id_hex']} ({row['id_bits']}-bit): "
                f"{row['constant_value_change_byte_mask_hex']}",
                file=output,
            )


def load_summary(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return value


def write_json(path: Path, comparison: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        json.dump(comparison, output, indent=2)
        output.write("\n")


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        description="Compare two saved CAN capture summary JSON reports; offline only."
    )
    argument_parser.add_argument("baseline", type=Path)
    argument_parser.add_argument("current", type=Path)
    argument_parser.add_argument("--json", type=Path, metavar="PATH")
    argument_parser.add_argument(
        "--rate-factor",
        type=float,
        default=2.0,
        help="flag common-ID rates differing by this factor (default: 2.0)",
    )
    argument_parser.add_argument(
        "--coverage-delta",
        type=float,
        default=0.25,
        help="flag activity-coverage changes at least this large (default: 0.25)",
    )
    return argument_parser


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.json is not None:
            output = args.json.resolve()
            if output in (args.baseline.resolve(), args.current.resolve()):
                raise ValueError("--json path must not overwrite an input summary")
        comparison = compare_summaries(
            load_summary(args.baseline),
            load_summary(args.current),
            rate_factor=args.rate_factor,
            coverage_delta=args.coverage_delta,
        )
        if args.json is not None:
            write_json(args.json, comparison)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print_human(comparison)
    if args.json is not None:
        print(f"\nJSON: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
