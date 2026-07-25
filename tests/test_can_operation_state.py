import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lib import can_operation_state as state


class OperationStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.directory.name)
        self.state_patch = mock.patch.object(state, "STATE_DIR", self.state_dir)
        self.boot_patch = mock.patch.object(
            state, "current_boot_id", return_value="boot-current"
        )
        self.state_patch.start()
        self.boot_patch.start()

    def tearDown(self):
        self.boot_patch.stop()
        self.state_patch.stop()
        self.directory.cleanup()

    def test_same_boot_explicit_topology_is_usable(self):
        written = state.set_topology(
            "can0",
            "c-can",
            pair="6/14",
            source="test",
        )
        self.assertTrue(written.usable)
        self.assertEqual(written.bus, "c-can")
        self.assertEqual(state.load_topology("can0").pair, "6/14")

    def test_topology_from_another_boot_fails_closed(self):
        state.set_topology("can0", "b-can", pair="3/11", source="test")
        with mock.patch.object(
            state, "current_boot_id", return_value="boot-next"
        ):
            loaded = state.load_topology("can0")
        self.assertFalse(loaded.usable)
        self.assertEqual(loaded.bus, "unknown")
        self.assertIn("another boot", loaded.reason)

    def test_missing_or_malformed_topology_fails_closed(self):
        self.assertFalse(state.load_topology("can0").usable)
        path = self.state_dir / "topology-can0.json"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")
        loaded = state.load_topology("can0")
        self.assertFalse(loaded.usable)
        self.assertIn("malformed", loaded.reason)

    def test_same_boot_inhibit_blocks_until_explicit_end(self):
        state.begin_inhibit(
            "alfaobd", channel="can0", reason="test campaign"
        )
        active = state.active_inhibits("can0")
        self.assertEqual([item["name"] for item in active], ["alfaobd"])
        self.assertTrue(state.end_inhibit("alfaobd"))
        self.assertEqual(state.active_inhibits("can0"), ())

    def test_stale_boot_inhibit_does_not_survive_reboot(self):
        state.begin_inhibit(
            "alfaobd", channel="can0", reason="test campaign"
        )
        with mock.patch.object(
            state, "current_boot_id", return_value="boot-next"
        ):
            self.assertEqual(state.active_inhibits("can0"), ())

    def test_malformed_inhibit_blocks_wake(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "inhibit-broken.json").write_text(
            "{bad", encoding="utf-8"
        )
        active = state.active_inhibits("can0")
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0]["invalid"])
