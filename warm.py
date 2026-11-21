"""Runs tests on a patched tree in a child forked from this process, as runtests.py did in a fresh python.
The parent preloads the heavy third-party modules the tests import (numpy, pandas, duckdb) but never sqlglot, so each
child imports sqlglot and the tests from the patched tree exactly as a fresh process would, minus the start-up cost."""
import concurrent.futures as cf, contextlib, json, os, pickle, signal, sys, unittest

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


class _Lazy(cf.Future):
    """A done future whose call runs when its result is first read, so a test that stops at its first failing case
    never computes the rest (wait and as_completed only look at the state)."""

    def __init__(self, fn, a, k):
        super().__init__()
        self._state, self._call = cf._base.FINISHED, (fn, a, k)

    def _go(self):
        if self._call:
            fn, a, k = self._call
            self._call = None
            try:  # real pools pickle both ways; sqlglot's serde runs there
                self._result = pickle.loads(pickle.dumps(fn(*a, **k)))
            except KeyboardInterrupt:  # the run's timeout
                raise
            except BaseException as e:  # noqa
                self._exception = e

    def result(self, timeout=None):
        self._go()
        return super().result(timeout)

    def exception(self, timeout=None):
        self._go()
        return super().exception(timeout)


class _Inline(cf.Executor):
    """Process pools run in the child: spawned workers would import the pinned tree, not the patched one."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        if initializer:
            initializer(*initargs)

    def submit(self, fn, *a, **k):
        try:
            a, k = pickle.loads(pickle.dumps((a, k)))  # the arguments as they are now, as a real pool sends them
        except BaseException as e:  # noqa
            f = cf.Future()
            f.set_exception(e)
            return f
        return _Lazy(fn, a, k)


def _eq(self, first, second, msg=None):
    """assertEqual without building a diff: every type-specific assertEqual fails exactly when not first == second."""
    if not first == second:
        raise self.failureException("differs")


@contextlib.contextmanager
def _sub(self, msg=None, **params):
    """subTest where a failure ends its test at once (one failing subtest fails the test); a skip stays local."""
    try:
        yield
    except unittest.SkipTest:
        pass


class _Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.ok, self.cur = [], None

    def startTest(self, t):
        super().startTest(t)
        self.cur = t

    def addSuccess(self, t):
        super().addSuccess(t)
        self.ok.append(t)

    def addSkip(self, t, reason):
        super().addSkip(t, reason)
        self.ok.append(t)


def _stop(*_):
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(2)  # test code that swallows the interrupt is killed instead (no answer: keep everything)
    raise KeyboardInterrupt


def _child(patch, src, ids):
    """Ids of the tests that passed, or None when a failure is not pinned to one test (setUpClass, an import)."""
    for m in [m for m in sys.modules if m.split(".")[0] in ("sqlglot", "tests", "fixture_helpers")]:
        del sys.modules[m]  # anything the parent loaded from the pinned tree
    pu.overlay(patch, src)
    cf.ProcessPoolExecutor = _Inline  # before the tests import it
    unittest.TestCase._getAssertEqualityFunc = lambda self, a, b: _eq.__get__(self)
    unittest.TestCase.subTest = _sub
    # sorted keeps each class together: every switch of class reruns setUpClass (TestExecutor's costs 1.9 s)
    name = {f"{i.split('::')[0][:-3].replace('/', '.')}.{'.'.join(i.split('::')[1:])}": i for i in sorted(ids)}
    res, cut = _Result(), None
    try:
        unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(n) for n in name).run(res)
    except KeyboardInterrupt:  # out of time: keep what passed, except in the module still open (its teardown is due)
        signal.alarm(0)
        cut = res.cur.id().rsplit(".", 2)[0] + "." if isinstance(res.cur, unittest.TestCase) else ""
    for t, _ in res.failures + res.errors + [(t, None) for t in res.unexpectedSuccesses]:
        if not isinstance(t, unittest.TestCase) or t.id() not in name:
            return None
    bad = {t.id() for t, _ in res.failures + res.errors} | {t.id() for t in res.unexpectedSuccesses}
    return sorted(name[t.id()] for t in res.ok
                  if t.id() in name and t.id() not in bad and not (cut is not None and t.id().startswith(cut)))


def run(patch, src, ids, timeout):
    """(ids that passed, or None when that cannot be told here; child CPU seconds)."""
    prime()
    sys.stdout.flush(), sys.stderr.flush()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        out = None
        try:
            signal.signal(signal.SIGALRM, _stop)
            signal.alarm(max(1, int(timeout)))
            dn = os.open(os.devnull, os.O_WRONLY)
            os.dup2(dn, 1), os.dup2(dn, 2)
            out = _child(str(patch), str(src), ids)
        except BaseException:  # noqa
            out = None
        signal.alarm(0)
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
