"""The `layout` key of .codeshape/baseline.json: loading it, deciding each finding's status, and
rebaselining. The key sits beside consolidate's metrics, outside `limits`, so older checkers read the
file unchanged; rebaseline replaces only this key. Until the key exists every rule is advisory.

    "layout": {"date": "...", "head": "<sha>", "findings": {"<rule>": ["<key>", ...]}}

Ratcheted findings are keyed by rule and path (contract: importer -> target; cycle: its members);
renames since the recorded head are followed with `git diff -M`, as consolidate's ratchet does."""
import datetime
import json
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import git_adapter, json_splice
from .config import ConfigError, path_matches
from .findings import (ADVISED, ADVISORY, BASELINED, BLOCK, EXEMPT, FAILING, NEW, RATCHET, RULES, Finding,
                       key_for)

KEY = "layout"


class RebaselineError(Exception):
    pass


@dataclass
class Baseline:
    path: str
    exists: bool
    layout: Optional[dict] = None  # None: no `layout` key yet

    def keys(self) -> Dict[str, set]:
        return {rule: set(keys) for rule, keys in (self.layout or {}).get("findings", {}).items()}


def load(path: str) -> Baseline:
    if not os.path.exists(path):
        return Baseline(path=path, exists=False)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise ConfigError(f"{path}: {e}") from None
    layout = data.get(KEY) if isinstance(data, dict) else None
    if layout is not None:
        findings = layout.get("findings") if isinstance(layout, dict) else None
        if not isinstance(findings, dict) or not all(
                rule in RULES and isinstance(keys, list) for rule, keys in findings.items()):
            raise ConfigError(f"{path}: malformed `{KEY}` key; rerun check_layout.py rebaseline")
    return Baseline(path=path, exists=True, layout=layout)


def exception_for(finding: Finding, exceptions: List[dict]) -> Optional[int]:
    return next((i for i, e in enumerate(exceptions)
                 if e["rule"] == finding.rule and any(path_matches(p, e["path"]) for p in finding.paths)), None)


def classify(findings: List[Finding], exceptions: List[dict], baseline: Baseline,
             renames: Dict[str, str]) -> List[int]:
    """Set each finding's status; returns how many findings each exception covered."""
    used = [0] * len(exceptions)
    known = baseline.keys()
    for f in findings:
        index = exception_for(f, exceptions)
        if index is not None:
            used[index] += 1
            f.status, f.exception = EXEMPT, exceptions[index]["reason"]
        elif baseline.layout is None or f.treatment == ADVISORY:
            f.status = ADVISED
        elif f.treatment == BLOCK:
            f.status = FAILING
        else:
            old = tuple(renames.get(p, p) for p in f.paths)
            old_key = key_for(f.rule, tuple(sorted(old)) if f.rule == "cycle" else old)
            keys = known.get(f.rule, set())
            f.status = BASELINED if f.key in keys or old_key in keys else NEW
    return used


def rebaseline(baseline: Baseline, findings: List[Finding], root: str) -> int:
    """Write the layout key: every ratcheted, non-exempt finding, as of HEAD. Returns the count."""
    if not baseline.exists:
        raise RebaselineError(f"{baseline.path} does not exist. consolidate's drift_check.py rebaseline owns "
                              f"that file: run it first, then rerun this.")
    recorded: Dict[str, List[str]] = {}
    for f in findings:
        if f.treatment == RATCHET and f.status != EXEMPT:
            recorded.setdefault(f.rule, []).append(f.key)
    value = {"date": datetime.date.today().isoformat(), "head": git_adapter.head(root),
             "findings": {rule: sorted(set(keys)) for rule, keys in sorted(recorded.items())}}
    with open(baseline.path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        updated = json_splice.set_key(text, KEY, value)
    except ValueError as e:
        raise RebaselineError(f"{baseline.path}: {e}") from None
    tmp = baseline.path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(updated)
    os.replace(tmp, baseline.path)
    return sum(len(v) for v in value["findings"].values())
