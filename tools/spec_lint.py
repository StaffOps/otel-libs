#!/usr/bin/env python3
"""Lint the spec format rules of specs/README.md (requirement RT4).

Usage: python tools/spec_lint.py [--root DIR] [--allow FILE]

Exit 0 when every finding is allow-listed, 1 otherwise. Stdlib only, Python
>= 3.10 (specs/repo-tooling/design.md T-D3).

Scope. "Active specs" are the directories under specs/ that are not linked
from the "Shipped (historical" item of specs/README.md (see speclib.py).

Rules (finding id in brackets):

[adr-index]
    Every specs/decisions/ADR-NNN-*.md is linked from the specs/README.md
    index, and every ADR link in specs/README.md points to an existing file.

[decision-reversal]
    Every decision in an active design.md carries a "Reversal action".
    Detection:
      * A decision is a level-2 or level-3 heading whose text starts with a
        decision id: "D<n>", "D<n>.<x>", "<PREFIX>-D<n>" (S-D1, T-D1), each
        optionally preceded by "Decision " ("### Decision D1.e: ..."), and
        followed by a separator ("—", "–", ":", " - ", "(") or the end of the
        line. "### D1 edge-case rulings" is therefore NOT a decision heading:
        it is part of the body of D1.
      * Headings inside fenced code blocks (``` or ~~~, closed by the same
        fence character) are ignored.
      * A decision's body runs from its heading to the next heading of the
        same or a higher level, or to the next decision heading of any level.
        So "## D1" ends where "### Decision D1.a" starts, and non-decision
        subsections ("### Shutdown result semantics" under "## D13") belong
        to their parent decision.
      * Satisfied when the body contains "Reversal action" (case-insensitive).
      * Exempt: a heading carrying a reference marker, i.e. a parenthesis
        starting with "reference": "(reference)", "(reference checklist)".
      * Delegated: a heading that links an ADR ("## D3 — Sampler → [ADR-002]")
        or a body line "→ [ADR-NNN](...)" delegates the decision to that ADR;
        it is satisfied when the linked ADR exists and contains a
        "Reversal action".

[xfail-task]
    Every "xfail:<task>" status in parity/cases.json names a task id that
    exists in an active tasks.md table and is not marked done. Task ids are
    the first token of a table row's first cell (A1, A7-go, B, C, S0, T4);
    "phase-<x>" names task "<x>". A row is done when its status cell — the
    column headed "Status" or "State" (case-insensitive) — contains "✅",
    "[x]" or the word "Done". The words elsewhere in the row (a description
    saying "Done when ...") do not count; a table without a Status/State
    column marks no row done.

[no-tbd]
    "TBD" or "TODO" (whole word) in an active spec file. Inline code spans
    and double-quoted text are ignored, so a rule that mentions the words
    ("TBD"/"TODO") does not trip it.

[corrected-date]
    A "Corrected" or "Amended" note in an active spec or an ADR carries an
    ISO date (YYYY-MM-DD) on the same line or the next one. Inline code and
    quoted text are ignored.

[allow-stale]
    An allow-list entry that no longer matches a finding (remove it).

Allow-list (tools/spec-lint-allow.txt): one entry per line,
"<rule> <location> <reason>", whitespace-separated. A line whose first
non-blank character is "#" is a comment; a "#" anywhere else is part of the
entry (locations contain one, path#key, and so may reasons). There are no
trailing comments. <location> is printed by this tool for each finding.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from speclib import REPO_ROOT, active_spec_dirs, rel, specs_dir

DECISION_HEADING_RE = re.compile(
    r"^(#{2,3})\s+(?:Decision\s+)?((?:[A-Z]+-)?D\d+(?:\.[A-Za-z0-9]+)?)"
    r"(?=\s*(?:—|–|:|\s-\s|\(|$))"
)
HEADING_RE = re.compile(r"^(#{1,6})\s+")
REFERENCE_MARKER_RE = re.compile(r"\(reference\b[^)]*\)", re.IGNORECASE)
ADR_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]*ADR-\d{3}[^)\s]*\.md)\)")
DELEGATION_RE = re.compile(r"→\s*\[ADR-\d{3}\]\(([^)\s]+\.md)\)")
REVERSAL_RE = re.compile(r"reversal action", re.IGNORECASE)
TBD_RE = re.compile(r"\b(TBD|TODO)\b")
NOTE_RE = re.compile(r"\b(Corrected|Amended)\b")
ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
INLINE_CODE_RE = re.compile(r"`[^`]*`")
QUOTED_RE = re.compile(r'"[^"\n]*"|“[^”\n]*”')
TASK_ID_RE = re.compile(r"^([A-Z][A-Za-z0-9]*(?:-[a-z0-9]+)?)\b")
DONE_RE = re.compile(r"✅|\[x\]|\bDone\b")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
STATUS_HEADERS = {"status", "state"}


@dataclass(frozen=True)
class Finding:
    rule: str
    location: str
    message: str


def strip_code_and_quotes(line: str) -> str:
    return QUOTED_RE.sub("", INLINE_CODE_RE.sub("", line))


def check_adr_index(root: Path) -> list[Finding]:
    readme = specs_dir(root) / "README.md"
    text = readme.read_text(encoding="utf-8")
    linked = {Path(t).name for t in ADR_LINK_RE.findall(text)}
    files = {p.name for p in (specs_dir(root) / "decisions").glob("ADR-*.md")}
    out = []
    for name in sorted(files - linked):
        out.append(Finding("adr-index", f"specs/decisions/{name}",
                           "ADR not listed in the specs/README.md index"))
    for name in sorted(linked - files):
        out.append(Finding("adr-index", f"specs/README.md#{name}",
                           "index links an ADR file that does not exist"))
    return out


def _adr_has_reversal(design: Path, target: str) -> bool:
    adr = (design.parent / target).resolve()
    return adr.is_file() and bool(REVERSAL_RE.search(adr.read_text(encoding="utf-8")))


def check_decisions(root: Path, design: Path) -> list[Finding]:
    lines = design.read_text(encoding="utf-8").splitlines()
    heads = []  # (index, level, decision id or None)
    fence = None  # the fence character of the open code block
    for i, line in enumerate(lines):
        f = FENCE_RE.match(line)
        if f:
            if fence is None:
                fence = f.group(1)
            elif f.group(1) == fence:
                fence = None
            continue
        if fence is not None:
            continue
        m = HEADING_RE.match(line)
        if m:
            d = DECISION_HEADING_RE.match(line)
            heads.append((i, len(m.group(1)), d.group(2) if d else None))
    out = []
    for n, (i, level, did) in enumerate(heads):
        if did is None:
            continue
        end = len(lines)
        for j, jlevel, jdid in heads[n + 1:]:
            if jlevel <= level or jdid is not None:
                end = j
                break
        heading = lines[i]
        body = "\n".join(lines[i + 1:end])
        if REFERENCE_MARKER_RE.search(heading):
            continue
        if REVERSAL_RE.search(body):
            continue
        targets = ADR_LINK_RE.findall(heading) + DELEGATION_RE.findall(body)
        if targets and all(_adr_has_reversal(design, t) for t in targets):
            continue
        reason = ("linked ADR lacks a Reversal action" if targets
                  else "no \"Reversal action\" and no (reference) marker")
        out.append(Finding("decision-reversal", f"{rel(root, design)}#{did}",
                           f"decision {did} (line {i + 1}): {reason}"))
    return out


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def collect_tasks(root: Path) -> dict[str, tuple[bool, str]]:
    """Task id (lowercase) -> (done, tasks.md path)."""
    tasks: dict[str, tuple[bool, str]] = {}
    for d in active_spec_dirs(root):
        path = d / "tasks.md"
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        status_col: int | None = None  # status column of the current table
        for i, line in enumerate(lines):
            if not line.lstrip().startswith("|"):
                status_col = None
                continue
            if TABLE_SEPARATOR_RE.match(line):
                continue
            cells = _cells(line)
            if i + 1 < len(lines) and TABLE_SEPARATOR_RE.match(lines[i + 1]):
                # Header row: locate the Status/State column.
                names = [c.replace("*", "").strip().lower() for c in cells]
                status_col = next((n for n, c in enumerate(names) if c in STATUS_HEADERS), None)
                continue
            first = cells[0].replace("*", "").replace("~", "").strip()
            m = TASK_ID_RE.match(first)
            if not m:
                continue
            tid = m.group(1)
            if not (any(ch.isdigit() for ch in tid) or len(tid) == 1):
                continue  # header cells such as "Task" / "ID"
            status = cells[status_col] if status_col is not None and status_col < len(cells) else ""
            done = bool(DONE_RE.search(status))
            prev = tasks.get(tid.lower())
            tasks[tid.lower()] = (done or bool(prev and prev[0]), rel(root, path))
    return tasks


def check_xfail_tasks(root: Path) -> list[Finding]:
    cases_path = root / "parity" / "cases.json"
    if not cases_path.is_file():
        return []
    cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
    tasks = collect_tasks(root)
    seen: dict[str, list[str]] = {}
    for case in cases:
        for lang, status in (case.get("status") or {}).items():
            if isinstance(status, str) and status.startswith("xfail:"):
                seen.setdefault(status[len("xfail:"):], []).append(f"{case['id']}[{lang}]")
    out = []
    for task, where in sorted(seen.items()):
        key = task.lower()
        if key.startswith("phase-"):
            key = key[len("phase-"):]
        found = tasks.get(key)
        example = f"{len(where)} statuses, e.g. {where[0]}"
        if found is None:
            out.append(Finding("xfail-task", f"parity/cases.json#xfail:{task}",
                               f"task '{task}' not found in any active tasks.md ({example})"))
        elif found[0]:
            out.append(Finding("xfail-task", f"parity/cases.json#xfail:{task}",
                               f"task '{task}' is marked done in {found[1]} ({example})"))
    return out


def check_text_rules(root: Path, path: Path, *, tbd: bool) -> list[Finding]:
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for i, raw in enumerate(lines):
        line = strip_code_and_quotes(raw)
        if tbd:
            for m in TBD_RE.finditer(line):
                out.append(Finding("no-tbd", f"{rel(root, path)}:{i + 1}",
                                   f"'{m.group(1)}' in an active spec"))
        for m in NOTE_RE.finditer(line):
            window = line + " " + (lines[i + 1] if i + 1 < len(lines) else "")
            if not ISO_DATE_RE.search(window):
                out.append(Finding("corrected-date", f"{rel(root, path)}:{i + 1}",
                                   f"'{m.group(1)}' note without an ISO date"))
    return out


def run(root: Path) -> list[Finding]:
    findings = check_adr_index(root)
    for d in active_spec_dirs(root):
        design = d / "design.md"
        if design.is_file():
            findings += check_decisions(root, design)
        for md in sorted(d.glob("*.md")):
            findings += check_text_rules(root, md, tbd=True)
    for adr in sorted((specs_dir(root) / "decisions").glob("ADR-*.md")):
        findings += check_text_rules(root, adr, tbd=False)
    findings += check_xfail_tasks(root)
    return findings


def load_allow(path: Path) -> dict[tuple[str, str], str]:
    allow: dict[tuple[str, str], str] = {}
    if not path.is_file():
        return allow
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            raise SystemExit(f"{path}:{n}: expected '<rule> <location> <reason>'")
        allow[(parts[0], parts[1])] = parts[2].strip()
    return allow


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=REPO_ROOT)
    ap.add_argument("--allow", type=Path, default=None,
                    help="allow-list (default: tools/spec-lint-allow.txt under --root)")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    allow = load_allow(args.allow or root / "tools" / "spec-lint-allow.txt")

    findings = run(root)
    failed = 0
    used = set()
    for f in findings:
        key = (f.rule, f.location)
        if key in allow:
            used.add(key)
            print(f"allowed  [{f.rule}] {f.location}: {f.message} — {allow[key]}")
        else:
            failed += 1
            print(f"FAIL     [{f.rule}] {f.location}: {f.message}")
    for key in sorted(set(allow) - used):
        failed += 1
        print(f"FAIL     [allow-stale] {key[1]}: allow-list entry for '{key[0]}' matches no finding; remove it")
    print(f"spec_lint: {len(findings)} finding(s), {len(used)} allow-listed, {failed} failing")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
