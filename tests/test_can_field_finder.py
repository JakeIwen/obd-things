from __future__ import annotations

import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from tools import can_field_finder as finder


class CanFieldFinderStreamIdentityTests(unittest.TestCase):
    def test_parse_preserves_channel_namespace_numeric_id_and_dlc(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mixed.candump"
            path.write_text(
                "(1.000000) can0 123#7800\n"
                "(1.100000) can1 123  [3]  79 00 01\n"
                "can0 00000123#7A00\n"
                "can0 123  [3]  7B 00\n",  # declared DLC mismatch: rejected
                encoding="ascii",
            )

            parsed = finder.parse(path)

        self.assertEqual(
            set(parsed),
            {
                finder.StreamKey("can0", 11, 0x123, 2),
                finder.StreamKey("can1", 11, 0x123, 3),
                finder.StreamKey("can0", 29, 0x123, 2),
            },
        )
        self.assertEqual(
            parsed[finder.StreamKey("can0", 29, 0x123, 2)],
            [bytes.fromhex("7A00")],
        )

    def test_same_numeric_id_on_different_interfaces_never_becomes_common(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.log"
            second = root / "second.log"
            first.write_text("(1.0) can0 123#7800\n", encoding="ascii")
            second.write_text("(2.0) can1 123#8C00\n", encoding="ascii")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                finder.find([first, second], [12.0, 14.0], 20)

        self.assertIn("0 exact CAN streams common", output.getvalue())
        self.assertIn("(no plausible voltage field", output.getvalue())

    def test_report_keeps_each_complete_stream_and_field_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.log"
            second = root / "second.log"
            first.write_text(
                "(1.0) can0 123#7800\n"
                "(1.1) can1 123  [3]  7D 00 00\n",
                encoding="ascii",
            )
            second.write_text(
                "(2.0) can0 123#8C00\n"
                "(2.1) can1 123  [3]  91 00 00\n",
                encoding="ascii",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                finder.find([first, second], [12.0, 14.0], 20)

        report = output.getvalue()
        self.assertIn("interface", report)
        self.assertIn("ns", report)
        self.assertIn("dlc", report)
        self.assertRegex(report, r"can0\s+SFF\s+123\s+2\s+0\s+1")
        self.assertRegex(report, r"can1\s+SFF\s+123\s+3\s+0\s+1")


if __name__ == "__main__":
    unittest.main()
