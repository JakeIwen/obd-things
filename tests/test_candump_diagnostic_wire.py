import json
from unittest import mock
from pathlib import Path
import tempfile
import unittest

from lib.modules import MODULES
from tools import candump_diagnostic_wire as wire
from tools import can_timeseries_correlate as correlate


REPO = Path(__file__).resolve().parents[1]


class DiagnosticWireTests(unittest.TestCase):
    def test_extracts_only_paired_physical_reads_with_global_raw_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "capture.candump"
            capture.write_text(
                "\n".join(
                    [
                        "(1.000000) can0 18DA10F1#0322022A00000000",
                        "(1.010000) can0 123#0102",
                        "(1.020000) can0 18DAF110#0462022A34000000",
                        "(1.030000) can0 18DA10F1#0322011D00000000",
                        "(1.040000) can0 18DAF110#0462011D80000000",
                        "(1.050000) can0 18DAF110#0462069E33000000",
                    ]
                )
                + "\n",
                encoding="ascii",
            )
            output = root / "pcm_wire.jsonl"
            with mock.patch.object(wire, "TMP_ROOT", root):
                summary = wire.extract(
                    module=MODULES["pcm"],
                    captures=[capture],
                    output=output,
                )

            rows = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(summary["exchange_count"], 2)
            self.assertEqual(
                [(row["classification"], row["did"]) for row in rows],
                [
                    ("exact_request", "022A"),
                    ("exact_positive_response", "022A"),
                    ("exact_request", "011D"),
                    ("exact_positive_response", "011D"),
                ],
            )
            self.assertEqual(
                [row["raw_line_sequence"] for row in rows],
                [0, 2, 3, 4],
            )
            self.assertEqual(rows[1]["direction"], "ecu_to_tester")
            self.assertEqual(rows[1]["module_key"], "pcm")
            self.assertEqual(rows[1]["can_data_hex"], "04 62 02 2A 34 00 00 00")

            summary_path = root / "pcm_wire.summary.json"
            on_disk = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["wire_sha256"], summary["wire_sha256"])
            self.assertEqual(
                on_disk["ignored_unpaired_positive_responses"], 1
            )

    def test_generic_reference_reader_pins_pcm_endpoint_and_direction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "capture.candump"
            capture.write_text(
                "(1.000000) can0 18DA10F1#0322022A00000000\n"
                "(1.010000) can0 18DAF110#0462022A34000000\n",
                encoding="ascii",
            )
            output = root / "pcm_wire.jsonl"
            with mock.patch.object(wire, "TMP_ROOT", root):
                wire.extract(
                    module=MODULES["pcm"],
                    captures=[capture],
                    output=output,
                )
            stats = correlate.StreamStats(str(output), "none")
            samples = list(
                correlate.iter_reference_samples(
                    output,
                    did=0x022A,
                    decoder=correlate.ReferenceDecoder("byte:0"),
                    stats=stats,
                    module=MODULES["pcm"],
                )
            )
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0].value, 0x34)
            self.assertEqual(samples[0].expected_can_id, MODULES["pcm"].rxid)

    def test_rejects_partial_input_and_noncanonical_output_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            partial = root / "capture.zst.partial"
            partial.write_bytes(b"")
            with mock.patch.object(wire, "TMP_ROOT", root):
                with self.assertRaisesRegex(
                    correlate.CorrelateError, "partial evidence"
                ):
                    wire.extract(
                        module=MODULES["pcm"],
                        captures=[partial],
                        output=root / "pcm_wire.jsonl",
                    )

                capture = root / "capture.candump"
                capture.write_text("", encoding="ascii")
                with self.assertRaisesRegex(
                    correlate.CorrelateError, "exactly pcm_wire.jsonl"
                ):
                    wire.extract(
                        module=MODULES["pcm"],
                        captures=[capture],
                        output=root / "wrong.jsonl",
                    )

    def test_van_compute_result_root_requires_exact_sandbox_and_job_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "job" / "source"
            result_root = root / "job" / "result"
            wrong_result_root = root / "job" / "artifacts"
            other_result_root = root / "other-job" / "result"
            tmp_root = source_root / "tmp"
            source_root.mkdir(parents=True)
            result_root.mkdir(parents=True)
            wrong_result_root.mkdir()
            other_result_root.mkdir(parents=True)
            output = result_root / "pcm_wire.jsonl"
            with (
                mock.patch.object(wire, "REPO", source_root),
                mock.patch.object(wire, "TMP_ROOT", tmp_root),
            ):
                with mock.patch.dict(
                    wire.os.environ,
                    {"VAN_COMPUTE_JOB_ID": "20260727T120000Z-abcdef12"},
                    clear=False,
                ):
                    self.assertEqual(
                        wire._checked_output(
                            output,
                            module=MODULES["pcm"],
                            allow_van_compute_result=True,
                        ),
                        output.resolve(),
                    )
                    with self.assertRaisesRegex(
                        correlate.CorrelateError, "below"
                    ):
                        wire._checked_output(
                            output,
                            module=MODULES["pcm"],
                        )
                    with self.assertRaisesRegex(
                        correlate.CorrelateError,
                        "staged sibling result",
                    ):
                        wire._checked_output(
                            wrong_result_root / "pcm_wire.jsonl",
                            module=MODULES["pcm"],
                            allow_van_compute_result=True,
                        )
                    with self.assertRaisesRegex(
                        correlate.CorrelateError,
                        "staged sibling result",
                    ):
                        wire._checked_output(
                            other_result_root / "pcm_wire.jsonl",
                            module=MODULES["pcm"],
                            allow_van_compute_result=True,
                        )

                with mock.patch.dict(
                    wire.os.environ,
                    {"VAN_COMPUTE_JOB_ID": "not-a-job"},
                    clear=False,
                ):
                    with self.assertRaisesRegex(
                        correlate.CorrelateError,
                        "valid VAN_COMPUTE_JOB_ID",
                    ):
                        wire._checked_output(
                            output,
                            module=MODULES["pcm"],
                            allow_van_compute_result=True,
                        )


if __name__ == "__main__":
    unittest.main()
