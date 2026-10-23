"""Turning what an external caller names (a command line, a path, a `-m` module) into repo paths.

A reference is one of: a repo path that exists, a repo path that is missing (the caller clearly
points into the repo: relative to the repo, under a working directory matched to the repo, or a
module whose package exists), dynamic (variables, globs, a path matched only by name: check by
hand), or external (system tools, packages, test files run by a test command). Deployed absolute
paths are matched to repo files by their longest common suffix of two or more path components
(/home/pi/scripts/python-packages/current/pi/apps/x.py -> pi/apps/x.py).
"""
import os
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from typing import Dict, List, Optional

REPO, MISSING, DYNAMIC, EXTERNAL = "repo", "missing", "dynamic", "external"
INTERPRETER = re.compile(r"^(python(\d+(\.\d+)?)?|pypy3?|node|nodejs|tsx|ts-node|deno|bun|bash|sh|zsh|dash|ash|ksh"
                         r"|ruby|perl|osascript)$")
SHELLS = re.compile(r"^(bash|sh|zsh|dash|ash|ksh)$")
WRAPPERS = {"env", "exec", "nohup", "sudo", "nice", "ionice", "caffeinate", "time", "npx", "command", "uv", "uvx"}
VALUE_FLAGS = {"-W", "-X", "--import", "-r", "--require", "--loader", "--experimental-loader", "--env-file",
               "-C", "--conditions", "--test-reporter", "--test-reporter-destination", "--test-name-pattern",
               "--watch-path", "--input-type", "--inspect-port", "-u", "-g"}
SUBCOMMANDS = {"watch", "run"}  # tsx watch x.ts, deno run x.ts, bun run x.ts
INLINE_CODE = {"-c", "-e", "--eval", "-p", "--print"}
FILE_TESTS = {"-e", "-f", "-r", "-s", "-x", "-d"}  # `test -r <path>` prechecks in units
SYSTEM_PREFIXES = ("/usr/", "/bin/", "/sbin/", "/etc/", "/run/", "/var/", "/tmp/", "/dev/", "/proc/", "/sys/",
                   "/opt/homebrew/", "/Library/", "/System/", "/Applications/", "/private/")
CODE_EXT = (".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".go", ".rb", ".pl",
            ".swift", ".applescript", ".scpt")
VARIABLE = re.compile(r"\$|%[a-zA-Z]")
GLOB = re.compile(r"[*?\[]")
DOTTED = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")
COMMAND_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
TEST_NAME = re.compile(r"(^|/)(tests?|__tests__)/|(^|/)test[_-]|_test\.|\.test\.|\.spec\.")


@dataclass
class Reference:
    kind: str  # REPO, MISSING, DYNAMIC or EXTERNAL
    path: str  # repo-relative for REPO/MISSING, the original text otherwise
    hint: str = ""


@dataclass
class Workdir:
    """Where a command runs. cwd: the repo folder (None: outside the repo). deploy: the absolute path
    that folder is deployed at. confident: matched by the repo's name or two or more path components,
    so a path that resolves to nothing there is missing rather than unknown."""
    cwd: Optional[str] = ""
    deploy: Optional[str] = None
    confident: bool = True


ROOT = Workdir()


@dataclass
class RepoIndex:
    root: str
    by_name: Dict[str, List[str]] = field(default_factory=dict)  # file name -> repo paths
    dirs_by_name: Dict[str, List[str]] = field(default_factory=dict)  # folder name -> repo folders

    @classmethod
    def build(cls, root: str, paths: List[str]) -> "RepoIndex":
        index, folders = cls(root=root), set()
        for path in paths:
            index.by_name.setdefault(posixpath.basename(path), []).append(path)
            folder = posixpath.dirname(path)
            while folder and folder not in folders:
                folders.add(folder)
                index.dirs_by_name.setdefault(posixpath.basename(folder), []).append(folder)
                folder = posixpath.dirname(folder)
        return index

    def exists(self, rel: str) -> bool:
        return os.path.exists(os.path.join(self.root, rel))

    def working_directory(self, value: str) -> Workdir:
        """A unit's or plist's working directory, matched to a repo folder by the repo's own name or
        by the longest common path suffix (/home/pi/phone-drop -> pi/projects/phone-drop)."""
        value = value.rstrip("/")
        if not value.startswith("/"):
            return Workdir(posixpath.normpath(value))
        parts = [p for p in value.split("/") if p]
        if parts and parts[-1] == os.path.basename(os.path.normpath(self.root)):
            return Workdir("", value)
        scored = sorted(((common_suffix(parts, d.split("/")), d) for d in self.dirs_by_name.get(parts[-1], [])
                         if parts), reverse=True)
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return Workdir(scored[0][1], value, confident=scored[0][0] >= 2)
        return Workdir(None)


def looks_like_path(token: str) -> bool:
    return "/" in token or token.endswith(CODE_EXT)


def common_suffix(a: List[str], b: List[str]) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


def match_deployed(token: str, index: RepoIndex) -> Reference:
    parts = [p for p in token.split("/") if p]
    if not parts:
        return Reference(EXTERNAL, token)
    scored = sorted(((common_suffix(parts, c.split("/")), c) for c in index.by_name.get(parts[-1], [])),
                    reverse=True)
    if scored and scored[0][0] >= 2 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
        return Reference(REPO, scored[0][1])
    if scored:
        return Reference(DYNAMIC, token, "matches by file name only: " + ", ".join(c for _, c in scored[:3]))
    return Reference(EXTERNAL, token)


def unknown(rel: str, token: str, where: Workdir) -> Reference:
    """A path that should be in the repo but isn't: missing when the working directory match is sure."""
    if where.confident:
        return Reference(MISSING, rel)
    return Reference(DYNAMIC, token, f"working directory matched by folder name only; not found as {rel}")


def resolve_path(token: str, index: RepoIndex, where: Workdir = ROOT) -> Reference:
    ref = classify_path(token, index, where)
    if ref.kind in (REPO, MISSING) and TEST_NAME.search(ref.path):
        return Reference(EXTERNAL, ref.path, "a test file; the test command is the contract")
    return ref


def classify_path(token: str, index: RepoIndex, where: Workdir) -> Reference:
    if VARIABLE.search(token):
        return Reference(DYNAMIC, token, "contains a variable")
    if GLOB.search(token):
        return Reference(EXTERNAL if TEST_NAME.search(token) else DYNAMIC, token, "glob")
    if token.startswith("~/"):
        return match_deployed(token[1:], index)
    if token.startswith("/"):
        token = token.rstrip("/") or "/"
        deploy = where.deploy
        if deploy and where.cwd is not None and (token == deploy or token.startswith(deploy + "/")):
            rel = posixpath.normpath(posixpath.join(where.cwd, posixpath.relpath(token, deploy)))
            if index.exists(rel):
                return Reference(REPO, rel)
            by_suffix = match_deployed(token, index)
            return by_suffix if by_suffix.kind == REPO else unknown(rel, token, where)
        if token.startswith(SYSTEM_PREFIXES):
            return Reference(EXTERNAL, token)
        return match_deployed(token, index)
    if where.cwd is None:
        return Reference(EXTERNAL, token)
    rel = posixpath.normpath(posixpath.join(where.cwd, token))
    if rel.startswith(".."):
        return Reference(EXTERNAL, token)
    return Reference(REPO, rel) if index.exists(rel) else unknown(rel, token, where)


def module_file(dotted: str, base: str, index: RepoIndex) -> Optional[str]:
    stem = posixpath.join(base, *dotted.split("."))
    return next((c for c in (stem + ".py", stem + "/__main__.py", stem + "/__init__.py") if index.exists(c)), None)


def resolve_module(dotted: str, index: RepoIndex, where: Workdir = ROOT) -> Reference:
    """`-m` from the working directory, else from the repo root (units often set PYTHONPATH there)."""
    bases = [b for b in (where.cwd, "") if b is not None]
    found = next((f for f in (module_file(dotted, b, index) for b in dict.fromkeys(bases)) if f), None)
    if found:
        return Reference(REPO, found)
    top = dotted.split(".")[0]
    if any(index.exists(posixpath.join(b, top)) or index.exists(posixpath.join(b, top + ".py")) for b in bases):
        return unknown(posixpath.join(bases[0], *dotted.split(".")) + ".py", dotted, where)
    return Reference(EXTERNAL, dotted)


def split_tokens(command: str) -> List[str]:
    try:
        return shlex.split(command, comments=False, posix=True)
    except ValueError:
        return command.split()


def command_references(command, index: RepoIndex, where: Workdir = ROOT, depth: int = 0) -> List[Reference]:
    """What one command (a string, or argv as a list) runs, as references. A string may chain
    commands with && || ; |; each is read separately."""
    if isinstance(command, str):
        return [r for part in COMMAND_SPLIT.split(command) if part
                for r in command_references(split_tokens(part), index, where, depth)]
    tokens, i = list(command), 0
    while i < len(tokens) and (re.match(r"^\w+=", tokens[i]) or tokens[i] in WRAPPERS
                               or (i and tokens[i - 1] == "env" and tokens[i].startswith("-"))):
        i += 1
    if i >= len(tokens):
        return []
    program, name = tokens[i], posixpath.basename(tokens[i])
    if name in ("test", "[") and len(tokens) > i + 2 and tokens[i + 1] in FILE_TESTS:
        return [resolve_path(tokens[i + 2], index, where)]
    if not INTERPRETER.match(name):
        return [resolve_path(program, index, where)] if looks_like_path(program) else []
    return interpreter_references(program, tokens[i + 1:], index, where, depth)


def interpreter_references(program, args, index, where, depth) -> List[Reference]:
    shell = SHELLS.match(posixpath.basename(program))
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-m" and i + 1 < len(args):
            return [resolve_module(args[i + 1], index, where)]
        if arg in INLINE_CODE or (shell and re.match(r"^-[a-z]*c$", arg)):
            inline = args[i + 1] if i + 1 < len(args) else ""
            return command_references(inline, index, where, depth + 1) if shell and depth < 2 else []
        if arg in VALUE_FLAGS:
            i += 2
            continue
        if arg.startswith("-") or arg in SUBCOMMANDS:
            i += 1
            continue
        return [resolve_path(arg, index, where)]
    return []


def reference_strings(value: str, index: RepoIndex, where: Workdir = ROOT) -> List[Reference]:
    """A manifest or table value: a command when it has spaces, a path or dotted module otherwise.
    `{name:path}` placeholders (van-compute argv) contribute their path."""
    refs = [resolve_path(inner, index, where) for inner in re.findall(r"\{[a-z_]+:([^}]+)\}", value)
            if looks_like_path(inner)]
    value = re.sub(r"\{[a-z_]+:[^}]*\}", " ", value).strip()
    if not value or "://" in value:
        return refs
    if re.search(r"\s", value):
        return refs + command_references(value, index, where)
    if DOTTED.match(value) and not value.endswith(CODE_EXT + (".json", ".md")):
        ref = resolve_module(value, index, where)
        return refs + ([ref] if ref.kind != EXTERNAL else [])
    return refs + ([resolve_path(value, index, where)] if looks_like_path(value) else [])
