"""Generic UDS-over-ISO-TP helpers for the vehicle bus (PCAN/SocketCAN, 29-bit normal-fixed).

Shared by the live_data viewers and the tools/ scanners. Nothing module-specific lives here -
addressing is always passed in (see lib/modules.py). Reads are safe; this module never starts
routines or writes.
"""
import time
import struct
import subprocess

try:
    import isotp
except ImportError:
    raise SystemExit("Missing dependency: pip3 install --break-system-packages can-isotp")

DEFAULT_CHANNEL = "can0"
DEFAULT_BITRATE = 500000

# UDS negative-response codes we care about
NRC = {
    0x10: "generalReject", 0x11: "serviceNotSupported", 0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat", 0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError", 0x31: "requestOutOfRange",
    0x33: "securityAccessDenied", 0x35: "invalidKey", 0x78: "responsePending",
    0x7E: "subFunctionNotSupportedInActiveSession", 0x7F: "serviceNotSupportedInActiveSession",
}


# --- byte decoders for metric definitions -----------------------------------
def s16(b, o=0):  # signed 16-bit big-endian
    return struct.unpack_from(">h", b, o)[0]


def s32(b, o=0):  # signed 32-bit big-endian
    return struct.unpack_from(">i", b, o)[0]


def u8(b, o=0):   # unsigned 8-bit
    return b[o]


def hx(b):
    return " ".join(f"{x:02X}" for x in b)


# --- socket / link management -----------------------------------------------
def open_socket(txid, rxid, channel=DEFAULT_CHANNEL, timeout=2.0):
    s = isotp.socket()
    s.set_fc_opts(stmin=0, bs=0)              # stream FC: stmin 0, bs 0
    s.bind(channel, address=isotp.Address(
        isotp.AddressingMode.Normal_29bits, txid=txid, rxid=rxid))
    s.settimeout(timeout)
    return s


def bring_up_can(channel=DEFAULT_CHANNEL, bitrate=DEFAULT_BITRATE):
    """Best-effort: ensure the interface is UP at bitrate, listen-only OFF (the flag is sticky)."""
    try:
        if subprocess.run(["ip", "link", "show", channel], capture_output=True).returncode != 0:
            return False
        subprocess.run(["sudo", "ip", "link", "set", channel, "down"], capture_output=True)
        r = subprocess.run(["sudo", "ip", "link", "set", channel, "up", "type", "can",
                            "bitrate", str(bitrate), "listen-only", "off"], capture_output=True)
        time.sleep(0.3)
        return r.returncode == 0
    except Exception:
        return False


def recover_socket(txid, rxid, channel=DEFAULT_CHANNEL, bitrate=DEFAULT_BITRATE,
                   max_wait=60, timeout=0.6):
    """Wait for the interface to come back after a USB drop, re-up it, re-open the socket."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if bring_up_can(channel, bitrate):
            try:
                return open_socket(txid, rxid, channel, timeout)
            except OSError:
                pass
        time.sleep(1.0)
    raise RuntimeError(f"{channel} did not come back within {max_wait}s; replug the adapter")


def drain(sock):
    """Discard stale/late frames so a previous timeout can't desync the next read."""
    sock.settimeout(0)                         # non-blocking
    try:
        while True:
            try:
                if not sock.recv():
                    break
            except (BlockingIOError, OSError):
                break
    finally:
        sock.settimeout(0.5)


# --- request / classify ------------------------------------------------------
def request(sock, payload, timeout=2.0, retries=1):
    """Send raw UDS bytes; return (response_bytes_or_None, status_str). Handles 0x78 pending
    and retries empty/timeout responses."""
    sock.settimeout(timeout)
    for _ in range(retries + 1):
        sock.send(bytes(payload))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = sock.recv()
            except Exception:
                resp = None
            if not resp:
                break
            if len(resp) >= 3 and resp[0] == 0x7F and resp[2] == 0x78:   # responsePending
                deadline = time.time() + timeout
                continue
            return bytes(resp), classify(payload, resp)
    return None, "NO_RESPONSE (timeout/empty after retries)"


def classify(req, resp):
    if not resp:
        return "EMPTY"
    if resp[0] == 0x7F:
        nrc = resp[2] if len(resp) >= 3 else None
        return f"NEGATIVE 7F sid={resp[1]:02X} nrc={nrc:02X} ({NRC.get(nrc, '?')})"
    if resp[0] == (req[0] + 0x40):
        return "POSITIVE"
    return "UNEXPECTED"
