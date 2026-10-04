"""Command-line orchestration for the passive in-vehicle scan harvester."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence


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
    discover_campaigns_fn: Callable[..., list[Path]],
    load_state_fn: Callable[..., Mapping[str, Any]],
    harvest_campaign_fn: Callable[..., dict[str, Any]],
    scan_store_factory: Callable[..., Any],
    write_json_atomic_fn: Callable[..., None],
) -> dict[str, Any]:
    campaigns = discover_campaigns_fn(capture_root, patterns)
    summary: dict[str, Any] = {"campaigns": [], "imported": [], "chunks_processed": 0}
    remaining = max_chunks
    for campaign_dir in campaigns:
        state = load_state_fn(out_dir, campaign_dir.name)
        if state.get("complete") and not reimport:
            continue
        if remaining is not None and remaining <= 0:
            break
        result = harvest_campaign_fn(campaign_dir, out_dir, max_chunks=remaining, log=log)
        if remaining is not None:
            remaining -= result["chunks_processed"]
        summary["chunks_processed"] += result["chunks_processed"]
        summary["campaigns"].append(
            {
                "campaign": result["campaign"],
                "chunks_processed": result["chunks_processed"],
                "complete": result["complete"],
                "new_scans": [
                    {
                        "scan_id": scan["scan_id"],
                        "classification": scan["classification"],
                        "started_at": scan["started_at"],
                        "completed_at": scan["completed_at"],
                        "exchange_count": scan["exchange_count"],
                    }
                    for scan in result["new_scans"]
                ],
            }
        )
    if db_path is not None:
        with scan_store_factory(db_path) as store:
            for path in sorted((out_dir / "scans").glob("*.json")):
                try:
                    report = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                outcome = store.import_report(report, report_path=str(path))
                if outcome.get("inserted"):
                    summary["imported"].append(
                        {"scan_id": report["scan_id"], "module_results": outcome["module_results"]}
                    )
            if cache_path is not None:
                payload = store.cache_payload()
                write_json_atomic_fn(cache_path, payload)
                summary["cache"] = {
                    "path": str(cache_path),
                    "scan_count": payload["scan_count"],
                    "modules": len(payload["modules"]),
                }
    return summary


def build_parser(
    *,
    default_capture_root: Path,
    default_out_dir: Path,
    default_db: Path,
    default_cache: Path,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Passively harvest the van's own diagnostic sweeps (tester F1) from completed drive "
            "recorder chunks. Receive-only files; never transmits or touches the recorder."
        )
    )
    parser.add_argument("--capture-root", type=Path, default=default_capture_root)
    parser.add_argument("--out-dir", type=Path, default=default_out_dir)
    parser.add_argument("--db", type=Path, default=default_db)
    parser.add_argument("--cache", type=Path, default=default_cache)
    parser.add_argument(
        "--campaign",
        action="append",
        dest="patterns",
        help="campaign name or glob (repeatable); default: every campaign not yet complete",
    )
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=None,
        help="stop after this many chunk files in one run (bounds CPU on the Pi)",
    )
    parser.add_argument("--no-import", action="store_true", help="write scan reports only")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None,
    *,
    parser_factory: Callable[[], argparse.ArgumentParser],
    run_fn: Callable[..., dict[str, Any]],
    harvest_error: type[Exception],
) -> int:
    args = parser_factory().parse_args(argv)
    log = (lambda text: None) if args.quiet else (lambda text: print(text, file=sys.stderr))
    try:
        summary = run_fn(
            capture_root=args.capture_root,
            out_dir=args.out_dir,
            db_path=None if args.no_import else args.db,
            cache_path=None if args.no_import else args.cache,
            patterns=args.patterns,
            max_chunks=args.max_chunks,
            log=log,
        )
    except harvest_error as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0
