"""Where research CLIs write, and keeping that within the fixed-size volume.

Everything a research CLI can write is under the volume (a preallocated loop-mounted image, so its
footprint on the SD card never grows): the Claude token, the private Codex home, each run's empty
working directory and HOME (which holds Claude's session), the attempt's TMPDIR and the
discovery home. The worker keeps the volume under its targets: per run, across runs, and free space.
"""

import json
import os
from pathlib import Path
import re
import shutil

from projects.vehicle_data.api_credits_contract import PATHS
from projects.vehicle_data.dtc_research_contract import RUN_ID_RE, WORKER
from projects.vehicle_data.research_cli import claude_session_files

MIB = 1024 * 1024
FREE_FLOOR_MIB = 16
RUN_CAP_MIB = 16
SESSIONS_TARGET_MIB = 128
CODEX_HOME_SOFT_MIB = 48
VOLUME_IMAGE = "/var/lib/vanresearch.img"  # the loop-mounted image behind WORKER["volume"] (systemd/var-lib-vanresearch.mount)


def mount_problem(root, image=VOLUME_IMAGE, *, is_mount=os.path.ismount, mountinfo=Path("/proc/self/mountinfo"),
                  sys_block=Path("/sys/block")):
    """``None`` when ``root`` is a mount point whose source is the research image, else a sentence.
    An unmounted volume is just a directory on the SD card, where a credential must never be written.
    The mount's source is the image itself or the loop device whose backing file is the image."""
    root = str(root)
    if not is_mount(root):
        return f"{root} is not a mount point; the research volume is not mounted"
    source = _mount_source(root, mountinfo)
    if source is None:
        return f"{root} has no readable mount record"
    if source == image:
        return None
    loop = re.fullmatch(r"/dev/(loop\d+)", source)
    if loop:
        try:
            backing = (Path(sys_block) / loop[1] / "loop" / "backing_file").read_text().strip()
        except OSError:
            return f"the loop device behind {root} cannot be identified"
        if backing.removesuffix(" (deleted)") == image:
            return None
    return f"{root} is mounted from something other than the research image"


def _mount_source(root, mountinfo):
    """The source of the last (visible) mount at ``root`` in a ``mountinfo`` file, or ``None``."""
    try:
        lines = Path(mountinfo).read_text().splitlines()
    except OSError:
        return None
    source = None
    for line in lines:
        head, separator, tail = line.partition(" - ")
        fields, parts = head.split(), tail.split()
        if separator and len(fields) >= 5 and len(parts) > 1 and fields[4].replace("\\040", " ") == root:
            source = parts[1]
    return source


def footprint(path):
    total = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


class Volume:
    def __init__(self, root=WORKER["volume"]):
        self.root = Path(root)

    token = property(lambda self: self.root / "claude-oauth-token")
    codex_home = property(lambda self: self.root / "codex-home")
    runs = property(lambda self: self.root / "runs")
    tmp = property(lambda self: self.root / "tmp")
    catalog = property(lambda self: self.root / "catalog")
    api_key = property(lambda self: self.root / Path(PATHS["keyFile"]).name)
    credit_ledger = property(lambda self: self.root / Path(PATHS["ledgerFile"]).name)

    def run_dir(self, run_id):
        if not RUN_ID_RE.fullmatch(run_id):
            raise ValueError("invalid run id")
        return self.runs / run_id

    def prepare_run(self, run_id):
        run = self.run_dir(run_id)
        for sub in ("work", "home"):
            (run / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
        return run

    def tmpdir(self, attempt_id):
        path = self.tmp / attempt_id
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def clear(self, path):
        shutil.rmtree(path, ignore_errors=True)

    def session_record(self, run_id):
        try:
            return json.loads((self.run_dir(run_id) / "session.json").read_text())
        except (OSError, ValueError):
            return None

    def save_session_record(self, run_id, record):
        path = self.run_dir(run_id) / "session.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record))
        os.replace(temporary, path)

    def drop_session(self, run_id):
        """Delete a run's CLI session files; later follow-ups replay."""
        record = self.session_record(run_id) or {}
        self.clear(self.run_dir(run_id) / "home")
        rollout = record.get("rollout_path")
        if rollout and Path(rollout).resolve().is_relative_to(self.codex_home.resolve()):
            Path(rollout).unlink(missing_ok=True)
        (self.run_dir(run_id) / "session.json").unlink(missing_ok=True)

    def _runs_billed(self, billing):
        for path in self.runs.glob("*"):
            if path.is_dir() and RUN_ID_RE.fullmatch(path.name) \
                    and (self.session_record(path.name) or {}).get("billing") == billing:
                yield path.name

    def drop_sessions_billed(self, billing):
        """Delete the CLI session of every run whose ``session.json`` says it was created under ``billing``."""
        for name in list(self._runs_billed(billing)):
            self.drop_session(name)

    def count_sessions_billed(self, billing):
        """Runs created under ``billing`` whose CLI transcript still exists (a marker alone does not count)."""
        count = 0
        for name in self._runs_billed(billing):
            record = self.session_record(name) or {}
            if isinstance(record.get("session_id"), str) \
                    and claude_session_files(self.run_dir(name) / "home", record["session_id"]):
                count += 1
        return count

    def delete_run(self, run_id):
        self.drop_session(run_id)
        self.clear(self.run_dir(run_id))

    def run_footprint(self, run_id):
        record = self.session_record(run_id) or {}
        rollout = record.get("rollout_path")
        extra = Path(rollout).stat().st_size if rollout and Path(rollout).exists() else 0
        return footprint(self.run_dir(run_id)) + extra

    def free_bytes(self):
        stats = os.statvfs(self.root)
        return stats.f_bavail * stats.f_frsize

    def startup_cleanup(self):
        for path in (self.tmp, self.catalog):
            self.clear(path)
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runs.mkdir(parents=True, exist_ok=True, mode=0o700)

    def sweep(self, keep):
        """Delete every run directory (and its Codex rollout) not in ``keep``; returns the count."""
        keep, removed = set(keep), 0
        for path in self.runs.glob("*"):
            if path.is_dir() and RUN_ID_RE.fullmatch(path.name) and path.name not in keep:
                self.delete_run(path.name)
                removed += 1
        return removed

    def enforce_targets(self, busy_run=None):
        """Drop sessions, least recently used first, until all sessions fit their target."""
        runs = [p for p in self.runs.glob("*") if p.is_dir() and RUN_ID_RE.fullmatch(p.name) and p.name != busy_run]
        sizes = {p.name: self.run_footprint(p.name) for p in runs}
        for name in [n for n, size in sizes.items() if size > RUN_CAP_MIB * MIB]:
            self.drop_session(name)
            sizes[name] = 0
        order = sorted(runs, key=lambda p: p.stat().st_mtime)
        total = sum(sizes.values())
        for path in order:
            if total <= SESSIONS_TARGET_MIB * MIB:
                break
            total -= sizes[path.name]
            self.drop_session(path.name)
        logs = list(self.codex_home.glob("logs_2.sqlite*"))
        if self.codex_home.exists() and footprint(self.codex_home) - footprint(self.codex_home / "sessions") \
                > CODEX_HOME_SOFT_MIB * MIB:
            for path in logs:
                path.unlink(missing_ok=True)
