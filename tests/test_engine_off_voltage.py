import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from projects.vehicle_data.engine_off_voltage import (
    EngineOffVoltageCapture,
    MAX_STATUS_BYTES,
    read_engine_off_voltage,
)
from projects.vehicle_data.models import failure, success


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class EngineOffVoltageCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.temporary.name) / "engine-off-voltage.json"
        self.clock = Clock()
        self.stopped_at = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temporary.cleanup()

    def sample(self, value, seconds):
        return success(
            metric="battery.voltage",
            unit="V",
            value=value,
            source="ccan.broadcast.0x41a",
            bus="c-can",
            acquisition="passive",
            interface_mode="listen_only",
            quality="verified",
            observed_monotonic=self.clock.value,
            observed_at=self.stopped_at + timedelta(seconds=seconds),
        )

    def test_saves_newest_passive_sample_after_settling_window(self):
        capture = EngineOffVoltageCapture(
            self.path,
            settle_seconds=30,
            monotonic=self.clock,
            wall_clock=lambda: self.stopped_at + timedelta(seconds=31),
        )
        capture.arm(engine_stopped_at=self.stopped_at)

        self.clock.value = 5
        self.assertFalse(capture.observe(self.sample(12.5, 5)))
        self.clock.value = 24
        self.assertFalse(capture.observe(self.sample(12.7, 24)))
        self.clock.value = 30
        unavailable = failure(
            metric="battery.voltage",
            unit="V",
            reason="bus_asleep",
            detail="silent",
            bus="c-can",
            acquisition="passive",
            interface_mode="listen_only",
        )
        self.assertTrue(capture.observe(unavailable))

        saved = read_engine_off_voltage(self.path)
        self.assertEqual(saved["value"], 12.7)
        self.assertEqual(saved["source"], "ccan.broadcast.0x41a")
        self.assertEqual(saved["acquisition"], "passive")
        self.assertEqual(capture.status_snapshot()["state"], "complete")

    def test_never_accepts_wake_assisted_or_pre_stop_voltage(self):
        capture = EngineOffVoltageCapture(
            self.path,
            settle_seconds=10,
            monotonic=self.clock,
            wall_clock=lambda: self.stopped_at + timedelta(seconds=11),
        )
        capture.arm(engine_stopped_at=self.stopped_at)
        wake = success(
            metric="battery.voltage",
            unit="V",
            value=12.7,
            source="bcan.broadcast.0x46c",
            bus="b-can",
            acquisition="wake_assisted",
            quality="verified",
            observed_monotonic=0,
            observed_at=self.stopped_at + timedelta(seconds=2),
        )
        self.assertFalse(capture.observe(wake))
        self.clock.value = 5
        self.assertFalse(capture.observe(self.sample(13.9, -1)))
        self.clock.value = 10
        self.assertTrue(capture.observe(wake))

        self.assertFalse(self.path.exists())
        status = capture.status_snapshot()
        self.assertEqual(status["state"], "no_sample")
        self.assertIn("no fresh passive voltage", status["last_error"])

    def test_engine_restart_cancels_pending_capture(self):
        capture = EngineOffVoltageCapture(
            self.path,
            monotonic=self.clock,
        )
        capture.arm(engine_stopped_at=self.stopped_at)

        self.assertTrue(capture.cancel("engine running again"))
        self.clock.value = 60
        self.assertFalse(capture.observe(self.sample(14.1, 60)))
        self.assertFalse(self.path.exists())
        self.assertEqual(capture.status_snapshot()["state"], "cancelled")

    def test_reader_rejects_links_oversized_and_malformed_state(self):
        target = pathlib.Path(self.temporary.name) / "target.json"
        target.write_text("{}", encoding="utf-8")
        linked = pathlib.Path(self.temporary.name) / "linked.json"
        linked.symlink_to(target)
        self.assertIsNone(read_engine_off_voltage(linked))

        self.path.write_bytes(b"x" * (MAX_STATUS_BYTES + 1))
        self.assertIsNone(read_engine_off_voltage(self.path))
        self.path.write_text(json.dumps({"version": 1}), encoding="utf-8")
        self.assertIsNone(read_engine_off_voltage(self.path))


if __name__ == "__main__":
    unittest.main()
