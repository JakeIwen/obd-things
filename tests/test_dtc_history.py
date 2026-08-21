import json
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from lib import dtc
from lib.modules import MODULES


def scan(
    source_key,
    module_key,
    completed_at,
    *,
    records=(),
    outcome="success",
    unavailable_reason=None,
    channel="can7",
    status_mask=0xCF,
):
    module = MODULES[module_key]
    return dtc.ModuleScan(
        source_key=source_key,
        source_ref=f"memory:{source_key}",
        module_key=module.key,
        module_name=module.name,
        logical_bus=module.bus,
        resolved_channel=channel,
        bitrate=module.bitrate,
        started_at=completed_at,
        completed_at=completed_at,
        outcome=outcome,
        unavailable_reason=unavailable_reason,
        status_availability_mask=status_mask if outcome == "success" else None,
        dtcs=tuple(dtc.DtcRecord(raw, status) for raw, status in records),
        physical_pair="test fixture",
        conditions="offline test",
    )


def inventory_report(module_key, *, response=None, category="timeout"):
    module = MODULES[module_key]
    result = {
        "label": "dtcs_by_status",
        "request_hex": "19 02 FF",
        "response_hex": response,
        "category": category,
        "negative_response": None,
    }
    return {
        "tool": "tools/dtc_inventory.py",
        "clear_service_implemented": False,
        "diagnostic_session_control_sent": False,
        "started_at": "2026-08-20T10:00:00-06:00",
        "completed_at": "2026-08-20T10:00:01-06:00",
        "module": {
            "key": module.key,
            "name": module.name,
            "bus": module.bus,
            "channel": "can3",
            "bitrate": module.bitrate,
        },
        "physical_pair": "fixture",
        "conditions": "fixture",
        "interrupted": False,
        "fatal_error": None,
        "results": [result],
    }


class DtcStatusTests(unittest.TestCase):
    def test_exact_status_semantics_do_not_call_history_current(self):
        expected = {
            0x01: "current",
            0x04: "pending",
            0x08: "confirmed_history",
            0x0C: "pending",
            0x40: "incomplete_only",
            0x4D: "current",
            0x80: "warning_requested",
        }
        for value, group in expected.items():
            with self.subTest(status=value):
                self.assertEqual(dtc.status_semantics(value)["display_group"], group)
        self.assertFalse(dtc.status_semantics(0x08)["current"])
        self.assertFalse(dtc.status_semantics(0x40)["current"])
        self.assertTrue(dtc.status_semantics(0x4D)["pending"])

    def test_fca_display_keeps_failure_type_suffix(self):
        self.assertEqual(dtc.fca_dtc_name(bytes.fromhex("55 03 31")), "C1503-31")
        self.assertEqual(dtc.DtcRecord("D42787", 0x08).fca_display, "U1427-87")

    def test_strict_parser_distinguishes_positive_zero_from_malformed(self):
        mask, records = dtc.parse_dtc_list_response(bytes.fromhex("59 02 4F"))
        self.assertEqual(mask, 0x4F)
        self.assertEqual(records, ())
        with self.assertRaises(dtc.DtcParseError):
            dtc.parse_dtc_list_response(bytes.fromhex("59 02 4F 55"))
        with self.assertRaises(dtc.DtcParseError):
            dtc.parse_dtc_list_response(bytes.fromhex("59 03 4F"))

    def test_conflicting_duplicate_dtc_in_one_response_is_rejected(self):
        with self.assertRaisesRegex(dtc.DtcParseError, "conflicting"):
            dtc.parse_dtc_list_response(
                bytes.fromhex("59 02 FF 55 03 31 08 55 03 31 40")
            )

    def test_byte_identical_duplicate_is_deduplicated_for_known_tcm_quirk(self):
        _mask, records = dtc.parse_dtc_list_response(
            bytes.fromhex("59 02 FF C4 15 00 40 C4 15 00 40")
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0], dtc.DtcRecord("C41500", 0x40))


class InventoryImportTests(unittest.TestCase):
    def test_timeout_is_unavailable_not_zero_dtcs(self):
        converted = dtc.scan_from_inventory_report(
            inventory_report("pcm"), source_key="one", source_ref="fixture"
        )
        self.assertEqual(converted.outcome, "unavailable")
        self.assertEqual(converted.unavailable_reason, "timeout")
        self.assertEqual(converted.dtcs, ())
        self.assertEqual(converted.logical_bus, "c-can")
        self.assertEqual(converted.resolved_channel, "can3")

    def test_valid_empty_positive_is_explicit_success(self):
        converted = dtc.scan_from_inventory_report(
            inventory_report("rf_hub", response="59 02 CF", category="positive"),
            source_key="two",
            source_ref="fixture",
        )
        self.assertEqual(converted.outcome, "success")
        self.assertEqual(converted.status_availability_mask, 0xCF)
        self.assertEqual(converted.dtcs, ())

    def test_report_bus_must_match_registry(self):
        report = inventory_report("rf_hub", response="59 02 CF", category="positive")
        report["module"]["bus"] = "b-can"
        with self.assertRaisesRegex(dtc.DtcParseError, "does not match"):
            dtc.scan_from_inventory_report(report, source_key="three", source_ref="fixture")

    def test_reports_with_session_control_or_clear_capability_are_rejected(self):
        for key in ("clear_service_implemented", "diagnostic_session_control_sent"):
            report = inventory_report("rf_hub", response="59 02 CF", category="positive")
            report[key] = True
            with self.subTest(key=key), self.assertRaises(dtc.DtcParseError):
                dtc.scan_from_inventory_report(report, source_key=key, source_ref="fixture")


class DtcHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "dtcs.sqlite3"
        self.history = dtc.DtcHistory(self.path)
        self.addCleanup(self.history.close)

    def test_success_tracks_status_resolution_and_recurrence(self):
        self.history.record_scan(
            scan("s1", "rf_hub", "2026-08-20T10:00:00Z", records=(("550331", 0x40),))
        )
        self.history.record_scan(
            scan(
                "s2",
                "rf_hub",
                "2026-08-20T10:01:00Z",
                records=(("550331", 0x08), ("D42787", 0x01)),
            )
        )
        self.history.record_scan(scan("s3", "rf_hub", "2026-08-20T10:02:00Z"))
        self.history.record_scan(
            scan(
                "s4",
                "rf_hub",
                "2026-08-20T10:03:00Z",
                outcome="unavailable",
                unavailable_reason="timeout",
            )
        )
        self.history.record_scan(
            scan("s5", "rf_hub", "2026-08-20T10:04:00Z", records=(("550331", 0x01),))
        )

        module = next(
            item for item in self.history.snapshot()["modules"] if item["module_key"] == "rf_hub"
        )
        self.assertEqual(module["availability"], "available")
        self.assertEqual(module["result_state"], "dtcs_present")
        self.assertEqual(module["dtcs"][0]["raw_dtc"], "550331")
        self.assertEqual(module["dtcs"][0]["observation_count"], 3)
        self.assertEqual(module["dtcs"][0]["episode_count"], 2)
        self.assertEqual(module["dtcs"][0]["recurrence_count"], 1)
        self.assertEqual(
            [row["kind"] for row in self.history.transitions("rf_hub")],
            ["discovered", "status_changed", "discovered", "resolved", "resolved", "recurred"],
        )

    def test_unavailable_scan_preserves_last_successful_dtc(self):
        self.history.record_scan(
            scan("a1", "pcm", "2026-08-20T10:00:00Z", records=(("123456", 0x08),))
        )
        self.history.record_scan(
            scan(
                "a2",
                "pcm",
                "2026-08-20T10:01:00Z",
                outcome="unavailable",
                unavailable_reason="timeout",
            )
        )

        module = next(
            item for item in self.history.snapshot()["modules"] if item["module_key"] == "pcm"
        )
        self.assertEqual(module["availability"], "unavailable")
        self.assertEqual(module["result_state"], "unavailable")
        self.assertEqual(module["unavailable_reason"], "timeout")
        self.assertEqual(module["last_success_dtc_count"], 1)
        self.assertEqual(module["dtcs"][0]["raw_dtc"], "123456")
        self.assertTrue(module["dtcs"][0]["present"])
        self.assertEqual(
            module["dtcs"][0]["observation_state"], "stale_after_unavailable_attempt"
        )

    def test_positive_empty_response_is_no_dtcs(self):
        self.history.record_scan(scan("z1", "cluster", "2026-08-20T10:00:00Z"))
        module = next(
            item for item in self.history.snapshot()["modules"] if item["module_key"] == "cluster"
        )
        self.assertEqual(module["availability"], "available")
        self.assertEqual(module["result_state"], "no_dtcs")
        self.assertEqual(module["last_success_dtc_count"], 0)

    def test_narrower_status_mask_cannot_resolve_an_omitted_dtc(self):
        self.history.record_scan(
            scan(
                "m1",
                "cluster",
                "2026-08-20T10:00:00Z",
                records=(("550331", 0x40),),
                status_mask=0xCF,
            )
        )
        self.history.record_scan(
            scan("m2", "cluster", "2026-08-20T10:01:00Z", status_mask=0x0F)
        )
        module = next(
            item for item in self.history.snapshot()["modules"] if item["module_key"] == "cluster"
        )
        self.assertEqual(module["result_state"], "status_coverage_incomplete")
        self.assertFalse(module["absence_authoritative"])
        self.assertEqual(module["dtcs"][0]["observation_state"], "retained_incompatible_status_mask")
        self.assertNotIn("resolved", [row["kind"] for row in self.history.transitions("cluster")])

        self.history.record_scan(
            scan("m3", "cluster", "2026-08-20T10:02:00Z", status_mask=0xCF)
        )
        module = next(
            item for item in self.history.snapshot()["modules"] if item["module_key"] == "cluster"
        )
        self.assertEqual(module["result_state"], "no_dtcs")
        self.assertEqual(module["dtcs"], [])
        self.assertIn("resolved", [row["kind"] for row in self.history.transitions("cluster")])

    def test_zero_status_mask_is_not_an_authoritative_no_dtc_result(self):
        self.history.record_scan(
            scan("zero-mask", "cluster", "2026-08-20T10:00:00Z", status_mask=0)
        )
        module = next(
            item for item in self.history.snapshot()["modules"] if item["module_key"] == "cluster"
        )
        self.assertEqual(module["availability"], "available")
        self.assertEqual(module["result_state"], "status_coverage_incomplete")
        self.assertFalse(module["absence_authoritative"])

    def test_idempotent_source_does_not_double_count(self):
        item = scan("same", "cluster", "2026-08-20T10:00:00Z", records=(("550331", 8),))
        self.assertTrue(self.history.record_scan(item)["inserted"])
        self.assertFalse(self.history.record_scan(item)["inserted"])
        module = next(
            row for row in self.history.snapshot()["modules"] if row["module_key"] == "cluster"
        )
        self.assertEqual(module["successful_scans"], 1)
        self.assertEqual(module["dtcs"][0]["observation_count"], 1)

    def test_scan_batch_rolls_back_if_one_insert_fails(self):
        first = scan("batch-1", "cluster", "2026-08-20T10:00:00Z")
        second = scan("batch-2", "rf_hub", "2026-08-20T10:01:00Z")
        original = self.history._insert_scan
        calls = 0

        def fail_second(item):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic batch failure")
            return original(item)

        with mock.patch.object(self.history, "_insert_scan", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                self.history.record_scans((first, second))
        count = self.history.connection.execute("SELECT COUNT(*) FROM module_scans").fetchone()[0]
        self.assertEqual(count, 0)

    def test_out_of_order_import_rebuilds_chronologically(self):
        self.history.record_scan(scan("later", "cluster", "2026-08-20T10:01:00Z"))
        self.history.record_scan(
            scan("earlier", "cluster", "2026-08-20T10:00:00Z", records=(("550331", 4),))
        )
        all_states = self.history.snapshot(include_resolved=True)
        module = next(item for item in all_states["modules"] if item["module_key"] == "cluster")
        self.assertFalse(module["dtcs"][0]["present"])
        self.assertEqual(
            [row["kind"] for row in self.history.transitions("cluster")],
            ["discovered", "resolved"],
        )

    def test_dashboard_summary_groups_and_coverage(self):
        self.history.record_scan(
            scan(
                "groups",
                "rf_hub",
                "2026-08-20T10:00:00Z",
                records=(
                    ("100001", 0x01),
                    ("100002", 0x04),
                    ("100003", 0x08),
                    ("100004", 0x40),
                    ("100005", 0x20),
                ),
            )
        )
        self.history.record_scan(
            scan(
                "timeout",
                "pcm",
                "2026-08-20T10:01:00Z",
                outcome="unavailable",
                unavailable_reason="timeout",
            )
        )
        summary = self.history.dashboard_summary()
        self.assertEqual(summary["acquisition"], "cache_only")
        self.assertEqual(summary["coverage"]["available_modules"], 1)
        self.assertEqual(summary["coverage"]["unavailable_modules"], 1)
        self.assertEqual(
            {name: len(records) for name, records in summary["groups"].items()},
            {
                "current": 1,
                "pending": 1,
                "confirmed_history": 1,
                "incomplete_only": 1,
                "other": 1,
            },
        )
        self.assertEqual(summary["group_counts"]["current"], 1)
        current = summary["groups"]["current"][0]
        self.assertEqual(current["module_key"], "rf_hub")
        self.assertEqual(current["logical_bus"], "c-can")
        self.assertEqual(current["resolved_channel"], "can7")
        self.assertEqual(current["observation_state"], "observed_in_latest_success")

    def test_compact_summary_limits_records_but_keeps_counts_and_module_coverage(self):
        self.history.record_scan(
            scan(
                "compact",
                "rf_hub",
                "2026-08-20T10:00:00Z",
                records=(("100001", 0x40), ("100002", 0x40), ("100003", 0x40)),
            )
        )
        summary = self.history.dashboard_summary(compact=True, per_group_limit=2)
        self.assertTrue(summary["compact"])
        self.assertTrue(summary["groups_truncated"])
        self.assertEqual(summary["group_counts"]["incomplete_only"], 3)
        self.assertEqual(summary["group_returned_counts"]["incomplete_only"], 2)
        self.assertEqual(len(summary["modules"]), len(MODULES))
        self.assertTrue(all("dtcs" not in module for module in summary["modules"]))

    def test_compact_summary_limit_is_bounded(self):
        for value in (0, 1001, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.history.dashboard_summary(compact=True, per_group_limit=value)

    def test_atomic_cache_is_valid_json(self):
        destination = Path(self.temporary.name) / "nested" / "cache.json"
        payload = self.history.dashboard_summary()
        dtc.write_cache(destination, payload)
        self.assertEqual(json.loads(destination.read_text()), payload)


if __name__ == "__main__":
    unittest.main()
