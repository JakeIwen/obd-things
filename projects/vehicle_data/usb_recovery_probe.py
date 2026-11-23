"""Perform one receive-only observation in a disposable, unprivileged process."""

import argparse
import json
import time
from dataclasses import asdict

from lib import canbus
from projects.vehicle_data import active_session_gates, ccan_powertrain
from projects.vehicle_data.usb_recovery_contract import BOARD_A_ROLES, IP_TIMEOUT_SECONDS, ProbeKind, ProbeReply


def observe(kind, channel, *, monotonic=time.monotonic, identify=canbus.identify_bus,
            snapshot=ccan_powertrain.read_broadcast_snapshot):
    # Date the entire observation, including the first counter read for identity and
    # socket setup for RPM. Never date old samples from when the parent receives them.
    started = monotonic()
    if kind == ProbeKind.IDENTITY:
        result = identify(channel, probe=0.25, subprocess_timeout=IP_TIMEOUT_SECONDS,
                          monotonic=monotonic) == BOARD_A_ROLES[0]
    elif kind == ProbeKind.RPM:
        sample = snapshot(channel, timeout=0.5, required_rpm_samples=active_session_gates.REQUIRED_RPM_SAMPLES)
        result = active_session_gates.running_snapshot(sample)
    else:
        raise ValueError('unknown probe')
    return ProbeReply(result, started, monotonic())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=list(ProbeKind))
    parser.add_argument('channel')
    args = parser.parse_args(argv)
    started = time.monotonic()
    try:
        reply = observe(ProbeKind(args.kind), args.channel)
        status = 0
    except Exception:
        # A failed observation is not a valid negative RPM reading to retry.
        reply = ProbeReply(False, started, time.monotonic())
        status = 1
    print(json.dumps(asdict(reply), allow_nan=False), flush=True)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
