"""test_generator_memory at 1/10 the loop: every dialect builds 100 generators to warm up, then 100 under tracemalloc
(the test: 1000, under 1024 bytes kept). The pinned tree keeps 0 bytes at 100 (32 at 1000), and a leak the test catches
keeps about 1 byte per generator, so more than 63 bytes here means keep the test. Imports the test module first, so an
import it would fail on fails here too. Also prints the CPU seconds of the loop (a much slower generator could time out).
With a patch, its files are applied into /tmp and imported in place of the pinned ones.
usage: gengate.py [PATCH]   prints ok and the CPU seconds, or the first failure"""
import gc, os, sys, time, tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patchutil as pu  # noqa: E402

pu.overlay(sys.argv[1] if len(sys.argv) > 1 else None, os.environ.get("SQLGLOT_SRC", "/opt/sqlglot"))
import tests.test_generator  # noqa: E402,F401  also imports unittest, as the test run does

from sqlglot.dialects import Dialect, Dialects  # noqa: E402

t = time.process_time()
for name in Dialects:
    dialect = Dialect.get_or_raise(name)
    for _ in range(100):
        dialect.generator()
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        for _ in range(100):
            dialect.generator()
        gc.collect()
        after = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    if after - before > 63:
        print("leak", name, after - before)
        sys.exit()
print("ok", f"{time.process_time() - t:.3f}")
