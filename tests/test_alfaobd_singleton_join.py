from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import alfaobd_singleton_join as join


TXID = 0x18DA60F1
RXID = 0x18DAF160
BASE_TIME = 1_800_000_000.0


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def alfa_bin(decoded: str) -> bytes:
    return "".join(f"{byte ^ 0xFF:02X}" for byte in decoded.encode("latin-1")).encode(
        "ascii"
    )


def debug_send(clock: str, request: bytes) -> str:
    sent = (request.hex().upper() + "\r").encode("latin-1").hex().upper()
    return f"{clock}.000 S: {sent}\r\n"


def debug_receive(clock: str, response: bytes) -> str:
    received = (response.hex().upper() + "\r\r>").encode("latin-1").hex().upper()
    return f"{clock}.050 R: {received}\r\n"


def debug_response_text(clock: str, text: str) -> str:
    encoded = text.encode("latin-1").hex().upper()
    return f"{clock}.050 R: {encoded}\r\n"


def debug_exchange(clock: str, request: bytes, response: bytes) -> str:
    return debug_send(clock, request) + debug_receive(clock, response)


def sf_line(timestamp: float, can_id: int, payload: bytes) -> bytes:
    assert 0 < len(payload) <= 7
    data = bytes((len(payload),)) + payload
    data = data.ljust(8, b"\x00")
    return (
        f"({timestamp:.6f}) can0 {can_id:X}#{data.hex().upper()}\n".encode(
            "ascii"
        )
    )


class EvidenceFixture:
    def __init__(
        self,
        root: Path,
        *,
        repeated_did: int = 0x1000,
        info_schedule: list[str] | None = None,
        info_sample_counts: list[int] | None = None,
        bad_wire_response: bool = False,
        bad_debug_response: bool = False,
        extra_wire_request: bool = False,
        buffered_offsets: bool = True,
        wire_sample_count: int = 2,
        debug_sample_counts: list[int] | None = None,
        debug_response_counts: list[int] | None = None,
    ) -> None:
        self.root = root
        self.campaign = root / "singleton"
        self.capture = root / "capture"
        self.campaign.mkdir()
        self.capture.mkdir()
        final = self.campaign / "android_logs" / "final"
        final.mkdir(parents=True)

        self.schedule = ["Engine speed", "Vehicle speed", "Engine speed"]
        dids = [0x1000, 0x1001, repeated_did]
        rendered_labels = info_schedule or self.schedule
        rendered_counts = info_sample_counts or [wire_sample_count] * 3
        debug_counts = debug_sample_counts or [wire_sample_count] * 3
        debug_rx_counts = debug_response_counts or debug_counts
        assert len(rendered_labels) == len(self.schedule)
        assert len(rendered_counts) == len(self.schedule)
        assert len(debug_counts) == len(self.schedule)
        assert len(debug_rx_counts) == len(self.schedule)
        assert all(
            response_count <= request_count
            for request_count, response_count in zip(
                debug_counts,
                debug_rx_counts,
            )
        )
        plan = {
            "schema_version": 1,
            "campaign_id": "cluster-join-test",
            "module_key": "cluster",
            "expected_runtime": "Instrument panel Continental",
            "expected_app_version": "2.4.4.0",
            "expected_screen": {"width": 800, "height": 1280, "rotation": 0},
            "dialog_labels": ["Engine speed", "Vehicle speed"],
            "gauges": ["Engine speed", "Vehicle speed"],
            "repeat_anchors": ["Engine speed"],
            "segment_seconds": 5,
            "settle_seconds": 0,
            "verify_seconds": 1,
            "min_free_bytes": 104857600,
            "min_tablet_free_bytes": 104857600,
            "artifacts": ["AlfaOBD_Debug.bin", "MARELLI_DASH_EP_Info.log"],
            "required_segment_growth": [
                "AlfaOBD_Debug.bin",
                "MARELLI_DASH_EP_Info.log",
            ],
            "required_stop_stability": ["MARELLI_DASH_EP_Info.log"],
            "screenshot_each_segment": False,
        }
        (self.campaign / "plan.json").write_text(
            json.dumps(plan, sort_keys=True), encoding="utf-8"
        )

        debug = bytearray(alfa_bin("prefix\r\n"))
        info = bytearray(b"prefix\r\n")
        boundaries: list[dict[str, tuple[int, int]]] = []
        wire = bytearray()
        wire.extend(sf_line(BASE_TIME - 0.5, TXID, b"\x3e\x00"))
        wire.extend(sf_line(BASE_TIME - 0.4, RXID, b"\x7e\x00"))
        for sequence, (label, did) in enumerate(zip(self.schedule, dids)):
            debug_start = len(debug)
            info_start = len(info)
            tester = debug_exchange(
                f"12:00:{sequence * 10:02d}", b"\x3e\x00", b"\x7e\x00"
            )
            decoded = tester
            rendered_values = []
            for sample in range(debug_counts[sequence]):
                value = 1000 + sequence * 10 + sample
                request = b"\x22" + did.to_bytes(2, "big")
                response = b"\x62" + did.to_bytes(2, "big") + value.to_bytes(2, "big")
                if bad_debug_response and sequence == 1 and sample == 1:
                    response = response[:-1] + bytes((response[-1] ^ 1,))
                clock = f"12:00:{sequence * 10 + sample + 1:02d}"
                decoded += debug_send(clock, request)
                if sample < debug_rx_counts[sequence]:
                    decoded += debug_receive(clock, response)
            rendered_label = rendered_labels[sequence]
            unit = "rpm" if rendered_label == "Engine speed" else "km/h"
            for sample in range(rendered_counts[sequence]):
                value = 1000 + sequence * 10 + sample
                rendered_values.append(f"{value} {unit}")
            debug.extend(alfa_bin(decoded))
            for rendered in rendered_values:
                info.extend(
                    f"{rendered_label}: {rendered}\r\n".encode("latin-1")
                )
            boundaries.append(
                {
                    "AlfaOBD_Debug.bin": (debug_start, len(debug)),
                    "MARELLI_DASH_EP_Info.log": (info_start, len(info)),
                }
            )

            start = BASE_TIME + sequence * 10
            wire.extend(sf_line(start + 0.5, TXID, b"\x3e\x00"))
            wire.extend(sf_line(start + 0.6, RXID, b"\x7e\x00"))
            for sample in range(wire_sample_count):
                value = 1000 + sequence * 10 + sample
                request = b"\x22" + did.to_bytes(2, "big")
                response = b"\x62" + did.to_bytes(2, "big") + value.to_bytes(2, "big")
                if bad_wire_response and sequence == 1 and sample == 1:
                    response = b"\x63" + response[1:]
                wire.extend(sf_line(start + 1.5 + sample, TXID, request))
                wire.extend(sf_line(start + 1.6 + sample, RXID, response))
            if extra_wire_request and sequence == 1:
                extra_did = 0x10FF
                wire.extend(
                    sf_line(
                        start + 3.5,
                        TXID,
                        b"\x22" + extra_did.to_bytes(2, "big"),
                    )
                )
                wire.extend(
                    sf_line(
                        start + 3.6,
                        RXID,
                        b"\x62" + extra_did.to_bytes(2, "big") + b"\x00",
                    )
                )

        if buffered_offsets:
            for name in ("AlfaOBD_Debug.bin", "MARELLI_DASH_EP_Info.log"):
                outer_start = boundaries[0][name][0]
                first_end = boundaries[0][name][1]
                second_end = boundaries[1][name][1]
                final_end = boundaries[2][name][1]
                first_cut = first_end + (second_end - first_end) // 2
                second_cut = second_end + (final_end - second_end) // 2
                if name == "AlfaOBD_Debug.bin":
                    first_cut -= first_cut % 2
                    second_cut -= second_cut % 2
                boundaries[0][name] = (outer_start, first_cut)
                boundaries[1][name] = (first_cut, second_cut)
                boundaries[2][name] = (second_cut, final_end)

        debug.extend(alfa_bin("suffix\r\n"))
        info.extend(b"suffix\r\n")
        wire.extend(sf_line(BASE_TIME + 25.5, TXID, b"\x3e\x00"))
        wire.extend(sf_line(BASE_TIME + 25.6, RXID, b"\x7e\x00"))
        debug_path = final / "AlfaOBD_Debug.bin"
        info_path = final / "MARELLI_DASH_EP_Info.log"
        debug_path.write_bytes(debug)
        info_path.write_bytes(info)

        events: list[dict] = [
            {
                "event": "campaign_started",
                "campaign_id": "cluster-join-test",
                "module_key": "cluster",
                "wall_time_utc": datetime_iso(BASE_TIME - 1),
            }
        ]
        for sequence, label in enumerate(self.schedule):
            before_time = BASE_TIME + sequence * 10
            after_time = before_time + 5

            def artifacts(which: int) -> dict:
                return {
                    name: {
                        "path": (
                            "/sdcard/Android/data/com.android.AlfaOBD/files/logs/"
                            + name
                        ),
                        "size": pair[which],
                    }
                    for name, pair in boundaries[sequence].items()
                }

            events.extend(
                [
                    {
                        "event": "singleton_selected",
                        "sequence": sequence,
                        "gauge": label,
                        "wall_time_utc": datetime_iso(before_time - 0.1),
                    },
                    {
                        "event": "segment_offsets_before",
                        "sequence": sequence,
                        "gauge": label,
                        "wall_time_utc": datetime_iso(before_time),
                        "artifacts": artifacts(0),
                    },
                    {
                        "event": "segment_started",
                        "sequence": sequence,
                        "gauge": label,
                        "wall_time_utc": datetime_iso(before_time + 1),
                    },
                    {
                        "event": "segment_stopped_verified",
                        "sequence": sequence,
                        "gauge": label,
                        "wall_time_utc": datetime_iso(before_time + 4),
                    },
                    {
                        "event": "segment_offsets_after",
                        "sequence": sequence,
                        "gauge": label,
                        "wall_time_utc": datetime_iso(after_time),
                        "artifacts": artifacts(1),
                    },
                    {
                        "event": "segment_complete",
                        "sequence": sequence,
                        "gauge": label,
                        "wall_time_utc": datetime_iso(after_time + 0.1),
                    },
                ]
            )
        for path in (debug_path, info_path):
            events.append(
                {
                    "event": "artifact_pull",
                    "filename": path.name,
                    "source_present": True,
                    "pulled": True,
                    "size": path.stat().st_size,
                    "sha256": sha256(path),
                    "wall_time_utc": datetime_iso(BASE_TIME + 40),
                }
            )
        events.append(
            {
                "event": "campaign_complete",
                "segments": len(self.schedule),
                "wall_time_utc": datetime_iso(BASE_TIME + 41),
            }
        )
        (self.campaign / "events.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in events),
            encoding="utf-8",
        )
        (self.campaign / "state.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "campaign_id": "cluster-join-test",
                    "phase": "complete",
                    "next_sequence": len(self.schedule),
                    "manual_reconcile": False,
                }
            ),
            encoding="utf-8",
        )

        stream = self.capture / "chunk_000000_priority.candump"
        stream.write_bytes(wire)
        (self.capture / "run.json").write_text(
            json.dumps(
                {
                    "type": "run_metadata",
                    "campaign": "cluster-join-test",
                    "interaction": "passive_receive_only",
                    "duration_seconds": 32,
                    "interface": {
                        "up": True,
                        "bitrate": 500000,
                        "listen_only": True,
                        "controller_state": "ERROR-ACTIVE",
                        "rx_dropped": 0,
                        "rx_missed": 0,
                    },
                    "priority_ids": [f"0x{TXID:X}", f"0x{RXID:X}"],
                }
            ),
            encoding="utf-8",
        )
        capture_end = {
            "type": "capture_end",
            "time_utc": datetime_iso(BASE_TIME + 31),
            "reason": "duration_complete",
            "success": True,
            "duration_complete": True,
            "signal_number": None,
            "full_stream_complete": True,
            "requested_duration_seconds": 32,
            "elapsed_seconds": 32.0,
            "error": None,
            "free_bytes": 100000000000,
            "detected_socket_drops": 0,
        }
        manifest = [
            {
                "type": "capture_start",
                "time_utc": datetime_iso(BASE_TIME - 1),
            },
            {
                "type": "chunk",
                "sequence": 0,
                "started_utc": datetime_iso(BASE_TIME - 1),
                "ended_utc": datetime_iso(BASE_TIME + 31),
                "elapsed_seconds": 32.0,
                "first_frame_timestamp": BASE_TIME - 0.5,
                "last_frame_timestamp": BASE_TIME + 25.6,
                "complete": True,
                "streams": {
                    "priority": {
                        "path": str(stream),
                        "compressed_bytes": stream.stat().st_size,
                        "sha256": sha256(stream),
                        "zstd_exit": 0,
                        "complete": True,
                    }
                },
            },
            capture_end,
        ]
        (self.capture / "manifest.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in manifest),
            encoding="utf-8",
        )
        (self.capture / "checkpoint.json").write_text(
            json.dumps({"status": "complete", **capture_end}, sort_keys=True),
            encoding="utf-8",
        )


def datetime_iso(timestamp: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def refresh_capture_stream_record(
    capture_dir: Path,
    *,
    stream_kind: str = "priority",
) -> None:
    manifest_path = capture_dir / "manifest.jsonl"
    rows = read_jsonl(manifest_path)
    chunk = next(row for row in rows if row.get("type") == "chunk")
    stream = chunk["streams"][stream_kind]
    path = Path(stream["path"])
    stream["compressed_bytes"] = path.stat().st_size
    stream["sha256"] = sha256(path)
    write_jsonl(manifest_path, rows)


def write_capture_run(
    directory: Path,
    *,
    run_id: str,
    lines: list[bytes],
    start_time: float,
    end_time: float,
) -> dict:
    directory.mkdir()
    duration = int(end_time - start_time)
    assert duration > 0
    stream = directory / "chunk_000000_priority.candump"
    stream.write_bytes(b"".join(lines))
    timestamps = [
        parsed[0]
        for line in lines
        if (parsed := join.parse_candump_frame(line)) is not None
    ]
    run = {
        "type": "run_metadata",
        "campaign": run_id,
        "interaction": "passive_receive_only",
        "duration_seconds": duration,
        "interface": {
            "up": True,
            "bitrate": 500000,
            "listen_only": True,
            "controller_state": "ERROR-ACTIVE",
            "rx_dropped": 0,
            "rx_missed": 0,
        },
        "priority_ids": [f"0x{TXID:X}", f"0x{RXID:X}"],
    }
    run_path = directory / "run.json"
    run_path.write_text(json.dumps(run, sort_keys=True), encoding="utf-8")
    capture_end = {
        "type": "capture_end",
        "time_utc": datetime_iso(end_time),
        "reason": "duration_complete",
        "success": True,
        "duration_complete": True,
        "signal_number": None,
        "full_stream_complete": True,
        "requested_duration_seconds": duration,
        "elapsed_seconds": float(duration),
        "error": None,
        "free_bytes": 100000000000,
        "detected_socket_drops": 0,
    }
    manifest = [
        {
            "type": "capture_start",
            "time_utc": datetime_iso(start_time),
        },
        {
            "type": "chunk",
            "sequence": 0,
            "started_utc": datetime_iso(start_time),
            "ended_utc": datetime_iso(end_time),
            "elapsed_seconds": float(duration),
            "first_frame_timestamp": min(timestamps),
            "last_frame_timestamp": max(timestamps),
            "complete": True,
            "streams": {
                "priority": {
                    "path": str(stream),
                    "compressed_bytes": stream.stat().st_size,
                    "sha256": sha256(stream),
                    "zstd_exit": 0,
                    "complete": True,
                }
            },
        },
        capture_end,
    ]
    manifest_path = directory / "manifest.jsonl"
    write_jsonl(manifest_path, manifest)
    checkpoint_path = directory / "checkpoint.json"
    checkpoint_path.write_text(
        json.dumps({"status": "complete", **capture_end}, sort_keys=True),
        encoding="utf-8",
    )
    return {
        "run_id": run_id,
        "path": str(directory),
        "run_sha256": sha256(run_path),
        "checkpoint_sha256": sha256(checkpoint_path),
        "manifest_sha256": sha256(manifest_path),
    }


class SingletonJoinTests(unittest.TestCase):
    def test_completed_campaign_joins_deterministically_and_discards_tester_present(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            first = join.build_report(fixture.campaign, fixture.capture)
            second = join.build_report(fixture.campaign, fixture.capture)

        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True),
        )
        self.assertEqual(first["classification"], "candidate_only")
        self.assertEqual(first["summary"]["candidate_segments"], 3)
        self.assertEqual(first["segments"][0]["candidate"]["did"], "0x1000")
        self.assertEqual(first["segments"][1]["candidate"]["did"], "0x1001")
        self.assertTrue(first["buffered_artifact_mode"]["enabled"])
        self.assertEqual(
            [
                segment["wire"]["tester_present_messages_discarded"]
                for segment in first["segments"]
            ],
            [2, 2, 2],
        )
        self.assertEqual(
            first["debug_artifact"]["tester_present_requests_discarded"],
            3,
        )
        self.assertEqual(
            first["debug_artifact"]["tester_present_responses_discarded"],
            3,
        )
        self.assertTrue(
            first["debug_artifact"]["corroboration"]["planned_did_run_order_exact"]
        )
        self.assertGreater(
            first["segments"][0]["artifact_offset_witnesses"][
                "MARELLI_DASH_EP_Info.log"
            ]["bytes"],
            0,
        )
        self.assertTrue(first["segments"][0]["info_wire_count_match"])
        self.assertEqual(first["anchor_checks"][0]["request"], "221000")
        self.assertEqual(first["passive_capture"]["stream_kind"], "priority")

    def test_incomplete_campaign_and_artifact_hash_mismatch_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            state_path = fixture.campaign / "state.json"
            state = json.loads(state_path.read_text())
            state["phase"] = "failed"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaisesRegex(join.JoinError, "not complete"):
                join.build_report(fixture.campaign, fixture.capture)

        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            debug = (
                fixture.campaign
                / "android_logs"
                / "final"
                / "AlfaOBD_Debug.bin"
            )
            payload = bytearray(debug.read_bytes())
            payload[-1] ^= 1
            debug.write_bytes(payload)
            with self.assertRaisesRegex(join.JoinError, "hash mismatch"):
                join.build_report(fixture.campaign, fixture.capture)

    def test_shortened_capture_and_checkpoint_disagreement_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            manifest_path = fixture.capture / "manifest.jsonl"
            rows = read_jsonl(manifest_path)
            end = rows[-1]
            end["reason"] = "signal"
            end["duration_complete"] = False
            end["signal_number"] = 15
            write_jsonl(manifest_path, rows)
            (fixture.capture / "checkpoint.json").write_text(
                json.dumps({"status": "complete", **end}, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(join.JoinError, "duration-complete"):
                join.build_report(fixture.campaign, fixture.capture)

        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            checkpoint_path = fixture.capture / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["free_bytes"] += 1
            checkpoint_path.write_text(
                json.dumps(checkpoint, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(join.JoinError, "does not agree"):
                join.build_report(fixture.campaign, fixture.capture)

        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            manifest_path = fixture.capture / "manifest.jsonl"
            rows = read_jsonl(manifest_path)
            end = rows[-1]
            end["elapsed_seconds"] = 31.999
            write_jsonl(manifest_path, rows)
            (fixture.capture / "checkpoint.json").write_text(
                json.dumps({"status": "complete", **end}, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(join.JoinError, "shorter than"):
                join.build_report(fixture.campaign, fixture.capture)

        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            manifest_path = fixture.capture / "manifest.jsonl"
            rows = read_jsonl(manifest_path)
            end = rows[-1]
            end["elapsed_seconds"] = 34.0
            write_jsonl(manifest_path, rows)
            (fixture.capture / "checkpoint.json").write_text(
                json.dumps({"status": "complete", **end}, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(join.JoinError, "wall-clock interval"):
                join.build_report(fixture.campaign, fixture.capture)

    def test_event_sequence_time_and_artifact_pull_order_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            events_path = fixture.campaign / "events.jsonl"
            rows = read_jsonl(events_path)
            rows = [
                row
                for row in rows
                if not (
                    row.get("event") == "singleton_selected"
                    and row.get("sequence") == 0
                )
            ]
            write_jsonl(events_path, rows)
            with self.assertRaisesRegex(join.JoinError, "singleton_selected"):
                join.build_report(fixture.campaign, fixture.capture)

        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            events_path = fixture.campaign / "events.jsonl"
            rows = read_jsonl(events_path)
            for row in rows:
                if (
                    row.get("event") == "segment_offsets_before"
                    and row.get("sequence") == 1
                ):
                    row["wall_time_utc"] = datetime_iso(BASE_TIME + 4)
                elif (
                    row.get("event") == "segment_offsets_after"
                    and row.get("sequence") == 1
                ):
                    row["wall_time_utc"] = datetime_iso(BASE_TIME + 9)
            write_jsonl(events_path, rows)
            with self.assertRaisesRegex(join.JoinError, "overlaps/regresses"):
                join.build_report(fixture.campaign, fixture.capture)

        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            events_path = fixture.campaign / "events.jsonl"
            rows = read_jsonl(events_path)
            pull_index = next(
                index
                for index, row in enumerate(rows)
                if row.get("event") == "artifact_pull"
            )
            pull = rows.pop(pull_index)
            final_segment_done = next(
                index
                for index, row in enumerate(rows)
                if row.get("event") == "segment_complete"
                and row.get("sequence") == 2
            )
            rows.insert(final_segment_done, pull)
            write_jsonl(events_path, rows)
            with self.assertRaisesRegex(join.JoinError, "artifact_pull"):
                join.build_report(fixture.campaign, fixture.capture)

        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            events_path = fixture.campaign / "events.jsonl"
            rows = read_jsonl(events_path)
            before = next(
                row
                for row in rows
                if row.get("event") == "segment_offsets_before"
                and row.get("sequence") == 0
            )
            after = next(
                row
                for row in rows
                if row.get("event") == "segment_offsets_after"
                and row.get("sequence") == 0
            )
            after["artifacts"]["AlfaOBD_Debug.bin"]["size"] = before[
                "artifacts"
            ]["AlfaOBD_Debug.bin"]["size"]
            write_jsonl(events_path, rows)
            with self.assertRaisesRegex(join.JoinError, "did not grow"):
                join.build_report(fixture.campaign, fixture.capture)

    def test_repeated_anchor_must_resolve_to_same_exact_request(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory), repeated_did=0x1002)
            with self.assertRaisesRegex(join.JoinError, "repeat anchor"):
                join.build_report(fixture.campaign, fixture.capture)

    def test_bad_wire_response_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(
                Path(directory),
                bad_wire_response=True,
            )
            with self.assertRaisesRegex(join.JoinError, "positive 62"):
                join.build_report(fixture.campaign, fixture.capture)

    def test_extra_distinct_wire_request_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(
                Path(directory),
                extra_wire_request=True,
            )
            with self.assertRaisesRegex(join.JoinError, "one distinct wire request"):
                join.build_report(fixture.campaign, fixture.capture)

    def test_wrong_info_run_order_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(
                Path(directory),
                info_schedule=[
                    "Vehicle speed",
                    "Engine speed",
                    "Engine speed",
                ],
            )
            with self.assertRaisesRegex(join.JoinError, "label-run sequence"):
                join.build_report(fixture.campaign, fixture.capture)

    def test_buffered_info_count_mismatch_is_reported_not_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(
                Path(directory),
                info_sample_counts=[3, 2, 1],
            )
            report = join.build_report(fixture.campaign, fixture.capture)

        self.assertEqual(report["summary"]["candidate_segments"], 3)
        self.assertEqual(
            [
                segment["info"]["sample_count"]
                for segment in report["segments"]
            ],
            [3, 2, 1],
        )
        self.assertEqual(
            [
                segment["wire"]["pair_count"]
                for segment in report["segments"]
            ],
            [2, 2, 2],
        )
        self.assertEqual(
            [
                segment["info_wire_count_match"]
                for segment in report["segments"]
            ],
            [False, True, False],
        )
        self.assertEqual(
            report["segments"][0]["info"]["rendered_distribution"],
            [
                {"value": "1000 rpm", "count": 1},
                {"value": "1001 rpm", "count": 1},
                {"value": "1002 rpm", "count": 1},
            ],
        )
        self.assertEqual(
            report["segments"][0]["wire"]["raw_data_distribution"],
            [
                {"value": "03E8", "count": 1},
                {"value": "03E9", "count": 1},
            ],
        )

    def test_info_runs_decode_utf8_after_trimming_partial_boundary_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster_Info.log"
            payload = (
                "partial Ω\r\n"
                "Outside temperature: 21.00 °C\r\n"
                "Battery Voltage (+30): 11.80 V\r\n"
                "trailing €"
            ).encode("utf-8")
            path.write_bytes(payload)
            start = payload.index("Ω".encode("utf-8")) + 1
            runs, _slice_hash = join.parse_info_runs(
                path,
                join.ArtifactBoundary(start, len(payload) - 1),
                ("Outside temperature", "Battery Voltage (+30)"),
                maximum_bytes=4096,
                maximum_samples=10,
            )

        self.assertEqual(
            [
                (run.label, list(run.rendered_values))
                for run in runs
            ],
            [
                ("Outside temperature", ["21.00 °C"]),
                ("Battery Voltage (+30)", ["11.80 V"]),
            ],
        )

    def test_info_runs_reject_unexpected_parameter_shaped_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster_Info.log"
            path.write_text(
                "Engine speed: 1000 rpm\r\n"
                "Unexpected pressure: 42 kPa\r\n"
                "Vehicle speed: 10 km/h\r\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                join.JoinError,
                "unexpected parameter-shaped Info row",
            ):
                join.parse_info_runs(
                    path,
                    join.ArtifactBoundary(0, path.stat().st_size),
                    ("Engine speed", "Vehicle speed"),
                    maximum_bytes=4096,
                    maximum_samples=10,
                )

    def test_debug_boundary_clipping_and_unanswered_final_send_are_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(
                Path(directory),
                wire_sample_count=4,
                debug_sample_counts=[4, 4, 4],
                debug_response_counts=[4, 4, 3],
            )
            report = join.build_report(fixture.campaign, fixture.capture)

        final_run = report["debug_artifact"]["corroboration"]["runs"][-1]
        self.assertEqual(final_run["debug_request_count"], 4)
        self.assertEqual(final_run["debug_response_count"], 3)
        self.assertEqual(
            final_run["request_alignment"]["status"],
            "exact_full_run",
        )
        self.assertEqual(
            final_run["response_alignment"]["status"],
            "clipped_contiguous_subset",
        )
        self.assertEqual(report["summary"]["candidate_segments"], 3)

    def test_debug_interior_response_contradiction_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(
                Path(directory),
                bad_debug_response=True,
            )
            with self.assertRaisesRegex(
                join.JoinError,
                "interior segment 1 Debug response",
            ):
                join.build_report(fixture.campaign, fixture.capture)

    def test_debug_boundary_request_response_retention_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(
                Path(directory),
                wire_sample_count=4,
                debug_sample_counts=[4, 4, 4],
                debug_response_counts=[4, 4, 2],
            )
            with self.assertRaisesRegex(
                join.JoinError,
                "retention counts are incompatible",
            ):
                join.build_report(fixture.campaign, fixture.capture)

    def test_debug_first_run_clipping_must_be_a_wire_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(
                Path(directory),
                wire_sample_count=4,
                debug_sample_counts=[3, 4, 4],
            )
            with self.assertRaisesRegex(
                join.JoinError,
                "first segment Debug response clipping is not a suffix",
            ):
                join.build_report(fixture.campaign, fixture.capture)

    def test_debug_allows_boundary_empty_prompt_but_rejects_interior_empty_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "AlfaOBD_Debug.bin"
            leading = debug_response_text("12:00:00", "\r>")
            first = debug_exchange(
                "12:00:01",
                bytes.fromhex("221000"),
                bytes.fromhex("62100001"),
            )
            path.write_bytes(alfa_bin(leading + first))
            streams, _digest = join.parse_debug_transport_streams(
                path,
                join.ArtifactBoundary(0, path.stat().st_size),
                maximum_bytes=4096,
                maximum_messages=10,
            )
            self.assertEqual(streams.leading_empty_prompts, 1)
            self.assertEqual(streams.trailing_empty_prompts, 0)

            interior = debug_response_text("12:00:02", "\r>")
            second = debug_exchange(
                "12:00:03",
                bytes.fromhex("221000"),
                bytes.fromhex("62100002"),
            )
            path.write_bytes(alfa_bin(leading + first + interior + second))
            with self.assertRaisesRegex(join.JoinError, "interior empty"):
                join.parse_debug_transport_streams(
                    path,
                    join.ArtifactBoundary(0, path.stat().st_size),
                    maximum_bytes=4096,
                    maximum_messages=10,
                )

    def test_debug_parses_elm_length_header_with_indexed_segments(self):
        payload = join._parse_debug_response_block(
            "008\r\n"
            "0:62F190313233\r\n"
            "1:34353637\r\n"
        )
        self.assertEqual(payload, bytes.fromhex("62F1903132333435"))
        wrapped_payload = bytes(range(118))
        wrapped_parts = [wrapped_payload[:6]]
        wrapped_parts.extend(
            wrapped_payload[offset : offset + 7]
            for offset in range(6, len(wrapped_payload), 7)
        )
        wrapped = join._parse_debug_response_block(
            "076\r\n"
            + "\r\n".join(
                f"{index & 0xF:X}:{part.hex().upper()}"
                for index, part in enumerate(wrapped_parts)
            )
            + "\r\n"
        )
        self.assertEqual(wrapped, wrapped_payload)
        with self.assertRaisesRegex(join.JoinError, "does not terminate"):
            join._parse_debug_response_block(
                "00B\r\n"
                "0:62F190313233\r\n"
                "1:34353637\r\n"
            )
        with self.assertRaisesRegex(join.JoinError, "without a length header"):
            join._parse_debug_response_block(
                "0:62F190313233\r\n"
                "1:34353637\r\n"
            )

    def test_capture_must_cover_every_segment_and_empty_sequence_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            manifest_path = fixture.capture / "manifest.jsonl"
            rows = read_jsonl(manifest_path)
            chunk = next(row for row in rows if row.get("type") == "chunk")
            chunk["first_frame_timestamp"] = BASE_TIME + 0.1
            write_jsonl(manifest_path, rows)
            with self.assertRaisesRegex(join.JoinError, "manifest bounds"):
                join.build_report(fixture.campaign, fixture.capture)

        segment = join.SegmentEvidence(
            sequence=0,
            gauge="No traffic",
            before_time=1.0,
            after_time=2.0,
            boundaries={},
        )
        with self.assertRaisesRegex(join.JoinError, "no complete"):
            join.parse_wire_segment(
                [],
                segment,
                txid=TXID,
                rxid=RXID,
            )

    def test_chunk_interval_gap_cannot_collapse_into_capture_min_max(self):
        capture = join.ValidatedCapture(
            spec=join.CaptureSpec(Path("/evidence/run"), "gap-run"),
            frames=(),
            coverage_intervals=((0.0, 4.0, 0), (6.0, 10.0, 1)),
            provenance={},
        )
        segment = join.SegmentEvidence(
            sequence=0,
            gauge="Gap",
            before_time=1.0,
            after_time=9.0,
            boundaries={},
        )
        with self.assertRaisesRegex(join.JoinError, "not continuous"):
            join.validate_continuous_coverage([capture], [segment])

    def test_chunk_timing_and_hash_verified_stream_lines_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            manifest_path = fixture.capture / "manifest.jsonl"
            rows = read_jsonl(manifest_path)
            chunk = next(row for row in rows if row.get("type") == "chunk")
            chunk["elapsed_seconds"] = 1.0
            write_jsonl(manifest_path, rows)
            with self.assertRaisesRegex(join.JoinError, "elapsed_seconds disagrees"):
                join.build_report(fixture.campaign, fixture.capture)

        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            stream = fixture.capture / "chunk_000000_priority.candump"
            stream.write_bytes(stream.read_bytes() + b"not-a-candump-frame\n")
            refresh_capture_stream_record(fixture.capture)
            with self.assertRaisesRegex(join.JoinError, "malformed candump line"):
                join.build_report(fixture.campaign, fixture.capture)

        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            stream = fixture.capture / "chunk_000000_priority.candump"
            lines = stream.read_bytes().splitlines(keepends=True)
            lines.append(
                f"({BASE_TIME + 25.0:.6f}) can0 {TXID:X}#\n".encode("ascii")
            )
            lines.sort(key=lambda line: join.parse_candump_frame(line)[0])
            stream.write_bytes(b"".join(lines))
            refresh_capture_stream_record(fixture.capture)
            with self.assertRaisesRegex(join.JoinError, "empty module"):
                join.build_report(fixture.campaign, fixture.capture)

    def test_per_segment_resource_cap_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            with self.assertRaisesRegex(join.JoinError, "per-segment resource cap"):
                join.build_report(
                    fixture.campaign,
                    fixture.capture,
                    maximum_segment_bytes=2,
                )

    def test_fixed_whole_evidence_caps_cannot_be_raised_by_cli_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            with mock.patch.object(join, "HARD_MAX_OUTER_MESSAGES", 1):
                with self.assertRaisesRegex(join.JoinError, "Info sample cap"):
                    join.build_report(
                        fixture.campaign,
                        fixture.capture,
                        maximum_exchanges_per_segment=1_000_000,
                    )

        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            with mock.patch.object(join, "HARD_MAX_WIRE_FRAMES", 1):
                with self.assertRaisesRegex(join.JoinError, "wire-frame cap"):
                    join.build_report(
                        fixture.campaign,
                        fixture.capture,
                        maximum_wire_messages=1_000_000,
                    )

    def test_iso_tp_reassembly_supports_11_bit_ids_and_multiframe_response(self):
        payload = bytes.fromhex("62F19031323334353637")
        lines = [
            sf_line(1.0, 0x7E0, bytes.fromhex("22F190")),
            b"(1.100000) can0 7E8#100A62F190313233\n",
            b"(1.200000) can0 7E0#3000000000000000\n",
            b"(1.300000) can0 7E8#2134353637000000\n",
        ]
        messages = join.reassemble_wire_messages(
            lines,
            selected_ids=frozenset((0x7E0, 0x7E8)),
            maximum_messages=10,
        )
        self.assertEqual(
            [(message.can_id, message.payload) for message in messages],
            [(0x7E0, bytes.fromhex("22F190")), (0x7E8, payload)],
        )

    def test_pre_boundary_first_frame_is_reassembled_before_message_filtering(self):
        payload = bytes.fromhex("62F19031323334353637")
        frames = [
            join.WireFrame(
                0.9, 0x7E8, bytes.fromhex("100A62F190313233"), 0
            ),
            join.WireFrame(
                1.0, 0x7E0, bytes.fromhex("3000000000000000"), 0
            ),
            join.WireFrame(
                1.1, 0x7E8, bytes.fromhex("2134353637000000"), 0
            ),
        ]
        messages = join.reassemble_wire_frames(
            frames,
            maximum_messages=10,
        )
        self.assertEqual(
            [(message.can_id, message.payload) for message in messages],
            [(0x7E8, payload)],
        )

    def test_zstd_recorder_path_uses_bounded_streaming_decompression(self):
        class FakeProcess:
            def __init__(self):
                self.stdout = io.BytesIO(sf_line(1.0, TXID, bytes.fromhex("221000")))

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

            def terminate(self):
                pass

        commands = []

        def popen(command, **kwargs):
            commands.append((command, kwargs))
            return FakeProcess()

        budget = [0]
        with mock.patch.object(join.shutil, "which", return_value="/usr/bin/zstd"):
            lines = list(
                join.iter_capture_lines(
                    Path("/evidence/chunk_000000_priority.candump.zst"),
                    byte_budget=budget,
                    maximum_bytes=4096,
                    popen=popen,
                )
            )
        self.assertEqual(len(lines), 1)
        self.assertEqual(commands[0][0][:3], ["/usr/bin/zstd", "-dc", "--"])
        self.assertEqual(budget[0], len(lines[0]))

    def test_cli_writes_only_requested_offline_report(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvidenceFixture(Path(directory))
            output = Path(directory) / "result" / "joined.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                returncode = join.main(
                    [
                        str(fixture.campaign),
                        str(fixture.capture),
                        "--output",
                        str(output),
                    ]
                )
            payload = json.loads(output.read_text(encoding="utf-8"))
            original = output.read_bytes()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                second_returncode = join.main(
                    [
                        str(fixture.campaign),
                        str(fixture.capture),
                        "--output",
                        str(output),
                    ]
                )
            after_second = output.read_bytes()

        self.assertEqual(returncode, 0)
        self.assertEqual(second_returncode, 2)
        self.assertIn("refusing to overwrite", stderr.getvalue())
        self.assertEqual(after_second, original)
        self.assertEqual(payload["classification"], "candidate_only")
        self.assertIn("Candidate-only singleton join", stdout.getvalue())

    def test_explicit_two_run_capture_set_merges_and_deduplicates_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = EvidenceFixture(root)
            source = fixture.capture / "chunk_000000_priority.candump"
            lines = source.read_bytes().splitlines(keepends=True)

            def timestamp(line: bytes) -> float:
                parsed = join.parse_candump_frame(line)
                assert parsed is not None
                return parsed[0]

            first_lines = [
                line for line in lines if timestamp(line) <= BASE_TIME + 12.6
            ]
            second_lines = [
                line for line in lines if timestamp(line) >= BASE_TIME + 10.5
            ]
            first = write_capture_run(
                root / "capture-a",
                run_id="cluster-join-test-part-a",
                lines=first_lines,
                start_time=BASE_TIME - 1,
                end_time=BASE_TIME + 14,
            )
            second = write_capture_run(
                root / "capture-b",
                run_id="cluster-join-test-part-b",
                lines=second_lines,
                start_time=BASE_TIME + 10,
                end_time=BASE_TIME + 27,
            )
            capture_set = root / "capture-set.json"
            capture_set.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "singleton_campaign_id": "cluster-join-test",
                        "captures": [first, second],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            report = join.build_report(
                fixture.campaign,
                capture_set=capture_set,
            )

        self.assertEqual(report["summary"]["candidate_segments"], 3)
        self.assertEqual(report["passive_capture"]["mode"], "capture_set")
        self.assertEqual(len(report["passive_capture"]["runs"]), 2)
        self.assertGreater(
            report["passive_capture"]["merge"][
                "exact_overlap_observations_deduplicated"
            ],
            0,
        )
        self.assertEqual(
            set(report["passive_capture"]["coverage_union"][0]["run_ids"]),
            {
                "cluster-join-test-part-a",
                "cluster-join-test-part-b",
            },
        )
        self.assertTrue(
            all(
                "chunk_sequence" in member
                for member in report["passive_capture"]["coverage_union"][0][
                    "members"
                ]
            )
        )

    def test_capture_set_declared_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = EvidenceFixture(root)
            capture_set = root / "capture-set.json"
            capture_set.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "singleton_campaign_id": "cluster-join-test",
                        "captures": [
                            {
                                "run_id": "cluster-join-test",
                                "path": str(fixture.capture),
                                "run_sha256": sha256(
                                    fixture.capture / "run.json"
                                ),
                                "checkpoint_sha256": sha256(
                                    fixture.capture / "checkpoint.json"
                                ),
                                "manifest_sha256": "0" * 64,
                            }
                        ],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(join.JoinError, "hash does not match"):
                join.build_report(
                    fixture.campaign,
                    capture_set=capture_set,
                )

    def test_capture_set_conflicting_overlap_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = EvidenceFixture(root)
            source_lines = (
                fixture.capture / "chunk_000000_priority.candump"
            ).read_bytes().splitlines(keepends=True)
            first = write_capture_run(
                root / "capture-a",
                run_id="cluster-join-test-conflict-a",
                lines=source_lines,
                start_time=BASE_TIME - 1,
                end_time=BASE_TIME + 31,
            )
            conflicting_lines = list(source_lines)
            conflict_index = next(
                index
                for index, line in enumerate(conflicting_lines)
                if (
                    (parsed := join.parse_candump_frame(line)) is not None
                    and parsed[0] == BASE_TIME + 1.5
                    and parsed[1] == TXID
                )
            )
            conflicting_lines[conflict_index] = sf_line(
                BASE_TIME + 1.5,
                TXID,
                bytes.fromhex("2210FF"),
            )
            second = write_capture_run(
                root / "capture-b",
                run_id="cluster-join-test-conflict-b",
                lines=conflicting_lines,
                start_time=BASE_TIME - 1,
                end_time=BASE_TIME + 31,
            )
            capture_set = root / "capture-set.json"
            capture_set.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "singleton_campaign_id": "cluster-join-test",
                        "captures": [first, second],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(join.JoinError, "captures disagree"):
                join.build_report(
                    fixture.campaign,
                    capture_set=capture_set,
                )


if __name__ == "__main__":
    unittest.main()
