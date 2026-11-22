"""Record, per test, which keys it looked up in any EXPRESSION_METADATA dict (sqlglot/typing/*.py globals and the
dialect class copies) and whether it read one in bulk (iterate, copy, compare, mutate).
record (pytest plugin, run in a scratch copy of the pinned tree):
    EMAP_OUT=<dir> PYTHONPATH=<this tools dir>:<selector dir> python -m pytest -p emap -n 2 tests/
merge:  python emap.py <tests.json> <dir> > map/emap.json   ({"k": {key fp: [test numbers]}, "b": [test numbers]})
A lookup outside a test's call phase (setUpClass, fixtures) counts for every test of that class or module."""
import importlib, json, os, pkgutil, sys

CUR = ["<collect>"]
REC = {}  # unit -> [set of key fps, bulk]
KFP = {}


def note(k=None):
    r = REC.setdefault(CUR[0], [set(), False])
    if k is None:
        r[1] = True
    else:
        try:
            r[0].add(KFP[k])
        except (KeyError, TypeError):
            import fpu
            r[0].add(fpu.kfp(k))
            try:
                KFP[k] = fpu.kfp(k)
            except TypeError:  # unhashable: never a dict key anyway
                pass


class Rec(dict):
    __slots__ = ()

    def get(self, k, d=None):
        note(k)
        return dict.get(self, k, d)

    def __getitem__(self, k):
        note(k)
        return dict.__getitem__(self, k)

    def __contains__(self, k):
        note(k)
        return dict.__contains__(self, k)


def _bulk(f):
    def w(self, *a, **kw):
        note()
        return f(self, *a, **kw)
    return w


for _n in ("__iter__", "__reversed__", "keys", "items", "values", "copy", "__eq__", "__ne__", "__or__", "__ror__",
           "__ior__", "__repr__", "__reduce_ex__", "__setitem__", "__delitem__", "setdefault", "pop", "popitem", "update",
           "clear"):
    setattr(Rec, _n, _bulk(getattr(dict, _n)))


def install():
    import sqlglot.dialects, sqlglot.typing
    for pkg in (sqlglot.dialects, sqlglot.typing):
        for m in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
            try:
                importlib.import_module(m.name)
            except Exception:  # noqa: the same module fails the same way without the plugin
                pass
    new = {}

    def wrap(v):
        if id(v) not in new:
            new[id(v)] = Rec(v)
        return new[id(v)]

    def cls(c):
        for a, v in list(vars(c).items()):
            if "EXPRESSION_METADATA" in a and type(v) is dict:
                setattr(c, a, wrap(v))
            elif isinstance(v, type) and v.__module__ == c.__module__ and v.__qualname__.startswith(c.__qualname__ + "."):
                cls(v)

    for name, mod in list(sys.modules.items()):
        if mod is None or not (name == "sqlglot" or name.startswith("sqlglot.")):
            continue
        for a, v in list(vars(mod).items()):
            if "EXPRESSION_METADATA" in a and type(v) is dict:
                setattr(mod, a, wrap(v))
            elif isinstance(v, type) and v.__module__ == name:
                cls(v)
    print(f"emap: wrapped {len(new)} dicts", file=sys.stderr)


try:
    import pytest
except ImportError:
    pytest = None

if pytest:
    def pytest_configure(config):
        install()

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_setup(item):
        CUR[0] = "U" + item.nodeid.rsplit("::", 1)[0]
        yield

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(item):
        CUR[0] = "T" + item.nodeid
        yield
        CUR[0] = "U" + item.nodeid.rsplit("::", 1)[0]

    def pytest_sessionfinish(session):
        out = os.environ.get("EMAP_OUT")
        if out:
            with open(os.path.join(out, os.environ.get("PYTEST_XDIST_WORKER", "main") + ".json"), "w") as f:
                json.dump({u: [sorted(s), b] for u, (s, b) in REC.items()}, f)


def merge(tests_json, d):
    ids = {t["number"]: t["id"] for t in json.load(open(tests_json))["tests"]}
    k, b = {}, set()
    for fn in sorted(os.listdir(d)):
        for u, (keys, bulk) in json.load(open(os.path.join(d, fn))).items():
            if u == "<collect>":  # a lookup while importing a test module: every test
                nums = set(ids)
            elif u[0] == "T":
                nums = {n for n, i in ids.items() if i == u[1:] or u[1:].startswith(i + "[")}
            else:
                nums = {n for n, i in ids.items() if i.startswith(u[1:] + "::")}
            if bulk:
                b |= nums
            for x in keys:
                k.setdefault(x, set()).update(nums)
    json.dump({"k": {x: sorted(v) for x, v in sorted(k.items())}, "b": sorted(b)}, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    merge(*sys.argv[1:3])
