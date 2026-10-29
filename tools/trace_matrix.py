#!/usr/bin/env python3
"""Requirement -> test traceability matrix for the active specs (RT5, T-D4).

Usage:
  python tools/trace_matrix.py [--root DIR] [--out FILE] [--check] [--allow FILE]
  python tools/trace_matrix.py --gaps          # print uncovered SHALL ids (for the allow-list)

Requirements: every id defined in an active spec's requirements.md (active =
not linked from the "Shipped (historical" item of specs/README.md), in one of
these forms:
  "### R1 — ...", "### SR4 — ..."          (heading, levels 2-4)
  "- **G3** ...", "- **RT2 Guard** ..."    (bold bullet)
with a prefix R, G, P, SR, RT or JS. A requirement's text runs to the next
definition or heading; it is a SHALL requirement when that text contains
"SHALL".

Citations (word-bounded id, T-D4):
  * Go test files: **/*_test.go
  * Python tests: python/tests/**/*.py
  * tools/tests/**/*.py (tests of these tools)
  * Node.js tests: nodejs/**/test/**/*.{cjs,mjs,js,ts} (node_modules excluded)
  * parity/cases.json: a case's "id", "requirement" or "note" field, counted
    only when the case passes in at least one language (an xfail/skip-only
    case is not evidence)
  * testing/e2e/** text files (go.sum excluded)

In the test files (the Go, Python and tools test globs above) only lines from
the first test definition on count: "func Test"/"func Fuzz" in Go, "def test_"
(sync or async) or a test class ("class Test...", "class ...Test") in Python;
in Node.js files (.cjs/.mjs/.js/.ts) a "test(", "it(", "describe(" or "suite("
call (also with .skip/.only/.todo) or a "t.test(" subtest.
A file-header comment or module docstring that names an id is a claim, not an
asserting test, and is ignored; a test file with no test definition cites
nothing. Cite an id in the test function, its docstring or its class.

--check exits 1 when a SHALL requirement has no citation and is not listed in
the allow-list, or when an allow-list entry is stale (id unknown or now
covered). Allow-list (tools/trace-allow.txt): "<ID> <reason>" per line, "#"
starts a comment line.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from speclib import REPO_ROOT, active_spec_dirs, rel

ID_PATTERN = r"(?:SR|RT|JS|R|G|P)\d+"
DEF_HEADING_RE = re.compile(rf"^#{{2,4}}\s+({ID_PATTERN})\b")
DEF_BULLET_RE = re.compile(rf"^\s*[-*]\s+\*\*({ID_PATTERN})\b")
HEADING_RE = re.compile(r"^#{1,6}\s")
# First line of an asserting test; citations above it (file headers) do not count.
TEST_START_RE = re.compile(
    r"^\s*(?:func\s+(?:Test|Fuzz)\w*\s*\("
    r"|(?:async\s+)?def\s+test_\w*"
    r"|class\s+(?:Test\w*|\w+Test)\b)")
# Node.js test files only (node:test / mocha-style starts).
NODE_TEST_START_RE = re.compile(
    r"^\s*(?:(?:test|it|describe|suite)(?:\.(?:skip|only|todo))?|t\.test)\s*\(")
TEXT_SUFFIXES = {".go", ".py", ".sh", ".yaml", ".yml", ".md", ".json", ".txt"}
NODE_TEST_SUFFIXES = {".cjs", ".mjs", ".js", ".ts"}


def id_regex(ids: list[str]) -> re.Pattern[str]:
    alt = "|".join(sorted(map(re.escape, ids), key=len, reverse=True))
    # Not part of a longer identifier or a base64-like token.
    return re.compile(rf"(?<![\w+=.-])({alt})(?![\w+=])")


@dataclass
class Requirement:
    rid: str
    spec: str
    line: int
    shall: bool
    citations: list[str] = field(default_factory=list)


def parse_requirements(root: Path) -> dict[str, Requirement]:
    reqs: dict[str, Requirement] = {}
    for d in active_spec_dirs(root):
        path = d / "requirements.md"
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        current: Requirement | None = None
        body: list[str] = []

        def close() -> None:
            if current is not None:
                current.shall = "SHALL" in "\n".join(body)

        for i, line in enumerate(lines):
            m = DEF_HEADING_RE.match(line) or DEF_BULLET_RE.match(line)
            if m or HEADING_RE.match(line):
                close()
                current, body = None, []
            if m:
                rid = m.group(1)
                if rid in reqs:
                    raise SystemExit(f"{rel(root, path)}:{i + 1}: requirement {rid} "
                                     f"already defined in {reqs[rid].spec}")
                current = Requirement(rid, d.name, i + 1, False)
                reqs[rid] = current
            if current is not None:
                body.append(line)
        close()
    return reqs


def test_files(root: Path) -> set[Path]:
    """Unit-test files, where only lines inside test definitions count."""
    files = {p for p in root.rglob("*_test.go") if "/vendor/" not in p.as_posix()}
    files |= set((root / "python" / "tests").rglob("*.py"))
    files |= set((root / "tools" / "tests").rglob("*.py"))
    files |= node_test_files(root / "nodejs")
    return files


def node_test_files(nodejs: Path) -> set[Path]:
    """nodejs/**/test/**/*.{cjs,mjs,js,ts}; node_modules is never descended into."""
    found: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(nodejs):
        dirnames[:] = [d for d in dirnames if d != "node_modules"]
        here = Path(dirpath)
        if "test" not in here.relative_to(nodejs).parts:
            continue
        found |= {here / f for f in filenames if Path(f).suffix in NODE_TEST_SUFFIXES}
    return found


def citation_files(root: Path) -> list[Path]:
    files = test_files(root)
    e2e = root / "testing" / "e2e"
    files |= {p for p in e2e.rglob("*")
              if p.is_file() and p.suffix in TEXT_SUFFIXES and p.name != "go.sum"}
    return sorted(files)


def first_test_line(lines: list[str], node: bool = False) -> int | None:
    """Index of the first test definition, or None when the file has none.

    node=True uses the Node.js markers (test(, it(, describe(, …) instead of
    the Go/Python ones.
    """
    rx = NODE_TEST_START_RE if node else TEST_START_RE
    for i, line in enumerate(lines):
        if rx.match(line):
            return i
    return None


def collect_citations(root: Path, reqs: dict[str, Requirement]) -> None:
    if not reqs:
        return
    rx = id_regex(list(reqs))
    tests = test_files(root)
    for path in citation_files(root):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        start = 0
        if path in tests:
            first = first_test_line(lines, node=path.suffix in NODE_TEST_SUFFIXES)
            if first is None:
                continue
            start = first
        for n, line in enumerate(lines[start:], start + 1):
            for rid in set(rx.findall(line)):
                reqs[rid].citations.append(f"{rel(root, path)}:{n}")
    cases_path = root / "parity" / "cases.json"
    if cases_path.is_file():
        cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
        for case in cases:
            if "pass" not in (case.get("status") or {}).values():
                continue
            blob = " ".join(str(case.get(k, "")) for k in ("id", "requirement", "note"))
            for rid in set(rx.findall(blob)):
                reqs[rid].citations.append(f"parity:{case['id']}")


def load_allow(path: Path) -> dict[str, str]:
    allow: dict[str, str] = {}
    if not path.is_file():
        return allow
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            raise SystemExit(f"{path}:{n}: expected '<ID> <reason>'")
        allow[parts[0]] = parts[1].strip()
    return allow


def _sort_key(rid: str) -> tuple[str, int]:
    m = re.match(r"([A-Z]+)(\d+)", rid)
    assert m
    return (m.group(1), int(m.group(2)))


def render(reqs: dict[str, Requirement], allow: dict[str, str]) -> str:
    out = ["# Requirement traceability (generated by tools/trace_matrix.py)", "",
           "| ID | Spec | SHALL | Citations | Status |", "|---|---|---|---|---|"]
    for rid in sorted(reqs, key=_sort_key):
        r = reqs[rid]
        cites = ", ".join(f"`{c}`" for c in r.citations[:3])
        if len(r.citations) > 3:
            cites += f" (+{len(r.citations) - 3})"
        if r.citations:
            status = "covered"
        elif rid in allow:
            status = f"allow-listed: {allow[rid]}"
        elif r.shall:
            status = "**UNCOVERED**"
        else:
            status = "no SHALL (not gated)"
        out.append(f"| {rid} | {r.spec} | {'yes' if r.shall else 'no'} | "
                   f"{cites or '—'} | {status} |")
    shall = [r for r in reqs.values() if r.shall]
    covered = [r for r in shall if r.citations]
    out += ["", f"SHALL requirements: {len(shall)}; cited: {len(covered)}; "
                f"allow-listed: {sum(1 for r in shall if not r.citations and r.rid in allow)}; "
                f"uncovered: {sum(1 for r in shall if not r.citations and r.rid not in allow)}."]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=REPO_ROOT)
    ap.add_argument("--out", type=Path, help="write the matrix here instead of stdout")
    ap.add_argument("--check", action="store_true", help="fail on uncovered SHALL requirements")
    ap.add_argument("--gaps", action="store_true", help="print uncovered SHALL ids, one per line")
    ap.add_argument("--allow", type=Path, default=None,
                    help="allow-list (default: tools/trace-allow.txt under --root)")
    args = ap.parse_args(argv)
    root = args.root.resolve()

    reqs = parse_requirements(root)
    collect_citations(root, reqs)
    allow = load_allow(args.allow or root / "tools" / "trace-allow.txt")

    if args.gaps:
        for rid in sorted(reqs, key=_sort_key):
            if reqs[rid].shall and not reqs[rid].citations:
                print(f"{rid} {reqs[rid].spec}")
        return 0

    matrix = render(reqs, allow)
    if args.out:
        args.out.write_text(matrix, encoding="utf-8")
    elif not args.check:
        sys.stdout.write(matrix)

    if not args.check:
        return 0
    errors = []
    for rid in sorted(reqs, key=_sort_key):
        r = reqs[rid]
        if r.shall and not r.citations and rid not in allow:
            errors.append(f"UNCOVERED {rid} ({r.spec}, requirements.md:{r.line}): "
                          "cite it in a test or add it to the allow-list with a reason")
    for rid in sorted(allow):
        if rid not in reqs:
            errors.append(f"STALE {rid}: allow-list entry for an id no active spec defines")
        elif reqs[rid].citations:
            errors.append(f"STALE {rid}: now cited ({reqs[rid].citations[0]}); remove the entry")
    print(matrix.strip().splitlines()[-1])
    for e in errors:
        print(e)
    print(f"trace --check: {'FAIL' if errors else 'OK'} ({len(errors)} error(s))")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
