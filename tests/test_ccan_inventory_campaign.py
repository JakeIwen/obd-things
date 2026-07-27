"""Inert CLI checks for the parked C-CAN campaign wrapper."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "tools" / "ccan_inventory_campaign.sh"


class CcanInventoryCampaignTests(unittest.TestCase):
    def run_campaign(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), *arguments],
            cwd=REPO,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )

    def test_priority_telemetry_dry_run_is_exact_and_inert(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory)
            python = fake_bin / "python3"
            python.write_text(
                "#!/bin/sh\n"
                "printf 'PYTHON3'\n"
                "for argument in \"$@\"; do printf ' <%s>' \"$argument\"; done\n"
                "printf '\\n'\n"
            )
            python.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}:/usr/bin:/bin"
            result = self.run_campaign(
                "--priority-telemetry",
                environment=environment,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "PYTHON3 <tools/did_sweep.py> <pcm> <--did> <3159> <--did> <315A> "
            "<--session> <92> <--confirm-session-change>",
            result.stdout,
        )
        self.assertIn(
            "PYTHON3 <tools/did_sweep.py> <tcm> <--did> <F40C> <--did> <0500> "
            "<--did> <2102> <--did> <2103> <--did> <F405> <--did> <0301> "
            "<--did> <04FE> <--did> <1018> <--did> <101A> <--did> <101B> "
            "<--did> <101D> <--did> <101F> <--did> <1020>",
            result.stdout,
        )
        self.assertEqual(result.stdout.count("PYTHON3"), 2)
        self.assertNotIn("Checking noninteractive passwordless sudo", result.stdout)

    def test_priority_telemetry_live_requires_separate_session_confirmation(self):
        result = self.run_campaign(
            "--priority-telemetry",
            "--execute",
            "--confirm-parked",
            "--conditions",
            "parked; ignition ON; engine OFF",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "priority-telemetry live use requires --confirm-session-change",
            result.stderr,
        )
        self.assertNotIn("Checking noninteractive passwordless sudo", result.stdout)


if __name__ == "__main__":
    unittest.main()
