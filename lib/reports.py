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
from typing import Literal


def atomic_json(
    path: str | os.PathLike[str],
    payload: object,
    *,
    temporary: str | os.PathLike[str] | None = None,
    prefix: str = "tmp",
    encoding: str | None = "utf-8",
    newline: str | None = None,
    indent: int | None = 2,
    sort_keys: bool = False,
    ensure_ascii: bool = True,
    fsync: bool = False,
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

    No directory fsync, directory creation, or overwrite refusal is added here.
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
    finally:
        if cleanup == "always-best-effort":
            try:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            except OSError:
                pass
        elif cleanup == "failure" and not completed:
            Path(temporary).unlink(missing_ok=True)
