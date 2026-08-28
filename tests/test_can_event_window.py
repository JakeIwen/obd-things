import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from tools import can_event_window


class SelectorTests(unittest.TestCase):
    def test_requires_explicit_namespace(self):
        self.assertEqual(can_event_window.parse_id_selector("11:46C"), (11, 0x46C))
        self.assertEqual(
            can_event_window.parse_id_selector("29:1E340041"),
            (29, 0x1E340041),
        )
        with self.assertRaisesRegex(Exception, "explicit"):
            can_event_window.parse_id_selector("46C")
        with self.assertRaisesRegex(Exception, "explicit"):
            can_event_window.parse_id_selector("11:800")

    def test_crc8_sae_j1850_known_frames(self):
        self.assertEqual(
            can_event_window.crc8_sae_j1850(bytes.fromhex("42 00 00 00 00 00 00")),
            0x6E,
        )
        self.assertEqual(
            can_event_window.crc8_sae_j1850(bytes.fromhex("42 04 00 00 11 E0 0E")),
            0x0C,
        )


class ExtractTests(unittest.TestCase):
    def test_filters_time_and_ids_then_sorts_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.candump"
            second = root / "second.candump"
            first.write_text(
                "(10.0) can0 123#AA\n"
                "(12.0) can0 00000123#BB\n"
                "(14.0) can0 123#CC\n",
                encoding="utf-8",
            )
            second.write_text(
                "(11.0) can1 123#DD\n"
                "(13.0) can1 124#EE\n",
                encoding="utf-8",
            )

            payload = can_event_window.extract_window(
                [first, second],
                start=10.5,
                end=13.5,
                selectors={(11, 0x123), (29, 0x123)},
                maximum_frames=10,
            )

        self.assertEqual(payload["selected_frames"], 2)
        self.assertEqual(
            [row["timestamp"] for row in payload["frames"]], [11.0, 12.0]
        )
        self.assertEqual(payload["frames"][0]["data_hex"], "DD")
        self.assertEqual(payload["frames"][1]["id_bits"], 29)
        self.assertEqual(payload["sources"][0]["selected_frames"], 1)
        self.assertEqual(payload["sources"][1]["selected_frames"], 1)

    def test_unfiltered_selection_requires_bounded_short_window(self):
        capture = Path("/does/not/matter")
        with self.assertRaisesRegex(
            can_event_window.EventWindowError, "requires both"
        ):
            can_event_window.extract_window(
                [capture],
                start=None,
                end=None,
                selectors=set(),
                maximum_frames=10,
            )
        with self.assertRaisesRegex(
            can_event_window.EventWindowError, "limited to 30 seconds"
        ):
            can_event_window.extract_window(
                [capture],
                start=0.0,
                end=31.0,
                selectors=set(),
                maximum_frames=10,
            )

    def test_frame_cap_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "capture.candump"
            capture.write_text(
                "(1.0) can0 123#AA\n(2.0) can0 123#BB\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                can_event_window.EventWindowError, "exceeded"
            ):
                can_event_window.extract_window(
                    [capture],
                    start=None,
                    end=None,
                    selectors={(11, 0x123)},
                    maximum_frames=1,
                )

    def test_optional_crc_audit_counts_match_and_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "capture.candump"
            capture.write_text(
                "(1.0) can0 1EF#420000000000006E\n"
                "(2.0) can0 1EF#4204000011E00E00\n",
                encoding="utf-8",
            )
            payload = can_event_window.extract_window(
                [capture],
                start=None,
                end=None,
                selectors={(11, 0x1EF)},
                maximum_frames=10,
                audit_crc8_j1850=True,
            )

        audit = payload["crc8_sae_j1850_audit"]
        self.assertEqual(audit["checked_frames"], 2)
        self.assertEqual(audit["mismatch_count"], 1)
        self.assertEqual(audit["mismatch_samples"][0]["computed_crc_hex"], "0C")
        self.assertEqual(audit["mismatch_samples"][0]["observed_crc_hex"], "00")


class CliTests(unittest.TestCase):
    def test_writes_explicit_json_without_touching_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "capture.candump"
            output = root / "result" / "window.json"
            original = "(10.0) can0 46C#0011223344556677\n"
            capture.write_text(original, encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = can_event_window.main(
                    [
                        str(capture),
                        "--id",
                        "11:46C",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(capture.read_text(encoding="utf-8"), original)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["selected_frames"], 1)
            self.assertEqual(payload["frames"][0]["can_id_hex"], "46C")
            self.assertIn("selected 1 exact frame", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
