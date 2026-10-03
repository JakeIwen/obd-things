import unittest
from types import SimpleNamespace

from lib.capture_pipeline import candump_command
from tools import three_bus_capture


class CaptureCommandTests(unittest.TestCase):
    def test_three_bus_wrapper_preserves_optional_buffer_argv(self):
        lease = SimpleNamespace(channel="can7")
        for size in (None, 0, 1, 16777216):
            with self.subTest(size=size):
                expected = ["/oracle/candump", "-L", "-d"]
                if size is not None:
                    expected.extend(("-r", str(size)))
                expected.append("can7")
                self.assertEqual(
                    three_bus_capture.candump_command(
                        "/oracle/candump", lease, receive_buffer_bytes=size
                    ),
                    expected,
                )

    def test_extra_options_keep_the_compressed_recorders_order(self):
        self.assertEqual(
            candump_command(
                "candump", "can8", receive_buffer_bytes=16777216,
                extra_args=("-D",),
            ),
            ["candump", "-L", "-D", "-d", "-r", "16777216", "can8"],
        )
