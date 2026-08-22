import contextlib
import io
import json
from pathlib import Path
import signal
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from lib import dtc_batch
from lib.modules import MODULES, NORMAL_11BITS, Module, bind_channel
from projects.vehicle_data import ccan_powertrain
from tools import dtc_batch as dtc_batch_tool
from tools import dtc_scan


def observation(metric, value):
    return ccan_powertrain.PassiveObservation(
        metric=metric,
        value=value,
        unit="fixture",
        source="fixture",
        quality="fixture",
        detail="fixture",
    )


def safe_snapshot(*, rpm=(0.0, 0.0, 0.0), speed=0.0, ignition=True):
    return ccan_powertrain.BroadcastSnapshot(
        observations=(
            observation("engine.rpm", rpm[len(rpm) // 2]),
            observation("vehicle.speed", speed),
            observation("vehicle.ignition_on", ignition),
        ),
        rpm_samples=tuple(rpm),
        frame_count=9,
        completed_monotonic=123.5,
    )


def broker_status(**active_changes):
    active = {
        "enabled": True,
        "state": "idle",
        "interface_mode": "listen_only",
        "restoration_failed": False,
    }
    active.update(active_changes)
    return {
        "service": "van-telemetry",
        "active_drive": active,
        "vehicle_state": {
            "state": "ignition_on",
            "running": False,
            "confidence": "verified",
            "basis": "qualified_ccan_0x0fc_engine_speed",
            "age_ms": 100,
        },
    }


class TerminationGuardFixture:
    def __init__(self):
        self.received_signal = None
        self.cleanup_started = False
        self._interruption_raised = False

    def begin_cleanup(self):
        self.cleanup_started = True

    def handle(self, signum, _frame=None):
        if self.received_signal is None:
            self.received_signal = signum
        if self.cleanup_started or self._interruption_raised:
            return
        self._interruption_raised = True
        raise KeyboardInterrupt


class PolicyTests(unittest.TestCase):
    def test_default_plan_is_fixed_registered_physical_request(self):
        selected = dtc_batch.select_modules([], None)
        plan = dtc_batch.build_plan(selected, rate_hz=1.0)

        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["request_bytes"], [0x19, 0x02, 0xFF])
        self.assertEqual(plan["request_count"], len(MODULES) - 1)
        self.assertEqual(
            dtc_batch.DTC_BATCH_SUPPORTED_KEYS,
            frozenset(MODULES) - {"pcm"},
        )
        self.assertTrue(plan["registered_modules_only"])
        self.assertTrue(plan["physical_addressing_only"])
        self.assertFalse(plan["clear_dtc_service_implemented"])
        self.assertFalse(plan["diagnostic_session_control_implemented"])
        self.assertFalse(plan["tester_present_implemented"])
        self.assertFalse(plan["functional_broadcast_implemented"])
        self.assertFalse(plan["arbitrary_payload_implemented"])
        request_rows = [row for row in plan["modules"] if row["request_hex"]]
        self.assertEqual({row["request_hex"] for row in request_rows}, {"19 02 FF"})
        self.assertEqual(
            {row["module_key"] for row in plan["modules"]}, set(MODULES)
        )

    def test_pcm_is_explicitly_unsupported_and_never_grouped(self):
        selected = dtc_batch.select_modules(["pcm", "rf_hub"], None)
        plan = dtc_batch.build_plan(selected, rate_hz=0.5)
        pcm = next(row for row in plan["modules"] if row["module_key"] == "pcm")

        self.assertEqual(pcm["execution_state"], "unsupported")
        self.assertIsNone(pcm["request_hex"])
        self.assertIn("framing/padding", pcm["unsupported_reason"])
        groups = dtc_batch.modules_by_bus(selected)
        self.assertEqual(
            [item.module.key for _bus, members in groups for item in members],
            ["rf_hub"],
        )

    def test_future_registry_module_is_unsupported_until_explicitly_reviewed(self):
        future = Module(
            key="future_module",
            name="Future module",
            txid=0x18DA55F1,
            rxid=0x18DAF155,
            bus="c-can",
        )
        with mock.patch.dict(MODULES, {future.key: future}):
            selected = dtc_batch.select_modules([future.key], None)
            plan = dtc_batch.build_plan(selected, rate_hz=1.0)

        self.assertEqual(plan["request_count"], 0)
        self.assertEqual(plan["modules"][0]["execution_state"], "unsupported")
        self.assertIn("reviewed DTC batch allowlist", plan["modules"][0]["unsupported_reason"])

    def test_unknown_duplicate_empty_and_high_rate_selection_fail(self):
        with self.assertRaises(dtc_batch.BatchPolicyError):
            dtc_batch.select_modules(["not_registered"], None)
        with self.assertRaises(dtc_batch.BatchPolicyError):
            dtc_batch.select_modules(["rf_hub", "rf_hub"], None)
        with self.assertRaises(dtc_batch.BatchPolicyError):
            dtc_batch.select_modules(["rf_hub"], ["b-can"])
        with self.assertRaises(dtc_batch.BatchPolicyError):
            dtc_batch.build_plan(
                dtc_batch.select_modules(["rf_hub"], None), rate_hz=1.01
            )

    def test_functional_or_malformed_registry_addressing_is_rejected(self):
        functional_11bit = Module(
            key="fixture_functional",
            name="fixture",
            txid=0x7DF,
            rxid=0x7E8,
            addressing_mode=NORMAL_11BITS,
        )
        malformed_29bit = Module(
            key="fixture_29bit",
            name="fixture",
            txid=0x18DB10F1,
            rxid=0x18DAF110,
        )
        for module in (functional_11bit, malformed_29bit):
            with self.subTest(module=module.key), self.assertRaises(
                dtc_batch.BatchPolicyError
            ):
                dtc_batch.require_physical_module(module)

    def test_dry_cli_has_no_payload_option_and_never_constructs_runner(self):
        option_strings = {
            option
            for action in dtc_batch_tool.build_parser()._actions
            for option in action.option_strings
        }
        self.assertNotIn("--payload", option_strings)
        with (
            mock.patch.object(dtc_batch_tool, "BatchRunner") as runner,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            result = dtc_batch_tool.main(["rf_hub"])
        self.assertEqual(result, 0)
        runner.assert_not_called()
        self.assertIn("no CAN socket opened", stdout.getvalue())

    def test_execute_requires_all_operator_assertions_before_job_creation(self):
        with (
            mock.patch.object(dtc_batch_tool.JobStore, "create") as create,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = dtc_batch_tool.main(["rf_hub", "--execute"])
        self.assertEqual(result, 2)
        create.assert_not_called()


class GateTests(unittest.TestCase):
    def test_sudo_preflight_lists_each_exact_arm_and_restore_command(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=0)

        errors = dtc_batch_tool._exact_sudo_link_permission_errors(
            "can7",
            500000,
            run=run,
            ip_path="/usr/sbin/ip",
        )
        self.assertEqual(errors, ())
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            calls[0][0],
            [
                "sudo", "-n", "-l", "--", "/usr/sbin/ip",
                "link", "set", "can7", "down",
            ],
        )
        self.assertIn("listen-only", calls[1][0])
        self.assertIn("off", calls[1][0])
        self.assertIn("on", calls[2][0])
        self.assertTrue(all(call[1]["check"] is False for call in calls))

        denied = dtc_batch_tool._exact_sudo_link_permission_errors(
            "can7",
            500000,
            run=lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
            ip_path="/usr/sbin/ip",
        )
        self.assertEqual(len(denied), 3)

    def test_broker_must_be_enabled_idle_listen_only_and_restored(self):
        checked = dtc_batch.validate_broker_idle(broker_status())
        self.assertEqual(checked["state"], "idle")

        changes = (
            {"enabled": False},
            {"state": "starting"},
            {"state": "armed_diagnostic", "interface_mode": "armed_diagnostic"},
            {"interface_mode": "armed_diagnostic"},
            {"restoration_failed": True},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(
                dtc_batch.BrokerGateError
            ):
                dtc_batch.validate_broker_idle(broker_status(**change))

    def test_initial_broker_state_requires_fresh_verified_engine_off(self):
        checked = dtc_batch.validate_initial_broker_vehicle_state(broker_status())
        self.assertFalse(checked["running"])

        payload = broker_status()
        payload["vehicle_state"]["age_ms"] = 5001
        with self.assertRaises(dtc_batch.VehicleGateError):
            dtc_batch.validate_initial_broker_vehicle_state(payload)
        payload = broker_status()
        payload["vehicle_state"].update(state="running", running=True)
        with self.assertRaises(dtc_batch.VehicleGateError):
            dtc_batch.validate_initial_broker_vehicle_state(payload)

    def test_fresh_snapshot_proves_ignition_engine_off_and_speed_zero(self):
        checked = dtc_batch.validate_vehicle_snapshot(safe_snapshot())
        self.assertTrue(checked["ignition_on"])
        self.assertTrue(checked["engine_off"])
        self.assertEqual(checked["rpm_sample_count"], 3)
        self.assertEqual(checked["speed_mph"], 0.0)

    def test_snapshot_rejects_running_moving_missing_and_too_few_samples(self):
        cases = (
            safe_snapshot(rpm=(0.0, 700.0, 710.0)),
            safe_snapshot(speed=0.2),
            safe_snapshot(ignition=False),
            safe_snapshot(rpm=(0.0, 0.0)),
        )
        for snapshot in cases:
            with self.subTest(snapshot=snapshot), self.assertRaises(
                dtc_batch.VehicleGateError
            ):
                dtc_batch.validate_vehicle_snapshot(snapshot)

    def test_before_tx_checks_cancel_target_broker_then_fresh_vehicle(self):
        gate = object.__new__(dtc_batch_tool.SafetyGate)
        gate.target_owner = SimpleNamespace(route=SimpleNamespace(role="c-can"))
        gate.target_ready = mock.Mock()
        gate.broker_status = mock.Mock(return_value={"state": "idle"})
        gate.fresh_vehicle_state = mock.Mock(return_value={"engine_off": True})
        store = mock.Mock()
        store.cancellation_requested.return_value = False

        checked = gate.before_tx(store)

        self.assertEqual(gate.target_ready.call_count, 2)
        gate.broker_status.assert_called_once_with()
        gate.fresh_vehicle_state.assert_called_once_with(ccan_armed=True)
        self.assertEqual(store.cancellation_requested.call_count, 2)
        self.assertTrue(checked["vehicle"]["engine_off"])

    def test_cancel_arriving_during_fresh_snapshot_stops_before_final_target_gate(self):
        gate = object.__new__(dtc_batch_tool.SafetyGate)
        gate.target_owner = SimpleNamespace(route=SimpleNamespace(role="b-can"))
        gate.target_ready = mock.Mock()
        gate.broker_status = mock.Mock(return_value={"state": "idle"})
        gate.fresh_vehicle_state = mock.Mock(return_value={"engine_off": True})
        store = mock.Mock()
        store.cancellation_requested.side_effect = (False, True)

        with self.assertRaises(dtc_batch_tool.BatchCancelled):
            gate.before_tx(store)

        gate.target_ready.assert_called_once_with()
        gate.broker_status.assert_called_once_with()
        gate.fresh_vehicle_state.assert_called_once_with(ccan_armed=False)


class ResponseAndReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_response_classifier_distinguishes_zero_timeout_negative_and_shape_fault(self):
        positive, fatal = dtc_batch.classify_response(bytes.fromhex("59 02 CF"), "ok")
        self.assertEqual(positive["category"], "positive")
        self.assertEqual(positive["parsed"]["dtcs"], [])
        self.assertFalse(fatal)

        timeout, fatal = dtc_batch.classify_response(None, "timeout")
        self.assertEqual(timeout["category"], "timeout")
        self.assertFalse(fatal)

        negative, fatal = dtc_batch.classify_response(
            bytes.fromhex("7F 19 31"), "negative"
        )
        self.assertEqual(negative["negative_response"]["nrc"], "31")
        self.assertFalse(fatal)

        malformed, fatal = dtc_batch.classify_response(
            bytes.fromhex("59 02 CF 55"), "malformed"
        )
        self.assertEqual(malformed["category"], "unexpected")
        self.assertTrue(fatal)

    def make_report(self, result, *, restored_passive=True, fatal_error=None):
        result = dict(result)
        result["request_attempted"] = True
        return dtc_batch.inventory_compatible_report(
            module=bind_channel(MODULES["rf_hub"], "can7"),
            channel="can7",
            topology_fingerprint="fixture-topology",
            physical_pair="6/14",
            started_at="2026-08-21T12:00:00Z",
            completed_at="2026-08-21T12:00:01Z",
            result=result,
            elapsed_s=1.0,
            job_id="fixture-job",
            restored_passive=restored_passive,
            max_request_rate_hz=0.5,
            fatal_error=fatal_error,
        )

    def test_atomic_batch_report_is_accepted_by_offline_importer(self):
        result, _fatal = dtc_batch.classify_response(
            bytes.fromhex("59 02 CF 55 03 31 08"), "ok"
        )
        report = self.make_report(result)
        path = self.root / "report.json"
        dtc_batch.atomic_json(path, report)

        scan, preview = dtc_scan.load_inventory(str(path))

        self.assertEqual(report["producer"], "tools/dtc_batch.py")
        self.assertFalse(report["diagnostic_session_control_sent"])
        self.assertFalse(report["tester_present_sent"])
        self.assertFalse(report["functional_broadcast_sent"])
        self.assertEqual(report["max_request_rate_hz"], 0.5)
        self.assertEqual(scan.outcome, "success")
        self.assertEqual(scan.dtcs[0].raw_dtc, "550331")
        self.assertEqual(preview["module_key"], "rf_hub")

    def test_positive_response_with_failed_cleanup_is_non_authoritative(self):
        result, _fatal = dtc_batch.classify_response(
            bytes.fromhex("59 02 CF"), "ok"
        )
        report = self.make_report(
            result,
            restored_passive=False,
            fatal_error="passive restoration was not verified",
        )
        path = self.root / "failed-restore.json"
        dtc_batch.atomic_json(path, report)

        scan, preview = dtc_scan.load_inventory(str(path))

        wire_result = report["results"][0]
        self.assertEqual(wire_result["observed_category"], "positive")
        self.assertEqual(wire_result["response_hex"], "59 02 CF")
        self.assertFalse(wire_result["authoritative_for_history"])
        self.assertEqual(wire_result["category"], "inventory_error")
        self.assertEqual(scan.outcome, "unavailable")
        self.assertEqual(scan.unavailable_reason, "inventory_error")
        self.assertIsNone(preview["dtc_count"])
        self.assertFalse(preview["explicit_zero_dtcs"])

    def test_timeout_report_imports_as_unavailable_never_zero(self):
        result, _fatal = dtc_batch.classify_response(None, "timeout")
        report = self.make_report(result)
        path = self.root / "timeout.json"
        dtc_batch.atomic_json(path, report)

        scan, preview = dtc_scan.load_inventory(str(path))

        self.assertEqual(scan.outcome, "unavailable")
        self.assertEqual(scan.unavailable_reason, "timeout")
        self.assertEqual(scan.dtcs, ())
        self.assertIsNone(preview["dtc_count"])
        self.assertFalse(preview["explicit_zero_dtcs"])


class JobAndRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.planned = dtc_batch.select_modules(["radar_acc", "rf_hub"], None)
        self.plan = dtc_batch.build_plan(self.planned, rate_hz=1.0)
        self.store = dtc_batch.JobStore(self.root / "jobs", "fixture-job")
        self.store.create(self.plan, command=["fixture"])

    def runner(self):
        return dtc_batch_tool.BatchRunner(
            planned=self.planned,
            rate_hz=1.0,
            timeout_s=1.0,
            store=self.store,
            report_root=self.root / "reports",
            database=self.root / "history.sqlite3",
            cache_path=self.root / "cache.json",
            socket_path=str(self.root / "broker.sock"),
        )

    def test_job_record_and_cancel_flag_are_atomic_ui_primitives(self):
        record = self.store.read()
        self.assertEqual(record["state"], "created")
        self.assertEqual(record["progress"]["requestable"], 2)
        self.assertTrue(self.store.request_cancel())
        self.assertFalse(self.store.request_cancel())
        self.assertTrue(self.store.cancellation_requested())
        self.assertTrue(self.store.read()["cancel_requested"])
        json.loads(self.store.record_path.read_text())
        json.loads(self.store.cancel_path.read_text())

    def test_query_uses_only_constant_request_and_revalidates_immediately_before_tx(self):
        runner = self.runner()
        owner = SimpleNamespace(route=SimpleNamespace(channel="can7"))
        gate = mock.Mock()
        gate.target_owner = owner
        sock = mock.Mock()
        with (
            mock.patch.object(dtc_batch_tool.uds, "open_module_socket", return_value=sock),
            mock.patch.object(dtc_batch_tool.uds, "drain") as drain,
            mock.patch.object(
                dtc_batch_tool.uds,
                "request",
                return_value=(bytes.fromhex("59 02 CF"), "ok"),
            ) as request,
        ):
            result, _elapsed, fatal = runner._query_module(
                MODULES["rf_hub"], gate
            )

        drain.assert_called_once_with(sock)
        gate.before_tx.assert_called_once_with(self.store)
        request.assert_called_once_with(
            sock,
            bytes.fromhex("19 02 FF"),
            timeout=1.0,
            retries=0,
        )
        self.assertTrue(result["request_attempted"])
        self.assertEqual(result["category"], "positive")
        self.assertFalse(fatal)
        sock.close.assert_called_once_with()

    def test_same_bus_modules_share_one_arm_restore_window_then_import(self):
        runner = self.runner()
        runner.manager = mock.Mock()
        route = SimpleNamespace(
            role="c-can",
            channel="can7",
            pair="6/14",
            topology_fingerprint="fixture-topology",
            module=bind_channel(MODULES["radar_acc"], "can7"),
        )
        owner = mock.Mock(route=route)
        owner.release.return_value = True
        result = {
            **dtc_batch.classify_response(bytes.fromhex("59 02 CF"), "ok")[0],
            "request_attempted": True,
        }
        with (
            mock.patch.object(
                dtc_batch_tool.can_runtime_route,
                "resolve_module_route",
                return_value=route,
            ),
            mock.patch.object(
                dtc_batch_tool.can_runtime_route,
                "acquire_armed_module_route",
                return_value=owner,
            ) as acquire,
            mock.patch.object(
                runner, "_query_module", side_effect=[(result, 0.1, False)] * 2
            ) as query,
            mock.patch.object(
                dtc_batch_tool, "_import_reports", return_value=(2, {})
            ) as importer,
        ):
            runner._run_role("c-can", self.planned)

        acquire.assert_called_once()
        owner.release.assert_called_once_with()
        self.assertEqual(query.call_count, 2)
        importer.assert_called_once()
        record = self.store.read()
        self.assertEqual(record["progress"]["queried"], 2)
        self.assertEqual(record["progress"]["imported"], 2)
        self.assertEqual({row["state"] for row in record["modules"]}, {"imported"})

    def test_pretransmit_gate_failure_is_not_imported_as_ecu_unavailable(self):
        runner = self.runner()
        runner.manager = mock.Mock()
        route = SimpleNamespace(
            role="c-can",
            channel="can7",
            pair="6/14",
            topology_fingerprint="fixture-topology",
            module=bind_channel(MODULES["radar_acc"], "can7"),
        )
        owner = mock.Mock(route=route)
        owner.release.return_value = True
        failed_before_tx = {
            "category": "transport_error",
            "response_hex": None,
            "status": "vehicle gate failed",
            "negative_response": None,
            "parsed": None,
            "request_attempted": False,
        }
        with (
            mock.patch.object(
                dtc_batch_tool.can_runtime_route,
                "resolve_module_route",
                return_value=route,
            ),
            mock.patch.object(
                dtc_batch_tool.can_runtime_route,
                "acquire_armed_module_route",
                return_value=owner,
            ),
            mock.patch.object(
                runner,
                "_query_module",
                return_value=(failed_before_tx, 0.1, True),
            ),
            mock.patch.object(dtc_batch_tool, "_import_reports") as importer,
        ):
            with self.assertRaises(dtc_batch_tool.FatalBatchError):
                runner._run_role("c-can", self.planned[:1])

        owner.release.assert_called_once_with()
        importer.assert_not_called()
        record = self.store.read()
        self.assertEqual(record["progress"]["queried"], 0)
        self.assertEqual(record["reports"], [])
        self.assertEqual(record["modules"][0]["state"], "failed_before_tx")

    def test_unverified_restore_stops_before_history_import(self):
        runner = self.runner()
        runner.manager = mock.Mock()
        route = SimpleNamespace(
            role="c-can",
            channel="can7",
            pair="6/14",
            topology_fingerprint="fixture-topology",
            module=bind_channel(MODULES["radar_acc"], "can7"),
        )
        owner = mock.Mock(route=route)
        owner.release.return_value = False
        result = {
            **dtc_batch.classify_response(bytes.fromhex("59 02 CF"), "ok")[0],
            "request_attempted": True,
        }
        with (
            mock.patch.object(
                dtc_batch_tool.can_runtime_route,
                "resolve_module_route",
                return_value=route,
            ),
            mock.patch.object(
                dtc_batch_tool.can_runtime_route,
                "acquire_armed_module_route",
                return_value=owner,
            ),
            mock.patch.object(
                runner, "_query_module", return_value=(result, 0.1, False)
            ),
            mock.patch.object(dtc_batch_tool, "_import_reports") as importer,
            mock.patch.object(
                dtc_batch_tool.can_operation_state, "begin_inhibit"
            ) as inhibit,
        ):
            with self.assertRaises(dtc_batch_tool.RestorationFailure):
                runner._run_role("c-can", self.planned[:1])

        importer.assert_not_called()
        inhibit.assert_called_once()
        module = self.store.read()["modules"][0]
        self.assertEqual(module["state"], "restore_unverified")
        report = json.loads(Path(module["report"]).read_text())
        self.assertFalse(report["restored_passive"])

    def test_restore_failure_precedes_and_survives_report_persistence_failure(self):
        runner = self.runner()
        runner.manager = mock.Mock()
        route = SimpleNamespace(
            role="c-can",
            channel="can7",
            pair="6/14",
            topology_fingerprint="fixture-topology",
            module=bind_channel(MODULES["radar_acc"], "can7"),
        )
        owner = mock.Mock(route=route)
        owner.release.return_value = False
        result = {
            **dtc_batch.classify_response(bytes.fromhex("59 02 CF"), "ok")[0],
            "request_attempted": True,
        }
        with (
            mock.patch.object(
                dtc_batch_tool.can_runtime_route,
                "resolve_module_route",
                return_value=route,
            ),
            mock.patch.object(
                dtc_batch_tool.can_runtime_route,
                "acquire_armed_module_route",
                return_value=owner,
            ),
            mock.patch.object(
                runner, "_query_module", return_value=(result, 0.1, False)
            ),
            mock.patch.object(
                runner,
                "_finalize_role_reports",
                side_effect=OSError("report filesystem unavailable"),
            ),
            mock.patch.object(
                dtc_batch_tool.can_operation_state, "begin_inhibit"
            ) as inhibit,
            mock.patch.object(dtc_batch_tool, "_import_reports") as importer,
        ):
            with self.assertRaises(dtc_batch_tool.RestorationFailure) as raised:
                runner._run_role("c-can", self.planned[:1])

        inhibit.assert_called_once()
        importer.assert_not_called()
        self.assertIn("could not verify passive restoration", str(raised.exception))
        self.assertIn("evidence persistence also failed", str(raised.exception))

    def test_positive_wire_result_with_cleanup_fault_is_counted_unavailable(self):
        runner = self.runner()
        runner.manager = mock.Mock()
        route = SimpleNamespace(
            role="c-can",
            channel="can7",
            pair="6/14",
            topology_fingerprint="fixture-topology",
            module=bind_channel(MODULES["radar_acc"], "can7"),
        )
        owner = mock.Mock(route=route)
        owner.release.return_value = True
        result = {
            **dtc_batch.classify_response(bytes.fromhex("59 02 CF"), "ok")[0],
            "request_attempted": True,
            "transport_cleanup_error": "OSError: close failed",
        }
        with (
            mock.patch.object(
                dtc_batch_tool.can_runtime_route,
                "resolve_module_route",
                return_value=route,
            ),
            mock.patch.object(
                dtc_batch_tool.can_runtime_route,
                "acquire_armed_module_route",
                return_value=owner,
            ),
            mock.patch.object(
                runner, "_query_module", return_value=(result, 0.1, True)
            ),
            mock.patch.object(
                dtc_batch_tool, "_import_reports", return_value=(1, {})
            ) as importer,
        ):
            with self.assertRaises(dtc_batch_tool.FatalBatchError):
                runner._run_role("c-can", self.planned[:1])

        importer.assert_called_once()
        record = self.store.read()
        self.assertEqual(record["progress"]["unavailable"], 1)
        self.assertEqual(record["progress"]["imported"], 1)
        self.assertEqual(record["modules"][0]["outcome"], "inventory_error")
        self.assertIsNone(record["modules"][0]["dtc_count"])

    def test_signal_during_arming_is_recorded_then_role_is_restored_without_tx(self):
        runner = self.runner()
        runner.manager = mock.Mock()
        route = SimpleNamespace(
            role="c-can",
            channel="can7",
            pair="6/14",
            topology_fingerprint="fixture-topology",
            module=bind_channel(MODULES["radar_acc"], "can7"),
        )
        owner = mock.Mock(route=route)
        owner.release.return_value = True
        role_guard = TerminationGuardFixture()

        def acquire_after_signal(*_args, **_kwargs):
            role_guard.handle(signal.SIGTERM)
            return owner

        with (
            mock.patch.object(
                dtc_batch_tool.diagnostic_safety,
                "interrupt_on_termination",
                return_value=contextlib.nullcontext(role_guard),
            ),
            mock.patch.object(
                dtc_batch_tool.can_runtime_route,
                "resolve_module_route",
                return_value=route,
            ),
            mock.patch.object(
                dtc_batch_tool.can_runtime_route,
                "acquire_armed_module_route",
                side_effect=acquire_after_signal,
            ),
            mock.patch.object(runner, "_query_module") as query,
        ):
            with self.assertRaises(dtc_batch_tool.BatchCancelled):
                runner._run_role("c-can", self.planned[:1])

        self.assertTrue(role_guard.cleanup_started)
        owner.release.assert_called_once_with()
        query.assert_not_called()

    def test_signal_during_active_work_restores_role_before_cancelling(self):
        runner = self.runner()
        runner.manager = mock.Mock()
        route = SimpleNamespace(
            role="c-can",
            channel="can7",
            pair="6/14",
            topology_fingerprint="fixture-topology",
            module=bind_channel(MODULES["radar_acc"], "can7"),
        )
        owner = mock.Mock(route=route)
        owner.release.return_value = True
        role_guard = TerminationGuardFixture()
        operation_guard = TerminationGuardFixture()

        def interrupted_query(*_args, **_kwargs):
            operation_guard.handle(signal.SIGHUP)

        with (
            mock.patch.object(
                dtc_batch_tool.diagnostic_safety,
                "interrupt_on_termination",
                side_effect=(
                    contextlib.nullcontext(role_guard),
                    contextlib.nullcontext(operation_guard),
                ),
            ),
            mock.patch.object(
                dtc_batch_tool.can_runtime_route,
                "resolve_module_route",
                return_value=route,
            ),
            mock.patch.object(
                dtc_batch_tool.can_runtime_route,
                "acquire_armed_module_route",
                return_value=owner,
            ),
            mock.patch.object(runner, "_query_module", side_effect=interrupted_query),
        ):
            with self.assertRaises(dtc_batch_tool.BatchCancelled):
                runner._run_role("c-can", self.planned[:1])

        self.assertTrue(operation_guard.cleanup_started)
        owner.release.assert_called_once_with()

    def test_first_signal_during_normal_restore_cannot_interrupt_cleanup(self):
        runner = self.runner()
        runner.manager = mock.Mock()
        route = SimpleNamespace(
            role="c-can",
            channel="can7",
            pair="6/14",
            topology_fingerprint="fixture-topology",
            module=bind_channel(MODULES["radar_acc"], "can7"),
        )
        role_guard = TerminationGuardFixture()
        operation_guard = TerminationGuardFixture()
        owner = mock.Mock(route=route)

        def release_after_signal():
            self.assertTrue(role_guard.cleanup_started)
            self.assertTrue(operation_guard.cleanup_started)
            role_guard.handle(signal.SIGTERM)
            return True

        owner.release.side_effect = release_after_signal
        result = {
            **dtc_batch.classify_response(bytes.fromhex("59 02 CF"), "ok")[0],
            "request_attempted": True,
        }
        with (
            mock.patch.object(
                dtc_batch_tool.diagnostic_safety,
                "interrupt_on_termination",
                side_effect=(
                    contextlib.nullcontext(role_guard),
                    contextlib.nullcontext(operation_guard),
                ),
            ),
            mock.patch.object(
                dtc_batch_tool.can_runtime_route,
                "resolve_module_route",
                return_value=route,
            ),
            mock.patch.object(
                dtc_batch_tool.can_runtime_route,
                "acquire_armed_module_route",
                return_value=owner,
            ),
            mock.patch.object(
                runner, "_query_module", return_value=(result, 0.1, False)
            ),
            mock.patch.object(
                dtc_batch_tool, "_import_reports", return_value=(1, {})
            ) as importer,
        ):
            with self.assertRaises(dtc_batch_tool.BatchCancelled):
                runner._run_role("c-can", self.planned[:1])

        owner.release.assert_called_once_with()
        importer.assert_called_once()
        self.assertEqual(self.store.read()["modules"][0]["state"], "imported")


if __name__ == "__main__":
    unittest.main()
