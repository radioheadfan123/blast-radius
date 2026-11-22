"""Real-commit fuzz (Boss round 3, item 2): the sqlglot commits after the pinned eb11aec on origin/main (first parent),
their sqlglot/ edits only (so the tests they fixed up keep the old expectations and fail where behaviour changed), when
those apply to the pinned tree. Pick with the selector (no gates), then run the whole suite on a patched copy:
unsafe = a test that fails (and passes pristine) which the pick left out. Commits where newdef, rescue or keys fired
run first; ALL=1 runs the rest too.
usage (from ~/code/blast-radius): DATA_DIR=kit/sample SQLGLOT_SRC=$PWD/sqlglot .venv/bin/python wt/b14/tools/ufuzz.py"""
import json, os, re, shutil, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import selector as S  # noqa: E402

W = Path(os.path.expanduser("~/code/blast-radius/scratch"))
PF, MUT = W / "ufz", W / "umut"
PF.mkdir(parents=True, exist_ok=True)
RULES = {"newdef", "rescue"}
tests = json.loads((S.DATA / "tests.json").read_text())["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
ids = {t["number"]: t["id"] for t in tests}
fileof = {t["number"]: t["file"] for t in tests}
every, maps = set(ms), S.load_maps()
env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")


def git(*a, cwd=S.SRC):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)


def failing():
    """Numbers of the tests that fail in MUT, or None when pytest itself broke."""
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-n", "2", "-p", "no:cacheprovider", "--timeout", "300",
                        "--tb=no", "-rfE", "tests/"], cwd=MUT, capture_output=True, text=True, env=env, timeout=3600)
    if r.returncode not in (0, 1):
        return None
    out = r.stdout.split("short test summary info")[-1] if r.returncode == 1 else ""
    hit = set(re.findall(r"tests/[\w/]+\.py(?:::\w+)*", out))
    return {n for n, i in ids.items() if any(i == h or i.startswith(h + "::") for h in hit)}


shutil.rmtree(MUT, ignore_errors=True)
shutil.copytree(S.SRC, MUT, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv"))
cs = git("rev-list", "--reverse", "--first-parent", "eb11aec..origin/main").stdout.split()
rows = []
for c in cs:
    d = git("diff", f"{c}^", c, "--", "sqlglot/").stdout
    if not d.strip():
        continue
    pf = PF / f"{c[:8]}.patch"
    pf.write_text(d)
    if git("apply", "--check", str(pf)).returncode:
        print(c[:8], "does not apply", flush=True)
        continue
    S.START = time.monotonic()
    try:
        A, whys, _ = S.pick(pf, maps, every, ids, ms, fileof, {})
    except Exception as x:  # noqa
        print(c[:8], "pick error", type(x).__name__, x, flush=True)
        continue
    parts = {w.rstrip("0123456789") for y in whys for w in y.split("+")}
    hot = bool(parts & RULES) or any(w.startswith("keys") for w in whys)
    rows.append((not hot, c, pf, A, whys))
    print(c[:8], "hot" if hot else "", whys, len(A), git("log", "-1", "--format=%s", c).stdout.strip()[:70], flush=True)

base = failing()
print("pristine failing", sorted(ids[n] for n in base), flush=True)
unsafe = done = 0
for cold, c, pf, A, whys in sorted(rows, key=lambda r: r[0]):
    if cold and not os.environ.get("ALL"):
        break
    t0 = time.time()
    if subprocess.run(["git", "apply", str(pf)], cwd=MUT).returncode:
        print(c[:8], "apply failed in copy", flush=True)
        continue
    try:
        f = failing()
    finally:
        subprocess.run(["git", "apply", "-R", str(pf)], cwd=MUT)
    done += 1
    if f is None:
        print(c[:8], "pytest broke", flush=True)
        continue
    miss = (f - base) - A
    unsafe += bool(miss)
    print(done, c[:8], "hot" if not cold else "", whys, "picked", len(A), "fail", len(f - base), "caught", len((f - base) & A),
          ("UNSAFE " + str([ids[n] for n in sorted(miss)][:4])) if miss else "ok", f"{time.time() - t0:.0f}s", flush=True)
print("commits", done, "unsafe", unsafe)
