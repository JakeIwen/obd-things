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

Appends CSV to tmp/tpms/tpms_drive_log.csv; prints changes to stdout. Read-only UDS
(22 / 19), no writes. Survives socket drops (uds.recover_socket) and ignition state
changes (the RFH answers on battery). Ctrl-C to stop.

    ./bringup.sh --tx                       # iface must be ARMED for UDS
    python3 projects/tpms/tpms_logger.py    # run for the duration of the drive

AUTO MODE (systemd service tpms-logger.service runs this):

    python3 projects/tpms/tpms_logger.py --auto

Battery-safe unattended operation for a van that is lived in: IDLE = pure-RX watch for
the ignition-only broadcast 0x2EF (2 s filtered listen every 30 s, transmits NOTHING, so
a parked/asleep bus stays asleep); 0x2EF present -> poll/log as above; 0x2EF gone
(ignition off) -> session ends within ~12 s and the bus is released to sleep. Gate is
0x2EF, NOT raw frame count, because our own diag polling holds network management awake.
The ready-interface pure-RX watch does not take the diagnostic lock. If the interface needs
reconfiguration, that mutation uses a brief lock/recheck; each polling session holds the lock
from before socket creation through final socket cleanup. Manages the iface itself (500k, armed).
Stop before manual bus work:
sudo systemctl stop tpms-logger
"""
import os
import csv
import re
import sys
import time
import socket
import struct
import argparse
import datetime
import subprocess
from dataclasses import dataclass

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)
from lib import diagnostic_safety, uds
from lib.modules import get

CYCLE_S = 10
IGNITION_WINDOW_S = 2.0
AUTO_POLL_BUDGET_S = 8.0
AUTO_RECOVERY_MAX_WAIT_S = 8.0
CSV_PATH = os.path.join(REPO, "tmp", "tpms", "tpms_drive_log.csv")
# Current physical wheel names for slots 1-4, verified by the 2026-07-07 deflate test.
WHEELS = ("FL", "FR", "RR", "RL")
PRESS_DIDS = (0x31D0, 0x31D1, 0x31D2, 0x31D3)
LASTRX_DIDS = (0x301E, 0x301F, 0x3020, 0x3021)
DTC_NAMES = {b"\x90\x40\x64": "B1040-64", b"\x55\x12\x88": "C1512-88",
             b"\x55\x03\x31": "C1503-31"}

READ_OK = "OK"
READ_NO_RESPONSE = "NO_RESPONSE"
READ_AMBIGUOUS_NEGATIVE = "AMBIGUOUS_NEGATIVE"
READ_WRONG_ECHO = "WRONG_ECHO"
READ_MALFORMED_DATA = "MALFORMED_DATA"
READ_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


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
    # Pressure DIDs are exactly one u16. Reject truncation or appended/stale data defensively.
    return round(int.from_bytes(raw, "big") * 0.1 * 0.145038, 1) if raw and len(raw) == 2 else None


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


def iface_is_armed(channel="can0", bitrate=500000):
    """Pure inspection: whether ``channel`` is UP at ``bitrate`` and able to transmit."""
    result = subprocess.run(
        ["ip", "-details", "link", "show", channel], capture_output=True, text=True
    )
    if result.returncode != 0:
        return False
    show = result.stdout
    flags = re.search(r"^\s*\d+:\s+[^\n]*<([^>\n]*)>", show, re.MULTILINE)
    rate = re.search(r"\bbitrate\s+(\d+)\b", show)
    can_state = re.search(r"\bcan(?:\s+<[^>\n]*>)?\s+state\s+([A-Z-]+)\b", show)
    return bool(
        flags
        and "UP" in flags.group(1).split(",")
        and rate
        and int(rate.group(1)) == bitrate
        and "LISTEN-ONLY" not in show
        and can_state
        and can_state.group(1) == "ERROR-ACTIVE"
    )


def _reconfigure_iface(channel="can0", bitrate=500000):
    """Reconfigure the interface; caller must already own the channel lock."""
    sudo = [] if os.geteuid() == 0 else ["sudo"]
    subprocess.run(sudo + ["ip", "link", "set", channel, "down"], check=False)
    subprocess.run(sudo + ["ip", "link", "set", channel, "up", "type", "can",
                           "bitrate", str(bitrate), "listen-only", "off"], check=True)
    print(f"iface {channel} (re)configured: {bitrate} armed", flush=True)


def _ensure_iface_locked(channel="can0", bitrate=500000):
    """Recheck and, if needed, reconfigure while the caller owns the channel lock."""
    if iface_is_armed(channel, bitrate):
        return True
    try:
        _reconfigure_iface(channel, bitrate)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"iface {channel} reconfiguration failed: {exc}", flush=True)
        return False
    return iface_is_armed(channel, bitrate)


def ensure_iface_coordinated(channel="can0", bitrate=500000):
    """Ensure active mode without ever mutating an uncoordinated channel.

    The common idle path is inspection only and takes no lock. A required mutation uses a
    nonblocking lock, rechecks after acquiring it, and defers cleanly if another transmitter owns
    the channel.
    """
    if iface_is_armed(channel, bitrate):
        return True
    try:
        with diagnostic_safety.channel_lock(channel):
            return _ensure_iface_locked(channel, bitrate)
    except diagnostic_safety.ChannelLockError as exc:
        print(f"iface {channel} needs reconfiguration; deferring while busy: {exc}", flush=True)
        return False


IGN_BCAST = 0x2EF   # broadcast only present with ignition ON (see ccan_voltage.py).
                    # Gating on it (not raw frame count) matters: our own diag polling
                    # holds FCA network management awake, so a frame-count gate would
                    # never see the bus go quiet and would drain the battery
                    # (verified 2026-07-07: polling stopped -> bus asleep in 60 s).


def ignition_on(channel="can0", window=2.0):
    """True if the ignition-only broadcast is on the wire. Pure RX -- never transmits."""
    s = None
    try:
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        flt = struct.pack("=II", IGN_BCAST, 0x1FFFFFFF)
        s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, flt)
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


def log_session(auto=False):
    """Poll/log until Ctrl-C (manual) or until the bus goes quiet (auto). """
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    new = not os.path.exists(CSV_PATH)
    m = get("rf_hub")
    with diagnostic_safety.interrupt_on_termination() as termination:
        with diagnostic_safety.channel_lock(m.channel) as lock_handle:
            s = None
            f = None
            try:
                if auto and not _ensure_iface_locked(m.channel, m.bitrate):
                    print("cannot arm RF Hub interface; deferring this logging session", flush=True)
                    return
                s = uds.open_socket(m.txid, m.rxid, m.channel, timeout=0.8)
                f = open(CSV_PATH, "a", newline="")
                w = csv.writer(f)
                if new:
                    w.writerow(["time"] + [f"psi_{x}" for x in WHEELS]
                               + [f"lastrx_{x}" for x in WHEELS] + ["dtcs"])
                prev = None
                print(f"logging to {CSV_PATH} every {CYCLE_S}s", flush=True)
                while True:
                    if auto and not ignition_on(m.channel, IGNITION_WINDOW_S):
                        print("ignition off (0x2EF gone) -> session end, releasing bus to sleep",
                              flush=True)
                        return
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
                        print(f"! socket error ({e}); recovering", flush=True)
                        try:
                            s.close()
                        except Exception:
                            pass
                        s = None
                        recover_kwargs = {
                            "bitrate": m.bitrate,
                            "addressing_mode": m.addressing_mode,
                            "lock_handle": lock_handle,
                        }
                        if auto:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                print("auto-session poll budget exhausted during socket error; "
                                      "releasing bus", flush=True)
                                return
                            recover_kwargs["max_wait"] = min(AUTO_RECOVERY_MAX_WAIT_S, remaining)
                        try:
                            s = uds.recover_socket(
                                m.txid,
                                m.rxid,
                                m.channel,
                                **recover_kwargs,
                            )
                        except (OSError, RuntimeError) as recovery_error:
                            if not auto:
                                raise
                            print(f"auto-session recovery failed ({recovery_error}); releasing bus",
                                  flush=True)
                            return
                        if auto and not ignition_on(m.channel, IGNITION_WINDOW_S):
                            print("ignition off during socket recovery -> releasing bus to sleep",
                                  flush=True)
                            return
                        continue
                    press = [result.value if result.ok else None for result in press_results]
                    lastrx = [result.value if result.ok else None for result in lastrx_results]
                    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    row = ([psi(p) for p in press]
                           + [x.hex() if x else "" for x in lastrx]
                           + [_dtc_csv_cell(press_results, lastrx_results, dtc_result)])
                    w.writerow([now] + row)
                    f.flush()
                    if row != prev:
                        issues = _quality_markers(press_results, lastrx_results, dtc_result)
                        tag = f"  << READ ISSUES: {','.join(issues)}" if issues else ""
                        print(f"{now}  psi={row[0:4]}  dtc={row[8]}{tag}", flush=True)
                        prev = row
                    if auto and not ignition_on(m.channel, IGNITION_WINDOW_S):
                        print("ignition off (0x2EF gone) -> session end, releasing bus to sleep",
                              flush=True)
                        return
                    cycle_elapsed = time.monotonic() - cycle_started
                    time.sleep(max(0.0, CYCLE_S - cycle_elapsed))
            finally:
                termination.begin_cleanup()
                if f is not None:
                    f.close()
                try:
                    if s is not None:
                        s.close()
                except Exception:
                    pass


def auto_loop():
    """IDLE (pure-RX watch, no TX, lets the bus sleep) <-> logging sessions."""
    m = get("rf_hub")
    print("auto mode: watching for ignition (0x2EF), no TX while idle", flush=True)
    while True:
        if not ensure_iface_coordinated(m.channel, m.bitrate):
            time.sleep(28)
            continue
        if ignition_on(m.channel, 2.0):
            print("ignition on (0x2EF seen) -> logging session", flush=True)
            try:
                log_session(auto=True)
            except diagnostic_safety.ChannelLockError as exc:
                print(f"RF Hub polling deferred while {m.channel} is busy: {exc}", flush=True)
                time.sleep(28)
        else:
            time.sleep(28)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true",
                    help="unattended: log only while the bus is awake (systemd mode)")
    args = ap.parse_args(argv)
    try:
        auto_loop() if args.auto else log_session()
    except diagnostic_safety.ChannelLockError as exc:
        raise SystemExit(f"refusing to start TPMS polling: {exc}") from None
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
