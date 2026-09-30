"""CLI gate contracts and old-baseline live-path call sequences.

The harness below also runs unchanged against 390404e. Expected call lists are
literal recordings from that revision, not an implementation of the old tools.
All sockets, subprocesses and safety-core functions fail closed unless explicitly
faked here. Only scratch-directory report I/O is permitted.
"""

from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
import importlib
import inspect
import io
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


class ForbiddenBoundary(BaseException):
    pass


def record_live_calls(site):
    """Exercise the real main, query loops, report writes and cleanup with fakes."""
    from lib import can_operation_state, can_runtime_route, canbus, diagnostic_safety, uds
    from lib.modules import Module

    calls = []
    sock = SimpleNamespace()
    route = SimpleNamespace()
    manager = object()

    def normalize(value):
        if value is sock:
            return "<socket>"
        if value is route:
            return "<route>"
        if value is manager:
            return "<manager>"
        if isinstance(value, Module):
            return ("Module", asdict(value))
        if isinstance(value, bytes):
            return ("bytes", value.hex())
        if callable(value):
            return f"{value.__module__}.{value.__name__}"
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return tuple(normalize(item) for item in value)
        return value

    def record(name, *args, **kwargs):
        calls.append((name, normalize(args), normalize(kwargs)))

    def forbidden(*args, **kwargs):
        raise ForbiddenBoundary("unexpected real boundary in CLI sequence test")

    def close():
        record("socket.close")

    sock.close = close

    def acquire(module, **kwargs):
        record("can_runtime_route.acquire_armed_module_route", module, **kwargs)
        route.module = replace(module, channel="can9")
        route.channel = "can9"
        route.pair = "6/14"
        route.topology_fingerprint = "fake-topology"
        return SimpleNamespace(route=route, manager=manager, release=release)

    def release():
        record("ownership.release")
        return True

    @contextmanager
    def termination():
        record("diagnostic_safety.interrupt_on_termination")
        record("termination.__enter__")
        yield SimpleNamespace(received_signal=None,
                              begin_cleanup=lambda: record("termination.begin_cleanup"))
        record("termination.__exit__")

    def request(socket_arg, payload, **kwargs):
        record("uds.request", socket_arg, payload, **kwargs)
        responses = {
            b"\x19\x01\xff": b"\x59\x01\xff\x01\x00\x00",
            b"\x19\x02\xff": b"\x59\x02\xff",
            b"\x19\x03": b"\x59\x03",
            b"\x19\x0a": b"\x59\x0a\xff",
        }
        reply = responses.get(payload, bytes((payload[0] + 0x40,)) + payload[1:])
        return reply, "POSITIVE"

    def open_socket(*args, **kwargs):
        record("uds.open_module_socket", *args, **kwargs)
        return sock

    def wrap(name, fn):
        def call(*args, **kwargs):
            record(name, *args, **kwargs)
            return fn(*args, **kwargs)
        return call

    clock = [0.0]

    def monotonic():
        clock[0] += 0.01
        return clock[0]

    def sleep(seconds):
        clock[0] += seconds

    with ExitStack() as stack, tempfile.TemporaryDirectory() as directory:
        for target in ("socket.socket", "isotp.socket", "subprocess.run", "subprocess.Popen",
                       "subprocess.call", "subprocess.check_call", "subprocess.check_output",
                       "os.system"):
            stack.enter_context(mock.patch(target, side_effect=forbidden))
        allowed = {
            (can_runtime_route, "acquire_armed_module_route"): acquire,
            (can_runtime_route, "revalidate_module_route"):
                wrap("can_runtime_route.revalidate_module_route", lambda *a, **k: None),
            (diagnostic_safety, "interrupt_on_termination"): termination,
            (can_operation_state, "active_inhibits"):
                wrap("can_operation_state.active_inhibits", lambda *a, **k: []),
            (uds, "open_module_socket"): open_socket,
            (uds, "drain"): wrap("uds.drain", lambda *a, **k: None),
            (uds, "request"): request,
            (uds, "hx"): wrap("uds.hx", uds.hx),
            (uds, "negative_response_details"):
                wrap("uds.negative_response_details", uds.negative_response_details),
        }
        for module in (can_runtime_route, diagnostic_safety, can_operation_state, uds):
            for name, fn in vars(module).copy().items():
                if inspect.isfunction(fn) and fn.__module__ == module.__name__:
                    stack.enter_context(mock.patch.object(module, name,
                                        side_effect=allowed.get((module, name), forbidden)))
        tool = importlib.import_module(f"tools.{site}")
        if hasattr(tool, "REPO"):
            stack.enter_context(mock.patch.object(tool, "REPO", directory))
        stack.enter_context(mock.patch.object(tool, "preflight", side_effect=
                            wrap("preflight", lambda *a, **k: [])))
        stack.enter_context(mock.patch.object(canbus, "interface_state", side_effect=
                            wrap("canbus.interface_state", lambda channel: canbus.InterfaceState(
                                channel, True, True, 500000, False, "ERROR-ACTIVE", 0, False, False))))
        stack.enter_context(mock.patch("time.monotonic", side_effect=monotonic))
        stack.enter_context(mock.patch("time.sleep", side_effect=sleep))
        stack.enter_context(mock.patch("signal.signal"))
        argv = {
            "identity_inventory": ["radar_acc", "--did", "F187"],
            "dtc_inventory": ["radar_acc"],
            "did_sweep": ["radar_acc", "0800", "0802", "--session", "03",
                          "--confirm-session-change", "--rate", "0.5"],
            "routine_scan": ["radar_acc", "0200", "0200", "--session", "03",
                             "--confirm-session-change", "--confirm-no-active-routine"],
            "ecu_discover": ["--target", "test=18DA2AF1:18DAF12A", "--probe", "legacy-1a87",
                             "--session", "03", "--confirm-session-change",
                             "--confirm-custom-physical"],
            "uds_send": ["radar_acc", "22", "F1", "87"],
            "signal_correlate": ["capture", "radar_acc", "--dids", "0845", "--seconds", "10",
                                 "--max-requests", "2", "--confirm-session-change",
                                 "--confirm-no-active-routine"],
        }[site]
        argv += ["--execute", "--confirm-parked", "--pair", "6/14", "--conditions", "fake test"]
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            result = tool.main(argv)
        if result != 0:
            raise AssertionError((site, result, stdout.getvalue(), stderr.getvalue()))
    return calls


class ExecutionGateTests(unittest.TestCase):
    def call(self, execute, failures):
        from lib.cli_gates import execution_gate
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            result = execution_gate(execute, dry_run_message="dry plan", failures=failures)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_dry_run_never_iterates_checks(self):
        def failures():
            raise AssertionError("dry run evaluated live checks")
            yield
        self.assertEqual(self.call(False, failures()), (0, "dry plan\n", ""))

    def test_first_failure_preserves_order_and_exact_message(self):
        def failures():
            yield False, "not printed"
            yield True, "ERROR: first"
            raise AssertionError("later check evaluated")
        self.assertEqual(self.call(True, failures()), (2, "", "ERROR: first\n"))

    def test_live_success_is_silent(self):
        self.assertEqual(self.call(True, [(False, "unused")]), (None, "", ""))

    def test_empty_requirements_allow_live_path(self):
        self.assertEqual(self.call(True, []), (None, "", ""))

    def test_failure_condition_uses_truthiness_without_formatting(self):
        self.assertEqual(self.call(True, [("missing", "verbatim\nsecond line")]),
                         (2, "", "verbatim\nsecond line\n"))


# Live-path expectations are added with each tool adoption.


class DidSweepCallSequenceTests(unittest.TestCase):
    def test_live_calls_match_390404e(self):
        expected = [('can_runtime_route.acquire_armed_module_route',
          (('Module',
            {'key': 'radar_acc',
             'name': 'Bosch ACC radar (DASM / MRR1evo)',
             'txid': 416951025,
             'rxid': 417001770,
             'channel': None,
             'bus': 'c-can',
             'note': 'ACKs frames even with ignition cut mid-sweep; speed only via DID 0x1002 (no OBD PIDs behind '
                     'SGW).',
             'bitrate': 500000,
             'addressing_mode': 'normal_29bits'}),),
          {'asserted_pair': '6/14', 'prearm_check': 'lib.diagnostic_preflight.prearm_conflict_errors'}),
         ('preflight', ('can9', 500000), {}),
         ('uds.open_module_socket',
          (('Module',
            {'key': 'radar_acc',
             'name': 'Bosch ACC radar (DASM / MRR1evo)',
             'txid': 416951025,
             'rxid': 417001770,
             'channel': 'can9',
             'bus': 'c-can',
             'note': 'ACKs frames even with ignition cut mid-sweep; speed only via DID 0x1002 (no OBD PIDs behind '
                     'SGW).',
             'bitrate': 500000,
             'addressing_mode': 'normal_29bits'}),),
          {'timeout': 0.75}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '1003')), {'timeout': 0.75, 'retries': 0}),
         ('uds.hx', (('bytes', '1003'),), {}),
         ('uds.hx', (('bytes', '5003'),), {}),
         ('uds.negative_response_details', (('bytes', '5003'),), {}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '220800')), {'timeout': 0.75, 'retries': 0}),
         ('uds.hx', (('bytes', '220800'),), {}),
         ('uds.hx', (('bytes', '620800'),), {}),
         ('uds.negative_response_details', (('bytes', '620800'),), {}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '3e00')), {'timeout': 0.5, 'retries': 0}),
         ('uds.hx', (('bytes', '3e00'),), {}),
         ('uds.hx', (('bytes', '7e00'),), {}),
         ('uds.negative_response_details', (('bytes', '7e00'),), {}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '220801')), {'timeout': 0.75, 'retries': 0}),
         ('uds.hx', (('bytes', '220801'),), {}),
         ('uds.hx', (('bytes', '620801'),), {}),
         ('uds.negative_response_details', (('bytes', '620801'),), {}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '3e00')), {'timeout': 0.5, 'retries': 0}),
         ('uds.hx', (('bytes', '3e00'),), {}),
         ('uds.hx', (('bytes', '7e00'),), {}),
         ('uds.negative_response_details', (('bytes', '7e00'),), {}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '220802')), {'timeout': 0.75, 'retries': 0}),
         ('uds.hx', (('bytes', '220802'),), {}),
         ('uds.hx', (('bytes', '620802'),), {}),
         ('uds.negative_response_details', (('bytes', '620802'),), {}),
         ('socket.close', (), {}),
         ('ownership.release', (), {})]
        self.assertEqual(record_live_calls("did_sweep"), expected)


class RoutineScanCallSequenceTests(unittest.TestCase):
    def test_live_calls_match_390404e(self):
        expected = [('can_runtime_route.acquire_armed_module_route',
          (('Module',
            {'key': 'radar_acc',
             'name': 'Bosch ACC radar (DASM / MRR1evo)',
             'txid': 416951025,
             'rxid': 417001770,
             'channel': None,
             'bus': 'c-can',
             'note': 'ACKs frames even with ignition cut mid-sweep; speed only via DID 0x1002 (no OBD PIDs behind '
                     'SGW).',
             'bitrate': 500000,
             'addressing_mode': 'normal_29bits'}),),
          {'asserted_pair': '6/14', 'prearm_check': 'lib.diagnostic_preflight.prearm_conflict_errors'}),
         ('preflight', ('can9', 500000), {}),
         ('diagnostic_safety.interrupt_on_termination', (), {}),
         ('termination.__enter__', (), {}),
         ('uds.open_module_socket',
          (('Module',
            {'key': 'radar_acc',
             'name': 'Bosch ACC radar (DASM / MRR1evo)',
             'txid': 416951025,
             'rxid': 417001770,
             'channel': 'can9',
             'bus': 'c-can',
             'note': 'ACKs frames even with ignition cut mid-sweep; speed only via DID 0x1002 (no OBD PIDs behind '
                     'SGW).',
             'bitrate': 500000,
             'addressing_mode': 'normal_29bits'}),),
          {'timeout': 0.75}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '1003')), {'timeout': 0.75, 'retries': 0}),
         ('uds.hx', (('bytes', '1003'),), {}),
         ('uds.hx', (('bytes', '5003'),), {}),
         ('uds.negative_response_details', (('bytes', '5003'),), {}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '31030200')), {'timeout': 0.75, 'retries': 0}),
         ('uds.hx', (('bytes', '31030200'),), {}),
         ('uds.hx', (('bytes', '71030200'),), {}),
         ('uds.negative_response_details', (('bytes', '71030200'),), {}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '3103ff00')), {'timeout': 0.75, 'retries': 0}),
         ('uds.hx', (('bytes', '3103ff00'),), {}),
         ('uds.hx', (('bytes', '7103ff00'),), {}),
         ('uds.negative_response_details', (('bytes', '7103ff00'),), {}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '3103ff01')), {'timeout': 0.75, 'retries': 0}),
         ('uds.hx', (('bytes', '3103ff01'),), {}),
         ('uds.hx', (('bytes', '7103ff01'),), {}),
         ('uds.negative_response_details', (('bytes', '7103ff01'),), {}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '3e00')), {'timeout': 0.5, 'retries': 0}),
         ('uds.hx', (('bytes', '3e00'),), {}),
         ('uds.hx', (('bytes', '7e00'),), {}),
         ('uds.negative_response_details', (('bytes', '7e00'),), {}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '3103ff02')), {'timeout': 0.75, 'retries': 0}),
         ('uds.hx', (('bytes', '3103ff02'),), {}),
         ('uds.hx', (('bytes', '7103ff02'),), {}),
         ('uds.negative_response_details', (('bytes', '7103ff02'),), {}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '3103ff03')), {'timeout': 0.75, 'retries': 0}),
         ('uds.hx', (('bytes', '3103ff03'),), {}),
         ('uds.hx', (('bytes', '7103ff03'),), {}),
         ('uds.negative_response_details', (('bytes', '7103ff03'),), {}),
         ('termination.begin_cleanup', (), {}),
         ('socket.close', (), {}),
         ('ownership.release', (), {}),
         ('termination.__exit__', (), {})]
        self.assertEqual(record_live_calls("routine_scan"), expected)


class EcuDiscoverCallSequenceTests(unittest.TestCase):
    def test_live_calls_match_390404e(self):
        expected = [('uds.hx', (('bytes', '1a87'),), {}),
         ('can_runtime_route.acquire_armed_module_route',
          (('Module',
            {'key': 'test',
             'name': 'Custom candidate test',
             'txid': 416951025,
             'rxid': 417001770,
             'channel': None,
             'bus': 'c-can',
             'note': 'Discovery target metadata; source: operator-supplied explicit TX/RX pair',
             'bitrate': 500000,
             'addressing_mode': 'normal_29bits'}),),
          {'asserted_pair': '6/14', 'prearm_check': 'lib.diagnostic_preflight.prearm_conflict_errors'}),
         ('preflight', ('can9', 500000), {}),
         ('uds.open_module_socket',
          (('Module',
            {'key': 'test',
             'name': 'Custom candidate test',
             'txid': 416951025,
             'rxid': 417001770,
             'channel': 'can9',
             'bus': 'c-can',
             'note': 'Discovery target metadata; source: operator-supplied explicit TX/RX pair',
             'bitrate': 500000,
             'addressing_mode': 'normal_29bits'}),),
          {'timeout': 0.75, 'tx_padding': None}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '1003')), {'timeout': 0.75, 'retries': 0}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '1a87')), {'timeout': 0.75, 'retries': 0}),
         ('uds.hx', (('bytes', '1003'),), {}),
         ('uds.hx', (('bytes', '5003'),), {}),
         ('uds.negative_response_details', (('bytes', '5003'),), {}),
         ('uds.hx', (('bytes', '1a87'),), {}),
         ('uds.hx', (('bytes', '5a87'),), {}),
         ('uds.negative_response_details', (('bytes', '5a87'),), {}),
         ('socket.close', (), {}),
         ('ownership.release', (), {}),
         ('uds.hx', (('bytes', '1a87'),), {})]
        self.assertEqual(record_live_calls("ecu_discover"), expected)


class IdentityInventoryCallSequenceTests(unittest.TestCase):
    def test_live_calls_match_390404e(self):
        expected = [('can_runtime_route.acquire_armed_module_route',
          (('Module',
            {'key': 'radar_acc',
             'name': 'Bosch ACC radar (DASM / MRR1evo)',
             'txid': 416951025,
             'rxid': 417001770,
             'channel': None,
             'bus': 'c-can',
             'note': 'ACKs frames even with ignition cut mid-sweep; speed only via DID 0x1002 (no OBD PIDs behind '
                     'SGW).',
             'bitrate': 500000,
             'addressing_mode': 'normal_29bits'}),),
          {'asserted_pair': '6/14', 'prearm_check': 'lib.diagnostic_preflight.prearm_conflict_errors'}),
         ('preflight', ('can9', 500000), {}),
         ('diagnostic_safety.interrupt_on_termination', (), {}),
         ('termination.__enter__', (), {}),
         ('uds.open_module_socket',
          (('Module',
            {'key': 'radar_acc',
             'name': 'Bosch ACC radar (DASM / MRR1evo)',
             'txid': 416951025,
             'rxid': 417001770,
             'channel': 'can9',
             'bus': 'c-can',
             'note': 'ACKs frames even with ignition cut mid-sweep; speed only via DID 0x1002 (no OBD PIDs behind '
                     'SGW).',
             'bitrate': 500000,
             'addressing_mode': 'normal_29bits'}),),
          {'timeout': 0.75}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '22f187')), {'timeout': 0.75, 'retries': 0}),
         ('uds.hx', (('bytes', '62f187'),), {}),
         ('uds.hx', (('bytes', '22f187'),), {}),
         ('uds.negative_response_details', (('bytes', '62f187'),), {}),
         ('termination.begin_cleanup', (), {}),
         ('socket.close', (), {}),
         ('ownership.release', (), {}),
         ('termination.__exit__', (), {})]
        self.assertEqual(record_live_calls("identity_inventory"), expected)


class DtcInventoryCallSequenceTests(unittest.TestCase):
    def test_live_calls_match_390404e(self):
        expected = [('uds.hx', (('bytes', '1901ff'),), {}),
         ('uds.hx', (('bytes', '1902ff'),), {}),
         ('uds.hx', (('bytes', '1903'),), {}),
         ('can_runtime_route.acquire_armed_module_route',
          (('Module',
            {'key': 'radar_acc',
             'name': 'Bosch ACC radar (DASM / MRR1evo)',
             'txid': 416951025,
             'rxid': 417001770,
             'channel': None,
             'bus': 'c-can',
             'note': 'ACKs frames even with ignition cut mid-sweep; speed only via DID 0x1002 (no OBD PIDs behind '
                     'SGW).',
             'bitrate': 500000,
             'addressing_mode': 'normal_29bits'}),),
          {'asserted_pair': '6/14', 'prearm_check': 'lib.diagnostic_preflight.prearm_conflict_errors'}),
         ('can_operation_state.active_inhibits', ('can9',), {}),
         ('canbus.interface_state', ('can9',), {}),
         ('preflight', ('can9', 500000), {}),
         ('diagnostic_safety.interrupt_on_termination', (), {}),
         ('termination.__enter__', (), {}),
         ('uds.open_module_socket',
          (('Module',
            {'key': 'radar_acc',
             'name': 'Bosch ACC radar (DASM / MRR1evo)',
             'txid': 416951025,
             'rxid': 417001770,
             'channel': 'can9',
             'bus': 'c-can',
             'note': 'ACKs frames even with ignition cut mid-sweep; speed only via DID 0x1002 (no OBD PIDs behind '
                     'SGW).',
             'bitrate': 500000,
             'addressing_mode': 'normal_29bits'}),),
          {'timeout': 1.0}),
         ('can_runtime_route.revalidate_module_route', ('<route>',), {'manager': '<manager>'}),
         ('can_operation_state.active_inhibits', ('can9',), {}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '1901ff')), {'timeout': 1.0, 'retries': 0}),
         ('uds.hx', (('bytes', '1901ff'),), {}),
         ('uds.hx', (('bytes', '5901ff010000'),), {}),
         ('uds.negative_response_details', (('bytes', '5901ff010000'),), {}),
         ('uds.hx', (('bytes', '1901ff'),), {}),
         ('can_runtime_route.revalidate_module_route', ('<route>',), {'manager': '<manager>'}),
         ('can_operation_state.active_inhibits', ('can9',), {}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '1902ff')), {'timeout': 1.0, 'retries': 0}),
         ('uds.hx', (('bytes', '1902ff'),), {}),
         ('uds.hx', (('bytes', '5902ff'),), {}),
         ('uds.negative_response_details', (('bytes', '5902ff'),), {}),
         ('uds.hx', (('bytes', '1902ff'),), {}),
         ('can_runtime_route.revalidate_module_route', ('<route>',), {'manager': '<manager>'}),
         ('can_operation_state.active_inhibits', ('can9',), {}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request', ('<socket>', ('bytes', '1903')), {'timeout': 1.0, 'retries': 0}),
         ('uds.hx', (('bytes', '1903'),), {}),
         ('uds.hx', (('bytes', '5903'),), {}),
         ('uds.negative_response_details', (('bytes', '5903'),), {}),
         ('uds.hx', (('bytes', '1903'),), {}),
         ('termination.begin_cleanup', (), {}),
         ('socket.close', (), {}),
         ('ownership.release', (), {}),
         ('termination.__exit__', (), {})]
        self.assertEqual(record_live_calls("dtc_inventory"), expected)


class SignalCorrelateCallSequenceTests(unittest.TestCase):
    def test_live_calls_match_390404e(self):
        expected = [('can_runtime_route.acquire_armed_module_route',
          (('Module',
            {'key': 'radar_acc',
             'name': 'Bosch ACC radar (DASM / MRR1evo)',
             'txid': 416951025,
             'rxid': 417001770,
             'channel': None,
             'bus': 'c-can',
             'note': 'ACKs frames even with ignition cut mid-sweep; speed only via DID 0x1002 (no OBD PIDs behind '
                     'SGW).',
             'bitrate': 500000,
             'addressing_mode': 'normal_29bits'}),),
          {'asserted_pair': '6/14', 'prearm_check': 'lib.diagnostic_preflight.prearm_conflict_errors'}),
         ('preflight', ('can9', 500000), {}),
         ('diagnostic_safety.interrupt_on_termination', (), {}),
         ('termination.__enter__', (), {}),
         ('uds.open_module_socket',
          (('Module',
            {'key': 'radar_acc',
             'name': 'Bosch ACC radar (DASM / MRR1evo)',
             'txid': 416951025,
             'rxid': 417001770,
             'channel': 'can9',
             'bus': 'c-can',
             'note': 'ACKs frames even with ignition cut mid-sweep; speed only via DID 0x1002 (no OBD PIDs behind '
                     'SGW).',
             'bitrate': 500000,
             'addressing_mode': 'normal_29bits'}),),
          {'timeout': 0.75}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request',
          ('<socket>', ('bytes', '1003')),
          {'timeout': 0.75, 'retries': 0, 'response_pending_timeout': 5.0, 'max_pending_responses': 32}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request',
          ('<socket>', ('bytes', '220845')),
          {'timeout': 0.75, 'retries': 0, 'response_pending_timeout': 5.0, 'max_pending_responses': 32}),
         ('termination.begin_cleanup', (), {}),
         ('socket.close', (), {}),
         ('ownership.release', (), {}),
         ('termination.__exit__', (), {})]
        self.assertEqual(record_live_calls("signal_correlate"), expected)


class UdsSendCallSequenceTests(unittest.TestCase):
    def test_live_calls_match_390404e(self):
        expected = [('uds.hx', (('bytes', '22f187'),), {}),
         ('can_runtime_route.acquire_armed_module_route',
          (('Module',
            {'key': 'radar_acc',
             'name': 'Bosch ACC radar (DASM / MRR1evo)',
             'txid': 416951025,
             'rxid': 417001770,
             'channel': None,
             'bus': 'c-can',
             'note': 'ACKs frames even with ignition cut mid-sweep; speed only via DID 0x1002 (no OBD PIDs behind '
                     'SGW).',
             'bitrate': 500000,
             'addressing_mode': 'normal_29bits'}),),
          {'asserted_pair': '6/14', 'prearm_check': 'lib.diagnostic_preflight.prearm_conflict_errors'}),
         ('preflight', ('can9', 500000), {}),
         ('diagnostic_safety.interrupt_on_termination', (), {}),
         ('termination.__enter__', (), {}),
         ('uds.open_module_socket',
          (('Module',
            {'key': 'radar_acc',
             'name': 'Bosch ACC radar (DASM / MRR1evo)',
             'txid': 416951025,
             'rxid': 417001770,
             'channel': 'can9',
             'bus': 'c-can',
             'note': 'ACKs frames even with ignition cut mid-sweep; speed only via DID 0x1002 (no OBD PIDs behind '
                     'SGW).',
             'bitrate': 500000,
             'addressing_mode': 'normal_29bits'}),),
          {'timeout': 0.75}),
         ('uds.drain', ('<socket>',), {}),
         ('uds.request',
          ('<socket>', ('bytes', '22f187')),
          {'timeout': 0.75, 'retries': 0, 'response_pending_timeout': 5.0, 'max_pending_responses': 32}),
         ('termination.begin_cleanup', (), {}),
         ('socket.close', (), {}),
         ('ownership.release', (), {}),
         ('termination.__exit__', (), {}),
         ('uds.hx', (('bytes', '22f187'),), {}),
         ('uds.hx', (('bytes', '62f187'),), {})]
        self.assertEqual(record_live_calls("uds_send"), expected)
