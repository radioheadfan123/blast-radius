"""Runs tests on a patched tree in a child forked from this process, as runtests.py did in a fresh python.
The parent preloads the heavy third-party modules the tests import (numpy, pandas, duckdb) but never sqlglot, so each
child imports sqlglot and the tests from the patched tree exactly as a fresh process would, minus the start-up cost."""
import concurrent.futures as cf, json, os, pickle, signal, sys, unittest

import patchutil as pu

_primed = False


def prime():
    global _primed
    if not _primed:
        _primed = True
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")  # no BLAS threads in a process that forks
        for m in ("numpy", "pandas", "pandas.testing", "duckdb"):
            try:
                __import__(m)
            except BaseException:  # noqa: the child imports it itself, or the test fails there as it would anyway
                pass


class _Inline(cf.Executor):
    """Process pools run in the child: spawned workers would import the pinned tree, not the patched one."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        if initializer:
            initializer(*initargs)

    def submit(self, fn, *a, **k):
        f = cf.Future()
        try:  # real pools pickle both ways; sqlglot's serde runs there
            a, k = pickle.loads(pickle.dumps((a, k)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:  # noqa
            f.set_exception(e)
        return f


def _child(patch, src, ids):
    for m in [m for m in sys.modules if m.split(".")[0] in ("sqlglot", "tests", "fixture_helpers")]:
        del sys.modules[m]  # anything the parent loaded from the pinned tree
    pu.overlay(patch, src)
    cf.ProcessPoolExecutor = _Inline  # before the tests import it
    name = {f"{i.split('::')[0][:-3].replace('/', '.')}.{'.'.join(i.split('::')[1:])}": i for i in ids}
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(n) for n in name)
    res = unittest.TextTestRunner(stream=open(os.devnull, "w")).run(suite)
    bad = set()
    for t, _ in res.failures + res.errors + [(t, None) for t in res.unexpectedSuccesses]:
        t = getattr(t, "test_case", t)  # a failing subTest fails its test
        tid = t.id() if isinstance(t, unittest.TestCase) else ""
        if tid not in name:
            return None  # a failure not pinned to one test (setUpClass, an import): keep them all
        bad.add(name[tid])
    return sorted(bad)


def run(patch, src, ids, timeout):
    """(failing ids or None when that cannot be told here, child CPU seconds)."""
    prime()
    sys.stdout.flush(), sys.stderr.flush()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        out = None
        try:
            signal.alarm(max(1, int(timeout)))
            dn = os.open(os.devnull, os.O_WRONLY)
            os.dup2(dn, 1), os.dup2(dn, 2)
            out = _child(str(patch), str(src), ids)
        except BaseException:  # noqa
            out = None
        os.write(w, json.dumps(out).encode())
        os._exit(0)
    os.close(w)
    data = b""
    while chunk := os.read(r, 65536):
        data += chunk
    os.close(r)
    _, status, ru = os.wait4(pid, 0)
    cpu = ru.ru_utime + ru.ru_stime
    return (json.loads(data) if status == 0 and data else None), cpu
