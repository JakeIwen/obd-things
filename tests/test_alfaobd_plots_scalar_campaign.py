from __future__ import annotations

import argparse
import contextlib
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import alfaobd_plots_catalog as catalog
from tools import alfaobd_plots_scalar_campaign as scalar


REPO = Path(__file__).resolve().parents[1]
TRACKED_SCALAR_PLAN = (
    REPO
    / "projects"
    / "ecu_mapping"
    / "configs"
    / "alfaobd_pcm_plots_scalars.json"
)
TRACKED_TCM_SCALAR_PLAN = (
    REPO
    / "projects"
    / "ecu_mapping"
    / "configs"
    / "alfaobd_tcm_plots_scalars.json"
)
TRACKED_CATALOG_REPORT = (
    REPO
    / "tmp"
    / "ecu_mapping"
    / "alfaobd_plots_catalog"
    / "pcm-plots-catalog-20260726T224830Z"
    / "catalog.json"
)
TRACKED_CATALOG_STATE = TRACKED_CATALOG_REPORT.with_name("state.json")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class _PinnedFixture:
    """Small, internally consistent catalog/report/scalar-plan fixture."""

    labels = ("A", "B", "C", "D")

    def __init__(self, root: Path):
        self.root = root
        self.catalog_path = root / "catalog.json"
        self.report_path = root / "catalog-report.json"
        self.state_path = root / "state.json"
        self.scalar_path = root / "scalar.json"
        catalog_digest = catalog.catalog_sha256(self.labels)
        self.catalog_payload: dict[str, object] = {
            "schema_version": 1,
            "campaign_id": "catalog-fixture",
            "module_key": "pcm",
            "expected_app_version": "2.4.4.0",
            "expected_screen": {
                "width": 800,
                "height": 1280,
                "rotation": 0,
            },
            "expected_connection_texts": ["Connected to synthetic PCM"],
            "expected_catalog_count": len(self.labels),
            "expected_first_label": self.labels[0],
            "expected_last_label": self.labels[-1],
            "required_labels": [self.labels[0], self.labels[-1]],
            "expected_catalog_sha256": catalog_digest,
            "max_pages": 12,
            "swipe_duration_ms": 500,
            "settle_seconds": 0.5,
            "min_free_bytes": 100 * 1024**2,
            "screenshot_each_page": False,
        }
        self.report_payload: dict[str, object] = {
            "schema_version": 1,
            "classification": "live_ui_catalog_pinned_match",
            "validation": {
                "passed": True,
                "errors": [],
                "hash_pinned_before_run": True,
            },
            "selection_committed": False,
            "gauge_rows_tapped": False,
            "dialog_ok_tapped": False,
            "scan_started": False,
            "conditions": "parked synthetic fixture; PCM Plots stopped",
            "adb_serial": "SYNTHETIC-SERIAL",
            "catalog_sha256": catalog_digest,
            "label_count": len(self.labels),
            "catalog": [
                {
                    "zero_based_index": index,
                    "display_order_key": index + 1,
                    "label": label,
                    "checked": False,
                }
                for index, label in enumerate(self.labels)
            ],
            "pages": [
                {"phase": "forward", "page_index": 0},
                {"phase": "reverse", "page_index": 1},
            ],
            "plan": {
                **deepcopy(self.catalog_payload),
                "campaign_id": "inventory-fixture",
            },
        }
        self.state_payload: dict[str, object] = {
            "schema_version": 1,
            "campaign_id": "inventory-fixture",
            "phase": "complete",
            "manual_reconcile": False,
            "catalog_sha256": catalog_digest,
            "hash_pinned_before_run": True,
        }
        self.scalar_payload: dict[str, object] = {
            "schema_version": 1,
            "campaign_id": "scalar-fixture",
            "catalog_plan_path": self.catalog_path.name,
            "catalog_review": {
                "inventory_run_id": "inventory-fixture",
                "catalog_report_path": self.report_path.name,
                "catalog_report_sha256": None,
                "catalog_state_sha256": None,
                "catalog_plan_sha256": None,
                "scalar_plan_sha256": None,
                "reviewed_at_utc": "2026-07-26T08:00:00Z",
                "reviewed_by": "synthetic-reviewer",
                "review_note": "Synthetic test fixture; no vehicle evidence.",
            },
            "targets": [
                {
                    "target_id": "alpha",
                    "display_order_key": 1,
                    "zero_based_index": 0,
                    "label": "A",
                },
                {
                    "target_id": "delta",
                    "display_order_key": 4,
                    "zero_based_index": 3,
                    "label": "D",
                },
            ],
            "schedule": ["alpha", "delta"],
            "segment_seconds": 45,
            "settle_seconds": 2,
            "verify_seconds": 3,
            "flush_timeout_seconds": 30,
            "min_free_bytes": 1024**3,
            "min_tablet_free_bytes": 512 * 1024**2,
            "artifacts": [
                scalar.DEBUG_ARTIFACT,
                scalar.CSV_ARTIFACT,
            ],
            "required_segment_growth": [
                scalar.DEBUG_ARTIFACT,
                scalar.CSV_ARTIFACT,
            ],
            "required_stop_stability": [scalar.CSV_ARTIFACT],
            "stop_stability_observations": 3,
            "recording_oracle_samples": 5,
            "recording_oracle_interval_seconds": 0.22,
            "max_initial_checked": 4,
            "screenshot_each_segment": False,
        }
        self.write_catalog()
        self.write_report()
        self.write_state()
        self.write_scalar()

    @property
    def review(self) -> dict[str, object]:
        value = self.scalar_payload["catalog_review"]
        assert isinstance(value, dict)
        return value

    def write_catalog(self) -> None:
        _write_json(self.catalog_path, self.catalog_payload)

    def write_report(self) -> None:
        _write_json(self.report_path, self.report_payload)

    def write_state(self) -> None:
        _write_json(self.state_path, self.state_payload)

    def write_scalar(
        self,
        *,
        pin_catalog: bool = True,
        pin_report: bool = True,
        pin_state: bool = True,
        pin_scalar: bool = True,
    ) -> None:
        if pin_catalog:
            self.review["catalog_plan_sha256"] = _sha256(
                self.catalog_path
            )
        if pin_report:
            self.review["catalog_report_sha256"] = _sha256(
                self.report_path
            )
        if pin_state:
            self.review["catalog_state_sha256"] = _sha256(
                self.state_path
            )
        if pin_scalar:
            self.review["scalar_plan_sha256"] = None
            self.review["scalar_plan_sha256"] = (
                scalar._reviewable_plan_sha256(self.scalar_payload)
            )
        _write_json(self.scalar_path, self.scalar_payload)

    def load(self) -> scalar.ScalarPlan:
        return scalar.load_plan(self.scalar_path)


@contextlib.contextmanager
def _fixture():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        with mock.patch.object(scalar, "TMP_ROOT", root):
            yield _PinnedFixture(root)


@contextlib.contextmanager
def _forbid_external_or_output_calls():
    """Explode on the common ADB/subprocess/output construction paths."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            mock.patch(
                "subprocess.run",
                side_effect=AssertionError("subprocess.run is forbidden"),
            )
        )
        stack.enter_context(
            mock.patch(
                "subprocess.Popen",
                side_effect=AssertionError("subprocess.Popen is forbidden"),
            )
        )
        stack.enter_context(
            mock.patch.object(
                scalar,
                "AdbClient",
                side_effect=AssertionError("ADB construction is forbidden"),
                create=True,
            )
        )
        stack.enter_context(
            mock.patch.object(
                scalar,
                "CommandRunner",
                side_effect=AssertionError(
                    "command-runner construction is forbidden"
                ),
                create=True,
            )
        )
        stack.enter_context(
            mock.patch.object(
                Path,
                "mkdir",
                side_effect=AssertionError("output mkdir is forbidden"),
            )
        )
        stack.enter_context(
            mock.patch.object(
                Path,
                "write_text",
                side_effect=AssertionError("output write is forbidden"),
            )
        )
        stack.enter_context(
            mock.patch.object(
                Path,
                "write_bytes",
                side_effect=AssertionError("output write is forbidden"),
            )
        )
        yield


def _all_option_strings(parser: argparse.ArgumentParser) -> set[str]:
    options: set[str] = set()
    pending = [parser]
    while pending:
        current = pending.pop()
        for action in current._actions:
            options.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                pending.extend(action.choices.values())
    return options


class TrackedPlanTests(unittest.TestCase):
    def test_tracked_plan_has_exact_target_triples_and_reviewed_catalog_pins(self):
        payload = json.loads(TRACKED_SCALAR_PLAN.read_text(encoding="utf-8"))
        self.assertEqual(
            [
                (
                    target["target_id"],
                    target["display_order_key"],
                    target["zero_based_index"],
                    target["label"],
                )
                for target in payload["targets"]
            ],
            [
                ("vehicle_speed", 1, 0, "Vehicle speed, km/h"),
                ("engine_speed", 7, 6, "Engine speed, rpm"),
                (
                    "current_engine_torque",
                    13,
                    12,
                    "Current engine torque, Nm",
                ),
                (
                    "coolant_temperature",
                    15,
                    14,
                    "Coolant temperature, °C",
                ),
                (
                    "radiator_fan_pwm",
                    16,
                    15,
                    "Desired PWM Radiator Fan, %",
                ),
                (
                    "engine_oil_pressure",
                    17,
                    16,
                    "Engine oil pressure, KPa",
                ),
                (
                    "oil_pressure_sensor",
                    18,
                    17,
                    "Oil pressure sensor, V",
                ),
                (
                    "vvt_oil_pressure",
                    19,
                    18,
                    "VVT Oil Pressure, KPa",
                ),
                (
                    "vvt_oil_temperature",
                    20,
                    19,
                    "VVT Oil Temperature, °C",
                ),
                (
                    "target_charging_voltage",
                    44,
                    43,
                    "Target Charging Voltage, V",
                ),
                (
                    "generator_duty_cycle",
                    45,
                    44,
                    "Generator Duty Cycle, %",
                ),
                (
                    "battery_voltage",
                    47,
                    46,
                    "Battery voltage, V",
                ),
                (
                    "transmission_oil_temperature",
                    188,
                    187,
                    "Transmission Oil Temperature, °C",
                ),
                (
                    "turbine_speed",
                    191,
                    190,
                    "Turbine speed, rpm",
                ),
                (
                    "output_speed",
                    192,
                    191,
                    "Output Speed, rpm",
                ),
            ],
        )

    def test_tracked_tcm_plan_is_exact_and_deliberately_unpinned(self):
        plan = scalar.load_plan(TRACKED_TCM_SCALAR_PLAN)
        self.assertEqual(plan.catalog_plan.module_key, "tcm")
        self.assertEqual(
            [
                (
                    target.target_id,
                    target.display_order_key,
                    target.zero_based_index,
                    target.label,
                )
                for target in plan.targets
            ],
            [
                ("vehicle_speed", 1, 0, "Vehicle speed, km/h"),
                ("engine_speed", 6, 5, "Engine speed, rpm"),
                (
                    "converter_slip_speed",
                    7,
                    6,
                    "Torque Converter Slip Speed, rpm",
                ),
                ("turbine_speed", 8, 7, "Turbine speed, rpm"),
                (
                    "gearbox_output_speed",
                    9,
                    8,
                    "Gearbox output revs, rpm",
                ),
                (
                    "output_calculated_vehicle_speed",
                    10,
                    9,
                    "Vehicle Speed Calculated By Output Shaft Speed, km/h",
                ),
                (
                    "tcu_chip_temperature",
                    15,
                    14,
                    "TCU chip temperature, °C",
                ),
                (
                    "gearbox_oil_temperature",
                    16,
                    15,
                    "Gearbox oil temperature, °C",
                ),
                (
                    "actual_crankshaft_torque",
                    17,
                    16,
                    "Actual Crankshaft Torque, Nm",
                ),
                (
                    "crankshaft_torque_without_tcu_requests",
                    18,
                    17,
                    "Crankshaft Torque, without TCU Torque Requests, Nm",
                ),
                (
                    "target_crankshaft_torque",
                    19,
                    18,
                    "Target Crankshaft Torque, Nm",
                ),
                (
                    "transmission_torque_intervention",
                    20,
                    19,
                    "Transmission Torque Intervention, Nm",
                ),
                (
                    "maximum_engine_torque_request",
                    21,
                    20,
                    "Maximum Engine Torque Requested By Transmission, Nm",
                ),
                (
                    "slow_path_torque_intervention",
                    22,
                    21,
                    "Slow Path Transmission Torque Intervention, Nm",
                ),
            ],
        )
        blockers = scalar.execution_blockers(plan)
        self.assertIn(
            "catalog expected_catalog_sha256 is null",
            " ".join(blockers),
        )
        self.assertNotIn(
            "catalog module_key must be",
            " ".join(blockers),
        )

    @unittest.skipUnless(
        TRACKED_CATALOG_REPORT.exists() and TRACKED_CATALOG_STATE.exists(),
        "live catalog evidence is absent from the portable source bundle",
    )
    def test_tracked_plan_reviewed_catalog_pins_audit(self):
        plan = scalar.load_plan(TRACKED_SCALAR_PLAN)
        audit = scalar.offline_audit(plan)
        self.assertEqual(audit["implementation_status"], "offline_gates_only")
        self.assertFalse(audit["live_execution_enabled"])
        self.assertFalse(audit["execution_ready"])
        self.assertTrue(audit["pinning_prerequisites_ready"])
        self.assertEqual(audit["pinning_blockers"], [])
        self.assertIn(
            "live selector mutation/scan execution is intentionally disabled",
            audit["live_blocker"],
        )

    def test_plan_and_audit_are_offline_and_create_no_output(self):
        with _fixture() as fixture:
            before = {
                path.relative_to(fixture.root)
                for path in fixture.root.rglob("*")
            }
            plan_stdout = io.StringIO()
            audit_stdout = io.StringIO()
            with _forbid_external_or_output_calls():
                with contextlib.redirect_stdout(plan_stdout):
                    plan_status = scalar.main(
                        ["plan", str(fixture.scalar_path)]
                    )
                with contextlib.redirect_stdout(audit_stdout):
                    audit_status = scalar.main(
                        ["audit", str(fixture.scalar_path)]
                    )
            after = {
                path.relative_to(fixture.root)
                for path in fixture.root.rglob("*")
            }

        self.assertEqual(plan_status, 0)
        self.assertEqual(audit_status, 0)
        self.assertEqual(before, after)
        self.assertIn("OFFLINE PLAN ONLY", plan_stdout.getvalue())
        self.assertIn("OFFLINE AUDIT ONLY", audit_stdout.getvalue())
        self.assertIn(
            '"pinning_prerequisites_ready": true',
            audit_stdout.getvalue(),
        )
        self.assertIn(
            '"live_execution_enabled": false',
            audit_stdout.getvalue(),
        )


class ScalarPlanValidationTests(unittest.TestCase):
    def test_duplicate_target_fields_and_off_by_one_are_rejected(self):
        cases = {
            "duplicate target_id": lambda fixture: fixture.scalar_payload[
                "targets"
            ][1].__setitem__("target_id", "alpha"),
            "duplicate display_order_key": lambda fixture: (
                fixture.scalar_payload["targets"][1].update(
                    {
                        "display_order_key": 1,
                        "zero_based_index": 0,
                    }
                )
            ),
            "duplicate label": lambda fixture: fixture.scalar_payload[
                "targets"
            ][1].__setitem__("label", "A"),
            "off by one": lambda fixture: fixture.scalar_payload["targets"][
                1
            ].__setitem__("display_order_key", 5),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name), _fixture() as fixture:
                mutate(fixture)
                fixture.write_scalar()
                with self.assertRaises(scalar.CampaignError):
                    fixture.load()

    def test_unknown_schedule_target_is_rejected(self):
        with _fixture() as fixture:
            fixture.scalar_payload["schedule"] = ["alpha", "unknown"]
            fixture.write_scalar()
            with self.assertRaisesRegex(
                scalar.CampaignError,
                "unknown target IDs",
            ):
                fixture.load()

    def test_review_report_and_all_provenance_pins_validate(self):
        with _fixture() as fixture:
            plan = fixture.load()
            self.assertEqual(scalar.execution_blockers(plan), [])
            scalar.require_execution_ready(plan)
            report_errors = scalar._review_report_errors(plan)

        self.assertEqual(report_errors, [])

    def test_report_semantics_fail_closed(self):
        def bad_classification(fixture: _PinnedFixture) -> None:
            fixture.report_payload["classification"] = "candidate_guess"

        def dirty_validation(fixture: _PinnedFixture) -> None:
            fixture.report_payload["validation"] = {
                "passed": True,
                "errors": ["synthetic error"],
            }

        def selection_committed(fixture: _PinnedFixture) -> None:
            fixture.report_payload["selection_committed"] = True

        def unpinned_validation(fixture: _PinnedFixture) -> None:
            fixture.report_payload["validation"][
                "hash_pinned_before_run"
            ] = False

        def missing_reverse_pages(fixture: _PinnedFixture) -> None:
            fixture.report_payload["pages"] = [
                {"phase": "forward", "page_index": 0}
            ]

        def missing_conditions(fixture: _PinnedFixture) -> None:
            fixture.report_payload["conditions"] = ""

        def missing_adb_serial(fixture: _PinnedFixture) -> None:
            fixture.report_payload["adb_serial"] = None

        def bad_row_index(fixture: _PinnedFixture) -> None:
            fixture.report_payload["catalog"][2][
                "display_order_key"
            ] = 4

        def bad_checked_type(fixture: _PinnedFixture) -> None:
            fixture.report_payload["catalog"][2]["checked"] = 0

        def wrong_inventory_run(fixture: _PinnedFixture) -> None:
            fixture.report_payload["plan"]["campaign_id"] = "other-run"

        cases = (
            (
                "classification",
                bad_classification,
                "unacceptable classification",
            ),
            ("validation", dirty_validation, "did not pass cleanly"),
            (
                "selection",
                selection_committed,
                "selection_committed=false",
            ),
            (
                "pre-run hash pin",
                unpinned_validation,
                "hash-pinned validation state contradicts",
            ),
            (
                "reverse traversal",
                missing_reverse_pages,
                "both forward and reverse",
            ),
            (
                "conditions",
                missing_conditions,
                "lacks non-empty vehicle/UI conditions",
            ),
            (
                "ADB serial",
                missing_adb_serial,
                "lacks a resolved ADB serial",
            ),
            ("row index", bad_row_index, "indices are not exact"),
            (
                "checked state",
                bad_checked_type,
                "lacks boolean checked state",
            ),
            (
                "inventory provenance",
                wrong_inventory_run,
                "inventory_run_id does not match",
            ),
        )
        for name, mutate, expected in cases:
            with self.subTest(name=name), _fixture() as fixture:
                mutate(fixture)
                fixture.write_report()
                fixture.write_scalar()
                blockers = scalar.execution_blockers(fixture.load())
                self.assertIn(expected, " ".join(blockers))

    def test_embedded_catalog_plan_safety_fields_must_match_exactly(self):
        def change_app(plan: dict[str, object]) -> None:
            plan["expected_app_version"] = "0.0-test"

        def change_profile(plan: dict[str, object]) -> None:
            plan["module_key"] = "cluster"

        def change_screen(plan: dict[str, object]) -> None:
            plan["expected_screen"]["width"] = 801

        def change_connection(plan: dict[str, object]) -> None:
            plan["expected_connection_texts"] = [
                "Connected to a different PCM profile"
            ]

        for name, mutate in (
            ("app version", change_app),
            ("module/profile", change_profile),
            ("screen", change_screen),
            ("connection text", change_connection),
        ):
            with self.subTest(name=name), _fixture() as fixture:
                embedded = fixture.report_payload["plan"]
                self.assertIsInstance(embedded, dict)
                mutate(embedded)
                fixture.write_report()
                fixture.write_scalar()
                blockers = scalar.execution_blockers(fixture.load())
                self.assertIn(
                    "embedded plan does not match the reviewed catalog "
                    "plan safety fields",
                    " ".join(blockers),
                )

    def test_completion_state_semantics_and_hash_are_pinned(self):
        def incomplete(fixture: _PinnedFixture) -> None:
            fixture.state_payload["phase"] = "inventory"

        def manual_reconcile(fixture: _PinnedFixture) -> None:
            fixture.state_payload["manual_reconcile"] = True

        def wrong_campaign(fixture: _PinnedFixture) -> None:
            fixture.state_payload["campaign_id"] = "different-inventory"

        def wrong_catalog_hash(fixture: _PinnedFixture) -> None:
            fixture.state_payload["catalog_sha256"] = "0" * 64

        def unpinned(fixture: _PinnedFixture) -> None:
            fixture.state_payload["hash_pinned_before_run"] = False

        cases = (
            ("incomplete", incomplete, "state is not complete"),
            (
                "manual reconcile",
                manual_reconcile,
                "does not prove manual_reconcile=false",
            ),
            ("campaign", wrong_campaign, "state campaign mismatch"),
            (
                "catalog hash",
                wrong_catalog_hash,
                "state hash does not match the pinned catalog",
            ),
            (
                "pre-run pin",
                unpinned,
                "state pin status contradicts report classification",
            ),
        )
        for name, mutate, expected in cases:
            with self.subTest(name=name), _fixture() as fixture:
                mutate(fixture)
                fixture.write_state()
                fixture.write_scalar()
                blockers = scalar.execution_blockers(fixture.load())
                self.assertIn(expected, " ".join(blockers))

        with self.subTest(name="state file SHA-256"), _fixture() as fixture:
            fixture.review["catalog_state_sha256"] = "0" * 64
            fixture.write_scalar(pin_state=False)
            blockers = scalar.execution_blockers(fixture.load())
            self.assertIn(
                "completion-state SHA-256 mismatch",
                " ".join(blockers),
            )

    def test_excessive_initial_checked_count_is_rejected(self):
        with _fixture() as fixture:
            fixture.scalar_payload["max_initial_checked"] = 1
            fixture.report_payload["catalog"][0]["checked"] = True
            fixture.report_payload["catalog"][1]["checked"] = True
            fixture.write_report()
            fixture.write_scalar()
            blockers = scalar.execution_blockers(fixture.load())

        self.assertIn("checked-gauge count 2 exceeds cap 1", " ".join(blockers))

    def test_scalar_target_indices_require_exact_json_integer_types(self):
        cases = (
            ("zero bool", "zero_based_index", False),
            ("zero float", "zero_based_index", 0.0),
            ("zero string", "zero_based_index", "0"),
            ("order bool", "display_order_key", True),
            ("order float", "display_order_key", 1.0),
            ("order string", "display_order_key", "1"),
        )
        for name, field, value in cases:
            with self.subTest(name=name), _fixture() as fixture:
                fixture.scalar_payload["targets"][0][field] = value
                fixture.write_scalar()
                with self.assertRaisesRegex(
                    scalar.CampaignError,
                    "indices must be JSON integers",
                ):
                    fixture.load()

    def test_report_indices_require_exact_json_integer_types(self):
        cases = (
            ("zero bool", "zero_based_index", False),
            ("zero float", "zero_based_index", 0.0),
            ("zero string", "zero_based_index", "0"),
            ("order bool", "display_order_key", True),
            ("order float", "display_order_key", 1.0),
            ("order string", "display_order_key", "1"),
        )
        for name, field, value in cases:
            with self.subTest(name=name), _fixture() as fixture:
                fixture.report_payload["catalog"][0][field] = value
                fixture.write_report()
                fixture.write_scalar()
                blockers = scalar.execution_blockers(fixture.load())
                self.assertIn(
                    "catalog indices are not exact/sequential",
                    " ".join(blockers),
                )

    def test_nonfinite_json_is_rejected_in_plan_report_and_state(self):
        values = (
            ("NaN", float("nan")),
            ("Infinity", float("inf")),
            ("negative Infinity", float("-inf")),
        )
        for name, value in values:
            with self.subTest(source="scalar plan", value=name), _fixture() as fixture:
                fixture.scalar_payload["segment_seconds"] = value
                _write_json(fixture.scalar_path, fixture.scalar_payload)
                with self.assertRaisesRegex(
                    scalar.CampaignError,
                    "non-finite JSON constant",
                ):
                    fixture.load()

            with (
                self.subTest(source="catalog report", value=name),
                _fixture() as fixture,
            ):
                fixture.report_payload["pages"][0]["synthetic_number"] = value
                fixture.write_report()
                fixture.write_scalar()
                blockers = scalar.execution_blockers(fixture.load())
                self.assertIn(
                    "invalid reviewed catalog report: non-finite JSON constant",
                    " ".join(blockers),
                )

            with (
                self.subTest(source="completion state", value=name),
                _fixture() as fixture,
            ):
                fixture.state_payload["synthetic_number"] = value
                fixture.write_state()
                fixture.write_scalar()
                blockers = scalar.execution_blockers(fixture.load())
                self.assertIn(
                    "invalid reviewed catalog completion state: "
                    "non-finite JSON constant",
                    " ".join(blockers),
                )

    def test_huge_json_integers_raise_campaign_error_not_overflow(self):
        huge_integer = int("9" * 400)

        with self.subTest(source="scalar plan"), _fixture() as fixture:
            fixture.scalar_payload["segment_seconds"] = huge_integer
            _write_json(fixture.scalar_path, fixture.scalar_payload)
            with self.assertRaises(scalar.CampaignError):
                fixture.load()

        with self.subTest(source="referenced catalog"), _fixture() as fixture:
            fixture.catalog_payload["settle_seconds"] = huge_integer
            fixture.write_catalog()
            with self.assertRaises(scalar.CampaignError):
                fixture.load()

    def test_full_catalog_hash_and_target_triples_are_recomputed(self):
        with self.subTest("ordered label hash"), _fixture() as fixture:
            fixture.report_payload["catalog"][1]["label"] = "Changed B"
            fixture.write_report()
            fixture.write_scalar()
            blockers = scalar.execution_blockers(fixture.load())
            self.assertIn(
                "catalog SHA-256 does not match its full ordered labels",
                " ".join(blockers),
            )

        with self.subTest("target triple"), _fixture() as fixture:
            fixture.scalar_payload["targets"][1]["label"] = "Not D"
            fixture.write_scalar()
            blockers = scalar.execution_blockers(fixture.load())
            self.assertIn(
                "target triple mismatch for delta",
                " ".join(blockers),
            )

    def test_report_catalog_and_scalar_source_hash_mismatches_block(self):
        with self.subTest("report source"), _fixture() as fixture:
            fixture.review["catalog_report_sha256"] = "0" * 64
            fixture.write_scalar(pin_report=False)
            blockers = scalar.execution_blockers(fixture.load())
            self.assertIn(
                "reviewed catalog report SHA-256 mismatch",
                " ".join(blockers),
            )

        with self.subTest("catalog plan source"), _fixture() as fixture:
            fixture.review["catalog_plan_sha256"] = "0" * 64
            fixture.write_scalar(pin_catalog=False)
            blockers = scalar.execution_blockers(fixture.load())
            self.assertIn(
                "catalog plan source SHA-256 differs",
                " ".join(blockers),
            )

        with self.subTest("scalar plan review"), _fixture() as fixture:
            fixture.scalar_payload["segment_seconds"] = 46
            _write_json(fixture.scalar_path, fixture.scalar_payload)
            blockers = scalar.execution_blockers(fixture.load())
            self.assertIn(
                "scalar plan review SHA-256 differs",
                " ".join(blockers),
            )

    def test_review_hash_ignores_only_its_own_field(self):
        with _fixture() as fixture:
            payload = deepcopy(fixture.scalar_payload)
            original = scalar._reviewable_plan_sha256(payload)

            self_hash_changed = deepcopy(payload)
            self_hash_changed["catalog_review"][
                "scalar_plan_sha256"
            ] = "f" * 64
            self.assertEqual(
                scalar._reviewable_plan_sha256(self_hash_changed),
                original,
            )

            mutations = {
                "target": lambda item: item["targets"][0].__setitem__(
                    "label", "Changed A"
                ),
                "schedule": lambda item: item.__setitem__(
                    "schedule", ["delta", "alpha"]
                ),
                "timing": lambda item: item.__setitem__(
                    "segment_seconds", 46
                ),
                "artifact policy": lambda item: item.__setitem__(
                    "stop_stability_observations", 4
                ),
                "report pin": lambda item: item["catalog_review"].__setitem__(
                    "catalog_report_sha256", "0" * 64
                ),
                "state pin": lambda item: item["catalog_review"].__setitem__(
                    "catalog_state_sha256", "0" * 64
                ),
                "reviewer": lambda item: item["catalog_review"].__setitem__(
                    "reviewed_by", "different-reviewer"
                ),
            }
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    changed = deepcopy(payload)
                    mutate(changed)
                    self.assertNotEqual(
                        scalar._reviewable_plan_sha256(changed),
                        original,
                    )


class CliSafetyTests(unittest.TestCase):
    def test_parser_has_no_force_or_catalog_target_override_surface(self):
        options = _all_option_strings(scalar._parser())
        forbidden = {
            "--force",
            "--target",
            "--target-id",
            "--label",
            "--index",
            "--zero-based-index",
            "--display-order-key",
            "--catalog-sha256",
            "--catalog-report-sha256",
            "--catalog-plan-sha256",
            "--scalar-plan-sha256",
            "--reviewed-by",
        }
        self.assertTrue(forbidden.isdisjoint(options), options & forbidden)

    def test_status_reads_only_the_existing_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            campaign.mkdir()
            state = '{"status":"synthetic","manual_reconcile":false}\n'
            (campaign / "state.json").write_text(state, encoding="utf-8")
            before = {
                path.relative_to(campaign)
                for path in campaign.rglob("*")
            }
            stdout = io.StringIO()
            with _forbid_external_or_output_calls():
                with contextlib.redirect_stdout(stdout):
                    status = scalar.main(["status", str(campaign)])
            after = {
                path.relative_to(campaign)
                for path in campaign.rglob("*")
            }

        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue(), state)
        self.assertEqual(before, after)

    def test_fully_pinned_run_still_cannot_reach_adb_subprocess_or_output(self):
        with _fixture() as fixture:
            plan = fixture.load()
            self.assertEqual(scalar.execution_blockers(plan), [])
            required_mount = fixture.root / "missing-mount"
            output = required_mount / "campaign-output"
            before = {
                path.relative_to(fixture.root)
                for path in fixture.root.rglob("*")
            }
            stderr = io.StringIO()
            with _forbid_external_or_output_calls():
                with contextlib.redirect_stderr(stderr):
                    status = scalar.main(
                        [
                            "run",
                            str(fixture.scalar_path),
                            "--adb-serial",
                            "SYNTHETIC",
                            "--campaign-id",
                            "scalar-fixture-run",
                            "--out-root",
                            str(output),
                            "--require-mount",
                            str(required_mount),
                            "--conditions",
                            "parked synthetic fixture; scan stopped",
                            "--passive-capture-campaign",
                            "passive-fixture",
                            "--execute",
                            "--confirm-read-only-diagnostics",
                            "--confirm-parked-shakedown",
                            "--confirm-scan-stopped",
                            "--confirm-debug-recording-enabled",
                            "--confirm-gauges-recording-enabled",
                            "--confirm-catalog-reviewed",
                        ]
                    )
            after = {
                path.relative_to(fixture.root)
                for path in fixture.root.rglob("*")
            }

        self.assertEqual(status, 2)
        self.assertIn(
            "live execution is intentionally disabled",
            stderr.getvalue(),
        )
        self.assertIn(
            "no ADB, service, mount, CAN, or output access was attempted",
            stderr.getvalue(),
        )
        self.assertEqual(before, after)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
