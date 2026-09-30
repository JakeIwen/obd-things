"""Frozen report-writer source from commit 390404e.

Only these function definitions are executed by test_reports, in a namespace
containing standard-library file/JSON helpers. No CLI or transport is loaded.
Keep these excerpts unchanged: they are the before-side equivalence oracle.
"""

WRITERS = {
    "alfaobd_bcm_decode": r'''
def write_json_atomic(output: Path, report: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{output.name}.",
            dir=output.parent,
            delete=False,
        ) as destination:
            temporary_name = destination.name
            json.dump(report, destination, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_name, output)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
''',
    "alfaobd_catalog": r'''
def main() -> int:
    args = parse_args()
    if not args.device_id:
        raise SystemExit("at least one --device-id is required")
    report = build_export(args.database, args.labels, args.model_code, args.device_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=f".{args.output.name}.",
            dir=args.output.parent, delete=False
        ) as destination:
            temporary_name = destination.name
            json.dump(report, destination, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_name, args.output)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    print(f"wrote {args.output}")
    return 0
''',
    "alfaobd_dat": r'''
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
''',
    "alfaobd_gauge_join": r'''
def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
''',
    "alfaobd_gauges": r'''
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
''',
    "did_sweep": r'''
def atomic_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp-{os.getpid()}"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass
''',
    "dtc_inventory": r'''
def write_report(path, report):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp-{os.getpid()}"
    with open(temporary, "w") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
''',
    "ecu_discover": r'''
def write_report(path, report):
    """Atomically publish a complete or explicitly partial discovery report."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp-{os.getpid()}"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass
''',
    "identity_inventory": r'''
def write_report(path, report):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp-{os.getpid()}"
    with open(temporary, "w") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
''',
    "routine_scan": r'''
def write_report(path, report):
    """Atomically publish a complete or explicitly marked partial JSON report."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp-{os.getpid()}"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        # os.replace removes the temporary path. Clean it only when an earlier write/replace failed.
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass
''',
    "signal_correlate": r'''
def _dump(module, dids, start, samples, outfile, metadata=None):
    """Atomically checkpoint a capture so interruption cannot leave truncated JSON."""
    payload = metadata or {
        "module": module.key,
        "starttime": datetime.datetime.fromtimestamp(start).isoformat(),
        "dids": [f"{did:04X}" for did in dids],
        "samples": samples,
    }
    directory = os.path.dirname(outfile) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = f"{outfile}.tmp-{os.getpid()}"
    with open(temporary, "w") as handle:
        json.dump(payload, handle)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, outfile)
''',
}
