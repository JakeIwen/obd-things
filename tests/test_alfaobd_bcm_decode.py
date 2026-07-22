from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from tools.alfaobd_bcm_decode import (
    classify_response,
    decode_field,
    decode_positive,
    extract_bits,
    load_catalog,
    load_inventory_evidence,
    load_module_map_evidence,
)


CATALOG_SCHEMA = """
CREATE TABLE ver(version_code INTEGER, version_name TEXT);
INSERT INTO ver VALUES(134, '2.4.4.0');
CREATE TABLE FGA_BCM_DATA(
    request TEXT NOT NULL,
    request_name TEXT,
    bit_pos TEXT NOT NULL,
    response_name TEXT NOT NULL,
    bit_len TEXT NOT NULL,
    lower_level TEXT,
    upper_level TEXT,
    slope TEXT,
    offset TEXT,
    unit TEXT,
    table_value TEXT,
    table_name TEXT,
    hex TEXT,
    device_id TEXT NOT NULL
);
"""


def catalog_row(
    request,
    bit_pos,
    response_name,
    bit_len,
    *,
    device_id="55851",
    lower="",
    upper="",
    slope="",
    offset="",
    unit="",
    table_value="",
    table_name="",
    hex_hint="",
):
    return (
        request,
        "(request)",
        str(bit_pos),
        response_name,
        str(bit_len),
        lower,
        upper,
        slope,
        offset,
        unit,
        table_value,
        table_name,
        hex_hint,
        device_id,
    )


class AlfaObdBcmDecodeTests(unittest.TestCase):
    def make_catalog(self, rows):
        temporary = tempfile.TemporaryDirectory()
        path = Path(temporary.name) / "catalog.db"
        connection = sqlite3.connect(path)
        connection.executescript(CATALOG_SCHEMA)
        connection.executemany(
            "INSERT INTO FGA_BCM_DATA VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        connection.commit()
        connection.close()
        self.addCleanup(temporary.cleanup)
        return path

    def test_response_classification_requires_exact_did_echo(self):
        positive = classify_response("22 F1 90", "62 F1 90 01 02")
        self.assertEqual(positive["category"], "positive")
        self.assertEqual(positive["data_hex"], "0102")

        negative = classify_response("22F190", "7F 22 31")
        self.assertEqual(negative["category"], "negative")
        self.assertEqual(negative["nrc"], "31")
        self.assertEqual(negative["nrc_name"], "requestOutOfRange")

        pending = classify_response("22F190", "7F 22 78")
        self.assertEqual(pending["category"], "pending")
        self.assertEqual(pending["status_family"], "pending")

        mismatch = classify_response("22F190", "62 F1 91 01")
        self.assertEqual(mismatch["category"], "echo_mismatch")
        self.assertEqual(mismatch["status_family"], "invalid")

        truncated = classify_response("22F190", "62F1900")
        self.assertEqual(truncated["category"], "positive_malformed")
        self.assertEqual(truncated["status_family"], "positive")

        timeout = classify_response("22F190", "", category_hint="timeout")
        self.assertEqual(timeout["category"], "timeout")
        self.assertEqual(timeout["status_family"], "timeout")
        transport = classify_response(
            "22F190", "", category_hint="transport_error"
        )
        self.assertEqual(transport["status_family"], "transport_error")

    def test_extract_bits_uses_msb0_over_the_complete_response(self):
        response = "62 01 30 A5 5A"
        self.assertEqual(extract_bits(response, 0, 8), 0x62)
        self.assertEqual(extract_bits(response, 24, 8), 0xA5)
        self.assertEqual(extract_bits(response, 28, 8), 0x55)
        with self.assertRaisesRegex(ValueError, "exceed"):
            extract_bits(response, 39, 2)

    def test_load_catalog_uses_exact_csv_membership_without_trailing_comma(self):
        database = self.make_catalog(
            [
                catalog_row(
                    "220130", 24, "(state)", 1,
                    table_value="0", table_name="(off)", device_id="55851"
                ),
                catalog_row(
                    "220130", 24, "(state)", 1,
                    table_value="1", table_name="(on)", device_id="55851"
                ),
                catalog_row(
                    "220131", 24, "(temperature)", 8,
                    lower="0", upper="255", slope="0.5", offset="-40.0",
                    unit="5", device_id="56056, 55851,"
                ),
                catalog_row(
                    "220132", 24, "(wrong device)", 8,
                    device_id="155851,558510,"
                ),
            ]
        )

        requests, metadata = load_catalog(database)
        self.assertEqual(set(requests), {"220130", "220131"})
        self.assertEqual(metadata["catalog_row_count"], 3)
        enum_field = requests["220130"]["fields"][0]
        self.assertEqual(enum_field["encoding"], "enum")
        self.assertEqual(len(enum_field["enum_choices_raw"]), 2)
        numeric_field = requests["220131"]["fields"][0]
        self.assertEqual(numeric_field["encoding"], "numeric")
        self.assertEqual(numeric_field["numeric_catalog_raw"]["slope"], ["0.5"])

    def test_decode_positive_groups_enum_and_applies_catalog_arithmetic(self):
        definition = {
            "request": "220130",
            "fields": [
                {
                    "bit_pos": 24,
                    "bit_len": 1,
                    "response_name_raw": "(state)",
                    "encoding": "enum",
                    "catalog_rowids": [1, 2],
                    "enum_choices_raw": [
                        {"table_value_raw": "0", "table_name_raw": "(off)", "catalog_rowid": 1},
                        {"table_value_raw": "1", "table_name_raw": "(on)", "catalog_rowid": 2},
                    ],
                },
                {
                    "bit_pos": 32,
                    "bit_len": 8,
                    "response_name_raw": "(temperature)",
                    "encoding": "numeric",
                    "catalog_rowids": [3],
                    "numeric_catalog_raw": {
                        "lower_level": ["0"],
                        "upper_level": ["255"],
                        "slope": ["0.5"],
                        "offset": ["-40.0"],
                        "unit": ["5"],
                    },
                },
            ],
        }

        decoded = decode_positive(definition, "62 01 30 80 64")
        self.assertEqual(decoded["decoded_field_count"], 2)
        self.assertEqual(decoded["fields"][0]["raw_value"], 1)
        self.assertEqual(decoded["fields"][0]["enum_match_status"], "matched_raw_catalog_entry")
        self.assertEqual(decoded["fields"][1]["raw_value"], 100)
        self.assertEqual(decoded["fields"][1]["numeric"]["scaled_value_decimal"], "10.0")

    def test_malformed_numeric_catalog_value_is_reported_not_guessed(self):
        field = {
            "bit_pos": 24,
            "bit_len": 8,
            "response_name_raw": "(malformed slope)",
            "encoding": "numeric",
            "catalog_rowids": [1],
            "numeric_catalog_raw": {
                "lower_level": ["0"],
                "upper_level": ["255"],
                "slope": ["0.10.0"],
                "offset": ["2"],
                "unit": ["40"],
            },
        }
        decoded = decode_field(field, "62 10 04 7F")
        self.assertEqual(
            decoded["numeric"]["scaling_status"],
            "invalid_or_conflicting_catalog_number",
        )
        self.assertNotIn("scaled_value_decimal", decoded["numeric"])

    def test_inventory_requires_matching_endpoint_and_carries_session_provenance(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        results = root / "dids_example.results.jsonl"
        results.write_text(
            json.dumps(
                {
                    "did": "40A3",
                    "request_hex": "22 40 A3",
                    "response_hex": "62 40 A3 01",
                    "category": "positive",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "did": "40A4",
                    "request_hex": "22 40 A4",
                    "response_hex": "",
                    "category": "timeout",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        summary = root / "dids_example.summary.json"
        summary.write_text(
            json.dumps(
                {
                    "module": {
                        "key": "bcm_ccan",
                        "txid": "18DA40F1",
                        "rxid": "18DAF140",
                        "addressing_mode": "normal_29bits",
                        "bus": "c-can",
                        "bitrate": 500000,
                        "channel": "can0",
                    },
                    "results_jsonl": str(results),
                    "results_written": 2,
                    "requested_session": "03",
                    "session_state": "explicit_session_confirmed",
                    "diagnostic_session_policy": "explicit session change",
                    "conditions": "ignition ON, engine OFF",
                    "physical_pair": "6/14",
                    "status": "complete",
                    "partial": False,
                    "fatal_error": None,
                    "restored_passive": True,
                    "started_at": "2026-07-21T01:00:00-06:00",
                    "completed_at": "2026-07-21T01:01:00-06:00",
                }
            ),
            encoding="utf-8",
        )

        evidence, summaries, audit = load_inventory_evidence(
            [results], expected_txid="18DA40F1", expected_rxid="18DAF140"
        )
        self.assertEqual(summaries, [summary])
        self.assertEqual(audit["evidence_rows_loaded"], 2)
        self.assertEqual(audit["category_hint_mismatch_count"], 0)
        self.assertEqual(audit["noncomplete_or_failed_campaign_count"], 0)
        self.assertEqual(evidence[0].module_key, "bcm_ccan")
        self.assertEqual(evidence[0].module_bus, "c-can")
        self.assertEqual(evidence[0].module_bitrate, 500000)
        self.assertEqual(evidence[0].physical_pair, "6/14")
        self.assertEqual(evidence[0].requested_session, "03")
        self.assertEqual(evidence[0].session_state, "explicit_session_confirmed")
        self.assertEqual(evidence[0].conditions, "ignition ON, engine OFF")
        timeout = classify_response(
            evidence[1].request,
            evidence[1].response,
            category_hint=evidence[1].category_hint,
        )
        self.assertEqual(timeout["status_family"], "timeout")

        bad = json.loads(summary.read_text(encoding="utf-8"))
        bad["module"]["key"] = "tcm"
        summary.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "module mismatch"):
            load_inventory_evidence(
                [results], expected_txid="18DA40F1", expected_rxid="18DAF140"
            )

        bad["module"]["key"] = "bcm_ccan"
        bad["module"]["txid"] = "18DA18F1"
        summary.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "endpoint mismatch"):
            load_inventory_evidence(
                [results], expected_txid="18DA40F1", expected_rxid="18DAF140"
            )

    def test_module_map_does_not_assign_aggregate_reads_to_one_prefix(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "module_map.txt"
        path.write_text(
            "## ATSH DA40F1\n"
            "  2240A3 reads=3 resp=6240A301 (+1 more)\n",
            encoding="utf-8",
        )
        evidence = load_module_map_evidence(path)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].occurrences, 1)
        self.assertEqual(evidence[0].request_occurrences_total, 3)


if __name__ == "__main__":
    unittest.main()
