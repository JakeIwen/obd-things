"""Finding records and the rule vocabulary every backend-layout module shares."""
from dataclasses import dataclass
from typing import Tuple

# Treatments, once a baseline has a `layout` key. Before that every rule is advisory.
RATCHET, BLOCK, ADVISORY = "ratchet", "block", "advisory"

RULES = {
    "contract": RATCHET,
    "declared-target": BLOCK,
    "prefix-group": RATCHET,
    "entry-heavy": RATCHET,
    "kind-folder": RATCHET,
    "cycle": RATCHET,  # Python; JS cycles are emitted with ADVISORY
    "fan-in-size": ADVISORY,
    "shim-stale": ADVISORY,
    "effects-in-rules": ADVISORY,
    "shell-size": ADVISORY,
}

# Statuses after exceptions and the baseline are applied. NEW and FAILING fail the run.
NEW, BASELINED, FAILING, ADVISED, EXEMPT = "new", "baselined", "failing", "advisory", "exempt"
FAILING_STATUSES = (NEW, FAILING)

# How a finding with several paths is keyed in the baseline (one path: the path itself).
KEY_SEPARATORS = {"contract": " -> ", "cycle": " | ", "declared-target": " <- "}


@dataclass
class Finding:
    rule: str
    paths: Tuple[str, ...]  # primary path first; contract: (importer, target); cycle: sorted members
    message: str
    treatment: str = ""
    line: int = 0
    group: str = ""  # prefix-group label, so output can show one group at a time
    status: str = ""
    exception: str = ""  # the exception's reason when status is EXEMPT

    def __post_init__(self):
        if self.rule not in RULES:
            raise ValueError(f"unknown rule {self.rule}")
        if not self.treatment:
            self.treatment = RULES[self.rule]

    @property
    def path(self) -> str:
        return self.paths[0]

    @property
    def key(self) -> str:
        return key_for(self.rule, self.paths)

    def as_dict(self) -> dict:
        out = {"rule": self.rule, "path": self.path, "key": self.key, "treatment": self.treatment,
               "status": self.status, "message": self.message}
        if self.line:
            out["line"] = self.line
        if self.group:
            out["group"] = self.group
        if self.exception:
            out["exception"] = self.exception
        return out


def key_for(rule: str, paths) -> str:
    return KEY_SEPARATORS.get(rule, "").join(paths) if len(paths) > 1 else paths[0]


def key_paths(rule: str, key: str) -> Tuple[str, ...]:
    separator = KEY_SEPARATORS.get(rule)
    return tuple(key.split(separator)) if separator else (key,)
