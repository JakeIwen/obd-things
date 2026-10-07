"""Hardware gaps cannot qualify parked/running warnings or clear an episode."""

from datetime import timedelta

from projects.vehicle_data.early_warning import EarlyWarningEvaluator
from tests.test_can_missing_consumers import LIFECYCLE_BASES, unavailable_vehicle
from tests.test_vehicle_historian import available, snapshot, unavailable
from tests.test_warning_rules_v3 import Harness, rule_by_key


class CanUnavailableWarningTests(Harness):
    def store(self, historian, second, basis, values):
        at = self.start + timedelta(seconds=second)
        metrics = {
            name: available(definition, values[name], at)
            if name in values else unavailable(definition)
            for name, definition in self.defs.items()
        }
        payload = snapshot(at, list(self.defs.values()), metrics, running=False)
        if basis is not None:
            payload["status"]["vehicle_state"] = unavailable_vehicle(basis)
        historian.ingest_snapshot(payload, captured_at=at)
        return at

    def test_missing_can_cannot_qualify_as_parked_even_with_fresh_low_voltage(self):
        rule = rule_by_key("battery_voltage_low_parked")
        for basis in LIFECYCLE_BASES:
            with self.subTest(basis=basis):
                historian = self.open()
                evaluator = EarlyWarningEvaluator(historian, (rule,))
                for second in (0, 6, 12, 60):
                    at = self.store(historian, second, basis, {"battery.voltage": 11.4})
                    result = evaluator.evaluate(at=at)["assessments"][0]
                    self.assertEqual(result["state"], "unavailable")

    def test_missing_can_cannot_qualify_as_running_from_cached_motion(self):
        rule = rule_by_key("engine_coolant_temperature_hot")
        historian = self.open()
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        for second in range(0, 66, 6):
            at = self.store(historian, second, LIFECYCLE_BASES[0], {
                "engine.rpm": 800, "vehicle.speed": 0,
                "engine.coolant_temperature": 240,
            })
            self.assertEqual(evaluator.evaluate(at=at)["assessments"][0]["state"], "unavailable")

    def test_missing_coverage_does_not_clear_confirmed_warning(self):
        rule = rule_by_key("battery_voltage_low_parked")
        historian = self.open()
        evaluator = EarlyWarningEvaluator(historian, (rule,))
        for second in (0, 6, 12):
            at = self.store(historian, second, None, {"engine.rpm": 0, "battery.voltage": 11.4})
            result = evaluator.evaluate(at=at)["assessments"][0]
            historian.record_advisory_assessments([result], evaluated_at=at)
        self.assertEqual(result["state"], "warning")
        episode_id = historian.list_advisory_episodes(active_only=True)[0]["id"]
        for second in (18, 24, 30, 60):
            at = self.store(historian, second, LIFECYCLE_BASES[0], {"battery.voltage": 14})
            result = evaluator.evaluate(at=at)["assessments"][0]
            historian.record_advisory_assessments([result], evaluated_at=at)
            active = historian.list_advisory_episodes(active_only=True)
            self.assertEqual([episode["id"] for episode in active], [episode_id])
