"""Build repo/map/lines.json from the probe coverage run.
usage: build_map2.py COV_DIR SQLGLOT_DIR TESTS_JSON OUT_DIR
lines.json = {files: {rel: {line: setid}}, sets: [[test numbers]], mod: {rel: setid}, fixtures: {path: setid}}
 mod[rel] = tests that instantiated a dialect whose static sqlglot import closure contains rel
"""
import ast, glob, json, os, sys, collections
from coverage import CoverageData

cov_dir, src, tests_json, out_dir = sys.argv[1:5]
tests = json.load(open(tests_json))["tests"]
idx = {t["id"]: t["number"] for t in tests}
klass = collections.defaultdict(set)  # "file::Class" -> tests; setUpClass work runs in the first test's setup phase
for t in tests:
    klass[t["id"].rsplit("::", 1)[0]].add(t["number"])


def ctx_tests(c):
    nid, _, phase = c.partition("|")
    if nid not in idx:
        return set()
    return klass[nid.rsplit("::", 1)[0]] if phase == "setup" else {idx[nid]}
sets, setid = [], {}


def sid(nums):
    key = tuple(sorted(set(nums)))
    if key not in setid:
        setid[key] = len(sets); sets.append(list(key))
    return setid[key]


# 1. per-line runtime contexts ("" = import time, dropped)
acc = collections.defaultdict(lambda: collections.defaultdict(set))
for cd in cov_dir.split(","):
    d = CoverageData(os.path.join(cd, ".coverage")); d.read()
    for f in d.measured_files():
        rel = f[f.index("/sqlglot/sqlglot/") + len("/sqlglot/"):]
        for line, cs in d.contexts_by_lineno(f).items():
            for c in cs:
                if c:
                    acc[rel][line] |= ctx_tests(c)
files = {rel: {ln: sid(n) for ln, n in lm.items() if n} for rel, lm in acc.items()}

# 2. static import graph of sqlglot modules
def modfile(name):
    p = os.path.join(src, *name.split("."))
    for cand in (p + ".py", os.path.join(p, "__init__.py")):
        if os.path.exists(cand):
            return os.path.relpath(cand, src)
    return None


graph, lazy = {}, []  # lazy: (importer rel, line, target rel) for imports inside functions


def targets(n):
    names = []
    if isinstance(n, ast.Import):
        names = [a.name for a in n.names]
    elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
        names = [n.module] + [n.module + "." + a.name for a in n.names]
    out = set()
    for nm in names:
        if nm.startswith("sqlglot"):
            parts = nm.split(".")
            for i in range(1, len(parts) + 1):  # package __init__s run too
                m = modfile(".".join(parts[:i]))
                if m:
                    out.add(m)
    return out


def walk(node, rel, deps, in_func):
    for c in ast.iter_child_nodes(node):
        if isinstance(c, ast.If) and "TYPE_CHECKING" in ast.unparse(c.test):
            walk_list = c.orelse
            for e in walk_list:
                walk(ast.Module(body=[e], type_ignores=[]), rel, deps, in_func)
            continue
        if isinstance(c, (ast.Import, ast.ImportFrom)):
            for tg in targets(c):
                if in_func:
                    lazy.append((rel, c.lineno, tg))
                else:
                    deps.add(tg)
        walk(c, rel, deps, in_func or isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)))


for path in glob.glob(os.path.join(src, "sqlglot", "**", "*.py"), recursive=True):
    rel = os.path.relpath(path, src)
    deps = set()
    walk(ast.parse(open(path).read()), rel, deps, False)
    graph[rel] = deps


def closure(rel, seen=None):
    seen = set() if seen is None else seen
    if rel in seen:
        return seen
    seen.add(rel)
    for dep in graph.get(rel, ()):
        closure(dep, seen)
    return seen


# 3. probe output: dialects used and fixtures opened per test
used = collections.defaultdict(set)   # module rel -> tests
fix = collections.defaultdict(set)
cls = collections.defaultdict(set)
ccache = {}
for pf in [g for cd in cov_dir.split(",") for g in glob.glob(os.path.join(cd, "probe.*.json"))]:
    pr = json.load(open(pf))
    for nodeid, paths in pr["open"].items():
        if nodeid in idx:
            for p in paths:
                fix[p].add(idx[nodeid])
    for nodeid, names in pr.get("expr", {}).items():
        if nodeid in idx:
            for nm in names:
                cls[nm].add(idx[nodeid])
    for nodeid, classes in pr["dialect"].items():
        if nodeid not in idx:
            continue
        for dcl in classes:
            m = modfile(dcl.rsplit(".", 1)[0])
            if m not in ccache:
                ccache[m] = closure(m) if m else set()
            for r in ccache[m]:
                used[r].add(idx[nodeid])

for rel, ln, tg in lazy:
    sidv = files.get(rel, {}).get(ln)
    if sidv is not None:
        for r in closure(tg):
            used[r].update(sets[sidv])

out = {"files": files, "mod": {r: sid(t) for r, t in used.items()},
       "fixtures": {p: sid(t) for p, t in fix.items()},
       "cls": {c: sid(t) for c, t in cls.items() if c.startswith("sqlglot.")}, "sets": sets}
os.makedirs(out_dir, exist_ok=True)
json.dump(out, open(os.path.join(out_dir, "lines.json"), "w"), separators=(",", ":"))
print(len(files), "files", len(sets), "sets", len(out["mod"]), "mods", len(out["fixtures"]), "fixtures")
