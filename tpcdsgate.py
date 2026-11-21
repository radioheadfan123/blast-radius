"""Digest of what test_execute_tpcds can see, minus the full-data run. Every TPC-DS query is parsed and pickled (as the
test sends it to a worker); the executed ones go through sqlglot.executor.execute as the worker calls it, on the first
ROWS rows of each table, recording the optimized AST, every logical plan, every line of Python the executor compiles,
the rows returned and the duckdb transpile (the expected side). Also the sqlglot values the executor reads per row.
With a patch, its files are applied into /tmp and imported in place of the pinned ones.
With a patch, stops at the first query whose running digest differs from map/tpcds.steps (written by the pristine run).
usage: tpcdsgate.py [PATCH]   prints the md5 digest, or diff"""
import ast, csv, gzip, hashlib, itertools, os, pickle, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patchutil as pu  # noqa: E402

SRC = os.environ.get("SQLGLOT_SRC", "/opt/sqlglot")
ROWS = int(os.environ.get("GATE_ROWS", 200))  # rows read per table: enough for scans and filters to return rows
pu.overlay(sys.argv[1] if len(sys.argv) > 1 else None, SRC)
import unittest  # noqa: E402,F401  sqlglot validates node args only under tests (expressions/core.py UNITTEST)

import sqlglot.executor as ex  # noqa: E402
from sqlglot import exp, find_tables, parse_one, planner, transpile  # noqa: E402
from sqlglot.executor import python as py  # noqa: E402
from sqlglot.executor.table import Table  # noqa: E402
from sqlglot.generator import Generator  # noqa: E402
from sqlglot.helper import is_int, seq_get  # noqa: E402
from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA, load_sql_fixture_pairs  # noqa: E402

out, code = [], []  # compile order follows set order, so code is sorted per query
_opt, _plan, _compile = ex.optimize, planner.Plan, compile


def optimize(*a, **k):
    e = _opt(*a, **k)
    out.append(repr(e))
    return e


def steps(s, seen):
    if s not in seen:
        seen.add(s)
        for d in s.dependencies:
            steps(d, seen)
    return seen


def plan(e):  # every attribute of every step, for the query plan and each subquery plan the executor builds
    p = _plan(e)
    out.extend(sorted(re.sub(r" \(\d+\)", "", f"{type(s).__name__} {sorted((k, repr(v)) for k, v in vars(s).items() if k not in ('dependencies', 'dependents'))}")
                      for s in steps(p.root, set())))  # step ids and set order vary
    return p


ex.optimize, ex.Plan, planner.Plan = optimize, plan, plan
py.compile = lambda src, *a, **k: (code.append(src), _compile(src, *a, **k))[1]

tables = {}
for name, cols in TPCDS_SCHEMA.items():  # loaded as the test's setUpClass does, first ROWS rows only
    with gzip.open(f"{FIXTURES_DIR}/optimizer/tpc-ds/{name}.csv.gz", "rt", newline="") as f:
        reader = csv.reader(f, delimiter="|")
        next(reader)
        rows, ctypes = [], []
        for row in itertools.islice(reader, ROWS):
            if not ctypes:
                for v in row:
                    try:
                        ctypes.append(type(ast.literal_eval(v)))
                    except (ValueError, SyntaxError):
                        ctypes.append(str)
            rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
    tables[name] = Table(columns=cols, rows=rows)

h = hashlib.md5(repr([sorted(map(str, exp.DataType.TEXT_TYPES)), sorted(map(str, exp.DataType.NUMERIC_TYPES)),
                      sorted(Generator.TIME_PART_SINGULARS.items()), [(d.name, str(d.value)) for d in exp.DType],
                      [is_int(x) for x in ("1", "-2", "a", "1.5", "")], seq_get([1], 0), seq_get([1], 3)]).encode())
STEPS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map", "tpcds.steps")  # pristine running digest per query
want = open(STEPS).read().split() if len(sys.argv) > 1 and os.path.exists(STEPS) else None
prefix = []
for i, (meta, sql, _) in enumerate(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")):
    out.clear(), code.clear()
    try:
        e = pickle.loads(pickle.dumps(parse_one(sql)))  # the test parses every query and pickles it to a worker
        out.append("parsed")
        if meta.get("execute"):  # what mp_execute does
            res = ex.execute(e, schema=TPCDS_SCHEMA, tables={t.name: tables[t.name] for t in find_tables(e)})
            out += [repr(res.columns), repr(res.rows), transpile(sql, write="duckdb")[0]]
    except Exception as x:  # noqa
        out += ["ERR", type(x).__name__]
    h.update("\n".join(out + sorted(code)).encode() + b"\0")
    prefix.append(h.hexdigest()[:8])
    if want and prefix[-1] != (want[i] if i < len(want) else None):  # a running digest that differs stays different
        print("diff")
        sys.exit()
if not want:
    try:
        open(STEPS, "w").write(" ".join(prefix))
    except OSError:
        pass
print(h.hexdigest())
