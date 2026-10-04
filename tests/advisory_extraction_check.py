"""Check that expanding extracted steps reproduces the original method ASTs.

Usage: python tests/advisory_extraction_check.py OLD_ADVISORIES NEW_ADVISORIES
This is independent of the lifecycle data oracle: string constants (including
SQL whitespace), every control-flow node and statement order must be identical.
Only new helper-call scaffolding, helper docstrings and terminal return values
are removed. Existing methods, assertions and return/continue nodes are not.
"""
from __future__ import annotations

import ast
import copy
from pathlib import Path
import sys


TARGETS = ("record_advisory_assessments", "_record_tiered_notification_locked")


def methods(path):
    tree = ast.parse(Path(path).read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    return {node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)}


def check(before, after):
    old, new = methods(before), methods(after)
    helpers = new.keys() - old.keys()

    class Expand(ast.NodeTransformer):
        def visit_statement(self, node):
            call = node.value
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "self" and call.func.attr in helpers):
                return self.generic_visit(node)
            helper = new[call.func.attr]
            positional = [arg.arg for arg in helper.args.args if arg.arg != "self"]
            supplied = dict(zip(positional, call.args))
            supplied.update({kw.arg: kw.value for kw in call.keywords})
            expected = set(positional) | {arg.arg for arg in helper.args.kwonlyargs}
            assert set(supplied) == expected, helper.name
            # No hidden renaming, expression evaluation or argument conversions.
            assert all(isinstance(value, ast.Name) and value.id == key
                       for key, value in supplied.items()), helper.name
            body = copy.deepcopy(helper.body)
            if (isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
            if isinstance(body[-1], ast.Return):
                returned = body.pop().value
                assert isinstance(node, ast.Assign), helper.name
                assert ast.dump(node.targets[0]).replace("Store()", "Load()") == ast.dump(returned), helper.name
            else:
                assert isinstance(node, ast.Expr), helper.name
            expanded = []
            for statement in body:
                result = self.visit(statement)
                expanded.extend(result if isinstance(result, list) else [result])
            return expanded

        visit_Expr = visit_statement
        visit_Assign = visit_statement

    for name in TARGETS:
        expanded = Expand().visit(copy.deepcopy(new[name]))
        assert ast.dump(old[name]) == ast.dump(expanded), name
        print(f"{name}: expanded AST identical, including SQL literals and control flow")
    print(f"{len(helpers)} new helpers checked")


if __name__ == "__main__":
    check(*sys.argv[1:])
