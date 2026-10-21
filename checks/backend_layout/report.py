"""The run's result record and its two renderings: human text (default) and JSON (--json).
Coverage is always shown as separate counts, so an empty finding list can never hide an empty scan."""
import json
from dataclasses import dataclass, field
from typing import Dict, List

from .baseline import Baseline
from .contracts_table import Table
from .edges import Graph
from .files import Inventory
from .findings import ADVISED, BASELINED, EXEMPT, FAILING, FAILING_STATUSES, NEW, Finding
from .targets import Targets

SHOWN_PER_RULE = 15
STATUS_ORDER = (NEW, FAILING, BASELINED, ADVISED, EXEMPT)
STATUS_TITLES = {NEW: "NEW (fails the run)", FAILING: "BLOCKING (fails the run)", BASELINED: "baselined",
                 ADVISED: "advisory", EXEMPT: "exempt (layout.exceptions)"}


@dataclass
class Result:
    root: str
    config: str
    inventory: Inventory
    graph: Graph
    baseline: Baseline
    table: Table
    targets: Targets
    findings: List[Finding]
    exceptions: List[dict]
    exceptions_used: List[int]
    warnings: List[str] = field(default_factory=list)

    @property
    def scanned(self) -> int:
        return len(self.inventory.subjects)

    def count(self, status: str) -> int:
        return sum(1 for f in self.findings if f.status == status)

    @property
    def exit_code(self) -> int:
        failing = any(f.status in FAILING_STATUSES for f in self.findings)
        return 1 if failing or not self.scanned else 0

    def verdict(self) -> str:
        if not self.scanned:
            return "FAIL: zero files scanned (check layout.sourceRoots and exclusions)"
        if self.exit_code:
            return f"FAIL: {self.count(NEW)} new ratchet finding(s), {self.count(FAILING)} blocking"
        if self.baseline.layout is None:
            return "OK (advisory only: no layout baseline yet)"
        return "OK"

    def counts(self) -> Dict[str, object]:
        g = self.graph
        return {
            "filesScanned": self.scanned, "byKind": self.inventory.scanned_by_kind(),
            "skippedLarge": self.inventory.skipped_large, "skippedUi": self.inventory.skipped_ui,
            "findings": {s: self.count(s) for s in STATUS_ORDER},
            "exceptionsApplied": self.count(EXEMPT), "exceptionsUnused": self.exceptions_used.count(0),
            "parseErrors": len(self.inventory.parse_errors), "targetFileErrors": len(self.targets.errors),
            "importsResolved": g.resolved, "importsUnresolved": len(g.unresolved), "importsUnsupported": g.unsupported,
            "targetFiles": len(self.targets.files), "targets": len(self.targets.found) + len(self.targets.missing),
            "dynamicTargets": len(self.targets.dynamic),
        }


def baseline_line(result: Result) -> str:
    b = result.baseline
    if b.layout is None:
        where = "no baseline file" if not b.exists else "no `layout` key"
        return f"baseline: {where} at {b.path}; every rule is advisory until check_layout.py rebaseline"
    total = sum(len(v) for v in b.layout.get("findings", {}).values())
    return f"baseline: layout key from {b.layout.get('date', '?')} at {str(b.layout.get('head', ''))[:8] or '?'} " \
           f"({total} recorded finding(s))"


def summary_lines(result: Result) -> List[str]:
    c = result.counts()
    kinds = ", ".join(f"{k} {n}" for k, n in sorted(c["byKind"].items())) or "none"
    table = result.table
    table_text = f"{table.file} ({len(table.rows)} row(s))" if table.found else f"none ({table.note})"
    f = c["findings"]
    return [
        f"backend-layout check: {result.root} (config: {result.config})",
        f"files scanned: {c['filesScanned']} ({kinds}); skipped: {c['skippedLarge']} too large, "
        f"{c['skippedUi']} UI (React/Preact apps and .tsx/.jsx, react-app's)",
        f"imports: {c['importsResolved']} resolved, {c['importsUnresolved']} unresolved, "
        f"{c['importsUnsupported']} unsupported (shell source lines, Go files, unconfigured aliases)",
        f"errors: {c['parseErrors']} parse, {c['targetFileErrors']} target file; warnings: {len(result.warnings)}",
        baseline_line(result),
        f"targets: {c['targetFiles']} file(s) read, {c['targets']} target(s), {c['dynamicTargets']} dynamic "
        f"(check by hand); Contracts table: {table_text}",
        f"findings: {f[NEW]} new, {f[FAILING]} blocking, {f[BASELINED]} baselined, {f[ADVISED]} advisory, "
        f"{f[EXEMPT]} exempt",
        f"exceptions applied: {c['exceptionsApplied']} finding(s) covered; {c['exceptionsUnused']} exception(s) "
        f"matched nothing",
    ]


def finding_lines(findings: List[Finding]) -> List[str]:
    lines, shown_groups = [], set()
    by_rule: Dict[str, List[Finding]] = {}
    for f in findings:
        by_rule.setdefault(f.rule, []).append(f)
    for rule, items in sorted(by_rule.items()):
        lines.append(f"  {rule} ({len(items)})")
        shown = 0
        for f in items:
            if f.group and f.group in shown_groups:
                continue
            if shown >= SHOWN_PER_RULE:
                lines.append(f"    ... more in --json")
                break
            shown += 1
            if f.group:
                shown_groups.add(f.group)
                members = [x.path for x in items if x.group == f.group]
                lines.append(f"    {f.message}")
                lines.append(f"      files: {', '.join(members)}")
            else:
                where = f"{f.path}:{f.line}" if f.line and f.rule != "declared-target" else f.path
                lines.append(f"    {where}: {f.message}" + (f" [exception: {f.exception}]" if f.exception else ""))
    return lines


def render_text(result: Result) -> str:
    lines = summary_lines(result)
    for status in STATUS_ORDER:
        chosen = [f for f in result.findings if f.status == status]
        if chosen:
            lines += ["", STATUS_TITLES[status] + ":"] + finding_lines(chosen)
    sections = [("exceptions in force", [f"{e['rule']} {e['path']}: {e['reason']} (covers {n})"
                                         for e, n in zip(result.exceptions, result.exceptions_used)]),
                ("warnings", result.warnings),
                ("dynamic targets (check by hand)", result.targets.dynamic),
                ("unresolved imports", result.graph.unresolved),
                ("parse errors", result.inventory.parse_errors + result.targets.errors)]
    for title, items in sections:
        if items:
            lines += ["", f"{title} ({len(items)}):"] + [f"  {x}" for x in items[:SHOWN_PER_RULE]]
            if len(items) > SHOWN_PER_RULE:
                lines.append("  ... more in --json")
    lines += ["", "verdict: " + result.verdict()]
    return "\n".join(lines)


def render_json(result: Result) -> str:
    data = {
        "root": result.root, "config": result.config, "counts": result.counts(),
        "baseline": {"path": result.baseline.path, "exists": result.baseline.exists,
                     "layout": None if result.baseline.layout is None else
                     {k: v for k, v in result.baseline.layout.items() if k != "findings"}},
        "contractsTable": {"file": result.table.file, "found": result.table.found, "note": result.table.note,
                           "rows": len(result.table.rows)},
        "findings": [f.as_dict() for f in result.findings],
        "exceptions": [dict(e, covers=n) for e, n in zip(result.exceptions, result.exceptions_used)],
        "warnings": result.warnings, "dynamicTargets": result.targets.dynamic,
        "unresolvedImports": result.graph.unresolved,
        "parseErrors": result.inventory.parse_errors + result.targets.errors,
        "verdict": result.verdict(), "exitCode": result.exit_code,
    }
    return json.dumps(data, indent=2, sort_keys=False)
