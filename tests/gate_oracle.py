#!/usr/bin/env python3
"""CLI safety-gate oracle for every ``--execute`` / ``--confirm-*`` tool.

Usage::

    python tests/gate_oracle.py REPO_ROOT OUTPUT.json
    python tests/gate_oracle.py --compare OLD.json NEW.json

Run it from one checkout against the old and new trees of a refactor, then
compare the two outputs; ``--compare`` exits 1 and names every changed tool and
scenario. Give both trees the same ``tmp/`` layout (create ``tmp/ecu_mapping``
in each), because one AlfaOBD error message names the first missing directory.
Write outputs under ``tmp/``. Use a development machine, not the live vanpi.

The oracle imports the tools from REPO_ROOT in-process, after installing the
guards in ``gate_oracle_guards``: ``socket.socket``/``isotp.socket``, every
``lib.uds`` transport opener, every ``acquire_*`` route/lock entry point, and
``subprocess`` for ``ip``/``candump``/``cansend``/``sudo``/``systemctl``/``adb``
all raise ``ReachedCanBoundary``.  Any other process start or any filesystem
write outside a private temporary run directory raises ``BlockedSideEffect``.
Reaching ``ReachedCanBoundary`` therefore means "every CLI gate passed".

For every tool scenario it records the parser surface, the dry-run default,
``--execute`` with every subset of the ``--confirm-*`` flags (or, above six
flags, the empty set, the full set, and every single omission), one omission
per required value option, and a few explicit edge runs.  The JSON output is
normalised (repository/scratch paths, PIDs, timestamps) and deterministic.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import itertools
import json
import os
import pathlib
import posixpath
import re
import shutil
import signal
import stat
import sys
import tempfile

ORACLE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, ORACLE)
import gate_oracle_guards as oc  # noqa: E402

SCHEMA = "gate-oracle/1"
MAX_FULL_ENUMERATION = 6
PAIR = ["--pair", "6/14"]
COND = ["--conditions", "oracle conditions"]
SCOPE_FLAG_NAMES = {
    "--all-29bit-targets", "--address-byte-range", "--full-range", "--did", "--session",
    "--include-supported", "--include-vin", "--profile", "--target", "--recover-partials",
    "--probe", "--tx-padding", "--bus", "--dids", "--max-requests", "--seconds",
}
VALUE_GATE_NAMES = {
    "--pair", "--conditions", "--require-mount", "--out-root", "--state-path",
    "--passive-capture-campaign", "--campaign", "--campaign-id", "--job-id", "--job-root",
}
EMULATED_BOOT_ID = "oracle-boot-id"


# ---------------------------------------------------------------------------
# scenario specification
# ---------------------------------------------------------------------------
def plan_path(name: str) -> str:
    return "{REPO}/projects/ecu_mapping/configs/" + name


def _live_data_entry(session):
    def entry(argv):
        live = sys.modules["live_data.live_data"]
        modules = sys.modules["lib.modules"]
        metrics = [live.Metric(0x0845, "oracle angle", live.s32, 1e-6, "deg")]
        return live.run(modules.get("radar_acc"), metrics, argv=argv, session=session)

    return entry


def _queue_dtc_web_request(run_dir: str) -> None:
    dtc_web = importlib.import_module("lib.dtc_web")
    runtime = os.path.join(run_dir, "runtime")
    os.mkdir(runtime, 0o700)
    os.chmod(runtime, 0o700)
    os.mkdir(os.path.join(runtime, "cancel"), 0o700)
    dtc_web.queue_request(
        os.path.join(runtime, "dtc-batch.request.json"),
        dtc_web.build_request("oracle-job"),
    )


def _make_runtime_dir(run_dir: str) -> None:
    runtime = os.path.join(run_dir, "runtime")
    os.mkdir(runtime, 0o700)
    os.chmod(runtime, 0o700)


def _make_campaign_dir(run_dir: str) -> None:
    os.makedirs(os.path.join(run_dir, "mnt", "out", "oracle-campaign"))


def _scalar_pinned_fixture(run_dir: str) -> None:
    """Build the repository test-suite's internally consistent pinned scalar plan.

    The tracked scalar plans pin machine output under the (absent) repository
    ``tmp/`` tree, so they fail in ``load_plan`` before any CLI confirmation is
    evaluated.  The test fixture writes a pinned catalog/report/state/scalar set
    below ``TMP_ROOT`` (redirected to ``<RUN>/repo-tmp``) so the confirmation
    order itself becomes observable.
    """
    import importlib.util

    repo = sys.modules["lib"].__path__[0].rsplit(os.sep, 1)[0]
    path = os.path.join(repo, "tests", "test_alfaobd_plots_scalar_campaign.py")
    name = "oracle_fixture_scalar_campaign_tests"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    scalar = sys.modules["tools.alfaobd_plots_scalar_campaign"]
    root = pathlib.Path(scalar.TMP_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True)
    module._PinnedFixture(root)


DTC_BATCH_PATHS = [
    "--job-id", "oracle-job",
    "--job-root", "{RUN}/jobs",
    "--report-root", "{RUN}/reports",
    "--db", "{RUN}/dtc.sqlite",
    "--cache-out", "{RUN}/cache.json",
    "--socket", "{RUN}/broker.sock",
]
WEB_REQUEST_ARGS = [
    "--request-file", "{RUN}/runtime/dtc-batch.request.json",
    "--current-file", "{RUN}/runtime/dtc-batch-current.json",
    "--cancel-dir", "{RUN}/runtime/cancel",
    "--job-root", "{RUN}/jobs",
]


def S(name, argv, *, value_gates=(), setup=None, entry=None, note=None):
    return {
        "name": name,
        "argv": list(argv),
        "value_gates": list(value_gates),
        "setup": setup,
        "entry": entry,
        "note": note,
    }


TOOLS = [
    {
        "tool": "tools/did_sweep.py",
        "module": "tools.did_sweep",
        "scenarios": [
            S("bounded_range", ["radar_acc", "0800", "0801", *PAIR, *COND], value_gates=["--pair", "--conditions"]),
            S("explicit_did_list", ["radar_acc", "--did", "F187", "--did", "F1A5", *PAIR, *COND], value_gates=["--pair", "--conditions"]),
            S("explicit_session_03", ["radar_acc", "0800", "0801", "--session", "03", *PAIR, *COND], value_gates=["--pair", "--conditions"]),
            S("full_range_flag", ["radar_acc", "--full-range", *PAIR, *COND]),
            S("legacy_start_only", ["radar_acc", "0800", *PAIR, *COND]),
            S("implicit_full_range", ["radar_acc", *PAIR, *COND]),
            S("pcm_legacy_session_92", ["pcm", "0100", "0101", "--session", "92", *PAIR, *COND]),
        ],
        "extra_runs": [
            ("legacy_session_92_on_non_pcm", ["radar_acc", "0800", "0801", "--session", "92", *PAIR, *COND, "--execute", "--confirm-parked", "--confirm-session-change"]),
            ("session_with_rate_below_keepalive", ["radar_acc", "0800", "0801", "--session", "03", "--rate", "0.1", *PAIR, *COND, "--execute", "--confirm-parked", "--confirm-session-change"]),
            ("did_combined_with_range", ["radar_acc", "0800", "--did", "F187", *PAIR, *COND, "--execute", "--confirm-parked"]),
            ("unknown_module", ["no_such_module", "0800", "0801"]),
            ("session_suppress_bit_refused", ["radar_acc", "0800", "0801", "--session", "83"]),
        ],
    },
    {
        "tool": "tools/routine_scan.py",
        "module": "tools.routine_scan",
        "scenarios": [
            S("default_range", ["radar_acc", *PAIR, *COND], value_gates=["--pair", "--conditions"]),
            S("bounded_range", ["radar_acc", "0200", "0203", *PAIR, *COND], value_gates=["--pair", "--conditions"]),
            S("explicit_session_03", ["radar_acc", "0200", "0203", "--session", "03", *PAIR, *COND], value_gates=["--pair", "--conditions"]),
            S("wide_range", ["radar_acc", "0000", "FFFF", *PAIR, *COND]),
        ],
        "extra_runs": [
            ("session_with_rate_below_keepalive", ["radar_acc", "0200", "0203", "--session", "03", "--rate", "0.1", *PAIR, *COND, "--execute", "--confirm-parked", "--confirm-session-change", "--confirm-no-active-routine"]),
            ("retries_out_of_range", ["radar_acc", "0200", "0203", "--retries", "3", *PAIR, *COND, "--execute", "--confirm-parked"]),
            ("reversed_range", ["radar_acc", "0203", "0200"]),
        ],
    },
    {
        "tool": "tools/ecu_discover.py",
        "module": "tools.ecu_discover",
        "scenarios": [
            S("ccan_verified_profile", [*PAIR, *COND], value_gates=["--pair", "--conditions"]),
            S("all_29bit_targets", ["--all-29bit-targets", *PAIR, *COND], value_gates=["--pair", "--conditions"]),
            S("address_byte_range", ["--address-byte-range", "F2", "FF", *PAIR, *COND]),
            S("bcan_catalog_profile", ["--profile", "promaster88-bcan", "--pair", "3/11", *COND], value_gates=["--pair", "--conditions"]),
            S("custom_target", ["--target", "probe=18DA10F1:18DAF110", *PAIR, *COND], value_gates=["--pair", "--conditions"]),
            S("custom_target_session_92", ["--target", "pcm=18DA10F1:18DAF110", "--probe", "legacy-1a87", "--session", "92", "--tx-padding", "00", *PAIR, *COND], value_gates=["--pair", "--conditions"]),
        ],
        "extra_runs": [
            ("bcan_profile_wrong_pair", ["--profile", "promaster88-bcan", *PAIR, *COND, "--execute", "--confirm-parked", "--confirm-catalog-candidates"]),
            ("legacy_channel_override", ["--channel", "can0", *PAIR, *COND, "--execute", "--confirm-parked"]),
            ("session_without_custom_target", ["--session", "03", "--probe", "legacy-1a87", *PAIR, *COND, "--execute", "--confirm-parked", "--confirm-session-change"]),
            ("functional_11bit_target_refused", ["--target", "f=7DF:7E8", "--addressing-mode", "normal_11bits", "--bitrate", "500000"]),
            ("custom_only_option_without_target", ["--bitrate", "125000"]),
            ("two_custom_targets", ["--target", "a=18DA10F1:18DAF110", "--target", "b=18DA11F1:18DAF111", *PAIR, *COND, "--execute", "--confirm-parked", "--confirm-custom-physical"]),
        ],
    },
    {
        "tool": "tools/uds_send.py",
        "module": "tools.uds_send",
        "scenarios": [
            S("read_22_F1A5", ["radar_acc", "22", "F1", "A5", *PAIR, *COND], value_gates=["--pair", "--conditions"]),
            S("routine_results_31_03", ["radar_acc", "31", "03", "02", "51", *PAIR, *COND]),
            S("session_change_10_03", ["radar_acc", "10", "03", *PAIR, *COND], value_gates=["--pair", "--conditions"]),
            S("tester_present_3E_00", ["radar_acc", "3E", "00", *PAIR, *COND]),
            S("write_2E", ["radar_acc", "2E", "F1", "90", "00", *PAIR, *COND], value_gates=["--pair", "--conditions"]),
            S("routine_start_31_01", ["radar_acc", "31", "01", "02", "51", *PAIR, *COND]),
            S("unclassified_sid_AA", ["radar_acc", "AA", *PAIR, *COND]),
        ],
        "extra_runs": [
            ("timeout_out_of_range", ["radar_acc", "22", "F1", "A5", "--timeout", "11", *PAIR, *COND, "--execute", "--confirm-parked"]),
            ("malformed_payload_byte", ["radar_acc", "2", "F1"]),
        ],
    },
    {
        "tool": "tools/dtc_inventory.py",
        "module": "tools.dtc_inventory",
        "scenarios": [
            S("default_requests", ["radar_acc", *PAIR, *COND], value_gates=["--pair", "--conditions"]),
            S("include_supported", ["radar_acc", "--include-supported", *PAIR, *COND]),
        ],
        "extra_runs": [],
    },
    {
        "tool": "tools/identity_inventory.py",
        "module": "tools.identity_inventory",
        "scenarios": [
            S("default_dids", ["radar_acc", *PAIR, *COND], value_gates=["--pair", "--conditions"]),
            S("include_vin", ["radar_acc", "--include-vin", *PAIR, *COND]),
        ],
        "extra_runs": [],
    },
    {
        "tool": "tools/dtc_batch.py",
        "module": "tools.dtc_batch",
        "scenarios": [
            S("single_module_tcm", ["tcm", *DTC_BATCH_PATHS]),
            S("all_registered_modules", [*DTC_BATCH_PATHS]),
            S("bcan_bus_only", ["--bus", "b-can", *DTC_BATCH_PATHS]),
            S("pcm_only_unsupported", ["pcm", *DTC_BATCH_PATHS]),
        ],
        "extra_runs": [
            ("json_plan", ["tcm", "--json", *DTC_BATCH_PATHS]),
            ("rate_above_policy", ["tcm", "--rate", "2", *DTC_BATCH_PATHS, "--execute", "--confirm-parked", "--confirm-park-gear", "--confirm-ignition-on-engine-off"]),
        ],
    },
    {
        "tool": "tools/dtc_batch_request.py",
        "module": "tools.dtc_batch_request",
        "scenarios": [
            S("valid_web_request", WEB_REQUEST_ARGS, setup=_queue_dtc_web_request,
              note="the queued web request is the only authorization; the wrapper injects --execute and all dtc_batch confirmations"),
            S("missing_web_request", WEB_REQUEST_ARGS, setup=_make_runtime_dir),
        ],
        "extra_runs": [],
    },
    {
        "tool": "tools/signal_correlate.py",
        "module": "tools.signal_correlate",
        "scenarios": [
            S("capture", ["capture", "radar_acc", "--seconds", "1", "-o", "{RUN}/capture.json", *PAIR, *COND], value_gates=["--pair", "--conditions"]),
        ],
        "extra_runs": [
            ("capture_seconds_out_of_range", ["capture", "radar_acc", "--seconds", "601", "-o", "{RUN}/capture.json"]),
            ("analyze_no_match", ["analyze", "{RUN}/nothing-*.json"]),
        ],
    },
    {
        "tool": "live_data/live_data.py",
        "module": "live_data.live_data",
        "argv0": "live_data.py",
        "scenarios": [
            S("session_03_wrapper", ["--seconds", "1", *PAIR, *COND], entry=_live_data_entry(0x03), value_gates=["--pair", "--conditions"],
              note="live_data.run(radar_acc, [Metric(0x0845,...)], argv=..., session=0x03)"),
            S("no_session_wrapper", ["--seconds", "1", *PAIR, *COND], entry=_live_data_entry(None), value_gates=["--pair", "--conditions"],
              note="live_data.run(..., session=None)"),
            S("no_session_wrapper_explicit_03", ["--session", "03", "--seconds", "1", *PAIR, *COND], entry=_live_data_entry(None)),
        ],
        "extra_runs": [],
    },
    {
        "tool": "tools/alfaobd_controller.py",
        "module": "tools.alfaobd_controller",
        "scenarios": [
            S("action_connect", ["action", "connect", "--failure-root", "{RUN}/failures"],
              note="SAFE_ACTIONS['connect'].diagnostic_confirmation is True"),
            S("action_disconnect", ["action", "disconnect", "--failure-root", "{RUN}/failures"],
              note="SAFE_ACTIONS['disconnect'].diagnostic_confirmation is False"),
            S("observe_ungated", ["observe", "--failure-root", "{RUN}/failures"]),
            S("campaign_status_ungated", ["campaign-status"]),
        ],
        "extra_runs": [],
    },
    {
        "tool": "tools/alfaobd_plots_catalog.py",
        "module": "tools.alfaobd_plots_catalog",
        "scenarios": [
            S("inventory_pcm", ["inventory", plan_path("alfaobd_pcm_plots_catalog.json"), "--out-root", "{RUN}/mnt/out", "--campaign-id", "oracle-campaign", *COND]),
            S("audit_ungated", ["audit", plan_path("alfaobd_pcm_plots_catalog.json")]),
            S("plan_offline", ["plan", plan_path("alfaobd_pcm_plots_catalog.json")]),
        ],
        "extra_runs": [
            ("inventory_blank_conditions", ["inventory", plan_path("alfaobd_pcm_plots_catalog.json"), "--out-root", "{RUN}/mnt/out", "--conditions", "  ", "--execute", "--confirm-read-only-navigation", "--confirm-parked", "--confirm-scan-stopped"]),
        ],
    },
    {
        "tool": "tools/alfaobd_plots_scalar_campaign.py",
        "module": "tools.alfaobd_plots_scalar_campaign",
        "scenarios": [
            S("run_pcm_scalars", ["run", plan_path("alfaobd_pcm_plots_scalars.json"), "--out-root", "{RUN}/mnt/out", "--require-mount", "{RUN}/mnt", "--passive-capture-campaign", "oracle-passive", *COND], value_gates=["--require-mount"]),
            S("plan_offline", ["plan", plan_path("alfaobd_pcm_plots_scalars.json")]),
            S("run_pinned_test_fixture", ["run", "{RUN}/repo-tmp/scalar.json", "--out-root", "{RUN}/mnt/out", "--require-mount", "{RUN}/mnt", "--passive-capture-campaign", "oracle-passive", *COND],
              setup=_scalar_pinned_fixture, value_gates=["--require-mount"],
              note="plan built by tests/test_alfaobd_plots_scalar_campaign.py::_PinnedFixture under the redirected TMP_ROOT"),
        ],
        "extra_runs": [],
    },
    {
        "tool": "tools/alfaobd_singleton_campaign.py",
        "module": "tools.alfaobd_singleton_campaign",
        "scenarios": [
            S("run_cluster_shakedown", ["run", plan_path("alfaobd_cluster_singleton_shakedown.json"), "--out-root", "{RUN}/mnt/out", "--require-mount", "{RUN}/mnt", "--campaign-id", "oracle-campaign", *COND], value_gates=["--require-mount"]),
            S("audit_ungated", ["audit", plan_path("alfaobd_cluster_singleton_shakedown.json")]),
            S("plan_offline", ["plan", plan_path("alfaobd_cluster_singleton_shakedown.json")]),
        ],
        "extra_runs": [
            ("run_blank_conditions", ["run", plan_path("alfaobd_cluster_singleton_shakedown.json"), "--out-root", "{RUN}/mnt/out", "--require-mount", "{RUN}/mnt", "--conditions", " ", "--execute", "--confirm-read-only-diagnostics", "--confirm-parked-shakedown", "--confirm-monitor-stopped"]),
            ("run_unsafe_campaign_id", ["run", plan_path("alfaobd_cluster_singleton_shakedown.json"), "--out-root", "{RUN}/mnt/out", "--require-mount", "{RUN}/mnt", "--campaign-id", "../x", *COND, "--execute", "--confirm-read-only-diagnostics", "--confirm-parked-shakedown", "--confirm-monitor-stopped"]),
        ],
    },
    {
        "tool": "tools/ignition_triggered_passive_capture.py",
        "module": "tools.ignition_triggered_passive_capture",
        "scenarios": [
            S("arm_one_drive", ["--out-root", "{RUN}/mnt/out", "--require-mount", "{RUN}/mnt", "--state-path", "{RUN}/state.json", *COND], value_gates=["--conditions"]),
        ],
        "extra_runs": [
            ("relative_state_path", ["--out-root", "{RUN}/mnt/out", "--require-mount", "{RUN}/mnt", "--state-path", "state.json", *COND, "--execute", "--confirm-passive", "--confirm-one-drive"]),
        ],
    },
    {
        "tool": "tools/passive_drive_capture.py",
        "module": "tools.passive_drive_capture",
        "out_of_scope_note": "passive capture; out of scope for the refactor but gated",
        "scenarios": [
            S("record", ["--out-root", "{RUN}/mnt/out", "--require-mount", "{RUN}/mnt", "--campaign", "oracle-campaign", *COND], value_gates=["--require-mount", "--conditions"]),
            S("recover_partials", ["--out-root", "{RUN}/mnt/out", "--require-mount", "{RUN}/mnt", "--campaign", "oracle-campaign", "--recover-partials"], setup=_make_campaign_dir, value_gates=["--require-mount", "--campaign"]),
        ],
        "extra_runs": [
            ("record_bcan_with_ccan_profile", ["--bus", "b-can", "--out-root", "{RUN}/mnt/out", "--require-mount", "{RUN}/mnt", *COND, "--execute", "--confirm-passive"]),
        ],
    },
]


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------
class Harness:
    def __init__(self, repo: str):
        self.repo = os.path.realpath(repo)
        # A private parent per run keeps concurrent runs apart; the fixed basename keeps
        # any basename-only mention of the run directory deterministic.
        self.run_parent = tempfile.mkdtemp(prefix="gate-oracle-")
        self.run_dir = os.path.join(self.run_parent, "gate-run")
        self.mount = os.path.join(self.run_dir, "mnt")
        self.tmpdir = os.path.realpath(tempfile.gettempdir())
        self.redirects: list[tuple[object, str, object, object]] = []
        self.signal_handlers = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        }
        self.captured_parsers: list[argparse.ArgumentParser] = []
        self.import_errors: dict[str, str] = {}
        self.patched: list[str] = []
        self.emulation: list[str] = []

    # -- setup ---------------------------------------------------------------
    def prepare(self) -> None:
        os.environ["COLUMNS"] = "100"
        os.environ["LINES"] = "40"
        os.environ["NO_COLOR"] = "1"
        for name in ("FORCE_COLOR", "PYTHON_COLORS", "OBD_VIN"):
            os.environ.pop(name, None)
        oc.NORM.add(self.run_dir, "<RUN>")
        oc.NORM.add(self.repo, "<REPO>")
        oc.NORM.add(ORACLE, "<ORACLE>")
        oc.NORM.add(self.tmpdir, "<TMPDIR>")
        oc.NORM.add(sys.executable, "<PYTHON>")
        self._reset_run_dir()
        oc.install_guards(self.repo, [self.run_dir, self.tmpdir])

        # Emulate the Pi's required mount for {RUN}/mnt only (os.path.ismount is
        # captured as a default argument by the capture tools at import time).
        original_ismount = posixpath.ismount
        mount = os.path.realpath(self.mount)

        def ismount(path):
            try:
                if os.path.realpath(os.fspath(path)) == mount:
                    return True
            except TypeError:
                pass
            return original_ismount(path)

        posixpath.ismount = ismount
        self.emulation.append("os.path.ismount(<RUN>/mnt) -> True (emulated writable mount)")

        sys.path.insert(0, self.repo)
        argparse_parse_args = argparse.ArgumentParser.parse_args
        harness = self

        def parse_args_hook(parser, args=None, namespace=None):
            harness.captured_parsers.append(parser)
            return argparse_parse_args(parser, args, namespace)

        argparse.ArgumentParser.parse_args = parse_args_hook

        for name in ("lib.modules", "lib.uds", "lib.diagnostic_safety", "lib.canbus",
                     "lib.can_operation_state", "lib.can_runtime_route", "lib.can_wake",
                     "lib.can_handoff", "lib.vehicle_can_roles"):
            try:
                importlib.import_module(name)
            except Exception as exc:  # pragma: no cover - refactor may remove modules
                self.import_errors[name] = f"{type(exc).__name__}: {exc}"
        self.patched = oc.patch_repo_boundaries()
        operation_state = sys.modules.get("lib.can_operation_state")
        if operation_state is not None and hasattr(operation_state, "current_boot_id"):
            operation_state.current_boot_id = lambda: EMULATED_BOOT_ID
            self.emulation.append(
                f"lib.can_operation_state.current_boot_id() -> {EMULATED_BOOT_ID!r}"
            )
        for spec in TOOLS:
            try:
                importlib.import_module(spec["module"])
            except BaseException as exc:
                self.import_errors[spec["module"]] = f"{type(exc).__name__}: {exc}"
        oc.rebind_aliases()
        self._collect_redirects()

    def _collect_redirects(self) -> None:
        tmp_root = os.path.join(self.repo, "tmp")
        target_root = os.path.join(self.run_dir, "repo-tmp")
        for module_name, module in oc.repo_modules():
            for attribute, value in sorted(vars(module).items()):
                if isinstance(value, pathlib.PurePath):
                    text = str(value)
                elif isinstance(value, str):
                    text = value
                else:
                    continue
                if not os.path.isabs(text):
                    continue
                normalised = os.path.normpath(text)
                if normalised != tmp_root and not normalised.startswith(tmp_root + os.sep):
                    continue
                relative = os.path.relpath(normalised, tmp_root)
                new_text = os.path.normpath(os.path.join(target_root, relative))
                new_value = type(value)(new_text) if isinstance(value, pathlib.PurePath) else new_text
                self.redirects.append((module, attribute, value, new_value))
                setattr(module, attribute, new_value)

    def _reset_run_dir(self) -> None:
        oc.activate(False)
        if os.path.lexists(self.run_dir):
            for root, dirs, files in os.walk(self.run_dir):
                for name in dirs:
                    try:
                        os.chmod(os.path.join(root, name), 0o700)
                    except OSError:
                        pass
            shutil.rmtree(self.run_dir)
        os.makedirs(self.mount)

    # -- one invocation --------------------------------------------------------
    def substitute(self, argv: list[str]) -> list[str]:
        return [
            item.replace("{RUN}", self.run_dir).replace("{REPO}", self.repo)
            for item in argv
        ]

    def invoke(self, spec: dict, scenario: dict | None, argv_template: list[str]) -> dict:
        self._reset_run_dir()
        argv = self.substitute(argv_template)
        if scenario and scenario.get("setup"):
            scenario["setup"](self.run_dir)
        oc.EVENTS.clear()
        self.captured_parsers.clear()
        module = sys.modules.get(spec["module"])
        entry = scenario.get("entry") if scenario else None
        argv0 = spec.get("argv0") or os.path.basename(spec["tool"])
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="\n", write_through=True)
        stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="\n", write_through=True)
        old_cwd = os.getcwd()
        old_argv = sys.argv
        outcome: dict
        try:
            os.chdir(self.run_dir)
            sys.argv = [argv0, *argv]
            if module is None:
                raise RuntimeError(f"import failed: {self.import_errors.get(spec['module'])}")
            oc.activate(True)
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                if entry is not None:
                    value = entry(list(argv))
                else:
                    value = module.main(list(argv))
            outcome = {"kind": "return", "value": oc.NORM.value(value)}
        except oc.ReachedCanBoundary as exc:
            outcome = {"kind": "ReachedCanBoundary", "boundary": exc.boundary, "detail": exc.detail}
        except oc.BlockedSideEffect as exc:
            outcome = {"kind": "BlockedSideEffect", "blocked": exc.kind, "detail": exc.detail}
        except SystemExit as exc:
            outcome = {"kind": "SystemExit", "code": oc.NORM.value(exc.code)}
        except BaseException as exc:  # noqa: BLE001 - recorded, never re-raised
            outcome = {
                "kind": "exception",
                "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
                "message": oc.NORM.text(str(exc)),
            }
        finally:
            oc.activate(False)
            os.chdir(old_cwd)
            sys.argv = old_argv
            for signum, handler in self.signal_handlers.items():
                signal.signal(signum, handler)
        out_text = oc.NORM.text(stdout.buffer.getvalue().decode("utf-8", "replace"))
        err_text = oc.NORM.text(stderr.buffer.getvalue().decode("utf-8", "replace"))
        process_exit, process_stderr_suffix = self._process_view(outcome)
        return {
            "argv": [oc.NORM.text(item) for item in argv],
            "outcome": outcome,
            "reached_can_boundary": outcome["kind"] == "ReachedCanBoundary",
            "boundary_events": [dict(item) for item in oc.EVENTS],
            "process_exit": process_exit,
            "stdout": out_text,
            "stderr": err_text + process_stderr_suffix,
            "files_left_in_run_dir": self._listing(),
        }

    @staticmethod
    def _process_view(outcome: dict) -> tuple[object, str]:
        """What ``python3 tool.py`` would exit with (``raise SystemExit(main())``)."""
        kind = outcome["kind"]
        if kind == "return":
            value = outcome["value"]
            if value is None:
                return 0, ""
            if isinstance(value, int) and not isinstance(value, bool):
                return value, ""
            return 1, f"{value}\n"
        if kind == "SystemExit":
            code = outcome["code"]
            if code is None:
                return 0, ""
            if isinstance(code, int) and not isinstance(code, bool):
                return code, ""
            return 1, f"{code}\n"
        if kind == "exception":
            return 1, f"Traceback ... {outcome['type']}: {outcome['message']}\n"
        return f"<{kind}>", ""

    def _listing(self) -> list[str]:
        entries = []
        for root, dirs, files in os.walk(self.run_dir):
            dirs.sort()
            for name in dirs:
                path = os.path.join(root, name)
                entries.append(oc.NORM.text(os.path.relpath(path, self.run_dir)) + "/")
            for name in sorted(files):
                path = os.path.join(root, name)
                entries.append(oc.NORM.text(os.path.relpath(path, self.run_dir)))
        # The emulated mount directory is created for every run.
        return [item for item in sorted(entries) if item != "mnt/"]

    # -- parser surface --------------------------------------------------------
    @staticmethod
    def _type_name(value) -> object:
        if value is None:
            return None
        return getattr(value, "__qualname__", None) or getattr(value, "__name__", None) or repr(value)

    def surface(self, parser: argparse.ArgumentParser) -> dict:
        actions = []
        subparsers = {}
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                actions.append(
                    {
                        "kind": "subparsers",
                        "dest": action.dest,
                        "required": action.required,
                        "choices": sorted(action.choices),
                    }
                )
                for name, sub in sorted(action.choices.items()):
                    subparsers[name] = self.surface(sub)
                continue
            actions.append(
                {
                    "kind": type(action).__name__,
                    "option_strings": list(action.option_strings),
                    "dest": action.dest,
                    "nargs": action.nargs,
                    "const": oc.NORM.value(action.const),
                    "default": oc.NORM.value(action.default),
                    "type": self._type_name(action.type),
                    "choices": oc.NORM.value(list(action.choices)) if action.choices is not None else None,
                    "required": action.required,
                    "metavar": oc.NORM.value(action.metavar),
                    "help": oc.NORM.text(action.help) if isinstance(action.help, str) else action.help,
                }
            )
        groups = [
            sorted(
                option
                for grouped in group._group_actions
                for option in (grouped.option_strings or [grouped.dest])
            )
            for group in parser._mutually_exclusive_groups
        ]
        return {
            "prog": oc.NORM.text(parser.prog),
            "actions": actions,
            "mutually_exclusive_groups": groups,
            "subparsers": subparsers,
        }

    @staticmethod
    def gate_parser(parser: argparse.ArgumentParser, argv: list[str]) -> argparse.ArgumentParser:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for token in argv:
                    if token in action.choices:
                        return Harness.gate_parser(action.choices[token], argv[argv.index(token) + 1:])
                    if not token.startswith("-"):
                        break
        return parser

    @staticmethod
    def maximal_valid_confirm_sets(confirm: list[str], groups: list[list[str]]) -> list[tuple[str, ...]]:
        """Largest confirm-flag sets that argparse accepts (one member per exclusive group)."""
        conflicting = [sorted(set(group) & set(confirm)) for group in groups]
        conflicting = [group for group in conflicting if len(group) > 1]
        if not conflicting:
            return [tuple(confirm)]
        excluded = {flag for group in conflicting for flag in group}
        base = [flag for flag in confirm if flag not in excluded]
        return [
            tuple(sorted({*base, *pick}))
            for pick in itertools.product(*conflicting)
        ]

    @staticmethod
    def options(parser: argparse.ArgumentParser) -> list[str]:
        return [
            option
            for action in parser._actions
            for option in action.option_strings
            if option.startswith("--")
        ]

    # -- one tool ----------------------------------------------------------------
    def run_tool(self, spec: dict) -> dict:
        record: dict = {
            "module": spec["module"],
            "import_error": self.import_errors.get(spec["module"]),
            "scenarios": {},
            "extra_runs": {},
        }
        if spec.get("out_of_scope_note"):
            record["note"] = spec["out_of_scope_note"]
        parser_surface = None
        for scenario in spec["scenarios"]:
            scenario_record: dict = {"base_argv": [oc.NORM.text(item) for item in self.substitute(scenario["argv"])]}
            if scenario.get("note"):
                scenario_record["note"] = scenario["note"]
            dry = self.invoke(spec, scenario, scenario["argv"])
            top = self.captured_parsers[0] if self.captured_parsers else None
            if top is not None:
                this_surface = self.surface(top)
                if parser_surface is None:
                    parser_surface = this_surface
                elif this_surface != parser_surface:
                    scenario_record["parser_surface_override"] = this_surface
            gate = self.gate_parser(top, self.substitute(scenario["argv"])) if top is not None else None
            options = self.options(gate) if gate is not None else []
            has_execute = "--execute" in options
            confirm = sorted(option for option in options if option.startswith("--confirm-"))
            scenario_record["gate_parser_prog"] = oc.NORM.text(gate.prog) if gate is not None else None
            scenario_record["gate_flags"] = {
                "execute": has_execute,
                "confirm": confirm,
                "scope": sorted(option for option in options if option in SCOPE_FLAG_NAMES),
                "value_gates": sorted(option for option in options if option in VALUE_GATE_NAMES),
                "mutually_exclusive_groups": (
                    self.surface(gate)["mutually_exclusive_groups"] if gate is not None else []
                ),
            }
            runs: dict[str, dict] = {
                ("dry_run_default" if (has_execute or confirm) else "base_run"): dry
            }
            if has_execute or confirm:
                maximal = self.maximal_valid_confirm_sets(
                    confirm, scenario_record["gate_flags"]["mutually_exclusive_groups"]
                )
                primary = maximal[0]
                scenario_record["maximal_valid_confirm_sets"] = [list(item) for item in maximal]
                runs["dry_run_with_all_confirms"] = self.invoke(
                    spec, scenario, [*scenario["argv"], *primary]
                )
                execute = ["--execute"] if has_execute else []
                if len(confirm) <= MAX_FULL_ENUMERATION:
                    subsets = [
                        combo
                        for size in range(len(confirm) + 1)
                        for combo in itertools.combinations(confirm, size)
                    ]
                    scenario_record["enumeration"] = "all_subsets"
                else:
                    subsets = [(), tuple(confirm)] + [
                        tuple(flag for flag in confirm if flag != omitted) for omitted in confirm
                    ]
                    for valid in maximal:
                        subsets.append(tuple(valid))
                        subsets.extend(
                            tuple(flag for flag in valid if flag != omitted) for omitted in valid
                        )
                    subsets = list(dict.fromkeys(tuple(sorted(item)) for item in subsets))
                    scenario_record["enumeration"] = (
                        "empty_full_and_single_omissions"
                        "(+maximal_valid_sets_and_their_single_omissions)"
                    )
                for combo in subsets:
                    key = "execute+{" + ",".join(combo) + "}"
                    runs[key] = self.invoke(spec, scenario, [*scenario["argv"], *execute, *combo])
                    runs[key]["confirm_present"] = list(combo)
                for gate_option in scenario.get("value_gates", []):
                    base = list(scenario["argv"])
                    if gate_option not in base:
                        continue
                    index = base.index(gate_option)
                    reduced = base[:index] + base[index + 2:]
                    key = f"execute+all_confirms-without-{gate_option}"
                    runs[key] = self.invoke(spec, scenario, [*reduced, *execute, *primary])
            scenario_record["runs"] = runs
            record["scenarios"][scenario["name"]] = scenario_record
        for name, argv in spec.get("extra_runs", []):
            record["extra_runs"][name] = self.invoke(spec, None, argv)
        record["parser_surface"] = parser_surface
        return record


def discover_gated_files(repo: str) -> dict[str, list[str]]:
    pattern = re.compile(r"""add_argument\(\s*["'](--execute|--confirm-[A-Za-z0-9-]+)["']""")
    found: dict[str, list[str]] = {}
    for directory in ("tools", "live_data"):
        root = os.path.join(repo, directory)
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                flags = sorted(set(pattern.findall(handle.read())))
            if flags:
                found[f"{directory}/{name}"] = flags
    return found


def compare(old_path: str, new_path: str) -> int:
    """Print every tool and scenario whose recorded behaviour differs."""
    with open(old_path, encoding="utf-8") as handle:
        old = json.load(handle)["tools"]
    with open(new_path, encoding="utf-8") as handle:
        new = json.load(handle)["tools"]
    changed = 0
    for tool in sorted(set(old) | set(new)):
        if tool not in old or tool not in new:
            print(f"{tool}: only in {'new' if tool in new else 'old'} output")
            changed += 1
            continue
        if old[tool] == new[tool]:
            continue
        changed += 1
        old_scenarios, new_scenarios = old[tool].get("scenarios", {}), new[tool].get("scenarios", {})
        names = [name for name in sorted(set(old_scenarios) | set(new_scenarios))
                 if old_scenarios.get(name) != new_scenarios.get(name)]
        other = [key for key in sorted(set(old[tool]) | set(new[tool]))
                 if key != "scenarios" and old[tool].get(key) != new[tool].get(key)]
        print(f"{tool}: scenarios {names or '-'}; other keys {other or '-'}")
    total = len(set(old) | set(new))
    print(f"{total - changed} of {total} tools identical", file=sys.stderr)
    return 1 if changed else 0


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--compare":
        return compare(sys.argv[2], sys.argv[3])
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    repo, output = sys.argv[1], os.path.abspath(sys.argv[2])
    harness = Harness(repo)
    discovered = discover_gated_files(harness.repo)
    harness.prepare()
    covered = {spec["tool"] for spec in TOOLS}
    result = {
        "schema": SCHEMA,
        "normalisation_tokens": ["<REPO>", "<RUN>", "<ORACLE>", "<TMPDIR>", "<PYTHON>", "<PID>", "<TS>", "<STAMP>", "<EPOCH>", "0x<ADDR>"],
        "guards": {
            "patched_boundaries": harness.patched,
            "can_subprocess_commands": sorted(oc.CAN_COMMANDS),
            "socket": ["socket.socket", "socket.socketpair", "socket.create_connection", "socket.create_server", "socket.fromfd"],
            "writes_allowed_only_under": ["<RUN>", "<TMPDIR>"],
            "environment_emulation": harness.emulation,
            "redirected_repo_tmp_constants": sorted(
                f"{module.__name__}.{attribute}: {oc.NORM.text(str(old))} -> {oc.NORM.text(str(new))}"
                for module, attribute, old, new in harness.redirects
            ),
        },
        "import_errors": dict(sorted(harness.import_errors.items())),
        "discovered_gated_files": discovered,
        "uncovered_discovered_files": sorted(set(discovered) - covered),
        "tools": {},
    }
    for spec in TOOLS:
        result["tools"][spec["tool"]] = harness.run_tool(spec)
    harness._reset_run_dir()
    shutil.rmtree(harness.run_parent)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=1, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
    total = sum(
        len(scenario["runs"]) for tool in result["tools"].values() for scenario in tool["scenarios"].values()
    ) + sum(len(tool["extra_runs"]) for tool in result["tools"].values())
    print(f"wrote {output}: {len(result['tools'])} tools, {total} runs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
