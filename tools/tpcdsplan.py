"""Proxy for test_execute_tpcds: optimized SQL and logical plan of every TPC-DS query that test executes.
usage: tpcdsplan.py SQLGLOT_DIR   (prints one block per query; ERR <type> if a query raises)"""
import re, sys

sys.path.insert(0, sys.argv[1])
from sqlglot import parse_one  # noqa: E402
from sqlglot.optimizer import optimize  # noqa: E402
from sqlglot.planner import Plan  # noqa: E402
from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs  # noqa: E402

for meta, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
    if meta.get("execute"):
        try:
            e = optimize(parse_one(sql), TPCDS_SCHEMA, leave_tables_isolated=True)
            print(e.sql())
            print("\n".join(sorted(re.sub(r" \(\d+\)", "", s) for s in str(Plan(e)).splitlines())))  # ids and set order vary
        except Exception as x:  # noqa
            print("ERR", type(x).__name__)
