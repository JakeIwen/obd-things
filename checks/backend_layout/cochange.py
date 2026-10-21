"""Advisory co-change coupling for drift_check.py `check`: source files in different top-level folders
that keep changing in the same commits. Stdlib only; reads `git log`, never writes.

Window: the last COMMITS non-merge commits. Commits touching more than MAX_FILES files are skipped
(mass renames, formatting, moves), so one reorganization cannot dominate. A pair is reported when it
changed together at least MIN_TOGETHER times. Only files that exist now and are measured source files
count; test files are left out (a test changing with its subject is expected).
"""
import collections
import itertools
import subprocess

COMMITS, MAX_FILES, MIN_TOGETHER = 200, 10, 3


def commit_file_lists(root, commits=COMMITS):
    r = subprocess.run(["git", "-C", root, "log", f"-n{commits}", "--no-merges", "--no-renames", "--name-only",
                        "--format=%x00"], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return []
    return [[ln for ln in chunk.splitlines() if ln.strip()] for chunk in r.stdout.split("\0")[1:]]


def top_folder(path):
    return path.split("/", 1)[0] if "/" in path else "(root)"


def coupling(commits, current, tests=()):
    """Pairs of current non-test files in different top-level folders changed together MIN_TOGETHER+ times."""
    together, alone, skipped = collections.Counter(), collections.Counter(), 0
    for files in commits:
        if len(files) > MAX_FILES:
            skipped += 1
            continue
        kept = sorted({f for f in files if f in current and f not in tests})
        alone.update(kept)
        for a, b in itertools.combinations(kept, 2):
            if top_folder(a) != top_folder(b):
                together[(a, b)] += 1
    pairs = [{"a": a, "b": b, "together": n, "a_commits": alone[a], "b_commits": alone[b]}
             for (a, b), n in together.items() if n >= MIN_TOGETHER]
    pairs.sort(key=lambda p: (-p["together"], p["a"], p["b"]))
    return {"pairs": pairs, "commits": len(commits), "skipped_large": skipped}


def collect(root, current, tests=()):
    return coupling(commit_file_lists(root), set(current), set(tests))
