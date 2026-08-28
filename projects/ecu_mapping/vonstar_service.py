#!/usr/bin/env python3
"""Vonstar: serialized vehicle access control/state over a private Unix API."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import errno
import http.server
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import stat
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from projects.ecu_mapping import rke_front_unlock as rke  # noqa: E402


SERVICE = "vonstar"
SCHEMA_VERSION = 1
DEFAULT_SOCKET = "/run/vonstar/api.sock"
DEFAULT_STATE_DIR = Path("/var/lib/vonstar")
MAX_REQUEST_BYTES = 1024
REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{8,96}")
COOLDOWN_SECONDS = 3.0
ACTIONS = {
    "lock_all": {"label": "Lock All", "validation": "mapped_capture"},
    "unlock_front": {"label": "Unlock Front", "validation": "live_verified"},
    "unlock_cargo": {"label": "Unlock Cargo", "validation": "mapped_capture"},
}
ACCESS_STATE_PATH = "/v1/access-state"


class VonstarError(RuntimeError):
    pass


class VonstarController:
    def __init__(self, *, execute=False, state_dir=DEFAULT_STATE_DIR, clock=time.monotonic):
        self.execute = bool(execute)
        self.state_dir = Path(state_dir)
        self.clock = clock
        self.lock = threading.RLock()
        self.last_started = None
        self.last_completed = None
        self.last_result = None
        self.requests = {}

    def status(self):
        with self.lock:
            return {
                "service": SERVICE,
                "schema_version": SCHEMA_VERSION,
                "available": self.execute,
                "mode": "execute" if self.execute else "plan_only",
                "busy": self.last_started is not None and self.last_completed is None,
                "actions": ACTIONS,
                "access_state": {
                    "path": ACCESS_STATE_PATH,
                    "method": "POST",
                    "wakes_bus": True,
                    "max_wakes_per_request": 1,
                },
                "cooldown_seconds": COOLDOWN_SECONDS,
                "last_result": self.last_result,
            }

    def perform(self, action, request_id):
        if action not in ACTIONS:
            raise VonstarError("unsupported action")
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise VonstarError("invalid request_id")
        with self.lock:
            if request_id in self.requests:
                operation, result = self.requests[request_id]
                if operation != f"action:{action}":
                    raise VonstarError("request_id was already used for another operation")
                return result
            if not self.execute:
                raise VonstarError("Vonstar is plan-only")
            now = self.clock()
            if self.last_started is not None and now - self.last_started < COOLDOWN_SECONDS:
                raise VonstarError("Vonstar cooldown is active")
            self.last_started = now
            self.last_completed = None
            started_at = datetime.now(timezone.utc).isoformat()
            try:
                proof = rke.execute_once(action)
                result = {
                    "ok": True,
                    "action": action,
                    "label": ACTIONS[action]["label"],
                    "request_id": request_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "send_count": proof["send_count"],
                    "counter_streak": proof["counter_streak"],
                    "payload_hex": proof["payload_hex"],
                    "restored_passive": True,
                }
            except Exception as exc:
                result = {
                    "ok": False,
                    "action": action,
                    "label": ACTIONS[action]["label"],
                    "request_id": request_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            self.last_completed = self.clock()
            self.last_result = result
            self.requests[request_id] = (f"action:{action}", result)
            if len(self.requests) > 64:
                self.requests.pop(next(iter(self.requests)))
            self._audit(result)
            return result

    def read_access_state(self, request_id):
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise VonstarError("invalid request_id")
        with self.lock:
            if request_id in self.requests:
                operation, result = self.requests[request_id]
                if operation != "access_state":
                    raise VonstarError("request_id was already used for another operation")
                return result
            if not self.execute:
                raise VonstarError("Vonstar is plan-only")
            now = self.clock()
            if self.last_started is not None and now - self.last_started < COOLDOWN_SECONDS:
                raise VonstarError("Vonstar cooldown is active")
            self.last_started = now
            self.last_completed = None
            started_at = datetime.now(timezone.utc).isoformat()
            try:
                access_state = rke.read_access_state_once()
                result = {
                    "ok": True,
                    "operation": "access_state",
                    "request_id": request_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "access_state": access_state,
                }
            except Exception as exc:
                result = {
                    "ok": False,
                    "operation": "access_state",
                    "request_id": request_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            self.last_completed = self.clock()
            self.last_result = result
            self.requests[request_id] = ("access_state", result)
            if len(self.requests) > 64:
                self.requests.pop(next(iter(self.requests)))
            self._audit(result)
            return result

    def _audit(self, result):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / "events.jsonl"
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Vonstar/1"

    def log_message(self, _format, *_args):
        return

    def _json(self, status, payload):
        raw = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path != "/v1/status":
            return self._json(404, {"ok": False, "message": "not found"})
        return self._json(200, {"ok": True, "vonstar": self.server.controller.status()})

    def do_POST(self):
        is_access_state = self.path == ACCESS_STATE_PATH
        prefix = "/v1/actions/"
        action = self.path[len(prefix):] if self.path.startswith(prefix) else ""
        if not is_access_state and (action not in ACTIONS or "/" in action):
            return self._json(404, {"ok": False, "message": "unknown action"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 1 <= length <= MAX_REQUEST_BYTES:
            return self._json(400, {"ok": False, "message": "invalid request size"})
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json(400, {"ok": False, "message": "invalid JSON"})
        if not isinstance(payload, dict) or set(payload) != {"request_id"}:
            return self._json(400, {"ok": False, "message": "body requires only request_id"})
        try:
            if is_access_state:
                result = self.server.controller.read_access_state(payload["request_id"])
            else:
                result = self.server.controller.perform(action, payload["request_id"])
        except VonstarError as exc:
            status = 429 if "cooldown" in str(exc) else 503 if "plan-only" in str(exc) else 400
            return self._json(status, {"ok": False, "message": str(exc)})
        return self._json(200 if result["ok"] else 503, result)


class Server(socketserver.UnixStreamServer):
    def __init__(self, path, controller):
        self.controller = controller
        super().__init__(path, Handler)


def prepare_socket(path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        return
    if not stat.S_ISSOCK(target.lstat().st_mode):
        raise VonstarError(f"refusing to replace non-socket {target}")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        probe.connect(str(target))
    except OSError as exc:
        if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT):
            raise
    else:
        raise VonstarError("another Vonstar server is active")
    finally:
        probe.close()
    target.unlink()


def serve(path, controller):
    prepare_socket(path)
    server = Server(path, controller)
    os.chmod(path, 0o660)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-three-fixed-actions", action="store_true")
    parser.add_argument("--confirm-access-state-wake", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.execute and not args.confirm_three_fixed_actions:
        parser.error("--execute requires --confirm-three-fixed-actions")
    if args.execute and not args.confirm_access_state_wake:
        parser.error("--execute requires --confirm-access-state-wake")
    controller = VonstarController(execute=args.execute, state_dir=args.state_dir)
    if args.check:
        print(json.dumps(controller.status(), indent=2, sort_keys=True))
        return 0
    try:
        serve(args.socket, controller)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
