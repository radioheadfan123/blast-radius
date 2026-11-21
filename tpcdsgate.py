"""Digest of what test_execute_tpcds can see. Every TPC-DS query is parsed and pickled (as the test sends it to a
worker); the executed ones go through sqlglot.executor.execute as the worker calls it, on the first ROWS rows of each
table, recording the optimized AST, every logical plan, every line of Python the executor compiles, the rows returned
and the duckdb transpile (the expected side). Also the sqlglot values the executor reads per row.
With a patch, its files are applied into /tmp and imported in place of the pinned ones, and each query's piece is
compared with the pristine one (map/tpcds.steps, written by the build run). An executed query whose piece differs is
run again on the full tables, as the test does, and must return exactly the pristine columns and rows (map/tpcds.full)
with the same duckdb SQL, within BUDGET pristine CPU seconds of such re-runs per patch.
usage: tpcdsgate.py [PATCH]   prints the md5 digest (the pristine one when the patch passes), or diff"""
import ast, csv, gzip, hashlib, itertools, os, pickle, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patchutil as pu  # noqa: E402

SRC = os.environ.get("SQLGLOT_SRC", "/opt/sqlglot")
ROWS = int(os.environ.get("GATE_ROWS", 200))  # rows read per table: enough for scans and filters to return rows
BUDGET = float(os.environ.get("GATE_BUDGET", 6))  # full-data re-runs cost CPU; past this the test is just kept
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
record = py.compile = lambda src, *a, **k: (code.append(src), _compile(src, *a, **k))[1]


def load(limit):
    """Tables as the test's setUpClass loads them (first `limit` rows when given)."""
    tables = {}
    for name, cols in TPCDS_SCHEMA.items():
        with gzip.open(f"{FIXTURES_DIR}/optimizer/tpc-ds/{name}.csv.gz", "rt", newline="") as f:
            reader = csv.reader(f, delimiter="|")
            next(reader)
            rows, ctypes = [], []
            for row in itertools.islice(reader, limit):
                if not ctypes:
                    for v in row:
                        try:
                            ctypes.append(type(ast.literal_eval(v)))
                        except (ValueError, SyntaxError):
                            ctypes.append(str)
                rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
        tables[name] = Table(columns=cols, rows=rows)
    return tables


def full_run(e, sql):
    """md5 of what the test compares for one query on the full tables, and the CPU seconds it took."""
    t = time.process_time()
    ex.optimize, ex.Plan, planner.Plan, py.compile = _opt, _plan, _plan, _compile  # no recording here
    try:
        res = ex.execute(e, schema=TPCDS_SCHEMA, tables={x.name: big[x.name] for x in find_tables(e)})
    finally:
        ex.optimize, ex.Plan, planner.Plan, py.compile = optimize, plan, plan, record
    res = pickle.loads(pickle.dumps(res))  # the worker sends the table back
    md5 = hashlib.md5(repr([res.columns, res.rows, transpile(sql, write="duckdb")[0]]).encode()).hexdigest()
    return md5, time.process_time() - t


tables = load(ROWS)
big = None  # full tables, loaded (and pickled to the worker, as initargs are) only when needed
const = hashlib.md5(repr([sorted(map(str, exp.DataType.TEXT_TYPES)), sorted(map(str, exp.DataType.NUMERIC_TYPES)),
                          sorted(Generator.TIME_PART_SINGULARS.items()), [(d.name, str(d.value)) for d in exp.DType],
                          [is_int(x) for x in ("1", "-2", "a", "1.5", "")], seq_get([1], 0), seq_get([1], 3)]).encode()).hexdigest()[:8]
MAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map")
patched = len(sys.argv) > 1
want = open(os.path.join(MAP, "tpcds.steps")).read().split() if patched else None
pfull = dict(ln.split(None, 1) for ln in open(os.path.join(MAP, "tpcds.full")).read().splitlines()) if patched else {}
if patched and (const != want[0] or not pfull):
    print("diff")
    sys.exit()
pieces, fulls, spent, budget = [const], [], 0.0, 0.0
for i, (meta, sql, _) in enumerate(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")):
    out.clear(), code.clear()
    e = None
    try:
        e = pickle.loads(pickle.dumps(parse_one(sql)))  # the test parses every query and pickles it to a worker
        out.append("parsed")
        if meta.get("execute"):  # what mp_execute does
            res = ex.execute(e, schema=TPCDS_SCHEMA, tables={t.name: tables[t.name] for t in find_tables(e)})
            out += [repr(res.columns), repr(res.rows), transpile(sql, write="duckdb")[0]]
    except Exception as x:  # noqa
        out += ["ERR", type(x).__name__]
    piece = hashlib.md5("\n".join(out + sorted(code)).encode()).hexdigest()[:8]
    pieces.append(piece)
    if not meta.get("execute"):
        if patched and piece != (want[i + 1] if i + 1 < len(want) else None):
            print("diff")
            sys.exit()
        continue
    if not patched:
        big = big or pickle.loads(pickle.dumps(load(None)))
        fulls.append("%d %s %.3f" % (i, *full_run(e, sql)))
    elif piece != (want[i + 1] if i + 1 < len(want) else None):
        try:
            gold, gold_t = pfull[str(i)].split()
            budget += float(gold_t)
            if budget > BUDGET:
                raise TimeoutError
            if big is None:
                big = pickle.loads(pickle.dumps(load(None)))
            md5, took = full_run(e, sql)
            spent += took
        except Exception:  # noqa: over budget, or a query that fails or is missing from the pristine run
            md5, gold = None, ""
        if md5 != gold or spent > 2 * budget + 1:  # much slower can time the real test out
            print("diff")
            sys.exit()
if not patched:
    try:
        open(os.path.join(MAP, "tpcds.steps"), "w").write(" ".join(pieces))
        open(os.path.join(MAP, "tpcds.full"), "w").write("\n".join(fulls) + "\n")
    except OSError:
        pass
    print(hashlib.md5(" ".join(pieces).encode()).hexdigest())
else:
    print(open(os.path.join(MAP, "tpcds.md5")).read().split()[0])
