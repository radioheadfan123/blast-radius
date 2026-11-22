"""Build map/tmap.json: per (Generator class L, expression key), the tests during which a generator whose class has L in
its MRO looked that key up (dispatch, json_path_part, preprocess's getattr(key_sql) / TRANSFORMS.get); "L|*" adds every
test that built or used such a generator; "mro" lists every sqlglot Generator subclass's sqlglot MRO. Used by the new-def
rule (a method a patch adds to a generator class). Older notes below describe the first, TRANSFORMS-owner form:
per (Generator class that owns a TRANSFORMS dict, expression class), the tests during which a
generator looked that class up through it. A TRANSFORMS entry is read at call time only by key: the dispatch dict
(generator.py `self._dispatch.get(expression.__class__)`, a copy of TRANSFORMS plus foo_sql methods, built once per
class), json_path_part (generator.py, `self.TRANSFORMS.get`), preprocess's _to_sql (transforms.py, `self.TRANSFORMS.get`)
and postgres cast_sql (`exp.IntDiv in self.TRANSFORMS`). Each of those lines records (type(self), key); a lookup
credits every class in type(self)'s MRO that owns a TRANSFORMS, since an entry there reaches type(self) by inheritance or
by a {**Parent.TRANSFORMS} copy (copies made at import show up in the snapshot under their own class too).
Runs the whole suite in one pytest process with pools inline, on a patched copy.
tmap.json = {"sets": [[test numbers]], "p": {"module.Qualname.TRANSFORMS|repr(('cls', module, Qualname))": setid or -1}}
(-1: a lookup outside a test: import, collection, between tests)
usage: tmap.py SQLGLOT_DIR WORK_DIR TESTS_JSON OUT_JSON   (venv python)"""
import json, os, shutil, subprocess, sys

src, work, tests_json, out = sys.argv[1:5]
shutil.rmtree(work, ignore_errors=True)
shutil.copytree(src, work, ignore=shutil.ignore_patterns(".git", "__pycache__"))
EDITS = {  # file: [(old, new)], each old must occur exactly once
    "sqlglot/generator.py": [
        ("        handler = self._dispatch.get(expression.__class__)\n",
         "        __tm_rec__(self, expression.__class__)\n        handler = self._dispatch.get(expression.__class__)\n"),
        ("            transform = self.TRANSFORMS.get(expression.__class__)\n",
         "            __tm_rec__(self, expression.__class__)\n            transform = self.TRANSFORMS.get(expression.__class__)\n"),
        ("        cls = type(self)\n        dispatch = _DISPATCH_CACHE.get(cls)\n",
         "        __tm_rec__(self, None)\n        cls = type(self)\n        dispatch = _DISPATCH_CACHE.get(cls)\n")],
    "sqlglot/transforms.py": [
        ("        _sql_handler: SqlHandler | None = getattr(self, expression.key + \"_sql\", None)\n",
         "        __tm_rec__(self, type(expression))\n        _sql_handler: SqlHandler | None = getattr(self, expression.key + \"_sql\", None)\n")],
    "sqlglot/generators/postgres.py": [
        ("            and exp.IntDiv in self.TRANSFORMS\n",
         "            and __tm_rec__(self, exp.IntDiv) is None\n            and exp.IntDiv in self.TRANSFORMS\n")],
}
for rel, subs in EDITS.items():
    p = os.path.join(work, rel)
    text = open(p).read()
    for old, new in subs:
        assert text.count(old) == 1, (rel, old)
        text = text.replace(old, new)
    open(p, "w").write(text)
# every other call-time TRANSFORMS read would be missed: there must be none
r = subprocess.run(["grep", "-rn", "TRANSFORMS", "sqlglot", "--include=*.py"], cwd=src, capture_output=True, text=True)
known = ("self._dispatch.get", "dict(cls.TRANSFORMS)", "self.TRANSFORMS.get", "exp.IntDiv in self.TRANSFORMS", "TRANSFORMS.items()",
         "gen_cls.TRANSFORMS.pop", "TRANSFORMS = {", "TRANSFORMS: ", "**", "AFTER_HAVING_MODIFIER_TRANSFORMS", "JSON_PATH_PART_TRANSFORMS",
         "`Generator.TRANSFORMS`")
odd = [l for l in r.stdout.splitlines() if not any(k in l for k in known)]
print("unrecognized TRANSFORMS lines:", odd)
open(os.path.join(work, "tmplugin.py"), "w").write(r'''
import builtins, collections, concurrent.futures as cf, importlib, json, pickle, pkgutil

cur, seen = [None], collections.defaultdict(set)


def _rec(g, k):
    seen[(type(g), k)].add(cur[0])


builtins.__tm_rec__ = _rec


class _Inline(cf.Executor):  # pool work runs here, credited to the calling test
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        if initializer:
            initializer(*initargs)

    def submit(self, fn, *a, **k):
        f = cf.Future()
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
        if not m.name.endswith("__main__"):
            try:
                importlib.import_module(m.name)
            except Exception as e:  # noqa
                print("import fail", m.name, e)


def pytest_runtest_setup(item):
    cur[0] = "S" + item.nodeid


def pytest_runtest_call(item):
    cur[0] = item.nodeid


def pytest_runtest_teardown(item):
    cur[0] = item.nodeid


def pytest_runtest_logfinish(nodeid, location):
    cur[0] = None


def pytest_sessionfinish(session):
    from sqlglot.generator import Generator
    lab = lambda b: f"{b.__module__}.{b.__qualname__}"
    pairs = collections.defaultdict(set)
    for (g, k), who in list(seen.items()):
        for b in g.__mro__:
            if b.__module__.startswith("sqlglot") and issubclass(b, Generator):
                pairs[lab(b) + "|*"] |= who
                if k is not None:
                    pairs[f"{lab(b)}|{k.key}"] |= who
    mro, todo = {}, [Generator]
    while todo:
        c = todo.pop()
        if c.__module__.startswith("sqlglot") and lab(c) not in mro:
            mro[lab(c)] = [lab(b) for b in c.__mro__ if b.__module__.startswith("sqlglot")]
        todo += c.__subclasses__()
    json.dump({"p": {p: sorted(x or "" for x in w) if None not in w else None for p, w in pairs.items()}, "mro": mro},
              open("tmap.raw.json", "w"))
''')
r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "tmplugin", "-p", "no:cacheprovider", "tests"],
                   cwd=work, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": work, "PYTHONHASHSEED": "0"})
print(r.stdout[-1500:], r.stderr[-1500:])

tests = json.load(open(tests_json))["tests"]
num = {t["id"]: t["number"] for t in tests}
byfile = {}
for t in tests:
    byfile.setdefault(t["file"], set()).add(t["number"])
rawall = json.load(open(os.path.join(work, "tmap.raw.json")))
raw = rawall["p"]
sets, sid, res = [], {}, {}
for p, who in raw.items():
    nums = set()
    for w in who or [None]:
        if w is None:
            nums = None  # an import, collection or between-tests lookup: never trust this pair
        elif w.startswith("S"):  # setup phase: setUpClass/setUpModule state reaches the whole file
            nums |= byfile.get(w[1:].split("::")[0], set())
        elif w in num:
            nums.add(num[w])
        else:
            nums = None  # a test the list does not know (parametrized id drift)
        if nums is None:
            break
    if nums is None:
        res[p] = -1
        continue
    key = tuple(sorted(nums))
    if key not in sid:
        sid[key] = len(sets)
        sets.append(list(key))
    res[p] = sid[key]
json.dump({"sets": sets, "p": res, "mro": rawall["mro"]}, open(out, "w"))
print(len(res), "pairs,", sum(v == -1 for v in res.values()), "untrusted,", len(sets), "sets")
print("TMDONE")
