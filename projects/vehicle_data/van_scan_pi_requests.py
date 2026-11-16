"""Fixed Pi requests excluded from harvested in-vehicle scans."""

from typing import Mapping

# The Pi's reviewed fixed helper requests (target address -> request payloads).  The Pi also uses
# tester address F1, so these are recognised by content.  Kept in sync with
# pcm_electrical.py, active_drive.py, radar_alignment.py and bcan_auxiliary.py by the tests.
PI_FIXED_REQUESTS: Mapping[int, frozenset[bytes]] = {
    0x10: frozenset(bytes.fromhex(h) for h in ("22 01 A1", "22 06 DA", "22 06 9F", "22 21 85")),
    0xC7: frozenset(bytes.fromhex(h) for h in ("22 31 D0", "22 31 D1", "22 31 D2", "22 31 D3")),
    0x2A: frozenset(bytes.fromhex(h) for h in ("22 08 45", "19 02 8F")),
    0x40: frozenset((bytes.fromhex("19 02 8F"),)),
    0x85: frozenset((bytes.fromhex("22 20 01"),)),
}
# The Pi's DTC tooling (lib/dtc_batch.py, tools/dtc_inventory.py) may address any module.
PI_DTC_TOOL_REQUESTS = frozenset(
    bytes.fromhex(h) for h in ("19 02 FF", "19 01 FF", "19 03", "19 0A")
)

