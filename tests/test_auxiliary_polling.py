import dataclasses
import json
import subprocess
import sys
import unittest

from projects.vehicle_data import auxiliary_polling as polling


class AuxiliaryPollingTests(unittest.TestCase):
    def test_fixed_plan_is_single_bcan_target_and_canch_passive_only(self):
        plan = polling.plan()

        self.assertFalse(plan["live_can"])
        self.assertEqual(plan["active_bus"], "b-can")
        self.assertEqual(plan["can_ch_policy"], "passive_only")
        self.assertEqual(plan["enabled_target_count"], 1)
        self.assertFalse(plan["session_control"])
        self.assertFalse(plan["tester_present"])
        self.assertTrue(plan["response_before_next_request"])
        self.assertEqual(
            {row["support"] for row in plan["targets"]},
            {"ignition_on_no_session_verified"},
        )
        self.assertEqual(
            {row["owner_visible_effects"] for row in plan["targets"]},
            {"none_observed"},
        )
        self.assertEqual(
            {(row["module_key"], row["did"]) for row in plan["targets"]},
            {("ics_bcan", "2001")},
        )
        self.assertEqual(
            {row["name"]: row["readiness"] for row in plan["targets"]},
            {
                "ics_odometer": "odometer_relationship_validation_required",
            },
        )
        self.assertEqual(
            {row["name"]: row["metric"] for row in plan["targets"]},
            {
                "ics_odometer": "vehicle.odometer",
            },
        )

    def test_ics_odometer_decoder_requires_exact_echo_and_width(self):
        # 1000.0 km -> 621.371192... mi
        self.assertAlmostEqual(
            polling.decode_ics_odometer_miles(b"\x62\x20\x01\x00\x27\x10"),
            621.371192237334,
        )
        for response in (
            b"\x62\x20\x01\x00\x27",
            b"\x62\x20\x02\x00\x27\x10",
            b"\x7f\x22\x31",
        ):
            with self.subTest(response=response), self.assertRaises(
                polling.AuxiliaryPollingPolicyError
            ):
                polling.decode_ics_odometer_miles(response)

    def test_policy_rejects_canch_safety_module_even_with_bcan_did(self):
        with self.assertRaisesRegex(
            polling.AuxiliaryPollingPolicyError, "passive-only"
        ):
            dataclasses.replace(
                polling.TARGETS[0], module_key="abs_canch"
            )

    def test_policy_rejects_unreviewed_bcan_did_and_faster_cadence(self):
        with self.assertRaisesRegex(
            polling.AuxiliaryPollingPolicyError, "fixed ICS DID"
        ):
            dataclasses.replace(polling.TARGETS[0], did=0x2002)
        with self.assertRaisesRegex(
            polling.AuxiliaryPollingPolicyError, "at least 5s"
        ):
            dataclasses.replace(
                polling.TARGETS[0], minimum_interval_seconds=1.0
            )

    def test_policy_rejects_non_boolean_deployment_state(self):
        with self.assertRaisesRegex(
            polling.AuxiliaryPollingPolicyError, "must be boolean"
        ):
            dataclasses.replace(polling.TARGETS[0], enabled=1)

    def test_policy_rejects_unverified_support_or_wrong_next_gate(self):
        with self.assertRaisesRegex(
            polling.AuxiliaryPollingPolicyError, "fixed parked support"
        ):
            dataclasses.replace(
                polling.TARGETS[0], owner_visible_effects="unconfirmed"
            )
        with self.assertRaisesRegex(
            polling.AuxiliaryPollingPolicyError, "post-parked evidence gate"
        ):
            dataclasses.replace(
                polling.TARGETS[0], readiness="variation_validation_required"
            )

    def test_cli_is_json_plan_only(self):
        result = subprocess.run(
            [sys.executable, str(polling.__file__)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["mode"], "offline_plan_only")


if __name__ == "__main__":
    unittest.main()
