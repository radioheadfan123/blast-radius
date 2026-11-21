"""Widen map/lines.json fixtures for files a test class reads once in setUpClass.

The probe credits a file opened in setUpClass only to the first test of the class that ran,
but every test of the class uses what it loaded. So: if a fixture's tests include a test of a
class whose setUpClass reads files, the fixture selects every test of that class.
Usage: python tools/class_fixtures.py <sqlglot checkout> <tests.json>"""
import ast, json, sys
from pathlib import Path

src, tests = Path(sys.argv[1]), json.load(open(sys.argv[2]))["tests"]
lines = Path(__file__).resolve().parent.parent / "map" / "lines.json"
m = json.loads(lines.read_text())


def reads_files(fn):
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if "open" in name or name.startswith("load_"):
                return True
    return False


shared = set()  # "tests/x.py::Class::"
for p in (src / "tests").rglob("*.py"):
    for c in ast.parse(p.read_text()).body:
        if isinstance(c, ast.ClassDef) and any(isinstance(f, ast.FunctionDef) and f.name == "setUpClass" and reads_files(f) for f in c.body):
            shared.add(f"{p.relative_to(src)}::{c.name}::")
print("classes that read files in setUpClass:", sorted(shared))

members = {k: {t["number"] for t in tests if t["id"].startswith(k)} for k in shared}
num_id = {t["number"]: t["id"] for t in tests}
widened = 0
for path, sid in m["fixtures"].items():
    got = set(m["sets"][sid])
    add = set().union(*[members[k] for k in shared if any(num_id[n].startswith(k) for n in got)])
    if add - got:
        m["sets"].append(sorted(got | add))
        m["fixtures"][path] = len(m["sets"]) - 1
        widened += 1
print("fixtures widened:", widened)
lines.write_text(json.dumps(m, separators=(",", ":")))
