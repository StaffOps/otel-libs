"""Tests for tools/spec_lint.py, tools/trace_matrix.py and
tools/regen_index.py (RT6 index freshness). Stdlib unittest only:

    python -m unittest discover -s tools/tests
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import regen_index  # noqa: E402
import spec_lint  # noqa: E402
import speclib  # noqa: E402
import trace_matrix as trace_tool  # noqa: E402

README = """\
# Specs

1. [`alpha/`](alpha/) — **active**.
2. Shipped (historical):
   [`old/`](old/) (0.1.0).

| ADR | Decision | Status |
|-----|----------|--------|
| [001](decisions/ADR-001-first.md) | First | Accepted |
"""

ADR_OK = "# ADR-001\n\n## When wrong\n**Reversal action**: undo.\n"


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def run_main(fn, argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(argv)
    return code, buf.getvalue()


class Tree(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write(self.root, "specs/README.md", README)
        write(self.root, "specs/decisions/ADR-001-first.md", ADR_OK)
        write(self.root, "specs/old/design.md", "## D1 — shipped, never linted\n")
        write(self.root, "specs/alpha/tasks.md", """\
            | Task | Covers | Status |
            |---|---|---|
            | A1 Done thing | R91 | ✅ Done 2026-10-28 |
            | A7-go Open thing | R92 | ☐ |
            | C — later phase | R91 | Deferred |
            """)
        write(self.root, "parity/cases.json", json.dumps({"cases": []}))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def rules(self) -> list[tuple[str, str]]:
        return [(f.rule, f.location) for f in spec_lint.run(self.root)]


class ActiveSpecsTest(Tree):
    def test_shipped_dirs_excluded(self) -> None:
        self.assertEqual(speclib.shipped_spec_dirs(self.root), {"old"})
        self.assertEqual([d.name for d in speclib.active_spec_dirs(self.root)], ["alpha"])


class SpecLintTest(Tree):
    """RT4 spec lint rules."""

    def test_clean_tree_passes(self) -> None:
        self.assertEqual(self.rules(), [])
        code, out = run_main(spec_lint.main, ["--root", str(self.root)])
        self.assertEqual(code, 0, out)

    def test_adr_index_both_directions(self) -> None:
        write(self.root, "specs/decisions/ADR-002-unlisted.md", ADR_OK)
        (self.root / "specs/decisions/ADR-001-first.md").unlink()
        self.assertEqual(sorted(self.rules()), [
            ("adr-index", "specs/README.md#ADR-001-first.md"),
            ("adr-index", "specs/decisions/ADR-002-unlisted.md"),
        ])

    def test_decision_detection_and_bodies(self) -> None:
        write(self.root, "specs/alpha/design.md", """\
            # Design

            ## D1 — Contract table
            | a | b |
            ### D1 edge-case rulings
            text without the marker
            ### Decision D1.a: sub-decision
            **Reversal action**: revert.
            ## D2 — Delegated → [ADR-001](../decisions/ADR-001-first.md)
            ## D3 — Body delegation
            Go → [ADR-001](../decisions/ADR-001-first.md).
            ## D4 — Parent with plain subsection
            ### Shutdown semantics
            **Reversal action**: in a non-decision subsection, still D4's body.
            ## D11 — Checklist (reference checklist)
            1. item
            ### S-D5 — Prefixed decision
            no marker here
            ```
            ## D9 — inside a code fence, ignored
            ~~~
            ## D10 — a ~~~ line does not close a ``` fence
            ```
            ~~~markdown
            ## D12 — inside a tilde fence, ignored
            ```
            ~~~
            """)
        self.assertEqual(self.rules(), [
            ("decision-reversal", "specs/alpha/design.md#D1"),
            ("decision-reversal", "specs/alpha/design.md#S-D5"),
        ])

    def test_delegation_to_adr_without_reversal_fails(self) -> None:
        write(self.root, "specs/decisions/ADR-001-first.md", "# ADR-001\nno marker\n")
        write(self.root, "specs/alpha/design.md",
              "## D3 — Sampler → [ADR-001](../decisions/ADR-001-first.md)\n")
        findings = spec_lint.run(self.root)
        self.assertEqual(len(findings), 1)
        self.assertIn("linked ADR lacks", findings[0].message)

    def test_xfail_tasks(self) -> None:
        cases = [
            {"id": "a", "status": {"go": "xfail:A1", "python": "pass"}},
            {"id": "b", "status": {"go": "xfail:A7-go"}},
            {"id": "c", "status": {"dotnet": "xfail:phase-c"}},
            {"id": "d", "status": {"nodejs": "xfail:Z9"}},
            {"id": "e", "status": {"nodejs": "skip:phase-b"}},
        ]
        write(self.root, "parity/cases.json", json.dumps({"cases": cases}))
        found = {f.location: f.message for f in spec_lint.run(self.root)}
        self.assertEqual(set(found), {"parity/cases.json#xfail:A1", "parity/cases.json#xfail:Z9"})
        self.assertIn("marked done", found["parity/cases.json#xfail:A1"])
        self.assertIn("not found", found["parity/cases.json#xfail:Z9"])

    def test_done_only_in_status_column(self) -> None:
        write(self.root, "specs/alpha/tasks.md", """\
            | Task | Status | Notes |
            |:---|:---:|---|
            | A1 Done when the harness ✅ passes | ☐ open | Done elsewhere ✅ |
            | A2 Ship | ✅ 2026-10-28 | |

            | ID | Finding |
            |---|---|
            | N1 Done ✅ | no status column |
            """)
        tasks = spec_lint.collect_tasks(self.root)
        self.assertFalse(tasks["a1"][0])  # "Done"/"✅" outside the status cell
        self.assertTrue(tasks["a2"][0])
        self.assertFalse(tasks["n1"][0])  # no Status/State column: never done
        write(self.root, "specs/alpha/tasks.md", "| Task | Covers | State |\n|--|--|--|\n| B9 x | R1 | Done |\n")
        self.assertTrue(spec_lint.collect_tasks(self.root)["b9"][0])

    def test_allow_list_hash_is_part_of_the_entry(self) -> None:
        allow = write(self.root, "tools/spec-lint-allow.txt",
                      "  # a comment line\nno-tbd specs/x.md#key reason with # inside\n")
        self.assertEqual(spec_lint.load_allow(allow),
                         {("no-tbd", "specs/x.md#key"): "reason with # inside"})

    def test_tbd_and_corrected_notes(self) -> None:
        write(self.root, "specs/alpha/requirements.md", """\
            - fails on "TBD"/"TODO" quoted and `TODO` in code
            - this is TBD
            > **Corrected 2026-10-27.** dated
            > **Amended** after review,
            > on 2026-10-28 (date on the next line)
            *Corrected after review* without a date
            """)
        write(self.root, "specs/decisions/ADR-001-first.md",
              ADR_OK + "> Corrected later.\n\nTBD in an ADR is not this rule's scope.\n")
        self.assertEqual(self.rules(), [
            ("no-tbd", "specs/alpha/requirements.md:2"),
            ("corrected-date", "specs/alpha/requirements.md:6"),
            ("corrected-date", "specs/decisions/ADR-001-first.md:5"),
        ])

    def test_allow_list_and_stale_entries(self) -> None:
        write(self.root, "specs/alpha/design.md", "## D1 — No marker\n")
        allow = write(self.root, "tools/spec-lint-allow.txt", """\
            # comment
            decision-reversal specs/alpha/design.md#D1 owner decision
            no-tbd specs/alpha/x.md:1 fixed long ago
            """)
        code, out = run_main(spec_lint.main, ["--root", str(self.root)])
        self.assertEqual(code, 1)
        self.assertIn("allowed  [decision-reversal]", out)
        self.assertIn("[allow-stale]", out)
        allow.write_text("decision-reversal specs/alpha/design.md#D1 owner decision\n")
        code, out = run_main(spec_lint.main, ["--root", str(self.root)])
        self.assertEqual(code, 0, out)

    def test_malformed_allow_entry(self) -> None:
        write(self.root, "tools/spec-lint-allow.txt", "decision-reversal\n")
        with self.assertRaises(SystemExit):
            run_main(spec_lint.main, ["--root", str(self.root)])


class TraceTest(Tree):
    """RT5 traceability."""

    def setUp(self) -> None:
        super().setUp()
        write(self.root, "specs/alpha/requirements.md", """\
            ## Requirements
            ### R91 — Heading form
            WHEN x THEN it SHALL y.
            ### R910 — Not gated
            Informational only.
            ### R911 — Not gated, not cited
            Informational only.
            - **G93** Evidence: z. The helper SHALL log.
            - **RT92 Guard** — the hook SHALL ask.
            - **P99** SHALL be cited by an xfail-only case only.
            ## Out of scope
            - **R94** The helper SHALL be cited by a parity case.
            """)
        write(self.root, "go/x_test.go",
              "// Header: R911 is not covered by a file comment.\n"
              "func TestX(t *testing.T) { // covers R91 and R910, not R9100\n}\n")
        write(self.root, "python/tests/test_x.py",
              '"""Module docstring naming P99 is not a citation."""\n'
              'def test_x():\n    """G93 docstring."""\n    blob = "aR4b+R94/"\n')
        write(self.root, "python/tests/conftest.py", "# R911 helper, no test definition\n")
        write(self.root, "testing/e2e/smoke.sh", "# RT92 e2e\n")
        write(self.root, "testing/e2e/go-smoke/go.sum", "h1:R94= R94\n")
        cases = [
            {"id": "c1", "requirement": "R94/D1", "status": {"go": "pass"}},
            {"id": "c2", "requirement": "P99", "status": {"go": "xfail:C"}},
        ]
        write(self.root, "parity/cases.json", json.dumps({"cases": cases}))

    def reqs(self):
        reqs = trace_tool.parse_requirements(self.root)
        trace_tool.collect_citations(self.root, reqs)
        return reqs

    def test_parse_and_cite(self) -> None:
        reqs = self.reqs()
        self.assertEqual(sorted(reqs), ["G93", "P99", "R91", "R910", "R911", "R94", "RT92"])
        self.assertTrue(reqs["R91"].shall)
        self.assertFalse(reqs["R910"].shall)
        self.assertEqual(reqs["R91"].citations, ["go/x_test.go:2"])
        self.assertEqual(reqs["G93"].citations, ["python/tests/test_x.py:3"])
        self.assertEqual(reqs["R911"].citations, [])  # header comment / no-test file ignored
        self.assertEqual(reqs["RT92"].citations, ["testing/e2e/smoke.sh:1"])
        self.assertEqual(reqs["R94"].citations, ["parity:c1"])  # not the base64 blob, not go.sum
        self.assertEqual(reqs["P99"].citations, [])  # xfail-only case is not evidence

    def test_check_uncovered_allowed_and_stale(self) -> None:
        code, out = run_main(trace_tool.main, ["--root", str(self.root), "--check"])
        self.assertEqual(code, 1)
        self.assertIn("UNCOVERED P99", out)
        write(self.root, "tools/trace-allow.txt", "# c\nP99 not implemented yet\n")
        code, out = run_main(trace_tool.main, ["--root", str(self.root), "--check"])
        self.assertEqual(code, 0, out)
        write(self.root, "tools/trace-allow.txt", "P99 x\nR91 covered now\nZ9 unknown\n")
        code, out = run_main(trace_tool.main, ["--root", str(self.root), "--check"])
        self.assertEqual(code, 1)
        self.assertIn("STALE R91", out)
        self.assertIn("STALE Z9", out)

    def test_matrix_and_gaps_output(self) -> None:
        out_file = self.root / "matrix.md"
        code, _ = run_main(trace_tool.main, ["--root", str(self.root), "--out", str(out_file)])
        self.assertEqual(code, 0)
        matrix = out_file.read_text()
        self.assertIn("| P99 | alpha | yes | — | **UNCOVERED** |", matrix)
        self.assertIn("no SHALL (not gated)", matrix)
        code, out = run_main(trace_tool.main, ["--root", str(self.root)])
        self.assertIn("| R91 | alpha | yes | `go/x_test.go:2` | covered |", out)
        code, out = run_main(trace_tool.main, ["--root", str(self.root), "--gaps"])
        self.assertEqual(out.split(), ["P99", "alpha"])

    def test_duplicate_id_is_an_error(self) -> None:
        write(self.root, "specs/beta/requirements.md", "### R91 — again\nSHALL\n")
        with self.assertRaises(SystemExit):
            trace_tool.parse_requirements(self.root)

    def test_test_start_markers(self) -> None:
        for line in ("func TestA(t *testing.T) {", "func FuzzA(f *testing.F) {", "def test_a():",
                     "    async def test_b(self):", "class TestFoo:", "class FooTest(unittest.TestCase):"):
            self.assertEqual(trace_tool.first_test_line(["// R1", line]), 1, line)
        for line in ("func helper() {", "def make():", "class Fixture:", "# def test_x"):
            self.assertIsNone(trace_tool.first_test_line([line]), line)

    def test_many_citations_are_truncated(self) -> None:
        write(self.root, "go/y_test.go", "func TestY(t *testing.T) {\n// R91\n// R91\n// R91\n}\n")
        code, out = run_main(trace_tool.main, ["--root", str(self.root)])
        self.assertIn("(+1)", out)


class RegenIndexTest(unittest.TestCase):
    """RT6: docs/site/index.md freshness."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write(self.root, "mkdocs.yml",
              "site_name: x\nsite_url: https://docs.example.com/lib/\n"
              "repo_url: https://github.com/example/lib\n")
        write(self.root, "README.md", """\
            [ext](https://example.com) [anchor](#a) [mail](mailto:a@example.com)
            [go](go/) [go anchor](go/#setup) [site file](CONSUMING.md#auth)
            [spec](specs/x.md#d1) [dir](dashboards/) [dir2](parity)
            - [Documentation site](https://docs.example.com/lib/)
            """)
        write(self.root, "docs/site/go/README.md", "go\n")
        write(self.root, "docs/site/CONSUMING.md", "c\n")
        (self.root / "parity").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rewrite_rules(self) -> None:
        out = regen_index.render(self.root)
        self.assertTrue(out.startswith(regen_index.HEADER))
        body = out[len(regen_index.HEADER):]
        self.assertIn("[ext](https://example.com) [anchor](#a) [mail](mailto:a@example.com)", body)
        self.assertIn("[go](go/README.md) [go anchor](go/README.md#setup) [site file](CONSUMING.md#auth)", body)
        self.assertIn("[spec](https://github.com/example/lib/blob/main/specs/x.md#d1)", body)
        self.assertIn("[dir](https://github.com/example/lib/tree/main/dashboards/)", body)
        self.assertIn("[dir2](https://github.com/example/lib/tree/main/parity)", body)
        self.assertIn("- [Source repository](https://github.com/example/lib)", body)

    def test_check_and_write(self) -> None:
        argv = ["--root", str(self.root)]
        code, out = run_main(regen_index.main, argv + ["--check"])
        self.assertEqual(code, 1)
        self.assertIn("stale", out)
        self.assertEqual(run_main(regen_index.main, argv)[0], 0)
        self.assertEqual(run_main(regen_index.main, argv + ["--check"])[0], 0)
        code, out = run_main(regen_index.main, argv)
        self.assertIn("already up to date", out)

    def test_unsupported_constructs_fail_loudly(self) -> None:
        self.assertEqual(regen_index.unsupported(
            "ok `![x](y)` and `a[0][1]` in code\n"
            "![logo](img.png)\n"
            "[ref]: https://example.com\n"
            "see [text][ref]\n"
            "```\n[in fence](x.md)\n```\n"
            "~~~\n```\n[tilde fence](x.md)\n~~~\n"
            "[after fences](x.md)\n"), [
            "README.md:2: image",
            "README.md:3: reference-style link definition",
            "README.md:4: reference-style link",
            "README.md:6: Markdown link inside a fenced code block",
            "README.md:10: Markdown link inside a fenced code block",
        ])
        write(self.root, "README.md", "![logo](img.png)\n")
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as exc:
            regen_index.main(["--root", str(self.root), "--check"])
        self.assertEqual(exc.exception.code, 2)
        self.assertIn("README.md:1: image", err.getvalue())

    def test_missing_mkdocs_key(self) -> None:
        write(self.root, "mkdocs.yml", "site_name: x\n")
        with self.assertRaises(SystemExit):
            regen_index.render(self.root)


if __name__ == "__main__":
    unittest.main()
