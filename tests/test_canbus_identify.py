"""identify_bus() signature regressions from the 2026-09-24 drive."""

from unittest import mock

from lib import canbus


def identify(ids, rxd=0):
    with mock.patch.object(canbus, "probe_ids", return_value=(set(ids), rxd)):
        return canbus.identify_bus("can0", probe=0.1)


def test_ccan_carrying_forwarded_can_ch_requests_is_still_ccan():
    # The in-vehicle F1 scan puts 18DA28F1-style requests (and 18DAF2xx copies) on C-CAN.
    assert identify({0x100, 0x0EE, 0x18DA28F1, 0x18DAF228}) == "c-can"


def test_can_ch_response_is_decisive():
    assert identify({0x100, 0x18DAF128}) == "can-ch"


def test_bcan_with_0x41a_is_bcan():
    # B-CAN carries 0x41A at about 1 Hz while driving.
    assert identify({0x41A, 0x46C, 0x3E0}) == "b-can"
