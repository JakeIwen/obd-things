#!/usr/bin/env python3
"""Extract AlfaOBD's split SQLite catalog and English resource from an APK.

The APK is user-supplied input.  Reconstructed/extracted files are machine-written
research material and therefore default below tmp/ rather than into git.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import tempfile
from zipfile import ZipFile


MEMBER_RE = re.compile(r"^assets/alfaobd\.db\.(\d{3})$")
SQLITE_MAGIC = b"SQLite format 3\x00"
ENGLISH_LABEL_MEMBER = "res/raw/alfaobd5_en.txt"


def database_members(archive: ZipFile) -> list[str]:
    """Return the validated, numerically ordered database chunk names."""
    numbered = []
    for name in archive.namelist():
        match = MEMBER_RE.fullmatch(name)
        if match:
            numbered.append((int(match.group(1)), name))
    numbered.sort()
    if not numbered:
        raise ValueError("APK contains no assets/alfaobd.db.NNN chunks")
    expected = list(range(1, len(numbered) + 1))
    actual = [number for number, _ in numbered]
    if actual != expected:
        raise ValueError(f"database chunks are not contiguous from 001: found {actual}")
    return [name for _, name in numbered]


def extract_database(apk: Path, output: Path) -> tuple[int, int]:
    """Atomically reconstruct the database, returning chunk and byte counts."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with ZipFile(apk) as archive:
            members = database_members(archive)
            with tempfile.NamedTemporaryFile(
                mode="w+b", prefix=f".{output.name}.", dir=output.parent, delete=False
            ) as destination:
                temporary_name = destination.name
                total = 0
                for member in members:
                    with archive.open(member) as source:
                        while block := source.read(1024 * 1024):
                            destination.write(block)
                            total += len(block)
                destination.flush()
                os.fsync(destination.fileno())
                destination.seek(0)
                if destination.read(len(SQLITE_MAGIC)) != SQLITE_MAGIC:
                    raise ValueError("reassembled output does not have a SQLite header")
        os.replace(temporary_name, output)
        temporary_name = None
        return len(members), total
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def extract_member(apk: Path, member: str, output: Path) -> int:
    """Atomically copy one APK member verbatim and return its byte count."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with ZipFile(apk) as archive:
            if member not in archive.namelist():
                raise ValueError(f"APK contains no {member}")
            with archive.open(member) as source, tempfile.NamedTemporaryFile(
                mode="w+b", prefix=f".{output.name}.", dir=output.parent, delete=False
            ) as destination:
                temporary_name = destination.name
                total = 0
                while block := source.read(1024 * 1024):
                    destination.write(block)
                    total += len(block)
                destination.flush()
                os.fsync(destination.fileno())
        os.replace(temporary_name, output)
        temporary_name = None
        return total
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", type=Path, help="AlfaOBD base APK copied from the device")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tmp/ecu_mapping/android_tablet/alfaobd.db"),
        help="reconstructed SQLite path (default: %(default)s)",
    )
    parser.add_argument(
        "--labels-output",
        type=Path,
        default=Path("tmp/ecu_mapping/android_tablet/alfaobd5_en.txt"),
        help="verbatim English label-resource path (default: %(default)s)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    chunks, byte_count = extract_database(args.apk, args.output)
    print(f"wrote {args.output} ({byte_count} bytes from {chunks} chunks)")
    label_bytes = extract_member(args.apk, ENGLISH_LABEL_MEMBER, args.labels_output)
    print(f"wrote {args.labels_output} ({label_bytes} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
