from decimal import Decimal

import pytest

from projects.ecu_mapping import zf9hp


def value(did: int, payload: str) -> Decimal:
    decoded = zf9hp.decode_payload(did, bytes.fromhex(payload))
    assert len(decoded) == 1
    return decoded[0].value


def test_catalog_preserves_all_56_alfaobd_rows_and_repeated_composite_dids():
    assert [definition.order for definition in zf9hp.CATALOG] == list(range(1, 57))
    assert len(zf9hp.CATALOG) == 56
    assert [definition.did for definition in zf9hp.CATALOG[-6:]] == [
        0x213D,
        0x213D,
        0x213D,
        0x213E,
        0x213E,
        0x213E,
    ]


def test_priority_speed_and_temperature_formulas_match_zf9hp_decoder():
    assert value(0xF40C, "0FA0") == Decimal("1000")
    assert value(0x0500, "FFF0") == Decimal("-16")
    assert value(0x2102, "1000") == Decimal("1024")
    assert value(0x2103, "0FA0") == Decimal("1000")
    assert value(0xF405, "68") == Decimal("64")
    assert value(0x0301, "28") == Decimal("0")
    assert value(0x04FE, "57") == Decimal("47")


def test_priority_torque_formulas_match_zf9hp_decoder():
    for did in (0x1018, 0x101A, 0x101B, 0x101F, 0x1020):
        assert value(did, "0258") == Decimal("100")
    assert value(0x101D, "0960") == Decimal("100")


def test_composite_adaptation_response_decodes_three_signed_big_endian_values():
    decoded = zf9hp.decode_payload(0x213D, bytes.fromhex("0064FF9C7FFF"))
    assert [item.raw_value for item in decoded] == [100, -100, 32767]
    assert [item.value for item in decoded] == [
        Decimal("100"),
        Decimal("-100"),
        Decimal("32767"),
    ]


def test_complete_positive_response_validates_sid_and_payload_length():
    decoded = zf9hp.decode_positive_response(bytes.fromhex("6204FE57"))
    assert decoded[0].definition.key == "gearbox_oil_temperature"
    assert decoded[0].value == Decimal("47")

    with pytest.raises(ValueError, match="expected positive"):
        zf9hp.decode_positive_response(bytes.fromhex("7F2231"))
    with pytest.raises(ValueError, match="at least 2 payload"):
        zf9hp.decode_positive_response(bytes.fromhex("62F40C0F"))
    with pytest.raises(KeyError, match="unknown ZF9HP DID"):
        zf9hp.decode_positive_response(bytes.fromhex("62FFFF00"))
