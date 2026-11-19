"""Where research CLIs write, and keeping that within the fixed-size volume.

Everything a research CLI can write is under the volume (a preallocated loop-mounted image, so its
footprint on the SD card never grows): the Claude token, the private Codex home, each run's empty
working directory and HOME (which holds Claude's session), the attempt's TMPDIR and the
discovery home. The worker keeps the volume under its targets: per run, across runs, and free space.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import stat

from lib.reports import atomic_json
from projects.vehicle_data.api_credits_contract import API_CREDITS, PATHS, SUBSCRIPTION
from projects.vehicle_data.dtc_research_contract import RUN_ID_RE, WORKER

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
        if backing == image:  # a backing file the kernel marks "(deleted)" is no longer the image on disk
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


# Bound the walk as well as its reads. The run watchdog already caps total run storage.
MAX_TRANSCRIPT_BYTES = RUN_CAP_MIB * MIB
MAX_HOME_ENTRIES = 8192
MAX_HOME_DEPTH = 32
MAX_MARKER_BYTES = 8192
PENDING_SCAN = "pending_scan"
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


@contextmanager
def _directory(path, parent=None):
    fd = os.open(path, READ_FLAGS | os.O_DIRECTORY, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _regular_file(parent, name):
    fd = os.open(name, READ_FLAGS, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        yield fd, info
    finally:
        os.close(fd)


def _read_marker(run_fd):
    try:
        with _regular_file(run_fd, "session.json") as (fd, info):
            if info.st_size > MAX_MARKER_BYTES:
                raise ValueError("session marker is too large")
            data = os.read(fd, MAX_MARKER_BYTES + 1)
            if len(data) != info.st_size:
                raise ValueError("session marker read was incomplete")
            record = json.loads(data)
            if not isinstance(record, dict):
                raise ValueError("session marker is not an object")
            return record, False
    except FileNotFoundError:
        return {}, False
    except (OSError, ValueError, RecursionError):
        return {}, True


def _marker_origin(record):
    if not isinstance(record.get("session_id"), str) or record.get("poisoned", False) is not False \
            or record.get(PENDING_SCAN, False) is not False:
        return None
    origin = record.get("billing", SUBSCRIPTION)
    return origin if origin in (API_CREDITS, SUBSCRIPTION) else None


@dataclass
class TranscriptInventory:
    record: dict = field(default_factory=dict)
    files: dict[tuple[str, ...], int] = field(default_factory=dict)
    marker_error: bool = False
    inspection_error: bool = False
    key_found: bool = False
    entries: int = 0
    remaining_bytes: int = MAX_TRANSCRIPT_BYTES

    def matching_size(self, session_id):
        """A single matching transcript in the layout Claude can actually resume."""
        if len(self.files) != 1:
            return None
        (path, size), = self.files.items()
        if len(path) == 4 and path[:2] == (".claude", "projects") and path[-1] == session_id + ".jsonl":
            return size
        return None

    def resume(self, session_id):
        if self.inspection_error or self.marker_error or self.record.get("session_id") != session_id:
            return None
        size, origin = self.matching_size(session_id), _marker_origin(self.record)
        return (origin, size) if size is not None and origin is not None else None

    def counts_as(self, billing):
        session_id = self.record.get("session_id")
        safe = self.resume(session_id) if isinstance(session_id, str) else None
        if safe:
            return safe[0] == billing
        return billing == API_CREDITS and bool(
            self.files or self.marker_error or self.inspection_error or self.record.get(PENDING_SCAN))


def _scan_transcript(parent, name, inventory, needle):
    with _regular_file(parent, name) as (fd, info):
        size = info.st_size
        if size > min(MAX_TRANSCRIPT_BYTES, inventory.remaining_bytes):
            raise OSError("transcript read budget exceeded")
        remaining, tail = size, b""
        while remaining:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                raise OSError("transcript shrank during inspection")
            remaining -= len(chunk)
            inventory.remaining_bytes -= len(chunk)
            if needle:
                window = tail + chunk
                inventory.key_found |= needle in window
                tail = window[-len(needle):]
        after = os.fstat(fd)
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (size, info.st_mtime_ns, info.st_ctime_ns):
            raise OSError("transcript changed during inspection")
        return size


def _scan_home(fd, prefix, inventory, needle):
    if len(prefix) >= MAX_HOME_DEPTH:
        inventory.inspection_error = True
        return
    # Open each directory relative to its already-open parent: a symlink substitution cannot
    # redirect the walk outside the home. Keep scanning siblings after any failed inspection.
    try:
        with os.scandir(fd) as entries:
            for entry in entries:
                if inventory.entries >= MAX_HOME_ENTRIES:
                    inventory.inspection_error = True
                    return
                inventory.entries += 1
                path = (*prefix, entry.name)
                try:
                    mode = entry.stat(follow_symlinks=False).st_mode
                    if stat.S_ISLNK(mode):
                        inventory.inspection_error = True  # cannot attest to a linked subtree
                    elif stat.S_ISDIR(mode):
                        with _directory(entry.name, fd) as child:
                            _scan_home(child, path, inventory, needle)
                    elif entry.name.endswith(".jsonl"):
                        if not stat.S_ISREG(mode):
                            raise OSError("not a regular transcript")
                        inventory.files[path] = _scan_transcript(fd, entry.name, inventory, needle)
                except OSError:
                    inventory.inspection_error = True
    except OSError:
        inventory.inspection_error = True


def _scan_run_home(run_fd, inventory, secret=None):
    try:
        with _directory("home", run_fd) as home:
            _scan_home(home, (), inventory, secret.encode() if secret else None)
    except FileNotFoundError:
        pass  # no HOME means there can be no transcript
    except OSError:
        inventory.inspection_error = True


class Volume:
    def __init__(self, root=WORKER["volume"]):
        self.root = Path(root)
        # A failed directory fsync or marker removal is not a verified purge, even if a later
        # inventory can no longer see a transcript. A disabled-worker restart retries the drop.
        self._unverified_credit_drops = set()

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
            with _directory(self.runs) as runs, _directory(self.run_dir(run_id).name, runs) as run:
                record, error = _read_marker(run)
                return record if record and not error else None
        except OSError:
            return None

    def save_session_record(self, run_id, record):
        """Durably replace ``session.json`` before the process can create or append a transcript."""
        path = self.run_dir(run_id) / "session.json"
        atomic_json(path, record, prefix=".session-", indent=None, fsync=True, fsync_dir=True)

    def transcript_inventory(self, run_id, secret=None):
        """One bounded inspection, including files that have no matching safe session marker."""
        inventory = TranscriptInventory()
        try:
            with _directory(self.runs) as runs, _directory(self.run_dir(run_id).name, runs) as run:
                inventory.record, inventory.marker_error = _read_marker(run)
                _scan_run_home(run, inventory, secret)
        except FileNotFoundError:
            pass  # the run was never created or has been deleted
        except OSError:
            inventory.inspection_error = True
        return inventory

    def resume_session(self, run_id, session_id):
        """The origin and size of the only resumable transcript, or ``None`` (replay required)."""
        return self.transcript_inventory(run_id).resume(session_id)

    def poison_session(self, run_id):
        """Mark a run's session unresumable on either credential (durably)."""
        self.save_session_record(run_id, {**(self.session_record(run_id) or {}), "poisoned": True})

    def drop_session(self, run_id):
        """Delete the CLI session, and remove its marker only after verifying the home is clean."""
        path = self.run_dir(run_id)
        try:
            with _directory(self.runs) as runs, _directory(path.name, runs) as run:
                record, _ = _read_marker(run)
                shutil.rmtree("home", dir_fd=run, ignore_errors=True)
                rollout = record.get("rollout_path")
                if isinstance(rollout, str) and Path(rollout).resolve().is_relative_to(self.codex_home.resolve()):
                    Path(rollout).unlink(missing_ok=True)
                inventory = TranscriptInventory()
                _scan_run_home(run, inventory)
                if inventory.files or inventory.inspection_error:
                    return False
                # Make removal of HOME durable before allowing its provenance to disappear.
                os.fsync(run)
                try:
                    os.unlink("session.json", dir_fd=run)
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            return True  # no run or runs directory
        except (OSError, ValueError, RuntimeError):
            return False
        return True

    def _session_runs(self):
        """Yield names to inspect; ``None`` means enumeration could not be completed."""
        try:
            with _directory(self.runs) as runs, os.scandir(runs) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_HOME_ENTRIES:
                        yield None
                        return
                    if RUN_ID_RE.fullmatch(entry.name):
                        yield entry.name  # inventory opens it without following symlinks
        except FileNotFoundError:
            pass
        except OSError:
            yield None

    def drop_sessions_billed(self, billing):
        """Drop matching sessions (including unsafe ones for API billing); count unverified drops."""
        failed, unverified = 0, set()
        for name in self._session_runs():
            if name is None:
                failed += 1
            elif self.transcript_inventory(name).counts_as(billing) or (
                    billing == API_CREDITS and name in self._unverified_credit_drops):
                if not self.drop_session(name):
                    unverified.add(name)
        if billing == API_CREDITS:
            self._unverified_credit_drops = unverified | (self._unverified_credit_drops if failed else set())
        return failed + len(unverified)

    def count_sessions_billed(self, billing):
        """Count matching runs, including unsafe transcripts and failed API purge attestations."""
        names = set()
        uncertain = 0
        for name in self._session_runs():
            if name is None:
                uncertain += billing == API_CREDITS
            elif self.transcript_inventory(name).counts_as(billing):
                names.add(name)
        if billing == API_CREDITS:
            names.update(self._unverified_credit_drops)
        return len(names) + uncertain

    def delete_run(self, run_id):
        self.drop_session(run_id)
        self.clear(self.run_dir(run_id))

    def run_footprint(self, run_id):
        rollout = (self.session_record(run_id) or {}).get("rollout_path")
        extra = Path(rollout).stat().st_size if isinstance(rollout, str) and Path(rollout).exists() else 0
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
