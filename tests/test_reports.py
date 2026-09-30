"""Byte, permission and failure contracts for the B2 report writers.

The frozen definitions are excerpts from 390404e, not another implementation of
our helper. Each site is tested against both its helper profile and its public
wrapper. These tests require neither that commit nor a Git checkout at runtime.
"""

from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import csv
import datetime
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
from tests._reports_legacy import WRITERS


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
PREFIXED_TEMP = {"alfaobd_bcm_decode", "alfaobd_catalog"}


@contextmanager
def fixed_umask():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def legacy_namespace(site):
    namespace = {
        "os": os, "json": json, "tempfile": tempfile, "Path": Path,
        "datetime": datetime, "csv": csv,
    }
    exec(compile("from __future__ import annotations\n" + WRITERS[site],
                 f"<390404e:{site}>", "exec"), namespace)
    return namespace


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


def helper_write(site, path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    options = dict(SITES[site][1])
    if site in FIXED_TEMP:
        options["temporary"] = f"{path}.tmp-{os.getpid()}"
    elif site in PREFIXED_TEMP:
        options["prefix"] = f".{path.name}."
    reports.atomic_json(path, payload, **options)


def file_result(path):
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


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
        old = legacy_namespace(site)
        current = vars(importlib.import_module(f"tools.{site}"))
        # Empty containers, Unicode, ordering, escaping, JSON scalar types,
        # nested structures and non-finite floats exercise dump's defaults.
        payloads = [
            {"sections": [], "metrics": []},
            {"z": "café 雪\n\\\"", "a": [None, True, False, 12, -0.5],
             "sections": [], "metrics": [], "nested": {"b": 1, "a": 2}},
            {"float": [float("nan"), float("inf"), -0.0],
             "sections": [], "metrics": []},
        ]
        with tempfile.TemporaryDirectory() as directory, fixed_umask():
            root = Path(directory)
            for payload in payloads:
                paths = [root / name / "inventory.json" for name in ("old", "helper", "new")]
                for path in paths:
                    path.parent.mkdir(exist_ok=True)
                    # Replacing a 0664 destination must use the temporary's
                    # mode, rather than accidentally preserve the old mode.
                    path.write_text("previous\n")
                    path.chmod(0o664)
                invoke(site, old, paths[0], payload)
                helper_write(site, paths[1], payload)
                invoke(site, current, paths[2], payload)
                expected = file_result(paths[0])
                self.assertEqual(expected[1], 0o644 if site in FIXED_TEMP else 0o600)
                self.assertEqual(expected, file_result(paths[1]))
                self.assertEqual(expected, file_result(paths[2]))
                self.assertTrue(expected[0].endswith(b"\n"))
                for path in paths:
                    self.assertEqual(sorted(p.name for p in path.parent.iterdir()
                                            if p.suffix != ".csv"), [path.name])
                if site == "alfaobd_gauges":
                    for name in ("sections.csv", "metrics.csv"):
                        self.assertEqual(file_result(paths[0].with_name(name)),
                                         file_result(paths[2].with_name(name)))

    def test_failure_profiles_match_original_writers(self):
        for site in SITES:
            if site == "dtc_inventory":
                continue  # its intentional cleanup change is tested separately
            current = vars(importlib.import_module(f"tools.{site}"))
            for exception in (ValueError("invalid JSON"), OSError("disk full"),
                              KeyboardInterrupt()):
                with tempfile.TemporaryDirectory() as directory:
                    paths = [Path(directory) / name / "inventory.json"
                             for name in ("old", "new")]
                    states = []
                    for path, namespace in zip(paths, (legacy_namespace(site), current)):
                        path.parent.mkdir()
                        path.write_bytes(b"previous\n")
                        with mock.patch.object(json, "dump", side_effect=exception):
                            with self.assertRaises(type(exception)):
                                invoke(site, namespace, path, {"sections": [], "metrics": []})
                        states.append((path.read_bytes(), sorted(
                            (p.name, p.read_bytes()) for p in path.parent.iterdir())))
                    self.assertEqual(states[0], states[1], site)

    def test_dtc_encoding_and_cleanup_are_the_only_intentional_change(self):
        from tools import dtc_inventory

        with tempfile.TemporaryDirectory() as directory, fixed_umask():
            path = Path(directory) / "inventory.json"
            temporary = f"{path}.tmp-{os.getpid()}"
            old = legacy_namespace("dtc_inventory")
            with mock.patch("builtins.open", wraps=open) as opened:
                invoke("dtc_inventory", old, path, {"text": "café"})
            opened.assert_called_once_with(temporary, "w")
            before = file_result(path)
            with mock.patch("builtins.open", wraps=open) as opened, \
                 mock.patch.object(os, "fsync") as synced:
                dtc_inventory.write_report(path, {"text": "café"})
            opened.assert_called_once_with(temporary, "w", encoding="utf-8", newline=None)
            synced.assert_not_called()  # no extra durability change
            self.assertEqual(file_result(path), before)
            self.assertEqual(before[1], 0o644)
            for target in ("dump", "replace"):
                module = json if target == "dump" else os
                for exception in (OSError("failed"), KeyboardInterrupt()):
                    with mock.patch.object(module, target, side_effect=exception):
                        with self.assertRaises(type(exception)):
                            invoke("dtc_inventory", old, path, {"a": 1})
                    self.assertTrue(Path(temporary).exists())
                    Path(temporary).unlink()
                    with mock.patch.object(module, target, side_effect=exception):
                        with self.assertRaises(type(exception)):
                            dtc_inventory.write_report(path, {"a": 1})
                    self.assertFalse(Path(temporary).exists())
                    self.assertEqual(file_result(path), before)

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

    def test_helper_does_not_create_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                reports.atomic_json(Path(directory) / "missing" / "report.json", {})


# Separate unittest tests make per-site equivalence visible in both runners.
for _site in SITES:
    def _test(self, site=_site):
        self.check_site(site)
    _test.__name__ = f"test_bytes_and_modes_{_site}"
    setattr(ReportsTests, _test.__name__, _test)


if __name__ == "__main__":
    unittest.main()
