"""Bound the lifetime and output of an entire USB-recovery socket observation."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from projects.vehicle_data.usb_recovery_child import kill_and_reap
from projects.vehicle_data.usb_recovery_contract import PROBE_MODULE, PROBE_REPLY_BYTES, PROBE_WALL_SECONDS, ProbeReply

REPO = Path(__file__).resolve().parents[2]


class ProbeFailed(Exception):
    def __init__(self, detail, *, unreaped_pid=None):
        super().__init__(detail)
        self.unreaped_pid = unreaped_pid


def _validated_reply(raw, spawn, now):
    if not raw.endswith(b'\n') or raw.count(b'\n') != 1 or b'\r' in raw:
        raise ProbeFailed('expected one probe reply line')
    try:
        pairs = json.loads(raw.decode('utf-8'), object_pairs_hook=tuple)
        if not isinstance(pairs, tuple) or len(dict(pairs)) != len(pairs):
            raise ValueError('not a unique-key object')
        reply = ProbeReply(**dict(pairs))
    except (TypeError, ValueError) as exc:
        raise ProbeFailed('invalid probe reply') from exc
    if (type(reply.result) is not bool or type(reply.started_monotonic) not in (int, float)
            or type(reply.finished_monotonic) not in (int, float)
            or not spawn <= reply.started_monotonic <= reply.finished_monotonic <= now):
        raise ProbeFailed('invalid probe result or time interval')
    return reply


def run_probe(kind, channel, *, popen=subprocess.Popen, monotonic=time.monotonic, timeout=PROBE_WALL_SECONDS):
    spawn = monotonic()
    child = popen([sys.executable, '-m', PROBE_MODULE, kind, channel], cwd=REPO,
                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        try:
            status = child.wait(timeout=max(0.0, spawn + timeout - monotonic()))
        except subprocess.TimeoutExpired as exc:
            reaped = kill_and_reap(child)
            raise ProbeFailed('probe timed out', unreaped_pid=None if reaped else child.pid) from exc
        now = monotonic()
        if status != 0 or now > spawn + timeout:
            raise ProbeFailed(f'probe exited with status {status} or missed its deadline')
        # A valid line fits inside the pipe. Don't accumulate output from a broken
        # child or wait for EOF from an unexpected descendant that inherited stdout.
        fd = child.stdout.fileno()
        os.set_blocking(fd, False)
        raw = os.read(fd, PROBE_REPLY_BYTES + 1)
        if len(raw) > PROBE_REPLY_BYTES or os.read(fd, 1):
            raise ProbeFailed('oversized probe reply')
        return _validated_reply(raw, spawn, now)
    finally:
        child.stdout.close()
