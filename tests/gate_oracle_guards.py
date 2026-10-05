"""Defensive process guards and output normalisation for ``tests/gate_oracle.py``.

Everything here is installed *before* any repository module is imported so that
module-level ``from x import y`` bindings and function default arguments also
capture the guarded objects.  Nothing in this module ever opens a socket,
starts a process, or writes outside an explicitly allowed scratch root.

Sentinels are ``BaseException`` subclasses on purpose: every gated tool wraps
its route/arm call in ``except (OSError, RuntimeError, ValueError)`` or
``except Exception``; a ``BaseException`` sentinel passes through those
handlers unchanged, so reaching it proves that all preceding CLI gates passed.
"""

from __future__ import annotations

import builtins
import dataclasses
import enum
import io
import os
import pathlib
import re
import shlex
import socket
import sqlite3
import subprocess
import sys
import tempfile


class ReachedCanBoundary(BaseException):
    """A CAN/diagnostic transport, route acquisition, or CAN-tool process was reached."""

    def __init__(self, boundary: str, detail: object = None):
        super().__init__(boundary)
        self.boundary = boundary
        self.detail = detail


class BlockedSideEffect(BaseException):
    """A non-CAN side effect (write outside the sandbox, other process) was refused."""

    def __init__(self, kind: str, detail: object = None):
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


CAN_COMMANDS = frozenset({"ip", "candump", "cansend", "sudo", "systemctl", "adb"})

EVENTS: list[dict] = []          # every boundary/blocked event in the current run
_STATE = {
    "active": False,
    "allowed_roots": (),
    "repo": None,
}

_ORIG = {}


# --------------------------------------------------------------------------
# summarising arbitrary values deterministically
# --------------------------------------------------------------------------
class Normaliser:
    def __init__(self):
        self.replacements: list[tuple[str, str]] = []
        self.pid = str(os.getpid())

    def add(self, path: str | os.PathLike, token: str) -> None:
        text = os.fspath(path)
        for variant in {text, os.path.realpath(text), os.path.abspath(text)}:
            if variant and variant != "/":
                self.replacements.append((variant, token))
        # Longest first so nested roots are replaced before their parents.
        self.replacements.sort(key=lambda item: -len(item[0]))

    _ISO = re.compile(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    )
    _STAMP = re.compile(r"\b\d{8}[_T]\d{6}(?:_\d{6})?(?:Z|[+-]\d{4})?")
    _ADDR = re.compile(r" at 0x[0-9a-fA-F]+")
    _EPOCH = re.compile(r"\b1[6-9]\d{8}\.\d+\b")
    _CLAIM = re.compile(r"\.claiming-<PID>-[0-9a-f]{12}")

    def text(self, value: str) -> str:
        if not isinstance(value, str):
            return value
        for old, new in self.replacements:
            value = value.replace(old, new)
        value = re.sub(rf"(?<![0-9]){re.escape(self.pid)}(?![0-9])", "<PID>", value)
        value = self._ISO.sub("<TS>", value)
        value = self._STAMP.sub("<STAMP>", value)
        value = self._ADDR.sub(" at 0x<ADDR>", value)
        value = self._EPOCH.sub("<EPOCH>", value)
        value = self._CLAIM.sub(".claiming-<PID>-<HEX>", value)
        return value

    def value(self, value, depth: int = 0):
        if depth > 8:
            return "<depth>"
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if value == value and value not in (float("inf"), float("-inf")) else repr(value)
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {"bytes_hex": bytes(value).hex()}
        if isinstance(value, pathlib.PurePath):
            return {"path": self.text(str(value))}
        if isinstance(value, enum.Enum):
            return f"{type(value).__name__}.{value.name}"
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {
                "dataclass": type(value).__qualname__,
                "fields": {
                    field.name: self.value(getattr(value, field.name), depth + 1)
                    for field in dataclasses.fields(value)
                },
            }
        if isinstance(value, dict):
            return {str(key): self.value(item, depth + 1) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(item, depth + 1) for item in value]
        if isinstance(value, (set, frozenset)):
            return sorted((self.value(item, depth + 1) for item in value), key=repr)
        if callable(value):
            module = getattr(value, "__module__", None) or "?"
            name = getattr(value, "__qualname__", None) or type(value).__qualname__
            return {"callable": f"{module}.{name}"}
        return {"object": type(value).__qualname__, "repr": self.text(repr(value))}


NORM = Normaliser()


def _event(kind: str, name: str, detail) -> None:
    EVENTS.append({"kind": kind, "name": name, "detail": NORM.value(detail)})


def fire_boundary(name: str, args=(), kwargs=None) -> None:
    detail = {"args": list(args), "kwargs": dict(kwargs or {})}
    _event("ReachedCanBoundary", name, detail)
    raise ReachedCanBoundary(name, NORM.value(detail))


def block(kind: str, detail) -> None:
    _event("BlockedSideEffect", kind, detail)
    raise BlockedSideEffect(kind, NORM.value(detail))


# --------------------------------------------------------------------------
# path policy
# --------------------------------------------------------------------------
def _resolve(path) -> str | None:
    if isinstance(path, int):
        return None
    try:
        text = os.fsdecode(os.fspath(path))
    except TypeError:
        return None
    if not os.path.isabs(text):
        text = os.path.join(os.getcwd(), text)
    return os.path.realpath(text)


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def check_write(path, operation: str) -> None:
    if not _STATE["active"]:
        return
    resolved = _resolve(path)
    if resolved is None:
        block("write-unresolvable", {"operation": operation, "path": repr(path)})
    if resolved in ("/dev/null",):
        return
    for root in _STATE["allowed_roots"]:
        if _inside(resolved, root):
            return
    repo = _STATE["repo"]
    where = "repo" if repo and _inside(resolved, repo) else "outside-sandbox"
    block(f"write:{where}", {"operation": operation, "path": resolved})


def _mode_writes(mode: str) -> bool:
    return any(character in mode for character in "wax+")


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


# --------------------------------------------------------------------------
# guard installation
# --------------------------------------------------------------------------
def _classify_argv(args) -> tuple[str, str]:
    if isinstance(args, (str, bytes)):
        text = os.fsdecode(args)
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()
    else:
        tokens = [os.fsdecode(os.fspath(item)) if not isinstance(item, str) else item for item in args]
    first = os.path.basename(tokens[0]) if tokens else ""
    if first == os.path.basename(sys.executable) or first.startswith("python"):
        first = "python"
    return first, " ".join(tokens)


def _subprocess_gate(api: str, args, kwargs) -> None:
    name, _ = _classify_argv(args)
    argv = args if not isinstance(args, (str, bytes)) else [os.fsdecode(args)]
    detail = {"api": api, "argv": [str(item) for item in argv]}
    if name in CAN_COMMANDS:
        _event("ReachedCanBoundary", f"subprocess:{name}", detail)
        raise ReachedCanBoundary(f"subprocess:{name}", NORM.value(detail))
    block(f"subprocess:{name}", detail)


def install_guards(repo: str, allowed_roots: list[str]) -> None:
    """Install every guard once, before importing repository code."""
    if _ORIG:
        return
    _STATE["repo"] = os.path.realpath(repo)
    set_allowed_roots(allowed_roots)

    # ---- sockets -------------------------------------------------------
    _ORIG["socket.socket"] = socket.socket

    class GuardSocket(socket.socket):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):  # noqa: D401 - guard
            fire_boundary("socket.socket", args, kwargs)

    socket.socket = GuardSocket
    for name in ("socketpair", "create_connection", "create_server", "fromfd"):
        if hasattr(socket, name):
            _ORIG[f"socket.{name}"] = getattr(socket, name)

            def guarded(*args, _name=name, **kwargs):
                fire_boundary(f"socket.{_name}", args, kwargs)

            setattr(socket, name, guarded)

    # ---- processes -----------------------------------------------------
    _ORIG["subprocess.Popen"] = subprocess.Popen

    class GuardPopen(subprocess.Popen):  # type: ignore[misc]
        def __init__(self, args, *pargs, **kwargs):
            _subprocess_gate("subprocess.Popen", args, kwargs)

        def __del__(self):  # never constructed
            pass

    subprocess.Popen = GuardPopen
    for name in ("run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"):
        _ORIG[f"subprocess.{name}"] = getattr(subprocess, name)

        def guarded(args=None, *pargs, _name=name, **kwargs):
            if args is None:
                args = kwargs.get("args", [])
            _subprocess_gate(f"subprocess.{_name}", args, kwargs)

        setattr(subprocess, name, guarded)
    for name in (
        "system", "popen", "execv", "execve", "execl", "execle", "execlp", "execlpe",
        "execvp", "execvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv",
        "spawnve", "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp", "fork", "forkpty",
    ):
        if hasattr(os, name):
            _ORIG[f"os.{name}"] = getattr(os, name)

            def guarded(*args, _name=name, **kwargs):
                block(f"os.{_name}", {"args": [str(item) for item in args[:3]]})

            setattr(os, name, guarded)

    # ---- filesystem writes ----------------------------------------------
    original_open = builtins.open
    _ORIG["open"] = original_open

    def guarded_open(file, mode="r", *args, **kwargs):
        if _mode_writes(mode) and not isinstance(file, int):
            check_write(file, f"open({mode!r})")
        return original_open(file, mode, *args, **kwargs)

    builtins.open = guarded_open
    io.open = guarded_open

    original_os_open = os.open
    _ORIG["os.open"] = original_os_open

    def guarded_os_open(path, flags, *args, **kwargs):
        if _STATE["active"] and flags & _WRITE_FLAGS:
            if kwargs.get("dir_fd") is not None:
                block("write:dir_fd", {"path": str(path)})
            check_write(path, "os.open")
        return original_os_open(path, flags, *args, **kwargs)

    os.open = guarded_os_open

    def wrap_path_op(name: str, arity: int) -> None:
        original = getattr(os, name)
        _ORIG[f"os.{name}"] = original

        def guarded(*args, **kwargs):
            if _STATE["active"] and (
                kwargs.get("dir_fd") is not None or kwargs.get("src_dir_fd") is not None
            ):
                block(f"os.{name}:dir_fd", {"args": [str(item) for item in args]})
            for item in args[:arity]:
                check_write(item, f"os.{name}")
            return original(*args, **kwargs)

        setattr(os, name, guarded)

    for name, arity in (
        ("mkdir", 1), ("makedirs", 1), ("rmdir", 1), ("unlink", 1), ("remove", 1),
        ("replace", 2), ("rename", 2), ("renames", 2), ("link", 2), ("symlink", 2),
        ("chmod", 1), ("chown", 1), ("truncate", 1), ("utime", 1), ("mkfifo", 1),
    ):
        if hasattr(os, name):
            wrap_path_op(name, arity)

    original_connect = sqlite3.connect
    _ORIG["sqlite3.connect"] = original_connect

    def guarded_connect(database, *args, **kwargs):
        text = os.fspath(database) if not isinstance(database, int) else ""
        if isinstance(text, bytes):
            text = os.fsdecode(text)
        read_only = kwargs.get("uri") and "mode=ro" in text
        if text not in ("", ":memory:") and not read_only:
            check_write(text.removeprefix("file:").split("?", 1)[0], "sqlite3.connect")
        return original_connect(database, *args, **kwargs)

    sqlite3.connect = guarded_connect


def set_allowed_roots(roots: list[str]) -> None:
    _STATE["allowed_roots"] = tuple(os.path.realpath(root) for root in roots)


def activate(flag: bool) -> None:
    _STATE["active"] = bool(flag)


def original(name: str):
    return _ORIG[name]


def patch_repo_boundaries(record: list | None = None) -> list[str]:
    """Patch every CAN transport / route acquisition entry point in loaded repo modules.

    Returns the dotted names that were patched (for the JSON header).
    """
    patched: list[str] = []
    def make(name: str):
        def guarded(*args, **kwargs):
            fire_boundary(name, args, kwargs)

        guarded.__qualname__ = f"guard[{name}]"
        return guarded

    targets: list[tuple[str, str]] = []
    try:
        import isotp  # noqa: WPS433
    except ImportError:
        isotp = None
    if isotp is not None:
        _ORIG["isotp.socket"] = isotp.socket
        isotp.socket = make("isotp.socket")
        patched.append("isotp.socket")

    for module_name in (
        "lib.uds", "lib.can_runtime_route", "lib.diagnostic_safety", "lib.can_wake",
        "lib.can_handoff", "lib.canbus",
    ):
        module = sys.modules.get(module_name)
        if module is None:
            try:
                module = __import__(module_name, fromlist=["_"])
            except Exception:  # pragma: no cover - refactor may remove modules
                continue
        for attribute in sorted(vars(module)):
            value = getattr(module, attribute)
            if not callable(value) or isinstance(value, type):
                continue
            is_target = attribute.startswith("acquire_") or (
                module_name == "lib.uds" and attribute in ("open_socket", "open_module_socket")
            ) or (
                module_name == "lib.diagnostic_safety"
                and attribute in ("_acquire_channel_lock", "channel_lock", "channel_observer_lock")
            ) or (
                module_name == "lib.can_wake" and attribute in ("wake_once", "_open_wake_session")
            ) or (
                module_name == "lib.can_handoff" and attribute in ("active_turn", "passive_turn")
            ) or (
                module_name == "lib.canbus" and attribute in ("probe_ids", "identify_bus", "ip_up")
            )
            if not is_target or getattr(value, "__module__", module_name) != module_name:
                continue
            targets.append((module_name, attribute))
    replacements = {}
    for module_name, attribute in targets:
        module = sys.modules[module_name]
        value = getattr(module, attribute)
        dotted = f"{module_name}.{attribute}"
        replacement = make(dotted)
        # Keep the original alive inside the mapping so its id() can never be
        # reused by a later object while aliases are being rebound.
        replacements[id(value)] = (value, replacement)
        setattr(module, attribute, replacement)
        patched.append(dotted)
    rebind_aliases(replacements)
    if record is not None:
        record.extend(patched)
    _ORIG["__replacements__"] = replacements
    return sorted(set(patched))


def rebind_aliases(replacements: dict | None = None) -> None:
    """Replace ``from lib.x import f`` aliases held by any already-imported repo module."""
    replacements = replacements if replacements is not None else _ORIG.get("__replacements__", {})
    repo = _STATE["repo"]
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if not path or not _inside(os.path.realpath(path), repo):
            continue
        namespace = vars(module)
        for key, value in list(namespace.items()):
            pair = replacements.get(id(value))
            if pair is not None and pair[0] is value:
                namespace[key] = pair[1]


def repo_modules():
    repo = _STATE["repo"]
    for name, module in sorted(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if path and _inside(os.path.realpath(path), repo):
            yield name, module
