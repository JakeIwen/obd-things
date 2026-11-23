"""Bound direct-child cleanup even when its kernel operation cannot be interrupted."""

import subprocess

REAP_SECONDS = 2.0


def kill_and_reap(child):
    try:
        child.kill()
    except ProcessLookupError:
        pass
    # A descendant may own the pipe. Wait for this PID, not for EOF.
    try:
        child.wait(timeout=REAP_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return True
