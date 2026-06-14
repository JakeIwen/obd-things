"""Registry of diagnostic modules on the vehicle.

To extend to a new module: add a Module entry here, then add a live_data/<module>.py that
supplies its metric table, and reuse the generic tools/ scanners by passing the module key.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Module:
    key: str
    name: str
    txid: int           # tester -> ECU CAN id (29-bit normal-fixed)
    rxid: int           # ECU -> tester CAN id
    channel: str = "can0"


MODULES = {
    "radar_acc": Module(
        key="radar_acc",
        name="Bosch ACC radar (DASM / MRR1evo)",
        txid=0x18DA2AF1,
        rxid=0x18DAF12A,
    ),
    # e.g. add more modules here as the project expands:
    # "pcm": Module("pcm", "Powertrain Control Module", 0x18DA10F1, 0x18DAF110),
}


def get(key):
    try:
        return MODULES[key]
    except KeyError:
        raise SystemExit(f"unknown module '{key}'. known: {', '.join(MODULES)}")
