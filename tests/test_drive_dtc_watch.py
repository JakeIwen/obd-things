"""Real filter bytes, bounded drain, flags and receive-loss fail-closed behavior."""
import socket
import struct

import pytest

from projects.vehicle_data import active_drive, drive_dtc_contract as contract
from projects.vehicle_data import drive_dtc_watch as watch
from tests.drive_dtc_support import CHANNEL, Clock, WatchFeed, watch_entry


# Linux-only flag: the scripted feed also runs on macOS without SocketCAN.
MSG_CONFIRM = getattr(socket, "MSG_CONFIRM", 0x800)


def make_watch(entries=()):
    clock, feed = Clock(), WatchFeed(entries)
    sentinel = watch.ForeignTesterWatch(CHANNEL, rf_hub_profiles=active_drive.TPMS_PROFILES,
                                        socket_factory=lambda *args: feed, monotonic=clock).open()
    return clock, feed, sentinel


def own():
    return watch_entry(0x98DA2AF1, contract.REQUEST_DATA)


def foreign():
    return watch_entry(0x98DA2AF1, contract.REQUEST_DATA, flags=0)


def test_socket_filters_exact_format_and_tester_ranges():
    _clock, feed, sentinel = make_watch()
    filters = feed.options[0]
    assert filters[:2] == (101, 1)
    pairs = list(struct.iter_unpack('=II', filters[2]))
    assert all(not mask & 0x20000000 for _wanted, mask in pairs)
    def accepts(can_id):
        # Linux places ERR-masked filters on the error-only receive list.
        return any(not mask & 0x20000000 and can_id & mask == wanted & mask
                   for wanted, mask in pairs)
    for source in range(240, 256):
        assert accepts(0x98DA0000 | 0x4000 | source)
        assert accepts(0x98DB0000 | 0x3300 | source)
    for can_id in (0x7DF, *range(0x7E0, 0x7E8)):
        assert accepts(can_id)
        assert not accepts(can_id | 0x80000000)
    for can_id in (0x18DA40F1, 0x98DA40EF, 0x98DAF140, 0x98DC40F1, 0x7D0, 0x7E8):
        assert not accepts(can_id)
    assert not accepts(0x98DA40F1 | 0x40000000)
    assert not accepts(0x7DF | 0x40000000)
    # Error-flagged frames are rejected by _observe, exercised below, not
    # by adding the special ERR bit to normal kernel receive masks.
    assert (socket.SOL_SOCKET, watch.SO_RXQ_OVFL, 1) in feed.options
    assert feed.bound == (CHANNEL,) and feed.timeout == 0
    sentinel.close()
    assert feed.closed and not sentinel.clean


def test_every_existing_helper_request_is_ours_only_with_local_flag():
    _clock, feed, sentinel = make_watch()
    for can_id, data in sentinel.signatures:
        feed.entries.append(watch_entry(can_id, data))
    assert sentinel.drain()
    assert len(sentinel.signatures) == 4 + 4 + 2 + 5 + 2
    feed.entries.append(foreign())
    assert not sentinel.drain()


@pytest.mark.parametrize('entry', [
    foreign(), watch_entry(0x98DA2AF1, contract.REQUEST_DATA, flags=MSG_CONFIRM),
    watch_entry(0x98DA2AF1, b'\x03\x19\x02\xff\0\0\0\0'),
    watch_entry(0x98DA2AF2, contract.REQUEST_DATA),
    watch_entry(0x98DA2AF1, contract.REQUEST_DATA, dlc=7),
    watch_entry(0xD8DA2AF1, contract.REQUEST_DATA),
    watch_entry(0xB8DA2AF1, contract.REQUEST_DATA),
    watch_entry(0x98DA2AF1, contract.REQUEST_DATA, flags=socket.MSG_DONTROUTE | socket.MSG_TRUNC),
    watch_entry(0x98DA2AF1, contract.REQUEST_DATA, flags=socket.MSG_DONTROUTE | socket.MSG_CTRUNC),
    (b'\0', [], socket.MSG_DONTROUTE, (CHANNEL,)),
    OSError('receive lost'), ValueError('receive malformed'),
])
def test_ambiguous_or_foreign_receive_stays_dirty_for_full_quiet_period(entry):
    clock, feed, sentinel = make_watch([entry])
    assert not sentinel.drain()
    clock.value += 119.999
    feed.entries.append(own())
    assert not sentinel.drain()
    clock.value += 0.001
    assert sentinel.drain()


def test_dirty_watch_still_drains_every_cycle_and_resets_on_new_foreign():
    clock, feed, sentinel = make_watch([foreign()])
    assert not sentinel.drain()
    for _ in range(100):
        clock.value += 1
        feed.entries.extend([own()] * 6)
        assert not sentinel.drain() and not feed.entries
    feed.entries.append(foreign())
    assert not sentinel.drain()
    clock.value += 119
    assert not sentinel.drain()
    clock.value += 1
    assert sentinel.drain() and feed.reads > 600


def test_bound_stays_dirty_and_does_not_clear_continuing_queue():
    clock, feed, sentinel = make_watch([own()] * 130)
    assert not sentinel.drain() and len(feed.entries) == 66
    clock.value += 200
    assert not sentinel.drain() and len(feed.entries) == 2
    assert not sentinel.drain() and not feed.entries
    clock.value += 120
    assert sentinel.drain()


def test_overflow_counter_change_wrap_and_malformed_ancillary_are_dirty():
    clock, feed, sentinel = make_watch()
    def enqueue(counter):
        value = struct.pack('=I', counter) if isinstance(counter, int) else counter
        feed.entries.append(watch_entry(0x98DA2AF1, contract.REQUEST_DATA,
                            ancillary=[(socket.SOL_SOCKET, watch.SO_RXQ_OVFL, value)]))
    enqueue(0)
    assert sentinel.drain()
    for count in (1, 2**32 - 1, 0, b'\0'):
        enqueue(count)
        assert not sentinel.drain()
        clock.value += 120
        assert sentinel.drain()
    enqueue(0)
    assert sentinel.drain()


def test_open_failure_closes_socket_and_never_claims_clean():
    clock, feed = Clock(), WatchFeed()
    feed.fail_open = True
    sentinel = watch.ForeignTesterWatch(CHANNEL, rf_hub_profiles=active_drive.TPMS_PROFILES,
                                        socket_factory=lambda *args: feed, monotonic=clock)
    assert not sentinel.drain()
    with pytest.raises(OSError):
        sentinel.open()
    assert feed.closed and not sentinel.clean and sentinel.sock is None
