"""test_max_nodes at 1/100 the width: the same parses, with each node limit scaled the same way (the width-10000 SQL
holds about 100 counted nodes per column, so 1_000_000 / 10000 = 10_000 / 100), digesting every result or error
message (which carries the position the limit was hit at). The test itself spends 28.8 s tokenizing and parsing the
full width. Also prints the CPU seconds of the wide parses, so a slower parser (the test could time out) is caught.
With a patch, its files are applied into /tmp and imported in place of the pinned ones.
usage: nodesgate.py [PATCH]   prints the md5 digest and the CPU seconds"""
import hashlib, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patchutil as pu  # noqa: E402

pu.overlay(sys.argv[1] if len(sys.argv) > 1 else None, os.environ.get("SQLGLOT_SRC", "/opt/sqlglot"))
import unittest  # noqa: E402,F401  sqlglot validates node args only under tests (expressions/core.py UNITTEST)

from sqlglot import parse_one  # noqa: E402

out = []


def run(sql, **kw):
    try:
        out.append(repr(parse_one(sql, **kw)))
    except Exception as x:  # noqa
        out.append(f"{type(x).__name__}: {x}")


run("SELECT 1", max_nodes=1_000_000)
run(f"SELECT {','.join(f'col{i}' for i in range(500))} FROM t", max_nodes=1_000_000)
sql = "SELECT " + ",".join(["x" + "=x" * 100] * 100)
t = time.process_time()
run(sql, max_nodes=10_000)
run(sql, max_nodes=500)
secs = time.process_time() - t
run("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
run("SELECT 1, 2, 3")
print(hashlib.md5("\n".join(out).encode()).hexdigest(), f"{secs:.3f}")
