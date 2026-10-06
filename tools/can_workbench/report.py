"""Build immutable offline investigation bundles from an explicit manifest."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time

from lib.reports import atomic_json
from . import adapter as a

REPO = Path(__file__).resolve().parents[2]
VERSION = 1


def validate_manifest(config):
    if not isinstance(config, dict) or type(config.get("schema_version")) is not int or config["schema_version"] != 1:
        a.reject("unsupported investigation manifest schema_version (expected 1)")
    allowed = {"schema_version", "title", "summary", "questions", "next_checks", "captures",
               "fields", "references", "analyses"}
    if config.keys() - allowed:
        a.reject(f"unknown manifest keys: {sorted(config.keys() - allowed)}")
    for key in ("title", "summary"):
        if not isinstance(config.get(key), str) or not config[key]:
            a.reject(f"manifest requires {key} text")
    for key in ("questions", "next_checks"):
        if not isinstance(config.get(key, []), list) or any(not isinstance(v, str) for v in config.get(key, [])):
            a.reject(f"{key} must be a list of text")
    captures, fields = config.get("captures"), config.get("fields")
    for name, items, maximum in (("captures", captures, 16), ("fields", fields, 64)):
        if not isinstance(items, list) or not 1 <= len(items) <= maximum:
            a.reject(f"{name} requires 1..{maximum} entries")
        names = [item.get("name") for item in items if isinstance(item, dict)]
        if len(names) != len(items) or any(not isinstance(n, str) or not n for n in names) or len(set(names)) != len(names):
            a.reject(f"{name} requires unique nonempty names")
    for capture in captures:
        if not isinstance(capture.get("path"), str) or not capture["path"]:
            a.reject("capture requires a path")
        channels = capture.get("channels")
        if not isinstance(channels, dict) or not channels or any(
            not isinstance(c, str) or not c or not isinstance(b, str) or not b for c, b in channels.items()
        ):
            a.reject("capture requires explicit channel-to-bus mappings")
        if not isinstance(capture.get("conditions"), str) or not capture["conditions"]:
            a.reject("capture conditions required; use 'unknown' when absent")
    for field in fields:
        a.validate_field(field)
    for key in ("references", "analyses"):
        items = config.get(key, [])
        if not isinstance(items, list) or len(items) > 32 or any(not isinstance(p, str) for p in items):
            a.reject(f"{key} must contain at most 32 file paths")


def reference_report(path, captures, fields, window, max_points, gap):
    data = a.load_json(path)
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        a.reject("unsupported reference schema_version")
    capture = next((c for c in captures if c["name"] == data.get("capture")), None)
    field = next((f for f in fields if f["name"] == data.get("field")), None)
    if capture is None or field is None:
        a.reject("reference must name a selected capture and field")
    if data.get("unit") != field["unit"]:
        a.reject("reference units must exactly match the selected field; convert explicitly upstream")
    for key in ("name", "provenance", "timestamp_basis"):
        if not isinstance(data.get(key), str) or not data[key]:
            a.reject(f"reference requires {key}")
    if data["timestamp_basis"] != "capture_clock":
        a.reject("reference timestamp_basis must be capture_clock (document alignment in provenance)")
    rows = data.get("observations")
    if not isinstance(rows, list):
        a.reject("reference observations must be a list; an empty list means missing")
    series = a.Series(max_points, gap)
    origin = a.stamp(capture["coverage"]["first_timestamp"]) if capture["coverage"]["first_timestamp"] else None
    previous = None
    for row in rows:
        timestamp = a.stamp(row["timestamp"])
        if previous is not None and timestamp < previous:
            a.reject("reference timestamps decrease")
        previous = timestamp
        a.number(row["value"], "reference value")
        if origin is None or (window and not window[0] <= timestamp - origin <= window[1]):
            continue
        series.add(timestamp, row["value"])
    return {**{k: v for k, v in data.items() if k != "observations"},
            "source": a.identity(path), "series": series.report(origin or Decimal(0)),
            "observation_count": len(rows)}


def render(bundle):
    template = Path(__file__).with_name("viewer.html").read_text(encoding="utf-8")
    # Prevent user text (including imported reports) escaping the inert JSON node.
    encoded = json.dumps(bundle, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c")
    return template.replace("__BUNDLE_JSON__", encoded)


def _prepare_build(manifest, output, max_points, gap, window):
    manifest, output = Path(manifest).resolve(strict=True), Path(output).absolute()
    if output.exists() or output.is_symlink():
        a.reject(f"output already exists; choose a new directory: {output}")
    if not 4 <= max_points <= 20000 or type(max_points) is not int:
        a.reject("max_points must be an integer from 4 to 20000")
    if a.number(gap, "gap") <= 0:
        a.reject("gap must be positive")
    if window is not None:
        window = tuple(a.stamp(str(v)) for v in window)
        if len(window) != 2 or not 0 <= window[0] <= window[1]:
            a.reject("window must be ordered, nonnegative elapsed seconds")
    return manifest, output, window


def _resolve_manifest_path(manifest, path):
    return (manifest.parent / path).resolve(strict=True)


def _load_manifest_inputs(manifest, capture_names, field_names):
    manifest_identity = a.identity(manifest)
    config = a.load_json(manifest)
    validate_manifest(config)
    captures = [c for c in config["captures"]
                if not capture_names or c["name"] in capture_names]
    fields = [f for f in config["fields"]
              if not field_names or f["name"] in field_names]
    selections = ((capture_names, captures, "capture"),
                  (field_names, fields, "field"))
    for names, selected, label in selections:
        if not selected or set(names) - {s["name"] for s in selected}:
            a.reject(f"unknown or empty {label} selection")
    input_paths = [manifest] + [
        _resolve_manifest_path(manifest, c["path"]) for c in captures
    ]
    input_paths += [
        _resolve_manifest_path(manifest, p)
        for key in ("references", "analyses") for p in config.get(key, [])
    ]
    before = {str(p): a.identity(p) for p in input_paths}
    if before[str(manifest)] != manifest_identity:
        a.reject("manifest changed while loading")
    return config, captures, fields, before


def _analyze_inputs(manifest, config, captures, fields, before, window,
                    max_points, gap):
    capture_results = [
        a.capture_report(_resolve_manifest_path(manifest, c["path"]), c, fields,
                         window=window, max_points=max_points, gap=gap)
        for c in captures
    ]
    for result in capture_results:
        source_path = _resolve_manifest_path(manifest, result["path"])
        result["source"] = before[str(source_path)]
    references, excluded_references = [], []
    for path in config.get("references", []):
        data = a.load_json(_resolve_manifest_path(manifest, path))
        if (not isinstance(data, dict)
                or type(data.get("schema_version")) is not int
                or data["schema_version"] != 1):
            a.reject("unsupported reference schema_version")
        if (data.get("capture") not in {c["name"] for c in config["captures"]}
                or data.get("field") not in {f["name"] for f in config["fields"]}):
            a.reject("reference names an unknown capture or field")
        if (data.get("capture") not in {c["name"] for c in captures}
                or data.get("field") not in {f["name"] for f in fields}):
            excluded_references.append(path)
        else:
            references.append(reference_report(
                _resolve_manifest_path(manifest, path), capture_results, fields,
                window, max_points, gap))
    analyses = [a.import_analysis(_resolve_manifest_path(manifest, p))
                for p in config.get("analyses", [])]
    comparisons = [a.compare_captures(capture_results[0], c)
                   for c in capture_results[1:]]
    for path, expected in before.items():
        if a.identity(path) != expected:
            a.reject(f"input changed during generation: {path}")
    return capture_results, references, excluded_references, analyses, comparisons


def _assemble_bundle(config, before, capture_results, fields, references,
                     excluded_references, analyses, comparisons, capture_names,
                     field_names, window, max_points, gap, command, started):
    source_files = [Path(__file__), Path(__file__).with_name("adapter.py"),
                    Path(__file__).with_name("viewer.html"), REPO / "tools/can_workbench_report.py",
                    REPO / "lib/candump_io.py", REPO / "lib/signal_fields.py",
                    REPO / "lib/reports.py", REPO / "tools/can_capture_summary.py",
                    REPO / "tools/can_capture_compare.py"]
    return {
        "schema": "obd-things.can-workbench", "schema_version": VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "title": config["title"], "summary": config["summary"],
        "questions": config.get("questions", []), "next_checks": config.get("next_checks", []),
        "manifest": config, "inputs": list(before.values()),
        "reproduction": {"cwd": str(Path.cwd()), "argv": command,
                         "shell_command": shlex.join(command) if command else None,
                         "python": sys.version, "tool_version": VERSION,
                         "tool_sources": [a.identity(p) for p in source_files],
                         "settings": {"capture_names": list(capture_names), "field_names": list(field_names),
                                      "window_elapsed_s": [str(v) for v in window] if window else None,
                                      "max_points": max_points, "gap_s": gap}},
        "policies": ["Offline only. No search, live CAN, or telemetry promotion.",
                     "Plots use seconds from each capture's first valid frame; captures are NOT synchronized.",
                     "Reference clocks must be explicitly aligned upstream; no inferred offsets or sample holding.",
                     "Imported analyses keep their original source bindings; they are not automatically joined to the selected captures or fields.",
                     "Summaries use existing schema 3 semantics over all frames in the selected window; malformed and blank line counts cover the full file.",
                     "Exact decimal timestamps supplement legacy float summary timestamps. Decreasing timestamps are rejected.",
                     "Field evidence tiers are attributed to supplied provenance, not independently revalidated here.",
                     "Decimation can omit brief excursions; full-range extrema/counts are retained and segments never bridge gaps.",
                     "Reports may contain private raw statistics and source paths; review before sharing."],
        "captures": capture_results, "fields": fields, "references": references,
        "excluded_references": excluded_references, "analyses": analyses,
        "comparisons": comparisons, "generation_seconds": time.monotonic() - started,
    }


def _publish_bundle(output, bundle):
    # Stage the entire pair beside the final directory. Never overwrite a prior
    # successful report (nonempty directories are protected by atomic rename).
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".can-workbench-", dir=output.parent))
    try:
        atomic_json(stage / "bundle.json", bundle)
        (stage / "index.html").write_text(render(bundle), encoding="utf-8")
        # rename to a nonexistent path is atomic; an existing nonempty report is
        # protected by the OS. Refuse even an empty existing destination.
        if output.exists() or output.is_symlink():
            a.reject(f"output appeared during generation: {output}")
        os.rename(stage, output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def build(manifest, output, *, capture_names=(), field_names=(), window=None,
          max_points=2000, gap=1.0, command=None):
    started = time.monotonic()
    manifest, output, window = _prepare_build(
        manifest, output, max_points, gap, window)
    config, captures, fields, before = _load_manifest_inputs(
        manifest, capture_names, field_names)
    capture_results, references, excluded_references, analyses, comparisons = (
        _analyze_inputs(
            manifest, config, captures, fields, before, window, max_points, gap)
    )
    bundle = _assemble_bundle(
        config, before, capture_results, fields, references,
        excluded_references, analyses, comparisons, capture_names, field_names,
        window, max_points, gap, command, started)
    _publish_bundle(output, bundle)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="schema-1 investigation JSON; relative inputs resolve beside it")
    parser.add_argument("--out", type=Path, help="new output directory; existing directories are never replaced")
    parser.add_argument("--capture", action="append", default=[], help="select a capture by manifest name; repeatable")
    parser.add_argument("--field", action="append", default=[], help="select a field by manifest name; repeatable")
    parser.add_argument("--window", nargs=2, metavar=("START", "END"), help="inclusive elapsed seconds from each capture's first frame")
    parser.add_argument("--max-points", type=int, default=2000)
    parser.add_argument("--gap-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    output = args.out or REPO / "tmp/can_workbench" / datetime.now(timezone.utc).strftime("report-%Y%m%dT%H%M%S%fZ")
    command = [sys.executable, str(REPO / "tools/can_workbench_report.py"),
               *(sys.argv[1:] if argv is None else argv)]
    try:
        result = build(args.manifest, output, capture_names=args.capture, field_names=args.field,
                       window=args.window, max_points=args.max_points, gap=args.gap_seconds, command=command)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"can-workbench: {exc}\n")
    print(result / "index.html")
    return 0
