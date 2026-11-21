"""Gate v3 safety fuzz: random one-line edits in gated files whose selection includes test_execute_tpcds. The gate runs
on the edit as a patch (as the selector does); when it passes after re-running queries on the full data (the case gate
v2 never skipped), the real test runs on the edited tree and must pass.
usage (clone root, venv python, pristine sqlglot at ../sqlglot, map/ built, tools/tests.json): v3check.py N SEED"""
import difflib, json, os, random, re, shutil, subprocess, sys, time
SRC = os.path.abspath("../sqlglot")
os.environ["SQLGLOT_SRC"], os.environ["DATA_DIR"] = SRC, os.path.abspath("tools")
sys.path.insert(0, ".")
import selector as S

n, seed = int(sys.argv[1]), int(sys.argv[2])
rnd = random.Random(seed)
m = S.load_maps()
tests = json.load(open("tools/tests.json"))["tests"]
every = {t["number"] for t in tests}
ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
EX = next(t["number"] for t in tests if t["id"] == ID)
OPS = [(r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"\b(\d+)\b", None), (r"\band\b", "or"), (r"\bor\b", "and"),
       (r"\bnot ", ""), (r"==", "!=")]
MUT = os.path.abspath("../mut")
WANT = open("map/tpcds.md5").read().split()[0]


def capped(cmd, secs, cwd, env=None):
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, env=env)
    try:
        return p.communicate(timeout=secs)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, 9)
        p.communicate()
        return None


pool = [(f, int(ln)) for f in sorted(m["files"]) if S.GATE.fullmatch(f) for ln in m["files"][f]]
rnd.shuffle(pool)
done, tally = 0, {}
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
    newlines = lines[:ln - 1] + [new] + lines[ln:]
    open("/tmp/m.patch", "w").write("diff --git a/%s b/%s\n" % (rel, rel) + "".join(
        difflib.unified_diff([x + "\n" for x in lines], [x + "\n" for x in newlines], "a/" + rel, "b/" + rel)))
    t0 = time.time()
    g = capped([sys.executable, "tpcdsgate.py", "/tmp/m.patch"], 60, ".")
    reruns = re.findall(r"^rerun (\d+) (\w+)", g[1], re.M) if g else []
    verdict = "crash" if not g or not g[0].split() else ("same" if g[0].split()[0] == WANT else "diff")
    real = "-"
    if verdict == "same" and reruns:  # a skip gate v2 would not make: check it for real
        f = os.path.join(MUT, rel)
        open(f, "w").write("\n".join(newlines))
        try:
            r = capped([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE", ID], 150, MUT)
            real = "HANG" if r is None else ("FAIL" if re.search(r"^(FAILED|ERROR) ", r[0], re.M) else "pass")
        finally:
            open(f, "w").write("\n".join(lines))
    done += 1
    key = verdict + ("+rerun" if reruns else "") + ("" if real == "-" else " real " + real)
    tally[key] = tally.get(key, 0) + 1
    print(f"{rel}:{ln} {old.strip()[:50]!r} -> {new.strip()[:50]!r} gate {verdict} reruns {len(reruns)} "
          f"{[x for x in reruns if x[1] != 'True']} real {real} {time.time() - t0:.0f}s", flush=True)
print("done", done, tally)
