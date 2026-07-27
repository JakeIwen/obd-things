#!/usr/bin/env python3
"""Inventory literal byte arrays in owner-supplied JADX Java source.

This deliberately does not decode or redistribute AlfaOBD data.  It emits a
compact, provenance-bearing inventory that can be compared with independently
recovered catalog row counts and live diagnostic traces.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Iterator


ASCII_VALUES = {
    "NUL": 0,
    "SOH": 1,
    "STX": 2,
    "ETX": 3,
    "EOT": 4,
    "ENQ": 5,
    "ACK": 6,
    "BEL": 7,
    "BS": 8,
    "HT": 9,
    "LF": 10,
    "VT": 11,
    "FF": 12,
    "CR": 13,
    "SO": 14,
    "SI": 15,
    "DLE": 16,
    "DC1": 17,
    "DC2": 18,
    "DC3": 19,
    "DC4": 20,
    "NAK": 21,
    "SYN": 22,
    "ETB": 23,
    "CAN": 24,
    "EM": 25,
    "SUB": 26,
    "ESC": 27,
    "FS": 28,
    "GS": 29,
    "RS": 30,
    "US": 31,
    "DEL": 127,
}

DECLARATION_RE = re.compile(
    r"(?m)^[ \t]*(?:public[ \t]+)?static[ \t]+byte"
    r"(?P<suffix>\[\]|\[\]\[\])[ \t]+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"[ \t]*=[ \t]*\{"
)
ROW_RE = re.compile(r"new[ \t]+byte\[\][ \t]*\{(?P<body>[^{}]*)\}")


class ParseError(ValueError):
    """Raised when a literal byte array cannot be inventoried safely."""


def _balanced_initializer(source: str, open_brace: int) -> tuple[str, int]:
    depth = 0
    for index in range(open_brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace + 1 : index], index + 1
    raise ParseError("unterminated byte-array initializer")


def _parse_byte(token: str) -> int:
    token = token.strip()
    if not token:
        raise ParseError("empty byte literal")
    if token.startswith("Ascii."):
        name = token.removeprefix("Ascii.")
        if name not in ASCII_VALUES:
            raise ParseError(f"unsupported Ascii constant: {token}")
        value = ASCII_VALUES[name]
    elif token == "SignedBytes.MAX_POWER_OF_TWO":
        value = 64
    elif token == "UnsignedBytes.MAX_POWER_OF_TWO":
        value = -128
    elif re.fullmatch(r"-?[0-9]+", token):
        value = int(token)
    else:
        raise ParseError(f"unsupported byte literal: {token}")
    if not -128 <= value <= 127:
        raise ParseError(f"byte literal out of range: {token}")
    return value & 0xFF


def _parse_row(body: str) -> list[int]:
    if not body.strip():
        return []
    return [_parse_byte(token) for token in body.split(",")]


def iter_arrays(source: str) -> Iterator[dict[str, object]]:
    for match in DECLARATION_RE.finditer(source):
        open_brace = match.end() - 1
        body, _ = _balanced_initializer(source, open_brace)
        name = match.group("name")
        suffix = match.group("suffix")
        line = source.count("\n", 0, match.start()) + 1
        if suffix == "[][]":
            rows = [_parse_row(row.group("body")) for row in ROW_RE.finditer(body)]
            residue = ROW_RE.sub("", body).replace(",", "").strip()
            if residue:
                raise ParseError(f"{name}: unsupported two-dimensional initializer residue")
            yield {
                "name": name,
                "line": line,
                "dimensions": 2,
                "length": len(rows),
                "row_widths": sorted(set(map(len, rows))),
                "hex": ["".join(f"{value:02X}" for value in row) for row in rows],
            }
        else:
            values = _parse_row(body)
            yield {
                "name": name,
                "line": line,
                "dimensions": 1,
                "length": len(values),
                "hex": "".join(f"{value:02X}" for value in values),
            }


def inventory(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    source = raw.decode("utf-8")
    arrays = list(iter_arrays(source))
    return {
        "schema_version": 1,
        "source": {
            "name": path.name,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        "array_count": len(arrays),
        "arrays": arrays,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("java_source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--length", type=int, help="emit only arrays with this element count")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = inventory(args.java_source)
    if args.length is not None:
        report["arrays"] = [
            array for array in report["arrays"] if array["length"] == args.length
        ]
        report["array_count"] = len(report["arrays"])
        report["length_filter"] = args.length
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
