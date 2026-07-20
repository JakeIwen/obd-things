"""Base live-data viewer: a `top`-style display driven by a module's address + a metric table.

Per-module scripts (e.g. radar_acc.py) define a list of Metric(...) and call run(). This file is
generic - it knows nothing about any particular ECU. Direct mode is active diagnostic traffic,
dry-run by default, and bounded by explicit time/rate/request limits. It enters an extended
session and sends TesterPresent plus ReadDataByIdentifier requests only after parked safety gates.
"""
import argparse
import math
import os
import sys
import time
import struct
import shutil
from collections import namedtuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from lib import canbus, diagnostic_safety, uds
from lib.uds import s16, s32, u8        # re-export so metric tables can `from ... import s16,...`
from tools.ecu_discover import preflight

# A displayed row: which DID to read, friendly name, fn(data)->number, scale, unit string.
# unit beginning with "deg" gets the spec-window colour cue; others (V, C, ...) are neutral.
Metric = namedtuple("Metric", "did name fn scale unit")

# ANSI
ALT_ON, ALT_OFF = "\033[?1049h", "\033[?1049l"
CUR_OFF, CUR_ON = "\033[?25l", "\033[?25h"
HOME, CLR_EOL = "\033[H", "\033[K"
BOLD, DIM, RST = "\033[1m", "\033[2m", "\033[0m"
RED, YEL, GRN, CYA = "\033[31m", "\033[33m", "\033[32m", "\033[36m"

W_DID, W_NAME, W_VAL, W_UNIT = 4, 26, 8, 5
MAX_RUN_SECONDS = 600.0
MAX_REQUEST_RATE = 10.0
MIN_REQUEST_RATE = 0.5
MAX_REQUESTS = 5000
TESTER_PRESENT_INTERVAL_S = 2.0


def reading_cell(val, unit, spec_deg):
    """Colourised, fixed-width (W_VAL visible chars) reading. Angle units coloured by spec."""
    if unit.startswith("deg"):
        s = f"{val:+{W_VAL}.3f}"
        color = GRN if abs(val) <= spec_deg else (YEL if abs(val) <= 1.5 * spec_deg else RED)
    else:
        s = f"{val:{W_VAL}.3f}"
        color = CYA
    return color + s + RST


class LinkError(RuntimeError):
    """The bounded direct diagnostic link cannot continue without unsafe recovery."""


class Link:
    """Own one bounded socket/session without reconfiguring or re-arming the CAN interface."""
    def __init__(self, module, request_rate=5.0, max_requests=1000, timeout=0.75):
        self.m = module
        self.sock = None
        self.connected = False
        self.last_tp = 0.0
        self.last_send = None
        self.request_rate = request_rate
        self.max_requests = max_requests
        self.timeout = timeout
        self.request_attempts = 0

    def _request(self, payload, timeout=None):
        if self.sock is None:
            raise LinkError("diagnostic socket is not open")
        if self.request_attempts >= self.max_requests:
            raise LinkError("bounded live-view request budget exhausted")
        now = time.monotonic()
        interval = 1.0 / self.request_rate
        if self.last_send is not None:
            time.sleep(max(0.0, interval - (now - self.last_send)))
        uds.drain(self.sock)
        self.last_send = time.monotonic()
        self.request_attempts += 1
        return uds.request(
            self.sock,
            bytes(payload),
            timeout=timeout or self.timeout,
            retries=0,
            response_pending_timeout=max(5.0, self.timeout * 5.0),
            max_pending_responses=32,
        )

    def ensure(self):
        if self.sock is not None:
            return True
        try:
            self.sock = uds.open_module_socket(self.m, timeout=self.timeout)
            response, _ = self._request(bytes.fromhex("10 03"), timeout=max(1.0, self.timeout))
            if response is None or bytes(response)[:2] != bytes.fromhex("50 03"):
                detail = uds.hx(response) if response else "timeout"
                raise LinkError(f"extended session lacked exact 50 03 echo ({detail})")
        except Exception:
            self.drop()
            raise
        self.last_tp = time.monotonic()
        return True

    def drop(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None
        self.connected = False

    def read_did(self, did):
        """Return data bytes (after 62 + 2-byte echo) or None. Validates the echoed DID so a late
        reply never lands on the wrong row."""
        self.ensure()
        hi, lo = (did >> 8) & 0xFF, did & 0xFF
        try:
            if time.monotonic() - self.last_tp >= TESTER_PRESENT_INTERVAL_S:
                response, _ = self._request(bytes.fromhex("3E 00"), timeout=min(0.5, self.timeout))
                if response is None or bytes(response)[:2] != bytes.fromhex("7E 00"):
                    detail = uds.hx(response) if response else "timeout"
                    raise LinkError(f"TesterPresent lacked exact 7E 00 echo ({detail})")
                self.last_tp = time.monotonic()
            response, _ = self._request(bytes((0x22, hi, lo)))
            if response is None:
                raise LinkError(f"DID {did:04X} timed out; aborting instead of replaying/re-arming")
            response = bytes(response)
            if len(response) >= 3 and response[:3] == bytes((0x62, hi, lo)):
                self.connected = True
                return response[3:]
            if len(response) >= 3 and response[:2] == bytes.fromhex("7F 22"):
                return None
            raise LinkError(f"DID {did:04X} response lacked exact 62 {did:04X} echo")
        except Exception:
            self.drop()
            raise


def render(link, metrics, title, spec_deg, interval, tick):
    width = shutil.get_terminal_size((80, 24)).columns
    raw_room = max(8, width - (W_DID + 2 + W_NAME + 1 + W_VAL + 1 + W_UNIT + 1) - 1)

    cache = {}
    for mtr in metrics:
        if mtr.did not in cache:
            cache[mtr.did] = link.read_did(mtr.did)

    live = link.connected and any(v is not None for v in cache.values())
    ts = time.strftime("%H:%M:%S")
    status = f"{GRN}LIVE{RST}" if live else f"{RED}NO DATA{RST} {DIM}(ign on? can0 up? bus awake?){RST}"

    lines = []
    lines.append(f"{BOLD}{CYA}{title}{RST}  {link.m.name}  TX {link.m.txid:08X} / RX {link.m.rxid:08X}")
    lines.append(
        f"{ts}  target cycle {1.0/interval:.1f} Hz  request cap {link.request_rate:g}/s  "
        f"cycle {tick}  status {status}"
    )
    lines.append("")
    lines.append(f"{BOLD}{'DID':<{W_DID}}  {'Name':<{W_NAME}} {'Reading':>{W_VAL}} "
                 f"{'Units':<{W_UNIT}} {'Raw bytes'}{RST}")
    lines.append(f"{DIM}{'-'*W_DID}  {'-'*W_NAME} {'-'*W_VAL} {'-'*W_UNIT} {'-'*raw_room}{RST}")

    for mtr in metrics:
        data = cache.get(mtr.did)
        if data is None:
            cell = f"{DIM}{'---':>{W_VAL}}{RST}"
            rawhex = f"{DIM}(no response){RST}"
        else:
            try:
                cell = reading_cell(mtr.fn(data) * mtr.scale, mtr.unit, spec_deg)
            except (struct.error, IndexError):
                cell = f"{DIM}{'short':>{W_VAL}}{RST}"
            rawhex = uds.hx(data)[:raw_room]
        lines.append(f"{mtr.did:0{W_DID}X}  {mtr.name:<{W_NAME}} {cell} {mtr.unit:<{W_UNIT}} {rawhex}")

    lines.append("")
    lines.append(f"{DIM}spec +/-{spec_deg:.1f} deg  {GRN}green{RST}{DIM}=in spec "
                 f"{YEL}yellow{RST}{DIM}=marginal {RED}red{RST}{DIM}=out of spec{RST}")
    lines.append(f"{DIM}angle units/labels inferred - see findings/. Ctrl-C quits.{RST}")

    sys.stdout.write(HOME + "".join(ln + CLR_EOL + "\r\n" for ln in lines) + "\033[J")
    sys.stdout.flush()


def _run_parser():
    p = argparse.ArgumentParser(
        description="Bounded direct UDS live-data view (dry-run by default)."
    )
    p.add_argument(
        "refresh_seconds",
        nargs="?",
        type=float,
        help="legacy display refresh interval override",
    )
    p.add_argument("--seconds", type=float, default=60.0, help="bounded live duration")
    p.add_argument("--rate", type=float, default=5.0, help="maximum total UDS requests/s")
    p.add_argument("--max-requests", type=int, default=1000)
    p.add_argument("--timeout", type=float, default=0.75)
    p.add_argument("--session", default="03", choices=("03",), help="fixed reviewed session")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--confirm-parked", action="store_true")
    p.add_argument("--confirm-engine-off", action="store_true")
    p.add_argument("--confirm-session-change", action="store_true")
    p.add_argument("--confirm-no-active-routine", action="store_true")
    p.add_argument("--pair")
    p.add_argument("--conditions")
    return p


def run(module, metrics, title=None, spec_deg=1.0, refresh_hz=5.0, argv=None):
    """Plan or drive a bounded live view; direct CAN access is never implicit."""
    args = _run_parser().parse_args(sys.argv[1:] if argv is None else argv)
    interval = args.refresh_seconds if args.refresh_seconds is not None else 1.0 / refresh_hz
    if not math.isfinite(interval) or interval <= 0:
        raise SystemExit("refresh_seconds must be a positive finite number")
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= MAX_RUN_SECONDS:
        raise SystemExit(f"--seconds must be >0 and <= {MAX_RUN_SECONDS:g}")
    if not math.isfinite(args.rate) or not MIN_REQUEST_RATE <= args.rate <= MAX_REQUEST_RATE:
        raise SystemExit(
            f"--rate must be between {MIN_REQUEST_RATE:g} and {MAX_REQUEST_RATE:g} requests/s"
        )
    if not isinstance(args.max_requests, int) or not 2 <= args.max_requests <= MAX_REQUESTS:
        raise SystemExit(f"--max-requests must be between 2 and {MAX_REQUESTS}")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 5.0:
        raise SystemExit("--timeout must be finite, >0, and <=5 seconds")

    title = title or module.name
    unique_dids = tuple(dict.fromkeys(metric.did for metric in metrics))
    print(f"ACTIVE LIVE-DATA PLAN: {module.key} ({module.name})")
    print(
        f"session=10 03; DIDs={' '.join(f'{did:04X}' for did in unique_dids) or '(none)'}; "
        f"duration<={args.seconds:g}s; rate<={args.rate:g}/s; requests<={args.max_requests}"
    )
    if not args.execute:
        print("DRY RUN: no preflight, lock, CAN socket, interface change, or transmission occurred.")
        return 0
    if (
        not args.confirm_parked
        or not args.confirm_engine_off
        or not args.confirm_session_change
        or not args.confirm_no_active_routine
        or not args.pair
        or not args.conditions
    ):
        raise SystemExit(
            "--execute requires --confirm-parked, --confirm-engine-off, "
            "--confirm-session-change, --confirm-no-active-routine, --pair, and --conditions"
        )

    errors = preflight(module.channel, module.bitrate)
    if errors:
        raise SystemExit("live-data preflight failed: " + "; ".join(errors))
    try:
        lock = diagnostic_safety.acquire_channel_lock(module.channel)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"refusing to start live diagnostics: {exc}") from None

    link = None
    fatal_error = None
    interrupted = False
    restored_passive = False
    terminal_active = False
    with diagnostic_safety.interrupt_on_termination() as termination:
        try:
            link = Link(
                module,
                request_rate=args.rate,
                max_requests=args.max_requests,
                timeout=args.timeout,
            )
            tick = 0
            deadline = time.monotonic() + args.seconds
            sys.stdout.write(ALT_ON + CUR_OFF)
            terminal_active = True
            while time.monotonic() < deadline and link.request_attempts < args.max_requests:
                tick += 1
                started = time.monotonic()
                render(link, metrics, title, spec_deg, interval, tick)
                elapsed = time.monotonic() - started
                if elapsed < interval:
                    remaining = max(0.0, deadline - time.monotonic())
                    sleep_for = min(interval - elapsed, remaining)
                    if sleep_for > 0:
                        time.sleep(sleep_for)
        except KeyboardInterrupt:
            interrupted = True
        except Exception as exc:
            fatal_error = f"{type(exc).__name__}: {exc}"
        finally:
            termination.begin_cleanup()
            try:
                if link is not None:
                    link.drop()
            except Exception as exc:
                if fatal_error is None:
                    fatal_error = f"link close failed: {type(exc).__name__}: {exc}"
            finally:
                try:
                    restored_passive = bool(canbus.restore_passive(module.channel, module.bitrate))
                except Exception as exc:
                    if fatal_error is None:
                        fatal_error = f"passive restore failed: {type(exc).__name__}: {exc}"
                finally:
                    diagnostic_safety.release_channel_lock(lock)
            if terminal_active:
                sys.stdout.write(CUR_ON + ALT_OFF)
                sys.stdout.flush()

    if termination.received_signal is not None:
        interrupted = True

    print(f"adapter restored passive: {'yes' if restored_passive else 'NO - CHECK IT NOW'}")
    if fatal_error:
        raise SystemExit(f"live diagnostics aborted: {fatal_error}")
    if not restored_passive:
        raise SystemExit("live diagnostics ended but passive restoration could not be verified")
    return 130 if interrupted else 0
