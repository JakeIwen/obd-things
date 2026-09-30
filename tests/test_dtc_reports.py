import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lib import dtc_reports
from lib.dtc import DtcParseError
from tests.test_dtc_scan import report
from tools import dtc_batch, dtc_scan


class DtcReportTests(unittest.TestCase):
    def test_reexports_and_default_paths(self):
        self.assertIs(dtc_scan.load_inventory, dtc_reports.load_inventory)
        self.assertIs(dtc_batch.load_inventory, dtc_reports.load_inventory)
        for name in ("DEFAULT_CACHE", "DEFAULT_DB"):
            self.assertIs(getattr(dtc_scan, name), getattr(dtc_batch, name))
        self.assertEqual(dtc_scan.DEFAULT_DB,
                         os.path.join(dtc_scan.REPO, "tmp", "vehicle_data", "dtc-history.sqlite3"))
        self.assertEqual(dtc_scan.DEFAULT_CACHE,
                         os.path.join(dtc_scan.REPO, "tmp", "vehicle_data", "dtc-cache.json"))

    def test_exact_preview_and_semantic_source_identity(self):
        payload = report("rf_hub", "59 02 FF")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            raw = json.dumps(payload).encode("utf-8")
            path.write_bytes(raw)
            scan, preview = dtc_reports.load_inventory(str(path))
            self.assertEqual(preview, {
                "source_ref": str(path), "source_sha256": hashlib.sha256(raw).hexdigest(),
                "source_key": scan.source_key, "module_key": "rf_hub", "logical_bus": "c-can",
                "resolved_channel": "can2", "completed_at": "2026-08-20T16:00:01Z",
                "outcome": "success", "unavailable_reason": None, "dtc_count": 0,
                "explicit_zero_dtcs": True,
            })
            path.write_text(json.dumps(payload, indent=4, sort_keys=True))
            formatted_scan, formatted_preview = dtc_reports.load_inventory(str(path))
            self.assertEqual(formatted_scan.source_key, scan.source_key)
            self.assertNotEqual(formatted_preview["source_sha256"], preview["source_sha256"])

    def test_invalid_json_retains_exception_type_and_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_bytes(b"not json")
            with self.assertRaises(DtcParseError) as caught:
                dtc_reports.load_inventory(str(path))
            self.assertIsInstance(caught.exception.__cause__, json.JSONDecodeError)
            self.assertTrue(str(caught.exception).startswith(f"{path}: invalid JSON: "))
            path.write_text("[]")
            with self.assertRaises(DtcParseError) as caught:
                dtc_reports.load_inventory(str(path))
            self.assertEqual(str(caught.exception), f"{path}: report root must be an object")

    def test_load_inventory_does_not_open_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(report("rf_hub", "59 02 FF")))
            with mock.patch("socket.socket", side_effect=AssertionError("no sockets")):
                scan, _ = dtc_reports.load_inventory(str(path))
            self.assertEqual(scan.outcome, "success")


if __name__ == "__main__":
    unittest.main()
