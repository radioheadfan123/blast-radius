"""New-def mutants for exp-b13: add one method to a dialect generator class (sqlglot/generators/<d>.py), either a name an
ancestor defines (anc) or a <key>_sql no class in its MRO defines (key, keys the map saw looked up on it first, not in
its TRANSFORMS, which would win over the method; DEAD=1 allows those). The body
raises or, for _sql names, returns "MUTANT". Pick with the new-def rule (map/tmap.json) and without it (the import
fallback), run pytest on what the rule dropped plus what it kept in a mutated copy. Unsafe = a dropped test fails;
"caught" counts kept tests that fail (the mutant is live).
usage (from ~/code/blast-radius): DATA_DIR=kit/sample SQLGLOT_SRC=$PWD/sqlglot .venv/bin/python tools/dfuzz.py N SEED"""
import ast, difflib, importlib, json, os, random, re, shutil, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, os.path.expanduser("~/code/blast-radius/wt/b13"))
import selector as S  # noqa: E402
sys.path.insert(0, str(S.SRC))  # pristine sqlglot, to read each generator's TRANSFORMS

n, seed = int(sys.argv[1]), int(sys.argv[2])
rnd = random.Random(seed)
tests = json.loads((S.DATA / "tests.json").read_text())["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
ids = {t["number"]: t["id"] for t in tests}
fileof = {t["number"]: t["file"] for t in tests}
every, maps = set(ms), S.load_maps()
T = maps["tmap"]
off = dict(maps, tmap=None)

cands = []  # (rel, class name, def name, kind)
for L, mro in T["mro"].items():
    mod, _, q = L.rpartition(".")
    rel = mod.replace(".", "/") + ".py"
    if not re.fullmatch(r"sqlglot/generators/\w+\.py", rel) or "." in q:
        continue
    names = {}
    for a in mro:
        am, _, aq = a.rpartition(".")
        src = S.sources().get(am.replace(".", "/") + ".py")
        c = src and next((c for c in ast.parse(src).body if isinstance(c, ast.ClassDef) and c.name == aq), None)
        for st in (c.body if c else []):
            if isinstance(st, ast.FunctionDef):
                names.setdefault(st.name, a)
    anc = [x for x, a in names.items() if a != L and not x.startswith("__")]
    seen = [p.split("|")[1] for p in T["p"] if p.startswith(L + "|") and not p.endswith("|*")]
    tr = {c.key for c in getattr(importlib.import_module(mod), q).TRANSFORMS}  # TRANSFORMS wins: a <key>_sql there is dead
    key = [k for k in seen if k + "_sql" not in names and (k not in tr or os.environ.get("DEAD"))]
    cands += [(rel, q, x, "anc") for x in rnd.sample(anc, min(6, len(anc)))]
    cands += [(rel, q, k + "_sql", "key") for k in rnd.sample(key, min(6, len(key)))]
rnd.shuffle(cands)
files = sorted({c[0] for c in cands})
print("classes", len(T["mro"]), "candidates", len(cands), "files", len(files), flush=True)

W = Path(os.path.expanduser("~/code/blast-radius/scratch"))
MUT = W / "mut"
shutil.rmtree(MUT, ignore_errors=True)
shutil.copytree(S.SRC, MUT, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv"))
(W / "dfz").mkdir(parents=True, exist_ok=True)
env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
done = unsafe = 0
used_files = set()
for rel, q, name, kind in cands:
    if done >= n:
        break
    if rel in used_files and len(used_files) < min(10, len(files)):
        continue  # spread over 10 dialect files first
    used_files.add(rel)
    src = (S.SRC / rel).read_text()
    lines = src.splitlines(keepends=True)
    c = next(c for c in ast.parse(src).body if isinstance(c, ast.ClassDef) and c.name == q)
    defs = [st for st in c.body if isinstance(st, ast.FunctionDef)]
    at = rnd.choice(defs).end_lineno if defs else c.end_lineno
    body = '        return "MUTANT"\n' if name.endswith("_sql") and rnd.random() < 0.5 else '        raise RuntimeError("MUTANT")\n'
    new = ["\n", f"    def {name}(self, *args, **kwargs):\n", body]
    msrc = "".join(lines[:at] + new + lines[at:])
    ast.parse(msrc)
    pf = W / "dfz" / f"{seed}_{done}.patch"
    pf.write_text(f"diff --git a/{rel} b/{rel}\n" + "".join(difflib.unified_diff(lines, msrc.splitlines(keepends=True), f"a/{rel}", f"b/{rel}")))
    S.START = time.monotonic()
    A, wa, _ = S.pick(pf, maps, every, ids, ms, fileof, {})
    S.START = time.monotonic()
    B, wb, _ = S.pick(pf, off, every, ids, ms, fileof, {})
    drop = sorted(B - A)
    t0, bad, caught = time.time(), [], []
    run = sorted(set(drop) | A)
    if run:
        (MUT / rel).write_text(msrc)
        try:
            r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-n", "2", "-p", "no:cacheprovider", "--timeout", "300", "--tb=no", "-rfE",
                                *[ids[x] for x in run]], cwd=MUT, capture_output=True, text=True, env=env, timeout=3600)
            out = r.stdout.split("short test summary info")[-1] if r.returncode == 1 else r.stdout
            hit = set(re.findall(r"tests/[\w/]+\.py(?:::\w+)*", out)) if r.returncode in (1, 2, 3, 4) else set()
            fail = [x for x in run if any(ids[x] == h or ids[x].startswith(h + "::") for h in hit)]
            bad = [ids[x] for x in fail if x in set(drop)]
            caught = [x for x in fail if x in A]
            if r.returncode not in (0, 1):
                bad = bad or [f"pytest rc {r.returncode}"]
        except subprocess.TimeoutExpired:
            bad = ["timeout"]
        finally:
            (MUT / rel).write_text(src)
    done += 1
    unsafe += bool(bad)
    print(done, kind, f"{rel}:{q}.{name}", wa, wb, "A", len(A), "B", len(B), "drop", len(drop), "caught", len(caught),
          ("UNSAFE " + str(bad[:3])) if bad else "ok", f"run {time.time() - t0:.0f}s", flush=True)
print("mutants", done, "unsafe", unsafe)
