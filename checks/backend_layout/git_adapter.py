"""Every git call the checks make: listing files, the folder's prefix inside the repo, renames,
uncommitted changes and recent commits. Read-only; a missing repo or git yields empty results."""
import os
import subprocess
from typing import Dict, List, Optional

from . import cochange


def git(root: str, *args: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)
    except OSError:
        return None
    return r.stdout if r.returncode == 0 else None


def inside_work_tree(root: str) -> bool:
    return (git(root, "rev-parse", "--is-inside-work-tree") or "").strip() == "true"


def listed_files(root: str) -> List[str]:
    """Tracked plus untracked-but-not-ignored files, relative to `root`."""
    r = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                       capture_output=True, check=False)
    return sorted({os.fsdecode(p) for p in r.stdout.split(b"\0") if p}) if r.returncode == 0 else []


def head(root: str) -> str:
    return (git(root, "rev-parse", "HEAD") or "").strip()


def renames_since(root: str, sha: str) -> Dict[str, str]:
    """new path -> old path for files git sees as renamed between `sha` and the working tree."""
    out = git(root, "diff", "-M", "--name-status", "--relative", sha) if sha else None
    mapping = {}
    for line in (out or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].startswith("R"):
            mapping[parts[2]] = parts[1]
    return mapping


def dirty_paths(root: str) -> List[str]:
    out = git(root, "status", "--porcelain", "--untracked-files=all", "--", ".")
    return [ln[3:] for ln in (out or "").splitlines() if ln[3:] and ".codeshape/" not in ln[3:]]


def recent_commits(root: str) -> List[List[str]]:
    """consolidate's commit file lists (cochange.py), re-rooted when `root` is a subfolder of the repo."""
    prefix = (git(root, "rev-parse", "--show-prefix") or "").strip()
    commits = cochange.commit_file_lists(root)
    return [[f[len(prefix):] for f in files if f.startswith(prefix)] for files in commits] if prefix else commits
