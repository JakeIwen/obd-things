"""Fail-closed state shared by unattended CAN helpers and external-tool campaigns.

The cooperative SocketCAN channel lock cannot see a separate scan tool such as AlfaOBD over an
OBDLink.  This module adds two small, same-boot records under the repository's ignored lock tree:

* explicit external-operation inhibits (for example ``alfaobd``);
* the last explicitly set or passively observed physical bus topology.

Records from an earlier boot are ignored: a reboot ends live diagnostic sessions and invalidates
physical-topology assumptions. Missing, malformed, or unknown topology never authorizes a wake.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile

from lib import diagnostic_safety


STATE_DIR = Path(diagnostic_safety.LOCK_DIR) / "operation-state"
VALID_BUSES = frozenset(("c-can", "b-can", "can-ch", "unknown"))
SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class TopologyState:
    channel: str
    bus: str
    pair: str
    source: str
    note: str
    boot_id: str
    updated_at: str
    usable: bool
    reason: str


def _validate_name(value: str, label: str) -> str:
    if not isinstance(value, str) or not SAFE_NAME.fullmatch(value):
        raise ValueError(f"unsafe {label}: {value!r}")
    return value


def _validate_inhibit_channel(value: str) -> str:
    # A literal wildcard is permitted only in inhibit payloads. It is never
    # used in a path or passed to a subprocess, and lets a same-boot safety
    # latch survive ephemeral SocketCAN renumbering.
    if value == "*":
        return value
    return _validate_name(value, "channel")


def _bounded_text(value: str, label: str, *, maximum: int = 240) -> str:
    if not isinstance(value, str) or "\n" in value or "\r" in value or len(value) > maximum:
        raise ValueError(f"invalid {label}")
    return value


def current_boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return ""
    return value if SAFE_NAME.fullmatch(value) else ""


def _topology_path(channel: str) -> Path:
    return STATE_DIR / f"topology-{_validate_name(channel, 'channel')}.json"


def _inhibit_path(name: str) -> Path:
    return STATE_DIR / f"inhibit-{_validate_name(name, 'inhibit name')}.json"


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def set_topology(
    channel: str,
    bus: str,
    *,
    pair: str = "",
    source: str,
    note: str = "",
) -> TopologyState:
    channel = _validate_name(channel, "channel")
    if bus not in VALID_BUSES:
        raise ValueError(f"unsupported bus {bus!r}; choose from {sorted(VALID_BUSES)}")
    pair = _bounded_text(pair, "pair", maximum=32)
    source = _bounded_text(source, "source", maximum=80)
    note = _bounded_text(note, "note")
    boot_id = current_boot_id()
    if not boot_id:
        raise RuntimeError("cannot establish current boot id")
    payload = {
        "channel": channel,
        "bus": bus,
        "pair": pair,
        "source": source,
        "note": note,
        "boot_id": boot_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_json(_topology_path(channel), payload)
    return load_topology(channel)


def _unknown_topology(channel: str, reason: str) -> TopologyState:
    return TopologyState(
        channel=channel,
        bus="unknown",
        pair="",
        source="",
        note="",
        boot_id="",
        updated_at="",
        usable=False,
        reason=reason,
    )


def load_topology(channel: str) -> TopologyState:
    channel = _validate_name(channel, "channel")
    path = _topology_path(channel)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _unknown_topology(channel, "topology record missing")
    except (OSError, json.JSONDecodeError):
        return _unknown_topology(channel, "topology record unreadable or malformed")
    if not isinstance(payload, dict):
        return _unknown_topology(channel, "topology record is not an object")
    boot_id = current_boot_id()
    if not boot_id or payload.get("boot_id") != boot_id:
        return _unknown_topology(channel, "topology record is from another boot")
    if payload.get("channel") != channel or payload.get("bus") not in VALID_BUSES:
        return _unknown_topology(channel, "topology record fields are invalid")
    bus = str(payload["bus"])
    usable = bus in ("c-can", "b-can", "can-ch")
    return TopologyState(
        channel=channel,
        bus=bus,
        pair=str(payload.get("pair", "")),
        source=str(payload.get("source", "")),
        note=str(payload.get("note", "")),
        boot_id=boot_id,
        updated_at=str(payload.get("updated_at", "")),
        usable=usable,
        reason="" if usable else "topology is explicitly unknown",
    )


def begin_inhibit(
    name: str,
    *,
    channel: str,
    reason: str,
) -> dict[str, object]:
    name = _validate_name(name, "inhibit name")
    channel = _validate_inhibit_channel(channel)
    reason = _bounded_text(reason, "reason")
    boot_id = current_boot_id()
    if not boot_id:
        raise RuntimeError("cannot establish current boot id")
    payload: dict[str, object] = {
        "name": name,
        "channel": channel,
        "reason": reason,
        "boot_id": boot_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_json(_inhibit_path(name), payload)
    return payload


def end_inhibit(name: str) -> bool:
    path = _inhibit_path(name)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def active_inhibits(channel: str) -> tuple[dict[str, object], ...]:
    channel = _validate_name(channel, "channel")
    boot_id = current_boot_id()
    if not boot_id:
        return ({"name": "boot-id-unavailable", "channel": channel},)
    try:
        entries = sorted(STATE_DIR.glob("inhibit-*.json"))
    except OSError:
        return ({"name": "inhibit-state-unreadable", "channel": channel},)
    active: list[dict[str, object]] = []
    for path in entries:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            active.append({"name": path.name, "channel": channel, "invalid": True})
            continue
        if not isinstance(payload, dict):
            active.append({"name": path.name, "channel": channel, "invalid": True})
            continue
        if payload.get("boot_id") != boot_id:
            continue
        if payload.get("channel") in (channel, "*"):
            active.append(payload)
    return tuple(active)


def all_active_inhibits() -> tuple[dict[str, object], ...]:
    """Return every same-boot inhibit without inventing a netdev identity."""

    boot_id = current_boot_id()
    if not boot_id:
        return ({"name": "boot-id-unavailable", "channel": "*"},)
    try:
        entries = sorted(STATE_DIR.glob("inhibit-*.json"))
    except OSError:
        return ({"name": "inhibit-state-unreadable", "channel": "*"},)
    active: list[dict[str, object]] = []
    for path in entries:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            active.append({"name": path.name, "channel": "*", "invalid": True})
            continue
        if not isinstance(payload, dict):
            active.append({"name": path.name, "channel": "*", "invalid": True})
            continue
        if payload.get("boot_id") == boot_id:
            active.append(payload)
    return tuple(active)


def status(channel: str) -> dict[str, object]:
    topology = load_topology(channel)
    return {
        "topology": asdict(topology),
        "active_inhibits": active_inhibits(channel),
    }
