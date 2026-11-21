"""Blast Radius selector: pick the tests a sqlglot patch can break, from maps built on the pinned commit."""
import ast
import json
import os
import sys
from pathlib import Path

import patchutil as pu

DATA = Path(os.environ.get("DATA_DIR", "/data"))
HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SQLGLOT_SRC", "/opt/sqlglot"))  # pristine checkout at the pinned commit
WORKER_MS = 2000


def log(*a):
    print(*a, file=sys.stderr)


def load_maps():
    return json.loads((HERE / "map" / "lines.json").read_text())


def regions(tree):
    """Function bodies and lambda bodies as (start_line, start_col, end_line, end_col): code here runs at call time."""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            b0, b1 = n.body[0], n.body[-1]
            out.append((b0.lineno, b0.col_offset, b1.end_lineno, b1.end_col_offset, n))
        elif isinstance(n, ast.Lambda):
            b = n.body
            out.append((b.lineno, b.col_offset, b.end_lineno, b.end_col_offset, n))
    return out


def stmt_range(tree):
    """line -> (first, last) line of the innermost statement holding it (header only for compound stmts)."""
    rng = {}
    for n in ast.walk(tree):
        if not isinstance(n, ast.stmt):
            continue
        body = getattr(n, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
            lo = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
            hi = body[0].lineno - 1
        else:
            lo, hi = n.lineno, n.end_lineno
        for ln in range(lo, hi + 1):
            cur = rng.get(ln)
            if cur is None or hi - lo <= cur[1] - cur[0]:
                rng[ln] = (lo, hi)
    return rng


def edits(hunks):
    """[(old_line, old_text or None, new_text or None)]: replaced lines paired, others single-sided."""
    out = []
    for start, body in hunks:
        n, i = start, 0
        while i < len(body):
            if body[i][0] == " ":
                n += 1; i += 1; continue
            minus, plus = [], []
            while i < len(body) and body[i][0] == "-":
                minus.append(body[i][1]); i += 1
            while i < len(body) and body[i][0] == "+":
                plus.append(body[i][1]); i += 1
            for j in range(max(len(minus), len(plus))):
                o = minus[j] if j < len(minus) else None
                w = plus[j] if j < len(plus) else None
                out.append((n + min(j, max(len(minus) - 1, 0)) if minus else max(n - 1, 1), o, w))
            n += len(minus)
    return out


def span(o, w):
    """Column range in the old line that differs from the new one."""
    if o is None or w is None:
        return 0, 10 ** 6
    a = 0
    while a < min(len(o), len(w)) and o[a] == w[a]:
        a += 1
    b = 0
    while b < min(len(o), len(w)) - a and o[-1 - b] == w[-1 - b]:
        b += 1
    return a, max(len(o) - b, a + 1)


def runtime(regs, ln, c0, c1):
    """Innermost call-time region fully holding the edit, or None if the edit touches import-time code."""
    best = None
    for l0, k0, l1, k1, node in regs:
        if (l0, k0) <= (ln, c0) and (ln, c1) <= (l1, k1):
            if best is None or (l0, k0) >= (best[0], best[1]):
                best = (l0, k0, l1, k1, node)
    return best


def is_comment_only(changes):
    return all(t.strip() == "" or t.strip().startswith("#") for _, t in changes)


def class_at(tree, ln):
    """Qualified name and line span of the innermost class whose body holds line ln."""
    best, stack = None, [(tree, "")]
    while stack:
        node, q = stack.pop()
        for c in ast.iter_child_nodes(node):
            if isinstance(c, ast.ClassDef):
                name = q + c.name
                if c.lineno <= ln <= c.end_lineno:
                    best = (name, c.lineno, c.end_lineno)
                    stack.append((c, name + "."))
    return best


def tests_in(fm, sets, lo, hi):
    out = set()
    for ln in range(lo, hi + 1):
        if str(ln) in fm:
            out.update(sets[fm[str(ln)]])
    return out


def select(path, hunks, m, every):
    sets = m["sets"]
    changes = pu.changed_lines(hunks)
    if path.endswith(".py") and is_comment_only(changes):
        return set(), "comment"
    if path.startswith("sqlglot/") and path.endswith(".py"):
        fm = m["files"].get(path, {})
        tree = ast.parse((SRC / path).read_text())
        regs, rng = regions(tree), stmt_range(tree)
        out, why = set(), []
        for ln, o, w in edits(hunks):
            c0, c1 = span(o, w)
            reg = runtime(regs, ln, c0, c1) if o is not None and w is not None else None
            if reg:
                lo, hi = rng.get(ln, (ln, ln))
                got = tests_in(fm, sets, lo, hi)
                if not got:  # ponytail: edited line never ran; whole enclosing function as a cheap safety net
                    got = tests_in(fm, sets, reg[0], reg[2])
                out |= got
                why.append("rt")
            else:
                ca = class_at(tree, ln) if path.startswith("sqlglot/expressions/") else None
                key = ca and path[:-3].replace("/", ".") + "." + ca[0]
                if key and key in m["cls"]:
                    # class-level edit of an expression class: tests that built it (or a subclass), or ran its code
                    out |= set(sets[m["cls"][key]]) | tests_in(fm, sets, ca[1], ca[2])
                    why.append("cls")
                    continue
                got = set(sets[m["mod"][path]]) if path in m["mod"] else set()
                got |= {t for sid in set(fm.values()) for t in sets[sid]}
                out |= got or every
                why.append("imp")
        return out, "+".join(sorted(set(why)))
    if path in m["fixtures"]:
        return set(sets[m["fixtures"][path]]), "fixture"
    return None, "unknown"


def split(nums, ms):
    """Best worker count by LPT; returns (cost_ms, workers)."""
    if not nums:
        return 0, []
    order = sorted(nums, key=lambda n: -ms[n])
    best = None
    for k in range(1, 9):
        loads, ws = [0] * k, [[] for _ in range(k)]
        for n in order:
            i = loads.index(min(loads))
            ws[i].append(n)
            loads[i] += ms[n]
        ws = [w for w in ws if w]
        cost = max(loads) + WORKER_MS * len(ws)
        if best is None or cost < best[0]:
            best = (cost, ws)
    return best


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    every = set(ms)
    maps = load_maps()
    for patch in sorted((DATA / "commits").glob("*.patch")):
        files = pu.parse(patch.read_text())
        chosen, whys = set(), []
        for path, hunks in files.items():
            got, why = select(path, hunks, maps, every)
            whys.append(why)
            chosen |= every if got is None else got
        cost, ws = split(chosen, ms)
        log(patch.stem, whys, len(chosen), cost)
        print(json.dumps({"commit": patch.stem, "workers": ws}))


if __name__ == "__main__":
    main()
