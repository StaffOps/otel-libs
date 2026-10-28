# Repo tooling — design

**Status**: Draft (2026-10-28). Requirements: [requirements.md](requirements.md).

### T-D1 — Hooks are scripts in `.claude/hooks/`, filtering in the script

**Choice**: each hook is a small POSIX-sh + `jq` script under
`.claude/hooks/`, referenced from `.claude/settings.json` via
`$CLAUDE_PROJECT_DIR`; command matching happens inside the script on
`.tool_input.command`, not in the `if` field.
**Justification**: the `if` filter did not restrict the existing hook in
practice (RT1 evidence); scripts are testable by piping JSON (acceptance).
**Trade-offs**: one process spawn per Bash call (milliseconds).
**When wrong**: `if` filtering proves reliable and cheaper.
**Reversal action**: move the match into `if` and keep the script as body.
**Alternatives**: inline `jq` one-liners (untestable, already failed).

### T-D2 — Guard asks, never denies

**Choice**: `permissionDecision: "ask"` for destructive patterns.
**Justification**: the owner did approve some deletions (history rewrite,
volume cleanup); a human decision is the goal, not a ban.
**When wrong**: approvals are given reflexively.
**Reversal action**: switch specific patterns to `deny`.
**Alternatives**: `permissions.ask` rules (prefix-only matching misses
`cd … && docker …` compound commands).

### T-D3 — Python stdlib tools in `tools/`, run in `python:3.12-slim`

**Choice**: `tools/spec_lint.py`, `tools/trace_matrix.py`, `tools/regen_index.py`
(the `docs/site/index.md` generator, moved from the A13 scratchpad); stdlib
only; CI runs them with the runner's Python, locally via Docker.
*(Amended 2026-10-28, code review M3/M4/LOW: the traceability tool is
`trace_matrix.py`, not `trace.py`, which shadowed the stdlib `trace` module.
The tools support Python ≥ 3.10; CI `spec-checks` runs a 3.10 and a 3.12 leg.
The hook tests need `jq` and `git`, absent from `python:3.12-slim`:
`verify.sh tools-tests` installs both in its throwaway container, and
`REQUIRE_HOOK_TESTS=1` (set there and in CI) turns their skip into a failure.)*
**Justification**: no new dependency; Docker-only rule.
**When wrong**: rules outgrow regexes.
**Reversal action**: adopt a Markdown parser dependency in a dev extra.

### T-D4 — Traceability by citation, with an explicit allow-list

**Choice**: a requirement is covered when its id appears (word-bounded) in a
test file, a parity case `id`/`requirement`/`note`, or an e2e check;
`tools/trace-allow.txt` lists uncovered ids with a reason. First run
generates the list of current gaps for review instead of failing silently.
*(Amended 2026-10-28, code review M3: in unit-test files only citations from
the first test definition on count — `func Test`/`func Fuzz`, `def test_`,
a test class — so a file-header comment or module docstring that names an id
is not coverage. Ids that lost their only citation got one in an asserting
test.)*
**When wrong**: citations without real assertions inflate coverage.
**Reversal action**: require a structured `requirement` field on parity cases
and pytest/go-test markers.

### T-D5 — Skills hold procedure; AGENTS.md stays the source of rules

**Choice**: skills in `.claude/skills/<name>/SKILL.md` describe *how* (steps,
brief templates, commands) and link AGENTS.md / specs for *what*; no rule is
defined only in a skill.
**When wrong**: skills and AGENTS.md diverge.
**Reversal action**: spec lint checks skills only reference existing files.
