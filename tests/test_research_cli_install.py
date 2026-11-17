"""The research CLI installer against a temp CLI root and an in-process worker (fake Claude)."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from projects.vehicle_data import research_cli_install as installer_module
from projects.vehicle_data.dtc_research_worker import dispatch
from projects.vehicle_data.research_attempts import Refused
from projects.vehicle_data.research_cli_install import Abort, Installer, memory_block
from projects.vehicle_data.research_cli_policy import IdentityChecker
from tests import fake_research_cli
from tests.research_support import WorkerRig


class Client:
    def __init__(self, worker):
        self.worker = worker
        self.before_promotion = None

    def request(self, method, path, payload=None):
        if path.endswith("/end") and self.before_promotion:
            self.before_promotion()
        try:
            return dispatch(self.worker, method, path, lambda: payload or {})
        except Refused as refused:
            return 409, {"reason": refused.reason, "detail": refused.detail}


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.rig = WorkerRig()
        self.tmp = tempfile.TemporaryDirectory()
        self.source = fake_research_cli.install(Path(self.tmp.name) / "build", {"mode": "ok"},
                                                state=Path(self.tmp.name) / "owner-state")
        # the owner's installed copy is a file named by its version, like ~/.local/share/claude/versions/X
        self.owner_file = Path(self.tmp.name) / "versions" / "1.2.3"
        self.owner_file.parent.mkdir()
        self.source.rename(self.owner_file)
        self.restarts = []
        self.client = Client(self.rig.worker)
        self.installer = Installer(cli_root=self.rig.cli_root, client=self.client, poll=.05,
                                   restart=lambda: self.restarts.append(True))
        # the worker reads the staging directory under its own CLI root
        self.rig.worker.identity = IdentityChecker(self.rig.cli_root, self.rig.policy)
        self.rig.worker.runner.identity = self.rig.worker.identity

    def tearDown(self):
        self.rig.close()
        self.tmp.cleanup()

    def install(self):
        with mock.patch.object(installer_module, "OWNER_CLAUDE", self.owner_file):
            return self.installer.install_claude()

    def test_dry_run_changes_nothing(self):
        dry = Installer(cli_root=self.rig.cli_root, client=self.client, dry_run=True)
        with mock.patch.object(installer_module, "OWNER_CLAUDE", self.owner_file):
            self.assertEqual(dry.install_claude(), 0)
        self.assertFalse((self.rig.cli_root / "staging").exists())
        self.assertEqual(self.restarts, [])

    def test_install_verifies_in_the_worker_then_promotes_and_records_the_digest(self):
        self.assertEqual(self.install(), 0)
        identity = json.loads((self.rig.cli_root / "identity.json").read_text())["providers"]["claude"]
        self.assertEqual(identity["version"], "1.2.3")
        self.assertEqual(identity["verification"]["result"], "passed")
        self.assertEqual(identity["verification"]["digest"], identity["digest"])
        self.assertEqual(self.restarts, [True])
        self.assertEqual([p.name for p in (self.rig.cli_root / "claude").iterdir()], ["1.2.3"])
        self.assertEqual(IdentityChecker(self.rig.cli_root, self.rig.policy).check("claude").state, "ok")

    def test_failed_verification_keeps_the_installed_copy(self):
        fake_research_cli.set_scenario(Path(self.tmp.name) / "owner-state", {"help": "--print only"})
        with self.assertRaises(Abort):
            self.install()
        self.assertEqual(IdentityChecker(self.rig.cli_root, self.rig.policy).check("claude").state, "ok")
        entry = json.loads((self.rig.cli_root / "identity.json").read_text())["providers"]["claude"]
        self.assertIn("lacks flags", entry["last_failed_install"]["reason"])
        self.assertFalse(any((self.rig.cli_root / "staging").iterdir()))
        self.assertIsNone(self.rig.worker.table.current)  # the reservation was ended

    def test_busy_worker_refuses_the_install(self):
        self.rig.worker.admit_maintenance({"attempt_id": "1" * 32, "purpose": "install",
                                           "expected_instance_id": self.rig.worker.table.instance_id})
        with self.assertRaises(Abort) as busy:
            self.install()
        self.assertIn("busy", str(busy.exception))
        self.assertFalse(any((self.rig.cli_root / "staging").iterdir()))

    def test_an_expired_reservation_is_never_promoted(self):
        original = self.installer.still_open

        def expire_first(attempt_id, instance_id):
            self.rig.worker.table.current.detail["deadline"] = 0
            self.rig.worker.expire_maintenance()
            return original(attempt_id, instance_id)

        self.installer.still_open = expire_first
        with self.assertRaises(Abort) as expired:
            self.install()
        self.assertIn("expired", str(expired.exception))
        self.assertEqual(self.restarts, [])
        self.assertEqual([p.name for p in (self.rig.cli_root / "claude").iterdir()], ["9.9.9"])

    def test_memory_block_from_a_measurement(self):
        block = memory_block({"cli_digest": "d" * 64, "cli_version": "2.1.325", "figures": {
            "report": {"peak_tree_hwm_mib": 300.4}, "follow_up": {"peak_tree_hwm_mib": 250}}}, 150)
        self.assertEqual(block["measured_values"], {"launch_floor_mib": 704, "tree_cap_mib": 512, "critical_mib": 300})
        self.assertEqual(block["cli_digest"], "d" * 64)
        bigger_chat = memory_block({"cli_digest": "d", "figures": {"report": {"peak_tree_hwm_mib": 100}}}, 400)
        self.assertEqual(bigger_chat["measured_values"]["launch_floor_mib"], 832)  # reserve grows by 320


if __name__ == "__main__":
    unittest.main()
