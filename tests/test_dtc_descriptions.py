import unittest

from projects.vehicle_data.dtc_descriptions import (
    OEM_EXACT_VEHICLE,
    REPOSITORY_VERIFIED_VEHICLE,
    STANDARD_SUBTYPE_ONLY,
    describe_dtc,
)


class DtcDescriptionTests(unittest.TestCase):
    def test_exact_vehicle_title_is_module_scoped(self):
        result = describe_dtc("telematics", "U0100-00")

        self.assertTrue(result["description_reviewed"])
        self.assertEqual(result["description_source"], OEM_EXACT_VEHICLE)
        self.assertIn("Engine Control Module", result["description"])

        other_module = describe_dtc("cluster", "U0100-00")
        self.assertFalse(other_module["description_reviewed"])

    def test_repository_verified_tpms_meaning_wins_over_other_module_context(self):
        result = describe_dtc("rf_hub", "C1503-31")

        self.assertEqual(
            result["description"],
            "Tire pressure sensor (rear left) — No signal",
        )
        self.assertEqual(
            result["description_source"], REPOSITORY_VERIFIED_VEHICLE
        )

    def test_unknown_component_reports_only_standard_failure_subtype(self):
        result = describe_dtc("shifter", "P1C73-24")

        self.assertFalse(result["description_reviewed"])
        self.assertEqual(result["description_source"], STANDARD_SUBTYPE_ONLY)
        self.assertIn("No reviewed module-specific", result["description"])
        self.assertIn("Signal stuck high", result["description"])


if __name__ == "__main__":
    unittest.main()
