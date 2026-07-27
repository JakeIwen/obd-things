#!/usr/bin/env python3
"""Decode a checkpointed ZF9HP DID-sweep result file without vehicle access.

The input is the JSONL evidence written by ``tools/did_sweep.py``. Positive
responses are decoded with the vendor-derived, ECU-scoped ZF9HP catalog;
negative responses and decoding failures remain explicit report records.
American display conversions are included beside AlfaOBD's native units.

This tool opens no CAN, ADB, network, service, or device interface. Its default
output is under gitignored ``tmp/``.
"""

from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from projects.ecu_mapping import zf9hp


DEFAULT_OUTPUT = REPO / "tmp/ecu_mapping/zf9hp_support_decode.json"
HEX_DID = re.compile(r"^[0-9A-Fa-f]{4}$")
DISPLAY_CONVERSIONS = {
    "°C": ("°F", Decimal("1.8"), Decimal("32")),
    "Nm": ("lb-ft", Decimal("0.737562149277"), Decimal("0")),
    "km/h": ("mph", Decimal("0.621371192237"), Decimal("0")),
    "mbar": ("psi", Decimal("0.014503773773"), Decimal("0")),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def display_value(value: Decimal, native_unit: str) -> tuple[Decimal, str]:
    conversion = DISPLAY_CONVERSIONS.get(native_unit)
    if conversion is None:
        return value, native_unit
    unit, scale, offset = conversion
    return value * scale + offset, unit


def compact_hex(value: object) -> bytes:
    if not isinstance(value, str):
        raise ValueError("response_hex must be a string")
    compact = re.sub(r"\s+", "", value)
    if not compact or len(compact) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", compact):
        raise ValueError("response_hex is not even-length hexadecimal")
    return bytes.fromhex(compact)


def decode_record(record: dict[str, object], line_number: int) -> dict[str, object]:
    did_text = record.get("did")
    if not isinstance(did_text, str) or not HEX_DID.fullmatch(did_text):
        raise ValueError(f"line {line_number}: did must be exactly four hexadecimal digits")
    did = int(did_text, 16)
    category = record.get("category")
    output = {
        "line": line_number,
        "did": f"{did:04X}",
        "category": category,
        "request_hex": record.get("request_hex"),
        "response_hex": record.get("response_hex"),
        "status": record.get("status"),
        "negative_response": record.get("negative_response"),
        "decoded": [],
    }
    if category != "positive":
        output["decode_status"] = "not_positive"
        return output

    try:
        response = compact_hex(record.get("response_hex"))
        if response[:3] != bytes((0x62, did >> 8, did & 0xFF)):
            raise ValueError(f"response lacks exact 62 {did:04X} echo")
        decoded = zf9hp.decode_positive_response(response)
    except (KeyError, ValueError) as exc:
        output["decode_status"] = "decode_error"
        output["decode_error"] = str(exc)
        return output

    for item in decoded:
        shown, shown_unit = display_value(
            item.value,
            item.definition.unit,
        )
        output["decoded"].append(
            {
                "catalog_order": item.definition.order,
                "key": item.definition.key,
                "label": item.definition.label,
                "raw_value": item.raw_value,
                "native_value": decimal_text(item.value),
                "native_unit": item.definition.unit,
                "display_value": decimal_text(shown),
                "display_unit": shown_unit,
                "evidence_quality": "vendor_derived_static",
                "vehicle_support": "observed_positive_read",
            }
        )
    output["decode_status"] = "decoded"
    return output


def load_results(path: Path) -> list[dict[str, object]]:
    records = []
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {line_number}: invalid JSON: {exc.msg}") from exc
            if not isinstance(raw, dict):
                raise ValueError(f"line {line_number}: expected one JSON object")
            records.append(decode_record(raw, line_number))
    if not records:
        raise ValueError("result file contains no JSONL records")
    return records


def build_report(path: Path) -> dict[str, object]:
    records = load_results(path)
    categories = Counter(str(record["category"]) for record in records)
    decode_statuses = Counter(str(record["decode_status"]) for record in records)
    decoded_signals = sum(len(record["decoded"]) for record in records)
    return {
        "schema_version": 1,
        "profile": "ZF9HP",
        "source": {
            "path": os.path.relpath(path.resolve(), REPO),
            "sha256": sha256(path),
            "size": path.stat().st_size,
        },
        "evidence": {
            "definition_quality": "vendor_derived_static",
            "applicability": (
                "A decoded positive read proves this TCM answered the DID in the "
                "captured state; controlled physical plausibility is still required "
                "before dashboard allowlisting."
            ),
            "native_units_preserved": True,
            "american_display_conversions": ["°C->°F", "Nm->lb-ft", "km/h->mph", "mbar->psi"],
        },
        "summary": {
            "records": len(records),
            "category_counts": dict(sorted(categories.items())),
            "decode_status_counts": dict(sorted(decode_statuses.items())),
            "decoded_signals": decoded_signals,
        },
        "records": records,
    }


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline decoder for ZF9HP did_sweep.py JSONL evidence."
    )
    parser.add_argument("results_jsonl", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    input_path = args.results_jsonl.resolve()
    if not input_path.is_file():
        parser.error(f"result file does not exist: {input_path}")
    report = build_report(input_path)
    output_path = args.output.resolve()
    atomic_json(output_path, report)
    print(
        f"decoded {report['summary']['decoded_signals']} signal(s) from "
        f"{report['summary']['records']} record(s)"
    )
    print(f"report: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
