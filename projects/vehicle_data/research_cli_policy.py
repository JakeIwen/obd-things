"""Which CLI copies research may run, and whether the installed ones are the verified ones.

The tracked policy (``research_cli_policy.json``) says what each provider is allowed to do; the
identity record (``/opt/van-research-cli/identity.json``, written only by the root-run installer)
says which files were installed and verified. A provider is usable only when its installed release
directory hashes to the digest its passing verification was recorded against: an extra, missing or
changed file makes it unavailable until the installer verifies again.
"""

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import stat
import threading

from projects.vehicle_data.dtc_research_contract import WORKER



def _validated(policy):
    credits = policy["claude"].get("api_credits")
    if not isinstance(credits, dict) or not isinstance(credits.get("enabled"), bool):
        raise ValueError("research_cli_policy.json: claude.api_credits.enabled must be true or false")
    return policy


POLICY = _validated(json.loads(Path(__file__).with_name("research_cli_policy.json").read_text()))
CLI_ROOT = Path(WORKER["cliRoot"])
IDENTITY_FILE = "identity.json"
# "checking": the copy's files changed since they were last hashed and a caller that must not wait
# (a status read, an admission check) asked before the background hash finished.
IDENTITY_STATES = ("ok", "changed", "missing", "unverified", "disabled", "checking")


def file_list(root):
    """Sorted ``[relative path, mode, size, sha256 or link target]`` for every entry under ``root``."""
    root = Path(root)
    rows = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            rows.append([rel, "link", 0, os.readlink(path)])
        elif stat.S_ISDIR(info.st_mode):
            rows.append([rel, f"dir:{mode:o}", 0, ""])
        elif stat.S_ISREG(info.st_mode):
            rows.append([rel, f"file:{mode:o}", info.st_size, _sha256(path)])
        else:
            rows.append([rel, "other", 0, ""])
    return rows


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_of(rows):
    return hashlib.sha256("\n".join(json.dumps(row) for row in rows).encode()).hexdigest()


def _stat_key(root):
    """Cheap change detector: every entry's (path, inode, size, mtime), plus the listing itself."""
    keys = []
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        keys.append((path.as_posix(), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
    return tuple(keys)


@dataclass
class ProviderIdentity:
    state: str
    reason: str | None = None
    executable: Path | None = None
    digest: str | None = None
    version: str | None = None
    evidence: dict = field(default_factory=dict)


class IdentityChecker:
    """Answers whether a provider's installed copy is the verified one; re-hashes only on change.

    Hashing the whole copy is slow on this Pi (Claude 2.1.325 is about 348 MiB: 2.5 s of CPU on an
    idle Pi 4, at least 8 s under the research unit's ``CPUQuota=30%``), so one hash runs at a time
    and callers that must answer at once pass ``rehash=False``: they get ``checking`` instead of
    waiting for it."""

    def __init__(self, cli_root=CLI_ROOT, policy=POLICY):
        self.cli_root, self.policy = Path(cli_root), policy
        self._cache = {}
        self._cache_lock = threading.Lock()
        self._hash_lock = threading.Lock()

    def record(self):
        path = self.cli_root / IDENTITY_FILE
        try:
            loaded = json.loads(path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return None
        return loaded.get("providers", {}) if isinstance(loaded, dict) else None

    def check(self, provider, *, rehash=True):
        if not self.policy.get(provider, {}).get("enabled"):
            return ProviderIdentity("disabled", self.policy.get(provider, {}).get("reason")
                                    or f"{provider} research is disabled by policy")
        providers = self.record()
        if providers is None:
            return ProviderIdentity("missing", "the CLI identity record cannot be read")
        entry = providers.get(provider)
        if not entry:
            return ProviderIdentity("missing", f"no {provider} copy is installed for research")
        verification = entry.get("verification") or {}
        if verification.get("result") != "passed" or verification.get("digest") != entry.get("digest"):
            return ProviderIdentity("unverified", verification.get("reason")
                                    or f"the {provider} copy has not passed verification")
        root = Path(entry["root"])
        if not root.is_dir() or self.cli_root not in root.parents:
            return ProviderIdentity("missing", f"the {provider} copy is missing")
        key = _stat_key(root)
        digest = self._cached_digest(provider, key)
        if digest is None and not rehash:
            return ProviderIdentity("checking", f"the research service is still checking the installed {provider} "
                                                "copy; try again in a minute")
        if digest is None:
            with self._hash_lock:
                digest = self._cached_digest(provider, key)  # another caller may have hashed it meanwhile
                if digest is None:
                    digest = digest_of(file_list(root))
                    with self._cache_lock:
                        self._cache[provider] = (key, digest)
        if digest != entry["digest"]:
            return ProviderIdentity("changed", f"the installed {provider} copy changed; reinstall it for research")
        return ProviderIdentity("ok", None, root / entry["executable"], digest, entry.get("version"),
                                verification.get("evidence") or {})

    def _cached_digest(self, provider, key):
        with self._cache_lock:
            cached = self._cache.get(provider)
        return cached[1] if cached and cached[0] == key else None
