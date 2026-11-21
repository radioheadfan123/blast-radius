"""Blast Radius selector: pick the tests a sqlglot patch can break, from maps built on the pinned commit."""
import ast
import json
import os
import re
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


def default_reads(tree, rng, ln, o, w, c0, c1):
    """Line spans in the top-level def whose one-line literal default holds the edit (and stays a literal)
    that read that parameter; the whole body if it calls locals()/vars(). None if no such default.
    Defaults are built once at import, but only a statement reading the parameter can see the value."""
    if o is None or w is None or not (o.isascii() and w.isascii()):
        return None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = n.args
            pos = a.posonlyargs + a.args
            pairs = list(zip(pos[len(pos) - len(a.defaults):], a.defaults)) + list(zip(a.kwonlyargs, a.kw_defaults))
            for arg, d in pairs:
                if d and d.lineno == d.end_lineno == ln and d.col_offset <= c0 and c1 <= d.end_col_offset:
                    try:
                        ast.literal_eval(o[d.col_offset:d.end_col_offset])
                        ast.literal_eval(w[d.col_offset:d.end_col_offset + len(w) - len(o)])
                    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                        return None
                    names = [x for s in n.body for x in ast.walk(s) if isinstance(x, ast.Name)]
                    if any(x.id in ("locals", "vars") for x in names):
                        return [(n.body[0].lineno, n.end_lineno)]
                    return [rng.get(x.lineno, (x.lineno, x.end_lineno)) for x in names if x.id == arg.arg]
    return None


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


def class_setting(tree, ln):
    """Name X if line ln sits in a plain `X = ...` statement directly in a class body."""
    for c in ast.walk(tree):
        if isinstance(c, ast.ClassDef):
            for s in c.body:
                if s.lineno <= ln <= s.end_lineno:
                    tg = s.targets if isinstance(s, ast.Assign) else [s.target] if isinstance(s, ast.AnnAssign) else []
                    if len(tg) == 1 and isinstance(tg[0], ast.Name):
                        return tg[0].id
    return None


_src, _rng, _enums = {}, {}, {}


def sources():
    if not _src:
        for p in SRC.glob("sqlglot/**/*.py"):
            _src[str(p.relative_to(SRC))] = p.read_text()
    return _src


def enums():
    """sqlglot enum class name -> 'pure' (identity equality) or 'mixin' (str/int values can equal a member)."""
    if not _enums:
        bases, other = {}, set()
        for text in sources().values():
            for name, b in re.findall(r"^\s*class (\w+)(?:\(([^)]*)\))?:", text, re.M):
                if name in bases:
                    other.add(name)  # name defined twice: ambiguous, never trust it
                bases[name] = [x.strip().split(".")[-1] for x in b.split(",") if x.strip()]
        kind = {"Enum": "pure", "IntEnum": "mixin", "StrEnum": "mixin", "IntFlag": "mixin"}
        grew = True
        while grew:
            grew = False
            for n, bs in bases.items():
                ks = [kind[x] for x in bs if x in kind]
                if n not in kind and ks:
                    kind[n] = "mixin" if "mixin" in ks or {"str", "int", "float", "bytes"} & set(bs) else "pure"
                    grew = True
        _enums.update({n: k for n, k in kind.items() if n in bases and n not in other})
    return _enums


class Norm(ast.NodeTransformer):
    """`x is None` and `x is E.MEMBER` mean `==` in Python (no __eq__ in sqlglot is truthy against None, and
    enum members are singletons); rewrite them so an is/== swap compares equal. Counts identity tests
    against str/int enums, where `==` -> `is` does change the answer for a plain value."""

    def __init__(self):
        self.mixin_is = 0

    def kind(self, n):
        if isinstance(n, ast.Constant) and n.value is None:
            return "pure"
        if isinstance(n, ast.Attribute) and n.attr.isupper():
            v = n.value
            return enums().get(v.id if isinstance(v, ast.Name) else v.attr if isinstance(v, ast.Attribute) else "")
        return None

    def visit_Compare(self, node):
        self.generic_visit(node)
        for i, op in enumerate(node.ops):
            k = self.kind(node.left if i == 0 else node.comparators[i - 1]) or self.kind(node.comparators[i])
            if k and isinstance(op, (ast.Is, ast.IsNot)):
                self.mixin_is += k == "mixin"
                node.ops[i] = ast.Eq() if isinstance(op, ast.Is) else ast.NotEq()
        return node


def same_meaning(old, new):
    """True if the two sources differ only by is/== swaps that Python evaluates the same way."""
    a, b = Norm(), Norm()
    ta, tb = a.visit(ast.parse(old)), b.visit(ast.parse(new))
    return ast.dump(ta) == ast.dump(tb) and b.mixin_is <= a.mixin_is


def readers(name, m, attr_ok=False):
    """Tests that ran a line reading `name` at call time; None if anything reads it at import time.
    attr_ok: import-time `.name` reads are known to hit other classes' copies, only bare-word reads count."""
    sources()
    word = re.compile(r"\b%s\b" % re.escape(name))
    bare = re.compile(r"(?<!\.)\b%s\b" % re.escape(name))
    define = re.compile(r"^\s*%s\s*(:[^=]*)?=[^=]" % re.escape(name))
    out = set()
    for rel, text in _src.items():
        if not word.search(text):
            continue
        if rel not in _rng:
            _rng[rel] = stmt_range(ast.parse(text))
        imp, fm = set(m["imp"].get(rel, ())), m["files"].get(rel, {})
        for i, line in enumerate(text.splitlines(), 1):
            if not word.search(line) or define.match(line) or line.lstrip().startswith("#"):
                continue
            lo, hi = _rng[rel].get(i, (i, i))
            if any(l in imp for l in range(lo, hi + 1)) and (not attr_ok or bare.search(line)):
                return None
            out |= tests_in(fm, m["sets"], lo, hi)
    return out


_impread = []


def read_at_import(key, name):
    """True if import or test collection read attribute `name` through class `key` (module.Qualname)."""
    if not _impread:
        p = HERE / "map" / "impread.json"
        _impread.append(set(json.loads(p.read_text())) if p.is_file() else None)
    s = _impread[0]
    return s is None or key + "." + name in s or "*." + name in s


def tests_in(fm, sets, lo, hi):
    out = set()
    for ln in range(lo, hi + 1):
        if str(ln) in fm:
            out.update(sets[fm[str(ln)]])
    return out


def undoc(tree):
    """Blank every bare string statement in place: docstrings, and attribute notes that Python just drops."""
    for s in ast.walk(tree):
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str):
            s.value.value = ""
    return tree


def same_code(path, hunks):
    """(old_tree, verdict): 'same' if the patch leaves the AST unchanged, 'broken' if it stops parsing."""
    old = (SRC / path).read_text()
    tree = ast.parse(old)
    new = "\n".join(pu.apply(old.splitlines(), hunks)) + "\n"
    try:
        nt = ast.parse(new)
    except SyntaxError:
        return tree, "broken"
    head = lambda s: s.splitlines()[:2]  # a coding cookie changes how the file decodes
    if [l for l in head(new) if "coding" in l] == [l for l in head(old) if "coding" in l]:
        if ast.dump(tree) == ast.dump(nt):
            return tree, "same"
        # docstrings only: no listed test reads them (tests/test_docs.py uses load_tests, which pytest skips)
        if path.startswith("sqlglot/") and ast.dump(undoc(tree)) == ast.dump(undoc(nt)):
            return tree, "same"
        swap = any(t != " " and re.search(r"\bis\b", l) for _, body in hunks for t, l in body)
        if swap and path.startswith("sqlglot/") and same_meaning(old, new):
            return tree, "same-meaning"
    return tree, "changed"


def select(path, hunks, m, every):
    sets = m["sets"]
    if path.endswith(".py") and (SRC / path).is_file():
        tree, verdict = same_code(path, hunks)
        if verdict == "same":
            return set(), "same-ast"
        if verdict == "same-meaning":
            return set(), "same-meaning"
        if verdict == "broken":
            return None, "broken"
    if path.startswith("sqlglot/") and path.endswith(".py") and (SRC / path).is_file():
        fm = m["files"].get(path, {})
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
                dr = default_reads(tree, rng, ln, o, w, c0, c1)
                imp = set(m["imp"].get(path, ()))
                if dr is not None and not any(set(range(lo, hi + 1)) & imp for lo, hi in dr):
                    # default argument, read only at call time: tests that ran a statement reading it
                    for lo, hi in dr:
                        out |= tests_in(fm, sets, lo, hi)
                    why.append("dflt")
                    continue
                ca = class_at(tree, ln) if path.startswith("sqlglot/expressions/") else None
                key = ca and path[:-3].replace("/", ".") + "." + ca[0]
                if key and key in m["cls"]:
                    # class-level edit of an expression class: tests that built it (or a subclass), or ran its code
                    out |= set(sets[m["cls"][key]]) | tests_in(fm, sets, ca[1], ca[2])
                    why.append("cls")
                    continue
                got = set(sets[m["mod"][path]]) if path in m["mod"] else set()
                name = class_setting(tree, ln) if o is not None and w is not None else None
                rd = readers(name, m) if name and got else None
                if rd is None and name and got:
                    cq = class_at(tree, ln)
                    mod = path[:-3].replace("/", ".").removesuffix(".__init__")
                    # TRANSFORMS feeds generator._DISPATCH_CACHE, built once per class per process: coverage misses later readers
                    if cq and name != "TRANSFORMS" and not read_at_import(mod + "." + cq[0], name):
                        rd = readers(name, m, attr_ok=True)
                if rd is not None:
                    # class setting read only at call time: tests that read it while using this module's dialect
                    out |= rd & got
                    why.append("attr")
                    continue
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
