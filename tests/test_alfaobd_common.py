from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import pickle
import tempfile
import unittest
from unittest import mock

from lib import alfaobd_common
from tools import alfaobd_singleton_campaign as campaign
from tools import alfaobd_plots_catalog as catalog
from tools import alfaobd_plots_scalar_campaign as scalar
from tools import alfaobd_singleton_join as join


class AlfaobdCommonTests(unittest.TestCase):
    def test_original_bindings(self):
        for name in (
            "PACKAGE", "SAFE_ID_PREFIX", "CAMPAIGN_ID_RE", "ACTIVE_DIAGNOSTIC_IDS",
            "BLOCKING_DIALOG_TEXT", "Bounds", "UiNode", "ArtifactStat", "CampaignPlan",
            "EventWriter", "_write_bytes", "_write_text",
        ):
            self.assertIs(getattr(campaign, name), getattr(alfaobd_common, name))
        for name in ("Bounds", "UiNode", "EventWriter", "_write_bytes", "_write_text"):
            self.assertIs(getattr(catalog, name), getattr(alfaobd_common, name))
        self.assertIs(scalar.EventWriter, alfaobd_common.EventWriter)
        self.assertIs(join.CampaignPlan, alfaobd_common.CampaignPlan)

    def test_records_keep_repr_fields_and_pickle_roundtrip(self):
        bounds = alfaobd_common.Bounds(1, 3, 8, 12)
        self.assertEqual(bounds.center, (4, 7))
        self.assertEqual(repr(bounds), "Bounds(left=1, top=3, right=8, bottom=12)")
        node = alfaobd_common.UiNode("Speed", "id", "view", "package",
                                     True, False, True, True, False, bounds)
        self.assertEqual(asdict(node)["bounds"], {"left": 1, "top": 3, "right": 8, "bottom": 12})
        self.assertEqual(pickle.loads(pickle.dumps(node)), node)
        self.assertEqual(alfaobd_common.ArtifactStat("log", None).as_dict(),
                         {"path": "log", "size": None})

    def test_campaign_plan_matches_existing_plan_bytes(self):
        plan_path = Path(campaign.REPO) / "projects/ecu_mapping/configs/alfaobd_cluster_singleton_shakedown.json"
        plan = campaign.load_plan(plan_path)
        self.assertIs(type(plan), alfaobd_common.CampaignPlan)
        self.assertEqual(plan.schedule, plan.gauges + plan.repeat_anchors)
        self.assertEqual(pickle.loads(pickle.dumps(plan)), plan)
        self.assertEqual(plan.as_dict()["schedule"], list(plan.schedule))

    def test_catalog_records_and_helpers_are_reexported(self):
        for name in ("CatalogPlan", "DialogPage", "CatalogInventory", "plot_labels", "catalog_sha256"):
            shared = getattr(alfaobd_common, name)
            self.assertIs(getattr(catalog, name), shared)
            self.assertIs(getattr(scalar, name), shared)
        plan_path = Path(catalog.REPO) / "projects/ecu_mapping/configs/alfaobd_pcm_plots_catalog.json"
        plan = catalog.load_plan(plan_path)
        self.assertIs(type(plan), alfaobd_common.CatalogPlan)
        self.assertEqual(pickle.loads(pickle.dumps(plan)).as_dict(), plan.as_dict())

    def test_catalog_hash_and_label_order(self):
        import hashlib

        self.assertEqual(alfaobd_common.catalog_sha256(["Speed", "café"]),
                         hashlib.sha256(b'alfaobd-plots-catalog-v1\x00["Speed","caf\xc3\xa9"]').hexdigest())
        nodes = [
            alfaobd_common.UiNode(text, alfaobd_common.SAFE_ID_PREFIX + name,
                                 "view", "package", False, False, True, True, False,
                                 alfaobd_common.Bounds(0, 0, 1, 1))
            for name, text in (("Plot12Title", "  Second "), ("Plot2Title", "First"),
                               ("Plot1Title", " "), ("labelPar0", "Other"))
        ]
        self.assertEqual(alfaobd_common.plot_labels(nodes), ("First", "Second"))

    def test_exception_class_stays_in_original_tool(self):
        self.assertEqual(campaign.CampaignError.__qualname__, "CampaignError")
        self.assertIs(catalog.CampaignError, campaign.CampaignError)
        self.assertIs(scalar.CampaignError, campaign.CampaignError)
        self.assertIs(join.CampaignError, campaign.CampaignError)
        self.assertFalse(hasattr(alfaobd_common, "CampaignError"))
        error = campaign.CampaignError("fixture")
        self.assertEqual(repr(error), "CampaignError('fixture')")

    def test_event_and_state_exact_bytes_and_fsync(self):
        stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            writer = alfaobd_common.EventWriter(Path(directory))
            with mock.patch.object(alfaobd_common, "datetime") as clock, \
                 mock.patch.object(alfaobd_common.time, "monotonic", return_value=12.5), \
                 mock.patch.object(alfaobd_common.os, "fsync") as fsync:
                clock.now.return_value = stamp
                writer.event("observed", value="café")
                writer.state({"z": 1, "a": "café"})
            self.assertEqual(fsync.call_count, 2)
            self.assertEqual(writer.events_path.read_bytes(),
                             b'{"event": "observed", "monotonic_s": 12.5, "value": "caf\\u00e9", '
                             b'"wall_time_utc": "2026-01-02T03:04:05+00:00"}\n')
            self.assertEqual(writer.state_path.read_bytes(), b'{\n  "a": "caf\\u00e9",\n  "z": 1\n}\n')
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()),
                             ["events.jsonl", "state.json"])

    def test_state_replace_failure_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = alfaobd_common.EventWriter(Path(directory))
            writer.state({"old": True})
            with mock.patch.object(alfaobd_common.os, "replace", side_effect=OSError("fixture")):
                with self.assertRaises(OSError):
                    writer.state({"new": True})
            self.assertEqual(json.loads(writer.state_path.read_text()), {"old": True})
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["state.json"])

    def test_binary_and_text_writers_preserve_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested/artifact"
            with mock.patch.object(alfaobd_common.os, "fsync") as fsync:
                alfaobd_common._write_bytes(path, b"\x00\xff\n")
                self.assertEqual(path.read_bytes(), b"\x00\xff\n")
                alfaobd_common._write_text(path, "café\n")
                self.assertEqual(path.read_bytes(), b"caf\xc3\xa9\n")
            self.assertEqual(fsync.call_count, 2)


if __name__ == "__main__":
    unittest.main()
