import unittest
from types import SimpleNamespace

from projects.vehicle_data.broker import TelemetryBroker


class FakeAcquirer:
    channel = "can7"

    def status_snapshot(self):
        return {
            "channel": "can7",
            "adapter_present": True,
            "up": True,
            "fd_enabled": False,
            "one_shot": False,
            "listen_only": True,
            "controller_state": "ERROR-ACTIVE",
            "topology": {"bus": "c-can", "usable": True},
            "active_inhibits": [],
            "role_interfaces": {"generation": "g1", "roles": {}},
        }


class FakeMonitor:
    def __init__(self):
        self.producer_instance = "usb-monitor:fixture"
        self.boot_id = "boot-fixture"
        self.started = 0
        self.closed = 0
        self.reconciled = []
        self.acked = []

    def start(self):
        self.started += 1

    def close(self):
        self.closed += 1

    def reconcile(self, status):
        self.reconciled.append(status)
        return ()

    def status_snapshot(self):
        return {
            "enabled": True,
            "state": "running",
            "producer_instance": self.producer_instance,
            "boot_id": self.boot_id,
            "receive_only": True,
            "hardware_actions": False,
            "active_count": 0,
        }

    def persistence_batch(self):
        return {
            "schema_version": 1,
            "source": "kernel_kobject_uevent",
            "producer_instance": self.producer_instance,
            "boot_id": self.boot_id,
            "events": [
                {
                    "event_id": "event-a",
                    "kind": "usb_can_adapter_added",
                }
            ],
            "incidents": [],
            "dropped_event_count": 0,
        }

    def acknowledge_events(self, event_ids):
        self.acked.append(tuple(event_ids))
        return len(event_ids)


class FakeInsights:
    def __init__(self):
        self.broker = None
        self.snapshots = []
        self.closed = 0

    def ingest_snapshot(self, snapshot, *, captured_at, ingest_key):
        self.snapshots.append((snapshot, captured_at, ingest_key))
        self.broker._history_stop.set()
        return SimpleNamespace(
            duplicate=False,
            captured_at=captured_at.isoformat(),
            advisory_checkpoint_complete=True,
            advisory_consumed_event_ids=(),
            advisory_checkpoint_error=None,
        )

    def close(self):
        self.closed += 1


class FailingInsights(FakeInsights):
    def ingest_snapshot(self, snapshot, *, captured_at, ingest_key):
        self.snapshots.append((snapshot, captured_at, ingest_key))
        self.broker._history_stop.set()
        raise RuntimeError("fixture historian failure")


class IncompleteAdvisoryInsights(FakeInsights):
    def ingest_snapshot(self, snapshot, *, captured_at, ingest_key):
        self.snapshots.append((snapshot, captured_at, ingest_key))
        self.broker._history_stop.set()
        return SimpleNamespace(
            duplicate=False,
            captured_at=captured_at.isoformat(),
            advisory_checkpoint_complete=False,
            advisory_checkpoint_error="fixture advisory failure",
        )


class FutureRemovalMonitor(FakeMonitor):
    def persistence_batch(self):
        return {
            "schema_version": 1,
            "source": "kernel_kobject_uevent",
            "producer_instance": self.producer_instance,
            "boot_id": self.boot_id,
            "events": [
                {
                    "event_id": "future-removal",
                    "kind": "usb_can_adapter_removed",
                }
            ],
            "incidents": [],
            "dropped_event_count": 0,
        }


class UsbCanBrokerIntegrationTests(unittest.TestCase):
    def test_status_and_interface_refresh_include_receive_only_monitor(self):
        monitor = FakeMonitor()
        broker = TelemetryBroker(
            acquirer=FakeAcquirer(),
            usb_can_monitor=monitor,
        )
        broker.start_usb_monitor()
        broker._refresh_interface_status()
        status = broker.status_response()
        self.assertEqual(monitor.started, 1)
        self.assertEqual(len(monitor.reconciled), 1)
        self.assertTrue(status["usb_can_monitor"]["receive_only"])
        self.assertFalse(status["usb_can_monitor"]["hardware_actions"])
        broker.close()
        self.assertEqual(monitor.closed, 1)

    def test_history_acknowledges_only_after_successful_ingest(self):
        monitor = FakeMonitor()
        insights = FakeInsights()
        broker = TelemetryBroker(
            acquirer=FakeAcquirer(),
            usb_can_monitor=monitor,
            insights=insights,
            history_interval_seconds=0.01,
        )
        insights.broker = broker
        broker._history_stop.clear()
        broker._history_loop()
        self.assertEqual(len(insights.snapshots), 1)
        stored = insights.snapshots[0][0]
        self.assertEqual(
            stored["_usb_can_monitor"]["events"][0]["event_id"],
            "event-a",
        )
        self.assertEqual(monitor.acked, [("event-a",)])
        self.assertEqual(
            broker.status_response()["history_recorder"]["snapshots_stored"],
            1,
        )
        broker.close()

    def test_history_failure_leaves_kernel_edges_pending(self):
        monitor = FakeMonitor()
        insights = FailingInsights()
        broker = TelemetryBroker(
            acquirer=FakeAcquirer(),
            usb_can_monitor=monitor,
            insights=insights,
            history_interval_seconds=0.01,
        )
        insights.broker = broker
        broker._history_stop.clear()
        broker._history_loop()
        self.assertEqual(monitor.acked, [])
        recorder = broker.status_response()["history_recorder"]
        self.assertEqual(recorder["state"], "stopped")
        self.assertIn("fixture historian failure", recorder["last_error"])
        broker.close()

    def test_incomplete_advisory_checkpoint_leaves_kernel_edges_pending(self):
        monitor = FakeMonitor()
        insights = IncompleteAdvisoryInsights()
        broker = TelemetryBroker(
            acquirer=FakeAcquirer(),
            usb_can_monitor=monitor,
            insights=insights,
            history_interval_seconds=0.01,
        )
        insights.broker = broker
        broker._history_stop.clear()
        broker._history_loop()

        self.assertEqual(monitor.acked, [])
        recorder = broker.status_response()["history_recorder"]
        self.assertEqual(recorder["state"], "stopped")
        self.assertEqual(recorder["last_error"], "fixture advisory failure")
        broker.close()

    def test_complete_checkpoint_does_not_ack_unconsumed_removal(self):
        monitor = FutureRemovalMonitor()
        insights = FakeInsights()
        broker = TelemetryBroker(
            acquirer=FakeAcquirer(),
            usb_can_monitor=monitor,
            insights=insights,
            history_interval_seconds=0.01,
        )
        insights.broker = broker
        broker._history_stop.clear()
        broker._history_loop()

        self.assertEqual(monitor.acked, [])
        broker.close()


if __name__ == "__main__":
    unittest.main()
