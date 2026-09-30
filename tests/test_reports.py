"""Golden byte, permission and failure contracts for the B2 report writers.

Expectations are literal public-output contracts, independent of the implementation.
The tests call each tool's public writer without running live diagnostics.
"""

from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from lib import reports


SITES = {
    "alfaobd_bcm_decode": ("write_json_atomic", {"sort_keys": True, "fsync": True}),
    "alfaobd_catalog": ("main", {"sort_keys": True, "fsync": True}),
    "alfaobd_dat": ("write_json", {"ensure_ascii": False, "newline": ""}),
    "alfaobd_gauge_join": ("atomic_json", {"ensure_ascii": False}),
    "alfaobd_gauges": ("write_reports", {"ensure_ascii": False, "newline": ""}),
    "did_sweep": ("atomic_json", {"fsync": True, "cleanup": "always-best-effort"}),
    "dtc_inventory": ("write_report", {"cleanup": "always-best-effort"}),
    "ecu_discover": ("write_report", {"fsync": True, "cleanup": "always-best-effort"}),
    "identity_inventory": ("write_report", {"encoding": None, "cleanup": "none"}),
    "routine_scan": ("write_report", {"cleanup": "always-best-effort"}),
    "signal_correlate": ("_dump", {"encoding": None, "indent": None,
                                  "fsync": True, "cleanup": "none"}),
}
FIXED_TEMP = {
    "did_sweep", "dtc_inventory", "ecu_discover", "identity_inventory",
    "routine_scan", "signal_correlate",
}
# All sites receive exactly this insertion-ordered payload.
PAYLOAD = {"z": "café 雪", "float": -0.5, "nested": {"b": 1, "a": {"x": True}},
           "sections": [], "metrics": []}
ASCII = (b'{\n  "z": "caf\\u00e9 \\u96ea",\n  "float": -0.5,\n  "nested": {\n'
         b'    "b": 1,\n    "a": {\n      "x": true\n    }\n  },\n'
         b'  "sections": [],\n  "metrics": []\n}\n')
SORTED = (b'{\n  "float": -0.5,\n  "metrics": [],\n  "nested": {\n'
          b'    "a": {\n      "x": true\n    },\n    "b": 1\n  },\n'
          b'  "sections": [],\n  "z": "caf\\u00e9 \\u96ea"\n}\n')
UTF8 = (b'{\n  "z": "caf\xc3\xa9 \xe9\x9b\xaa",\n  "float": -0.5,\n  "nested": {\n'
        b'    "b": 1,\n    "a": {\n      "x": true\n    }\n  },\n'
        b'  "sections": [],\n  "metrics": []\n}\n')
COMPACT = (b'{"z": "caf\\u00e9 \\u96ea", "float": -0.5, '
           b'"nested": {"b": 1, "a": {"x": true}}, "sections": [], "metrics": []}\n')
GOLDENS = {
    "alfaobd_bcm_decode": SORTED, "alfaobd_catalog": SORTED,
    "alfaobd_dat": UTF8, "alfaobd_gauge_join": UTF8, "alfaobd_gauges": UTF8,
    "did_sweep": ASCII, "dtc_inventory": ASCII, "ecu_discover": ASCII,
    "identity_inventory": ASCII, "routine_scan": ASCII, "signal_correlate": COMPACT,
}
# Only these two public writers intentionally retain failed temporary files.
RETAIN_TEMP = {"identity_inventory", "signal_correlate"}
CSV_GOLDENS = {
    "sections.csv": (b"index,profile,profile_source,profile_marker,date,date_raw,start_line,"
                     b"header_line,end_line,first_time,last_time,sample_rows,valid_rows,"
                     b"short_rows,long_rows,metric_count,metrics\r\n"),
    "metrics.csv": (b"profile,metric,section_count,first_date,last_date,numeric_count,"
                    b"missing_count,nonnumeric_count,minimum,maximum\r\n"),
}

@contextmanager
def fixed_umask():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def invoke(site, namespace, path, payload):
    """Adapt only the call signature, without running diagnostics or parsers."""
    function = namespace[SITES[site][0]]
    if site == "alfaobd_catalog":
        args = SimpleNamespace(device_id=[1], database=None, labels=None,
                               model_code=None, output=path)
        with mock.patch.dict(namespace, {"parse_args": lambda: args,
                                        "build_export": lambda *args: payload}):
            with redirect_stdout(io.StringIO()):
                function()
    elif site == "alfaobd_gauges":
        function(path.parent, payload)
    elif site == "signal_correlate":
        function(SimpleNamespace(key="test"), [0x1234], 1.0, [], str(path),
                 metadata=payload)
    else:
        function(path, payload)


def file_result(path):
    return path.read_bytes(), path.stat().st_mode


class ReportsTests(unittest.TestCase):
    def setUp(self):
        # Even an accidental call outside these pure writers cannot open CAN.
        real_socket = socket.socket

        def guarded_socket(family=socket.AF_INET, *args, **kwargs):
            if family == getattr(socket, "AF_CAN", 29):
                raise AssertionError("CAN is forbidden in report tests")
            return real_socket(family, *args, **kwargs)

        patcher = mock.patch.object(socket, "socket", side_effect=guarded_socket)
        patcher.start()
        self.addCleanup(patcher.stop)

    def check_site(self, site):
        current = vars(importlib.import_module(f"tools.{site}"))
        with tempfile.TemporaryDirectory() as directory, fixed_umask():
            path = Path(directory) / "inventory.json"
            path.write_bytes(b"previous\n")
            path.chmod(0o664)
            invoke(site, current, path, PAYLOAD)
            mode = stat.S_IFREG | (0o644 if site in FIXED_TEMP else 0o600)
            self.assertEqual(file_result(path), (GOLDENS[site], mode))
            expected = {path.name}
            if site == "alfaobd_gauges":
                for name, contents in CSV_GOLDENS.items():
                    self.assertEqual(file_result(path.with_name(name)),
                                     (contents, stat.S_IFREG | 0o600))
                expected.update(CSV_GOLDENS)
            self.assertEqual({p.name for p in path.parent.iterdir()}, expected)

    def test_dump_failure_leftovers(self):
        for site in SITES:
            current = vars(importlib.import_module(f"tools.{site}"))
            for exception in (ValueError("invalid JSON"), OSError("disk full"),
                              KeyboardInterrupt()):
                with self.subTest(site=site, exception=type(exception).__name__), \
                     tempfile.TemporaryDirectory() as directory, fixed_umask():
                    path = Path(directory) / "inventory.json"
                    path.write_bytes(b"previous\n")
                    with mock.patch.object(json, "dump", side_effect=exception):
                        with self.assertRaises(type(exception)):
                            invoke(site, current, path, PAYLOAD)
                    expected = [(path.name, b"previous\n", stat.S_IFREG | 0o644)]
                    if site in RETAIN_TEMP:
                        expected.append((f"{path.name}.tmp-{os.getpid()}", b"",
                                         stat.S_IFREG | 0o644))
                    self.assertEqual(sorted((p.name, *file_result(p))
                                            for p in path.parent.iterdir()), expected)

    def test_partial_serialization_replace_and_fsync_leftovers(self):
        for site in SITES:
            current = vars(importlib.import_module(f"tools.{site}"))
            phases = ["serialization", "replace"]
            if SITES[site][1].get("fsync"):
                phases.append("fsync")
            for phase in phases:
                with self.subTest(site=site, phase=phase), \
                     tempfile.TemporaryDirectory() as directory, fixed_umask():
                    path = Path(directory) / "inventory.json"
                    path.write_bytes(b"previous\n")
                    if phase == "serialization":
                        payload = {"first": "written", "bad": object()}
                        context = mock.patch.object(os, "getpid", return_value=123)
                        error = TypeError
                        retained = (b'{"first": "written", "bad": ' if site == "signal_correlate"
                                    else b'{\n  "first": "written",\n  "bad": ')
                        pid = 123
                    else:
                        payload = PAYLOAD
                        context = mock.patch.object(os, phase, side_effect=OSError(phase))
                        error = OSError
                        retained = GOLDENS[site]
                        pid = os.getpid()
                    with context, self.assertRaises(error):
                        invoke(site, current, path, payload)
                    expected = [(path.name, b"previous\n", stat.S_IFREG | 0o644)]
                    if site in RETAIN_TEMP:
                        expected.append((f"{path.name}.tmp-{pid}", retained,
                                         stat.S_IFREG | 0o644))
                    self.assertEqual(sorted((p.name, *file_result(p))
                                            for p in path.parent.iterdir()), expected)

    def test_preexisting_deterministic_temporary_mode_is_preserved(self):
        for site in FIXED_TEMP:
            with self.subTest(site=site), tempfile.TemporaryDirectory() as directory, fixed_umask():
                path = Path(directory) / "inventory.json"
                temporary = Path(f"{path}.tmp-{os.getpid()}")
                temporary.write_bytes(b"interrupted report")
                temporary.chmod(0o640)
                invoke(site, vars(importlib.import_module(f"tools.{site}")), path, PAYLOAD)
                self.assertEqual(file_result(path), (GOLDENS[site], stat.S_IFREG | 0o640))
                self.assertEqual(list(path.parent.iterdir()), [path])

    def test_dtc_encoding_and_cleanup_are_the_only_intentional_change(self):
        from tools import dtc_inventory

        with tempfile.TemporaryDirectory() as directory, fixed_umask():
            path = Path(directory) / "inventory.json"
            temporary = f"{path}.tmp-{os.getpid()}"
            with mock.patch("builtins.open", wraps=open) as opened, \
                 mock.patch.object(os, "fsync") as synced:
                dtc_inventory.write_report(path, {"text": "café"})
            opened.assert_called_once_with(temporary, "w", encoding="utf-8", newline=None)
            synced.assert_not_called()  # no extra durability change
            expected = (b'{\n  "text": "caf\\u00e9"\n}\n', stat.S_IFREG | 0o644)
            self.assertEqual(file_result(path), expected)
            for target in ("dump", "replace"):
                module = json if target == "dump" else os
                for exception in (OSError("failed"), KeyboardInterrupt()):
                    with mock.patch.object(module, target, side_effect=exception):
                        with self.assertRaises(type(exception)):
                            dtc_inventory.write_report(path, {"a": 1})
                    self.assertEqual(list(path.parent.iterdir()), [path])
                    self.assertEqual(file_result(path), expected)

    def test_replace_failure_preserves_destination(self):
        for cleanup in ("failure", "always-best-effort", "none"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "report.json"
                temporary = str(path) + ".tmp"
                path.write_bytes(b"previous")
                with mock.patch.object(os, "replace", side_effect=OSError("replace failed")):
                    with self.assertRaisesRegex(OSError, "replace failed"):
                        reports.atomic_json(path, {"a": 1}, temporary=temporary,
                                            cleanup=cleanup)
                self.assertEqual(path.read_bytes(), b"previous")
                self.assertEqual(Path(temporary).exists(), cleanup == "none")

    def test_cleanup_error_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            for cleanup, error in (("failure", "unlink denied"),
                                   ("always-best-effort", "replace denied")):
                with mock.patch.object(os, "replace", side_effect=OSError("replace denied")), \
                     mock.patch.object(os, "unlink", side_effect=OSError("unlink denied")):
                    with self.assertRaisesRegex(OSError, error):
                        reports.atomic_json(path, {}, temporary=str(path) + ".tmp",
                                            cleanup=cleanup)

    def test_fsync_precedes_replace_and_is_optional(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            for durable in (False, True):
                events = []
                real_replace = os.replace

                def replace(src, dst):
                    events.append("replace")
                    real_replace(src, dst)

                with mock.patch.object(os, "fsync", side_effect=lambda fd: events.append("fsync")), \
                     mock.patch.object(os, "replace", side_effect=replace):
                    reports.atomic_json(path, {}, fsync=durable)
                self.assertEqual(events, ["fsync", "replace"] if durable else ["replace"])

    def test_temporary_naming_and_open_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            temporary = f"{path}.tmp-{os.getpid()}"
            with mock.patch("builtins.open", wraps=open) as opened:
                reports.atomic_json(path, {}, temporary=temporary, encoding=None)
            opened.assert_called_once_with(temporary, "w", encoding=None, newline=None)
            real_replace = os.replace
            with mock.patch.object(os, "replace", wraps=real_replace) as replaced:
                reports.atomic_json(path, {}, prefix=f".{path.name}.")
            source = Path(replaced.call_args.args[0])
            self.assertEqual(source.parent, path.parent)
            self.assertTrue(source.name.startswith(f".{path.name}."))
            with mock.patch.object(tempfile, "template", "custom-prefix-"), \
                 mock.patch.object(os, "replace", wraps=real_replace) as replaced:
                reports.atomic_json(path, {})
            self.assertTrue(Path(replaced.call_args.args[0]).name.startswith("custom-prefix-"))

    def test_helper_does_not_create_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                reports.atomic_json(Path(directory) / "missing" / "report.json", {})


# Separate unittest tests make each public writer contract visible in both runners.
for _site in SITES:
    def _test(self, site=_site):
        self.check_site(site)
    _test.__name__ = f"test_bytes_and_modes_{_site}"
    setattr(ReportsTests, _test.__name__, _test)


if __name__ == "__main__":
    unittest.main()
