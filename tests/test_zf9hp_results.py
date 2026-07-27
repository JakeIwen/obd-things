import json
from pathlib import Path

from tools import zf9hp_results


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def positive(did: str, response: str) -> dict[str, object]:
    return {
        "did": did,
        "request_hex": f"22 {did[:2]} {did[2:]}",
        "response_hex": response,
        "category": "positive",
        "status": "POSITIVE",
        "negative_response": None,
    }


def test_report_decodes_priority_values_and_american_display_units(tmp_path):
    source = tmp_path / "results.jsonl"
    write_jsonl(
        source,
        [
            positive("04FE", "62 04 FE 57"),
            positive("1018", "62 10 18 02 58"),
            positive("2106", "62 21 06 13 88"),
        ],
    )

    report = zf9hp_results.build_report(source)
    records = report["records"]
    assert records[0]["decoded"][0] == {
        "catalog_order": 16,
        "key": "gearbox_oil_temperature",
        "label": "Gearbox oil temperature",
        "raw_value": 87,
        "native_value": "47",
        "native_unit": "°C",
        "display_value": "116.6",
        "display_unit": "°F",
        "evidence_quality": "vendor_derived_static",
        "vehicle_support": "observed_positive_read",
    }
    assert records[1]["decoded"][0]["native_value"] == "100"
    assert records[1]["decoded"][0]["display_value"] == "73.7562149277"
    assert records[1]["decoded"][0]["display_unit"] == "lb-ft"
    assert records[2]["decoded"][0]["native_value"] == "50"
    assert records[2]["decoded"][0]["display_value"] == "31.06855961185"
    assert records[2]["decoded"][0]["display_unit"] == "mph"
    assert report["summary"]["decoded_signals"] == 3


def test_report_preserves_negatives_and_fails_closed_on_bad_positive_echo(tmp_path):
    source = tmp_path / "results.jsonl"
    write_jsonl(
        source,
        [
            {
                "did": "04FE",
                "request_hex": "22 04 FE",
                "response_hex": "7F 22 31",
                "category": "out_of_range_current_session",
                "status": "NEGATIVE",
                "negative_response": {"nrc": 0x31},
            },
            positive("04FE", "62 03 01 57"),
        ],
    )

    report = zf9hp_results.build_report(source)
    assert report["records"][0]["decode_status"] == "not_positive"
    assert report["records"][0]["decoded"] == []
    assert report["records"][1]["decode_status"] == "decode_error"
    assert "exact 62 04FE echo" in report["records"][1]["decode_error"]
    assert report["summary"]["decode_status_counts"] == {
        "decode_error": 1,
        "not_positive": 1,
    }


def test_main_writes_default_shape_to_requested_output(tmp_path):
    source = tmp_path / "results.jsonl"
    output = tmp_path / "report.json"
    write_jsonl(source, [positive("F405", "62 F4 05 68")])

    assert (
        zf9hp_results.main(
            [str(source), "--output", str(output)]
        )
        == 0
    )
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["profile"] == "ZF9HP"
    assert saved["summary"]["decoded_signals"] == 1
    assert saved["records"][0]["decoded"][0]["display_value"] == "147.2"
