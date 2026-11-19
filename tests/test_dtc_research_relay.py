"""Research replies at their largest stay readable through the real web relay (1 MiB limit)."""

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from projects.vehicle_data.api_client import TelemetryClient
from projects.vehicle_data.api_credits_contract import ADMIN_ROUTES
from projects.vehicle_data.assistant_models import Catalog
from projects.vehicle_data.dtc_research import ResearchManager
from projects.vehicle_data.dtc_research_contract import SIZES, encoded_len
from projects.vehicle_data import dtc_research_store as records
from projects.vehicle_data.dtc_research_ledger import Ledger
from projects.vehicle_data.dtc_research_store import Store
from projects.vehicle_data.http_common import MAX_RESPONSE_BYTES
from projects.vehicle_data.warning_chat import ChatHandler, ChatServer, WarningChatManager
from projects.vehicle_data.web import TelemetryWebServer
from tests.assistant_lock_support import private_cli_lock

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


class CreditsOnly(Unused):
    """A worker that answers only the credits route (the relay tests need no turns)."""

    def status(self):
        return {"instance_id": "i" * 32, "job": None}

    def credits(self):
        return {"key_installed": True, "enabled": True, "until": "2026-12-08T00:00:00Z", "entries": [],
                "latches": {"account": None, "models": {}}, "last_fallback": None, "ledger_full": False,
                "ledger_available": True}


class RelaySizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.chat = WarningChatManager(base / "advisor", Unused(), runner=None, cli_lock=private_cli_lock(base))
        store = Store(base / "advisor" / "dtc-research" / "runs")
        for index in range(60):
            record = full_record(index)
            store.records[record["run_id"]] = record
        self.store = store
        research = self.research = ResearchManager(store, Ledger(base / "ledger.json"), Catalog(Unused()), CreditsOnly(), Unused(),
                                  pause_file=base / "paused")
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
        return self.call("GET", path)

    def call(self, method, path, payload=None):
        """A request through the web listener with the paired browser's headers."""
        origin = f"http://127.0.0.1:{self.web.server_port}"
        headers = {"Origin": origin, "X-Van-Assistant-Key": self.chat.token, "X-Van-Chat-Client": "b" * 64}
        body = None
        if payload is not None:
            body, headers["Content-Type"] = json.dumps(payload), "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.web.server_port, timeout=20)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

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

    def test_the_credits_route_is_relayed_with_the_access_code_and_never_without_it(self):
        status, body = self.get("/v1/assistant/research/credits")
        self.assertEqual(status, 200, body[:200])
        self.assertEqual(json.loads(body)["balance"]["state"], "unknown")
        connection = http.client.HTTPConnection("127.0.0.1", self.web.server_port, timeout=20)
        connection.request("GET", "/v1/assistant/research/credits",
                           headers={"Origin": f"http://127.0.0.1:{self.web.server_port}",
                                    "X-Van-Assistant-Key": "w" * 40, "X-Van-Chat-Client": "b" * 64})
        self.assertEqual(connection.getresponse().status, 401)  # the advisor checks the code
        connection.close()

    def test_the_admin_routes_answer_404_through_the_web_relay_even_with_valid_credentials(self):
        for method, route in (("POST", "pause"), ("POST", "resume"), ("GET", "drained"),
                              ("GET", "rollbackReady")):
            path = ADMIN_ROUTES[route]
            status, body = self.call(method, path, {} if method == "POST" else None)
            self.assertEqual(status, 404, (path, body))
        self.assertFalse(self.research.pause.paused)

    def test_the_admin_routes_work_on_the_advisors_own_socket_without_an_access_code(self):
        client = TelemetryClient(self.socket, timeout=5)
        self.assertEqual(client.request("POST", ADMIN_ROUTES["pause"], {}), (200, {"paused": True}))
        self.assertTrue(self.research.pause.paused)
        self.assertEqual(client.request("POST", ADMIN_ROUTES["resume"], {}), (200, {"paused": False}))
        self.assertEqual(client.request("GET", ADMIN_ROUTES["pause"])[0], 404)
        status, reply = client.request("GET", ADMIN_ROUTES["rollbackReady"])
        self.assertEqual(status, 200, reply)
        self.assertIn("rollback_ready", reply)

    def test_a_full_run_refuses_another_follow_up(self):
        record = next(iter(self.store.records.values()))
        self.assertFalse(records.summary(record)["can_continue"])


if __name__ == "__main__":
    unittest.main()
