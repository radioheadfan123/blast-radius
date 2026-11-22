"""Blast Radius selector: pick the tests a sqlglot patch can break, from maps built on the pinned commit."""
import ast
import io
import tokenize
import importlib.util
import json
import os
import pickle
import re
import subprocess
import sys
import time
from pathlib import Path

import patchutil as pu
import warm

CLS_HDR = re.compile(r"^\s*(class\s|@)|_sql_names")

DATA = Path(os.environ.get("DATA_DIR", "/data"))
HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SQLGLOT_SRC", "/opt/sqlglot"))  # pristine checkout at the pinned commit
WORKER_MS = 2000


def log(*a):
    print(*a, file=sys.stderr)


def load_maps():
    m = json.loads((HERE / "map" / "lines.json").read_text())
    c = HERE / "map" / "col.json"  # sqlglot lines pytest collection ran, all of sqlglot already imported (colmap.py)
    m["col"] = json.loads(c.read_text()) if c.is_file() else None
    return m


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


def inserted(hunks):
    """New-file line numbers of the added lines edits() reports without an old line, in the same order."""
    out, d = [], 0
    for start, body in hunks:
        n, m, i = start, start + d, 0
        while i < len(body):
            if body[i][0] == " ":
                n += 1; m += 1; i += 1
                continue
            k = i
            while i < len(body) and body[i][0] == "-":
                i += 1
            nm, k = i - k, i
            while i < len(body) and body[i][0] == "+":
                i += 1
            out += [m + j for j in range(nm, i - k)]
            n, m = n + nm, m + i - k
        d = m - n
    return out


def new2old(hunks, m):
    """Old line number of new line m, None if m was added."""
    d = 0
    for start, body in hunks:
        if m < start + d:
            break
        n, k = start, start + d
        for t, _ in body:
            if t != "-" and k == m:
                return n if t == " " else None
            n, k = n + (t != "+"), k + (t != "-")
        d = k - n
    return m - d


def def_at(regs, ln, text):
    """Function bodies holding all of line ln, innermost first, as (region, def node)."""
    c0 = len(text) - len(text.lstrip())
    got = [r for r in regs if isinstance(r[4], (ast.FunctionDef, ast.AsyncFunctionDef))
           and (r[0], r[1]) <= (ln, c0) and (ln, len(text.rstrip())) <= (r[2], r[3])]
    return sorted(got, key=lambda r: (r[0], r[1]), reverse=True)


def one_sided(regs, nregs, hunks, ln, o, w, m):
    """Old function region a whole added or removed line sits in, or None. A removed line is placed in the old tree, an
    added one in the patched tree and mapped back by its def line (a def the patch adds is skipped for its parent).
    Lines that leave a function body (dedent to class or module level) find no def and stay import edits."""
    if w is None:
        got = def_at(regs, ln, o)
        return got[0] if got else None
    old = {(r[4].lineno, r[4].name): r for r in regs if isinstance(r[4], (ast.FunctionDef, ast.AsyncFunctionDef))}
    for r in def_at(nregs, m, w):
        if (lo := new2old(hunks, r[4].lineno)) is not None and (lo, r[4].name) in old:
            return old[(lo, r[4].name)]
    return None


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


_src, _rng, _enums, _pre = {}, {}, {}, {}


def pre():
    """map/pre.pkl, made at build by `selector.py --pre`: stmt_range of every pinned sqlglot file, every word in them,
    and the files a bare `import sqlglot` loads. Missing: everything is worked out here as before."""
    if not _pre:
        p = HERE / "map" / "pre.pkl"
        _pre.update(pickle.loads(p.read_bytes()) if p.is_file() else {"rng": {}})
        _rng.update(_pre["rng"])
    return _pre


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
    sources(), pre()
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


_tbf = {}
IMPORT = re.compile(r"^\s*(?:from (tests(?:\.\w+)*) import\s*\(?\s*(\w*)|import (tests(?:\.\w+)+))", re.M)


def blank_tests(tree):
    """{(class, name): dump} of every test* def directly in the module or a class body, then blank them in place."""
    out = {}
    for c in [tree] + [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for s in c.body:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name.startswith("test"):
                out[(getattr(c, "name", ""), s.name)] = ast.dump(s)
                s.body, s.decorator_list, s.returns = [ast.Pass()], [], None
                s.args = ast.arguments([], [], None, [], [], None, [])
    return out


def test_module(path, hunks, broken):
    """Edit to a test module: tests of that module and of every test module importing it (transitively);
    only the edited test* defs when nothing else changed and nothing else names them."""
    if not _tbf:
        for t in json.loads((DATA / "tests.json").read_text())["tests"]:
            _tbf.setdefault(t["file"], []).append((t["number"], t["id"].rsplit("::", 1)[-1]))
    if path.endswith("/__init__.py"):  # package init runs before every module under it
        d = path[: -len("__init__.py")]
        return {n for f, ts in _tbf.items() if f.startswith(d) for n, _ in ts}
    texts = {str(p.relative_to(SRC)): p.read_text() for p in SRC.glob("tests/**/*.py")}
    imps = {}
    for f, tx in texts.items():
        for a, b, c in IMPORT.findall(tx):
            mod = (a or c).replace(".", "/")
            imps.setdefault(f, set()).update({mod + ".py", mod + "/" + b + ".py"} if b else {mod + ".py"})
    mods, grew = {path}, True
    while grew:
        new = {f for f, s in imps.items() if s & mods} - mods
        grew, mods = bool(new), mods | new
    whole = {n for f in mods for n, _ in _tbf.get(f, ())}
    if broken:
        return whole
    old = texts[path]
    a, b = ast.parse(old), ast.parse("\n".join(pu.apply(old.splitlines(), hunks)) + "\n")
    da, db = blank_tests(a), blank_tests(b)
    if ast.dump(a) != ast.dump(b):
        return whole
    names = {k[1] for k in da if da[k] != db[k]}
    for nm in names:
        ref = re.compile(r"\b%s\b" % re.escape(nm))
        if any(ref.search(l) and not re.match(r"\s*(async\s+)?def %s\b" % re.escape(nm), l)
               for f in mods for l in texts[f].splitlines()):
            return whole
    return {n for f in mods for n, t in _tbf.get(f, ()) if t.split("[")[0] in names}


DOCS = re.compile(r"(?:[\w./-]*\.(?:md|rst|png)|LICENSE|MANIFEST\.in|Makefile|\.gitignore|\.gitpod\.yml|\.gitmodules|"
                  r"sqlglot-integration-tests|sqlglot/py\.typed|"  # submodule pointer (only a test with no tests reads it)
                  r"\.pre-commit-config\.yaml|(?:\.github|posts|pdoc|benchmarks)/[\w./-]+)")  # no test opens or imports these


ADDS = {}  # path -> words on the patch's added lines, per file
_words = []


def unknown_name(path, w):
    """True if new line w names an identifier that neither the pinned sqlglot tree nor the patch's other files
    mention: a class setting is evaluated at import, so a missing name there breaks every test, not just its readers."""
    if w is None:
        return False
    if not _words:
        _words.append(pre().get("words") or set(re.findall(r"\w+", "\n".join(sources().values()))))
    names = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(w.strip() + "\n").readline):
            if tok.type == tokenize.NAME:
                names.add(tok.string)
    except (tokenize.TokenError, SyntaxError):
        pass
    names -= _words[0]
    for p, ws in ADDS.items():
        if p != path:
            names -= ws
    return bool(names)


RT = {}  # path -> call-time edits [(lo, hi, body_lo, body_hi)] for the tpcds gate; None when any edit is not call-time
RESC = {}  # path -> coverage tests if its only import-time edits sit in function bodies import also ran, else None
# ponytail: a hand list of per-process caches filled at call time (generator.py's _DISPATCH_CACHE, once per class):
# coverage credits only the first test per class, and the import snapshot holds only classes built at import
NOCACHE = {"sqlglot/generator.py": {"_build_dispatch", "__init__"}}


def select(path, hunks, m, every):
    sets = m["sets"]
    RT[path] = RESC[path] = None
    if DOCS.fullmatch(path) and path not in m["fixtures"]:
        return set(), "docs"
    if path.endswith(".py") and (SRC / path).is_file():
        tree, verdict = same_code(path, hunks)
        if verdict in ("same", "same-meaning"):
            RT[path] = []
            return set(), "same-ast" if verdict == "same" else "same-meaning"
        if path.startswith("tests/") and path not in m["fixtures"]:
            return test_module(path, hunks, verdict == "broken"), "test"
        if verdict == "broken":
            return None, "broken"
    if path.startswith("sqlglot/") and path.endswith(".py") and (SRC / path).is_file():
        fm = m["files"].get(path, {})
        regs, rng = regions(tree), pre()["rng"].get(path) or stmt_range(tree)
        out, why, rts = set(), [], []
        resc, col = set(), m.get("col")
        col = None if col is None else set(col.get(path, ()))
        ins, nregs = iter(inserted(hunks)), None
        for ln, o, w in edits(hunks):
            c0, c1 = span(o, w)
            reg = runtime(regs, ln, c0, c1) if o is not None and w is not None else None
            lo, hi = rng.get(ln, (ln, ln))
            if o is None or w is None:  # a line added or removed outright: every test that ran the function it sits in
                if o is None and nregs is None:
                    nregs = regions(ast.parse("\n".join(pu.apply((SRC / path).read_text().splitlines(), hunks)) + "\n"))
                reg = one_sided(regs, nregs, hunks, ln, o, w, next(ins) if o is None else None)
                if reg:
                    lo, hi = reg[0], reg[2]
            if reg and set(range(lo, hi + 1)) & set(m["imp"].get(path, ())):
                # a function body that also runs at import (a metaclass, a trie built for a class): coverage only sees
                # the tests that re-ran it, not every test that uses what import built, so treat it as an import edit.
                # pick() may rescue it: if the patched tree imports to the same state (snapshot), its coverage tests.
                # Not if collection ran the body (test-module state no snapshot sees) or it fills a call-time cache
                # (NOCACHE, or any enclosing def or edited line mentioning a cache: @lru_cache, functools.cache, a dict)
                if (col is None or set(range(reg[0], reg[2] + 1)) & col
                        or any(t is not None and "cache" in t.lower() for t in (o, w))
                        or any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.lineno <= reg[0] <= reg[2] <= n.end_lineno
                               and (n.name in NOCACHE.get(path, ()) or "cache" in ast.unparse(n).lower()) for *_, n in regs)):
                    resc = None
                elif resc is not None:
                    resc |= tests_in(fm, sets, lo, hi) or tests_in(fm, sets, reg[0], reg[2])
                reg = None
            elif not reg:
                resc = None
            if reg:
                got = tests_in(fm, sets, lo, hi)
                if not got:  # ponytail: edited line never ran; whole enclosing function as a cheap safety net
                    got = tests_in(fm, sets, reg[0], reg[2])
                out |= got
                if resc is not None:
                    resc |= got
                why.append("rt")
                rts.append((lo, hi, reg[0], reg[2]))
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
                if ca and (lo <= ca[1] <= hi or any(t is not None and CLS_HDR.search(t) for t in (o, w)) or unknown_name(path, w)):
                    # class header, class added or deleted, or _sql_names: other modules read these at import
                    # (parser.py builds FUNCTIONS from sql_names()), so a break can fail every test
                    out |= every
                    why.append("imp")
                    continue
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
                if rd is not None and not unknown_name(path, w):
                    # class setting read only at call time: tests that read it while using this module's dialect
                    out |= rd & got
                    why.append("attr")
                    continue
                got |= {t for sid in set(fm.values()) for t in sets[sid]}
                out |= got or every
                why.append("imp")
        if why and set(why) == {"rt"}:
            RT[path] = rts
        RESC[path] = resc
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


# Each gate replays its test cheaply and digests all it builds (tpcdsgate.py runs every query through the executor on
# small tables and re-runs on the full tables the few queries whose digest differs, nodesgate.py parses at 1/100
# width, gengate.py loops 1/10 as often), so any sqlglot file
# but the executor's own (whose run-time behaviour only the full data shows) can be gated.
GATE = re.compile(r"sqlglot/(?!executor/)[\w/]+\.py")


START = time.monotonic()
BATCH = 100  # s the gates and import checks may use; main() scales it with the batch as the run has 3 min for 40
# commits, 4.5 for 60, so one slow gate early in the final does not switch the later ones off


GATES = {"tests/test_executor.py::TestExecutor::test_execute_tpcds": "tpcds",  # test id -> <name>gate.py, map/<name>.md5
         "tests/test_parser.py::TestParser::test_max_nodes": "nodes",
         "tests/test_generator.py::TestGenerator::test_generator_memory": "gen"}


TPC = ("tests/fixtures/optimizer/tpc-ds/tpc-ds.sql", "tests/fixtures/optimizer/tpc-h/tpc-h.sql")


def reads(path, hunks):
    """(before, after): what tests/helpers.py's two loaders return for a .sql fixture without and with the patch. Tests
    read .sql fixtures only through them (the files tests open directly are jsonpath's cts.json and the executor's
    csv tables). None when either cannot be read: that breaks setUpClass."""
    try:
        spec = importlib.util.spec_from_file_location("fixture_helpers", SRC / "tests" / "helpers.py")
        h = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(h)
        new = Path("/tmp/fixture") / path
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_text("\n".join(pu.apply((SRC / path).read_text(encoding="utf-8").splitlines(), hunks)) + "\n",
                       encoding="utf-8")
        read = lambda f: (list(h.load_sql_fixtures(str(f))), list(h.load_sql_fixture_pairs(str(f))))  # absolute path wins the join
        return read((SRC / path).resolve()), read(new)
    except Exception:  # noqa
        return None


def same_inputs(r):
    """True if a TPC fixture edit leaves every query's options and input SQL as load_sql_fixture_pairs reads them, so
    only expected outputs change. TestExecutor loads both files in setUpClass but reads expected SQL only in
    test_optimized_tpch, and only TPC-H's."""
    return [x[:2] for x in r[0][1]] == [x[:2] for x in r[1][1]]


def gateable(num, files, sel, name=""):
    """The gate replays test num on the pinned fixtures, so every file the test reads must be a sqlglot module it can
    import patched; data files the test never opens do not matter. The tpcds gate also takes executor edits: it
    re-runs on the full tables the queries that ran the edited lines there, or every executed query."""
    # a .py file with no hunks was renamed or its mode changed: the gate cannot see that
    exe = lambda p: name == "tpcds" and p.startswith("sqlglot/executor/") and p.endswith(".py")  # noqa: E731
    return all(((GATE.fullmatch(p) or exe(p)) and files[p]) or (not p.endswith(".py") and sel[p] is not None and num not in sel[p]) for p in files)


def gate_cpu(name, files):
    """Board CPU seconds of one gate call (Boss round 3): a gate whose sim saving is below this costs more than it can
    win. tpcds checks only the queries RT says the edits reach, else every query (8.3 s on the server for a patch it
    passes; one it stops early on costs 1-2 s)."""
    py = [p for p in files if p.endswith(".py")]
    return {"nodes": 0.6, "gen": 1.3}.get(name) or (7 if py and all(RT.get(p) is not None for p in py) else 9)


def unchanged(name, patch, files=()):
    """True when <name>gate.py digests the patched tree the same as the pinned one, so its test cannot break. A gate
    that also prints CPU seconds must stay within 1.5x the pinned run (a slower parse can time the real test out)."""
    left = BATCH - (time.monotonic() - START)  # the whole batch must finish in 3 min; a slow or hung gate just keeps the test
    if left < 10:
        return False
    try:
        env = None
        py = [p for p in files if p.endswith(".py")]
        if name == "tpcds" and py and all(RT.get(p) is not None for p in py):  # only the queries these edits can reach
            env = dict(os.environ, GATE_LINES=json.dumps({p: RT[p] for p in py}))
        t0 = time.monotonic()
        r = subprocess.run([sys.executable, str(HERE / f"{name}gate.py"), str(patch)], capture_output=True, text=True,
                           timeout=min(45, left), env=env)
        log("gate", name, Path(patch).stem, f"wall {time.monotonic() - t0:.1f}", *r.stdout.split()[1:2])
        got, want = r.stdout.split(), (HERE / "map" / f"{name}.md5").read_text().split()
        return (r.returncode == 0 and len(got) == len(want) and got[0] == want[0]
                and (len(got) < 2 or float(got[1]) <= 1.5 * float(want[1]) + 0.1))
    except Exception:  # noqa: timeout, missing digest
        return False


NORUN = ("::test_lazy_load", "tests/test_sqlglotc.py::", "tests/test_typing.py::", "::test_generator_memory",
         "::test_max_nodes", "::test_execute_tpcds")  # a child python imports the pinned tree; sqlglotc builds the C
# extension; test_typing scans typing/__file__, which the overlay moves; the memory test's result depends on what ran
# before it in its process (mutrun seed 101 caught one); the gates own the other two
CPU = json.loads((HERE / "map" / "cpu.json").read_text())  # CPU s per test and per class setup ("<mod.Class>"), here
BUDGET = float(os.environ.get("BUDGET", 8))  # total warm-run CPU s per batch: rivals spend 1-14 s CPU on the whole board
KEEP = float(os.environ.get("KEEP", 0.5))  # share of a run's saving expected to survive its failures (most commits break a test)
CAL = [1.0, 1.0]  # child CPU measured, predicted: this machine's speed against the one the map was made on


def cpu_of(R, ids, ms):
    """Predicted CPU seconds for warm.run on R: fork and sqlglot import, each class's import and setUpClass, tests."""
    cls = {"<" + ids[n].split("::")[0][:-3].replace("/", ".") + "." + ids[n].split("::")[1] + ">" for n in R}
    return 0.25 + sum(CPU.get(c, 0.5) for c in cls) + sum(CPU.get(ids[n], 2 * ms[n] / 1000) for n in R)


def plan(chosen, files, ids, ms):
    """(gain s, tests to run here, predicted CPU s) when running some of chosen here and keeping only those that
    fail is expected to beat sending them all to workers, else None. Only edits of existing .py files: the overlay
    imports patched modules, it cannot delete, add or rename files."""
    if not chosen or not all(p.endswith(".py") and hs and (SRC / p).is_file() for p, hs in files.items()):
        return None
    s, base, best = CAL[0] / CAL[1], split(chosen, ms)[0] / 1000, None
    run = {n for n in chosen if not any(x in ids[n] for x in NORUN)}
    for R in (run, {n for n in run if s * CPU.get(ids[n], 9e9) <= ms[n] / 1000}):
        c = cpu_of(R, ids, ms) if R else 0
        rest = chosen - R
        gain = KEEP * (base - split(rest, ms)[0] / 1000) - s * c
        if R and s * c <= 20 and gain > 0.1 and (best is None or gain > best[0]):
            best = (gain, R, c)
    return best


_lazy = {}


def lazy_ok(patch, files):
    """test_lazy_load runs `import sqlglot` in a fresh python, which coverage never sees: True when that import, on the
    patched tree, still loads only the default dialect, so the test cannot fail."""
    if patch not in _lazy and (lz := pre().get("lazy")) is not None and not re.search(
            r"^(?:new file mode|deleted file mode|rename from|copy from)", patch.read_text(errors="replace"), re.M) and all(
            p.endswith(".py") and hs and (SRC / p).is_file() and p not in lz for p, hs in files.items()):
        _lazy[patch] = True  # edits only files a bare `import sqlglot` never loads: the same modules load as pinned
    if patch not in _lazy:
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import patchutil as pu; pu.overlay(sys.argv[2], sys.argv[3]); "
                "import sqlglot; print(len(sqlglot.Dialect._classes))")
        left = BATCH - (time.monotonic() - START)  # same batch budget as the gates; False keeps test_lazy_load
        try:
            if left < 10:
                raise TimeoutError
            r = subprocess.run([sys.executable, "-c", code, str(HERE), str(patch), str(SRC)], capture_output=True,
                               text=True, timeout=min(30, left))
            _lazy[patch] = r.returncode == 0 and r.stdout.split() == ["1"]
        except Exception:  # noqa: timeout
            _lazy[patch] = False
    return _lazy[patch]


KEYMIN = float(os.environ.get("KEYMIN", 5000))  # ms of selected work worth a ~3 s CPU state snapshot


_keys = {}
_snap = {}


def snapdiff(patch):
    """snap.py's {(label, key): (pinned fp, patched fp)} for the entries that differ, or None (no time, import fails)."""
    if patch not in _snap:
        base, left, _snap[patch] = HERE / "map" / "dsnap.pkl", BATCH - (time.monotonic() - START), None
        if left >= 15 and base.is_file():
            try:
                r = subprocess.run([sys.executable, str(HERE / "snap.py"), str(SRC), str(patch), str(base)],
                                   capture_output=True, timeout=min(30, left), env=dict(os.environ, PYTHONHASHSEED="0"))
                if not r.returncode:
                    _snap[patch] = pickle.loads(r.stdout)
            except Exception:  # noqa: timeout
                pass
    return _snap[patch]


def nocode(v):
    """A fingerprint with every code object's fingerprint blanked: what is left is the state import built."""
    if isinstance(v, tuple):
        return ("code",) if v and v[0] == "code" else tuple(nocode(x) for x in v)
    return v


def same_state(patch):
    """True if the patched tree imports to the pinned state except for the code of functions (edited bodies)."""
    diff = snapdiff(patch)
    return diff is not None and all(x is not None and y is not None and nocode(x) == nocode(y) for x, y in diff.values())


def keysel(patch, files):
    """When the patched tree imports to the same state as the pinned one except for values of existing entries in
    dicts that map/keys.json recorded per key (dicts nothing iterated, copied or compared while tests ran), the tests
    that looked up one of those keys; else None. snap.py fingerprints every module global and class attribute after
    importing every module, so an entry copied into other dicts or folded into anything else at import shows up too."""
    base, km = HERE / "map" / "dsnap.pkl", HERE / "map" / "keys.json"
    left = BATCH - (time.monotonic() - START)
    if left < 15 or not (base.is_file() and km.is_file()):
        return None
    for p, hs in files.items():  # every edited line inside a dict display keymap.py recorded, or no ~3 s snapshot
        lines = set()

        def body(b):
            for s in b:
                if isinstance(s, (ast.Assign, ast.AnnAssign)) and isinstance(s.value, (ast.Dict, ast.DictComp)):
                    lines.update(range(s.value.lineno, s.value.end_lineno + 1))
                elif isinstance(s, ast.ClassDef):
                    body(s.body)
                elif isinstance(s, (ast.If, ast.Try, ast.With)):
                    for b2 in [s.body, getattr(s, "orelse", []), getattr(s, "finalbody", [])] + [h.body for h in getattr(s, "handlers", [])]:
                        body(b2)
        body(ast.parse((SRC / p).read_text()).body)
        if not {ln for ln, _, _ in edits(hs)} <= lines:  # a cheap filter: the snapshot diff below is the check
            return None
    diff = snapdiff(patch)
    if diff is None:
        return None
    try:
        if "d" not in _keys:
            _keys.update(json.loads(km.read_text()))
            am = HERE / "map" / "args.json"
            if am.is_file():
                _keys["A"] = json.loads(am.read_text())
        K = _keys
    except Exception:  # noqa: timeout
        return None
    out = set()
    for (label, key), (x, y) in diff.items():
        if "A" in K and label.endswith(".required_args") and key is None and x and y and x[0] == y[0] == "set":
            # arg_types values only feed required_args, which only error_messages reads: the tests where it ran on
            # an instance of this class missing a key that changed sides
            for k in {v for _, v in x[1:]} ^ {v for _, v in y[1:]}:
                s = K["A"]["p"].get(f"{label[:-14]}|{k}")
                if s == -1:
                    return None
                out |= set(K["A"]["sets"][s]) if s is not None else set()
            continue
        if "A" in K and label.endswith(".arg_types") and key is not None and x is not None and y is not None:
            continue  # a value read only by __init_subclass__ (core.py:110); its effect is the required_args entry
        e = K["d"].get(label)
        if key is None or x is None or y is None or not e or "k" not in e or e["k"].get(key, 0) == -1:
            return None  # not a dict value, a key added or removed, or a dict read some other way
        if key in e["k"]:
            out |= set(K["sets"][e["k"][key]])
    return out


NEW = re.compile(r"^diff --git a/(\S+) b/\1\nnew file mode", re.M)


def new_module(path, ids):
    """Tests that can see a brand-new sqlglot module, or None for every test. Python and pytest import a file only by
    name, so a name nothing in the pinned tree mentions is reached only by directory listings: sqlglotc/setup.py (lists
    subpackages, run by test_sqlglotc) and test_typing (lists sqlglot/typing)."""
    p = Path(path)
    if not path.startswith("sqlglot/") or p.suffix != ".py" or p.name.startswith("test") or p.name.endswith("_test.py") \
            or p.name == "conftest.py":
        return None  # pytest or unittest would collect it, or it is not code
    pkg, name = (p.parent.parent, p.parent.name) if p.name == "__init__.py" else (p.parent, p.stem)
    if (SRC / pkg / name).exists() or (SRC / pkg / f"{name}.py").exists() or not (SRC / pkg / "__init__.py").is_file():
        return None  # shadows or turns into something that exists, or lands outside a regular package
    word = re.compile(rf"\b{re.escape(name)}\b")
    for top in ("sqlglot", "tests", "sqlglotc"):
        for f in (SRC / top).rglob("*"):
            if f.is_file() and f.suffix not in (".gz", ".pyc") and word.search(f.read_text(encoding="utf-8", errors="replace")):
                return None  # something may import it by that name
    return {n for n, i in ids.items() if i.startswith(("tests/test_sqlglotc.py::", "tests/test_typing.py::"))}


_test_text = []


def new_test_file(path, hunks):
    """set() for a brand-new file under tests/ that no pinned test can see, else None. Its own tests have no number,
    and no test lists a directory, so it reaches others only by a name a pinned test already uses, or, for a module
    pytest collects (one process for the whole run), by what importing it runs: so that must be only pinned imports and
    plain class and function definitions."""
    p = Path(path)
    if not path.startswith("tests/") or p.name == "conftest.py" or not (SRC / p.parent / "__init__.py").is_file() \
            and not path.startswith("tests/fixtures/"):
        return None
    if not (SRC / p.parent).is_dir():
        return None
    if not _test_text:
        _test_text.append("\n".join(f.read_text(encoding="utf-8", errors="replace") for f in (SRC / "tests").rglob("*.py")))
    if re.search(rf"\b{re.escape(p.stem)}\b", _test_text[0]):
        return None  # a pinned test may open or import it by that name
    if p.suffix != ".py" or not (p.name.startswith("test") or p.stem.endswith("_test")):
        return set()  # data, or a module only an import by name would load
    try:
        tree = ast.parse("\n".join(pu.apply([], hunks)) + "\n")
    except SyntaxError:
        return None

    def pinned(mod):
        top = mod.split(".")[0]
        if top in ("sqlglot", "tests"):
            f = SRC / mod.replace(".", "/")
            return f.with_suffix(".py").is_file() or (f / "__init__.py").is_file()
        return top in sys.stdlib_module_names or top == "pytest"

    def plain(d):  # skip marks, mock patches, classmethod: build a wrapper at import, touch no registry a pinned test reads
        while isinstance(d, (ast.Call, ast.Attribute)):
            d = d.func if isinstance(d, ast.Call) else d.value
        return isinstance(d, ast.Name) and d.id in ("unittest", "pytest", "mock", "patch", "classmethod", "staticmethod",
                                                    "property")

    def const(v):
        return isinstance(v, (ast.Constant, ast.Name, ast.Attribute)) or isinstance(v, (ast.Tuple, ast.List, ast.Set)) \
            and all(const(e) for e in v.elts) or isinstance(v, ast.Dict) and all(const(e) for e in v.keys + v.values if e)

    def ok(s):
        if isinstance(s, ast.Import):
            return all(pinned(a.name) for a in s.names)
        if isinstance(s, ast.ImportFrom):
            return s.level == 0 and pinned(s.module)
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return all(plain(d) for d in s.decorator_list)
        if isinstance(s, ast.ClassDef):
            return all(plain(d) for d in s.decorator_list) and all(ok(x) for x in s.body)
        if isinstance(s, ast.Expr):
            return isinstance(s.value, ast.Constant)
        if isinstance(s, ast.Assign):
            return all(isinstance(t, ast.Name) for t in s.targets) and const(s.value)
        if isinstance(s, ast.If):  # if __name__ == "__main__": never runs under pytest
            return ast.unparse(s.test).replace("'", '"') == '__name__ == "__main__"'
        return isinstance(s, ast.Pass)

    return set() if all(ok(s) for s in tree.body) else None


def pick(patch, maps, every, ids, ms, fileof, gated):
    text = patch.read_text(encoding="utf-8", errors="replace")
    files = pu.parse(text)
    for b in re.findall(r"^diff --git a/.* b/(.*)$", text, re.M):
        files.setdefault(b, [])  # binary, rename or mode-only: no hunks, still a changed file
    new = set(NEW.findall(text))
    gone = set(re.findall(r"^diff --git a/(\S+) b/\1\ndeleted file mode", text, re.M))
    ADDS.clear()
    ADDS.update({p: {x for _, b in hs for t, l in b if t == "+" for x in re.findall(r"\w+", l)} for p, hs in files.items()})
    chosen, whys, sel = set(), [], {}
    RT.clear()
    RESC.clear()
    for path, hunks in files.items():
        got = None
        if path in new and not (SRC / path).exists():
            got = new_module(path, ids) if path.startswith("sqlglot/") else new_test_file(path, hunks)
        if got is not None:  # else fall through: the old rules still own every file the new rule does not claim
            why = "new"
        elif (path in ("pyproject.toml", "setup.py", "setup.cfg") and hunks and (SRC / path).is_file() and path not in gone
              and not any("pytest" in l for _, b in hunks for t, l in b if t != " ")):
            # tests run from the checkout on PYTHONPATH, nothing installs it; test_sqlglotc builds in a temp dir with a
            # pretend version. Only pytest reads these files, from a [tool.pytest*] / [tool:pytest] table
            got, why = set(), "pack"
        elif path.startswith("sqlglotc/"):  # only test_sqlglotc runs sqlglotc/setup.py; nothing else reads sqlglotc/
            got, why = {n for n, i in ids.items() if i.startswith("tests/test_sqlglotc.py::")}, "sqlglotc"
        else:
            got, why = select(path, hunks, maps, every)
        if got is not None and "imp" in why.split("+") and not lazy_ok(patch, files):
            got = got | {n for n, i in ids.items() if i.endswith("::test_lazy_load")}
        if why == "fixture" and path.endswith(".sql") and (r := reads(path, hunks)):
            if r[0] == r[1]:  # comments, blank lines: no test can see the edit
                got, why = set(), "fixture-same"
            elif path in TPC and same_inputs(r):
                got = {n for n in got if not ids[n].startswith("tests/test_executor.py::")
                       or (path == TPC[1] and ids[n].endswith("::test_optimized_tpch"))}
                why = "fixture-inputs"
        whys.append(why)
        sel[path] = got
        chosen |= every if got is None else got
    parts = {w for y in whys for w in y.split("+")}
    if (parts & {"imp", "attr", "cls"} and parts <= {"imp", "attr", "cls"} and split(chosen, ms)[0] >= KEYMIN
            and all(p.startswith("sqlglot/") and p.endswith(".py") and hs and (SRC / p).is_file() for p, hs in files.items())):
        ks = keysel(patch, files)
        if ks is not None:
            chosen = ks | {n for n in chosen if ids[n].endswith("::test_lazy_load")}
            whys.append(f"keys{len(ks)}")
    if ("imp" in parts and not any(w.startswith("keys") for w in whys) and split(chosen, ms)[0] >= KEYMIN
            and all(RESC.get(p) is not None and hs for p, hs in files.items()) and same_state(patch)):
        # every import edit sits in a function body that import also ran, and import built the same state
        chosen = set().union(*RESC.values()) | {n for n in chosen if ids[n].endswith("::test_lazy_load")}
        whys.append("rescue")
    for num, name in sorted(gated.items(), key=lambda g: g[1] == "tpcds"):  # tpcds last: its saving depends on the rest
        if (num in chosen and gateable(num, files, sel, name)
                and split(chosen, ms)[0] - split(chosen - {num}, ms)[0] > 1000 * gate_cpu(name, files)
                and unchanged(name, patch, files)):
            chosen.discard(num)
            whys.append(f"{name}-gate")
    return chosen, whys, files


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    fileof = {t["number"]: t["file"] for t in tests}
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    every = set(ms)
    gated = {t["number"]: GATES[t["id"]] for t in tests if t["id"] in GATES}
    ids = {t["number"]: t["id"] for t in tests}
    maps = load_maps()
    patches = sorted((DATA / "commits").glob("*.patch"))
    global BATCH
    BATCH = 100 * max(1, len(patches) / 40)
    out, queue = {}, []
    for patch in patches:
        try:
            chosen, whys, files = pick(patch, maps, every, ids, ms, fileof, gated)
            if p := plan(chosen, files, ids, ms):
                queue.append((p[0] / p[2], patch, files))
        except Exception as x:  # noqa: a change this program cannot read runs everything instead of losing the run
            chosen, whys = every, ["error", type(x).__name__]
        out[patch] = chosen, whys
    log("picked", round(time.monotonic() - START, 1))
    limit = 1.4 * BATCH  # the batch has 3 min for 40 commits, 4.5 for 60: runs stop well before
    spent = 0.0  # child CPU of the runs so far: the board adds every CPU second to the score
    for _, patch, files in sorted(queue, key=lambda q: -q[0]):  # best saving per CPU second first
        chosen, whys = out[patch]
        p = plan(chosen, files, ids, ms)  # again, at this machine's measured speed
        if not p:
            continue
        s = CAL[0] / CAL[1]
        wait = 2 * s * p[2] + 2  # a patch that makes a test hang or crawl times the run out: keep what passed by then
        if time.monotonic() - START + wait > limit or spent + s * p[2] > BUDGET:
            continue
        t0 = time.monotonic()
        ok, cpu, rss = warm.run(patch, SRC, [ids[n] for n in p[1]], wait)
        log("run", patch.stem, len(p[1]), f"wall {time.monotonic() - t0:.1f}", f"cpu {cpu:.1f}", f"wait {wait:.0f}", f"rss {rss}M")
        CAL[0], CAL[1], spent = CAL[0] + cpu, CAL[1] + p[2], spent + cpu
        if ok:
            num = {ids[n]: n for n in p[1]}
            out[patch] = chosen - {num[i] for i in ok}, whys + [f"ran{len(p[1])}-{len(p[1]) - len(ok)}"]
    for patch in patches:
        chosen, whys = out[patch]
        cost, ws = split(chosen, ms)
        log(patch.stem, whys, len(chosen), cost)
        print(json.dumps({"commit": patch.stem, "workers": ws}))
    log("cal", round(CAL[0], 1), round(CAL[1], 1), round(time.monotonic() - START, 1))


def build_pre():
    """map/pre.pkl (see pre()). The import list comes from a fresh python, as test_lazy_load runs it."""
    code = "import sys; sys.path.insert(0, sys.argv[1]); import sqlglot; print(*[getattr(m, '__file__', None) or '' for m in list(sys.modules.values())])"
    got = subprocess.run([sys.executable, "-c", code, str(SRC)], capture_output=True, text=True, check=True).stdout.split()
    root = SRC.resolve()
    lazy = {str(Path(f).resolve().relative_to(root)) for f in got if Path(f).resolve().is_relative_to(root)}
    assert "sqlglot/__init__.py" in lazy, lazy
    src = sources()
    out = {"rng": {p: stmt_range(ast.parse(t)) for p, t in src.items()}, "words": set(re.findall(r"\w+", "\n".join(src.values()))),
           "lazy": lazy}
    (HERE / "map" / "pre.pkl").write_bytes(pickle.dumps(out))
    log(len(out["rng"]), "files,", len(lazy), "loaded by import sqlglot")


if __name__ == "__main__":
    build_pre() if sys.argv[1:] == ["--pre"] else main()
