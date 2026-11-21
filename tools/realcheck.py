"""Real-run check of chosen selector verdicts: random hidden-style edits the selector files under KINDS,
full test suite on a mutated copy, report failing tests the selector skipped.
usage (clone root, venv python, pristine sqlglot at ../sqlglot): realcheck.py N SEED KIND[,KIND] [WORKERS]"""
import ast, json, os, random, re, shutil, subprocess, sys, time
os.environ["SQLGLOT_SRC"] = os.path.abspath("../sqlglot")
sys.path.insert(0, ".")
import selector as S

n, seed, kinds = int(sys.argv[1]), int(sys.argv[2]), set(sys.argv[3].split(","))
workers = sys.argv[4] if len(sys.argv) > 4 else "8"
rnd = random.Random(seed)
m = S.load_maps()
tests = json.load(open("tools/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in tests}
every = set(num.values())
OPS = [(r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"\b(\d+)\b", None), (r"\band\b", "or"), (r"\bor\b", "and"),
       (r"\bnot ", ""), (r"==", "!=")]
MUT = os.path.abspath("../mut")
if not os.path.isdir(MUT):
    shutil.copytree("../sqlglot", MUT, ignore=shutil.ignore_patterns(".git", "__pycache__"))


def run():
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-n", workers, "-p", "no:cacheprovider", "--timeout", "120",
                        "-rfE", "tests"], cwd=MUT, capture_output=True, text=True)
    failed = set()
    for x in re.findall(r"^(?:FAILED|ERROR) (\S+)", r.stdout, re.M):
        failed |= {num[x]} if x in num else {v for k, v in num.items() if k.startswith(x.split("::")[0] + "::")}
    return failed


pool = []  # (rel, line): import-time lines, plus every line of a bare string statement
for rel in sorted(m["imp"]):
    text = (S.SRC / rel).read_text()
    pool += [(rel, ln) for ln in m["imp"][rel]]
    for s in ast.walk(ast.parse(text)):
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str):
            pool += [(rel, ln) for ln in range(s.lineno, s.end_lineno + 1)]
rnd.shuffle(pool)

base = run()
print("baseline failing", len(base), flush=True)
done = bad = 0
for rel, ln in pool:
    if done >= n:
        break
    lines = (S.SRC / rel).read_text().split("\n")
    old = lines[ln - 1]
    ops = [(p, r) for p, r in OPS if re.search(p, old)]
    if not ops:
        continue
    p, r = rnd.choice(ops)
    new = re.sub(p, (lambda mo: str(int(mo.group(1)) + 1)) if r is None else r, old, count=1)
    if new == old:
        continue
    got, why = S.select(rel, [(ln, [("-", old), ("+", new)])], m, every)
    if not set(why.split("+")) & kinds:
        continue
    if "new" in kinds:  # only edits the import-read trace newly lets through
        S._impread[:] = [None]
        w0 = S.select(rel, [(ln, [("-", old), ("+", new)])], m, every)[1]
        S._impread.clear()
        if w0 == why:
            continue
    got = every if got is None else got
    f = os.path.join(MUT, rel)
    open(f, "w").write("\n".join(lines[:ln - 1] + [new] + lines[ln:]))
    t0 = time.time()
    failed = run() - base
    open(f, "w").write("\n".join(lines))
    missed = sorted(failed - got)
    done += 1; bad += bool(missed)
    print(f"{rel}:{ln} {old.strip()[:60]!r} -> {new.strip()[:60]!r} [{why}] sel {len(got)} failed {len(failed)} "
          f"{'MISSED ' + str([t['id'] for t in tests if t['number'] in missed][:3]) if missed else 'ok'} {time.time() - t0:.0f}s",
          flush=True)
print("mutants", done, "with misses", bad)
