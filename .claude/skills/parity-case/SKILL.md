---
name: parity-case
description: Use when adding, changing or xfail-marking a case in parity/cases.json (the cross-language contract fixture run by Go, Python, .NET and Node.js suites), or when a language lags a contract change.
---

# Parity cases (otel-libs)

Format and runner semantics: [parity/README.md](../../../parity/README.md);
decision: ADR-008. Read the README before writing a case.

## Rules
- **Never delete a case to make CI green.** A lagging language gets a status:
  `xfail:<task-id>` (strict — it fails when it starts passing, so flip it to
  `pass` in the same change) or `skip:<reason>` (`skip:phase-b` Node.js,
  `xfail:phase-c` .NET, `skip:no-upstream-…` for documented upstream gaps).
- `<task-id>` must exist in an active `specs/*/tasks.md` and be open —
  `tools/spec_lint.py` checks it.
- Cite the requirement id (R4, D1 row, SR8…) in the case `id`, `note` or
  `requirement` field so `tools/trace_matrix.py` counts it.
- One behavior per case; assert the **resolved value**, not only a warning.

## Editing safely
- Write atomically: load JSON, modify, write to a temp file in the same
  directory, `os.replace` — never edit in place while another agent or test
  run may read it. Keep the existing formatting (diff should show only your
  case).
- Run both suites after the edit:
  `docker compose -f testing/docker-compose.yml run --rm go-tests` and
  `… run --rm python-tests`; they run every case.
- Update the case count wherever docs state it (README parity section,
  CHANGELOG Internal) — grep for the old number.
