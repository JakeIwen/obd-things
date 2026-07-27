from pathlib import Path

import pytest

from tools.alfaobd_java_arrays import ParseError, inventory, iter_arrays


def test_inventories_one_and_two_dimensional_byte_arrays(tmp_path: Path) -> None:
    source = """
abstract class Example {
    static byte[] first = {1, -1, Ascii.DLE, SignedBytes.MAX_POWER_OF_TWO,
        UnsignedBytes.MAX_POWER_OF_TWO};
    public static byte[][] second = {
        new byte[]{Ascii.CAN, 1},
        new byte[]{-48, Ascii.DEL}
    };
}
"""
    path = tmp_path / "Example.java"
    path.write_text(source, encoding="utf-8")

    report = inventory(path)

    assert report["array_count"] == 2
    assert report["arrays"][0] == {
        "name": "first",
        "line": 3,
        "dimensions": 1,
        "length": 5,
        "hex": "01FF104080",
    }
    assert report["arrays"][1] == {
        "name": "second",
        "line": 5,
        "dimensions": 2,
        "length": 2,
        "row_widths": [2],
        "hex": ["1801", "D07F"],
    }


def test_rejects_unsupported_literal() -> None:
    with pytest.raises(ParseError, match="unsupported byte literal"):
        list(iter_arrays("static byte[] bad = {(byte) 200};"))


def test_rejects_nonliteral_two_dimensional_residue() -> None:
    with pytest.raises(ParseError, match="initializer residue"):
        list(iter_arrays("static byte[][] bad = {new byte[]{1}, helper()};"))
