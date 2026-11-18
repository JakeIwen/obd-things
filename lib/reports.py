"""Atomic JSON publication for offline CLI reports.

Parent creation belongs to the caller: Path.mkdir and os.makedirs(dirname(...))
do not behave identically for a bare filename. Temporary files must be siblings
of the destination. A caller-supplied temporary path uses ordinary open (and the
process umask); an omitted path uses NamedTemporaryFile (mode 0600).

This is deliberately not a replacement for JSONL/CSV writers, pre-serialized
write_text calls, or directory-durable checkpoint protocols.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Iterable, Literal


def require_distinct_output(
    output: str | os.PathLike[str], inputs: Iterable[str | os.PathLike[str]],
) -> None:
    """Reject report destinations that resolve to read-only evidence inputs."""
    if Path(output).resolve() in {Path(source).resolve() for source in inputs}:
        raise ValueError("report output must differ from every evidence input")


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(
    path: str | os.PathLike[str],
    payload: object,
    *,
    temporary: str | os.PathLike[str] | None = None,
    prefix: str | None = None,
    encoding: str | None = "utf-8",
    newline: str | None = None,
    indent: int | None = 2,
    sort_keys: bool = False,
    ensure_ascii: bool = True,
    fsync: bool = False,
    fsync_dir: bool = False,
    cleanup: Literal["failure", "always-best-effort", "none"] = "failure",
) -> None:
    """Write JSON plus one newline, then replace the destination.

    ``temporary`` preserves deterministic, umask-created temporary filenames.
    Otherwise a random temporary name with ``prefix`` is created in path.parent.
    ``encoding=None`` deliberately retains the platform's default encoding.
    JSON separators and default conversion retain json.dump's defaults.

    Cleanup policies preserve the existing writers' failure contracts:
    * failure: unlink on any BaseException, ignoring only a missing file;
    * always-best-effort: test existence even after replacement, and ignore
      OSError from either that check or unlink;
    * none: leave a failed temporary write in place.

    ``fsync_dir`` (default off) also fsyncs the destination's directory after the replace, so a
    crash cannot lose the rename; ``fsync`` alone makes only the file's bytes durable.
    No directory creation or overwrite refusal is added here.
    """
    handle = None
    if temporary is None:
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding=encoding, newline=newline,
            prefix=prefix, dir=Path(path).parent, delete=False,
        )
        temporary = handle.name
    completed = False
    try:
        if handle is None:
            handle = open(temporary, "w", encoding=encoding, newline=newline)
        with handle:
            json.dump(
                payload, handle, indent=indent, sort_keys=sort_keys,
                ensure_ascii=ensure_ascii,
            )
            handle.write("\n")
            if fsync:
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary, path)
        completed = True
        if fsync_dir:
            _fsync_directory(Path(path).parent)
    finally:
        if cleanup == "always-best-effort":
            try:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            except OSError:
                pass
        elif cleanup == "failure" and not completed:
            Path(temporary).unlink(missing_ok=True)
