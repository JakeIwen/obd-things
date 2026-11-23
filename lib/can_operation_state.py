"""Fail-closed state shared by unattended CAN helpers and external-tool campaigns.

The cooperative SocketCAN channel lock cannot see a separate scan tool such as AlfaOBD over an
OBDLink.  This module adds two small, same-boot records under the repository's ignored lock tree:

* explicit external-operation inhibits (for example ``alfaobd``);
* the last explicitly set or passively observed physical bus topology.

Records from an earlier boot are ignored: a reboot ends live diagnostic sessions and invalidates
physical-topology assumptions. Missing, malformed, or unknown topology never authorizes a wake.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Literal

from lib import diagnostic_safety


STATE_DIR = Path(diagnostic_safety.LOCK_DIR) / "operation-state"
VALID_BUSES = frozenset(("c-can", "b-can", "can-ch", "unknown"))
SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]+")
WRITER_LOCK_WAIT_SECONDS = 2.0
WRITER_LOCK_END_WAIT_SECONDS = 10.0
_WRITER_LOCK_POLL_SECONDS = 0.02
_WRITER_LOCK_NAME = ".inhibit-writers.lock"
_FALLBACK_PREFIX = "nolock."
_FALLBACK_BOOT = re.compile(r"[0-9a-fA-F]{32}")
_FALLBACK_FILE = re.compile(r"inhibit-nolock\.([0-9a-fA-F]{32})\.[0-9a-f]{64}\.json")
_EndOutcome = Literal["removed", "mismatch", "unavailable", "expired", "partial"]
_LockOutcome = Literal["held", "unavailable", "expired"]


class InhibitStateBusy(OSError):
    """The writer lock could not be acquired; no inhibits were ended."""


@dataclass(frozen=True)
class EndResult:
    outcome: _EndOutcome
    removed: tuple[str, ...] = ()
    durable: bool = True
    detail: str = ""
    late: bool = False


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


@contextmanager
def _writer_lock(
    deadline_monotonic: float, *, monotonic: Callable[[], float] = time.monotonic,
) -> Iterator[_LockOutcome]:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(STATE_DIR / _WRITER_LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o640)
    except OSError:
        yield "unavailable"
        return
    try:
        while monotonic() <= deadline_monotonic:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    yield "unavailable"
                    return
            else:
                yield "held" if monotonic() <= deadline_monotonic else "expired"
                return
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_WRITER_LOCK_POLL_SECONDS, remaining))
        yield "expired"
    finally:
        os.close(descriptor)


def _canonical_inhibit_path(name: str) -> Path:
    path = _inhibit_path(name)
    if name.startswith(_FALLBACK_PREFIX):
        raise ValueError(f"reserved inhibit name: {name!r}")
    return path


def _boot_component(boot_id: str) -> str | None:
    component = boot_id.replace("-", "")
    return component if _FALLBACK_BOOT.fullmatch(component) else None


def _fallback_name(name: str, boot_component: str) -> str:
    return f"{_FALLBACK_PREFIX}{boot_component}.{hashlib.sha256(name.encode('ascii')).hexdigest()}"


def _prune_old_fallbacks(boot_component: str | None) -> None:
    if boot_component is None:
        return
    # A current-boot fallback writer never publishes to any of these paths.
    for path in STATE_DIR.glob(f"inhibit-{_FALLBACK_PREFIX}*.json"):
        match = _FALLBACK_FILE.fullmatch(path.name)
        if match and match[1].lower() != boot_component.lower():
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # Housekeeping must not prevent a current-boot safety write.
                continue


def begin_inhibit(
    name: str,
    *,
    channel: str,
    reason: str,
) -> dict[str, object]:
    path = _canonical_inhibit_path(name)
    channel = _validate_inhibit_channel(channel)
    reason = _bounded_text(reason, "reason")
    boot_id = current_boot_id()
    if not boot_id:
        raise RuntimeError("cannot establish current boot id")
    boot_component = _boot_component(boot_id)
    payload: dict[str, object] = {
        "name": name,
        "channel": channel,
        "reason": reason,
        "boot_id": boot_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with _writer_lock(time.monotonic() + WRITER_LOCK_WAIT_SECONDS) as lock:
        if lock == "held":
            _atomic_json(path, payload)
            _prune_old_fallbacks(boot_component)
            return payload
    if boot_component is None:
        raise RuntimeError("cannot establish current boot id for fallback inhibit")
    fallback = _fallback_name(name, boot_component)
    payload.update(
        name=fallback, stands_for=name, channel="*",
        reason=f"{reason} [written without the inhibit writer lock; requested channel {channel}]",
    )
    _atomic_json(_inhibit_path(fallback), payload)
    return payload


def _unlink_inhibit(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def end_inhibit(name: str) -> bool:
    path = _inhibit_path(name)
    with _writer_lock(time.monotonic() + WRITER_LOCK_END_WAIT_SECONDS) as lock:
        if lock != "held":
            raise InhibitStateBusy("cannot acquire inhibit writer lock; no inhibits ended")
        boot_component = _boot_component(current_boot_id())
        removed = _unlink_inhibit(path)
        if boot_component is not None and not name.startswith(_FALLBACK_PREFIX):
            removed_fallback = _unlink_inhibit(_inhibit_path(_fallback_name(name, boot_component)))
            removed = removed or removed_fallback
        _prune_old_fallbacks(boot_component)
        return removed


def inhibit_is_for(record: Mapping[str, object], name: str) -> bool:
    return record.get("name") == name or record.get("stands_for") == name


def _same_boot_inhibits(
    boot_id: str, invalid_channel: str = "*",
) -> Iterator[tuple[Path, dict[str, object]]]:
    # Unlike glob, iterdir reports an unreadable directory rather than an empty match.
    try:
        entries = sorted(STATE_DIR.iterdir())
    except FileNotFoundError:
        return
    for path in entries:
        if not (path.name.startswith("inhibit-") and path.name.endswith(".json")):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            yield path, {"name": path.name, "channel": invalid_channel, "invalid": True}
        elif payload.get("boot_id") == boot_id:
            yield path, payload


def _inhibit_difference(expected_paths: Mapping[Path, dict], boot_id: str) -> str | None:
    unmatched = dict(expected_paths)
    for path, payload in _same_boot_inhibits(boot_id):
        if payload.get("invalid") is True or unmatched.pop(path, None) != payload:
            return path.name
    return next((path.name for path in unmatched), None)


def _end_compared_inhibits(
    paths: Mapping[Path, dict], directory_fd: int, deadline_monotonic: float,
    monotonic: Callable[[], float],
) -> EndResult:
    # No unlink is issued after the deadline. A removal cannot be retracted (readers act on the
    # record's absence), so one that finishes late is reported with late=True, never re-created.
    removed: list[str] = []
    late = False
    for path in paths:
        name = path.stem.removeprefix('inhibit-')
        if monotonic() > deadline_monotonic:
            return EndResult("partial" if removed else "expired", tuple(removed), detail=name, late=late)
        try:
            os.unlink(path)
        except OSError as exc:
            outcome = "mismatch" if isinstance(exc, FileNotFoundError) else "unavailable"
            return EndResult("partial" if removed else outcome, tuple(removed), detail=name, late=late)
        removed.append(name)
        late = monotonic() > deadline_monotonic
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            outcome = "partial" if len(removed) < len(paths) else "removed"
            return EndResult(outcome, tuple(removed), durable=False, detail=str(exc), late=late)
    return EndResult("removed", tuple(removed), late=late)


def end_inhibits_if_exactly(
    expected: Mapping[str, dict], *, deadline_monotonic: float, monotonic=time.monotonic,
) -> EndResult:
    """End only the exact current-boot record set, in the supplied order."""
    paths = {name: _canonical_inhibit_path(name) for name in expected}
    expected_paths = {path: expected[name] for name, path in paths.items()}
    with _writer_lock(deadline_monotonic, monotonic=monotonic) as lock:
        if lock != "held":
            return EndResult(lock)
        boot_id = current_boot_id()
        if not boot_id:
            return EndResult("unavailable", detail="cannot establish current boot id")
        try:
            difference = _inhibit_difference(expected_paths, boot_id)
            if difference is not None:
                return EndResult("mismatch", detail=difference)
            directory_fd = os.open(STATE_DIR, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            return EndResult("unavailable", detail=str(exc))
        try:
            return _end_compared_inhibits(expected_paths, directory_fd, deadline_monotonic, monotonic)
        finally:
            os.close(directory_fd)


def active_inhibits(channel: str) -> tuple[dict[str, object], ...]:
    channel = _validate_name(channel, "channel")
    boot_id = current_boot_id()
    if not boot_id:
        return ({"name": "boot-id-unavailable", "channel": channel},)
    try:
        return tuple(
            payload for _, payload in _same_boot_inhibits(boot_id, invalid_channel=channel)
            if payload.get("channel") in (channel, "*")
        )
    except OSError:
        return ({"name": "inhibit-state-unreadable", "channel": channel},)


def all_active_inhibits() -> tuple[dict[str, object], ...]:
    """Return every same-boot inhibit without inventing a netdev identity."""
    boot_id = current_boot_id()
    if not boot_id:
        return ({"name": "boot-id-unavailable", "channel": "*"},)
    try:
        return tuple(payload for _, payload in _same_boot_inhibits(boot_id))
    except OSError:
        return ({"name": "inhibit-state-unreadable", "channel": "*"},)


def status(channel: str) -> dict[str, object]:
    topology = load_topology(channel)
    return {
        "topology": asdict(topology),
        "active_inhibits": active_inhibits(channel),
    }
