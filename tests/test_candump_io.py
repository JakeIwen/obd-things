"""Compatibility coverage for saved candump syntax and zstd lifecycle profiles."""

import dataclasses
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from lib.candump_io import (
    CandumpFormat, CandumpSyntaxError, identifier_bits, parse_candump_line, zstd_stream,
)
from tools import alfaobd_singleton_join as join
from tools import can_capture_summary as summary
from tools import can_timeseries_correlate as timeseries


JOIN_FORMAT = CandumpFormat(id_width=(3, 8), long_form=False,
                            timestamp_syntax="float", leading_whitespace=False)


def normalized(value):
    if dataclasses.is_dataclass(value):
        value = dataclasses.asdict(value)
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if isinstance(value, (list, tuple)):
        return [normalized(item) for item in value]
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items()}
    return value


class CandumpParserTests(unittest.TestCase):
    def test_frozen_original_parser_corpus(self):
        # Expectations captured from unmodified 390404e, not recomputed by the
        # implementation under test. Includes the inventory conflicts, all its
        # harness variants, existing test literals, and Unicode edge cases.
        corpus = Path(__file__).with_name("fixtures") / "candump_profiles.json"
        for row in json.loads(corpus.read_text(encoding="utf-8")):
            for name, parse, binary, format in (
                ("join", join.parse_candump_frame, True, JOIN_FORMAT),
                ("timeseries", timeseries.parse_candump_frame, True, CandumpFormat()),
                ("summary", summary.parse_frame, False, CandumpFormat()),
            ):
                with self.subTest(label=row["label"], line=row["line"], parser=name):
                    line = row["line"].encode("utf-8") if binary else row["line"]
                    try:
                        actual = {"result": normalized(parse(line))}
                    except Exception as exc:
                        actual = {"error": type(exc).__name__, "message": str(exc)}
                    self.assertEqual(actual, row[name])
                    # Every accepted existing-tool test input must independently
                    # traverse the shared syntax profile, even before adoption.
                    result = row[name].get("result")
                    if result is not None:
                        fields = parse_candump_line(line, format=format)
                        payload = result[2] if name == "join" else result["payload"]
                        self.assertEqual(normalized(fields.payload), payload)
                        interface = result[3] if name == "join" else result.get("channel", result.get("interface"))
                        self.assertEqual(fields.interface.decode("ascii") if binary else fields.interface, interface)

    def test_long_and_compact_preserve_input_type(self):
        for line in (" (1.0) can0 123 [2] AA BB\n", "(1.0) can0 123#AABB\r\n"):
            text = parse_candump_line(line)
            binary = parse_candump_line(line.encode("ascii"))
            self.assertEqual(text.payload, b"\xaa\xbb")
            self.assertEqual(binary.payload, text.payload)
            self.assertEqual(text.timestamp, "1.0")
            self.assertEqual(binary.timestamp, b"1.0")

    def test_syntax_errors(self):
        for line in ("", "  \n", "can0 123#AA", "(1.0) can0 123#ABC",
                     "(1.0) can0 123#R8", "(1.0) can0 123##1", "(1.0) can0 123#AA junk"):
            with self.assertRaisesRegex(CandumpSyntaxError, "malformed nonempty candump line"):
                parse_candump_line(line)
        with self.assertRaisesRegex(CandumpSyntaxError, "candump line DLC does not match its payload"):
            parse_candump_line("(1.0) can0 123 [2] AA")

    def test_join_lexical_dimensions(self):
        fields = parse_candump_line(b"(1e9) can0 123#AA", format=JOIN_FORMAT)
        self.assertEqual(fields.timestamp, b"1e9")
        for line in (b" (1.0) can0 123#AA", b"(1.0) can0 12#AA", b"(1.0) can0 123 [1] AA"):
            with self.assertRaises(CandumpSyntaxError):
                parse_candump_line(line, format=JOIN_FORMAT)

    def test_optional_timestamp_and_trailing_text_are_explicit(self):
        format = CandumpFormat(timestamp_required=False, trailing_text="prefix")
        fields = parse_candump_line("can0 123#AABB trailing", format=format)
        self.assertIsNone(fields.timestamp)
        self.assertEqual(fields.payload, b"\xaa\xbb")
        self.assertEqual(parse_candump_line("(1) can0 123#AA", format=format).timestamp, "1")

    def test_unicode_is_not_silently_normalized(self):
        self.assertEqual(parse_candump_line("(١.5) can0 123#AA").timestamp, "١.5")
        with self.assertRaises(CandumpSyntaxError):
            parse_candump_line("(١.5) can0 123#AA".encode("utf-8"))
        with self.assertRaisesRegex(ValueError, "non-hexadecimal number found in fromhex"):
            parse_candump_line("(1.0) can0 123 [2] AA BB")

    def test_payload_limit_and_timestamp_policy_belong_to_consumer(self):
        self.assertEqual(len(parse_candump_line("(-1.0000001) can0 123#" + "00" * 9).payload), 9)
        self.assertEqual(parse_candump_line(b"(nan) can0 123#", format=JOIN_FORMAT).payload, b"")

    def test_eff_labeling_never_masks_flags(self):
        cases = [("7", 11, None), ("123", 11, 11), ("FFF", 29, None),
                 ("0123", 29, None), ("00000123", 29, 29),
                 ("1FFFFFFF", 29, 29), ("20000080", None, None), ("123456789", None, None)]
        for text, loose, strict in cases:
            self.assertEqual(identifier_bits(text, int(text, 16)), loose)
            self.assertEqual(identifier_bits(text, int(text, 16), strict_width=True), strict)

    def test_existing_strict_error_precedence(self):
        # Size -> interface -> identifier -> timestamp; no "cleaner" reordering.
        for line, expected in (
            (b"(-1) can1 FFF#" + b"00" * 9, "CAN FD payloads"),
            (b"(-1) can1 FFF#AA", "interface must match"),
            (b"(-1) can0 FFF#AA", "exactly three SFF"),
            (b"(-1) can0 123#AA", "non-finite or negative"),
        ):
            with self.assertRaisesRegex(timeseries.CorrelateError, expected):
                timeseries.parse_candump_frame(line, expected_channel="can0")


class ZstdStreamTests(unittest.TestCase):
    path = Path("capture.zst")

    def process(self, *, text=False, code=0, stderr=""):
        process = mock.Mock()
        process.stdout = io.StringIO("line\n") if text else io.BytesIO(b"line\n")
        process.stderr = io.StringIO(stderr) if text else None
        process.wait.return_value = code
        process.poll.return_value = None
        return process

    def test_binary_argv_and_normal_completion(self):
        process = self.process()
        popen = mock.Mock(return_value=process)
        with zstd_stream(self.path, error_type=RuntimeError, popen=popen,
                         executable="/usr/bin/zstd", stdin=subprocess.DEVNULL) as child:
            self.assertIs(child, process)
            self.assertEqual(list(child.stdout), [b"line\n"])
        popen.assert_called_once_with(["/usr/bin/zstd", "-dc", "--", str(self.path)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        process.wait.assert_called_once_with(timeout=10)
        process.terminate.assert_not_called()
        self.assertTrue(process.stdout.closed)

    def test_text_argv_and_normal_completion(self):
        process = self.process(text=True)
        popen = mock.Mock(return_value=process)
        with zstd_stream(self.path, error_type=RuntimeError, popen=popen, text=True) as child:
            self.assertEqual(list(child.stdout), ["line\n"])
        popen.assert_called_once_with(["zstd", "-dc", "--", str(self.path)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace")
        process.wait.assert_called_once_with()
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_start_error_class_message_and_cause(self):
        error = OSError("missing")
        with self.assertRaisesRegex(RuntimeError, "cannot start zstd for capture.zst: missing") as raised:
            with zstd_stream(self.path, error_type=RuntimeError, popen=mock.Mock(side_effect=error)):
                self.fail("unexpected yield")
        self.assertIs(raised.exception.__cause__, error)

    def test_binary_error_after_yield(self):
        process = self.process(code=1)
        with self.assertRaisesRegex(RuntimeError, "zstd failed while reading capture.zst with status 1"):
            with zstd_stream(self.path, error_type=RuntimeError, popen=mock.Mock(return_value=process)) as child:
                self.assertEqual(child.stdout.read(), b"line\n")

    def test_text_error_detail_and_fallback(self):
        for stderr, detail in (("corrupt frame\n", "corrupt frame"), ("", "exit status 7")):
            process = self.process(text=True, code=7, stderr=stderr)
            with self.assertRaisesRegex(OSError, "zstd decompression failed for capture.zst: " + detail):
                with zstd_stream(self.path, error_type=OSError, text=True,
                                 popen=mock.Mock(return_value=process)) as child:
                    self.assertEqual(child.stdout.read(), "line\n")
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)

    def test_binary_exception_terminates_and_reaps_without_status_error(self):
        process = self.process(code=9)
        with self.assertRaisesRegex(ValueError, "caller stopped"):
            with zstd_stream(self.path, error_type=RuntimeError, popen=mock.Mock(return_value=process)):
                raise ValueError("caller stopped")
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=10)

    def test_binary_generator_close_terminates_and_reaps(self):
        process = self.process(code=9)
        def lines():
            with zstd_stream(self.path, error_type=RuntimeError, popen=mock.Mock(return_value=process)) as child:
                yield from child.stdout
        iterator = lines()
        self.assertEqual(next(iterator), b"line\n")
        iterator.close()
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=10)

    def test_binary_timeout_kills_and_reports_minus_nine(self):
        process = self.process()
        process.wait.side_effect = [subprocess.TimeoutExpired("zstd", 10), 137]
        with self.assertRaisesRegex(RuntimeError, "status -9"):
            with zstd_stream(self.path, error_type=RuntimeError, popen=mock.Mock(return_value=process)):
                pass
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_args_list, [mock.call(timeout=10), mock.call()])

    def test_binary_exception_timeout_does_not_mask_caller(self):
        process = self.process()
        process.wait.side_effect = [subprocess.TimeoutExpired("zstd", 10), 137]
        with self.assertRaisesRegex(ValueError, "caller stopped"):
            with zstd_stream(self.path, error_type=RuntimeError, popen=mock.Mock(return_value=process)):
                raise ValueError("caller stopped")
        process.kill.assert_called_once_with()

    def test_text_exception_only_closes_pipes(self):
        process = self.process(text=True, code=9)
        with self.assertRaisesRegex(ValueError, "caller stopped"):
            with zstd_stream(self.path, error_type=RuntimeError, text=True,
                             popen=mock.Mock(return_value=process)):
                raise ValueError("caller stopped")
        process.wait.assert_not_called()
        process.kill.assert_not_called()
        process.terminate.assert_not_called()
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_missing_binary_pipe_kills_unconditionally(self):
        process = self.process()
        process.stdout = None
        process.poll.return_value = 0
        with self.assertRaisesRegex(RuntimeError, "zstd stdout pipe was not created"):
            with zstd_stream(self.path, error_type=RuntimeError, popen=mock.Mock(return_value=process)):
                self.fail("unexpected yield")
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with()
        process.poll.assert_not_called()

    def test_missing_text_pipe_only_kills_running_child(self):
        for status in (None, 0):
            process = self.process(text=True)
            process.stderr = None
            process.poll.return_value = status
            with self.assertRaisesRegex(RuntimeError, "zstd did not provide stdout/stderr pipes"):
                with zstd_stream(self.path, error_type=RuntimeError, text=True,
                                 popen=mock.Mock(return_value=process)):
                    self.fail("unexpected yield")
            self.assertEqual(process.kill.call_count, 1 if status is None else 0)
            process.wait.assert_called_once_with()
