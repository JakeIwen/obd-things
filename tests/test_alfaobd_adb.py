import json
from pathlib import Path
import tempfile
import unittest

from lib import alfaobd_adb as ui


def node(
    text="",
    *,
    resource_id="",
    package=ui.PACKAGE,
    clickable=False,
    enabled=True,
    checked=False,
    bounds="[0,0][100,40]",
):
    return (
        f'<node text="{text}" resource-id="{resource_id}" '
        f'class="android.widget.TextView" package="{package}" '
        f'clickable="{str(clickable).lower()}" '
        f'enabled="{str(enabled).lower()}" '
        f'checked="{str(checked).lower()}" bounds="{bounds}" />'
    )


def hierarchy(*nodes):
    return '<hierarchy rotation="0">' + "".join(nodes) + "</hierarchy>"


def connected(*extra):
    return hierarchy(
        node("Connect"),
        node("DISCONNECT", clickable=True),
        node("Connected to Example ECU"),
        *extra,
    )


def disconnected():
    return hierarchy(
        node("Connect"),
        node("CONNECT", clickable=True),
        node("Status: Idle"),
    )


class FakeAdb:
    def __init__(self, dumps, screenshot=b"fake-png"):
        self.dumps = list(dumps)
        self.last = self.dumps[-1] if self.dumps else hierarchy(node("unknown"))
        self.screenshot_payload = screenshot
        self.taps = []
        self.screenshot_calls = 0

    def dump_ui(self):
        if self.dumps:
            self.last = self.dumps.pop(0)
        return self.last

    def screenshot(self):
        self.screenshot_calls += 1
        return self.screenshot_payload

    def tap(self, selected):
        self.taps.append(selected)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class ClassificationTests(unittest.TestCase):
    def test_main_and_disconnected(self):
        snapshot = ui.classify_ui(
            hierarchy(
                node("My interface"),
                node("My car"),
                node("Interface not connected"),
                node("CONNECT", clickable=True),
            )
        )
        self.assertTrue(snapshot.has(ui.UiState.MAIN, ui.UiState.DISCONNECTED))
        self.assertEqual(snapshot.primary, ui.UiState.DISCONNECTED)

    def test_connected_and_intermediate(self):
        snapshot = ui.classify_ui(
            hierarchy(
                node("DISCONNECT", clickable=True),
                node("Status: Connected. Checking connected device model..."),
            )
        )
        self.assertTrue(snapshot.has(ui.UiState.CONNECTING))
        self.assertNotIn(ui.UiState.CONNECTED, snapshot.states)
        self.assertEqual(snapshot.primary, ui.UiState.CONNECTING)

    def test_disconnect_button_alone_is_not_connected(self):
        snapshot = ui.classify_ui(
            hierarchy(
                node("DISCONNECT", clickable=True),
                node("Connection in progress"),
            )
        )
        self.assertEqual(snapshot.primary, ui.UiState.CONNECTING)
        self.assertNotIn(ui.UiState.CONNECTED, snapshot.states)

    def test_adapter_and_iso_prompts(self):
        adapter = ui.classify_ui(
            hierarchy(
                node("Connect adapter", package="android"),
                node("Connect the GREY adapter", package="android"),
                node("OK", package="android", clickable=True),
            )
        )
        self.assertEqual(adapter.primary, ui.UiState.ADAPTER_PROMPT)

        iso = ui.classify_ui(
            hierarchy(
                node("ECU verification failed", package="android"),
                node("Failure to verify ISO code.", package="android"),
                node("CONTINUE", package="android", clickable=True),
            )
        )
        self.assertEqual(iso.primary, ui.UiState.ISO_WARNING)

    def test_populated_system_id(self):
        snapshot = ui.classify_ui(
            connected(
                node("System status"),
                node("READ SYSTEM ID", clickable=True),
                node("DEVICE INFO:"),
                node("Fiat drawing number: 123"),
                node("Hardware number: 456"),
                node("Software number: 789"),
                node("ISO code: 001122"),
            )
        )
        self.assertTrue(
            snapshot.has(ui.UiState.CONNECTED, ui.UiState.SYSTEM_ID_POPULATED)
        )
        self.assertEqual(snapshot.primary, ui.UiState.SYSTEM_ID_POPULATED)

    def test_populated_system_id_in_one_large_text_node(self):
        snapshot = ui.classify_ui(
            connected(
                node("System status"),
                node("READ SYSTEM ID", clickable=True),
                node(
                    "Tested device: Example&#10;"
                    "DEVICE INFO:&#10;"
                    "Fiat drawing number: 123&#10;"
                    "Hardware number: 456&#10;"
                    "Software number: 789&#10;"
                    "ISO code: 001122"
                ),
            )
        )
        self.assertEqual(snapshot.primary, ui.UiState.SYSTEM_ID_POPULATED)

    def test_faults_and_no_faults(self):
        faults = ui.classify_ui(
            connected(
                node("Faults"),
                node("READ ALL FAULTS", clickable=True),
                node("Faults found."),
                node("C1200"),
            )
        )
        self.assertEqual(faults.primary, ui.UiState.FAULTS_POPULATED)

        no_faults = ui.classify_ui(
            connected(
                node("Faults"),
                node("READ ALL FAULTS", clickable=True),
                node("No faults found"),
                node("No faults reported by the unit."),
            )
        )
        self.assertTrue(
            no_faults.has(ui.UiState.NO_FAULTS, ui.UiState.FAULTS_POPULATED)
        )
        self.assertEqual(no_faults.primary, ui.UiState.NO_FAULTS)

    def test_failure(self):
        snapshot = ui.classify_ui(hierarchy(node("Connection failed")))
        self.assertEqual(snapshot.primary, ui.UiState.FAILURE)

    def test_no_reply_dialog_is_failure(self):
        snapshot = ui.classify_ui(
            hierarchy(
                node("No reply"),
                node(
                    "No reply from interface. Please check that the key is in MAR."
                ),
                node("OK", package="android", clickable=True),
            )
        )
        self.assertEqual(snapshot.primary, ui.UiState.FAILURE)


class PollerTests(unittest.TestCase):
    def poller(self, adb, clock, root, events=None):
        return ui.UiPoller(
            adb,
            interval_seconds=0.75,
            failure_root=root,
            logger=(
                (lambda event, fields: events.append((event, fields)))
                if events is not None
                else None
            ),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    def test_returns_immediately_on_first_match(self):
        clock = FakeClock()
        adb = FakeAdb([connected()])
        with tempfile.TemporaryDirectory() as directory:
            result = self.poller(
                adb, clock, Path(directory)
            ).wait_for_states(
                operation="connect",
                expected=(ui.UiState.CONNECTED,),
                timeout_seconds=20,
            )
        self.assertEqual(result.outcome, ui.WaitOutcome.MATCHED)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(clock.sleeps, [])

    def test_polls_transient_then_matches(self):
        clock = FakeClock()
        adb = FakeAdb(
            [
                hierarchy(node("Status: Connected. Checking connected device model...")),
                connected(),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            result = self.poller(
                adb, clock, Path(directory)
            ).wait_for_states(
                operation="connect",
                expected=(ui.UiState.CONNECTED,),
                timeout_seconds=20,
            )
        self.assertEqual(result.outcome, ui.WaitOutcome.MATCHED)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(clock.sleeps, [0.75])

    def test_unexpected_iso_warning_fails_and_captures_evidence(self):
        clock = FakeClock()
        adb = FakeAdb(
            [
                hierarchy(
                    node("ECU verification failed", package="android"),
                    node("Failure to verify ISO code.", package="android"),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.poller(adb, clock, root).wait_for_states(
                operation="connect",
                expected=(ui.UiState.CONNECTED,),
                timeout_seconds=20,
                fail_on=(
                    ui.UiState.FAILURE,
                    ui.UiState.ADAPTER_PROMPT,
                    ui.UiState.ISO_WARNING,
                ),
            )
            self.assertEqual(result.outcome, ui.WaitOutcome.FAILURE)
            self.assertIsNotNone(result.evidence_prefix)
            prefix = Path(result.evidence_prefix)
            self.assertTrue(Path(f"{prefix}.xml").is_file())
            self.assertTrue(Path(f"{prefix}.png").is_file())
            metadata = json.loads(Path(f"{prefix}.json").read_text())
        self.assertEqual(metadata["primary"], "iso_warning")
        self.assertEqual(adb.screenshot_calls, 1)

    def test_timeout_is_terminal_and_captures_last_dump(self):
        clock = FakeClock()
        adb = FakeAdb([hierarchy(node("some other screen"))])
        with tempfile.TemporaryDirectory() as directory:
            result = self.poller(
                adb, clock, Path(directory)
            ).wait_for_states(
                operation="fault_read",
                expected=(ui.UiState.FAULTS_POPULATED,),
                timeout_seconds=1.5,
            )
            prefix = Path(result.evidence_prefix)
            self.assertTrue(Path(f"{prefix}.xml").is_file())
        self.assertEqual(result.outcome, ui.WaitOutcome.TIMEOUT)
        self.assertEqual(result.snapshot.primary, ui.UiState.TIMEOUT)
        self.assertIn(ui.UiState.TIMEOUT, result.snapshot.states)

    def test_dump_retries_are_bounded_and_logged(self):
        class FailingAdb(FakeAdb):
            def __init__(self):
                super().__init__([connected()])
                self.calls = 0

            def dump_ui(self):
                self.calls += 1
                if self.calls < 3:
                    raise ui.AlfaUiError("temporary dump failure")
                return super().dump_ui()

        events = []
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            result = self.poller(
                FailingAdb(), clock, Path(directory), events
            ).wait_for_states(
                operation="connect",
                expected=(ui.UiState.CONNECTED,),
                timeout_seconds=10,
            )
        self.assertEqual(result.outcome, ui.WaitOutcome.MATCHED)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(
            [event for event, _fields in events].count("ui_dump_retry"), 2
        )


class GuardedActionTests(unittest.TestCase):
    def controller(self, adb, root):
        clock = FakeClock()
        poller = ui.UiPoller(
            adb,
            interval_seconds=0.75,
            failure_root=root,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        return ui.GuardedController(adb, poller)

    def test_connect_checks_state_taps_once_and_waits(self):
        adb = FakeAdb([disconnected(), connected()])
        with tempfile.TemporaryDirectory() as directory:
            result = self.controller(adb, Path(directory)).perform(
                "connect", confirmed_read_only_diagnostics=True
            )
        self.assertEqual(result.outcome, ui.WaitOutcome.MATCHED)
        self.assertEqual(len(adb.taps), 1)
        self.assertEqual(adb.taps[0].text, "CONNECT")

    def test_duplicate_connect_is_refused_without_tap(self):
        adb = FakeAdb([connected()])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ui.AlfaUiError, "duplicate/stale"):
                self.controller(adb, Path(directory)).perform(
                    "connect", confirmed_read_only_diagnostics=True
                )
        self.assertEqual(adb.taps, [])

    def test_read_requires_explicit_diagnostic_confirmation(self):
        adb = FakeAdb([connected()])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ui.AlfaUiError, "explicit read-only"):
                self.controller(adb, Path(directory)).perform(
                    "read-faults", confirmed_read_only_diagnostics=False
                )
        self.assertEqual(adb.taps, [])

    def test_no_generic_or_write_action_exists(self):
        self.assertNotIn("tap", ui.SAFE_ACTIONS)
        self.assertNotIn("clear-faults", ui.SAFE_ACTIONS)
        self.assertNotIn("proxi-alignment", ui.SAFE_ACTIONS)
        self.assertNotIn("configuration-write", ui.SAFE_ACTIONS)


if __name__ == "__main__":
    unittest.main()
