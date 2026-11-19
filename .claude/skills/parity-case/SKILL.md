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
  `pass` in the same change) or `skip:<reason>` (`skip:no-upstream-…` for
  documented upstream gaps). A .NET status is `xfail:C<n>` or
  `skip:C<n>-<reason>` for an open task of `specs/dotnet-otel-helper/tasks.md`
  (the .NET runner fails on a skip naming a missing or done task;
  `xfail:phase-c` is retired since C4), or a recorded .NET difference — a row
  of `specs/dotnet-otel-helper/design.md` D-NET15 (`skip:no-upstream-dotnet-…`,
  `skip:no-upstream-rpc-metrics`, `skip:no-prefork-model`,
  `skip:no-detector-variable`); on this branch every `dotnet` status is `pass`
  or a D-NET15 skip. A Node.js `skip` is
  either `skip:B<n>-<reason>` for an open Phase B task in
  `specs/nodejs-otel-helper/tasks.md`, or a recorded Node difference — a row
  of `specs/nodejs-otel-helper/design.md` D-JS14 (`skip:no-upstream-…`); see
  `parity/README.md`.
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
- Run the four suites after the edit:
  `docker compose -f testing/docker-compose.yml run --rm go-tests`,
  `… run --rm python-tests`, `… run --rm node-tests` (Node.js:
  `nodejs/test/parity/runner.test.cjs`; rebuild `fakeotlp-node` first when
  the receiver changed) and `… run --rm dotnet-tests` (.NET:
  `ParityTests`, one driver process per case; one case with
  `-e PARITY_ONLY=<id>,<id>`; rebuild `fakeotlp-dotnet` when the receiver
  changed); they run every case.
- Before a release tag, `testing/verify.sh --release` (`PARITY_RELEASE=1`)
  fails while any status still names an open task (`xfail:*`, a Go/Python
  task skip, `skip:B<n>-…`, .NET `skip:phase-c` or `skip:C<n>-…`).
- Update the case count wherever docs state it (README parity section,
  CHANGELOG Internal) — grep for the old number.
