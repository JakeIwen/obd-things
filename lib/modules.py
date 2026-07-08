"""Registry of diagnostic modules on the vehicle. SOURCE OF TRUTH for module addressing.

To extend to a new module: add a Module entry here, then reuse the generic tools/ scanners by
passing the module key. For a live view, copy projects/radar/radar_acc_live.py (it imports the
base viewer from live_data/live_data.py) and swap the key + metric table. Per-target work and
docs live under projects/<name>/. Broadcast frames + wake behavior per bus: docs/bus-map.md.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Module:
    key: str
    name: str
    txid: int           # tester -> ECU CAN id (29-bit normal-fixed)
    rxid: int           # ECU -> tester CAN id
    channel: str = "can0"
    bus: str = "c-can"  # which physical bus (c-can 500k / b-can 125k) — see docs/bus-map.md
    note: str = ""      # operational quirk a caller should know (power gating, ignition state, etc.)


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
    # e.g. add more modules here as the project expands:
    # "pcm": Module("pcm", "Powertrain Control Module", 0x18DA10F1, 0x18DAF110),
    # BCM (not yet added): C-CAN 18DA40F1/18DAF140 for ignition-by-diag routine; also 11-bit UDS
    #   on B-CAN (needs an 11-bit Module variant). Actuation is power-mode gated. See docs/bus-map.md.
}


def get(key):
    try:
        return MODULES[key]
    except KeyError:
        raise SystemExit(f"unknown module '{key}'. known: {', '.join(MODULES)}")
