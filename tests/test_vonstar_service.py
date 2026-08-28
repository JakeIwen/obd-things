from pathlib import Path
import json
import socket
import tempfile
import threading
import unittest
from unittest import mock

from projects.ecu_mapping import vonstar_service as vonstar


class VonstarTests(unittest.TestCase):
    def test_actions_are_exactly_three(self):
        self.assertEqual(set(vonstar.ACTIONS), {"lock_all", "unlock_front", "unlock_cargo"})

    def test_plan_only_refuses_action(self):
        controller = vonstar.VonstarController(execute=False)
        with self.assertRaisesRegex(vonstar.VonstarError, "plan-only"):
            controller.perform("unlock_front", "request-12345678")

    def test_perform_is_one_shot_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = vonstar.VonstarController(execute=True, state_dir=Path(directory))
            proof = {"send_count": 1, "counter_streak": [1, 2, 3], "payload_hex": "AA"}
            with mock.patch.object(vonstar.rke, "execute_once", return_value=proof) as execute:
                first = controller.perform("unlock_front", "request-12345678")
                second = controller.perform("unlock_front", "request-12345678")
        self.assertTrue(first["ok"])
        self.assertIs(first, second)
        execute.assert_called_once_with("unlock_front")

    def test_cooldown_blocks_new_request(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = mock.Mock(side_effect=[10.0, 10.1, 11.0])
            controller = vonstar.VonstarController(execute=True, state_dir=Path(directory), clock=clock)
            proof = {"send_count": 1, "counter_streak": [1, 2, 3], "payload_hex": "AA"}
            with mock.patch.object(vonstar.rke, "execute_once", return_value=proof):
                controller.perform("lock_all", "request-12345678")
                with self.assertRaisesRegex(vonstar.VonstarError, "cooldown"):
                    controller.perform("unlock_front", "request-abcdefgh")

    def test_access_state_is_one_shot_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = vonstar.VonstarController(execute=True, state_dir=Path(directory))
            snapshot = {"wake": {"count": 1}, "doors": {"driver": {"ajar": None}}}
            with mock.patch.object(
                vonstar.rke, "read_access_state_once", return_value=snapshot
            ) as read:
                first = controller.read_access_state("request-state-1234")
                second = controller.read_access_state("request-state-1234")
        self.assertTrue(first["ok"])
        self.assertIs(first, second)
        self.assertEqual(first["access_state"], snapshot)
        read.assert_called_once_with()

    def test_request_id_cannot_change_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = vonstar.VonstarController(execute=True, state_dir=Path(directory))
            proof = {"send_count": 1, "counter_streak": [1, 2, 3], "payload_hex": "AA"}
            with mock.patch.object(vonstar.rke, "execute_once", return_value=proof):
                controller.perform("unlock_front", "request-12345678")
            with self.assertRaisesRegex(vonstar.VonstarError, "another operation"):
                controller.read_access_state("request-12345678")

    def test_access_state_http_path(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = str(Path(directory) / "vonstar.sock")
            controller = mock.Mock()
            controller.read_access_state.return_value = {
                "ok": True,
                "operation": "access_state",
                "access_state": {"wake": {"count": 1}},
            }
            server = vonstar.Server(path, controller)
            worker = threading.Thread(target=server.handle_request)
            worker.start()
            body = json.dumps({"request_id": "request-state-http"}).encode()
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.connect(path)
                client.sendall(
                    b"POST /v1/access-state HTTP/1.1\r\n"
                    b"Host: localhost\r\n"
                    b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode()
                    + body
                )
                response = b""
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    response += chunk
            finally:
                client.close()
                worker.join(timeout=2)
                server.server_close()
            self.assertIn(b"HTTP/1.0 200 OK", response)
            payload = json.loads(response.split(b"\r\n\r\n", 1)[1])
            self.assertEqual(payload["access_state"]["wake"]["count"], 1)
            controller.read_access_state.assert_called_once_with("request-state-http")


if __name__ == "__main__":
    unittest.main()
