import pytest

from tools.dex_field_usage import (
    DexError,
    allowed_opcodes_for_type,
    analyze_string_indexes,
    iter_instruction_offsets,
    read_uleb128,
    scan_all_fields_in_code_item,
    scan_code_item,
    scan_code_item_for_string,
)


@pytest.mark.parametrize(
    ("encoded", "expected"),
    [
        (b"\x00", 0),
        (b"\x7f", 127),
        (b"\x80\x01", 128),
        (b"\xe5\x8e\x26", 624485),
    ],
)
def test_read_uleb128(encoded: bytes, expected: int) -> None:
    value, offset = read_uleb128(encoded, 0)
    assert value == expected
    assert offset == len(encoded)


def test_read_uleb128_rejects_truncated_value() -> None:
    with pytest.raises(DexError, match="truncated"):
        read_uleb128(b"\x80", 0)


def test_scan_code_item_finds_static_object_field_reference() -> None:
    # Standard code_item header followed by:
    # const/4 v0, #0; sget-object v0, field@1234; return-object v0.
    header = (
        b"\x01\x00"  # registers_size
        b"\x00\x00"  # ins_size
        b"\x00\x00"  # outs_size
        b"\x00\x00"  # tries_size
        b"\x00\x00\x00\x00"  # debug_info_off
        b"\x04\x00\x00\x00"  # insns_size
    )
    instructions = b"\x12\x00\x62\x00\x34\x12\x11\x00"

    hits = scan_code_item(header + instructions, 0, 0x1234)

    assert hits == [
        {
            "code_unit_offset": 1,
            "opcode": "sget-object",
            "raw_units": ["0012", "0062", "1234", "0011"],
        }
    ]


def test_object_type_filter_excludes_impossible_wide_field_opcode() -> None:
    assert 0x62 in allowed_opcodes_for_type("[[B")
    assert 0x5A not in allowed_opcodes_for_type("[[B")


def test_scan_all_fields_filters_instruction_by_field_type() -> None:
    header = (
        b"\x01\x00\x00\x00\x00\x00\x00\x00"
        b"\x00\x00\x00\x00"
        b"\x04\x00\x00\x00"
    )
    instructions = b"\x62\x00\x00\x00\x5a\x00\x00\x00"
    fields = [(0, 1, 0)]
    strings = ["items"]
    types = ["LExample;", "[[B"]

    hits = scan_all_fields_in_code_item(
        header + instructions, 0, fields, strings, types
    )

    assert len(hits) == 1
    assert hits[0]["opcode"] == "sget-object"
    assert hits[0]["field_name"] == "items"


def test_scan_all_fields_applies_window_and_owner_filter() -> None:
    header = (
        b"\x01\x00\x00\x00\x00\x00\x00\x00"
        b"\x00\x00\x00\x00"
        b"\x07\x00\x00\x00"
    )
    # sget-object field@0; sget-object field@1; sget-object field@0; return.
    instructions = (
        b"\x62\x00\x00\x00"
        b"\x62\x00\x01\x00"
        b"\x62\x00\x00\x00"
        b"\x0e\x00"
    )
    fields = [(0, 2, 0), (1, 2, 1)]
    strings = ["first", "second"]
    types = ["LOne;", "LTwo;", "[B"]

    hits = scan_all_fields_in_code_item(
        header + instructions,
        0,
        fields,
        strings,
        types,
        start_code_unit=2,
        end_code_unit=6,
        field_class_filter="LOne;",
    )

    assert [(hit["code_unit_offset"], hit["field_name"]) for hit in hits] == [
        (4, "first")
    ]


def test_instruction_offsets_skip_packed_switch_payload_data() -> None:
    # nop; packed-switch-payload(size=1, first_key=0, target=0); return-void
    units = [0x0000, 0x0100, 1, 0, 0, 0, 0, 0x000E]
    assert list(iter_instruction_offsets(units)) == [0, 1, 7]


def test_string_index_lookup_rejects_out_of_bounds(monkeypatch) -> None:
    monkeypatch.setattr(
        "tools.dex_field_usage._identifiers",
        lambda _data: (["zero"], [], [], []),
    )

    with pytest.raises(DexError, match="out of bounds"):
        analyze_string_indexes(b"dex", [1])


def test_scan_code_item_finds_const_string_and_jumbo() -> None:
    header = (
        b"\x01\x00\x00\x00\x00\x00\x00\x00"
        b"\x00\x00\x00\x00"
        b"\x06\x00\x00\x00"
    )
    instructions = (
        b"\x1a\x00\x34\x12"
        b"\x1b\x00\x78\x56\x34\x12"
        b"\x0e\x00"
    )

    hits = scan_code_item_for_string(
        header + instructions,
        0,
        frozenset({0x1234, 0x12345678}),
    )

    assert [(hit["opcode"], hit["string_index"]) for hit in hits] == [
        ("const-string", 0x1234),
        ("const-string/jumbo", 0x12345678),
    ]
