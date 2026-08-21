#!/usr/bin/env python3
"""Bounded parked live view of five independently discriminated cluster DIDs.

Dry-run plan:

    python3 projects/ecu_mapping/cluster_live.py

Parked live view (the shared runner resolves, owns, arms, and restores C-CAN):

    python3 projects/ecu_mapping/cluster_live.py \
        --execute --confirm-parked --confirm-engine-off --pair 6/14 \
        --conditions "parked, ignition ON, engine OFF"

The five DIDs answer in explicitly confirmed default and extended sessions on this van, and a
separate session-unchanged pass also succeeded. By default this wrapper requests no session change
and sends no TesterPresent; it sends only physical ReadDataByIdentifier requests. Those requests can
refresh the ECU's S3 timer and may therefore prolong an inherited non-default session. An explicit
``--session 03`` override remains available behind the normal session-change confirmations. This is
still active diagnostic traffic and fails closed if the selected policy does not work in the
current state. It is a parked viewer, not a drive logger.

The AlfaOBD singleton campaign strongly associates the labels below with these DIDs, but nonzero
RPM/speed scaling, the remaining gear enum, and the temperature formula are not independently
verified. Those rows therefore display raw response bytes only. Battery voltage uses AlfaOBD's
observed raw x 0.1 V rendering and is visibly qualified; it is not an independent voltmeter.
"""
import os
import sys


_root = os.path.dirname(os.path.abspath(__file__))
while _root != os.path.dirname(_root) and not os.path.isdir(os.path.join(_root, "lib")):
    _root = os.path.dirname(_root)
sys.path.insert(0, _root)

from lib.modules import MODULES
from live_data.live_data import Metric, run, u8


METRICS = [
    Metric(0x1000, "Engine speed candidate", None, 1.0, "raw"),
    Metric(0x1002, "Vehicle speed candidate", None, 1.0, "raw"),
    Metric(0x0107, "Actual gear (00=P only)", None, 1.0, "raw"),
    Metric(0x1004, "Battery +30 (Alfa scale)", u8, 0.1, "V*"),
    Metric(0x1005, "Outside temp candidate", None, 1.0, "raw"),
]


def main(argv=None):
    return run(
        MODULES["cluster"],
        METRICS,
        title="cluster live (candidate labels)",
        refresh_hz=1.0,
        argv=argv,
        session=None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
