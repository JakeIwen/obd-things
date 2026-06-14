"""Registry of diagnostic modules on the vehicle.

To extend to a new module: add a Module entry here, then reuse the generic tools/ scanners by
passing the module key. For a live view, copy projects/radar/radar_acc_live.py (it imports the
base viewer from live_data/live_data.py) and swap the key + metric table. Per-target work and
docs live under projects/<name>/.
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
