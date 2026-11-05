#!/usr/bin/env python3
"""Regenerate docs/site/index.md from the root README.md (RT6/RT7, T-D3).

Usage: python tools/regen_index.py [--root DIR] [--check]

--check writes nothing and exits 1 when docs/site/index.md is stale (prints a
unified diff). Without --check the file is rewritten.

Rewrite rules (reverse-engineered from the A13 generator):
  * A generated-file comment and a blank line are prepended.
  * Markdown links "[text](target)" with an absolute URL, "mailto:" or a
    pure "#anchor" are kept.
  * A directory link ("go/") whose README.md is mirrored in the site
    (docs/site/go/README.md) points to that README, anchor kept.
  * A file link mirrored in the site (docs/site/<path> exists) is kept.
  * Anything else points to the repository on GitHub, branch main:
    "<repo_url>/tree/main/<path>" for directories, ".../blob/main/<path>"
    for files, anchor kept. repo_url comes from mkdocs.yml.
  * The "- [Documentation site](<site_url>)" bullet becomes
    "- [Source repository](<repo_url>)" (the site does not link to itself).

Known gaps — not rewritten, so README.md must not contain them; the tool
fails (exit 2, with line numbers) when it does, instead of emitting a broken
site page:
  * images "![alt](path)" (the inner link would be rewritten as a page link);
  * reference-style links: definitions "[ref]: url" and uses "[text][ref]"
    (the definition's URL is never rewritten);
  * Markdown links inside fenced code blocks (``` or ~~~), which the link
    regex would rewrite although they are code.
Inline code spans are ignored by these checks.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HEADER = ("<!-- Generated from the root README.md (links rewritten for MkDocs). "
          "Edit README.md, then regenerate. -->\n\n")
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
KEEP_RE = re.compile(r"^(https?:|mailto:|#)")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
UNSUPPORTED = (
    (re.compile(r"!\[[^\]]*\]\("), "image"),
    (re.compile(r"^\s{0,3}\[[^\]]+\]:\s*\S"), "reference-style link definition"),
    (re.compile(r"\[[^\]]+\]\[[^\]]*\]"), "reference-style link"),
)


def unsupported(src: str) -> list[str]:
    """README constructs the rewrite rules do not handle (see the module doc)."""
    out = []
    fence = None
    for n, line in enumerate(src.splitlines(), 1):
        f = FENCE_RE.match(line)
        if f:
            if fence is None:
                fence = f.group(1)
            elif f.group(1) == fence:
                fence = None
            continue
        if fence is not None:
            if LINK_RE.search(line):
                out.append(f"README.md:{n}: Markdown link inside a fenced code block")
            continue
        text = INLINE_CODE_RE.sub("", line)
        for rx, what in UNSUPPORTED:
            if rx.search(text):
                out.append(f"README.md:{n}: {what}")
                break
    return out


def mkdocs_value(root: Path, key: str) -> str:
    text = (root / "mkdocs.yml").read_text(encoding="utf-8")
    m = re.search(rf"^{key}:\s*(\S+)\s*$", text, re.MULTILINE)
    if not m:
        raise SystemExit(f"mkdocs.yml: no top-level '{key}:'")
    return m.group(1).strip("'\"")


def render(root: Path) -> str:
    repo_url = mkdocs_value(root, "repo_url").rstrip("/")
    site_url = mkdocs_value(root, "site_url")
    site = root / "docs" / "site"
    src = (root / "README.md").read_text(encoding="utf-8")
    problems = unsupported(src)
    if problems:
        print("regen_index: README.md uses constructs this tool cannot rewrite:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        raise SystemExit(2)

    def rewrite(m: re.Match[str]) -> str:
        text, target = m.group(1), m.group(2)
        if KEEP_RE.match(target):
            return m.group(0)
        path, _, anchor = target.partition("#")
        frag = f"#{anchor}" if anchor else ""
        if path.endswith("/") and (site / path / "README.md").is_file():
            return f"[{text}]({path}README.md{frag})"
        if not path.endswith("/") and (site / path).is_file():
            return m.group(0)
        kind = "tree" if path.endswith("/") or (root / path).is_dir() else "blob"
        return f"[{text}]({repo_url}/{kind}/main/{path}{frag})"

    out = LINK_RE.sub(rewrite, src)
    out = out.replace(f"- [Documentation site]({site_url})",
                      f"- [Source repository]({repo_url})")
    return HEADER + out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=REPO_ROOT)
    ap.add_argument("--check", action="store_true", help="exit 1 if docs/site/index.md is stale")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    target = root / "docs" / "site" / "index.md"
    want = render(root)
    have = target.read_text(encoding="utf-8") if target.is_file() else ""
    if args.check:
        if have == want:
            print("regen_index: docs/site/index.md is up to date")
            return 0
        sys.stdout.writelines(difflib.unified_diff(
            have.splitlines(keepends=True), want.splitlines(keepends=True),
            "docs/site/index.md", "docs/site/index.md (regenerated)"))
        print("regen_index: docs/site/index.md is stale; run python tools/regen_index.py")
        return 1
    if have != want:
        target.write_text(want, encoding="utf-8")
        print("regen_index: docs/site/index.md rewritten")
    else:
        print("regen_index: docs/site/index.md already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
