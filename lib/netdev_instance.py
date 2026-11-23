"""Read kernel instance identity separately from stable CAN role identity."""

import re
from dataclasses import dataclass
from pathlib import Path

_SYS_CLASS_NET = Path("/sys/class/net")
_LINK_HEADER = re.compile(r"\s*([0-9]+):\s+\S+:\s*(.*)")
_LINK_FLAGS = re.compile(r"<((?:[A-Za-z0-9_-]+(?:,[A-Za-z0-9_-]+)*)?)>(?:\s|$)")


@dataclass(frozen=True)
class NetdevInstance:
    channel: str
    ifindex: int
    netdev_sysfs_path: str
    usb_devpath: str
    usb_busnum: int
    usb_devnum: int
    usb_serial: str
    dev_id: int


def _ifindex(entry: Path) -> int:
    value = int((entry / "ifindex").read_text(), 10)
    if not 1 <= value < 2**31:
        raise ValueError("invalid ifindex")
    return value


def _usb_ancestor(path: Path) -> Path | None:
    # Match the resolver's USB identity rule without invoking its driver/role inventory.
    return next((parent for parent in path.parents if all(
        (parent / name).is_file() for name in ("idVendor", "idProduct", "serial")
    )), None)


def read_instance(
    channel: str, *, sys_class_net_root: str | Path = _SYS_CLASS_NET,
) -> NetdevInstance | None:
    if not channel or Path(channel).name != channel or channel in {".", ".."}:
        return None
    entry = Path(sys_class_net_root) / channel
    try:
        index = _ifindex(entry)
        netdev = entry.resolve(strict=True)
        usb = _usb_ancestor(netdev)
        if usb is None:
            return None
        busnum = int((usb / "busnum").read_text(), 10)
        devnum = int((usb / "devnum").read_text(), 10)
        serial = (usb / "serial").read_text().strip()
        dev_id = int((netdev / "dev_id").read_text(), 0)
        if not serial or min(busnum, devnum) < 1 or dev_id < 0 or _ifindex(entry) != index:
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return NetdevInstance(channel, index, str(netdev), usb.name, busnum, devnum, serial, dev_id)


def present_ifindexes(*, sys_class_net_root: str | Path = _SYS_CLASS_NET) -> set[int] | None:
    """Return a complete inventory or no evidence at all."""
    try:
        return {_ifindex(entry) for entry in Path(sys_class_net_root).iterdir()}
    except (OSError, ValueError):
        return None


def first_line(text: str) -> tuple[int, frozenset[str] | None] | None:
    """Parse the first link header; missing or malformed flags never mean DOWN."""
    header = _LINK_HEADER.fullmatch(text.partition("\n")[0])
    if header is None:
        return None
    try:
        index = int(header[1])
    except ValueError:
        return None
    if not 1 <= index < 2**31:
        return None
    match = _LINK_FLAGS.match(header[2])
    if match is None:
        return index, None
    return index, frozenset(match[1].split(",")) if match[1] else frozenset()


def ifindex_from_link_reply(text: str) -> int | None:
    header = first_line(text)
    return header[0] if header is not None else None
