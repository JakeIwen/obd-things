"""Pairing PINs end to end: the real advisor (``ChatServer``) behind two real web relays, as the LAN
and Tailscale listeners are. Every code and PIN here is made up or generated per test."""

import http.client
import io
import json
import os
import socket
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from projects.vehicle_data.assistant_models import Catalog
from projects.vehicle_data.assistant_pairing import OWNER_PATH, REDEEM_PATH, PairingPins
from projects.vehicle_data.dtc_research import ResearchManager
from projects.vehicle_data.dtc_research_ledger import Ledger
from projects.vehicle_data.dtc_research_store import Store
from projects.vehicle_data.warning_chat import ChatHandler, ChatServer, WarningChatManager
from projects.vehicle_data.web import TelemetryWebServer
from tests.assistant_lock_support import private_cli_lock

CLIENT = "b" * 64
CHAT = "a" * 32
# Every assistant route the advisor answers, with a body where it takes one.
AUTHENTICATED_ROUTES = (
    ("GET", "/v1/assistant/status", None),
    ("GET", f"/v1/assistant/chats/{CHAT}", None),
    ("POST", "/v1/assistant/chats", {"event": {"kind": "episode", "id": "1"}, "request_id": CHAT}),
    ("POST", f"/v1/assistant/chats/{CHAT}/messages", {"request_id": CHAT, "message": "hi"}),
    ("POST", f"/v1/assistant/chats/{CHAT}/cancel", {}),
    ("POST", f"/v1/assistant/chats/{CHAT}/apply-warning", {"confirmed": True, "proposal_id": "x"}),
    ("POST", f"/v1/assistant/chats/{CHAT}/remove-warning", {"confirmed": True, "proposal_id": "x"}),
    ("POST", f"/v1/assistant/warnings/{'c' * 64}/remove", {"confirmed": True}),
    ("GET", "/v1/assistant/models", None),
    ("GET", "/v1/assistant/dtc-research", None),
    ("GET", "/v1/assistant/dtc-research/code/radar_acc/C1418-78", None),
    ("GET", f"/v1/assistant/dtc-research/run/{CHAT}", None),
    ("GET", f"/v1/assistant/dtc-research/run/{CHAT}/progress", None),
    ("POST", "/v1/assistant/dtc-research", {"action": "cancel", "run_id": CHAT, "request_id": CHAT}),
    ("GET", OWNER_PATH, None),
    ("POST", OWNER_PATH, {"action": "create"}),
    ("GET", REDEEM_PATH, None),
    ("POST", REDEEM_PATH + "/x", {"pin": "123456"}),
    ("POST", REDEEM_PATH + "x", {"pin": "123456"}),
)


class Unused:
    def request(self, *_args, **_kwargs):
        raise OSError("not used")


class PairingRelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        # model_options() reads the Codex home; keep the owner's out of the test.
        patches = [mock.patch.dict(os.environ, {"CODEX_HOME": str(base / "codex")}),
                   mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO())]
        self.out = [patch.start() for patch in patches][1:]
        for patch in patches:
            self.addCleanup(patch.stop)
        self.chat = WarningChatManager(base / "advisor", Unused(), runner=None, cli_lock=private_cli_lock(base))
        research = ResearchManager(Store(base / "advisor" / "dtc-research" / "runs"), Ledger(base / "ledger.json"),
                                   Catalog(Unused()), Unused(), Unused())
        # A short directory: AF_UNIX paths are limited to about 104 bytes (macOS temp dirs are long).
        self.socket_dir = tempfile.TemporaryDirectory(dir="/tmp", prefix="ap")
        self.socket = str(Path(self.socket_dir.name) / "a.sock")
        self.advisor = ChatServer(self.socket, ChatHandler)
        self.advisor.manager, self.advisor.research = self.chat, research
        self.advisor.pairing = PairingPins(self.chat.token)
        self.webs = [TelemetryWebServer(("127.0.0.1", 0), socket_path="/unused-broker.sock", allow_acquisitions=False,
                                        stream_interval_seconds=.1, stream_max_seconds=.1, warning_chat_socket=self.socket)
                     for _ in range(2)]
        self.threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (self.advisor, *self.webs)]
        for thread in self.threads:
            thread.start()

    def tearDown(self):
        for server in (*self.webs, self.advisor):
            server.shutdown()
            server.server_close()
        self.socket_dir.cleanup()
        self.tmp.cleanup()

    def web(self, index, method, path, body=None, key=None, client=CLIENT):
        """One request through web relay ``index`` as a same-origin browser page would send it."""
        port = self.webs[index].server_port
        headers = {"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"}
        if key is not None:
            headers.update({"X-Van-Assistant-Key": key, "X-Van-Chat-Client": client})
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        response = connection.getresponse()
        reply = json.loads(response.read())
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        connection.close()
        return response.status, reply

    def advisor_request(self, method, path, body=None, headers=None):
        """One request straight to the advisor socket, skipping the web relay's checks."""
        # Head and body leave in ONE write: the advisor rejects before reading a body, and can close
        # between two writes, which made the client's second write a BrokenPipeError.
        payload = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
        lines = [f"{method} {path} HTTP/1.1", "Host: localhost", "Connection: close", f"Content-Length: {len(payload)}"]
        if body is not None:
            lines.append("Content-Type: application/json")
        lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
        wire = ("\r\n".join(lines) + "\r\n\r\n").encode() + payload
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(10)
            sock.connect(self.socket)
            sock.sendall(wire)
            response = http.client.HTTPResponse(sock, method=method)
            response.begin()
            raw = response.read()
        return response.status, json.loads(raw) if raw else {}

    def output(self):
        return "".join(stream.getvalue() for stream in self.out)

    def test_redeem_is_the_only_route_answered_without_the_key(self):
        for method, path, body in AUTHENTICATED_ROUTES:
            for headers in ({}, {"X-Van-Assistant-Key": "x" * 43, "X-Van-Chat-Client": CLIENT}):
                with self.subTest(method=method, path=path, headers=bool(headers)):
                    self.assertEqual(self.advisor_request(method, path, body, headers)[0], 401)
        status, reply = self.advisor_request("POST", REDEEM_PATH, {"pin": "123456"})
        self.assertEqual((status, reply["reason"]), (403, "pin_rejected"))
        status, reply = self.advisor_request("POST", REDEEM_PATH, {"pin": "12"})
        self.assertEqual((status, reply["reason"]), (400, "invalid_request"))
        self.assertEqual(self.advisor_request("GET", OWNER_PATH)[0], 401)

    def test_pairing_owner_route_requires_the_key_and_exact_bodies(self):
        key = {"X-Van-Assistant-Key": self.chat.token, "X-Van-Chat-Client": CLIENT}
        status, reply = self.advisor_request("GET", OWNER_PATH, headers=key)
        self.assertEqual((status, reply["state"], reply["pin_id"]), (200, "none", None))
        self.assertEqual(self.advisor_request("POST", OWNER_PATH, {"action": "create", "x": 1}, key)[0], 400)
        self.assertEqual(self.advisor_request("POST", OWNER_PATH, {"action": "make"}, key)[0], 400)
        status, reply = self.advisor_request("POST", OWNER_PATH, {"action": "create"}, key)
        self.assertEqual(status, 200)
        self.assertRegex(reply["pin"], r"\A[0-9]{6}\Z")
        status, reply = self.advisor_request("POST", OWNER_PATH, {"action": "cancel", "pin_id": reply["pin_id"]}, key)
        self.assertEqual((status, reply["state"]), (200, "cancelled"))

    def test_create_on_one_listener_redeem_on_the_other_without_the_key(self):
        status, created = self.web(0, "POST", OWNER_PATH, {"action": "create"}, key=self.chat.token)
        self.assertEqual(status, 200)
        self.assertEqual(self.web(1, "POST", REDEEM_PATH, {"pin": created["pin"]}, key="x" * 43)[0], 200)
        self.assertEqual(self.web(1, "POST", REDEEM_PATH, {"pin": created["pin"]}), (403, {
            "available": False, "reason": "pin_rejected", "detail": "That PIN was not accepted"}))
        status, reply = self.web(0, "GET", OWNER_PATH, key=self.chat.token)
        self.assertEqual((reply["state"], reply["pin_id"]), ("redeemed", created["pin_id"]))
        # The redeemed code works, from a new browser id, exactly as a pasted one does.
        self.assertEqual(self.web(1, "GET", "/v1/assistant/status", key=self.chat.token, client="d" * 64)[0], 200)
        text = self.output()
        for secret in (self.chat.token, created["pin"], created["pin_id"]):
            self.assertNotIn(secret, text)
        self.assertIn("assistant_pairing", text)

    def test_the_code_comes_back_once_through_the_relay(self):
        _, created = self.web(0, "POST", OWNER_PATH, {"action": "create"}, key=self.chat.token)
        status, reply = self.web(1, "POST", REDEEM_PATH, {"pin": created["pin"]})
        self.assertEqual((status, reply), (200, {"available": True, "access_code": self.chat.token}))
        self.assertNotIn(self.chat.token, self.output())

    def test_locked_pairing_leaves_authenticated_requests_working(self):
        for _ in range(10):
            self.assertEqual(self.web(0, "POST", REDEEM_PATH, {"pin": "999999"})[0], 403)
        status, reply = self.web(0, "POST", REDEEM_PATH, {"pin": "999999"})
        self.assertEqual((status, reply["reason"]), (429, "pairing_locked"))
        self.assertGreater(reply["retry_after_s"], 0)
        self.assertEqual(self.web(0, "POST", OWNER_PATH, {"action": "create"}, key=self.chat.token)[0], 429)
        # A browser that pastes the code (a new client id) and the existing ones keep working.
        for client in (CLIENT, "e" * 64):
            status, reply = self.web(1, "GET", "/v1/assistant/status", key=self.chat.token, client=client)
            self.assertEqual((status, reply["available"]), (200, True))
            status, reply = self.web(0, "GET", "/v1/assistant/dtc-research", key=self.chat.token, client=client)
            self.assertEqual((status, reply["available"]), (200, True))
        status, reply = self.web(1, "GET", OWNER_PATH, key=self.chat.token)
        self.assertGreater(reply["locked_for_s"], 0)

    def test_two_relays_share_one_failure_window(self):
        for index in (0, 1) * 5:
            self.assertEqual(self.web(index, "POST", REDEEM_PATH, {"pin": "999999"})[0], 403)
        for index in (0, 1):
            self.assertEqual(self.web(index, "POST", REDEEM_PATH, {"pin": "999999"})[0], 429)
            self.assertEqual(self.web(index, "POST", OWNER_PATH, {"action": "create"}, key=self.chat.token)[0], 429)
        # Malformed bodies are refused before they count, at the relay or the advisor.
        status, reply = self.web(1, "POST", REDEEM_PATH, {"pin": "1"})
        self.assertEqual((status, reply["reason"]), (400, "invalid_request"))


if __name__ == "__main__":
    unittest.main()
