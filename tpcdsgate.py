"""Digest of everything test_execute_tpcds hands to code outside the executor: for each TPC-DS query it executes,
the optimized AST and logical plan (what the executor runs) and the query transpiled to duckdb (the expected rows).
With a patch, its files are applied into /tmp and imported in place of the pinned ones.
usage: tpcdsgate.py [PATCH]   prints the md5 digest"""
import hashlib, importlib.util, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patchutil as pu  # noqa: E402

SRC = os.environ.get("SQLGLOT_SRC", "/opt/sqlglot")
over = {}
if len(sys.argv) > 1:
    for rel, hunks in pu.parse(open(sys.argv[1]).read()).items():
        p = os.path.join("/tmp/gate", rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write("\n".join(pu.apply(open(os.path.join(SRC, rel)).read().splitlines(), hunks)) + "\n")
        over[rel[:-3].replace("/", ".")] = p


class Over:
    @staticmethod
    def find_spec(name, path=None, target=None):
        return importlib.util.spec_from_file_location(name, over[name]) if name in over else None


sys.meta_path.insert(0, Over)
sys.path.insert(0, SRC)
import unittest  # noqa: E402,F401  sqlglot validates node args only under tests (expressions/core.py UNITTEST)
from sqlglot import parse_one, transpile  # noqa: E402
from sqlglot.optimizer import optimize  # noqa: E402
from sqlglot.planner import Plan  # noqa: E402
from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs  # noqa: E402

h = hashlib.md5()
for meta, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
    try:
        e = parse_one(sql)  # the test parses every query, executed or not
        out = ["parsed"]
        if meta.get("execute"):
            e = optimize(e, TPCDS_SCHEMA, leave_tables_isolated=True)
            out = [repr(e), *sorted(re.sub(r" \(\d+\)", "", s) for s in str(Plan(e)).splitlines()),  # ids, set order vary
                   transpile(sql, write="duckdb")[0]]
    except Exception as x:  # noqa
        out = ["ERR", type(x).__name__]
    h.update("\n".join(out).encode() + b"\0")
print(h.hexdigest())
