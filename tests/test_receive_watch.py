"""Deaf-adapter detection (2026-09-22 Board A incident)."""

from projects.vehicle_data.historian import TelemetryHistorian
from projects.vehicle_data.receive_watch import ReceiveSilenceWatch


def roles(ccan, bcan, canch, *, ccan_channel="can0"):
    def payload(channel, count):
        return {
            "resolution": "resolved",
            "channel": channel,
            "actual": {"rx_packets": count},
        }

    return {
        "c-can": payload(ccan_channel, ccan),
        "b-can": payload("can1", bcan),
        "can-ch": payload("can2", canch),
    }


def run(samples, *, step=6.0, watch=None):
    watch = watch or ReceiveSilenceWatch()
    findings = {}
    for index, sample in enumerate(samples):
        findings = watch.update(sample, index * step)
    return watch, findings


def test_flags_silent_ccan_and_bcan_while_can_ch_is_busy():
    samples = [roles(500, 40, 1000 + 7000 * i) for i in range(30)]  # 174 s
    watch, findings = run(samples)
    assert findings["c-can"]["receive_silent"] is True
    assert findings["b-can"]["receive_silent"] is True
    assert watch.silent_roles(findings) == ("c-can", "b-can")


def test_sleeping_van_is_not_flagged():
    samples = [roles(500, 40, 1000) for _ in range(60)]
    _, findings = run(samples)
    assert findings["c-can"]["receive_silent"] is False
    assert findings["b-can"]["receive_silent"] is False


def test_needs_two_minutes_of_evidence():
    samples = [roles(500, 40, 1000 + 7000 * i) for i in range(20)]  # 114 s
    _, findings = run(samples)
    assert findings["c-can"]["receive_silent"] is False


def test_can_ch_that_just_woke_does_not_flag_a_long_silent_ccan():
    asleep = [roles(500, 40, 1000) for _ in range(40)]
    woke = [roles(500, 40, 1000 + 7000 * i) for i in range(1, 10)]  # 54 s busy
    _, findings = run(asleep + woke)
    assert findings["c-can"]["silent_seconds"] > 120
    assert findings["c-can"]["receive_silent"] is False


def test_clears_on_first_received_frame():
    samples = [roles(500, 40, 1000 + 7000 * i) for i in range(30)]
    samples.append(roles(900, 60, 1000 + 7000 * 30))
    _, findings = run(samples)
    assert findings["c-can"]["receive_silent"] is False
    assert findings["b-can"]["receive_silent"] is False


def test_channel_change_or_counter_reset_restarts_the_baseline():
    samples = [roles(500, 40, 1000 + 7000 * i) for i in range(30)]
    samples.append(roles(0, 40, 1000 + 7000 * 30, ccan_channel="can4"))
    _, findings = run(samples)
    assert findings["c-can"]["receive_silent"] is False
    assert findings["c-can"]["silent_seconds"] == 0


def test_missing_counter_is_ignored():
    sample = roles(500, 40, 1000)
    sample["c-can"]["actual"]["rx_packets"] = None
    _, findings = run([sample, sample])
    assert "c-can" not in findings


def test_historian_marks_receive_silent_unhealthy():
    health, reason = TelemetryHistorian._interface_health(
        {
            "resolution": "resolved",
            "adapter_present": True,
            "up": True,
            "controller_state": "ERROR-ACTIVE",
            "topology": {"usable": True},
            "receive_silent": True,
        }
    )
    assert (health, reason) == ("unhealthy", "receive_silent")
