"""Fixed display-frame metadata shared by the broker and receive thread.

This pure module avoids importing the receiver's recorder/ownership machinery
back into the broker merely to share its source allowlist.
"""

from projects.vehicle_data import ccan_powertrain as cp, lighting


DISPLAY_SOURCES = frozenset((cp.SPEED_LIMIT_SOURCE, cp.ACC_DISPLAY_SOURCE, lighting.SOURCE))
DISPLAY_FRAME_DLCS = {cp.SPEED_LIMIT_ID: 4, cp.ACC_DISPLAY_ID: 8, lighting.FRAME_ID: lighting.DLC}
DISPLAY_FRAME_IDS = tuple(DISPLAY_FRAME_DLCS)
