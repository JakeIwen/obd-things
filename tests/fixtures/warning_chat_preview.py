"""Loopback UI fixture: deterministic answers, no Codex or vehicle access."""

from pathlib import Path
import argparse
import json
import signal
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_warning_chat import CachedEvidence
from projects.vehicle_data.warning_chat import ChatError, ChatHandler, ChatServer, WarningChatManager
from projects.vehicle_data.web import TelemetryWebHandler, TelemetryWebServer


class QuietWebHandler(TelemetryWebHandler):
    def log_message(self, *_args):
        pass


class FixtureClient(CachedEvidence):
    def request(self, method, path, payload=None, **_kwargs):
        if path == "/v1/snapshot":
            return 200, {"status": {"service": "fixture", "collector": {"state": "running"}},
                         "catalog": [], "metrics": {}}
        if path in ("/v1/maintenance", "/v1/diagnostics/dtcs"):
            return 200, {"available": False, "detail": "Preview fixture"}
        return super().request(method, path)


class FixtureRunner:
    failed = False

    def run(self, context, turns, cancelled, diagnostics=None, progress=None):
        diagnostics = diagnostics if diagnostics is not None else {}
        diagnostics.update(phase="waiting_for_model", outcome="running", elapsed_ms=100,
            milestones_ms={"process_started_ms": 10, "thread_started_ms": 70, "turn_started_ms": 100})
        if progress:
            progress(json.loads(json.dumps(diagnostics)))
        if cancelled.wait(1):
            diagnostics.update(outcome="cancelled", elapsed_ms=1000)
            raise ChatError(409, "Stopped")
        question = turns[-1]["user"]
        if question == "trigger fixture timeout":
            diagnostics.update(outcome="timeout", phase="starting", elapsed_ms=300000,
                milestones_ms={"process_started_ms": 10, "process_exit_ms": 300000},
                child_cpu_ms=90000, cgroup_throttled_ms=196000)
            raise ChatError(504, "Codex took too long during local startup. You can retry this question")
        diagnostics.update(outcome="complete", phase="finishing", elapsed_ms=1000, child_cpu_ms=10)
        diagnostics["milestones_ms"].update(first_model_item_ms=500, turn_completed_ms=990, process_exit_ms=1000)
        if context.get("event_kind") == "setup":
            from test_custom_warnings import RULE
            return json.dumps({"explanation": "What would you like to monitor?" if len(turns) == 1 else "Proposed low-voltage warning in volts; review before approval.",
                               "proposal": None if len(turns) == 1 else RULE})
        if question == "trigger fixture error" and not self.failed:
            self.failed = True
            raise ChatError(503, "Fixture failure; retry is safe")
        return f"Test answer {len(turns)}: {question}\nThis is a saved advisory, not proof of a current fault."


class FixtureDtcController:
    state = "idle"
    tokenless = None

    def status(self):
        return {"available": True, "state": self.state, "job": {
            "job_id": "dtc-web-fixture", "test_tokenless": self.tokenless}}

    def start(self, token=None):
        self.state = "queued"
        self.tokenless = token is None
        return self.status()

    def cancel(self):
        self.state = "cancelled"
        return self.status()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dtc-ui", action="store_true", help="enable in-memory fake DTC queue; never CAN")
    args = parser.parse_args()
    done = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: done.set())
    with tempfile.TemporaryDirectory(prefix="warning-chat-preview-") as tmp:
        client = FixtureClient()
        manager = WarningChatManager(Path(tmp) / "state", client, FixtureRunner())
        manager.token = "a" * 43  # Fixture-only credential, never the live worker code.
        socket = str(Path(tmp) / "advisor.sock")
        advisor = ChatServer(socket, ChatHandler)
        advisor.manager = manager
        web = TelemetryWebServer(("127.0.0.1", 0), socket_path="/unused.sock", allow_acquisitions=False,
            stream_interval_seconds=1, stream_max_seconds=15, warning_chat_socket=socket)
        web.telemetry_client = client
        web.RequestHandlerClass = QuietWebHandler
        if args.dtc_ui:
            web.dtc_controller = FixtureDtcController()
            web.dtc_trusted_origin = f"http://127.0.0.1:{web.server_port}"
        for server in (advisor, web):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"http://127.0.0.1:{web.server_port}", flush=True)
        done.wait()
        for server in (web, advisor):
            server.shutdown()
            server.server_close()
        manager.close()


if __name__ == "__main__":
    main()
