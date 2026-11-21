# Blast Radius entry

Picks the sqlglot tests a patch can break.

- `map/lines.json`: built on the pinned commit with `tools/probe.py` (pytest plugin) and `tools/build_map2.py`, from per-test line coverage, the dialects and expression classes each test creates, and the fixture files each test opens.
- `selector.py`: for each edited line, tests that ran it (code inside functions), or tests that used that class or dialect (class-level code). Comment-only edits select nothing. Then splits the chosen tests across workers by duration.
