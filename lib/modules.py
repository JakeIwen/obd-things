"""Registry of diagnostic modules on the vehicle. SOURCE OF TRUTH for module addressing.

To extend to a new module: add a Module entry here, then reuse the generic tools/ scanners by
passing the module key. For a live view, make a thin wrapper that passes this entry and a metric
table to live_data/live_data.py; do not copy radar-specific follow logic. Per-target work and
docs live under projects/<name>/. Broadcast frames + wake behavior per bus: docs/bus-map.md.
"""
from dataclasses import dataclass


NORMAL_29BITS = "normal_29bits"
NORMAL_11BITS = "normal_11bits"
ADDRESSING_MODES = frozenset((NORMAL_29BITS, NORMAL_11BITS))


@dataclass(frozen=True)
class Module:
    key: str
    name: str
    txid: int           # tester -> ECU CAN id
    rxid: int           # ECU -> tester CAN id
    channel: str = "can0"
    bus: str = "c-can"  # physical-bus key; bitrate is explicit below; see docs/bus-map.md
    note: str = ""      # operational quirk a caller should know (power gating, ignition state, etc.)
    bitrate: int = 500000
    addressing_mode: str = NORMAL_29BITS

    def __post_init__(self):
        if self.addressing_mode not in ADDRESSING_MODES:
            choices = ", ".join(sorted(ADDRESSING_MODES))
            raise ValueError(f"unsupported addressing_mode {self.addressing_mode!r}; choose {choices}")
        max_id = 0x7FF if self.addressing_mode == NORMAL_11BITS else 0x1FFFFFFF
        for field_name in ("txid", "rxid"):
            can_id = getattr(self, field_name)
            if not isinstance(can_id, int) or isinstance(can_id, bool) or not 0 <= can_id <= max_id:
                width = 11 if self.addressing_mode == NORMAL_11BITS else 29
                raise ValueError(f"{field_name} must be a {width}-bit CAN identifier")
        if self.txid == self.rxid:
            raise ValueError("txid and rxid must be different for physical ISO-TP addressing")
        if not isinstance(self.bitrate, int) or isinstance(self.bitrate, bool) or self.bitrate <= 0:
            raise ValueError("bitrate must be a positive integer")


MODULES = {
    "radar_acc": Module(
        key="radar_acc",
        name="Bosch ACC radar (DASM / MRR1evo)",
        txid=0x18DA2AF1,
        rxid=0x18DAF12A,
        bus="c-can",
        note="ACKs frames even with ignition cut mid-sweep; speed only via DID 0x1002 (no OBD PIDs behind SGW).",
    ),
    "rf_hub": Module(
        key="rf_hub",
        name="Radio Frequency Hub (RFH, Continental) - TPMS/RKE",
        txid=0x18DAC7F1,
        rxid=0x18DAF1C7,
        bus="c-can",
        note="Answers with ignition OFF (battery-powered RKE receiver).",
    ),
    "tcm": Module(
        key="tcm",
        name="Transmission Control Module (ZF 948TE / 9HP48)",
        txid=0x18DA18F1,
        rxid=0x18DAF118,
        bus="c-can",
        note=(
            "Live-verified 2026-07-19 ignition-on; F187=46342086, F192=ES11-1065 D, "
            "F194/F132=68532161AF. FCA's J2534 report maps 68532161AF to 2022 VF 948TE."
        ),
    ),
    "shifter": Module(
        key="shifter",
        name="SILATECH Electronic Shifter Module",
        txid=0x18DA1FF1,
        rxid=0x18DAF11F,
        bus="c-can",
        note="Live-verified 2026-07-19 ignition-on; F187 returned P7FK46LXHAD.",
    ),
    "bcm_ccan": Module(
        key="bcm_ccan",
        name="Body Control Module (C-CAN diagnostic endpoint)",
        txid=0x18DA40F1,
        rxid=0x18DAF140,
        bus="c-can",
        note="Live-verified 2026-07-19 ignition-on; F187 returned 68524831AF. Actuation is power-mode gated.",
    ),
    "cluster": Module(
        key="cluster",
        name="Marelli Instrument Panel Cluster (IPC)",
        txid=0x18DA60F1,
        rxid=0x18DAF160,
        bus="c-can",
        note=(
            "Live-verified 2026-07-19 ignition-on; F187=68517084AD and F192=50019990002. "
            "FCA's NHTSA Part 573 filing identifies 68517084AD as the Marelli IPC."
        ),
    ),
    "telematics": Module(
        key="telematics",
        name="Global Telematics Box Module (TBM2)",
        txid=0x18DAC6F1,
        rxid=0x18DAF1C6,
        bus="c-can",
        note=(
            "Live-verified 2026-07-19 ignition-on; F192=TBM200A11P and F132=68510377AC. "
            "Exact role is high-confidence from the TBM identifier, OEM TBM2 docs, and Mopar part catalog."
        ),
    ),
    # e.g. add more modules here as the project expands:
    # PCM 0x10 is AlfaOBD-observed but did not answer our 22 F187 or 1A87 default-session probes
    # on 2026-07-19; do not register it until independently verified from this tap.
    # BCM's C-CAN endpoint is registered above. A separate 11-bit body-bus endpoint was observed,
    # but its physical pair, bitrate, and exact TX/RX pairing remain unresolved. Register it only
    # after a repinned passive survey and explicit pairing evidence; see docs/bus-map.md.
}


def get(key):
    try:
        return MODULES[key]
    except KeyError:
        raise SystemExit(f"unknown module '{key}'. known: {', '.join(MODULES)}")
