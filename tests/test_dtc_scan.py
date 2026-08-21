import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

from lib.modules import MODULES
from tools import dtc_scan


def report(module_key, response, category="positive"):
    module = MODULES[module_key]
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
            "channel": "can2",
            "bitrate": module.bitrate,
        },
        "physical_pair": "fixture",
        "conditions": "fixture",
        "interrupted": False,
        "fatal_error": None,
        "results": [
            {
                "label": "dtcs_by_status",
                "request_hex": "19 02 FF",
                "response_hex": response,
                "category": category,
                "negative_response": None,
            }
        ],
    }


class DtcScanPlanTests(unittest.TestCase):
    def test_plan_is_one_read_only_request_per_module(self):
        modules = [MODULES["rf_hub"], MODULES["ics_bcan"], MODULES["abs_canch"]]
        plan = dtc_scan.build_plan(
            modules,
            {"c-can": "can2", "b-can": "can1", "can-ch": "can0"},
            1.0,
        )
        self.assertTrue(plan["dry_run"])
        self.assertFalse(plan["live_execution_implemented"])
        self.assertFalse(plan["clear_dtc_service_implemented"])
        self.assertFalse(plan["diagnostic_session_control_implemented"])
        self.assertFalse(plan["functional_broadcast_implemented"])
        self.assertTrue(all(row["request_hex"] == "19 02 FF" for row in plan["modules"]))
        self.assertEqual(
            [(row["logical_bus"], row["resolved_channel"]) for row in plan["modules"]],
            [("c-can", "can2"), ("b-can", "can1"), ("can-ch", "can0")],
        )

    def test_plan_never_uses_registry_static_channel_as_resolution(self):
        plan = dtc_scan.build_plan([MODULES["rf_hub"]], {}, 1.0)
        self.assertIsNone(plan["modules"][0]["resolved_channel"])
        self.assertEqual(plan["modules"][0]["route_state"], "unresolved")
        self.assertFalse(plan["registry_static_channel_used"])

    def test_route_must_be_one_to_one(self):
        with self.assertRaisesRegex(ValueError, "cannot resolve both"):
            dtc_scan.parse_routes(["c-can=can0", "can-ch=can0"])
        with self.assertRaises(ValueError):
            dtc_scan.parse_routes(["c-can=eth0"])

    def test_default_cli_is_dry_and_does_not_open_history(self):
        with (
            mock.patch.object(dtc_scan, "DtcHistory") as history,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            result = dtc_scan.main(["rf_hub", "--route", "c-can=can2"])
        self.assertEqual(result, 0)
        history.assert_not_called()
        self.assertIn("nothing transmitted", stdout.getvalue())

    def test_runtime_resolution_is_read_only_plan_metadata(self):
        with (
            mock.patch.object(
                dtc_scan,
                "resolve_runtime_routes",
                return_value=({"c-can": "can3"}, "topology-generation"),
            ) as resolve,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            result = dtc_scan.main(["rf_hub", "--resolve-runtime", "--json"])
        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["runtime_topology_fingerprint"], "topology-generation")
        self.assertEqual(payload["modules"][0]["resolved_channel"], "can3")
        self.assertFalse(payload["live_execution_implemented"])
        resolve.assert_called_once()

    def test_runtime_resolution_keeps_healthy_bus_when_another_role_is_missing(self):
        topology = SimpleNamespace(
            fingerprint="topology-generation",
            resolution=lambda bus: SimpleNamespace(
                state="resolved" if bus == "c-can" else "missing",
                channel="can3" if bus == "c-can" else None,
            ),
        )
        manager = mock.Mock()
        manager.topology.return_value = topology
        with mock.patch(
            "projects.vehicle_data.can_interfaces.PassiveInterfaceManager",
            return_value=manager,
        ):
            routes, generation = dtc_scan.resolve_runtime_routes(
                [MODULES["rf_hub"], MODULES["ics_bcan"]]
            )

        self.assertEqual(routes, {"c-can": "can3"})
        self.assertEqual(generation, "topology-generation")
        plan = dtc_scan.build_plan(
            [MODULES["rf_hub"], MODULES["ics_bcan"]], routes, 1.0
        )
        self.assertEqual(
            [row["route_state"] for row in plan["modules"]],
            ["resolved", "unresolved"],
        )

    def test_default_selection_covers_registry_across_three_buses(self):
        selected = dtc_scan.selected_modules([], None)
        self.assertEqual(len(selected), len(MODULES))
        self.assertEqual({module.bus for module in selected}, {"c-can", "b-can", "can-ch"})

    def test_rate_is_capped_at_one_request_per_second(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dtc_scan.main(["rf_hub", "--rate", "1.1"]), 2)

    def test_no_execute_argument_exists(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            dtc_scan.parser().parse_args(["rf_hub", "--execute"])


class DtcScanImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write_report(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload))
        return path

    def test_preview_does_not_create_database_or_cache(self):
        source = self.write_report("zero.json", report("cluster", "59 02 CF"))
        database = self.root / "history.sqlite3"
        cache = self.root / "cache.json"
        with contextlib.redirect_stdout(io.StringIO()):
            result = dtc_scan.main(
                [
                    "--import-report",
                    str(source),
                    "--db",
                    str(database),
                    "--cache-out",
                    str(cache),
                ]
            )
        self.assertEqual(result, 0)
        self.assertFalse(database.exists())
        self.assertFalse(cache.exists())

    def test_commit_writes_cache_only_summary_and_is_idempotent(self):
        positive = self.write_report(
            "positive.json", report("cluster", "59 02 CF 55 03 31 08")
        )
        timeout_payload = report("pcm", None, category="timeout")
        timeout = self.write_report("timeout.json", timeout_payload)
        database = self.root / "history.sqlite3"
        cache = self.root / "cache.json"
        argv = [
            "--import-report",
            str(positive),
            "--import-report",
            str(timeout),
            "--commit",
            "--db",
            str(database),
            "--cache-out",
            str(cache),
            "--json",
        ]
        with contextlib.redirect_stdout(io.StringIO()) as first_stdout:
            self.assertEqual(dtc_scan.main(argv), 0)
        first = json.loads(first_stdout.getvalue())
        self.assertEqual(first["inserted_reports"], 2)
        self.assertEqual(first["coverage"]["available_modules"], 1)
        self.assertEqual(first["coverage"]["unavailable_modules"], 1)

        cached = json.loads(cache.read_text())
        self.assertEqual(cached["acquisition"], "cache_only")
        self.assertTrue(cached["compact"])
        self.assertEqual(cached["per_group_limit"], 25)
        self.assertEqual(cached["groups"]["confirmed_history"][0]["raw_dtc"], "550331")
        pcm = next(row for row in cached["modules"] if row["module_key"] == "pcm")
        self.assertEqual(pcm["result_state"], "unavailable")
        cluster = next(row for row in cached["modules"] if row["module_key"] == "cluster")
        self.assertEqual(cluster["result_state"], "dtcs_present")

        with contextlib.redirect_stdout(io.StringIO()) as second_stdout:
            self.assertEqual(dtc_scan.main(argv), 0)
        second = json.loads(second_stdout.getvalue())
        self.assertEqual(second["inserted_reports"], 0)
        self.assertEqual(second["duplicate_reports"], 2)

    def test_import_preserves_recorded_channel_separate_from_logical_bus(self):
        source = self.write_report("bcan.json", report("ics_bcan", "59 02 4F"))
        converted, preview = dtc_scan.load_inventory(str(source))
        self.assertEqual(converted.logical_bus, "b-can")
        self.assertEqual(converted.resolved_channel, "can2")
        self.assertEqual(preview["logical_bus"], "b-can")
        self.assertEqual(preview["resolved_channel"], "can2")

    def test_semantic_source_key_survives_reformat_and_irrelevant_metadata(self):
        payload = report("cluster", "59 02 4F")
        first = self.root / "first.json"
        first.write_text(json.dumps(payload, separators=(",", ":")))
        payload["operator_note"] = "irrelevant annotation added later"
        second = self.root / "second.json"
        second.write_text(json.dumps(payload, indent=4, sort_keys=True))

        first_scan, first_preview = dtc_scan.load_inventory(str(first))
        second_scan, second_preview = dtc_scan.load_inventory(str(second))
        self.assertEqual(first_scan.source_key, second_scan.source_key)
        self.assertEqual(first_preview["source_key"], second_preview["source_key"])
        self.assertNotEqual(first_preview["source_sha256"], second_preview["source_sha256"])

    def test_semantic_source_key_normalizes_timestamps_and_hex(self):
        first_payload = report("cluster", "59 02 CF 55 03 31 08")
        first = self.write_report("offset-uppercase.json", first_payload)
        second_payload = report("cluster", "59   02  cf\n55 03 31 08")
        second_payload["started_at"] = "2026-08-20T16:00:00Z"
        second_payload["completed_at"] = "2026-08-20T16:00:01+00:00"
        second_payload["results"][0]["request_hex"] = "19  02 ff"
        second = self.write_report("utc-lowercase.json", second_payload)

        first_scan, first_preview = dtc_scan.load_inventory(str(first))
        second_scan, second_preview = dtc_scan.load_inventory(str(second))
        self.assertEqual(first_scan.started_at, second_scan.started_at)
        self.assertEqual(first_scan.response_hex, second_scan.response_hex)
        self.assertEqual(first_scan.source_key, second_scan.source_key)
        self.assertEqual(first_preview["source_key"], second_preview["source_key"])

    def test_summary_failure_reports_that_history_was_committed(self):
        source = self.write_report("positive.json", report("cluster", "59 02 CF"))
        database = self.root / "history.sqlite3"
        cache = self.root / "cache.json"
        with (
            mock.patch.object(
                dtc_scan.DtcHistory,
                "dashboard_summary",
                side_effect=RuntimeError("summary unavailable"),
            ),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            result = dtc_scan.main(
                [
                    "--import-report",
                    str(source),
                    "--commit",
                    "--db",
                    str(database),
                    "--cache-out",
                    str(cache),
                ]
            )
        self.assertEqual(result, 1)
        self.assertIn("history batch committed", stderr.getvalue())
        self.assertFalse(cache.exists())

        with dtc_scan.DtcHistory(str(database)) as history:
            cluster = next(
                row
                for row in history.snapshot()["modules"]
                if row["module_key"] == "cluster"
            )
            self.assertEqual(cluster["successful_scans"], 1)

    def test_import_rejects_inventory_with_non_dtc_payload(self):
        payload = report("cluster", "59 02 CF")
        payload["results"][0]["request_hex"] = "19 01 FF"
        source = self.write_report("bad.json", payload)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dtc_scan.main(["--import-report", str(source)]), 2)


if __name__ == "__main__":
    unittest.main()
