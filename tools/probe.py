"""pytest plugin for map building.
- imports every sqlglot module at session start, so import-time code runs under the empty coverage context
- records per test: files under tests/ it opened, and dialect classes it instantiated
Writes $PROBE_OUT.<pid>.json"""
import builtins, io, json, os, pkgutil, importlib

_cur = [None]
_open_seen, _dia_seen, _expr_seen = {}, {}, {}
_orig_open = builtins.open
ROOT = os.path.abspath("tests") + os.sep


def _open(f, *a, **k):
    if _cur[0]:
        try:
            p = os.path.abspath(os.fspath(f))
            if p.startswith(ROOT) and not p.endswith(".py"):
                _open_seen.setdefault(_cur[0], set()).add(os.path.relpath(p))
        except TypeError:
            pass
    return _orig_open(f, *a, **k)


builtins.open = _open
io.open = _open

if os.environ.get("PROBE_INPROC"):
    # map building only: run pool work in this process so coverage credits it to the calling test
    import concurrent.futures as cf

    class _Inline(cf.Executor):
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
            if initializer:
                initializer(*initargs)

        def submit(self, fn, *a, **k):
            f = cf.Future()
            import pickle  # real pools pickle both ways; sqlglot's serde runs there
            try:
                a, k = pickle.loads(pickle.dumps((a, k)))
                f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
            except BaseException as e:  # noqa
                f.set_exception(e)
            return f

    cf.ProcessPoolExecutor = _Inline


def pytest_sessionstart(session):
    import sqlglot
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(m.name)
        except Exception as e:  # noqa
            print("probe import fail", m.name, e)
    from sqlglot.dialects.dialect import Dialect
    orig = Dialect.__init__

    def init(self, *a, **k):
        if _cur[0]:
            _dia_seen.setdefault(_cur[0], set()).add(type(self).__module__ + "." + type(self).__qualname__)
        return orig(self, *a, **k)

    Dialect.__init__ = init

    from sqlglot.expressions.core import Expr
    eorig = Expr.__init__

    def einit(self, *a, **k):
        if _cur[0]:
            _expr_seen.setdefault(_cur[0], set()).add(type(self))
        return eorig(self, *a, **k)

    Expr.__init__ = einit


def pytest_runtest_setup(item):
    _cur[0] = item.nodeid


def pytest_sessionfinish(session):
    out = os.environ["PROBE_OUT"] + f".{os.getpid()}.json"
    with _orig_open(out, "w") as fh:
        json.dump({"open": {k: sorted(v) for k, v in _open_seen.items()},
                   "dialect": {k: sorted(v) for k, v in _dia_seen.items()},
                   "expr": {k: sorted({c.__module__ + "." + c.__qualname__ for k2 in v for c in k2.__mro__})
                            for k, v in _expr_seen.items()}}, fh)
