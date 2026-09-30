"""Offline candump syntax and streaming zstd boundaries.

Parsing deliberately separates wire syntax from consumer policy. Timestamps and
interfaces retain their input type: callers choose float versus exact microsecond
conversion, ASCII/channel validation, payload limits, and malformed-line policy.
No CAN interfaces or live services are accessed here.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re
import subprocess
from typing import Callable, Iterator, Literal


class CandumpSyntaxError(ValueError):
    """A nonmatching line or a declared DLC inconsistent with its payload."""


@dataclass(frozen=True)
class CandumpFormat:
    """Lexical format dimensions, not tool names or validation policies.

    ``decimal`` excludes exponents; ``float`` leaves timestamp spelling to the
    caller's float conversion. ``prefix`` permits trailing non-frame text.
    Bytes input uses ASCII regex semantics; str input uses Unicode semantics.
    """

    id_width: tuple[int, int] = (1, 8)
    long_form: bool = True
    timestamp_required: bool = True
    timestamp_syntax: Literal["decimal", "float"] = "decimal"
    leading_whitespace: bool = True
    trailing_text: Literal["reject", "prefix"] = "reject"


@dataclass(frozen=True)
class CandumpFields:
    timestamp: str | bytes | None
    interface: str | bytes
    can_id_text: str | bytes
    payload: bytes


@lru_cache(maxsize=32)
def candump_patterns(format: CandumpFormat, *, binary: bool = False) -> tuple:
    """Return long/compact patterns, preserving bytes-versus-text semantics."""
    timestamp = (
        r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
        if format.timestamp_syntax == "decimal" else r"[^)]+"
    )
    stamp = rf"\((?P<timestamp>{timestamp})\)\s+"
    if not format.timestamp_required:
        stamp = rf"(?:{stamp})?"
    start = r"^\s*" if format.leading_whitespace else "^"
    low, high = format.id_width
    header = (
        start + stamp + r"(?P<interface>\S+)\s+"
        + rf"(?P<can_id>[0-9A-Fa-f]{{{low},{high}}})"
    )
    tail = r"\s*$" if format.trailing_text == "reject" else ""
    long = (
        header + r"\s+\[(?P<dlc>\d{1,2})\]"
        r"(?:\s+(?P<data>[0-9A-Fa-f]{2}(?:\s+[0-9A-Fa-f]{2})*))?" + tail
    )
    compact = header + r"#(?P<data>(?:[0-9A-Fa-f]{2})*)" + tail
    return tuple(re.compile(pattern.encode("ascii") if binary else pattern)
                 for pattern in (long, compact))


def parse_candump_line(
    line: str | bytes, *, format: CandumpFormat = CandumpFormat(),
) -> CandumpFields:
    """Parse compact or long syntax without imposing consumer semantic gates.

    Nonmatching lines (including blanks) raise CandumpSyntaxError. Payload
    conversion ValueError/UnicodeDecodeError is intentionally not translated:
    text consumers historically expose bytes.fromhex's Unicode-whitespace error.
    """
    long, compact = candump_patterns(format, binary=isinstance(line, bytes))
    match_method = "fullmatch" if format.trailing_text == "reject" else "match"
    match = getattr(long, match_method)(line) if format.long_form else None
    if match is None:
        match = getattr(compact, match_method)(line)
    if match is None:
        raise CandumpSyntaxError("malformed nonempty candump line")
    data = match.group("data") or ""
    if isinstance(data, bytes):
        data = data.decode("ascii")
    payload = bytes.fromhex(data)
    if "dlc" in match.re.groupindex and len(payload) != int(match.group("dlc"), 10):
        raise CandumpSyntaxError("candump line DLC does not match its payload")
    return CandumpFields(match.group("timestamp"), match.group("interface"),
                         match.group("can_id"), payload)


def identifier_bits(text: str | bytes, can_id: int, *, strict_width: bool = False) -> int | None:
    """Label SFF/EFF by textual width and range, never mask identifier flags."""
    if (len(text) == 3 if strict_width else len(text) <= 3) and can_id <= 0x7FF:
        return 11
    if (len(text) == 8 if strict_width else len(text) <= 8) and can_id <= 0x1FFFFFFF:
        return 29
    return None


def _finish_binary(process: subprocess.Popen, completed: bool, path: Path,
                   error_type: Callable[[str], Exception]) -> None:
    process.stdout.close()
    if not completed and process.poll() is None:
        process.terminate()
    try:
        returncode = process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        returncode = -9
    if completed and returncode != 0:
        raise error_type(f"zstd failed while reading {path} with status {returncode}")


@contextmanager
def zstd_stream(
    path: Path, *, error_type: Callable[[str], Exception],
    popen: Callable = subprocess.Popen, executable: str = "zstd",
    text: bool = False, stdin: int | None = None,
) -> Iterator[subprocess.Popen]:
    """Yield the child with readable stdout; preserve two existing I/O profiles.

    Binary evidence readers discard stderr and terminate/reap on exceptional
    exit, with a ten-second wait and kill fallback. Text report readers capture
    UTF-8/replace stderr and only close pipes on exceptional exit. The latter is
    intentionally not hardened here: changing early-exit semantics is not a
    mechanical consolidation. Callers retain suffix selection and stream caps.
    """
    kwargs = {"stdout": subprocess.PIPE,
              "stderr": subprocess.PIPE if text else subprocess.DEVNULL}
    if stdin is not None:
        kwargs["stdin"] = stdin
    if text:
        kwargs.update(text=True, encoding="utf-8", errors="replace")
    try:
        process = popen([executable, "-dc", "--", str(path)], **kwargs)
    except OSError as exc:
        raise error_type(f"cannot start zstd for {path}: {exc}") from exc
    if process.stdout is None or (text and process.stderr is None):
        if not text or process.poll() is None:
            process.kill()
        process.wait()
        message = ("zstd did not provide stdout/stderr pipes" if text
                   else "zstd stdout pipe was not created")
        raise error_type(message)
    completed = False
    try:
        yield process
        completed = True
        if text:
            stderr = process.stderr.read()
            returncode = process.wait()
    finally:
        if text:
            process.stdout.close()
            process.stderr.close()
        else:
            _finish_binary(process, completed, path, error_type)
    if text and returncode != 0:
        detail = stderr.strip() or f"exit status {returncode}"
        raise error_type(f"zstd decompression failed for {path}: {detail}")
