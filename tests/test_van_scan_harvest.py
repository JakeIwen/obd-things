"""Passive harvest of the van's own (tester F1) diagnostic sweeps from recorder chunks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from lib.dtc import DtcHistory, READ_DTC_BY_STATUS_REQUEST
from lib.modules import MODULES
from projects.vehicle_data import van_scan_harvest as harvest


T0 = 1790284337.0  # 2026-09-24T21:12:17Z


def line(ts, can_id, data_hex, iface="can0"):
    return f"({ts:.6f}) {iface} {can_id}#{data_hex.replace(' ', '').upper()}"


def sf(payload_hex):
    payload = bytes.fromhex(payload_hex)
    return (bytes((len(payload),)) + payload).ljust(8, b"\x00").hex().upper()


def multi_frame(payload: bytes):
    """ISO-TP FF + CFs (classic CAN) for *payload*."""
    frames = [bytes((0x10 | (len(payload) >> 8), len(payload) & 0xFF)) + payload[:6]]
    rest = payload[6:]
    seq = 1
    while rest:
        frames.append((bytes((0x20 | seq,)) + rest[:7]).ljust(8, b"\xAA"))
        rest = rest[7:]
        seq = (seq + 1) & 0x0F
    return [frame.hex().upper() for frame in frames]


def exchange(ts, target, request_hex, response=None, *, iface="can0", via="F1", gap=0.004):
    """Request + (single or multi-frame) response lines."""
    out = [line(ts, f"18DA{target}F1", sf(request_hex), iface)]
    if response is None:
        return out
    payload = bytes.fromhex(response)
    if len(payload) <= 7:
        out.append(line(ts + gap, f"18DA{via}{target}", sf(response), iface))
        return out
    frames = multi_frame(payload)
    out.append(line(ts + gap, f"18DA{via}{target}", frames[0], iface))
    out.append(line(ts + gap + 0.001, f"18DA{target}F1", "300000", iface))  # tester FC
    for index, frame in enumerate(frames[1:]):
        out.append(line(ts + gap + 0.002 + index * 0.001, f"18DA{via}{target}", frame, iface))
    return out


def frames_of(lines):
    return [f for f in (harvest.parse_candump_line(x) for x in lines) if f is not None]


VIN = "3C6LRVDG4NE123456"
F190_RESPONSE = "62F190" + VIN.encode().hex()
F1A0_RESPONSE = "62F1A0" + (b"68518674AC " + VIN.encode() + b"0285015767").hex()


class IsoTpAndPairingTests(unittest.TestCase):
    def test_single_and_multi_frame_exchanges_with_response_pending(self):
        lines = exchange(T0, "40", "22F132", "62F132414243")
        lines += [line(T0 + 1.0, "18DA40F1", sf("22F1A0")), line(T0 + 1.01, "18DAF140", sf("7F2278"))]
        lines += exchange(T0 + 1.2, "40", "22F1A0", F1A0_RESPONSE)[1:]
        exchanges, intervals, carry, stats = harvest.process_frames("c-can", frames_of(lines))
        self.assertEqual([e["request_hex"] for e in exchanges], ["22F132", "22F1A0"])
        self.assertEqual(exchanges[0]["outcome"], "positive")
        self.assertEqual(exchanges[0]["response_hex"], "62F132414243")
        self.assertEqual(exchanges[1]["outcome"], "positive")
        self.assertEqual(exchanges[1]["response_pending_78"], 1)
        self.assertTrue(exchanges[1]["vin_masked"])
        self.assertNotIn(VIN.encode().hex().upper(), exchanges[1]["response_hex"])
        self.assertEqual(intervals, [])
        self.assertEqual(stats["pi_requests"], 0)

    def test_negative_and_no_response(self):
        lines = exchange(T0, "30", "22F100", "7F2231")
        lines += exchange(T0 + 1, "26", "22F132")  # silent module
        lines += exchange(T0 + 20, "30", "22F132", "62F13220")
        exchanges, _, _, _ = harvest.process_frames("can-ch", frames_of(lines))
        outcomes = [(e["target"], e["request_hex"], e["outcome"]) for e in exchanges]
        self.assertIn(("30", "22F100", "negative"), outcomes)
        self.assertIn(("26", "22F132", "no_response"), outcomes)
        self.assertEqual(next(e for e in exchanges if e["outcome"] == "negative")["nrc"], "31")

    def test_pi_helper_requests_are_counted_not_harvested(self):
        lines = []
        for i in range(5):
            lines += exchange(T0 + i, "10", "2201A1", "6201A18000")
            lines += exchange(T0 + i + 0.02, "C7", "2231D0", "6231D00F55")
            lines += exchange(T0 + i + 0.04, "10", "1902FF", "5902FF")
        lines += exchange(T0 + 60, "10", "22F190", F190_RESPONSE)
        exchanges, intervals, _, stats = harvest.process_frames("c-can", frames_of(lines))
        self.assertEqual([e["request_hex"] for e in exchanges], ["22F190"])
        self.assertEqual(stats["pi_requests"], 15)
        self.assertEqual(len(intervals), 1)  # one polling burst, T0..T0+4
        self.assertEqual(intervals[0][2], 15)
        masked = bytes.fromhex(exchanges[0]["response_hex"])
        self.assertEqual(masked, bytes.fromhex("62F190") + b"*" * 17)

    def test_obd_vin_environment_value_is_masked_anywhere(self):
        odd = "62F18C" + (b"XX" + VIN.encode()).hex()
        with mock.patch.dict(os.environ, {"OBD_VIN": VIN}):
            payload, masked = harvest.mask_vin(bytes.fromhex(odd))
        self.assertTrue(masked)
        self.assertNotIn(VIN.encode(), payload)

    def test_gateway_copy_is_merged_with_the_can_ch_exchange(self):
        ccan = exchange(T0, "28", "19020D", "5902CF5200174C", via="F2")
        canch = exchange(T0 + 0.0003, "28", "19020D", "5902CF5200174C", iface="can2")
        a, _, _, _ = harvest.process_frames("c-can", frames_of(ccan))
        b, _, _, _ = harvest.process_frames("can-ch", frames_of(canch))
        merged = harvest.dedupe_gateway_copies(a + b)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["bus"], "can-ch")
        self.assertEqual(merged[0]["buses_seen"], ["c-can", "can-ch"])

    def test_carry_state_pairs_a_response_in_the_next_chunk(self):
        first = [line(T0, "18DA18F1", sf("22F1A0"))]
        second = exchange(T0, "18", "22F1A0", "62F1A0" + "41" * 20)[1:]
        a, _, carry, _ = harvest.process_frames("c-can", frames_of(first))
        self.assertEqual(a, [])
        carry = json.loads(json.dumps(carry))  # survives the JSON checkpoint
        b, _, _, _ = harvest.process_frames("c-can", frames_of(second), carry)
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0]["outcome"], "positive")


class PiRequestSetSyncTests(unittest.TestCase):
    """The exclusion set must match what the Pi's helpers can actually send."""

    def test_fixed_helpers(self):
        from projects.vehicle_data import active_drive, bcan_auxiliary, pcm_electrical, radar_alignment

        def uds(frame: bytes) -> bytes:
            return frame[1 : 1 + frame[0]]

        def target(module) -> int:
            return (module.txid >> 8) & 0xFF

        pcm = {
            uds(pcm_electrical.GENERATOR_DUTY_REQUEST_DATA),
            uds(pcm_electrical.CRANKSHAFT_TORQUE_REQUEST_DATA),
            uds(pcm_electrical.VVT_OIL_TEMPERATURE_REQUEST_DATA),
        }
        self.assertEqual(harvest.PI_FIXED_REQUESTS[target(MODULES["pcm"])], pcm)
        tpms = {bytes((0x22, did >> 8, did & 0xFF)) for did, _ in active_drive.TPMS_PROFILES}
        self.assertEqual(harvest.PI_FIXED_REQUESTS[target(MODULES["rf_hub"])], tpms)
        self.assertEqual(
            harvest.PI_FIXED_REQUESTS[target(radar_alignment.MODULE)],
            {uds(radar_alignment.REQUEST_DATA)},
        )
        self.assertEqual(
            harvest.PI_FIXED_REQUESTS[target(bcan_auxiliary.ICS)],
            {uds(bcan_auxiliary.REQUEST_PAYLOAD)},
        )

    def test_dtc_tooling(self):
        from tools import dtc_inventory

        self.assertIn(READ_DTC_BY_STATUS_REQUEST, harvest.PI_DTC_TOOL_REQUESTS)
        for _, request in dtc_inventory.DEFAULT_REQUESTS + (dtc_inventory.SUPPORTED_DTCS_REQUEST,):
            self.assertIn(request, harvest.PI_DTC_TOOL_REQUESTS)
        self.assertNotIn(harvest.VAN_DTC_REQUEST, harvest.PI_DTC_TOOL_REQUESTS)


def van_sweep(t):
    """A small sweep: identity + 19 02 0D on BCM (C-CAN), ABS (CAN-CH via gateway), PCM KWP."""
    ccan, canch, bcan = [], [], []
    ccan += exchange(t, "40", "22F1A0", F1A0_RESPONSE)
    ccan += exchange(t + 2, "40", "19020D", "59024F9636154D962E150F")
    ccan += exchange(t + 3, "28", "19020D", "5902CF5200174C", via="F2")
    canch += exchange(t + 3.0003, "28", "19020D", "5902CF5200174C", iface="can2")
    ccan += exchange(t + 4, "10", "19020D", "7F1911")
    ccan += exchange(t + 5, "10", "1800FF00", "5800")
    bcan += exchange(t + 6, "87", "19020D", "59024F957713" + "0F", iface="can1")
    return ccan, canch, bcan


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="van-scan-")
        root = Path(self.tmp.name)
        self.captures = root / "captures"
        self.campaign = self.captures / "broker-drive-20260924T210710082916"
        self.out = root / "out"
        self.db = root / "dtc-history.sqlite3"
        self.cache = root / "van-scan-cache.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write_chunk(self, bus, sequence, lines, *, end=False):
        bus_dir = self.campaign / bus
        bus_dir.mkdir(parents=True, exist_ok=True)
        name = f"chunk_{sequence:06d}_full.candump.zst"
        raw = ("\n".join(lines) + "\n").encode()
        path = bus_dir / name
        path.write_bytes(subprocess.run(["zstd", "-q", "-c"], input=raw, check=True, capture_output=True).stdout)
        stamps = [float(x[1 : x.index(")")]) for x in lines] or [0.0]
        entry = {
            "type": "chunk",
            "complete": True,
            "sequence": sequence,
            "last_frame_timestamp": max(stamps),
            "streams": {"full": {"complete": True, "path": str(path), "compressed_bytes": path.stat().st_size}},
        }
        with (bus_dir / "manifest.jsonl").open("a") as handle:
            handle.write(json.dumps(entry) + "\n")
            if end:
                handle.write(json.dumps({"type": "capture_end"}) + "\n")

    def run_harvest(self):
        return harvest.run(
            capture_root=self.captures,
            out_dir=self.out,
            db_path=self.db,
            cache_path=self.cache,
            patterns=None,
            max_chunks=None,
            log=lambda _text: None,
        )

    def test_end_to_end_idempotent_import_and_cache(self):
        ccan, canch, bcan = van_sweep(T0)
        pi = []
        for i in range(3):
            pi += exchange(T0 - 30 + i, "10", "2201A1", "6201A18000")
        # Split the sweep across two C-CAN chunks; B-CAN/CAN-CH still recording.
        self.write_chunk("c-can", 0, pi + ccan[:4])
        self.write_chunk("c-can", 1, ccan[4:] + exchange(T0 + 200, "10", "2201A1", "6201A18000"))
        self.write_chunk("can-ch", 0, canch + [line(T0 + 200, "18DA99F1", "0000")])
        self.write_chunk("b-can", 0, bcan + [line(T0 + 200, "18DA99F1", "0000", "can1")])
        # A chunk being written is never read.
        (self.campaign / "c-can" / "chunk_000002_full.candump.zst.partial").write_bytes(b"not zstd")

        first = self.run_harvest()
        self.assertEqual(first["chunks_processed"], 4)
        self.assertEqual(len(first["imported"]), 1)
        report_path = next((self.out / "scans").glob("*.json"))
        report = json.loads(report_path.read_text())
        self.assertEqual(report["classification"], "in_vehicle_health_check")
        self.assertTrue(report["pi_quiet"])
        self.assertEqual(report["buses"], ["b-can", "c-can", "can-ch"])
        modules = {m["module_key"]: m for m in report["modules"]}
        self.assertEqual(modules["abs_canch"]["logical_bus"], "can-ch")
        self.assertEqual(
            [(d["fca_display"], d["status"]) for d in modules["bcm_ccan"]["dtc"]["dtcs"]],
            [("B1636-15", "4D"), ("B162E-15", "0F")],
        )
        self.assertEqual(modules["pcm"]["dtc"]["protocol"], "kwp2000")
        self.assertEqual(modules["pcm"]["dtc"]["dtcs"], [])
        self.assertNotIn(VIN, report_path.read_text())
        self.assertTrue(modules["bcm_ccan"]["dids"]["F1A0"]["vin_masked"])

        second = self.run_harvest()
        self.assertEqual(second["chunks_processed"], 0)
        self.assertEqual(second["imported"], [])
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM in_vehicle_scans").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM in_vehicle_module_results").fetchone()[0], 4
            )

        cache = json.loads(self.cache.read_text())
        self.assertEqual(cache["scan_count"], 1)
        self.assertEqual(cache["provenance"], "in-vehicle scan (source F1), passive")
        by_key = {m["module_key"]: m for m in cache["modules"]}
        self.assertEqual(set(by_key), {"bcm_ccan", "abs_canch", "pcm", "uconnect_bcan"})
        self.assertEqual(by_key["uconnect_bcan"]["dtcs"][0]["display_group"], "current")

        # The Pi's own history still opens and keeps its 19 02 FF constraint.
        with DtcHistory(self.db) as history:
            self.assertEqual(history.snapshot()["schema_version"], 2)
            with self.assertRaises(sqlite3.IntegrityError):
                history.connection.execute(
                    "INSERT INTO module_scans(source_key, source_ref, module_key, module_name, "
                    "logical_bus, bitrate, started_at, completed_at, outcome, request_hex, imported_at) "
                    "VALUES ('x','x','pcm','PCM','c-can',500000,'a','a','success','19 02 0D','a')"
                )

    def test_window_stays_open_until_every_bus_is_read_past_it(self):
        ccan, canch, bcan = van_sweep(T0)
        self.write_chunk("c-can", 0, ccan + [line(T0 + 200, "18DA99F1", "0000")])
        self.write_chunk("can-ch", 0, canch)  # ends right after the sweep; still recording
        first = self.run_harvest()
        self.assertEqual(first["imported"], [])
        self.write_chunk("can-ch", 1, [line(T0 + 300, "18DA99F1", "0000", "can2")])
        (self.campaign / "b-can").mkdir()  # recording, nothing read yet: still open
        self.assertEqual(self.run_harvest()["imported"], [])
        # A B-CAN chunk with no diagnostic frames still advances that bus past the window.
        self.write_chunk("b-can", 0, [line(T0 + 300, "46C", "0000", "can1")])
        second = self.run_harvest()
        self.assertEqual(len(second["imported"]), 1)

    def test_other_f1_traffic_is_reported_but_not_imported(self):
        lines = exchange(T0, "40", "22F132", "62F13241") + exchange(T0 + 1, "10", "3E01", "7E00")
        self.write_chunk("c-can", 0, lines, end=True)
        summary = self.run_harvest()
        self.assertEqual(summary["imported"], [])
        scans = summary["campaigns"][0]["new_scans"]
        self.assertEqual([s["classification"] for s in scans], ["other_f1_traffic"])

    def test_size_mismatch_chunk_is_skipped(self):
        ccan, _, _ = van_sweep(T0)
        self.write_chunk("c-can", 0, ccan)
        path = self.campaign / "c-can" / "chunk_000000_full.candump.zst"
        with path.open("ab") as handle:
            handle.write(b"x")
        self.assertEqual(harvest.completed_chunks(self.campaign / "c-can"), [])


class BusCaptureEndedTests(unittest.TestCase):
    def test_readmitted_secondary_segment_reopens_the_bus(self):
        # drive_recorder appends a new segment after a secondary route loss.
        with tempfile.TemporaryDirectory() as tmp:
            bus_dir = Path(tmp)
            manifest = bus_dir / "manifest.jsonl"
            self.assertFalse(harvest.bus_capture_ended(bus_dir))
            for marker, ended in (
                ("capture_start", False),
                ("capture_end", True),
                ("capture_start", False),
                ("capture_end", True),
            ):
                with manifest.open("a") as handle:
                    handle.write(json.dumps({"type": marker}) + "\n")
                    handle.write(json.dumps({"type": "chunk"}) + "\n")
                self.assertEqual(harvest.bus_capture_ended(bus_dir), ended, marker)


class DriveRecorderConditionsTests(unittest.TestCase):
    def test_conditions_no_longer_deny_the_vans_own_client(self):
        from projects.vehicle_data import drive_recorder

        self.assertNotIn("no external diagnostic client", drive_recorder.DEFAULT_CONDITIONS)
        self.assertIn("van_scan_harvest.py", drive_recorder.DEFAULT_CONDITIONS)


if __name__ == "__main__":
    unittest.main()
