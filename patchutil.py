"""Parse and apply unified diffs against the pinned sqlglot tree."""
import importlib.util
import os
import re
import sys

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse(text):
    """Return {path: [hunk, ...]}; a hunk is (old_start, [(tag, line), ...]) with tag in ' -+'."""
    files, cur, hunk = {}, None, None
    for line in text.splitlines():
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            p = line[4:].split("\t")[0]
            cur = files.setdefault(p[2:] if p.startswith("b/") else p, [])
            hunk = None
            continue
        m = HUNK.match(line)
        if m and cur is not None:
            hunk = (int(m.group(1)), [])
            cur.append(hunk)
            continue
        if hunk is not None and line[:1] in (" ", "-", "+"):
            hunk[1].append((line[0], line[1:]))
        elif hunk is not None and line == "":
            hunk[1].append((" ", ""))
    return files


def touched_old_lines(hunks):
    """Old-file line numbers that were removed, plus the old line just before each insertion."""
    out = set()
    for start, body in hunks:
        n = start
        for tag, _ in body:
            if tag == "-":
                out.add(n)
                n += 1
            elif tag == "+":
                out.add(max(n - 1, 1))
                out.add(n)
            else:
                n += 1
    return out


def changed_lines(hunks):
    return [(tag, text) for _, body in hunks for tag, text in body if tag != " "]


def apply(old_lines, hunks):
    """old_lines: list of lines without newline. Returns new list."""
    out, pos = [], 0
    for start, body in hunks:
        s = start - 1 if start > 0 else 0
        out.extend(old_lines[pos:s])
        pos = s
        for tag, text in body:
            if tag == " ":
                out.append(old_lines[pos]); pos += 1
            elif tag == "-":
                pos += 1
            else:
                out.append(text)
    out.extend(old_lines[pos:])
    return out


def overlay(patch, src):
    """Import sqlglot from src, with the files patch edits applied into /tmp (read-only disk) and imported instead."""
    over = {}
    for rel, hunks in (parse(open(patch).read()) if patch else {}).items():
        if not rel.endswith(".py"):
            continue  # gates read the pinned fixtures; the selector only gates a test on data files it never opens
        p = os.path.join("/tmp/gate", rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write("\n".join(apply(open(os.path.join(src, rel)).read().splitlines(), hunks)) + "\n")
        mod = rel[:-3].replace("/", ".")
        over[mod.removesuffix(".__init__")] = (p, [os.path.join(src, os.path.dirname(rel))] if mod.endswith(".__init__") else None)

    class Over:
        @staticmethod
        def find_spec(name, path=None, target=None):
            return importlib.util.spec_from_file_location(name, over[name][0], submodule_search_locations=over[name][1]) if name in over else None

    sys.meta_path.insert(0, Over)
    sys.path.insert(0, src)
