"""Build map/col.json: sqlglot lines that ran while pytest collected the tests, with every sqlglot module already
imported. The map's import set (lines.json "imp", coverage's "" context) mixes import and collection; a collection
line can feed test-module state (parametrize lists, module globals) that no sqlglot snapshot sees.
col.json = {"path": [lines]}; also prints per-file counts of imp lines that ran only at import.
usage: colmap.py SQLGLOT_DIR OUT_JSON [LINES_JSON]   (venv python, PYTHONDONTWRITEBYTECODE=1)"""
import importlib, json, os, pkgutil, sys

import coverage

src, out = os.path.abspath(sys.argv[1]), sys.argv[2]
sys.path.insert(0, src)
os.chdir(src)
cov = coverage.Coverage(data_file=None, include=[os.path.join(src, "sqlglot", "*")])
cov.start()
cov.switch_context("imp")
import sqlglot

for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if not m.name.endswith("__main__"):
        try:
            importlib.import_module(m.name)
        except Exception as e:  # noqa
            print("import fail", m.name, e)
cov.switch_context("col")
import pytest

rc = pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", "tests"])
cov.stop()
d = cov.get_data()
res, pure = {}, {}
for f in d.measured_files():
    rel = os.path.relpath(f, src)
    d.set_query_contexts(["col"])
    if ls := d.lines(f):
        res[rel] = sorted(ls)
    d.set_query_contexts(["imp"])
    pure[rel] = set(d.lines(f) or ())
json.dump(res, open(out, "w"))
print("collect rc", rc, "files", len(res), "col lines", sum(map(len, res.values())))
if len(sys.argv) > 3:
    imp = json.load(open(sys.argv[3]))["imp"]
    rows = []
    for p, ls in imp.items():
        s = set(ls)
        rows.append((len(s), len(s & set(res.get(p, ()))), len(s & pure.get(p, set())), p))
    for r in sorted(rows, reverse=True)[:15]:
        print("imp %5d col %5d pure %5d %s" % r)
