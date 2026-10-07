#!/usr/bin/env python3
"""Offline old-tree/new-tree capture oracle; contains no legacy implementation.

Run this same script against both checkouts, then compare the complete outputs::

    python tests/broker_pure_oracle.py --repo-root /path/to/old tmp/broker-old.json
    python tests/broker_pure_oracle.py --repo-root /path/to/new tmp/broker-new.json
    cmp tmp/broker-old.json tmp/broker-new.json

Only the requested checkout's broker is executed. Clocks, monitor inputs and
persistent-inhibit writes are faked; socket creation and subprocesses are
forbidden. The captures include ordered status JSON, HTTP/SSE payload injection,
helper responses/exceptions/state/inhibits, and passive vehicle-state outcomes.
This script is not collected by pytest and contains no golden implementation.

For an intentionally reviewed status-contract change, regenerate hashes first
under tmp, inspect the changed cases, then replace the pinned fixture::

    python tests/broker_pure_oracle.py --status-hashes tmp/broker-status-hashes.json
    cp tmp/broker-status-hashes.json tests/broker_status_hashes.json
"""

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import itertools
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import mock

def parse_args():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--status-hashes", action="store_true", help="write only the pinned status hashes")
    return parser.parse_args()


def forbid_hardware(event, args):
    if event in ("socket.__new__", "subprocess.Popen"):
        raise RuntimeError(f"broker pure oracle forbids {event}")


if __name__ == "__main__":
    args = parse_args()
    # Import the target checkout, not this script's checkout. This script can
    # therefore capture an older tree that does not yet contain the oracle.
    sys.path.insert(0, str(args.repo_root.resolve()))
    sys.addaudithook(forbid_hardware)


from projects.vehicle_data import broker as broker_module, models
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.models import AcquisitionResult, failure, success
from tests.test_vehicle_data import FakeAcquirer, FakeClock
from tests.can_missing_cases import availability_status_cases


NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@contextmanager
def fixed_time():
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(broker_module, "datetime", FixedDatetime))
        stack.enter_context(mock.patch.object(models, "utc_now", return_value=NOW))
        yield


def make_broker():
    route = SimpleNamespace(channel="can7", expected_usb_serial="test-b", expected_dev_id=1)
    broker = TelemetryBroker(
        acquirer=FakeAcquirer(), monotonic=FakeClock(),
        active_drive_supervisor=SimpleNamespace(), active_drive_enabled=True,
        auxiliary_drive_supervisor=route, auxiliary_drive_enabled=True,
    )
    broker._refresh_interface_status()
    broker._interface_status["role_interfaces"] = {
        "ready": True,
        "roles": {
            bus: {
                "resolution": "resolved", "reason": "ready", "channel": channel,
                "passive_ready": True,
                "expected": {"usb_serial": serial, "dev_id": dev_id, "bitrate": bitrate},
                "actual": {"present": True, "up": True, "bitrate": bitrate,
                           "fd_enabled": False, "one_shot": False, "restart_ms": 0,
                           "controller_state": "ERROR-ACTIVE", "listen_only": True},
            }
            for bus, channel, serial, dev_id, bitrate in (
                ("c-can", "can7", "test-c", 0, 500000),
                ("b-can", "can7", "test-b", 1, 125000),
            )
        },
    }
    return broker


def encode(value):
    # Deliberately do not sort keys: insertion order is part of the oracle.
    return json.dumps(value, ensure_ascii=True)


def status_cases():
    yield "initial", lambda b: None
    for state, basis, running in (
        ("asleep", "passive_bus_silence", False),
        ("awake", "passive_bus_activity", None),
        ("running", "qualified_ccan_0x0fc_engine_speed", True),
        ("ignition_on", "ccan_0x2ef_ignition_gate", None),
        ("unknown", "no_passive_observation", None),
    ):
        for age in (None, 0, 5, 5.001, 30, -1):
            def configure(b, state=state, basis=basis, running=running, age=age):
                b._vehicle_state.update(state=state, basis=basis, running=running)
                b._vehicle_state_observed_monotonic = None if age is None else 100 - age
            yield f"vehicle/{state}/{age}", configure
    for active, auxiliary in itertools.product(
        ("idle", "starting", "armed_diagnostic", "restoring", "restoration_failed"), repeat=2
    ):
        def configure(b, active=active, auxiliary=auxiliary):
            for payload, state in ((b._active_drive, active), (b._auxiliary_drive, auxiliary)):
                payload.update(
                    state=state, reason="running_gate_satisfied", helper_pid=42,
                    interface_mode="listen_only" if state == "idle" else "armed_diagnostic",
                    restoration_failed=state == "restoration_failed",
                    owner_route={"channel": "can7", "usb_serial": "test-b", "dev_id": 1},
                )
        yield f"helpers/{active}/{auxiliary}", configure
    for reason in ("ready", "interface_armed", "adapter_missing"):
        for latched, inhibited in itertools.product((False, True), repeat=2):
            def configure(b, reason=reason, latched=latched, inhibited=inhibited):
                b._auxiliary_drive.update(
                    state="armed_diagnostic", reason="running_gate_satisfied", helper_pid=42,
                    interface_mode="armed_diagnostic", restoration_failed=False,
                    owner_route={"channel": "can7", "usb_serial": "test-b", "dev_id": 1},
                )
                for role in b._interface_status["role_interfaces"]["roles"].values():
                    role["reason"] = reason
                b._auxiliary_drive_restoration_latched = latched
                b._interface_status["active_inhibits"] = ["test-inhibit"] if inhibited else []
            yield f"route/{reason}/{latched}/{inhibited}", configure
    yield "inflight", lambda b: b._inflight.update({("battery.voltage", "passive"): None})
    yield "foreign-owner", lambda b: b._last_error.update({
        "battery.voltage": failure(metric="battery.voltage", unit="V", reason="can_busy", detail="foreign owner")
    })
    for component in ("usb_can_monitor", "engine_off_voltage_capture", "display_receiver"):
        for state in ("ready", "degraded", "failed"):
            def configure(b, component=component, state=state):
                method = "status" if component == "display_receiver" else "status_snapshot"
                stub = mock.Mock()
                call = getattr(stub, method)
                if state == "failed":
                    call.side_effect = RuntimeError("test status failure")
                else:
                    call.return_value = {"state": state, "detail": "test", "nested": {"n": [1, 2]}}
                setattr(b, component, stub)
            yield f"component/{component}/{state}", configure
    for state in ("starting", "running", "failed", "stopped", "disabled"):
        def configure(b, state=state):
            b._history_recorder.update(state=state, snapshots_stored=12, last_error="test")
            b._supplemental_cache_status.update(state=state, last_error="test")
            b._collector_state = state
            b._collector_cycles = 10
            b._collector_failure_detail = "test"
        yield f"recorders/{state}", configure
    for rejected in (False, True):
        def configure(b, rejected=rejected):
            metric = "transmission.oil_temperature"
            b._cache[metric] = success(
                metric=metric, unit="°F", value=150, source="ccan.broadcast.0x1f7",
                bus="c-can", acquisition="passive", quality="verified",
                observed_at=NOW, observed_monotonic=99,
            )
            incident = {"metric": metric, "source": "ccan.broadcast.0x1f7"}
            b._recent_data_quality = [incident]
            if rejected:
                b._active_data_quality["test"] = incident
        yield f"temperature/{rejected}", configure
    yield from availability_status_cases()


def status_rows():
    rows = []
    with fixed_time():
        for label, configure in status_cases():
            broker = make_broker()
            configure(broker)
            try:
                payload = broker.status_response()
                row = {"case": label, "bytes": encode(payload)}
            except Exception as exc:
                row = {"case": label, "exception": [type(exc).__name__, str(exc)]}
            rows.append(row)
    return rows


def delivery_rows():
    """Pass real broker statuses through the unchanged HTTP/SSE injectors."""
    from projects.vehicle_data.web import TelemetryWebHandler
    from projects.vehicle_data.web_v2 import DashboardHandler

    rows = []
    for row in status_rows():
        if "bytes" not in row:
            continue
        status = json.loads(row["bytes"])
        snapshot = {"status": status, "catalog": [], "metrics": {}}

        def request(method, path, payload=None):
            return 200, deepcopy(status if path == "/v1/status" else snapshot)

        server = SimpleNamespace(
            telemetry_client=SimpleNamespace(request=request),
            warning_chat_socket=None, allow_acquisitions=True, dtc_controller=None,
            server_address=("127.0.0.1", 8765), build_id=lambda: "oracle-build",
            next_snapshot_delivery=lambda: {"instance_id": "oracle", "sequence": 1},
        )
        for kind, cls in (("web", TelemetryWebHandler), ("dashboard", DashboardHandler)):
            handler = object.__new__(cls)
            handler.server = server
            handler._json = lambda code, payload: rows.append(
                [row["case"], kind, code, encode(payload)]
            )
            if kind == "web":
                handler._broker_request("GET", "/v1/status")
                handler._broker_request("GET", "/v1/snapshot")
                event = handler._stream_event(200, deepcopy(snapshot))
            else:
                handler._snapshot()
                event = handler._stream_lite_event(200, deepcopy(snapshot))
            rows.append([row["case"], kind + "-stream", 200, encode(event)])
    return rows


def event_corpus():
    seeds = [
        {"type": "status", "state": "armed_diagnostic", "reason": "running_gate_satisfied",
         "detail": "test", "interface_mode": "armed_diagnostic", "pid": 42},
        {"type": "failure", "state": "failed", "reason": "helper_failed", "detail": "test"},
        {"type": "final", "state": "idle", "reason": "engine_not_running", "detail": "test",
         "restored": True, "interface_mode": "listen_only"},
        {"type": "final", "state": "restoration_failed", "reason": "restoration_failed",
         "detail": "test", "restored": False, "interface_mode": "armed_diagnostic"},
        {"type": "observation", "metric": "engine.rpm", "value": 751.0, "unit": "rpm",
         "source": "ccan.broadcast.0x0fc", "bus": "c-can", "quality": "observed_alfa_scale",
         "interface_mode": "armed_diagnostic"},
        {"type": "observation", "metric": "vehicle.odometer", "value": 25000.0, "unit": "mi",
         "source": "ics.did.2001", "bus": "b-can", "quality": "candidate",
         "interface_mode": "armed_diagnostic"},
        {"type": "metric_failure", "metric": "engine.crankshaft_torque", "source": "pcm.did.06da",
         "reason": "response_timeout", "detail": "test"},
        {"type": "quality_event"},
    ]
    missing = object()
    for seed in seeds:
        yield seed
        for key in ("type", "state", "reason", "detail", "interface_mode", "pid", "restored",
                    "metric", "source", "bus", "quality", "value", "unit"):
            for value in (missing, None, False, True, 0, -1, 1.5, "", "invalid", [], {}):
                event = dict(seed)
                if value is missing:
                    event.pop(key, None)
                else:
                    event[key] = value
                yield event
    for event_type, state, reason, mode, restored in itertools.product(
        ("status", "failure", "final"),
        (None, "idle", "starting", "armed_diagnostic", "restoring", "failed", "restoration_failed"),
        ("running_gate_satisfied", "restoration_failed", "helper_failed", "invalid"),
        ("listen_only", "armed_diagnostic", "unknown"),
        (None, False, True, "invalid"),
    ):
        yield dict(type=event_type, state=state, reason=reason, detail="test",
                   interface_mode=mode, restored=restored)
    for reason in sorted(broker_module.ACTIVE_DRIVE_FAILURE_REASONS):
        for event_type in ("failure", "final"):
            yield dict(type=event_type, state="idle", reason=reason, detail="test")


def event_outcome(broker, handler, event, *, inhibit_fails=False):
    with mock.patch.object(broker_module.can_operation_state, "begin_inhibit") as inhibit:
        if inhibit_fails:
            inhibit.side_effect = OSError("inhibit write failed")
        try:
            response = handler(broker, deepcopy(event))
            error = None
        except Exception as exc:
            response = None
            error = [type(exc).__name__, str(exc)]
        return {
            "response": response, "exception": error,
            "active": broker._active_drive, "auxiliary": broker._auxiliary_drive,
            "latched": [broker._active_drive_restoration_latched, broker._auxiliary_drive_restoration_latched],
            "cache": {k: v.as_dict(now_monotonic=100, stale_after_seconds=5) for k, v in broker._cache.items()},
            "errors": {k: v.as_dict(now_monotonic=100, stale_after_seconds=5) for k, v in broker._last_error.items()},
            "vehicle": broker._vehicle_state, "observed": broker._vehicle_state_observed_monotonic,
            "quality": [broker._active_data_quality, broker._recent_data_quality],
            "inhibits": [[list(call.args), call.kwargs] for call in inhibit.call_args_list],
        }


def event_rows():
    with fixed_time():
        for bus in ("active", "auxiliary"):
            method = "handle_" + bus + "_drive_event"
            for index, event in enumerate(event_corpus()):
                # Include persistent-inhibit failure and previously latched state.
                for latched in (False, True):
                    broker = make_broker()
                    broker._active_drive_restoration_latched = latched
                    broker._auxiliary_drive_restoration_latched = latched
                    outcome = event_outcome(broker, getattr(TelemetryBroker, method), event, inhibit_fails=latched)
                    yield [bus, index, latched, outcome]


def vehicle_state_rows():
    with fixed_time():
        for index, (metric, available, value, reason, bus, age) in enumerate(itertools.product(
            ("battery.voltage", "engine.rpm", "vehicle.ignition_on", "unrelated"),
            (False, True), (None, False, True, 0, 399, 400, 751, "invalid"),
            ("bus_asleep", "helper_failed"), ("c-can", "can-ch", "wrong-rate"), (None, 0, 5, 6),
        )):
            result = AcquisitionResult(
                metric=metric, available=available, unit="test", value=value,
                source="ccan.broadcast.0x0fc", reason=reason, bus=bus,
            )
            for basis in ("qualified_ccan_0x0fc_engine_speed", "ccan_0x2ef_ignition_gate", "passive_bus_activity"):
                broker = make_broker()
                broker._vehicle_state["basis"] = basis
                broker._vehicle_state_observed_monotonic = None if age is None else 100 - age
                broker._receive_silent_roles = ("c-can",) if value is None else ()
                yield [index, basis, vehicle_outcome(broker, result)]


def vehicle_outcome(broker, result):
    calls = []

    def clock():
        calls.append(broker._lock._is_owned())
        return 100.0 + len(calls) * 0.001

    broker.monotonic = clock
    try:
        response = broker._update_vehicle_state(result)
        error = None
    except Exception as exc:
        response = None
        error = [type(exc).__name__, str(exc)]
    return {
        "response": response, "exception": error,
        "vehicle": broker._vehicle_state,
        "observed": broker._vehicle_state_observed_monotonic,
        "clock_locks": calls,
    }


def vehicle_metadata_rows():
    with fixed_time():
        for index, (metric, source, acquisition, observed, monotonic, value) in enumerate(itertools.product(
            ("battery.voltage", "engine.rpm", "vehicle.ignition_on"),
            (None, "other", "ccan.broadcast.0x0fc"),
            (None, "physical_read_data_by_identifier"),
            (None, NOW), (None, 87.5), (0, True, 751, float("nan")),
        )):
            result = AcquisitionResult(
                metric=metric, available=True, unit="test", value=value,
                source=source, acquisition=acquisition,
                observed_at=observed, observed_monotonic=monotonic,
            )
            yield [index, vehicle_outcome(make_broker(), result)]


def status_hashes():
    return {row["case"]: hashlib.sha256(encode(row).encode()).hexdigest() for row in status_rows()}


def main(args):
    if args.status_hashes:
        args.output.write_text(json.dumps(status_hashes(), indent=2) + "\n")
        return
    payload = {
        "status": status_rows(),
        "events": list(event_rows()),
        "vehicle_states": list(vehicle_state_rows()),
        "vehicle_metadata": list(vehicle_metadata_rows()),
        "delivery": delivery_rows(),
    }
    args.output.write_text(encode(payload) + "\n")
    print(", ".join(f"{len(rows)} {name}" for name, rows in payload.items()))


if __name__ == "__main__":
    main(args)
