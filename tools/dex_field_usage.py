#!/usr/bin/env python3
"""Find bytecode references to one field in an owner-supplied APK or DEX.

The parser is intentionally narrow: it reads the standard DEX identifier,
class-data, and code-item structures and reports ordinary iget/iput/sget/sput
field-reference instructions.  It does not decompile methods or emit code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path


NO_INDEX = 0xFFFFFFFF
FIELD_OPCODES = {
    0x52: "iget",
    0x53: "iget-wide",
    0x54: "iget-object",
    0x55: "iget-boolean",
    0x56: "iget-byte",
    0x57: "iget-char",
    0x58: "iget-short",
    0x59: "iput",
    0x5A: "iput-wide",
    0x5B: "iput-object",
    0x5C: "iput-boolean",
    0x5D: "iput-byte",
    0x5E: "iput-char",
    0x5F: "iput-short",
    0x60: "sget",
    0x61: "sget-wide",
    0x62: "sget-object",
    0x63: "sget-boolean",
    0x64: "sget-byte",
    0x65: "sget-char",
    0x66: "sget-short",
    0x67: "sput",
    0x68: "sput-wide",
    0x69: "sput-object",
    0x6A: "sput-boolean",
    0x6B: "sput-byte",
    0x6C: "sput-char",
    0x6D: "sput-short",
}

OPCODE_WIDTHS = (
    [1, 1, 2, 3, 1, 2, 3, 1, 2, 3]
    + [1] * 9
    + [2, 3, 2, 2, 3, 5, 2, 2, 3, 2, 1, 1, 2, 2, 1, 2, 2, 3, 3, 3, 1, 1, 2, 3, 3, 3]
    + [2] * 5
    + [2] * 12
    + [1] * 6
    + [2] * 14
    + [2] * 14
    + [2] * 14
    + [3] * 5
    + [1]
    + [3] * 5
    + [1] * 2
    + [1] * 21
    + [2] * 32
    + [1] * 32
    + [2] * 19
    + [1] * 23
    + [4, 4, 3, 3, 2, 2]
)
if len(OPCODE_WIDTHS) != 256:  # pragma: no cover - import-time invariant
    raise RuntimeError(f"invalid DEX opcode width table: {len(OPCODE_WIDTHS)}")


class DexError(ValueError):
    """Raised when the required DEX structures are invalid or unsupported."""


@dataclass(frozen=True)
class MethodCode:
    method_index: int
    code_offset: int


def read_uleb128(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for count in range(5):
        if offset >= len(data):
            raise DexError("truncated ULEB128")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << (count * 7)
        if not byte & 0x80:
            return value, offset
    raise DexError("ULEB128 exceeds five bytes")


def _u16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise DexError("truncated uint16")
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise DexError("truncated uint32")
    return struct.unpack_from("<I", data, offset)[0]


def _table(data: bytes, size_offset: int, item_size: int) -> tuple[int, int]:
    size = _u32(data, size_offset)
    offset = _u32(data, size_offset + 4)
    if offset + size * item_size > len(data):
        raise DexError("DEX identifier table is out of bounds")
    return size, offset


def _decode_mutf8(data: bytes, offset: int) -> str:
    _, offset = read_uleb128(data, offset)
    end = data.find(b"\x00", offset)
    if end < 0:
        raise DexError("unterminated DEX string")
    # DEX uses modified UTF-8 (notably C0 80 for an embedded NUL and CESU-8
    # surrogate pairs). Class descriptors and member names are ASCII, which is
    # all this narrow lookup needs; replacement decoding safely preserves them
    # while allowing unrelated non-standard strings elsewhere in the table.
    return data[offset:end].decode("utf-8", errors="replace")


def _identifiers(data: bytes) -> tuple[list[str], list[str], list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    if not data.startswith(b"dex\n") or len(data) < 0x70:
        raise DexError("not a standard DEX file")
    string_count, string_offset = _table(data, 0x38, 4)
    type_count, type_offset = _table(data, 0x40, 4)
    field_count, field_offset = _table(data, 0x50, 8)
    method_count, method_offset = _table(data, 0x58, 8)

    strings = [
        _decode_mutf8(data, _u32(data, string_offset + index * 4))
        for index in range(string_count)
    ]
    types = [
        strings[_u32(data, type_offset + index * 4)]
        for index in range(type_count)
    ]
    fields = [
        (
            _u16(data, field_offset + index * 8),
            _u16(data, field_offset + index * 8 + 2),
            _u32(data, field_offset + index * 8 + 4),
        )
        for index in range(field_count)
    ]
    methods = [
        (
            _u16(data, method_offset + index * 8),
            _u16(data, method_offset + index * 8 + 2),
            _u32(data, method_offset + index * 8 + 4),
        )
        for index in range(method_count)
    ]
    return strings, types, fields, methods


def _method_code_items(data: bytes) -> list[MethodCode]:
    class_count, class_offset = _table(data, 0x60, 32)
    result: list[MethodCode] = []
    for class_index in range(class_count):
        class_data_offset = _u32(data, class_offset + class_index * 32 + 24)
        if not class_data_offset:
            continue
        offset = class_data_offset
        static_count, offset = read_uleb128(data, offset)
        instance_count, offset = read_uleb128(data, offset)
        direct_count, offset = read_uleb128(data, offset)
        virtual_count, offset = read_uleb128(data, offset)
        for _ in range(static_count + instance_count):
            _, offset = read_uleb128(data, offset)
            _, offset = read_uleb128(data, offset)
        for method_count in (direct_count, virtual_count):
            method_index = 0
            for _ in range(method_count):
                method_delta, offset = read_uleb128(data, offset)
                method_index += method_delta
                _, offset = read_uleb128(data, offset)
                code_offset, offset = read_uleb128(data, offset)
                if code_offset:
                    result.append(MethodCode(method_index, code_offset))
    return result


def allowed_opcodes_for_type(type_descriptor: str) -> frozenset[int]:
    if type_descriptor.startswith(("L", "[")):
        return frozenset({0x54, 0x5B, 0x62, 0x69})
    return {
        "J": frozenset({0x53, 0x5A, 0x61, 0x68}),
        "D": frozenset({0x53, 0x5A, 0x61, 0x68}),
        "Z": frozenset({0x55, 0x5C, 0x63, 0x6A}),
        "B": frozenset({0x56, 0x5D, 0x64, 0x6B}),
        "C": frozenset({0x57, 0x5E, 0x65, 0x6C}),
        "S": frozenset({0x58, 0x5F, 0x66, 0x6D}),
        "I": frozenset({0x52, 0x59, 0x60, 0x67}),
        "F": frozenset({0x52, 0x59, 0x60, 0x67}),
    }.get(type_descriptor, frozenset(FIELD_OPCODES))


def iter_instruction_offsets(units: list[int]):
    index = 0
    while index < len(units):
        unit = units[index]
        opcode = unit & 0xFF
        if opcode == 0 and unit in {0x0100, 0x0200, 0x0300}:
            if index + 1 >= len(units):
                raise DexError("truncated pseudo-instruction payload")
            size = units[index + 1]
            if unit == 0x0100:
                width = 4 + size * 2
            elif unit == 0x0200:
                width = 2 + size * 4
            else:
                if index + 3 >= len(units):
                    raise DexError("truncated fill-array-data payload")
                element_width = size
                element_count = units[index + 2] | (units[index + 3] << 16)
                width = 4 + (element_width * element_count + 1) // 2
        else:
            width = OPCODE_WIDTHS[opcode]
        if width <= 0 or index + width > len(units):
            raise DexError(
                f"invalid instruction width {width} for opcode 0x{opcode:02X}"
            )
        yield index
        index += width


def scan_code_item(
    data: bytes,
    code_offset: int,
    field_index: int,
    *,
    allowed_opcodes: frozenset[int] | None = None,
) -> list[dict[str, object]]:
    if code_offset + 16 > len(data):
        raise DexError("code item header is out of bounds")
    insns_size = _u32(data, code_offset + 12)
    insns_offset = code_offset + 16
    if insns_offset + insns_size * 2 > len(data):
        raise DexError("code item instructions are out of bounds")
    units = [
        _u16(data, insns_offset + index * 2)
        for index in range(insns_size)
    ]
    hits = []
    for index in iter_instruction_offsets(units):
        opcode = units[index] & 0xFF
        if (
            opcode in FIELD_OPCODES
            and index + 1 < len(units)
            and (allowed_opcodes is None or opcode in allowed_opcodes)
            and units[index + 1] == field_index
        ):
            hits.append(
                {
                    "code_unit_offset": index,
                    "opcode": FIELD_OPCODES[opcode],
                    "raw_units": [f"{unit:04X}" for unit in units[max(0, index - 2) : index + 4]],
                }
            )
    return hits


def analyze_dex(data: bytes, class_descriptor: str, field_name: str) -> dict[str, object]:
    strings, types, fields, methods = _identifiers(data)
    matches = [
        index
        for index, (class_index, _, name_index) in enumerate(fields)
        if types[class_index] == class_descriptor and strings[name_index] == field_name
    ]
    if len(matches) != 1:
        raise DexError(
            f"expected one field {class_descriptor}->{field_name}, found {len(matches)}"
        )
    target_index = matches[0]
    target_class, target_type, target_name = fields[target_index]
    target_type_descriptor = types[target_type]
    allowed_opcodes = allowed_opcodes_for_type(target_type_descriptor)
    usages = []
    for method_code in _method_code_items(data):
        for hit in scan_code_item(
            data,
            method_code.code_offset,
            target_index,
            allowed_opcodes=allowed_opcodes,
        ):
            method_class, _, method_name = methods[method_code.method_index]
            usages.append(
                {
                    "method_index": method_code.method_index,
                    "method_class": types[method_class],
                    "method_name": strings[method_name],
                    "code_offset": method_code.code_offset,
                    **hit,
                }
            )
    return {
        "mode": "field_usages",
        "field_index": target_index,
        "field_class": types[target_class],
        "field_type": target_type_descriptor,
        "field_name": strings[target_name],
        "usage_count": len(usages),
        "usages": usages,
        "scan_note": (
            "ordinary field-reference opcodes are detected without full instruction "
            "disassembly; raw units are retained for manual false-positive review"
        ),
    }


def scan_all_fields_in_code_item(
    data: bytes,
    code_offset: int,
    fields: list[tuple[int, int, int]],
    strings: list[str],
    types: list[str],
    *,
    start_code_unit: int = 0,
    end_code_unit: int | None = None,
    field_class_filter: str | None = None,
) -> list[dict[str, object]]:
    if code_offset + 16 > len(data):
        raise DexError("code item header is out of bounds")
    insns_size = _u32(data, code_offset + 12)
    insns_offset = code_offset + 16
    if insns_offset + insns_size * 2 > len(data):
        raise DexError("code item instructions are out of bounds")
    units = [_u16(data, insns_offset + index * 2) for index in range(insns_size)]
    hits = []
    for index in iter_instruction_offsets(units):
        if index < start_code_unit:
            continue
        if end_code_unit is not None and index >= end_code_unit:
            continue
        opcode = units[index] & 0xFF
        if index + 1 >= len(units):
            continue
        field_index = units[index + 1]
        if opcode not in FIELD_OPCODES or field_index >= len(fields):
            continue
        field_class, field_type, field_name = fields[field_index]
        type_descriptor = types[field_type]
        if (
            field_class_filter is not None
            and types[field_class] != field_class_filter
        ):
            continue
        if opcode not in allowed_opcodes_for_type(type_descriptor):
            continue
        hits.append(
            {
                "code_unit_offset": index,
                "opcode": FIELD_OPCODES[opcode],
                "field_index": field_index,
                "field_class": types[field_class],
                "field_type": type_descriptor,
                "field_name": strings[field_name],
                "raw_units": [
                    f"{unit:04X}" for unit in units[max(0, index - 2) : index + 4]
                ],
            }
        )
    return hits


def analyze_method_fields(
    data: bytes,
    method_class_descriptor: str,
    method_name: str,
    *,
    start_code_unit: int = 0,
    end_code_unit: int | None = None,
    field_class_filter: str | None = None,
) -> dict[str, object]:
    strings, types, fields, methods = _identifiers(data)
    code_by_method: dict[int, list[int]] = {}
    for method_code in _method_code_items(data):
        code_by_method.setdefault(method_code.method_index, []).append(method_code.code_offset)
    matches = [
        index
        for index, (class_index, _, name_index) in enumerate(methods)
        if types[class_index] == method_class_descriptor
        and strings[name_index] == method_name
        and index in code_by_method
    ]
    if not matches:
        raise DexError(
            f"found no coded method {method_class_descriptor}->{method_name}"
        )
    matched_methods = []
    for method_index in matches:
        hits = []
        for code_offset in code_by_method[method_index]:
            for hit in scan_all_fields_in_code_item(
                data,
                code_offset,
                fields,
                strings,
                types,
                start_code_unit=start_code_unit,
                end_code_unit=end_code_unit,
                field_class_filter=field_class_filter,
            ):
                hits.append({"code_offset": code_offset, **hit})
        matched_methods.append(
            {
                "method_index": method_index,
                "method_class": method_class_descriptor,
                "method_name": method_name,
                "field_reference_count": len(hits),
                "field_references": hits,
            }
        )
    return {
        "mode": "method_field_references",
        "method_class": method_class_descriptor,
        "method_name": method_name,
        "filters": {
            "start_code_unit": start_code_unit,
            "end_code_unit": end_code_unit,
            "field_class": field_class_filter,
        },
        "matching_method_count": len(matched_methods),
        "methods": matched_methods,
        "scan_note": (
            "ordinary type-compatible field-reference opcodes are detected "
            "without full instruction disassembly; raw units are retained for review"
        ),
    }


def analyze_string_indexes(data: bytes, indexes: list[int]) -> dict[str, object]:
    strings, _, _, _ = _identifiers(data)
    resolved = []
    for index in indexes:
        if index < 0 or index >= len(strings):
            raise DexError(
                f"string index {index} is out of bounds for {len(strings)} strings"
            )
        resolved.append({"index": index, "hex_index": f"0x{index:04X}", "value": strings[index]})
    return {
        "mode": "string_indexes",
        "string_count": len(strings),
        "strings": resolved,
    }


def scan_code_item_for_string(
    data: bytes,
    code_offset: int,
    string_indexes: frozenset[int],
) -> list[dict[str, object]]:
    if code_offset + 16 > len(data):
        raise DexError("code item header is out of bounds")
    insns_size = _u32(data, code_offset + 12)
    insns_offset = code_offset + 16
    if insns_offset + insns_size * 2 > len(data):
        raise DexError("code item instructions are out of bounds")
    units = [_u16(data, insns_offset + index * 2) for index in range(insns_size)]
    hits = []
    for index in iter_instruction_offsets(units):
        opcode = units[index] & 0xFF
        if opcode == 0x1A and index + 1 < len(units):
            string_index = units[index + 1]
            opcode_name = "const-string"
        elif opcode == 0x1B and index + 2 < len(units):
            string_index = units[index + 1] | (units[index + 2] << 16)
            opcode_name = "const-string/jumbo"
        else:
            continue
        if string_index not in string_indexes:
            continue
        hits.append(
            {
                "code_unit_offset": index,
                "opcode": opcode_name,
                "string_index": string_index,
                "raw_units": [
                    f"{unit:04X}" for unit in units[max(0, index - 2) : index + 5]
                ],
            }
        )
    return hits


def analyze_string_usages(data: bytes, value: str) -> dict[str, object]:
    strings, types, _, methods = _identifiers(data)
    indexes = frozenset(index for index, item in enumerate(strings) if item == value)
    if not indexes:
        raise DexError(f"DEX contains no exact string value {value!r}")
    usages = []
    for method_code in _method_code_items(data):
        for hit in scan_code_item_for_string(data, method_code.code_offset, indexes):
            method_class, _, method_name = methods[method_code.method_index]
            usages.append(
                {
                    "method_index": method_code.method_index,
                    "method_class": types[method_class],
                    "method_name": strings[method_name],
                    "code_offset": method_code.code_offset,
                    **hit,
                }
            )
    return {
        "mode": "string_usages",
        "value": value,
        "string_indexes": sorted(indexes),
        "usage_count": len(usages),
        "usages": usages,
    }


def load_dex(path: Path, dex_member: str) -> tuple[bytes, str]:
    raw = path.read_bytes()
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            try:
                return archive.read(dex_member), dex_member
            except KeyError as exc:
                raise DexError(f"APK has no {dex_member}") from exc
    return raw, path.name


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk_or_dex", type=Path)
    parser.add_argument("--class-descriptor")
    parser.add_argument("--field-name")
    parser.add_argument("--method-class")
    parser.add_argument("--method-name")
    parser.add_argument(
        "--string-index",
        type=lambda value: int(value, 0),
        action="append",
        help="resolve one DEX string index; repeat for multiple indexes",
    )
    parser.add_argument(
        "--string-value",
        help="find const-string references to this exact DEX string value",
    )
    parser.add_argument(
        "--start-code-unit",
        type=int,
        default=0,
        help="method mode: include references at or after this code-unit offset",
    )
    parser.add_argument(
        "--end-code-unit",
        type=int,
        help="method mode: exclude references at or after this code-unit offset",
    )
    parser.add_argument(
        "--field-class-filter",
        help="method mode: retain references owned by this exact DEX class descriptor",
    )
    parser.add_argument("--dex-member", default="classes.dex")
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    field_mode = bool(args.class_descriptor or args.field_name)
    method_mode = bool(args.method_class or args.method_name)
    string_index_mode = bool(args.string_index)
    string_usage_mode = args.string_value is not None
    if sum((field_mode, method_mode, string_index_mode, string_usage_mode)) != 1:
        raise DexError(
            "choose exactly one target: --class-descriptor/--field-name, "
            "--method-class/--method-name, one or more --string-index values, "
            "or --string-value"
        )
    if field_mode and not (args.class_descriptor and args.field_name):
        raise DexError("field mode requires --class-descriptor and --field-name")
    if method_mode and not (args.method_class and args.method_name):
        raise DexError("method mode requires --method-class and --method-name")
    if args.start_code_unit < 0:
        raise DexError("--start-code-unit must be non-negative")
    if (
        args.end_code_unit is not None
        and args.end_code_unit <= args.start_code_unit
    ):
        raise DexError("--end-code-unit must be greater than --start-code-unit")
    if not method_mode and (
        args.start_code_unit
        or args.end_code_unit is not None
        or args.field_class_filter is not None
    ):
        raise DexError("method-window filters require method mode")
    source_raw = args.apk_or_dex.read_bytes()
    dex, member = load_dex(args.apk_or_dex, args.dex_member)
    if field_mode:
        analysis = analyze_dex(dex, args.class_descriptor, args.field_name)
    elif method_mode:
        analysis = analyze_method_fields(
            dex,
            args.method_class,
            args.method_name,
            start_code_unit=args.start_code_unit,
            end_code_unit=args.end_code_unit,
            field_class_filter=args.field_class_filter,
        )
    elif string_index_mode:
        analysis = analyze_string_indexes(dex, args.string_index)
    else:
        analysis = analyze_string_usages(dex, args.string_value)
    report = {
        "schema_version": 1,
        "source": {
            "name": args.apk_or_dex.name,
            "size": len(source_raw),
            "sha256": hashlib.sha256(source_raw).hexdigest(),
            "dex_member": member,
            "dex_size": len(dex),
            "dex_sha256": hashlib.sha256(dex).hexdigest(),
        },
        **analysis,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
