"""Command line: run every rule, apply exceptions and the baseline, report, and exit
0 clean, 1 on new ratchet findings / declared-target failures / zero files scanned, 2 on config errors."""
import argparse
import os
import sys
from typing import List, Optional

from . import (advisory_rules, baseline, config, contracts_table, edges, files, git_adapter, structure_rules,
               targets)
from .findings import Finding
from .references import RepoIndex
from .report import Result, render_json, render_text

USAGE = """\
check_layout.py [check] [--root DIR] [--config FILE] [--baseline FILE] [--json]
check_layout.py rebaseline [--root DIR] [--config FILE] [--baseline FILE] [--allow-dirty]

--root defaults to the current directory. --config reads a file shaped like .codeshape/config.json
instead of the repo's own (a read-only audit). rebaseline writes only the `layout` key of the
baseline and leaves the file unstaged."""


def parse_args(argv: Optional[List[str]]):
    ap = argparse.ArgumentParser(prog="check_layout.py", description=USAGE,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="check", choices=["check", "rebaseline"])
    ap.add_argument("--root", default=".", help="the repo (or app) folder to check")
    ap.add_argument("--config", help="config file to read instead of <root>/.codeshape/config.json")
    ap.add_argument("--baseline", help="baseline file (default <root>/.codeshape/baseline.json)")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    ap.add_argument("--allow-dirty", action="store_true", help="rebaseline with uncommitted changes")
    return ap.parse_args(argv)


def collect_findings(settings, inventory, graph, table, found) -> List[Finding]:
    structure = structure_rules.react_structure(settings, inventory)
    contracts = structure_rules.active_contracts(settings, structure)

    def listed(path: str) -> bool:
        return table.lists(path) or path in found.found

    return (structure_rules.contract_findings(graph, contracts)
            + targets.findings(table, found)
            + structure_rules.prefix_group_findings(inventory, graph, settings)
            + structure_rules.entry_heavy_findings(inventory, settings)
            + structure_rules.kind_folder_findings(inventory)
            + structure_rules.cycle_findings(graph, inventory, structure)
            + advisory_rules.fan_in_size_findings(inventory, graph, settings)
            + advisory_rules.shim_stale_findings(inventory, graph, listed)
            + advisory_rules.effects_in_rules_findings(inventory)
            + advisory_rules.shell_size_findings(inventory, settings))


def run(root: str, config_path: Optional[str], baseline_path: Optional[str]) -> Result:
    settings = config.load(root, config_path)
    inventory = files.discover(settings)
    index = RepoIndex.build(root, inventory.paths)
    graph = edges.build(inventory, settings)
    table = contracts_table.read(root, settings["contractsTable"], index)
    found = targets.collect(settings, inventory.paths, index)
    findings = collect_findings(settings, inventory, graph, table, found)
    base = baseline.load(baseline_path or os.path.join(root, ".codeshape", "baseline.json"))
    head = (base.layout or {}).get("head", "")
    used = baseline.classify(findings, settings["exceptions"], base, git_adapter.renames_since(root, head))
    warnings = list(settings.warnings)
    warnings += [f"layout.exceptions[{i}] ({e['rule']} {e['path']}) matched nothing"
                 for i, (e, n) in enumerate(zip(settings["exceptions"], used)) if not n]
    return Result(root=root, config=settings.source, inventory=inventory, graph=graph, baseline=base, table=table,
                  targets=found, findings=findings, exceptions=settings["exceptions"], exceptions_used=used,
                  warnings=warnings)


def rebaseline(result: Result, allow_dirty: bool) -> int:
    dirty = git_adapter.dirty_paths(result.root)
    if dirty and not allow_dirty:
        print(f"rebaseline refused: {len(dirty)} uncommitted change(s) (e.g. {dirty[0]}). Commit first, or pass "
              f"--allow-dirty.", file=sys.stderr)
        return 2
    try:
        recorded = baseline.rebaseline(result.baseline, result.findings, result.root)
    except baseline.RebaselineError as e:
        print(f"rebaseline refused: {e}", file=sys.stderr)
        return 2
    print(f"layout baseline written to {result.baseline.path}: {recorded} ratcheted finding(s) recorded; "
          f"other keys untouched. Commit it with your next commit (it is not staged).")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"config error: root is not a folder: {root}", file=sys.stderr)
        return 2
    try:
        result = run(root, args.config, args.baseline)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if args.mode == "rebaseline":
        if not result.scanned:
            print("rebaseline refused: zero files scanned", file=sys.stderr)
            return 1
        return rebaseline(result, args.allow_dirty)
    print(render_json(result) if args.json else render_text(result))
    return result.exit_code
