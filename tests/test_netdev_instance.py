from pathlib import Path

import pytest

from lib.netdev_instance import NetdevInstance, first_line, ifindex_from_link_reply
from lib.netdev_instance import present_ifindexes, read_instance


@pytest.fixture
def sysfs(tmp_path):
    names = tmp_path / "class" / "net"
    names.mkdir(parents=True)
    usb = tmp_path / "devices" / "usb1" / "1-1.4.2"
    netdev = usb / "1-1.4.2:1.0" / "net" / "can0"
    netdev.mkdir(parents=True)
    for filename, value in {"idVendor": "1d50", "idProduct": "606f", "serial": "BOARD-A",
                            "busnum": "1", "devnum": "96"}.items():
        (usb / filename).write_text(value + "\n")
    (netdev / "ifindex").write_text("9\n")
    (netdev / "dev_id").write_text("0x1\n")
    (names / "can0").symlink_to(netdev)
    return names, usb, netdev


def test_reads_one_usb_netdev_instance(sysfs):
    names, usb, netdev = sysfs
    assert read_instance("can0", sys_class_net_root=names) == NetdevInstance(
        "can0", 9, str(netdev), usb.name, 1, 96, "BOARD-A", 1,
    )


@pytest.mark.parametrize("change", ["renumber", "vanish"])
def test_ifindex_must_survive_the_whole_read(sysfs, monkeypatch, change):
    names, _, netdev = sysfs
    original = Path.read_text

    def read(path, *args, **kwargs):
        content = original(path, *args, **kwargs)
        if path == netdev / "dev_id":
            if change == "renumber":
                (netdev / "ifindex").write_text("12\n")
            else:
                (netdev / "ifindex").unlink()
                (names / "can0").unlink()
        return content

    monkeypatch.setattr(Path, "read_text", read)
    assert read_instance("can0", sys_class_net_root=names) is None


def test_ancestry_requires_usb_identity(sysfs):
    names, usb, _ = sysfs
    (usb / "serial").unlink()
    assert read_instance("can0", sys_class_net_root=names) is None


def test_inventory_includes_non_can_and_refuses_partial_evidence(sysfs):
    names, _, _ = sysfs
    loopback = names / "lo"
    loopback.mkdir()
    (loopback / "ifindex").write_text("1\n")
    assert present_ifindexes(sys_class_net_root=names) == {1, 9}
    (loopback / "ifindex").write_text("invalid\n")
    assert present_ifindexes(sys_class_net_root=names) is None
    (loopback / "ifindex").unlink()
    assert present_ifindexes(sys_class_net_root=names) is None
    assert present_ifindexes(sys_class_net_root=names / "missing") is None


@pytest.mark.parametrize(("text", "expected"), [
    ("9: can0@if4: <NOARP,UP,LOWER_UP> mtu 16\n    can <LISTEN-ONLY>",
     (9, {"NOARP", "UP", "LOWER_UP"})),
    ("9: can0: <> mtu 16", (9, set())),
    ("9: can0: mtu 16\n    can <LISTEN-ONLY>", (9, None)),
    ("9: can0: <NOARP, UP> mtu 16", (9, None)),
    ("9: can0: <NOARP mtu 16", (9, None)),
    ("\n9: can0: <NOARP> mtu 16", None),
    ("0: can0: <NOARP>", None),
])
def test_ifindex_and_flags_come_only_from_the_first_line(text, expected):
    assert first_line(text) == expected
    assert ifindex_from_link_reply(text) == (expected[0] if expected is not None else None)
