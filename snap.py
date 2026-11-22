"""Fingerprint sqlglot's post-import state: every module global and class attribute, dicts split per key.
usage: PYTHONHASHSEED=0 snap.py SRC [PATCH [BASE]] -> pickle of {(label, key fingerprint or None): fingerprint} on stdout;
with BASE (an earlier snapshot), only the entries that differ: {(label, key): (BASE fingerprint, this one)}, None if absent"""
import importlib, os, pickle, pkgutil, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fpu
import patchutil as pu

src, patch, base = (sys.argv[1:] + [None, None])[:3]
pu.overlay(patch, src)
import sqlglot

for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if not m.name.endswith("__main__"):
        try:
            importlib.import_module(m.name)
        except Exception:  # noqa: the same module fails the same way on both sides
            pass
out = {}
SETLIKE = {"sqlglot.optimizer.canonicalize._CANONICALIZE_TYPES"}  # tuple(set of classes): order is address noise


def put(label, v):
    if label in SETLIKE and isinstance(v, tuple):
        v = frozenset(v)
    if isinstance(v, dict):
        for k, x in dict.items(v):
            out[(label, fpu.kfp(k))] = fpu.fp(x)
        out[(label, None)] = ("dict", len(v))
    else:
        out[(label, None)] = fpu.fp(v)


fpu.walk(put)
if base:
    with open(base, "rb") as f:
        a = pickle.load(f)
    out = {k: (a.get(k), out.get(k)) for k in a.keys() | out.keys() if a.get(k) != out.get(k)}
sys.stdout.buffer.write(pickle.dumps(out))
