"""The advisor's client for the research worker's Unix socket (fixed routes, short timeouts).

A transport failure raises ``WorkerUnreachable``; callers never read it as "idle". Launch requests
are sent at most once per attempt id (the send-once rule in docs/dtc-research.md): this client has
no retry.
"""

from projects.vehicle_data.api_client import TelemetryClient
from projects.vehicle_data.api_credits_contract import PROTOCOL
from projects.vehicle_data.dtc_research_contract import WORKER

TIMEOUT = 5


class WorkerUnreachable(Exception):
    """``sent`` is False only when the request certainly never reached the worker (no socket, or the
    connection was refused); otherwise it may or may not have arrived."""

    def __init__(self, message, sent=True):
        super().__init__(message)
        self.sent = sent


class WorkerClient:
    def __init__(self, socket_path=WORKER["socket"], timeout=TIMEOUT, client_factory=TelemetryClient):
        self._client = client_factory(socket_path, timeout=timeout)

    def _call(self, method, path, payload=None):
        try:
            return self._client.request(method, path, payload)
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise WorkerUnreachable(str(exc), sent=False) from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkerUnreachable(str(exc)) from exc

    def status(self):
        status, body = self._call("GET", "/status")
        if status != 200:
            raise WorkerUnreachable(f"worker status answered {status}")
        return body

    def credits(self):
        """The worker's credit ledger view (never the key); an old worker's 404 reads as unreachable."""
        status, body = self._call("GET", PROTOCOL["workerCreditsRoute"])
        if status != 200:
            raise WorkerUnreachable(f"worker credits answered {status}")
        return body

    def launch(self, route, body):
        """``(status, reply)`` for ``/turns`` or ``/discover``: 202 admitted, 409 refused."""
        return self._call("POST", route, body)

    def attempt(self, attempt_id):
        status, body = self._call("GET", f"/attempts/{attempt_id}")
        if status != 200:
            raise WorkerUnreachable(f"worker attempt query answered {status}")
        return body

    def acknowledge(self, attempt_id):
        self._call("POST", f"/attempts/{attempt_id}/ack", {})

    def cancel(self, attempt_id):
        self._call("POST", f"/attempts/{attempt_id}/cancel", {})

    def fence(self, attempt_id):
        """The attempt's state if the worker knows it, else ``{known: false, fenced: true}`` once the
        worker has recorded that a later launch with this id is refused."""
        status, body = self._call("POST", f"/attempts/{attempt_id}/fence", {})
        if status != 200:
            raise WorkerUnreachable(f"worker fence answered {status}")
        return body

    def sweep(self, keep):
        return self._call("POST", "/sweep", {"keep": sorted(keep)})

    def delete_run(self, run_id):
        return self._call("POST", f"/runs/{run_id}/delete", {})
