import errno
from hashlib import sha256
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
import socket
import struct
import subprocess
from types import SimpleNamespace

import pytest

from projects.vehicle_data.usb_recovery_contract import LINK_HELPER_SHA256

SOURCE = (Path(__file__).resolve().parents[1] / "projects" / "vehicle_data"
          / "usb_recovery_link" / "vanpi-can-link-by-index")
LOADER = SourceFileLoader("vanpi_can_link_by_index", str(SOURCE))
helper = module_from_spec(spec_from_loader(LOADER.name, LOADER))
LOADER.exec_module(helper)

UP_CAPTURE = bytes.fromhex(
    "000000009f8601000100000001000000480012000700010063616e003c000200"
    "08000600000000002400010020a1070000000000000000000000000000000000"
    "0000000000000000000000000c0005002a00000002000000"
)
DOWN_CAPTURE = bytes.fromhex("000000009f8601000000000001000000")


@pytest.fixture(autouse=True)
def linux_constants_for_scripted_netlink(monkeypatch):
    # The standalone command runs on Linux, but the packet tests also run on macOS.
    # Supply only missing constants to the loaded helper; a socket factory is always injected.
    if not all(hasattr(socket, name) for name in ("AF_NETLINK", "NETLINK_ROUTE")):
        monkeypatch.setattr(helper, "socket", SimpleNamespace(
            AF_NETLINK=getattr(socket, "AF_NETLINK", 16),
            NETLINK_ROUTE=getattr(socket, "NETLINK_ROUTE", 0),
            AF_UNSPEC=socket.AF_UNSPEC, SOCK_RAW=socket.SOCK_RAW,
        ))


class FakeSocket:
    def __init__(self, error=0, *, damage=None):
        self.error = error
        self.damage = damage
        self.requests = []
        self.opened = False

    def __call__(self, family, kind, protocol):
        assert (family, kind, protocol) == (16, socket.SOCK_RAW, 0)
        self.opened = True
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def connect(self, address):
        assert address == (0, 0)

    def settimeout(self, seconds):
        assert seconds == 2.0

    def send(self, request):
        self.requests.append(request)
        return len(request)

    def recvfrom(self, _):
        if self.damage == "timeout":
            raise TimeoutError
        (request,) = self.requests
        seq = struct.unpack_from("=I", request, 8)[0]
        data = struct.pack("=IHHIIi", 36, 2, 0x100, seq, 1500, -self.error) + request[:16]
        if self.damage == "short":
            data = data[:-1]
        if self.damage == "sequence":
            data = data[:8] + struct.pack("=I", seq + 1) + data[12:]
        return data, (0 if self.damage != "userspace" else 1000, 0)


def run(argv, sock, *, monotonic=lambda: 10.0):
    alarms = []
    code = helper.main(argv, socket_factory=sock, monotonic=monotonic, alarm=alarms.append)
    assert alarms == [3, 0]
    return code


def test_reviewed_helper_source_is_pinned():
    assert LINK_HELPER_SHA256 == sha256(SOURCE.read_bytes()).hexdigest()


@pytest.mark.parametrize(("source_delta", "installed_delta", "verb", "code"), [
    (b"", b"", "status", 0),
    (b"changed", b"changed", "status", 1),  # Two matching copies are not enough.
    (b"", b"changed", "status", 1),
    (b"changed", b"", "install", 1),
])
def test_installer_uses_contract_pin_before_reporting_verified_or_installing(
    tmp_path, source_delta, installed_delta, verb, code,
):
    source = tmp_path / "source"
    installed = tmp_path / "installed"
    contents = SOURCE.read_bytes()
    source.write_bytes(contents + source_delta)
    installed.write_bytes(contents + installed_delta)
    # Replace the pathname and privilege boundaries, not the hashing or shell dispatch.
    command = r'''
        . "$1"
        source_file=$2
        program=$3
        stat() { printf '0:0:755\n'; }
        if ! command -v sha256sum >/dev/null; then
            sha256sum() { shasum -a 256; }
        fi
        sudo() { printf 'unexpected privileged dispatch\n' >&2; exit 99; }
        main "$4"
    '''
    result = subprocess.run(
        ["bash", "-c", command, "test-installer", str(SOURCE.with_name("install.sh")),
         str(source), str(installed), verb],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == code, result.stderr
    if code == 0:
        assert result.stdout == f"verified: {installed}\n"
    else:
        expected_file = source if source_delta else installed
        assert result.stderr == f"differs from the contract's SHA-256 pin: {expected_file}\n"


@pytest.mark.parametrize(("argv", "capture"), [
    (["up-listen-only", "99999", "500000"], UP_CAPTURE),
    (["down", "99999"], DOWN_CAPTURE),
])
def test_request_body_matches_saved_iproute2_capture(argv, capture):
    sock = FakeSocket()
    assert run(argv + ["--not-after", "12"], sock) == 0
    (request,) = sock.requests
    assert struct.unpack_from("=IHHII", request) == (len(request), 16, 0x05, 1, 0)
    assert request[16:] == capture


def attrs(data):
    values = {}
    while data:
        size, kind = struct.unpack_from("=HH", data)
        assert 4 <= size <= len(data)
        assert kind not in values
        values[kind] = data[4:size]
        data = data[(size + 3) & ~3:]
    return values


@pytest.mark.parametrize("index,bitrate", [(1, 10000), (987, 125000), (2**31 - 1, 1000000)])
def test_other_requests_have_only_an_index_and_can_options(index, bitrate):
    sock = FakeSocket()
    assert run(["up-listen-only", str(index), str(bitrate), "--not-after", "12"], sock) == 0
    (request,) = sock.requests
    assert struct.unpack_from("=BBHiII", request, 16) == (0, 0, 0, index, 1, 1)
    link = attrs(request[32:])
    assert set(link) == {18}  # Only IFLA_LINKINFO, no device name or alternative name.
    info = attrs(link[18])
    assert info[1] == b"can"
    assert attrs(info[2]) == {
        1: struct.pack("=8I", bitrate, 0, 0, 0, 0, 0, 0, 0),
        5: struct.pack("=II", 0x2A, 0x02),
        6: struct.pack("=I", 0),
    }
    sock = FakeSocket()
    assert run(["down", str(index), "--not-after", "12"], sock) == 0
    (request,) = sock.requests
    assert request[16:] == struct.pack("=BBHiII", 0, 0, 0, index, 0, 1)


@pytest.mark.parametrize("argv", [
    ["down", "7"],
    ["down", "7", "--not-after", "inf"],
    ["down", "7", "--not-after", "nan"],
    ["down", "7", "--not-after", "12", "extra"],
    *(["down", bad, "--not-after", "12"] for bad in ("abc", "0", "-1", "2147483648", "0x1", "١")),
    *(["up-listen-only", "7", bad, "--not-after", "12"] for bad in ("9999", "1000001", "+500000")),
])
def test_invalid_arguments_do_not_open_a_socket(argv):
    sock = FakeSocket()
    assert run(argv, sock) == 2
    assert not sock.opened


def test_elapsed_deadline_refuses_before_sending_even_if_socket_opening_took_time():
    sock = FakeSocket()
    assert run(["down", "7", "--not-after", "9"], sock) == 5
    assert not sock.opened
    ticking = iter([10.0, 12.0])
    assert run(["down", "7", "--not-after", "12"], sock, monotonic=lambda: next(ticking)) == 5
    assert sock.opened
    assert sock.requests == []


@pytest.mark.parametrize("error", [errno.ENODEV, errno.EBUSY, errno.EPERM])
def test_kernel_refusal_is_distinct_from_an_unknown_result(error, capsys):
    assert run(["down", "7", "--not-after", "12"], FakeSocket(error)) == 3
    assert capsys.readouterr().out == f"errno={error} {errno.errorcode[error]}\n"


@pytest.mark.parametrize("damage", ["timeout", "short", "sequence", "userspace"])
def test_no_matching_complete_kernel_ack_is_uncertain(damage):
    assert run(["down", "7", "--not-after", "12"], FakeSocket(damage=damage)) == 4
