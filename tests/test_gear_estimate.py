"""Gear estimate from the 0x1F7 shaft-speed ratio (transmission.gear_estimate)."""

import unittest

from projects.vehicle_data import ccan_powertrain as cp
from projects.vehicle_data.metrics import METRICS


def obs(metric, value):
    return cp.PassiveObservation(metric=metric, value=value, unit="", source="x", quality="observed_alfa_scale", detail="")


def snapshot(turbine, output, rpm=2000.0, mph=40.0):
    return (
        obs("transmission.turbine_speed", turbine),
        obs("transmission.output_speed", output),
        obs("engine.rpm", rpm),
        obs("vehicle.speed", mph),
    )


class GearEstimateTests(unittest.TestCase):
    def test_each_frozen_band_maps_to_its_gear(self):
        for label, center in cp.GEAR_RATIO_BANDS:
            self.assertEqual(cp.gear_from_ratio(center), label)
            self.assertEqual(cp.gear_from_ratio(center * 1.029), label)

    def test_between_bands_and_eight_nine_are_unknown(self):
        self.assertIsNone(cp.gear_from_ratio(0.58))  # nominal 8th
        self.assertIsNone(cp.gear_from_ratio(0.48))  # nominal 9th
        self.assertIsNone(cp.gear_from_ratio(1.18))  # mid-shift between 4 and 5
        self.assertIsNone(cp.gear_from_ratio(float("nan")))

    def test_estimate_only_while_moving(self):
        est = cp.gear_estimate(snapshot(1398.0, 2000.0))
        self.assertEqual(est.value, "7")
        self.assertEqual(est.source, cp.GEAR_ESTIMATE_SOURCE)
        self.assertEqual(est.quality, "candidate")
        self.assertIsNone(cp.gear_estimate(snapshot(700.0, 0.0, rpm=750.0, mph=0.0)))
        self.assertIsNone(cp.gear_estimate(snapshot(1398.0, 2000.0, mph=2.0)))
        self.assertIsNone(cp.gear_estimate(snapshot(1398.0, 2000.0, rpm=450.0)))

    def test_forward_upshift_through_reverse_band_is_not_reverse(self):
        """2026-09-24: 1->2 upshifts crossed R's ratio at 8.2 and 13.8 mph."""
        r = dict(cp.GEAR_RATIO_BANDS)["R"]
        self.assertIsNone(cp.gear_estimate(snapshot(r * 300.0, 300.0, mph=8.2)))
        self.assertEqual(cp.gear_estimate(snapshot(r * 150.0, 150.0, rpm=900.0, mph=4.0)).value, "R")

    def test_registry_accepts_exactly_the_published_labels(self):
        definition = METRICS["transmission.gear_estimate"]
        self.assertEqual(definition.value_type, "string")
        source = definition.sources[0]
        self.assertEqual(source.name, cp.GEAR_ESTIMATE_SOURCE)
        self.assertEqual(source.quality, "candidate")
        self.assertEqual(set(source.publisher_values), {label for label, _ in cp.GEAR_RATIO_BANDS})


if __name__ == "__main__":
    unittest.main()
