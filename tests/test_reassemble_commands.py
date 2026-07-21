from pathlib import Path
import tempfile
import unittest

from projects.ecu_mapping import reassemble_commands


def decoded_line(timestamp, direction, text):
    return f"{timestamp} {direction}: {text.encode('ascii').hex().upper()}\n"


class PlainResponseTests(unittest.TestCase):
    def test_selects_final_response_after_response_pending(self):
        self.assertEqual(
            reassemble_commands.normalize_plain_response("037F2E78036E2023"),
            "6E2023",
        )

    def test_preserves_unframed_response(self):
        self.assertEqual(reassemble_commands.normalize_plain_response("5003"), "5003")

    def test_bare_prompt_finishes_multiframe_write_exchange(self):
        lines = [
            decoded_line("16:19:24.000", "S", "ATSHDA40F1\r"),
            decoded_line("16:19:24.100", "S", "100A2E20234142431\r"),
            decoded_line("16:19:24.200", "R", "300000\r>"),
            # AlfaOBD omits its trailing response-hint digit on the final CF.
            decoded_line("16:19:24.300", "S", "2144454647000000\r"),
            decoded_line("16:19:24.400", "R", "\r>"),
            decoded_line("16:19:24.500", "S", "STPTO3000\r"),
            decoded_line("16:19:24.600", "R", "OK\r\r>"),
            decoded_line("16:19:25.800", "R", "037F2E78\r"),
            decoded_line("16:19:27.200", "R", "036E2023\r"),
            decoded_line("16:19:27.300", "R", "\r>"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "decoded.txt"
            path.write_text("".join(lines))
            exchanges = list(reassemble_commands.iter_commands(path))

        self.assertEqual(len(exchanges), 1)
        self.assertEqual(exchanges[0]["addr"], "DA40F1")
        self.assertEqual(exchanges[0]["req"], "2E202341424344454647")
        self.assertEqual(exchanges[0]["resp"], "6E2023")
        self.assertEqual(
            reassemble_commands.interpret(exchanges[0]["req"], exchanges[0]["resp"])[1],
            "POS 6E2023",
        )

    def test_recording_uses_date_from_later_close_marker(self):
        lines = [
            "Recording closed 2026/06/12 23:59:00.000\n",
            "Recording data for Body computer Marelli\n",
            decoded_line("00:21:14.284", "S", "ATSHDA40F1\r"),
            decoded_line("00:21:14.300", "R", "OK\r>"),
            decoded_line("00:21:14.400", "S", "1003\r"),
            decoded_line("00:21:14.500", "R", "065003003201F4\r>"),
            "Recording closed 2026/06/22 00:22:34.108\n",
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "decoded.txt"
            path.write_text("".join(lines))
            exchanges = list(reassemble_commands.iter_commands(path))

        self.assertEqual(len(exchanges), 1)
        self.assertEqual(exchanges[0]["date"], "2026/06/22")
        self.assertEqual(exchanges[0]["req"], "1003")

    def test_long_open_recording_does_not_backdate_from_earlier_close_clock(self):
        lines = [
            "Recording closed 2026/06/25 16:24:02.968\n",
            "Recording data for ZF 948TE\n",
            decoded_line("16:24:40.771", "S", "ATSHDA18F1\r"),
            decoded_line("16:24:40.800", "R", "OK\r>"),
            decoded_line("16:24:40.900", "S", "14FFFFFF\r"),
            decoded_line("16:24:41.000", "R", "037F1422\r>"),
            "Recording closed 2026/07/07 15:06:58.779\n",
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "decoded.txt"
            path.write_text("".join(lines))
            exchanges = list(reassemble_commands.iter_commands(path))

        self.assertEqual(len(exchanges), 1)
        self.assertEqual(exchanges[0]["date"], "2026/06/25")


if __name__ == "__main__":
    unittest.main()
