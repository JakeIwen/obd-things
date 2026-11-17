"""Run one model CLI that prints JSON lines, inside hard bounds, and always reap it.

Used by the research worker for every research CLI launch. The child gets its own session (so its
whole process group can be signalled), its prompt on stdin, and nothing else from this process.
Stdout must be JSON objects, one per line: anything else, a line over ``max_line`` bytes or more
than ``max_total`` bytes in all fails closed (``protocol_error``). Stderr is kept only as a short
rolling buffer for classification. Every exit path ends with the process group gone and reaped.
"""

from dataclasses import dataclass, field
import json
import os
import selectors
import signal
import subprocess
import time

GRACE_SECONDS = 3
STDERR_KEEP = 8192
TICK_SECONDS = 0.5


class StopRun(Exception):
    """Raised by a callback to stop the child; ``immediate`` skips the SIGTERM grace."""

    def __init__(self, reason, detail="", immediate=False):
        super().__init__(detail or reason)
        self.reason, self.detail, self.immediate = reason, detail, immediate


@dataclass
class RunResult:
    returncode: int | None
    stop_reason: str | None = None
    stop_detail: str = ""
    stderr_tail: str = ""
    stdout_bytes: int = 0
    lines: int = 0
    milestones_ms: dict = field(default_factory=dict)


def run(argv, *, cwd, env, stdin_bytes, deadline, cancelled, on_line, on_tick=None, on_spawn=None,
        max_line=4 * 1024 * 1024, max_total=16 * 1024 * 1024, tick_seconds=TICK_SECONDS, keep_stdin_open=False):
    """Run ``argv`` until it exits, a callback raises ``StopRun``, ``cancelled`` is set or ``deadline``
    (a ``time.monotonic()`` value) passes. ``keep_stdin_open`` leaves stdin open after the prompt (a
    control-protocol exchange); the caller then ends the run from ``on_line``. An attempt already
    cancelled when this is called never spawns (``returncode`` stays ``None``)."""
    started = time.monotonic()
    result = RunResult(returncode=None)
    if cancelled.is_set():
        result.stop_reason = "cancelled"
        return result
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True, close_fds=True)
    if on_spawn:
        on_spawn(proc.pid)
    selector = selectors.DefaultSelector()
    try:
        _pump(proc, selector, result, stdin_bytes, deadline, cancelled, on_line, on_tick,
              max_line, max_total, tick_seconds, keep_stdin_open)
        result.returncode = proc.returncode
    except StopRun as stop:
        result.stop_reason, result.stop_detail = stop.reason, stop.detail
        _kill_group(proc, immediate=stop.immediate)
    finally:
        selector.close()
        if proc.poll() is None:
            _kill_group(proc, immediate=False)
        # Descendants left in the child's group (helpers, daemons) never outlive the attempt.
        _signal_group(proc.pid, signal.SIGKILL)
        result.returncode = proc.returncode
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream and not stream.closed:
                stream.close()
        result.milestones_ms["exit"] = round((time.monotonic() - started) * 1000)
    return result


def _pump(proc, selector, result, stdin_bytes, deadline, cancelled, on_line, on_tick,
          max_line, max_total, tick_seconds, keep_stdin_open):
    for stream, tag in ((proc.stdout, "out"), (proc.stderr, "err")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, tag)
    os.set_blocking(proc.stdin.fileno(), False)
    selector.register(proc.stdin, selectors.EVENT_WRITE, "in")
    pending, buffer, next_tick = memoryview(stdin_bytes), b"", time.monotonic()
    while selector.get_map() or proc.poll() is None:
        now = time.monotonic()
        if cancelled.is_set():
            raise StopRun("cancelled")
        if now >= deadline:
            raise StopRun("timeout")
        if on_tick and now >= next_tick:
            on_tick()
            next_tick = now + tick_seconds
        if not selector.get_map():
            cancelled.wait(.05)
            continue
        for key, _ in selector.select(min(.2, tick_seconds)):
            if key.data == "in":
                pending = _feed(key, selector, pending, keep_stdin_open)
                continue
            chunk = os.read(key.fd, 65536)
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            if key.data == "err":
                result.stderr_tail = (result.stderr_tail + chunk.decode(errors="replace"))[-STDERR_KEEP:]
                continue
            result.stdout_bytes += len(chunk)
            if result.stdout_bytes > max_total:
                raise StopRun("protocol_error", "the CLI printed more than the output limit")
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                _line(line, result, on_line)
            if len(buffer) > max_line:
                raise StopRun("protocol_error", "the CLI printed an over-long line")
    if buffer.strip():
        _line(buffer, result, on_line)


def _feed(key, selector, pending, keep_open):
    try:
        count = os.write(key.fd, pending[:65536])
        pending = pending[count:]
    except BrokenPipeError:
        pending = pending[:0]
    if not pending:
        selector.unregister(key.fileobj)
        if not keep_open:
            key.fileobj.close()
    return pending


def _line(raw, result, on_line):
    if not raw.strip():
        return
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise StopRun("protocol_error", "the CLI printed something that is not JSON") from None
    if not isinstance(event, dict):
        raise StopRun("protocol_error", "the CLI printed a JSON value that is not an object")
    result.lines += 1
    on_line(event)


def _signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _kill_group(proc, *, immediate):
    """SIGTERM the group, wait ``GRACE_SECONDS``, then SIGKILL; or SIGKILL at once. Always reaps."""
    steps = ((signal.SIGKILL, 5),) if immediate else ((signal.SIGTERM, GRACE_SECONDS), (signal.SIGKILL, 5))
    for sig, wait in steps:
        _signal_group(proc.pid, sig)
        try:
            proc.wait(timeout=wait)
            break
        except subprocess.TimeoutExpired:
            continue
    else:
        proc.wait()
