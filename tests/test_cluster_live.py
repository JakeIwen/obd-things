import unittest
from unittest import mock

from lib.modules import MODULES
from projects.ecu_mapping import cluster_live


class ClusterLiveWrapperTests(unittest.TestCase):
    def test_metric_table_has_only_reviewed_dids_in_display_order(self):
        self.assertEqual(
            [metric.did for metric in cluster_live.METRICS],
            [0x1000, 0x1002, 0x0107, 0x1004, 0x1005],
        )
        self.assertEqual(len({metric.did for metric in cluster_live.METRICS}), 5)

    def test_unverified_decodes_are_raw_only(self):
        by_did = {metric.did: metric for metric in cluster_live.METRICS}
        for did in (0x1000, 0x1002, 0x0107, 0x1005):
            self.assertIsNone(by_did[did].fn)
            self.assertEqual(by_did[did].unit, "raw")

        self.assertIn("00=P only", by_did[0x0107].name)
        self.assertIn("candidate", by_did[0x1000].name)
        self.assertIn("candidate", by_did[0x1002].name)
        self.assertIn("candidate", by_did[0x1005].name)

    def test_battery_conversion_is_visibly_alfa_qualified(self):
        battery = next(metric for metric in cluster_live.METRICS if metric.did == 0x1004)
        self.assertIn("Alfa scale", battery.name)
        self.assertEqual(battery.unit, "V*")
        self.assertAlmostEqual(battery.fn(bytes.fromhex("76")) * battery.scale, 11.8)
        self.assertAlmostEqual(battery.fn(bytes.fromhex("79")) * battery.scale, 12.1)

    def test_main_selects_cluster_no_session_and_one_hz_cycles(self):
        with mock.patch.object(cluster_live, "run", return_value=0) as run:
            result = cluster_live.main(["--seconds", "5"])

        self.assertEqual(result, 0)
        run.assert_called_once_with(
            MODULES["cluster"],
            cluster_live.METRICS,
            title="cluster live (candidate labels)",
            refresh_hz=1.0,
            argv=["--seconds", "5"],
            session=None,
        )


if __name__ == "__main__":
    unittest.main()
