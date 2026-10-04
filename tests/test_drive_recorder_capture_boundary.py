"""Offline equivalence checks for the broker recorder's capture imports."""

from types import SimpleNamespace
import unittest
from unittest import mock

from lib import capture_pipeline
from lib.broadcast_signals import B_CAN_VOLTAGE, IGNITION_ON
from lib.modules import MODULES
from projects.vehicle_data import drive_recorder
from tools import passive_drive_capture


class DriveRecorderCaptureBoundaryTests(unittest.TestCase):
    def test_priority_ids_match_passive_cli_defaults(self):
        args = passive_drive_capture.build_parser().parse_args(
            ["--out-root", "/tmp/plan"]
        )
        self.assertEqual(
            drive_recorder.priority_ids(),
            passive_drive_capture.resolved_priority_ids(args),
        )

    def test_priority_ids_resolve_current_c_can_registry_only(self):
        modules = {
            "primary": SimpleNamespace(bus="c-can", txid=0x18DA33F1, rxid=0x18DAF133),
            "body": SimpleNamespace(bus="b-can", txid=0x18DA44F1, rxid=0x18DAF144),
            "chassis": SimpleNamespace(bus="can-ch", txid=0x18DA55F1, rxid=0x18DAF155),
        }
        with mock.patch.dict(MODULES, modules, clear=True):
            args = passive_drive_capture.build_parser().parse_args(
                ["--out-root", "/tmp/plan"]
            )
            selected = drive_recorder.priority_ids()
            self.assertEqual(
                selected, passive_drive_capture.resolved_priority_ids(args)
            )
            self.assertIn(modules["primary"].txid, selected)
            self.assertIn(modules["primary"].rxid, selected)
            self.assertNotIn(modules["body"].txid, selected)
            self.assertNotIn(modules["chassis"].txid, selected)

    def test_library_boundary_and_witness_values(self):
        self.assertIs(drive_recorder.capture, capture_pipeline)
        self.assertIs(passive_drive_capture.Recorder.run, capture_pipeline.Recorder.run)
        self.assertEqual(drive_recorder.IGNITION_ID, 0x2EF)
        self.assertEqual(drive_recorder.IGNITION_ID, IGNITION_ON.can_id)
        self.assertEqual(drive_recorder.SECONDARY_START_IDS, {"b-can": 0x46C, "can-ch": 0x0DA})
        self.assertEqual(drive_recorder.SECONDARY_START_IDS["b-can"], B_CAN_VOLTAGE.can_id)
