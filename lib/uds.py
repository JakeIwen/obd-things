"""Generic UDS-over-ISO-TP helpers for the vehicle bus (PCAN/SocketCAN, 11- or 29-bit).

Shared by the live_data viewers and the tools/scanners. Nothing module-specific lives here;
addressing is always passed in (see lib/modules.py). This transport is safety-neutral: callers
choose the payload, so a caller can read, change session, start a routine, write, or actuate.
"""
import math
import time
import struct
import subprocess

from lib import diagnostic_safety
from lib.modules import NORMAL_11BITS, NORMAL_29BITS

try:
    import isotp
except ImportError:
    raise SystemExit("Missing dependency: pip3 install --break-system-packages can-isotp")

DEFAULT_CHANNEL = "can0"
DEFAULT_BITRATE = 500000
DEFAULT_ADDRESSING_MODE = NORMAL_29BITS

ISOTP_ADDRESSING_MODES = {
    NORMAL_29BITS: isotp.AddressingMode.Normal_29bits,
    NORMAL_11BITS: isotp.AddressingMode.Normal_11bits,
}

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


def negative_response_details(response):
    """Return structured UDS negative-response metadata, or ``None`` if not a valid 0x7F."""
    if response is None or len(response) < 3 or response[0] != 0x7F:
        return None
    return {
        "request_sid": f"{response[1]:02X}",
        "nrc": f"{response[2]:02X}",
        "nrc_name": NRC.get(response[2], "unknown"),
    }


# --- socket / link management -----------------------------------------------
def _positive_finite_timeout(value, name):
    """Normalize a timeout without accepting bools or numeric strings."""
    if isinstance(value, (bool, str, bytes, bytearray)):
        raise ValueError(f"{name} must be a positive finite number")
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a positive finite number") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def _resolve_addressing_mode(addressing_mode):
    try:
        return ISOTP_ADDRESSING_MODES[addressing_mode]
    except KeyError:
        choices = ", ".join(sorted(ISOTP_ADDRESSING_MODES))
        raise ValueError(f"unsupported addressing_mode {addressing_mode!r}; choose {choices}") from None


def open_socket(txid, rxid, channel=DEFAULT_CHANNEL, timeout=2.0,
                addressing_mode=DEFAULT_ADDRESSING_MODE, tx_padding=None):
    """Open an ISO-TP socket for explicit CAN IDs and addressing mode.

    ``addressing_mode`` is a repository-level string so module definitions do not depend on
    python-can-isotp. The default preserves the original 29-bit behavior.
    """
    isotp_mode = _resolve_addressing_mode(addressing_mode)
    if tx_padding is not None and (
        not isinstance(tx_padding, int)
        or isinstance(tx_padding, bool)
        or not 0 <= tx_padding <= 0xFF
    ):
        raise ValueError("tx_padding must be a byte between 0x00 and 0xFF, or None")
    s = isotp.socket()
    try:
        s.set_fc_opts(stmin=0, bs=0)          # stream FC: stmin 0, bs 0
        if tx_padding is not None:
            s.set_opts(txpad=tx_padding)
        s.bind(channel, address=isotp.Address(
            isotp_mode, txid=txid, rxid=rxid))
        s.settimeout(timeout)
        return s
    except BaseException:
        try:
            s.close()
        except Exception:
            pass
        raise


def open_module_socket(module, timeout=2.0, channel=None, tx_padding=None):
    """Open a socket using all transport metadata from a module registry entry."""
    return open_socket(
        module.txid,
        module.rxid,
        channel=channel or module.channel,
        timeout=timeout,
        addressing_mode=module.addressing_mode,
        tx_padding=tx_padding,
    )


def _bring_up_can_locked(channel, bitrate, lock_handle):
    """Mutate one CAN interface after proving that the caller owns its advisory lock."""
    diagnostic_safety.validate_channel_lock(lock_handle, channel)
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


def bring_up_can(channel=DEFAULT_CHANNEL, bitrate=DEFAULT_BITRATE, *, lock_handle=None):
    """Best-effort: configure an armed CAN interface without racing a diagnostic holder.

    A caller that already owns the full active-diagnostic lifecycle passes the exact handle from
    :func:`lib.diagnostic_safety.acquire_channel_lock`. Otherwise this helper takes the nonblocking
    per-channel lock for the interface mutation and releases it before returning. Lock contention
    fails closed as ``False``; an invalid supplied capability is a programming error and raises.
    """
    if lock_handle is not None:
        return _bring_up_can_locked(channel, bitrate, lock_handle)
    try:
        with diagnostic_safety.channel_lock(channel) as acquired_lock:
            return _bring_up_can_locked(channel, bitrate, acquired_lock)
    except diagnostic_safety.ChannelLockError:
        return False


def _recover_socket_locked(txid, rxid, channel, bitrate, max_wait, timeout,
                           addressing_mode, lock_handle):
    """Recovery loop with one validated lock held across every interface mutation."""
    diagnostic_safety.validate_channel_lock(lock_handle, channel)
    deadline = time.monotonic() + max_wait
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if bring_up_can(channel, bitrate, lock_handle=lock_handle):
            try:
                return open_socket(txid, rxid, channel, timeout, addressing_mode)
            except OSError:
                pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))
    raise RuntimeError(f"{channel} did not come back within {max_wait:g}s; replug the adapter")


def recover_socket(txid, rxid, channel=DEFAULT_CHANNEL, bitrate=DEFAULT_BITRATE,
                   max_wait=60, timeout=0.6, addressing_mode=DEFAULT_ADDRESSING_MODE,
                   *, lock_handle=None):
    """Wait boundedly for a dropped adapter, reconfigure it, and reopen ISO-TP.

    Recovery mutates the whole SocketCAN interface, so a long-lived diagnostic caller must pass
    its held channel-lock capability. A standalone caller may omit it; recovery then owns the lock
    for the complete wait/reconfigure/open loop. ``max_wait`` uses a monotonic, finite deadline.
    """
    max_wait = _positive_finite_timeout(max_wait, "max_wait")
    timeout = _positive_finite_timeout(timeout, "timeout")
    if lock_handle is not None:
        return _recover_socket_locked(
            txid, rxid, channel, bitrate, max_wait, timeout, addressing_mode, lock_handle
        )
    with diagnostic_safety.channel_lock(channel) as acquired_lock:
        return _recover_socket_locked(
            txid, rxid, channel, bitrate, max_wait, timeout, addressing_mode, acquired_lock
        )


def recover_module_socket(module, max_wait=60, timeout=0.6, channel=None, *, lock_handle=None):
    """Recover the CAN link using bitrate and addressing metadata from a module entry."""
    return recover_socket(
        module.txid,
        module.rxid,
        channel=channel or module.channel,
        bitrate=module.bitrate,
        max_wait=max_wait,
        timeout=timeout,
        addressing_mode=module.addressing_mode,
        lock_handle=lock_handle,
    )


def drain(sock):
    """Discard stale/late frames so a previous timeout can't desync the next read."""
    previous_timeout = sock.gettimeout()
    sock.settimeout(0)                         # non-blocking
    try:
        while True:
            try:
                if not sock.recv():
                    break
            except (BlockingIOError, TimeoutError):
                break
    finally:
        sock.settimeout(previous_timeout)


# --- request / classify ------------------------------------------------------
def request(sock, payload, timeout=2.0, retries=0, response_pending_timeout=None,
            max_pending_responses=64):
    """Send raw UDS bytes and return ``(response_or_None, status)``.

    Empty/initial-timeout responses are retried only when the caller explicitly requests it;
    the safe default is no replay because payloads are arbitrary. Once the ECU sends NRC
    0x78 responsePending, the request is never resent: it was already accepted, and replaying an
    arbitrary caller payload could duplicate a mutation. Repeated pending replies are bounded by
    both an absolute monotonic deadline and ``max_pending_responses``. The count is an allowance:
    exactly ``max_pending_responses`` matching pending replies are accepted while waiting for a
    final response, and the request is abandoned only if an (N+1)th pending reply arrives.
    """
    if isinstance(payload, (bool, int)):
        raise TypeError("payload must be a bytes-like object or iterable of byte values")
    request_payload = bytes(payload)
    if not request_payload:
        raise ValueError("payload must contain at least one UDS service byte")

    timeout = _positive_finite_timeout(timeout, "timeout")
    if response_pending_timeout is None:
        response_pending_timeout = max(5.0, timeout * 5.0)
    response_pending_timeout = _positive_finite_timeout(
        response_pending_timeout, "response_pending_timeout"
    )
    if (
        not isinstance(max_pending_responses, int)
        or isinstance(max_pending_responses, bool)
        or max_pending_responses <= 0
    ):
        raise ValueError("max_pending_responses must be a positive integer")
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
        raise ValueError("retries must be a non-negative integer")

    for _ in range(retries + 1):
        # A previous receive may have left a short/depleted timeout on this socket. Apply the
        # caller's validated per-attempt bound before send as well as before each receive.
        sock.settimeout(timeout)
        sock.send(request_payload)
        deadline = time.monotonic() + timeout
        pending_deadline = None
        pending_count = 0
        while True:
            active_deadline = pending_deadline if pending_deadline is not None else deadline
            remaining = active_deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                resp = sock.recv()
            except TimeoutError:
                resp = None
            if not resp:
                break
            if (
                len(resp) >= 3
                and resp[0] == 0x7F
                and resp[1] == request_payload[0]
                and resp[2] == 0x78
            ):  # responsePending for this request SID
                pending_count += 1
                if pending_deadline is None:
                    pending_deadline = time.monotonic() + response_pending_timeout
                if pending_count > max_pending_responses:
                    return None, (
                        "NO_RESPONSE (responsePending count limit: allowance "
                        f"{max_pending_responses} exceeded by reply {pending_count})"
                    )
                continue
            return bytes(resp), classify(request_payload, resp)
        if pending_deadline is not None:
            return None, (
                "NO_RESPONSE (responsePending deadline exceeded after "
                f"{response_pending_timeout:g}s)"
            )
    return None, "NO_RESPONSE (timeout/empty after retries)"


def classify(req, resp):
    if not resp:
        return "EMPTY"
    if resp[0] == 0x7F:
        if len(resp) < 3:
            return f"MALFORMED_NEGATIVE ({hx(resp)})"
        nrc = resp[2]
        return f"NEGATIVE 7F sid={resp[1]:02X} nrc={nrc:02X} ({NRC.get(nrc, '?')})"
    if resp[0] == (req[0] + 0x40):
        return "POSITIVE"
    return "UNEXPECTED"
