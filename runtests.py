"""Runs the given tests on the patched tree with unittest, as the scoring run would, and prints the ones that fail.
usage: runtests.py PATCH ID...   last stdout line: json list of failing ids, or "fallback" when a failure (a broken
setUpClass, a module that will not import) cannot be pinned to one of the given ids"""
import json, os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patchutil as pu  # noqa: E402

pu.overlay(sys.argv[1], os.environ.get("SQLGLOT_SRC", "/opt/sqlglot"))
ids = sys.argv[2:]
name = {f"{i.split('::')[0][:-3].replace('/', '.')}.{'.'.join(i.split('::')[1:])}": i for i in ids}
suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(n) for n in name)
out, sys.stdout = sys.stdout, open(os.devnull, "w")  # tests that print must not reach the selector
res = unittest.TextTestRunner(stream=open(os.devnull, "w")).run(suite)
sys.stdout = out
bad = set()
for t, _ in res.failures + res.errors + [(t, None) for t in res.unexpectedSuccesses]:
    t = getattr(t, "test_case", t)  # a failing subTest fails its test
    tid = t.id() if isinstance(t, unittest.TestCase) else ""
    if tid not in name:
        print("fallback")
        sys.exit()
    bad.add(name[tid])
print(json.dumps(sorted(bad)))
