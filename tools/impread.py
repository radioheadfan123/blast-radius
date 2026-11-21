"""Which class attributes do import and test collection read, keyed by the class that defines them -> JSON list.
Copies sqlglot, rewrites every `x.UPPER` load and getattr() call to record the owning class, then runs
pytest --collect-only after importing every sqlglot module (as the map probe does).
usage: impread.py SQLGLOT_DIR WORK_DIR OUT_JSON   (venv python)"""
import ast, os, shutil, subprocess, sys

src, work, out = sys.argv[1:4]
shutil.rmtree(work, ignore_errors=True)
shutil.copytree(src, work, ignore=shutil.ignore_patterns(".git", "__pycache__"))


class T(ast.NodeTransformer):
    def visit_Attribute(self, n):
        self.generic_visit(n)
        if isinstance(n.ctx, ast.Load) and n.attr.isupper():
            return ast.Call(ast.Name("__rec__", ast.Load()), [n.value, ast.Constant(n.attr)], [])
        return n

    def visit_Call(self, n):
        self.generic_visit(n)
        if isinstance(n.func, ast.Name) and n.func.id == "getattr":
            n.func = ast.Name("__recg__", ast.Load())
        return n


for top in ("sqlglot", "tests"):
    for root, _, fs in os.walk(os.path.join(work, top)):
        for f in fs:
            if f.endswith(".py"):
                p = os.path.join(root, f)
                t = ast.fix_missing_locations(T().visit(ast.parse(open(p).read())))
                open(p, "w").write(ast.unparse(t))

open(os.path.join(work, "recplugin.py"), "w").write('''
import builtins, enum, importlib, json, pkgutil
seen, on = set(), [True]


def _note(obj, name, v):
    if not on[0] or not isinstance(name, str) or isinstance(obj, enum.EnumMeta):
        return
    if isinstance(obj, super):
        seen.add("*." + name)
        return
    d = getattr(obj, "__dict__", None)
    if not isinstance(obj, type) and isinstance(d, dict) and name in d:
        return
    for k in obj.__mro__ if isinstance(obj, type) else type(obj).__mro__:
        if name in vars(k):
            seen.add(k.__module__ + "." + k.__qualname__ + "." + name)
            return


def __rec__(obj, name):
    v = getattr(obj, name)
    _note(obj, name, v)
    return v


def __recg__(obj, name, *d):
    v = getattr(obj, name, *d)
    _note(obj, name, v)
    return v


builtins.__rec__, builtins.__recg__ = __rec__, __recg__


def pytest_sessionstart(session):
    import sqlglot
    print("sqlglot from", sqlglot.__file__)
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if not m.name.endswith("__main__"):
            try:
                importlib.import_module(m.name)
            except Exception as e:  # noqa
                print("import fail", m.name, e)


def pytest_collection_finish(session):
    on[0] = False
    print("collected", len(session.items))
    json.dump(sorted(seen), open("impread.json", "w"))
''')
r = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "recplugin", "-p", "no:cacheprovider", "tests"],
                   cwd=work, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": work})
print(r.stdout[-1500:], r.stderr[-1500:])
shutil.copy(os.path.join(work, "impread.json"), out)
