"""Where research CLIs write, and keeping that within the fixed-size volume.

Everything a research CLI can write is under the volume (a preallocated loop-mounted image, so its
footprint on the SD card never grows): the Claude token, the private Codex home, each run's empty
working directory and HOME (which holds Claude's session), the attempt's TMPDIR and the
discovery home. The worker keeps the volume under its targets: per run, across runs, and free space.
"""

import json
import os
from pathlib import Path
import shutil

from projects.vehicle_data.dtc_research_contract import RUN_ID_RE, WORKER

MIB = 1024 * 1024
FREE_FLOOR_MIB = 16
RUN_CAP_MIB = 16
SESSIONS_TARGET_MIB = 128
CODEX_HOME_SOFT_MIB = 48


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
