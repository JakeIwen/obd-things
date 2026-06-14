"""Base live-data viewer: a `top`-style display driven by a module's address + a metric table.

Per-module scripts (e.g. radar_acc.py) define a list of Metric(...) and call run(). This file is
generic - it knows nothing about any particular ECU. Reads only (ReadDataByIdentifier), safe.
"""
import os
import sys
import time
import struct
import shutil
from collections import namedtuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from lib import uds
from lib.uds import s16, s32, u8        # re-export so metric tables can `from ... import s16,...`

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


def reading_cell(val, unit, spec_deg):
    """Colourised, fixed-width (W_VAL visible chars) reading. Angle units coloured by spec."""
    if unit.startswith("deg"):
        s = f"{val:+{W_VAL}.3f}"
        color = GRN if abs(val) <= spec_deg else (YEL if abs(val) <= 1.5 * spec_deg else RED)
    else:
        s = f"{val:{W_VAL}.3f}"
        color = CYA
    return color + s + RST


class Link:
    """Owns the socket + diagnostic session for one module; reconnects on USB/bus errors."""
    def __init__(self, module):
        self.m = module
        self.sock = None
        self.connected = False
        self.last_tp = 0.0

    def ensure(self):
        if self.sock is not None:
            return True
        try:
            self.sock = uds.open_socket(self.m.txid, self.m.rxid, self.m.channel, timeout=0.5)
        except OSError:
            if not uds.bring_up_can(self.m.channel):
                return False
            try:
                self.sock = uds.open_socket(self.m.txid, self.m.rxid, self.m.channel, 0.5)
            except OSError:
                self.sock = None
                return False
        try:
            uds.request(self.sock, [0x10, 0x03], timeout=1.0)   # extended session
        except OSError:
            # interface down / bus gone - try to bring it up, retry next cycle (shows NO DATA)
            uds.bring_up_can(self.m.channel)
            self.drop()
            return False
        self.last_tp = time.time()
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
        if not self.ensure():
            return None
        hi, lo = (did >> 8) & 0xFF, did & 0xFF
        try:
            uds.drain(self.sock)
            if time.time() - self.last_tp > 2.0:
                uds.request(self.sock, [0x3E, 0x00], timeout=0.3)   # TesterPresent
                self.last_tp = time.time()
            self.sock.send(bytes([0x22, hi, lo]))
            deadline = time.time() + 0.6
            while time.time() < deadline:
                try:
                    r = self.sock.recv()
                except Exception:
                    break
                if not r:
                    break
                if r[0] == 0x7F and len(r) >= 3 and r[2] == 0x78:
                    deadline = time.time() + 0.6
                    continue
                if r[0] == 0x62 and len(r) >= 3 and r[1] == hi and r[2] == lo:
                    self.connected = True
                    return bytes(r[3:])
        except OSError:
            self.drop()
        return None


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
    lines.append(f"{ts}  refresh {1.0/interval:.1f} Hz  cycle {tick}  status {status}")
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


def run(module, metrics, title=None, spec_deg=1.0, refresh_hz=5.0):
    """Drive the live view. Optional CLI arg overrides the refresh interval (seconds)."""
    interval = 1.0 / refresh_hz
    if len(sys.argv) > 1:
        try:
            interval = float(sys.argv[1])
        except ValueError:
            sys.exit(f"usage: {sys.argv[0]} [refresh_seconds]")
    title = title or module.name
    link = Link(module)
    tick = 0
    sys.stdout.write(ALT_ON + CUR_OFF)
    try:
        while True:
            tick += 1
            t0 = time.time()
            render(link, metrics, title, spec_deg, interval, tick)
            dt = time.time() - t0
            if dt < interval:
                time.sleep(interval - dt)
    except KeyboardInterrupt:
        pass
    finally:
        link.drop()
        sys.stdout.write(CUR_ON + ALT_OFF)
        sys.stdout.flush()
