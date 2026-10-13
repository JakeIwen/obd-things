"""Pure fixed profiles and reply decoding for parked PCM support checks."""

from __future__ import annotations

import struct

from lib.modules import MODULES
from projects.vehicle_data import pcm_electrical as pcm


TEMPERATURE_DIDS = (0xF45C, 0x069F)
OIL_LIFE_DIDS = (0x2185,)
CHECK_PROFILES = {
    "temperature": TEMPERATURE_DIDS,
    "oil-life": OIL_LIFE_DIDS,
}


def request_bytes(did: int) -> bytes:
    """Return the one permitted padded physical ReadDataByIdentifier frame."""
    if not any(did in profile for profile in CHECK_PROFILES.values()):
        raise ValueError("DID is outside the fixed support-check profiles")
    return bytes((3, 0x22, did >> 8, did & 255, 0, 0, 0, 0))



def build_plan(profile: str, timeout_seconds: float, handoff_wait_seconds: float) -> dict:
    dids = CHECK_PROFILES[profile]
    return {
        "profile": profile,
        "module": "pcm",
        "bus": "c-can",
        "pair": "6/14",
        "request_id": "18DA10F1",
        "response_id": "18DAF110",
        "requests": [request_bytes(did).hex(" ").upper() for did in dids],
        "maximum_requests": len(dids),
        "timeout_seconds": timeout_seconds,
        "retries": 0,
        "session_change": False,
        "flow_control": False,
        "handoff_wait_seconds": handoff_wait_seconds,
    }



def decode_reply(did: int, frame: bytes) -> dict:
    if did not in (*TEMPERATURE_DIDS, 0x2185):
        raise ValueError("DID is outside the fixed support-check profiles")
    if len(frame) != pcm.CAN_FRAME_SIZE:
        raise ValueError("malformed SocketCAN response length")
    can_id, dlc, padded = struct.unpack(pcm.CAN_FRAME_FORMAT, frame)
    if can_id != (pcm.CAN_EFF_FLAG | MODULES["pcm"].rxid) or not 1 <= dlc <= 8:
        raise ValueError("wrong PCM response identity, flags, or DLC")
    length = padded[0]
    if length not in (3, 4) or dlc < length + 1:
        raise ValueError("response must be a complete SingleFrame; no FlowControl sent")
    payload = padded[1:1 + length]
    if length == 3 and payload[:2] == b"\x7f\x22":
        return {"status": "negative_response", "nrc": f"{payload[2]:02X}",
                "response_hex": payload.hex(" ").upper()}
    if payload[:3] != bytes((0x62, did >> 8, did & 255)) or length != 4:
        raise ValueError("response does not match the exact requested DID and one-byte shape")
    if did == 0x2185:
        if payload[3] > 100:
            raise ValueError("oil-life byte is outside 0..100 percent; not clamped")
        return {"status": "positive", "response_hex": payload.hex(" ").upper(),
                "raw": payload[3], "value_percent": payload[3],
                "scale_basis": "observed_alfa_scale"}
    value_c = payload[3] - (64 if did == 0x069F else 40)
    return {"status": "positive", "response_hex": payload.hex(" ").upper(),
            "raw": payload[3], "value_c": value_c, "value_f": value_c * 1.8 + 32,
            "scale_basis": "observed_alfa_scale" if did == 0x069F else "standardized_candidate"}
