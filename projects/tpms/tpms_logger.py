#!/usr/bin/env python3
"""TPMS drive logger: poll the RF Hub and timestamp the verified physical-RL dropout
and its C1503-31 status transitions.

Polls every CYCLE_S seconds over UDS (module 'rf_hub', C-CAN via the SGW-bypass tap):
  * 31D0-31D3  per-slot pressure, raw x 0.1 kPa  (current slot->wheel map verified
                2026-07-07 by deflate/reinflate test: 1=FL, 2=FR, 3=RR, 4=RL)
  * 301E-3021  per-slot last-RX records [04 | 3-byte timestamp | age] (trigger not yet
                fully characterized -- logged raw for offline analysis)
  * 19 02 0D   DTC status: C1503-31 tracks the physical-RL dropout; C1512-88 and
                B1040-64 remain useful history. CSV keys retain each raw 3-byte DTC.

Appends CSV to tmp/tpms/tpms_drive_log.csv, publishes valid pressure observations to
the allowlisted local telemetry broker, and prints changes to stdout. Read-only UDS
(22 / 19), no writes. Survives bounded socket recovery while the engine-running and
ownership gates remain valid; otherwise it stops and restores listen-only. Ctrl-C to stop.

    python3 projects/tpms/tpms_logger.py    # guarded engine-running fallback

AUTO MODE (systemd service tpms-logger.service runs this):

    python3 projects/tpms/tpms_logger.py --auto

Auto mode first proves that the telemetry broker Unix socket is absent before inspecting CAN.
Any live broker response or uncertain status failure makes this process yield completely: no
channel lock, interface change, or UDS socket. The standalone fallback is passive-first and
engine-running-only. It requires
same-boot pins-6/14 C-CAN topology, no operation inhibit, passive C-CAN identity, and multiple
fresh 0x0FC samples above 400 rpm before taking the exclusive lock. It repeats every gate under
the lock, arms once for the session, then closes the socket and exactly restores the captured
listen-only state before unlocking. A failed restoration creates a same-boot inhibit and latches
the process against further polling.
For manual active C-CAN work, stop this fallback only if it is active and blocks
the required exclusive C-CAN lease.  It does not own or exclude B-CAN/CAN-CH.
Restart it only if it was deliberately stopped and its deployment handoff says
the effective installed unit is safe to start.
"""
import os
import csv
import errno
import sys
import time
import socket
import struct
import argparse
import datetime
from contextlib import ExitStack
from dataclasses import dataclass

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)
from lib import can_operation_state, can_runtime_route, canbus, diagnostic_safety, uds
from lib.modules import bind_channel, get
from projects.vehicle_data.api import TelemetryClient

CYCLE_S = 10
IGNITION_WINDOW_S = 2.0
AUTO_POLL_BUDGET_S = 8.0
DEFAULT_TELEMETRY_SOCKET = "/run/van-telemetry/api.sock"
TELEMETRY_TIMEOUT_S = 0.25
IDLE_SLEEP_S = 28.0
BUS_PROBE_S = 0.25
RUNNING_WINDOW_S = 0.75
RUNNING_SAMPLE_COUNT = 3
RUNNING_THRESHOLD_RPM = 400.0
CCAN_PAIR = "6/14"
ENGINE_SPEED_ID = 0x0FC
AF_CAN = getattr(socket, "AF_CAN", 29)
CAN_RAW = getattr(socket, "CAN_RAW", 1)
SOL_CAN_RAW = getattr(socket, "SOL_CAN_RAW", 101)
CAN_RAW_FILTER = getattr(socket, "CAN_RAW_FILTER", 1)
CAN_SFF_MASK = 0x7FF
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_FRAME_FLAGS = CAN_EFF_FLAG | CAN_RTR_FLAG | CAN_ERR_FLAG
CAN_FILTER_MASK = CAN_SFF_MASK | CAN_EFF_FLAG | CAN_RTR_FLAG
RESTORATION_INHIBIT_NAME = "tpms-restoration-failed"
CSV_PATH = os.path.join(REPO, "tmp", "tpms", "tpms_drive_log.csv")
# Current physical wheel names and pressure DIDs for RFH slots 1-4, verified by the
# 2026-07-07 deflate/reinflate test. Slots 3/4 are deliberately RR/RL.
PRESSURE_SLOTS = (
    ("FL", 0x31D0, "tire.pressure.fl"),
    ("FR", 0x31D1, "tire.pressure.fr"),
    ("RR", 0x31D2, "tire.pressure.rr"),
    ("RL", 0x31D3, "tire.pressure.rl"),
)
WHEELS = tuple(wheel for wheel, _did, _metric in PRESSURE_SLOTS)
PRESS_DIDS = tuple(did for _wheel, did, _metric in PRESSURE_SLOTS)
LASTRX_DIDS = (0x301E, 0x301F, 0x3020, 0x3021)
INVALID_PRESSURE_RAW = b"\xFF\xFF"
DTC_NAMES = {b"\x90\x40\x64": "B1040-64", b"\x55\x12\x88": "C1512-88",
             b"\x55\x03\x31": "C1503-31"}

READ_OK = "OK"
READ_NO_RESPONSE = "NO_RESPONSE"
READ_AMBIGUOUS_NEGATIVE = "AMBIGUOUS_NEGATIVE"
READ_WRONG_ECHO = "WRONG_ECHO"
READ_MALFORMED_DATA = "MALFORMED_DATA"
READ_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


class RestorationFailed(RuntimeError):
    """The logger could not prove that it returned CAN to its safe passive state."""


_restoration_failed = False


@dataclass(frozen=True)
class ReadEvidence:
    """One diagnostic read plus a compact, persistable evidence-quality status."""

    value: object
    status: str
    detail: str = ""

    @property
    def ok(self):
        return self.status == READ_OK

    def marker(self):
        """Return a delimiter-safe CSV/journal token for a non-OK result."""
        token = self.status
        if self.detail:
            token += f"({self.detail})"
        return token


def _request_with_echo(s, payload, positive_echo, timeout, attempts=2, deadline=None):
    """Return only a response carrying the exact positive echo for this request.

    A timed-out ISO-TP reply can arrive during the next request. Drain before *each* send, and
    never let a late response for another DID/subfunction become current evidence. Read retries
    are explicit here so the drain also runs between attempts; ``uds.request(retries=1)`` would
    resend internally without that boundary. A UDS negative response cannot echo a DID (or this
    DTC request's full parameters), so it is never attributed to the current read as conclusive
    evidence: drain and retry, then report it explicitly as ambiguous if no echoed positive arrives.
    """
    payload = bytes(payload)
    positive_echo = bytes(positive_echo)
    observations = []
    for _ in range(attempts):
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                observations.append(ReadEvidence(None, READ_BUDGET_EXHAUSTED))
                break
            # uds.request may first wait for the initial response and then for responsePending.
            # Giving each phase half the remaining wall-clock budget keeps the complete call
            # within the auto-session deadline while preserving the normal timeout when possible.
            call_timeout = min(timeout, remaining / 2.0)
        else:
            call_timeout = timeout
        uds.drain(s)
        response, _transport_status = uds.request(
            s,
            payload,
            timeout=call_timeout,
            retries=0,
            response_pending_timeout=call_timeout,
            max_pending_responses=16,
        )
        if response is None:
            observations.append(ReadEvidence(None, READ_NO_RESPONSE))
            continue
        if response[:len(positive_echo)] == positive_echo:
            return ReadEvidence(bytes(response), READ_OK)
        if len(response) >= 3 and response[0] == 0x7F and response[1] == payload[0]:
            observations.append(
                ReadEvidence(None, READ_AMBIGUOUS_NEGATIVE, bytes(response[:3]).hex().upper())
            )
            continue
        # A positive response with the wrong identifier/subfunction is stale or unrelated.
        # Retry the non-mutating read; the next iteration drains anything else queued behind it.
        observations.append(
            ReadEvidence(None, READ_WRONG_ECHO, bytes(response).hex().upper()[:32])
        )

    # Preserve the most informative failure seen across attempts. In particular, do not collapse
    # a negative response or wrong echo into a generic timeout just because the retry was silent.
    for status in (READ_AMBIGUOUS_NEGATIVE, READ_WRONG_ECHO,
                   READ_BUDGET_EXHAUSTED, READ_NO_RESPONSE):
        for observation in reversed(observations):
            if observation.status == status:
                return observation
    return ReadEvidence(None, READ_NO_RESPONSE)


def read_did_evidence(s, did, expected_length=None, deadline=None):
    request = bytes((0x22, did >> 8, did & 0xFF))
    result = _request_with_echo(
        s,
        request,
        bytes((0x62, did >> 8, did & 0xFF)),
        timeout=0.6,
        deadline=deadline,
    )
    if not result.ok:
        return result
    data = result.value[3:]
    if expected_length is not None and len(data) != expected_length:
        return ReadEvidence(
            None,
            READ_MALFORMED_DATA,
            f"LEN{len(data)}_EXPECTED{expected_length}",
        )
    return ReadEvidence(data, READ_OK)


def read_did(s, did, expected_length=None, deadline=None):
    """Compatibility wrapper returning bytes on a validated positive response, else ``None``."""
    result = read_did_evidence(s, did, expected_length=expected_length, deadline=deadline)
    return result.value if result.ok else None


def read_dtcs_evidence(s, deadline=None):
    """Return raw-preserving names and statuses reported by ``19 02 0D``.

    Known entries look like ``550331(C1503-31)``; unknown entries remain their six-digit raw
    hexadecimal value. A valid positive response with zero records is ``READ_OK`` plus an empty
    dict; timeout, negative, wrong-echo, and malformed responses are distinct non-OK evidence.
    """
    result = _request_with_echo(
        s,
        b"\x19\x02\x0D",
        b"\x59\x02",
        timeout=0.8,
        deadline=deadline,
    )
    if not result.ok:
        return result
    r = result.value
    if len(r) < 3:
        return ReadEvidence(None, READ_MALFORMED_DATA, f"LEN{len(r)}_MIN3")
    record_bytes = len(r) - 3
    if record_bytes % 4:
        return ReadEvidence(None, READ_MALFORMED_DATA, f"RECORD_BYTES{record_bytes}_MOD4")
    out = {}
    for i in range(3, len(r), 4):
        dtc, status = bytes(r[i:i + 3]), r[i + 3]
        raw = dtc.hex().upper()
        label = DTC_NAMES.get(dtc)
        out[f"{raw}({label})" if label else raw] = status
    return ReadEvidence(out, READ_OK)


def read_dtcs(s, deadline=None):
    """Compatibility wrapper: dict for valid data (including ``{}``), ``None`` on failure."""
    result = read_dtcs_evidence(s, deadline=deadline)
    return result.value if result.ok else None


def psi(raw):
    """Decode one verified RF Hub pressure, excluding its no-data sentinel."""
    # Pressure DIDs are exactly one u16. Reject truncation, appended/stale data,
    # and FFFF (invalid/no sensor data) before applying the verified scale.
    if not raw or len(raw) != 2 or raw == INVALID_PRESSURE_RAW:
        return None
    return round(int.from_bytes(raw, "big") * 0.1 * 0.145038, 1)


def publish_pressure_telemetry(client, press_results):
    """Best-effort publish valid pressure reads without affecting CSV logging.

    Missing reads and FFFF do not replace a prior cached value; the broker's
    30-second freshness window lets that observation expire naturally.
    """
    if len(press_results) != len(PRESSURE_SLOTS):
        raise ValueError("pressure result count does not match verified RF Hub slots")
    errors = []
    for (_wheel, did, metric), result in zip(PRESSURE_SLOTS, press_results):
        pressure = psi(result.value) if result.ok else None
        if pressure is None:
            continue
        try:
            status, response = client.publish(
                metric,
                value=pressure,
                unit="psi",
                source=f"rf_hub.did.{did:04x}",
                bus="c-can",
                quality="verified",
            )
        except Exception as exc:
            errors.append(f"{metric}: {type(exc).__name__}: {exc}")
            continue
        if not 200 <= status < 300:
            reason = (
                response.get("reason", "publication_rejected")
                if isinstance(response, dict)
                else "publication_rejected"
            )
            errors.append(f"{metric}: HTTP {status}: {reason}")
    return tuple(errors)


def _quality_markers(press_results, lastrx_results, dtc_result):
    """Encode read failures without changing the long-lived CSV schema."""
    markers = []
    for wheel, result in zip(WHEELS, press_results):
        if not result.ok:
            markers.append(f"!READ_PRESS_{wheel}={result.marker()}")
    for wheel, result in zip(WHEELS, lastrx_results):
        if not result.ok:
            markers.append(f"!READ_LASTRX_{wheel}={result.marker()}")
    if not dtc_result.ok:
        markers.append(f"!READ_DTCS={dtc_result.marker()}")
    return markers


def _dtc_csv_cell(press_results, lastrx_results, dtc_result):
    """Serialize DTCs and quality markers into the existing final CSV column.

    An empty string now has one unambiguous meaning: a valid ``59 02`` response reported zero
    DTC records and every companion read was valid. Failed/ambiguous reads receive ``!READ_*``
    markers, so continuing an existing CSV needs no header rewrite or historical migration.
    """
    entries = []
    if dtc_result.ok:
        entries.extend(
            f"{key}={status:02X}" for key, status in sorted(dtc_result.value.items())
        )
    entries.extend(_quality_markers(press_results, lastrx_results, dtc_result))
    return ";".join(entries)


def broker_absence_proven(client=None):
    """Whether the broker Unix endpoint is narrowly proven absent.

    Any HTTP response proves that a broker is live, regardless of version,
    configuration, status code, or payload. Timeout, malformed protocol/schema,
    permission, and general I/O failures are uncertain and therefore fail
    closed. Only a missing Unix socket or refused Unix connection authorizes
    the guarded standalone fallback.
    """
    client = client or TelemetryClient(
        DEFAULT_TELEMETRY_SOCKET,
        timeout=TELEMETRY_TIMEOUT_S,
    )
    try:
        client.request("GET", "/v1/status")
    except OSError as exc:
        return exc.errno in (errno.ENOENT, errno.ECONNREFUSED)
    except Exception:
        return False
    return False


def _active_inhibits(channel):
    """Fail closed when operation-inhibit state cannot be read."""
    try:
        return tuple(can_operation_state.active_inhibits(channel))
    except (OSError, RuntimeError, ValueError):
        return ({"name": "inhibit-state-unavailable", "channel": channel},)


def _topology_is_ccan(channel):
    """Require the current-boot topology record for the exact pins-6/14 branch."""
    try:
        topology = can_operation_state.load_topology(channel)
    except (OSError, RuntimeError, ValueError):
        return False
    return bool(
        topology.usable
        and topology.bus == "c-can"
        and topology.pair == CCAN_PAIR
    )


def _restoration_is_latched(channel):
    if _restoration_failed:
        return True
    return any(
        item.get("name") == RESTORATION_INHIBIT_NAME
        for item in _active_inhibits(channel)
        if isinstance(item, dict)
    )


def _latch_restoration_failure(channel):
    """Persist a same-boot stop condition so a systemd restart cannot resume polling."""
    global _restoration_failed
    _restoration_failed = True
    reason = (
        "TPMS logger could not verify exact listen-only restoration; "
        "manual inspection is required before clearing this inhibit"
    )
    print(f"CRITICAL: {reason}", flush=True)
    try:
        can_operation_state.begin_inhibit(
            RESTORATION_INHIBIT_NAME,
            channel="*",
            reason=reason,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            "CRITICAL: could not persist TPMS restoration inhibit: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


def _safe_passive_state(state, channel, bitrate):
    return bool(
        isinstance(state, canbus.InterfaceState)
        and state.channel == channel
        and state.present
        and state.up
        and state.bitrate == bitrate
        and state.fd_enabled is False
        and state.listen_only
        and state.controller_state == "ERROR-ACTIVE"
        and state.restart_ms == 0
    )


def _safe_armed_state(state, channel, bitrate, restart_ms):
    return bool(
        isinstance(state, canbus.InterfaceState)
        and state.channel == channel
        and state.present
        and state.up
        and state.bitrate == bitrate
        and state.fd_enabled is False
        and not state.listen_only
        and state.controller_state == "ERROR-ACTIVE"
        and state.restart_ms == restart_ms
    )


def engine_running(
    channel,
    window=RUNNING_WINDOW_S,
    required_samples=RUNNING_SAMPLE_COUNT,
    threshold_rpm=RUNNING_THRESHOLD_RPM,
    *,
    socket_factory=socket.socket,
    monotonic=None,
):
    """Require consecutive fresh standard 0x0FC samples above the running threshold."""
    if window <= 0 or required_samples < 1 or threshold_rpm < 0:
        raise ValueError("invalid engine-running gate configuration")
    monotonic = monotonic or time.monotonic
    sock = None
    try:
        sock = socket_factory(AF_CAN, socket.SOCK_RAW, CAN_RAW)
        can_filter = struct.pack("=II", ENGINE_SPEED_ID, CAN_FILTER_MASK)
        sock.setsockopt(SOL_CAN_RAW, CAN_RAW_FILTER, can_filter)
        sock.bind((channel,))
        deadline = monotonic() + window
        consecutive = 0
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            sock.settimeout(max(0.01, remaining))
            try:
                frame = sock.recv(16)
            except socket.timeout:
                return False
            if len(frame) != 16:
                return False
            can_id, dlc, data = struct.unpack("=IB3x8s", frame)
            if (
                can_id & CAN_FRAME_FLAGS
                or (can_id & CAN_SFF_MASK) != ENGINE_SPEED_ID
                or not 2 <= dlc <= 8
            ):
                continue
            rpm = (int.from_bytes(data[:2], "big") & 0xFFFC) / 4.0
            if rpm >= threshold_rpm:
                consecutive += 1
                if consecutive >= required_samples:
                    return True
            else:
                consecutive = 0
    except OSError:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _resolved_rfh_route():
    return can_runtime_route.resolve_module_route(get("rf_hub"))


def _passive_running_ready(module=None):
    """Complete the no-TX fallback preflight before attempting the exclusive lock."""
    ownership = None
    try:
        ownership = can_runtime_route.acquire_passive_bus_route(
            "c-can",
            asserted_pair=CCAN_PAIR,
        )
        module = bind_channel(module or get("rf_hub"), ownership.route.channel)
    except (OSError, RuntimeError, ValueError, diagnostic_safety.ChannelLockError):
        return False
    try:
        if (
            _restoration_is_latched(module.channel)
            or not _topology_is_ccan(module.channel)
            or _active_inhibits(module.channel)
        ):
            return False
        ownership.revalidate()
        try:
            bus = canbus.identify_bus(module.channel, probe=BUS_PROBE_S)
        except (OSError, RuntimeError, ValueError):
            return False
        return bus == "c-can" and engine_running(module.channel)
    finally:
        if ownership is not None:
            ownership.release()


def _locked_start_state(module, *, auto, telemetry):
    """Repeat every delegation, topology, interface, bus, inhibit, and RPM gate."""
    if not broker_absence_proven(telemetry):
        return None
    if (
        _restoration_is_latched(module.channel)
        or not _topology_is_ccan(module.channel)
        or _active_inhibits(module.channel)
    ):
        return None
    initial = canbus.interface_state(module.channel)
    if not _safe_passive_state(initial, module.channel, module.bitrate):
        return None
    try:
        bus = canbus.identify_bus(module.channel, probe=BUS_PROBE_S)
    except (OSError, RuntimeError, ValueError):
        return None
    if bus != "c-can" or not engine_running(module.channel):
        return None
    final = canbus.interface_state(module.channel)
    if (
        not initial.same_configuration(final)
        or not _safe_passive_state(final, module.channel, module.bitrate)
        or not _topology_is_ccan(module.channel)
        or _active_inhibits(module.channel)
        or not broker_absence_proven(telemetry)
    ):
        return None
    return final


def _arm_iface_locked(module, initial_state, lock_handle):
    diagnostic_safety.validate_channel_lock(lock_handle, module.channel)
    restart_ms = (
        initial_state.restart_ms if initial_state.restart_ms is not None else 0
    )
    try:
        configured = canbus.ip_up(
            module.channel,
            module.bitrate,
            listen_only=False,
            restart_ms=restart_ms,
            noninteractive=True,
        )
    except Exception:
        return False
    return bool(
        configured
        and _safe_armed_state(
            canbus.interface_state(module.channel),
            module.channel,
            module.bitrate,
            restart_ms,
        )
    )


def _active_session_gates_hold(
    module,
    initial_state,
    *,
    auto,
    telemetry,
    route=None,
):
    if route is not None:
        try:
            can_runtime_route.revalidate_module_route(route)
        except (OSError, RuntimeError, ValueError):
            return False
    if not broker_absence_proven(telemetry):
        return False
    if (
        _restoration_is_latched(module.channel)
        or not _topology_is_ccan(module.channel)
        or _active_inhibits(module.channel)
    ):
        return False
    if not engine_running(module.channel):
        return False
    if (
        not _topology_is_ccan(module.channel)
        or _active_inhibits(module.channel)
        or not broker_absence_proven(telemetry)
    ):
        return False
    expected_restart_ms = (
        initial_state.restart_ms if initial_state.restart_ms is not None else 0
    )
    return _safe_armed_state(
        canbus.interface_state(module.channel),
        module.channel,
        module.bitrate,
        expected_restart_ms,
    )


IGN_BCAST = 0x2EF   # broadcast only present with ignition ON (see ccan_voltage.py).
                    # Gating on it (not raw frame count) matters: our own diag polling
                    # holds FCA network management awake, so a frame-count gate would
                    # never see the bus go quiet and would drain the battery
                    # (verified 2026-07-07: polling stopped -> bus asleep in 60 s).


def ignition_on(channel, window=2.0):
    """True if the ignition-only broadcast is on the wire. Pure RX -- never transmits."""
    s = None
    try:
        s = socket.socket(AF_CAN, socket.SOCK_RAW, CAN_RAW)
        flt = struct.pack("=II", IGN_BCAST, 0x1FFFFFFF)
        s.setsockopt(SOL_CAN_RAW, CAN_RAW_FILTER, flt)
        s.bind((channel,))
        s.settimeout(window)
    except OSError:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
        return False                   # iface missing/down; caller re-ensures
    try:
        s.recv(16)
        return True
    except (socket.timeout, OSError):
        return False
    finally:
        s.close()


def log_session(auto=False, telemetry=None, module=None):
    """Run one guarded standalone session from passive gate through passive restore."""
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    new = not os.path.exists(CSV_PATH)
    route = _resolved_rfh_route()
    m = bind_channel(module or get("rf_hub"), route.channel)
    telemetry = telemetry or TelemetryClient(
        DEFAULT_TELEMETRY_SOCKET,
        timeout=TELEMETRY_TIMEOUT_S,
    )
    with diagnostic_safety.interrupt_on_termination() as termination:
        with ExitStack() as ownership:
            ownership.enter_context(
                diagnostic_safety.channel_lock(f"can-role-{m.bus}")
            )
            lock_handle = ownership.enter_context(
                diagnostic_safety.channel_lock(m.channel)
            )
            can_runtime_route.revalidate_module_route(route)
            s = None
            f = None
            initial_state = None
            mutation_attempted = False
            try:
                initial_state = _locked_start_state(
                    m,
                    auto=auto,
                    telemetry=telemetry,
                )
                if initial_state is None:
                    return False
                mutation_attempted = True
                if not _arm_iface_locked(m, initial_state, lock_handle):
                    print(
                        "cannot arm RF Hub interface from the verified passive baseline; "
                        "restoring",
                        flush=True,
                    )
                    return False
                # The one down/up transition creates a small evidence gap. Require fresh running
                # evidence again before opening a diagnostic transport or sending any request.
                if not _active_session_gates_hold(
                    m,
                    initial_state,
                    auto=auto,
                    telemetry=telemetry,
                    route=route,
                ):
                    print(
                        "engine/interface/ownership evidence vanished during interface transition; "
                        "restoring without UDS",
                        flush=True,
                    )
                    return False
                s = uds.open_socket(
                    m.txid,
                    m.rxid,
                    m.channel,
                    timeout=0.8,
                    addressing_mode=m.addressing_mode,
                )
                f = open(CSV_PATH, "a", newline="")
                w = csv.writer(f)
                if new:
                    w.writerow(["time"] + [f"psi_{x}" for x in WHEELS]
                               + [f"lastrx_{x}" for x in WHEELS] + ["dtcs"])
                prev = None
                previous_telemetry_errors = None
                print(f"logging to {CSV_PATH} every {CYCLE_S}s", flush=True)
                while True:
                    if not _active_session_gates_hold(
                        m,
                        initial_state,
                        auto=auto,
                        telemetry=telemetry,
                        route=route,
                    ):
                        print(
                            "engine/interface/ownership gate lost -> ending TPMS "
                            "session and restoring listen-only",
                            flush=True,
                        )
                        return True
                    cycle_started = time.monotonic()
                    deadline = cycle_started + AUTO_POLL_BUDGET_S if auto else None
                    try:
                        press_results = [
                            read_did_evidence(s, did, expected_length=2, deadline=deadline)
                            for did in PRESS_DIDS
                        ]
                        lastrx_results = [
                            read_did_evidence(s, did, deadline=deadline) for did in LASTRX_DIDS
                        ]
                        dtc_result = read_dtcs_evidence(s, deadline=deadline)
                    except OSError as e:
                        print(
                            f"! socket error ({e}); ending this resolved-role session "
                            "for passive restoration and fresh USB re-resolution",
                            flush=True,
                        )
                        try:
                            s.close()
                        except Exception:
                            pass
                        s = None
                        return True
                    press = [result.value if result.ok else None for result in press_results]
                    lastrx = [result.value if result.ok else None for result in lastrx_results]
                    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    row = ([psi(p) for p in press]
                           + [x.hex() if x else "" for x in lastrx]
                           + [_dtc_csv_cell(press_results, lastrx_results, dtc_result)])
                    w.writerow([now] + row)
                    f.flush()
                    telemetry_errors = publish_pressure_telemetry(
                        telemetry, press_results
                    )
                    if telemetry_errors != previous_telemetry_errors:
                        if telemetry_errors:
                            print(
                                "! telemetry publication: "
                                + "; ".join(telemetry_errors),
                                flush=True,
                            )
                        elif previous_telemetry_errors:
                            print("telemetry publication recovered", flush=True)
                        previous_telemetry_errors = telemetry_errors
                    if row != prev:
                        issues = _quality_markers(press_results, lastrx_results, dtc_result)
                        tag = f"  << READ ISSUES: {','.join(issues)}" if issues else ""
                        print(f"{now}  psi={row[0:4]}  dtc={row[8]}{tag}", flush=True)
                        prev = row
                    if not _active_session_gates_hold(
                        m,
                        initial_state,
                        auto=auto,
                        telemetry=telemetry,
                        route=route,
                    ):
                        print(
                            "engine/interface/ownership gate lost -> ending TPMS "
                            "session and restoring listen-only",
                            flush=True,
                        )
                        return True
                    cycle_elapsed = time.monotonic() - cycle_started
                    time.sleep(max(0.0, CYCLE_S - cycle_elapsed))
            finally:
                termination.begin_cleanup()
                try:
                    if s is not None:
                        s.close()
                except Exception as exc:
                    print(
                        f"! ISO-TP socket close failed: {type(exc).__name__}: {exc}",
                        flush=True,
                    )
                try:
                    if f is not None:
                        f.close()
                except Exception as exc:
                    print(
                        f"! TPMS CSV close failed: {type(exc).__name__}: {exc}",
                        flush=True,
                    )
                if mutation_attempted and initial_state is not None:
                    restored = False
                    try:
                        can_runtime_route.revalidate_module_route(route)
                        restored = bool(
                            canbus.restore_interface_state(
                                initial_state,
                                noninteractive=True,
                            )
                        )
                    except Exception as exc:
                        print(
                            "! exact TPMS interface restoration raised "
                            f"{type(exc).__name__}: {exc}",
                            flush=True,
                        )
                    if not restored:
                        _latch_restoration_failure(m.channel)
                        raise RestorationFailed(
                            f"could not verify exact listen-only restoration of "
                            f"{m.channel}; further TPMS polling is inhibited"
                        )


def auto_loop():
    """Yield unless broker absence is proven, then run the guarded fallback."""
    telemetry = TelemetryClient(
        DEFAULT_TELEMETRY_SOCKET,
        timeout=TELEMETRY_TIMEOUT_S,
    )
    delegated = None
    print(
        "auto mode: broker delegation first; standalone fallback requires passive "
        "C-CAN and fresh running RPM",
        flush=True,
    )
    while True:
        broker_absent = broker_absence_proven(telemetry)
        if not broker_absent:
            if delegated is not True:
                print(
                    "telemetry broker is live or its status is uncertain -> TPMS "
                    "logger yielding with no CAN lock, interface change, or UDS",
                    flush=True,
                )
            delegated = True
            time.sleep(IDLE_SLEEP_S)
            continue
        if delegated is True:
            print(
                "telemetry broker Unix endpoint is proven absent -> guarded "
                "standalone TPMS fallback",
                flush=True,
            )
        delegated = False
        try:
            m = _resolved_rfh_route().module
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"stable C-CAN role unavailable -> TPMS fallback deferred: {exc}", flush=True)
            time.sleep(IDLE_SLEEP_S)
            continue
        if _restoration_is_latched(m.channel):
            time.sleep(IDLE_SLEEP_S)
            continue
        if not _passive_running_ready(m):
            time.sleep(IDLE_SLEEP_S)
            continue
        print("fresh passive engine-running evidence -> guarded TPMS session", flush=True)
        try:
            log_session(auto=True, telemetry=telemetry, module=m)
        except diagnostic_safety.ChannelLockError as exc:
            print(f"RF Hub polling deferred while {m.channel} is busy: {exc}", flush=True)
            time.sleep(IDLE_SLEEP_S)
        except RestorationFailed as exc:
            print(f"CRITICAL: {exc}", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true",
                    help="unattended: log only while the bus is awake (systemd mode)")
    args = ap.parse_args(argv)
    try:
        if args.auto:
            auto_loop()
        else:
            try:
                module = _resolved_rfh_route().module
            except (OSError, RuntimeError, ValueError) as exc:
                raise SystemExit(
                    f"refusing standalone TPMS polling: stable C-CAN role unavailable: {exc}"
                ) from None
            telemetry = TelemetryClient(
                DEFAULT_TELEMETRY_SOCKET,
                timeout=TELEMETRY_TIMEOUT_S,
            )
            if not broker_absence_proven(telemetry):
                raise SystemExit(
                    "refusing standalone TPMS polling: the telemetry broker is "
                    "live or its Unix-socket status is uncertain"
                )
            if not _passive_running_ready(module):
                raise SystemExit(
                    "refusing standalone TPMS polling: passive pins-6/14 C-CAN "
                    "and fresh engine-running RPM evidence are required"
                )
            log_session(auto=False, telemetry=telemetry, module=module)
    except diagnostic_safety.ChannelLockError as exc:
        raise SystemExit(f"refusing to start TPMS polling: {exc}") from None
    except RestorationFailed as exc:
        raise SystemExit(f"TPMS restoration failure: {exc}") from None
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
