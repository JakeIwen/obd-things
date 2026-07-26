"""Subprocess coverage for the campaign commands documented in the runbook.

These tests deliberately remove ``PYTHONPATH``.  A user running
``python3 tools/<tool>.py`` from the repository root should not need an
environment-specific import path for the repository's own ``lib`` package.

Live gates are intentionally omitted from the documented execute commands:
the resulting invocations still exercise every documented argument while
remaining plan-only/inert and unable to touch ADB, CAN, services, or mounts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
CLUSTER_PLAN = (
    REPO
    / "projects"
    / "ecu_mapping"
    / "configs"
    / "alfaobd_cluster_singleton_shakedown.json"
)
PCM_PLOTS_CATALOG_PLAN = (
    REPO
    / "projects"
    / "ecu_mapping"
    / "configs"
    / "alfaobd_pcm_plots_catalog.json"
)


class CampaignCliEntrypointTests(unittest.TestCase):
    def _offline_environment(self, temporary: Path) -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        environment.update(
            {
                # An accidental child command cannot be found.  The Python
                # interpreter itself is invoked by its absolute executable.
                "PATH": "",
                "PYTHONDONTWRITEBYTECODE": "1",
                "HOME": str(temporary / "home"),
                "TMPDIR": str(temporary / "process-tmp"),
                "XDG_CACHE_HOME": str(temporary / "cache"),
            }
        )
        for name in ("home", "process-tmp", "cache"):
            (temporary / name).mkdir()
        return environment

    def _run(self, arguments: list[str], temporary: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, *arguments],
            cwd=REPO,
            env=self._offline_environment(temporary),
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )

    def test_documented_singleton_plan_runs_as_direct_script(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            result = self._run(
                [
                    "tools/alfaobd_singleton_campaign.py",
                    "plan",
                    str(CLUSTER_PLAN.relative_to(REPO)),
                ],
                temporary,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OFFLINE PLAN ONLY", result.stdout)
        self.assertIn('"module_key": "cluster"', result.stdout)

    def test_documented_plots_catalog_plan_runs_as_direct_script(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            result = self._run(
                [
                    "tools/alfaobd_plots_catalog.py",
                    "plan",
                    str(PCM_PLOTS_CATALOG_PLAN.relative_to(REPO)),
                ],
                temporary,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OFFLINE PLAN ONLY", result.stdout)
        self.assertIn('"expected_catalog_count": 193', result.stdout)

    def test_documented_passive_capture_arguments_are_plan_only_without_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            output = temporary / "must-not-exist"
            result = self._run(
                [
                    "tools/passive_drive_capture.py",
                    "--out-root",
                    str(output),
                    "--require-mount",
                    "/mnt/EXFAT512",
                    "--campaign",
                    "cluster-shakedown-20260724-120000",
                    "--duration-seconds",
                    "600",
                    "--soft-free-gib",
                    "30",
                    "--hard-free-gib",
                    "25",
                    "--confirm-passive",
                    "--conditions",
                    (
                        "parked; ignition ON; engine OFF; PCAN C-CAN 6/14; "
                        "OBDLink MX+ parallel"
                    ),
                ],
                temporary,
            )

            self.assertFalse(output.exists())

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "plan_only")
        self.assertEqual(payload["duration_seconds"], 600)
        self.assertEqual(payload["required_mount"], "/mnt/EXFAT512")
        self.assertEqual(payload["soft_free_bytes"], 30 * 1024**3)
        self.assertEqual(payload["hard_free_bytes"], 25 * 1024**3)

    def test_documented_singleton_run_arguments_are_inert_without_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            output = temporary / "must-not-exist"
            result = self._run(
                [
                    "tools/alfaobd_singleton_campaign.py",
                    "run",
                    str(CLUSTER_PLAN.relative_to(REPO)),
                    "--campaign-id",
                    "cluster-shakedown-20260724-120000",
                    "--out-root",
                    str(output),
                    "--require-mount",
                    "/mnt/EXFAT512",
                    "--confirm-read-only-diagnostics",
                    "--confirm-parked-shakedown",
                    "--confirm-monitor-stopped",
                    "--conditions",
                    "parked; ignition ON; engine OFF; cluster System-status page",
                ],
                temporary,
            )

            self.assertFalse(output.exists())

        self.assertEqual(result.returncode, 2)
        self.assertIn("run is inert without --execute", result.stderr)
        self.assertNotIn("adb", result.stderr.lower())

    def test_documented_plots_inventory_arguments_are_inert_without_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            output = temporary / "must-not-exist"
            result = self._run(
                [
                    "tools/alfaobd_plots_catalog.py",
                    "inventory",
                    str(PCM_PLOTS_CATALOG_PLAN.relative_to(REPO)),
                    "--campaign-id",
                    "pcm-plots-catalog-20260726-120000",
                    "--out-root",
                    str(output),
                    "--confirm-read-only-navigation",
                    "--confirm-parked",
                    "--confirm-scan-stopped",
                    "--conditions",
                    "parked; PCM connected; Plots page; scan stopped",
                ],
                temporary,
            )

            self.assertFalse(output.exists())

        self.assertEqual(result.returncode, 2)
        self.assertIn("inventory is inert without --execute", result.stderr)
        self.assertNotIn("adb", result.stderr.lower())

    def test_documented_recovery_arguments_plan_without_filesystem_access(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            output = temporary / "must-not-exist"
            result = self._run(
                [
                    "tools/passive_drive_capture.py",
                    "--out-root",
                    str(output),
                    "--require-mount",
                    "/mnt/EXFAT512",
                    "--campaign",
                    "EXACT_EXISTING_CAMPAIGN",
                    "--recover-partials",
                    "--confirm-recovery",
                ],
                temporary,
            )

            self.assertFalse(output.exists())

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "recovery_plan_only")
        self.assertEqual(
            payload["target"],
            str(output / "EXACT_EXISTING_CAMPAIGN"),
        )

    def test_documented_cluster_drive_arguments_are_inert_without_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            output = temporary / "must-not-exist-dids"
            raw_output = temporary / "must-not-exist-raw"
            result = self._run(
                [
                    "projects/ecu_mapping/cluster_drive_log.py",
                    "--out-root",
                    str(output),
                    "--raw-root",
                    str(raw_output),
                    "--require-mount",
                    "/mnt/EXFAT512",
                    "--campaign",
                    "cluster-drive-shakedown-20260724-120000",
                    "--duration-seconds",
                    "720",
                ],
                temporary,
            )

            self.assertFalse(output.exists())
            self.assertFalse(raw_output.exists())

        self.assertEqual(result.returncode, 0, result.stderr)
        payload_text, marker, _footer = result.stdout.partition("\nDRY RUN:")
        self.assertTrue(marker)
        payload = json.loads(payload_text)
        self.assertEqual(payload["mode"], "plan_only")
        self.assertEqual(payload["duration_seconds"], 720)
        self.assertEqual(payload["maximum_total_request_rate_hz"], 5.0)
        self.assertEqual(
            payload["raw_output"],
            str(raw_output / "cluster-drive-shakedown-20260724-120000"),
        )


if __name__ == "__main__":
    unittest.main()
