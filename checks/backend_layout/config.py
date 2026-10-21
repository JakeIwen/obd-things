"""The `layout` block of .codeshape/config.json: defaults, validation and path patterns.

Unknown keys fail, so typos surface. consolidate's keys (`file_lines`, `exclude`, ...) and
react-app's `css`/`structure` blocks share the file; only `file_lines` (the fan-in-size limit) and
`structure` (to skip contracts react-app's check-structure already enforces) are read from them.
"""
import copy
import fnmatch
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .findings import RULES

DEFAULTS = {
    "sourceRoots": ["."],
    "entries": [],
    "contracts": [],
    "contractsTable": {"file": "AGENTS.md", "heading": "Contracts"},
    "targets": ["**/*.service", "**/*.plist", "**/package.json"],
    "exclude": [],
    "aliases": {},
    "thresholds": {"entryLines": 200, "shellLines": 200, "prefixGroup": 3, "fanIn": 3},
    "exceptions": [],
}
CONTRACT_KEYS = {"from", "to", "allowDynamic"}
EXCEPTION_KEYS = {"rule", "path", "reason"}
FILE_LINES_DEFAULT = 800  # consolidate's default `file_lines`
GLOB_CHARS = set("*?[")


class ConfigError(Exception):
    pass


@dataclass
class Settings:
    root: str
    source: str  # the config file read, or "(defaults)"
    layout: dict
    file_lines: int = FILE_LINES_DEFAULT
    structure: Optional[dict] = None
    warnings: List[str] = field(default_factory=list)

    def __getitem__(self, key):
        return self.layout[key]

    @property
    def thresholds(self) -> Dict[str, int]:
        return self.layout["thresholds"]


def path_matches(path: str, pattern: str) -> bool:
    """Globs match with fnmatch (`*` crosses folders; `**/x` also matches `x` at the root);
    a pattern without glob characters is a path prefix (a file, or a folder and everything in it)."""
    if not GLOB_CHARS & set(pattern):
        prefix = os.path.normpath(pattern)
        return prefix == "." or path == prefix or path.startswith(prefix + "/")
    return fnmatch.fnmatch(path, pattern) or (pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]))


def matches_any(path: str, patterns) -> bool:
    return any(path_matches(path, p) for p in patterns)


def read_raw(root: str, config_path: Optional[str]):
    path = os.path.abspath(config_path) if config_path else os.path.join(root, ".codeshape", "config.json")
    if not os.path.exists(path):
        if config_path:
            raise ConfigError(f"config file not found: {path}")
        return {}, "(defaults)"
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as e:
        raise ConfigError(f"{path}: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a JSON object")
    return raw, path


def load(root: str, config_path: Optional[str] = None) -> Settings:
    raw, source = read_raw(root, config_path)
    block = raw.get("layout", {})
    if not isinstance(block, dict):
        raise ConfigError(f"{source}: `layout` must be an object")
    unknown = sorted(set(block) - set(DEFAULTS))
    if unknown:
        raise ConfigError(f"{source}: unknown layout setting(s): {', '.join(unknown)}")
    layout = copy.deepcopy(DEFAULTS)
    for key, value in block.items():
        if isinstance(DEFAULTS[key], dict) and key != "aliases":
            layout[key] = merge_block(key, DEFAULTS[key], value, source)
        else:
            layout[key] = value
    validate(layout, source)
    for folder in layout["sourceRoots"]:
        if not os.path.isdir(os.path.join(root, folder)):
            raise ConfigError(f"{source}: source root not found: {folder}")
    file_lines = raw.get("file_lines", FILE_LINES_DEFAULT)
    if not isinstance(file_lines, int) or isinstance(file_lines, bool) or file_lines < 1:
        raise ConfigError(f"{source}: file_lines must be a positive integer")
    structure = raw.get("structure") if isinstance(raw.get("structure"), dict) else None
    return Settings(root=root, source=source, layout=layout, file_lines=file_lines, structure=structure)


def merge_block(name, defaults, value, source):
    if not isinstance(value, dict):
        raise ConfigError(f"{source}: layout.{name} must be an object")
    unknown = sorted(set(value) - set(defaults))
    if unknown:
        raise ConfigError(f"{source}: unknown layout.{name} setting(s): {', '.join(unknown)}")
    return dict(defaults, **value)


def require(ok, source, message):
    if not ok:
        raise ConfigError(f"{source}: {message}")


def string_list(value):
    return isinstance(value, list) and all(isinstance(v, str) and v for v in value)


def validate(layout, source):
    for key in ("sourceRoots", "entries", "targets", "exclude"):
        require(string_list(layout[key]), source, f"layout.{key} must be a list of non-empty strings")
    require(layout["sourceRoots"], source, "layout.sourceRoots must not be empty")
    aliases = layout["aliases"]
    require(isinstance(aliases, dict) and all(isinstance(k, str) and k and isinstance(v, str)
                                              for k, v in aliases.items()),
            source, "layout.aliases must map specifier prefixes to repo-relative folders")
    table = layout["contractsTable"]
    require(all(isinstance(table[k], str) and table[k] for k in table), source,
            "layout.contractsTable needs non-empty `file` and `heading` strings")
    for name, value in layout["thresholds"].items():
        require(isinstance(value, int) and not isinstance(value, bool) and value > 0, source,
                f"layout.thresholds.{name} must be a positive integer")
    validate_contracts(layout["contracts"], source)
    validate_exceptions(layout["exceptions"], source)


def validate_contracts(contracts, source):
    require(isinstance(contracts, list), source, "layout.contracts must be a list")
    for index, item in enumerate(contracts):
        where = f"layout.contracts[{index}]"
        require(isinstance(item, dict), source, f"{where} must be an object")
        unknown = sorted(set(item) - CONTRACT_KEYS)
        require(not unknown, source, f"{where}: unknown key(s) {', '.join(unknown)}")
        require(isinstance(item.get("from"), str) and item["from"], source, f"{where} needs `from`")
        require(string_list(item.get("to")) and item["to"], source, f"{where} needs a non-empty `to` list")
        require(isinstance(item.get("allowDynamic", False), bool), source, f"{where}.allowDynamic must be true/false")


def validate_exceptions(exceptions, source):
    require(isinstance(exceptions, list), source, "layout.exceptions must be a list")
    for index, item in enumerate(exceptions):
        where = f"layout.exceptions[{index}]"
        require(isinstance(item, dict), source, f"{where} must be an object")
        unknown = sorted(set(item) - EXCEPTION_KEYS)
        require(not unknown, source, f"{where}: unknown key(s) {', '.join(unknown)}")
        require(item.get("rule") in RULES, source, f"{where}.rule must be one of {', '.join(sorted(RULES))}")
        require(isinstance(item.get("path"), str) and item["path"], source, f"{where} needs `path`")
        require(isinstance(item.get("reason"), str) and item["reason"].strip(), source, f"{where} needs a `reason`")
