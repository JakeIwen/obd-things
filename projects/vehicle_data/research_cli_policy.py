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

from projects.vehicle_data.dtc_research_contract import WORKER

POLICY = json.loads(Path(__file__).with_name("research_cli_policy.json").read_text())
CLI_ROOT = Path(WORKER["cliRoot"])
IDENTITY_FILE = "identity.json"
IDENTITY_STATES = ("ok", "changed", "missing", "unverified", "disabled")


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
    """Answers whether a provider's installed copy is the verified one; re-hashes only on change."""

    def __init__(self, cli_root=CLI_ROOT, policy=POLICY):
        self.cli_root, self.policy = Path(cli_root), policy
        self._cache = {}

    def record(self):
        path = self.cli_root / IDENTITY_FILE
        try:
            loaded = json.loads(path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return None
        return loaded.get("providers", {}) if isinstance(loaded, dict) else None

    def check(self, provider):
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
        cached = self._cache.get(provider)
        digest = cached[1] if cached and cached[0] == key else digest_of(file_list(root))
        self._cache[provider] = (key, digest)
        if digest != entry["digest"]:
            return ProviderIdentity("changed", f"the installed {provider} copy changed; reinstall it for research")
        return ProviderIdentity("ok", None, root / entry["executable"], digest, entry.get("version"),
                                verification.get("evidence") or {})
