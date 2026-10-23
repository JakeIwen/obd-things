"""Advisory import-graph metrics for drift_check.py `check`: import cycles (Python and JS/TS) and
grab-bag modules (JS/TS). Stdlib only; parses text, never imports or executes project code.

Deliberately conservative (advisory, never enforced):
- Python: module-level imports only (function-level imports are the usual way to break a cycle, and
  `if TYPE_CHECKING:` blocks never run). Relative imports resolve against the package directory;
  absolute ones against the importer's directory, then each parent up to the repo root, then src/.
  Unresolved names (stdlib, third-party) are ignored.
- JS/TS: static `import ... from`, `export ... from`, side-effect `import '...'` and `require('...')`
  with relative specifiers only; type-only imports are erased by the compiler and skipped. Path
  aliases (tsconfig paths, package imports) are not resolved, so cycles through them are missed.
- Grab-bag: a JS/TS module from which importers take named imports that fall into two or more groups
  with disjoint importer sets (at least three names in all). A module that any importer reads via
  `import * as` is skipped, since which names it uses is unknown. Python's `import m; m.x` is skipped.
"""
import ast
import collections
import os
import re
import warnings

JS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
JS_TO_TS = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}
JS_FROM = re.compile(r"(?:^|[;}\n])\s*(import|export)\s+(type\s+)?([\w$\s,{}*]*?)\s*from\s*['\"]([^'\"\n]+)['\"]")
JS_BARE = re.compile(r"(?:^|[;}\n])\s*import\s*['\"]([^'\"\n]+)['\"]")
JS_REQUIRE = re.compile(r"(?<![\w$.])require\(\s*['\"]([^'\"\n]+)['\"]\s*\)")


# ---------- Python ----------

def module_level_imports(tree):
    """Import nodes executed at import time: top level, inside if/try/with, not TYPE_CHECKING blocks."""
    todo = list(tree.body)
    while todo:
        node = todo.pop(0)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, ast.If):
            if "TYPE_CHECKING" not in ast.dump(node.test):
                todo += node.body + node.orelse
        elif isinstance(node, ast.Try):
            todo += node.body + node.orelse + node.finalbody + [s for h in node.handlers for s in h.body]
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            todo += node.body


def py_module_file(base_dir, dotted, files):
    rel = os.path.normpath(os.path.join(base_dir, *dotted.split("."))) if dotted else os.path.normpath(base_dir or ".")
    if rel.startswith(".."):
        return None
    rel = "" if rel == "." else rel
    cands = ([rel + ".py"] if rel else []) + [os.path.join(rel, "__init__.py")]
    return next((c for c in cands if c in files), None)


def py_search_dirs(path):
    d = os.path.dirname(path)
    dirs = [d]
    while d:
        d = os.path.dirname(d)
        dirs.append(d)
    return dirs + ["src"]


def py_edges(path, text, files):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = ast.parse(text)
    pkg = os.path.dirname(path)
    out = set()
    for node in module_level_imports(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = next((t for d in py_search_dirs(path) for t in [py_module_file(d, alias.name, files)] if t), None)
                if target:
                    out.add(target)
            continue
        if node.level:
            base = pkg
            for _ in range(node.level - 1):
                base = os.path.dirname(base)
            bases = [base]
        else:
            bases = py_search_dirs(path)
        for b in bases:
            mod = py_module_file(b, node.module or "", files)
            subs = [py_module_file(b, ((node.module or "") + "." + a.name).lstrip("."), files) for a in node.names]
            subs = [s for s in subs if s]
            if mod or subs:
                out.update(subs or [mod])
                break
    out.discard(path)
    return out


# ---------- JS/TS ----------

def strip_js_comments(text):
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def js_resolve(path, spec, files):
    if not spec.startswith("."):
        return None
    spec = spec.split("?")[0]
    base = os.path.normpath(os.path.join(os.path.dirname(path), spec))
    if base.startswith(".."):
        return None
    stem, ext = os.path.splitext(base)
    cands = [base] + [base + e for e in JS_EXT] + [os.path.join(base, "index" + e) for e in JS_EXT]
    cands += [stem + e for e in JS_TO_TS.get(ext, ())]
    return next((c for c in cands if c in files and c.endswith(JS_EXT)), None)


def js_imports(path, text, files):
    """[(target, names or None for namespace, type_only)] for relative static imports."""
    text = strip_js_comments(text)
    out = []
    for m in JS_FROM.finditer(text):
        target = js_resolve(path, m.group(4), files)
        if not target:
            continue
        clause = m.group(3).strip()
        if m.group(2):
            out.append((target, set(), True))
            continue
        if "* as" in clause.replace("  ", " ") or (m.group(1) == "export" and clause.startswith("*")):
            out.append((target, None, False))
            continue
        names, typed = set(), True
        brace = re.search(r"\{([^}]*)\}", clause)
        if brace:
            for spec in brace.group(1).split(","):
                spec = spec.strip()
                if not spec:
                    continue
                is_type = spec.startswith("type ")
                typed = typed and is_type
                names.add(spec.replace("type ", "", 1).split(" as ")[0].strip())
        default = clause.split("{")[0].strip().rstrip(",").strip()
        if default and m.group(1) == "import":
            names.add("default")
            typed = False
        out.append((target, names, typed and bool(names)))
    for rx in (JS_BARE, JS_REQUIRE):
        for m in rx.finditer(text):
            target = js_resolve(path, m.group(1), files)
            if target:
                out.append((target, None, False))
    return out


# ---------- graph ----------

def cycles(graph):
    """Strongly connected components with two or more modules (Tarjan, iterative)."""
    index, low, on, stack, out, counter = {}, {}, set(), [], [], [0]
    for start in sorted(graph):
        if start in index:
            continue
        work = [(start, iter(sorted(graph.get(start, ()))))]
        index[start] = low[start] = counter[0]
        counter[0] += 1
        stack.append(start)
        on.add(start)
        while work:
            node, it = work[-1]
            nxt = next(it, None)
            if nxt is not None:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on.add(nxt)
                    work.append((nxt, iter(sorted(graph.get(nxt, ())))))
                elif nxt in on:
                    low[node] = min(low[node], index[nxt])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1:
                    out.append(sorted(comp))
    return sorted(out, key=lambda c: (-len(c), c))


def grab_bags(uses):
    """uses: module -> {importer: names or None}. Modules whose imported names split into disjoint groups."""
    out = []
    for mod, by_importer in sorted(uses.items()):
        if any(names is None for names in by_importer.values()):
            continue
        parent = {}

        def find(x):
            while parent.setdefault(x, x) != x:
                x = parent[x]
            return x
        for names in by_importer.values():
            names = sorted(names)
            for a in names:
                find(a)
            for a in names[1:]:
                parent[find(a)] = find(names[0])
        if len(parent) < 3:
            continue
        groups = collections.defaultdict(lambda: {"names": set(), "importers": set()})
        for imp, names in by_importer.items():
            for n in names:
                g = groups[find(n)]
                g["names"].add(n)
                g["importers"].add(imp)
        if len(groups) > 1:
            out.append({"module": mod, "groups": sorted(
                ({"names": sorted(g["names"]), "importers": sorted(g["importers"])} for g in groups.values()),
                key=lambda g: (-len(g["importers"]), g["names"]))})
    return sorted(out, key=lambda r: (-len(r["groups"]), r["module"]))


def collect(root, paths, tests=()):
    """Advisory import metrics for the repo-relative source paths (non-Python/JS paths are ignored).
    Test files count toward cycles but not as grab-bag importers (a test imports one helper at a time)."""
    files = set(paths)
    py_graph, js_graph, uses, errors = {}, {}, collections.defaultdict(dict), 0
    for path in sorted(files):
        is_py, is_js = path.endswith(".py"), path.endswith(JS_EXT)
        if not (is_py or is_js):
            continue
        try:
            with open(os.path.join(root, path), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if is_py:
            try:
                py_graph[path] = py_edges(path, text, files)
            except (SyntaxError, ValueError):
                errors += 1
            continue
        edges = set()
        for target, names, type_only in js_imports(path, text, files):
            if target == path:
                continue
            if not type_only:
                edges.add(target)
            if (names is None or names) and path not in tests:
                prev = uses[target].get(path, set())
                uses[target][path] = None if names is None or prev is None else prev | names
        js_graph[path] = edges
    return {"python_cycles": cycles(py_graph), "js_cycles": cycles(js_graph), "grab_bags": grab_bags(uses),
            "python_unparsed": errors}
