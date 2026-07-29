import unittest

from projects.ecu_mapping import pcm_tigershark


class PcmTigersharkCatalogTests(unittest.TestCase):
    def test_catalog_is_complete_and_keeps_order(self):
        self.assertEqual(len(pcm_tigershark.CATALOG), 193)
        self.assertEqual(
            [definition.order for definition in pcm_tigershark.CATALOG],
            list(range(1, 194)),
        )

    def test_independent_alignment_anchors_match_catalog(self):
        for did, label in pcm_tigershark.ALIGNMENT_ANCHORS.items():
            with self.subTest(did=f"{did:04X}"):
                self.assertEqual(
                    [entry.label for entry in pcm_tigershark.BY_DID[did]],
                    [label],
                )

    def test_shared_response_dids_preserve_all_catalog_rows(self):
        self.assertEqual(
            [entry.label for entry in pcm_tigershark.BY_DID[0xFE11]],
            ["Trans Temperature Voltage", "Line Pressure Sensor"],
        )
        self.assertEqual(
            [entry.label for entry in pcm_tigershark.BY_DID[0xFE62]],
            ["Turbine speed", "Output Speed", "Transfer speed"],
        )

    def test_related_profile_eot_pair_is_not_promoted_into_selected_catalog(self):
        self.assertNotIn(0x3159, pcm_tigershark.BY_DID)
        self.assertNotIn(0x315A, pcm_tigershark.BY_DID)
        self.assertEqual(
            pcm_tigershark.RELATED_PROFILE_EOT_CANDIDATES[0x3159],
            ("Engine oil temperature", "|C", "u8 - 40"),
        )

    def test_sohc_v6_thermal_candidates_remain_separate(self):
        for did in (0xB010, 0xB011, 0xB012):
            with self.subTest(did=f"{did:04X}"):
                self.assertNotIn(did, pcm_tigershark.BY_DID)
        self.assertEqual(
            pcm_tigershark.SOHC_V6_THERMAL_CANDIDATES[0xB011],
            (
                "oil temperature, thermistor measured",
                "|C",
                "((s16be * 0.015625) - 32) / 1.8",
            ),
        )
        self.assertEqual(
            pcm_tigershark.INSTALLED_PCM_REJECTED_RELATED_THERMAL_DIDS,
            frozenset({0x3159, 0x315A, 0xB010, 0xB011, 0xB012}),
        )


if __name__ == "__main__":
    unittest.main()
