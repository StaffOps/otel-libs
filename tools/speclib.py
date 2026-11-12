"""Shared helpers for the spec tools (spec_lint.py, trace_matrix.py).

Stdlib only (specs/repo-tooling/design.md T-D3).

Active specs: every directory under ``specs/`` that holds a ``requirements.md``,
``design.md`` or ``tasks.md``, except ``decisions/`` and the directories linked
from the "Shipped (historical ..." item of ``specs/README.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_FILES = ("requirements.md", "design.md", "tasks.md")

_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
_LIST_ITEM_RE = re.compile(r"^\s*(?:\d+\.|[-*])\s+")


def specs_dir(root: Path) -> Path:
    return root / "specs"


def shipped_spec_dirs(root: Path) -> set[str]:
    """Directory names linked from the "Shipped (historical" list item."""
    readme = specs_dir(root) / "README.md"
    lines = readme.read_text(encoding="utf-8").splitlines()
    shipped: set[str] = set()
    in_item = False
    for line in lines:
        if _LIST_ITEM_RE.match(line):
            in_item = "Shipped (historical" in line
        elif not line.strip() or line.startswith("#"):
            in_item = False
        if in_item:
            for target in _LINK_RE.findall(line):
                if target.endswith("/") and "://" not in target:
                    shipped.add(target.strip("/").split("/")[0])
    return shipped


def active_spec_dirs(root: Path) -> list[Path]:
    shipped = shipped_spec_dirs(root)
    out = []
    for d in sorted(specs_dir(root).iterdir()):
        if not d.is_dir() or d.name in ("decisions",) or d.name in shipped:
            continue
        if any((d / f).is_file() for f in SPEC_FILES):
            out.append(d)
    return out


def rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()
