"""Research replies at their largest stay readable through the real web relay (1 MiB limit)."""

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from projects.vehicle_data.assistant_models import Catalog
from projects.vehicle_data.dtc_research import ResearchManager
from projects.vehicle_data.dtc_research_contract import SIZES, encoded_len
from projects.vehicle_data import dtc_research_store as records
from projects.vehicle_data.dtc_research_ledger import Ledger
from projects.vehicle_data.dtc_research_store import Store
from projects.vehicle_data.http_common import MAX_RESPONSE_BYTES
from projects.vehicle_data.warning_chat import ChatHandler, ChatServer, WarningChatManager
from projects.vehicle_data.web import TelemetryWebServer

WIDE = "\U0001F600"  # 12 encoded bytes per character
CODE = ("radar_acc", "C1418-78")


def widest(limit):
    return WIDE * ((limit - 64) // 12)


def full_record(index):
    context = {"code": {"module_key": CODE[0], "fca_display": CODE[1]}}
    fields = {"module_key": CODE[0], "fca_display": CODE[1], "provider": "claude", "model": "opus", "effort": "high"}
    record = records.new_record(f"{index:032x}", fields, context, uuid.uuid4().hex)
    report = records.new_turn("report")
    report.update(state="complete", answer=widest(SIZES["reportEncodedBytes"]), summary=WIDE * 140,
                  phase=None, completed_at=report["started_at"], duration_s=99)
    record["turns"].append(report)
    while records.has_room(record) and len(record["turns"]) < 24:
        turn = records.new_turn("follow_up", widest(SIZES["questionEncodedBytes"]))
        turn.update(state="complete", answer=widest(SIZES["answerEncodedBytes"]), phase=None,
                    completed_at=turn["started_at"], duration_s=9)
        record["turns"].append(turn)
    record["created_at"] = f"2026-11-16T00:{index // 60:02d}:{index % 60:02d}+00:00"
    return record


class Unused:
    def request(self, *_args, **_kwargs):
        raise OSError("not used")


class RelaySizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.chat = WarningChatManager(base / "advisor", Unused(), runner=None)
        store = Store(base / "advisor" / "dtc-research" / "runs")
        for index in range(60):
            record = full_record(index)
            store.records[record["run_id"]] = record
        self.store = store
        research = ResearchManager(store, Ledger(base / "ledger.json"), Catalog(Unused()), Unused(), Unused())
        # A short directory: AF_UNIX paths are limited to about 104 bytes (macOS temp dirs are long).
        self.socket_dir = tempfile.TemporaryDirectory(dir="/tmp", prefix="rs")
        self.socket = str(Path(self.socket_dir.name) / "a.sock")
        self.advisor = ChatServer(self.socket, ChatHandler)
        self.advisor.manager, self.advisor.research = self.chat, research
        self.web = TelemetryWebServer(("127.0.0.1", 0), socket_path="/unused-broker.sock", allow_acquisitions=False,
                                      stream_interval_seconds=.1, stream_max_seconds=.1, warning_chat_socket=self.socket)
        self.threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (self.advisor, self.web)]
        for thread in self.threads:
            thread.start()

    def tearDown(self):
        for server in (self.web, self.advisor):
            server.shutdown()
            server.server_close()
        self.socket_dir.cleanup()
        self.tmp.cleanup()

    def get(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.web.server_port, timeout=20)
        connection.request("GET", path, headers={"Origin": f"http://127.0.0.1:{self.web.server_port}",
                                                 "X-Van-Assistant-Key": self.chat.token, "X-Van-Chat-Client": "b" * 64})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def test_largest_replies_fit_the_relay(self):
        biggest_run = max(encoded_len(records.full(r)) for r in self.store.records.values())
        self.assertLessEqual(biggest_run, SIZES["runEncodedBytes"])
        status, body = self.get("/v1/assistant/dtc-research/code/radar_acc/C1418-78")
        self.assertEqual(status, 200, body[:200])
        self.assertLess(len(body), 600 * 1024)
        self.assertLess(len(body), MAX_RESPONSE_BYTES)
        reply = json.loads(body)
        self.assertEqual(len(reply["runs"]), 60)
        self.assertTrue(all(encoded_len(run) <= 3 * 1024 for run in reply["runs"]))
        status, body = self.get("/v1/assistant/dtc-research")
        self.assertEqual(status, 200)
        self.assertLess(len(body), 200 * 1024)
        run_id = next(iter(self.store.records))
        status, body = self.get(f"/v1/assistant/dtc-research/run/{run_id}")
        self.assertEqual(status, 200)
        self.assertLessEqual(len(body), SIZES["runEncodedBytes"] + 1024)
        status, body = self.get(f"/v1/assistant/dtc-research/run/{run_id}/progress")
        self.assertLess(len(body), 2048)

    def test_a_full_run_refuses_another_follow_up(self):
        record = next(iter(self.store.records.values()))
        self.assertFalse(records.summary(record)["can_continue"])


if __name__ == "__main__":
    unittest.main()
