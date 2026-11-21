"""Random one-line code edits whose selection includes test_execute_tpcds: does that test really fail,
and does the plan-diff proxy (tpcdsplan.py) see a change? Also runs the cheap optimizer test_tpcds.
usage (clone root, venv python, pristine sqlglot at ../sqlglot): tpcdscheck.py N SEED"""
import json, os, random, re, shutil, subprocess, sys, time
os.environ["SQLGLOT_SRC"] = os.path.abspath("../sqlglot")
os.environ["DATA_DIR"] = os.path.abspath("tools")  # tests.json
sys.path.insert(0, ".")
import selector as S

n, seed = int(sys.argv[1]), int(sys.argv[2])
rnd = random.Random(seed)
m = S.load_maps()
tests = json.load(open("tools/tests.json"))["tests"]
every = {t["number"] for t in tests}
EX = next(t["number"] for t in tests if t["id"].endswith("::test_execute_tpcds"))
IDS = ["tests/test_executor.py::TestExecutor::test_execute_tpcds", "tests/test_optimizer.py::TestOptimizer::test_tpcds"]
OPS = [(r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"\b(\d+)\b", None), (r"\band\b", "or"), (r"\bor\b", "and"),
       (r"\bnot ", ""), (r"==", "!=")]
MUT = os.path.abspath("../mut")
if not os.path.isdir(MUT):
    shutil.copytree("../sqlglot", MUT, ignore=shutil.ignore_patterns(".git", "__pycache__"))
GATE = os.path.abspath("tpcdsgate.py")
GATED = re.compile(r"sqlglot/(?!executor/)[\w/]+\.py")  # selector GATE (gate v2)
V1 = re.compile(r"sqlglot/(parser|parsers/(?!__init__)\w+|optimizer/(?!__init__|scope\b)\w+|generators/duckdb|dialects/duckdb)\.py")  # already fuzzed with gate v1 (tc1, tc2), so only the newly gated files


def capped(cmd, secs, **kw):
    """stdout and return code, or None when it runs past secs (the whole process group is killed: pool workers too)."""
    p = subprocess.Popen(cmd, cwd=MUT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True, **kw)
    try:
        return p.communicate(timeout=secs)[0], p.returncode
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, 9)
        p.communicate()
        return None


def plan():
    r = capped([sys.executable, GATE], 60, env={**os.environ, "SQLGLOT_SRC": MUT})  # the selector gives up at 30 s
    return r[0].split()[-1] if r and r[1] == 0 else "CRASH"


def run():
    r = capped([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE", *IDS], 150)  # normal: 25-45 s
    if r is None:
        return {"test_execute_tpcds", "HANG"}  # a hang fails on the board too (3 min cap)
    return {x.split("::")[-1] for x in IDS if re.search(r"^(FAILED|ERROR) " + re.escape(x), r[0], re.M)}


t0 = time.time()
base_plan, base = plan(), run()
print("baseline failing", base, "gate", base_plan, f"{time.time() - t0:.0f}s", flush=True)
pool = [(f, int(ln)) for f in sorted(m["files"]) if GATED.fullmatch(f) and not V1.fullmatch(f) for ln in m["files"][f]]
rnd.shuffle(pool)
done = 0
for rel, ln in pool:
    if done >= n:
        break
    lines = (S.SRC / rel).read_text().split("\n")
    old = lines[ln - 1]
    if old.lstrip().startswith(("#", "def ", "class ", "@", "import ", "from ")):
        continue
    ops = [(p, r) for p, r in OPS if re.search(p, old.split("#")[0])]
    if not ops:
        continue
    p, r = rnd.choice(ops)
    new = re.sub(p, (lambda mo: str(int(mo.group(1)) + 1)) if r is None else r, old, count=1)
    if new == old:
        continue
    got, why = S.select(rel, [(ln, [("-", old), ("+", new)])], m, every)
    if got is None or EX not in got:
        continue
    f = os.path.join(MUT, rel)
    open(f, "w").write("\n".join(lines[:ln - 1] + [new] + lines[ln:]))
    t0 = time.time()
    try:
        pl = plan()
        prox = "same" if pl == base_plan else ("crash" if pl == "CRASH" else "diff")
        failed = run() - base
    finally:
        open(f, "w").write("\n".join(lines))
    done += 1
    print(f"{rel}:{ln} {old.strip()[:50]!r} -> {new.strip()[:50]!r} [{why}] sel {len(got)} proxy {prox} "
          f"exec {'FAIL' if 'test_execute_tpcds' in failed else 'pass'} opt {'FAIL' if 'test_tpcds' in failed else 'pass'} "
          f"{time.time() - t0:.0f}s{' HANG' if 'HANG' in failed else ''}", flush=True)
print("done", done)
